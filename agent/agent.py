# Copyright (c) Microsoft. All rights reserved.

"""
NOC Agent — MAF orchestrator delegating to four persisted Foundry Prompt Agents.

**Multi-agent design** (see docs/ARCHITECTURE.md for the full rationale and
migration history from the single-agent/single-toolbox design). Each IQ
surface is now its own **persisted Foundry Prompt Agent**
(`project.agents.create_version(...)`, provisioned by
`scripts/create_foundry_agents.py`), holding exactly one native
`MCPTool(project_connection_id=...)` pointed directly at its own project
Connection -- no toolbox layer needed, since nothing is shared across these
agents:
  - `noc-knowledge-agent`   -> Foundry IQ  (runbooks/tickets/specs KB)
  - `noc-topology-agent`    -> Fabric IQ   (live network topology graph)
  - `noc-threatintel-agent` -> Web IQ      (public vendor/carrier advisories)
  - `noc-comms-agent`       -> Work IQ     (Teams/Outlook: on-call, bridge chatter)

This app runs ONE ephemeral, in-process MAF `Agent` (the "orchestrator"),
always authenticated as the app's own SERVICE identity, with each specialist
exposed as a plain Python tool function (**agents-as-tools**). The
orchestrator's own model performs the routing across specialists exactly as
the old single-agent design routed across IQ tools -- only the granularity of
what's being routed to has changed (a whole persisted agent turn, not a raw
MCP tool call).

Each specialist tool function calls its persisted agent via
`AIProjectClient(allow_preview=True).get_openai_client(agent_name=...)`,
which binds an `openai.OpenAI` client directly to that agent's own endpoint
(`{project_endpoint}/agents/{name}/endpoint/protocols/openai`) -- no
`extra_body={"agent_reference": ...}` needed, the SDK does that internally.

Auth-type matrix (unchanged from the single-agent design):
  - Foundry IQ (knowledge base MCP)   -> project connection, service identity
  - Web IQ (web MCP)                  -> project connection, CustomKeys (x-apikey)
  - Fabric IQ (Fabric Data Agent MCP) -> project connection, UserEntraToken (OBO identity passthrough)
  - Work IQ (Microsoft 365 toolbox)   -> project connection, UserEntraToken (OBO identity passthrough)

**What changed with persisted Prompt Agents vs. the old ephemeral design**:
tool execution moves server-side into Agent Service, so identity passthrough
for the two `UserEntraToken` surfaces becomes Agent-Service-managed **OAuth
identity passthrough**: the credential used to construct the per-call
`AIProjectClient`/OpenAI client for `noc-topology-agent`/`noc-comms-agent`
must be the CALLING USER's OBO token (`_StaticTokenCredential`, below), not
the service credential -- Agent Service attributes the stored OAuth consent
grant to whichever identity signed the call. Consent surfaces as an
`oauth_consent_request` item inside a normal (200 OK) `response.output`, NOT
as a raised MCP error (that was the old client-side-MCP consent shape; see
`_extract_oauth_consent_url`, replacing the old `_extract_a2a_consent_url`).
Every user who calls `noc-topology-agent`/`noc-comms-agent` also needs the
**Foundry Agent Consumer** role at *project* scope (`infra/core/ai/rbac.bicep`)
and must be in the project's own tenant -- cross-tenant token exchange isn't
supported.
"""


import asyncio
import base64
import hashlib
import contextvars
import json
import logging
import os
import re
import time
from typing import Callable, Optional

import httpx
from agent_framework import Agent, Message, tool
from agent_framework.foundry import FoundryChatClient
from azure.ai.projects import AIProjectClient
from azure.core.credentials import AccessToken, TokenCredential
from azure.identity import AzureDeveloperCliCredential, ManagedIdentityCredential
from dotenv import load_dotenv
from microsoft_agents.activity import Activity
from microsoft_agents.hosting.core import Authorization, CardFactory, TurnContext
from microsoft_agents_a365.notifications.agent_notification import NotificationTypes

from agent_interface import AgentInterface

load_dotenv()

# ponytail: GenAI tracing left OFF. azure-ai-projects 2.3.0 + opentelemetry
# 1.40.0 crash with "'NonRecordingSpan' object has no attribute 'attributes'"
# under concurrent multi-specialist fan-out, which _call_specialist's generic
# except then reports to the user as "specialist unavailable". Token/cost
# usage already reaches the run ledger via response.usage in Python, not via
# these spans, so no governance data is lost by leaving this off. Revisit if
# a fixed azure-ai-projects/opentelemetry pairing is confirmed.
os.environ.setdefault("AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING", "false")

_appinsights_conn = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
if _appinsights_conn:
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(connection_string=_appinsights_conn)
    # ponytail: configure_azure_monitor() already attaches a handler to the root
    # logger, so the logging.basicConfig(level=...) below is a documented no-op
    # (it only acts when root has zero handlers) -- root stays at the Python
    # default WARNING, silently dropping every logger.info() call app-wide
    # (including usage_event). Force INFO explicitly instead of relying on basicConfig.
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger(__name__).info("📊 Tracing enabled → Application Insights")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
MODEL_DEPLOYMENT_NAME = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID")

# The 4 persisted Foundry Prompt Agents this orchestrator delegates to (see
# scripts/create_foundry_agents.py, which provisions them, and the module
# docstring). Each agent name maps 1:1 to one IQ surface / connection.
KNOWLEDGE_AGENT_NAME = os.getenv("NOC_KNOWLEDGE_AGENT_NAME", "noc-knowledge-agent")
TOPOLOGY_AGENT_NAME = os.getenv("NOC_TOPOLOGY_AGENT_NAME", "noc-topology-agent")
THREATINTEL_AGENT_NAME = os.getenv("NOC_THREATINTEL_AGENT_NAME", "noc-threatintel-agent")
COMMS_AGENT_NAME = os.getenv("NOC_COMMS_AGENT_NAME", "noc-comms-agent")
INCIDENT_AGENT_NAME = os.getenv("NOC_INCIDENT_AGENT_NAME", "noc-incident-agent")

# fabric_iq/work_iq/rti_iq require the CALLING USER's OBO token (UserEntraToken
# identity passthrough); foundry_iq/web_iq use the app's own service
# credential. See module docstring "What changed with persisted Prompt Agents".
SPECIALIST_AGENTS: dict[str, tuple[str, bool]] = {
    "foundry_iq": (KNOWLEDGE_AGENT_NAME, False),
    "fabric_iq": (TOPOLOGY_AGENT_NAME, True),
    "web_iq": (THREATINTEL_AGENT_NAME, False),
    "work_iq": (COMMS_AGENT_NAME, True),
    "rti_iq": (INCIDENT_AGENT_NAME, True),
}
RUN_LEDGER_AGENT_NAME = "noc-agent"

# OBO token scope used to call each specialist agent's endpoint as the
# calling Teams user (fabric_iq/work_iq only). Agent Service performs its own
# server-side OAuth exchange from this identity to each UserEntraToken tool
# connection's real audience, so a single ai.azure.com-scoped token is enough
# for both user-scoped specialists -- no separate per-tool token exchange
# needed.
FOUNDRY_USER_TOKEN_SCOPES = os.getenv("FOUNDRY_USER_TOKEN_SCOPES", "https://ai.azure.com/.default")

# Separate OBO scope for outbound email (agent.notifications / broadcast_incident_update).
# Requires delegated Mail.Send consented on the agentic identity the same way
# Mail.Read already is for Work IQ -- see docs/OUTBOUND_NOTIFICATIONS.md.
GRAPH_MAIL_TOKEN_SCOPES = os.getenv("GRAPH_MAIL_TOKEN_SCOPES", "https://graph.microsoft.com/.default")

# ponytail: each specialist call is a full model turn on Agent Service, so
# the orchestrator's own wall-clock cap has to cover several sequential/
# fanned-out specialist round trips, not one MCP tool call.
AGENT_RUN_TIMEOUT_SECONDS = float(os.getenv("AGENT_RUN_TIMEOUT_SECONDS", "90"))

ORCHESTRATOR_INSTRUCTIONS = """You are the NOC/NOA network operations orchestrator for a
telecom provider. You help on-call engineers triage incidents such as fibre cuts, router
failures, and amplifier faults by delegating to five specialist agents. Call as many of them
as the question needs, in any order, and synthesize their answers yourself -- each specialist
only sees the single question you send it, not the rest of the conversation.

Delegate as follows:
- ask_knowledge_agent (Foundry IQ): runbooks, equipment specs, infra specs, and past ticket
  history. Use it for "how do we handle X" and "what's the spec / history for Y" questions.
- ask_topology_agent (Fabric IQ): the live network topology graph -- core routers, transport
  links, physical conduits, amplifier sites, services, and SLA policies. Use it for blast-radius
  questions ("what depends on link X", "what conduit does this link ride on", "is any other
  link sharing that same conduit"). ALWAYS check for shared-conduit risk when analyzing a
  physical fault -- two logically independent links can still share one physical conduit,
  which is the most common non-obvious root cause of a "redundant" path also failing.
  This tool's connection is verified working end-to-end. If it returns no data or an empty
  result for a specific query, that means no matching entity/relationship exists in the
  topology for what was asked -- say so plainly (e.g. "no data found for X in the network
  topology") and do NOT claim there was a connection/access/technical problem reaching
  Fabric IQ. Only report an actual connection/consent problem if the tool call itself
  errors out (e.g. a Fabric consent/authorization requirement -- relay the consent
  instructions verbatim to the user in that case).
- ask_threatintel_agent (Web IQ): live public information -- vendor advisories, carrier status
  pages, breaking news about an outage. Use it ONLY to corroborate or find public information
  directly relevant to a NOC/NOA network-operations question (equipment vendor advisories,
  carrier/fibre outage news, etc.). If the user asks about public/web data unrelated to the
  NOC/NOA domain (e.g. general news, unrelated companies, personal topics), do NOT call it --
  politely decline and explain you're scoped to network-operations incidents for this
  telecom provider.
- ask_comms_agent (Work IQ): the user's Teams/Outlook context -- bridge chatter, on-call
  roster, pending change approvals. Use it to answer "who is on-call", "what's being discussed
  on the incident bridge", or to draft a status update. If it requires consent, relay the
  consent URL to the user verbatim.
- ask_incident_agent (RTI): raw incident telemetry and alert evidence from Eventhouse --
  exact alert timelines, per-sensor optical readings, suppressed alerts, and detection-vs-
  acknowledgement latency. Use it for "what actually happened, when, and by how much"
  questions. Prefer it over ask_knowledge_agent when you need machine-generated evidence
  rather than written runbook / ticket narrative; use ask_knowledge_agent instead for
  "how do we handle X", "what does the runbook say", or past-ticket narrative questions.
  Do NOT reflexively call both -- only call both if you truly need both the raw evidence
  and the written procedure/history.

Scope discipline is mandatory: when the user asks only for blast radius, dependencies,
alternate paths, or shared-conduit exposure, call only ask_topology_agent. Do not call RTI,
Foundry IQ, Web IQ, or Work IQ unless the question separately requests telemetry, history,
public information, or collaboration context. Likewise, use exactly one specialist for each
of the focused single-domain test prompts; reserve multi-specialist fan-out for explicitly
combined questions.

Always cite which specialist grounded each factual claim. When summarizing an incident,
report: (1) blast radius (services + SLA exposure), (2) the incident-telemetry evidence
(timeline, exact readings, latency, suppression), (3) any shared-conduit or other non-obvious
compounding risk, (4) the relevant runbook step, (5) any live vendor advisory, and (6)
on-call / bridge context, in that order, clearly labeled. Say plainly when a specialist was
unavailable or returned nothing rather than inventing an answer.
"""


# =============================================================================
# Auth helpers
# =============================================================================


def _get_service_credential():
    """Return the app-level (non-user) credential used for the model AND all 4 IQ tools.

    WEBSITE_INSTANCE_ID is set by the Azure App Service platform itself (Linux
    and Windows, all SKUs) and is the standard way to detect "running inside App
    Service" -- unlike the previous FOUNDRY_HOSTING_ENVIRONMENT check, which was
    never actually set by anything in this repo's infra and caused the deployed
    container to fall through to AzureDeveloperCliCredential, which shells out to
    the `azd` binary. That binary doesn't exist in the App Service container, so
    every credential.get_token() call failed with
    `CredentialUnavailableError: Azure Developer CLI could not be found.`
    """
    if "WEBSITE_INSTANCE_ID" in os.environ or "IDENTITY_ENDPOINT" in os.environ:
        client_id = os.getenv("AZURE_CLIENT_ID")
        return ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
    return AzureDeveloperCliCredential(tenant_id=AZURE_TENANT_ID, process_timeout=60)


def _jwt_exp_epoch(token: str, default_ttl_seconds: float = 300.0) -> float:
    """Best-effort JWT `exp` claim (unverified -- we already trust this token, we're
    only reading its own stated expiry to know when to stop reusing it from cache).
    Falls back to a short default TTL if the token isn't a standard 3-part JWT.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # restore stripped base64url padding
        return float(json.loads(base64.urlsafe_b64decode(payload_b64))["exp"])
    except Exception:  # noqa: BLE001
        return time.time() + default_ttl_seconds


def _run_ledger_base_url() -> str:
    return os.getenv("RUN_LEDGER_BASE_URL", "").strip().rstrip("/")


def _run_ledger_subscription_key() -> str:
    """Ocp-Apim-Subscription-Key for the APIM `/ledger` pass-through API.

    The run-ledger Container App has internal-only ingress (VNet-reachable
    only); App Service is not VNet-integrated, so RUN_LEDGER_BASE_URL points
    at the APIM gateway's `/ledger` path (see gateway/infra/core/apim/apim.bicep
    `runLedgerApi`), which requires this key when clientAuthMode=subscription-key.
    """
    return os.getenv("RUN_LEDGER_SUBSCRIPTION_KEY", "").strip()


def _run_ledger_headers() -> dict[str, str]:
    key = _run_ledger_subscription_key()
    return {"Ocp-Apim-Subscription-Key": key} if key else {}


class _RunHaltedError(Exception):
    """Raised when the run ledger's precall decision is halt/queue.

    Mirrors the shape `_extract_run_halt_reason` already knows how to read
    off an HTTP 403/429 (the APIM-side halt path for any traffic that DOES
    cross the gateway); this is the same signal, raised directly, for calls
    this app makes straight to the run ledger's `/v1/precall` API.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def _run_ledger_precall(
    run_id: str,
    agent_name: str,
    step: str,
    model: str,
    est_input_tokens: int,
    max_output_tokens: int = 1024,
    prompt: str = "",
) -> Optional[dict]:
    """Best-effort precall decision from the run ledger. Returns None (== "allow, no
    ledger opinion") if the ledger is unreachable/unconfigured -- never blocks a turn
    on ledger availability by itself.
    """
    base_url = _run_ledger_base_url()
    if not base_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=_run_ledger_timeout_seconds()) as client:
            response = await client.post(
                f"{base_url}/v1/precall",
                headers=_run_ledger_headers(),
                json={
                    "run_id": run_id,
                    "agent": agent_name,
                    "step": step,
                    "model": model,
                    "est_input_tokens": max(0, est_input_tokens),
                    "max_output_tokens": max_output_tokens,
                    "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                },
            )
            response.raise_for_status()
            return response.json()
    except Exception as exc:  # noqa: BLE001 -- best-effort only, never break the turn
        logger.warning("⚠️ Run ledger precall unavailable, continuing without a decision: %s", exc)
        return None


async def _run_ledger_postcall(
    run_id: str,
    reservation_id: Optional[str],
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    failed: bool = False,
) -> None:
    """Best-effort postcall usage report. Fire-and-forget from the caller's point of
    view (never raises) -- a missed postcall degrades run-ledger accuracy, not the turn.
    """
    base_url = _run_ledger_base_url()
    if not base_url or not reservation_id:
        return
    payload: dict = {"run_id": run_id, "reservation_id": reservation_id}
    if failed:
        payload["failed"] = True
    else:
        payload.update({"input_tokens": input_tokens, "output_tokens": output_tokens, "model": model})
    try:
        async with httpx.AsyncClient(timeout=_run_ledger_timeout_seconds()) as client:
            response = await client.post(
                f"{base_url}/v1/postcall", headers=_run_ledger_headers(), json=payload
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 -- best-effort only, never break the turn
        logger.warning("⚠️ Run ledger postcall failed (best-effort only): %s", exc)


def _apply_precall_decision(decision: Optional[dict]) -> Optional[str]:
    """Translate a precall decision into either None (proceed) or a reservation id
    to use, raising `_RunHaltedError` for halt/queue. Kept separate from the mutate
    (max_output_tokens override) handling so callers that can't honor a mutate still
    get the halt/queue enforcement.
    """
    if not decision:
        return None
    action = decision.get("action")
    if action == "halt":
        raise _RunHaltedError(decision.get("halt_reason") or "Run budget or policy exhausted.")
    if action == "queue":
        raise _RunHaltedError("Run concurrency cap reached. Please retry shortly.")
    return decision.get("reservation_id")


def _run_ledger_timeout_seconds() -> float:
    try:
        return float(os.getenv("RUN_LEDGER_CREATE_TIMEOUT_SECONDS", "2.0"))
    except ValueError:
        return 2.0


def _run_ledger_budget_micros() -> int:
    try:
        return max(1, int(os.getenv("RUN_LEDGER_BUDGET_MICROS", "2000000")))
    except ValueError:
        return 2_000_000


def _run_ledger_policy_set() -> str:
    return os.getenv("RUN_LEDGER_POLICY_SET", "default").strip() or "default"


def _build_run_headers(
    run_token: Optional[str],
    step: Optional[str],
    agent_name: str = RUN_LEDGER_AGENT_NAME,
) -> dict[str, str]:
    if not run_token or not step:
        return {}
    return {
        "x-run-token": run_token,
        "x-agent": agent_name,
        "x-step": step,
    }


def _normalize_headers(headers) -> dict[str, str]:
    if headers is None:
        return {}
    try:
        return {str(k).lower(): str(v) for k, v in headers.items()}
    except Exception:  # noqa: BLE001
        return {}


def _extract_http_error_details(exc: BaseException) -> tuple[int, dict[str, str]] | None:
    queue = [exc]
    seen: set[int] = set()
    while queue:
        current = queue.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        response = getattr(current, "response", None)
        if response is not None and getattr(response, "status_code", None) is not None:
            return int(response.status_code), _normalize_headers(getattr(response, "headers", None))
        status_code = getattr(current, "status_code", None)
        headers = getattr(current, "headers", None)
        if status_code is not None and headers is not None:
            return int(status_code), _normalize_headers(headers)
        queue.extend(arg for arg in getattr(current, "args", ()) if isinstance(arg, BaseException))
        queue.extend(
            nested
            for nested in (getattr(current, "__cause__", None), getattr(current, "__context__", None))
            if isinstance(nested, BaseException)
        )
    return None


def _extract_run_halt_reason(exc: BaseException) -> Optional[str]:
    if isinstance(exc, _RunHaltedError):
        return exc.reason
    details = _extract_http_error_details(exc)
    if details is None:
        return None
    status_code, headers = details
    if status_code == 403 and headers.get("x-run-halt-reason"):
        return headers["x-run-halt-reason"]
    if status_code == 429 and headers.get("retry-after"):
        return f"Concurrency cap reached. Retry after {headers['retry-after']} second(s)."
    return None


def _build_run_halt_activity(halt_reason: str) -> Activity:
    card = {
        "type": "AdaptiveCard",
        "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [
            {
                "type": "TextBlock",
                "text": "⏸️ Run paused by policy",
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": (
                    "This request hit a TokenOps policy at the gateway, so I stopped before "
                    "sending more model or tool calls."
                ),
                "wrap": True,
            },
            {
                "type": "FactSet",
                "facts": [{"title": "Reason", "value": halt_reason}],
            },
        ],
    }
    return Activity(type="message", attachments=[CardFactory.adaptive_card(card)])



def _build_consent_activity(consent_url: str, surface_label: str) -> Activity:
    """Build a Teams Adaptive Card (with an Action.OpenUrl button) for an OAuth consent link.

    Consent URLs are long, ugly, single-use, and short-TTL -- rendering them as
    raw chat text relies on Teams' own auto-linkification (not always reliable
    for URLs this size/shape) and makes the link hard for a user to actually
    tap. An Adaptive Card with a dedicated "Sign in" button is the standard
    Bot Framework/Teams pattern for this and guarantees a tappable action.
    Shared by both `noc-topology-agent` (Fabric IQ) and `noc-comms-agent`
    (Work IQ) -- the two specialists using OAuth identity passthrough.
    """
    card = {
        "type": "AdaptiveCard",
        "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [
            {
                "type": "TextBlock",
                "text": f"🔐 {surface_label} needs your consent",
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": (
                    f"Before I can pull this data from {surface_label}, please sign in and "
                    "grant consent. This link is single-use and expires quickly -- please "
                    "open it right away."
                ),
                "wrap": True,
            },
        ],
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": f"Sign in to {surface_label}",
                "url": consent_url,
            }
        ],
    }
    return Activity(type="message", attachments=[CardFactory.adaptive_card(card)])


def _extract_oauth_consent_url(response) -> Optional[str]:
    """Pull an OAuth identity-passthrough consent link out of a Prompt Agent response, if present.

    Persisted Prompt Agents surface a first-use consent requirement as an
    `oauth_consent_request`-typed item inside a normal (200 OK)
    `response.output` list, under `consent_link` -- NOT as a raised
    exception (that was the old client-side-MCP `a2a_preview` error shape
    this replaces; see docs/ARCHITECTURE.md).
    """
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) == "oauth_consent_request":
            return getattr(item, "consent_link", None)
    return None



def parse_incident_email(subject: str, body: str) -> Optional[dict]:
    """Detect and parse a "[INCIDENT:<stage>]"-tagged notification email.

    Returns the `incident_context` dict for `broadcast_incident_update()` if
    the tag is present, else None (meaning: handle as a normal conversational
    email per the existing EMAIL_NOTIFICATION path).

    The tag is looked for in TWO places, in order:
      1. `subject` (kept for forward-compatibility / other delivery shapes
         that do carry a subject, e.g. a future typed notification entity).
      2. The first non-blank line of `body`. This is the primary, reliable
         convention: live testing showed the A365 email connector's plain
         "message" activity delivers `channel_data` as only
         `{"tenant": {...}, "productContext": "email"}` -- the email Subject
         line is NOT transmitted at all on this path. See
         docs/OUTBOUND_NOTIFICATIONS.md, which documents the tag as the
         first line of the email BODY for this reason.

    Remaining body convention is one `Field: value` pair per line (HTML tags
    stripped first) -- deliberately simple, no YAML/JSON parser dependency.
    """
    clean_body = re.sub("<[^>]+>", "", body or "")
    lines = clean_body.splitlines()

    match = re.match(r"\s*\[INCIDENT:(\w+)\]", subject or "")
    remaining_lines = lines
    if not match:
        for idx, line in enumerate(lines):
            if line.strip():
                match = re.match(r"\s*\[INCIDENT:(\w+)\]", line)
                if match:
                    remaining_lines = lines[idx + 1 :]
                break

    if not match:
        return None
    context: dict = {"LifecycleStage": match.group(1)}
    for line in remaining_lines:
        if ":" in line:
            key, _, value = line.partition(":")
            if key.strip():
                context[key.strip()] = value.strip()
    return context


# =============================================================================
# Agent
# =============================================================================

# Per-turn context, isolated per asyncio Task (set at the top of
# process_user_message's _run_turn(), read by the specialist tool functions
# and by process_user_message itself afterwards). Using contextvars rather
# than threading extra parameters through agent_framework's tool-calling
# machinery (whose function signatures we don't control) keeps the specialist
# tool functions to a plain `(question: str) -> str` shape.
_current_user_token: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_current_user_token", default=None
)
_pending_consent: contextvars.ContextVar[Optional[tuple[str, str]]] = contextvars.ContextVar(
    "_pending_consent", default=None
)
_current_run_token: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_current_run_token", default=None
)
_current_run_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_current_run_id", default=None
)
_current_deadline: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "_current_deadline", default=None
)
_next_run_step: contextvars.ContextVar[Optional[Callable[[], str]]] = contextvars.ContextVar(
    "_next_run_step", default=None
)
# (user_id, display_name) for the turn -- read by _call_specialist to stamp the
# per-request usage_event log line (see below) with who asked, not just the run id.
_current_user_ctx: contextvars.ContextVar[Optional[tuple[str, str]]] = contextvars.ContextVar(
    "_current_user_ctx", default=None
)


class _StaticTokenCredential(TokenCredential):
    """ponytail: minimal TokenCredential wrapping an already-fetched OBO token.

    AIProjectClient only ever calls `get_token()` on the credential it's
    given (to build a bearer-token-provider for the OpenAI client) -- no
    other TokenCredential method is needed here.
    """

    def __init__(self, token: str, expires_on: float):
        self._token = token
        self._expires_on = int(expires_on)

    def get_token(self, *scopes: str, **kwargs) -> AccessToken:  # noqa: ARG002
        return AccessToken(self._token, self._expires_on)


class NocAgent(AgentInterface):
    """A365-hosted MAF orchestrator delegating to 4 persisted Foundry Prompt Agents."""

    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self._service_credential = _get_service_credential()
        self._project_client: Optional[AIProjectClient] = None
        # Specialist agents confirmed to exist at startup (key -> agent name);
        # a missing one degrades that single specialist, not the whole app.
        self._available_agents: dict[str, str] = {}
        # Long-lived orchestrator FoundryChatClient/Agent: always
        # authenticated as the SERVICE identity -- identity passthrough for
        # fabric_iq/work_iq happens per-call, inside each specialist tool
        # function (see _call_specialist).
        self._chat_client: Optional[FoundryChatClient] = None
        self._agent: Optional[Agent] = None
        # Per-user conversation history for context continuity across turns.
        self._conversations: dict[str, list] = {}
        # ponytail: (user_id, scope) -> (token, exp_epoch). _exchange_user_token
        # was re-running the A365 SDK's full OBO broker (several sequential
        # Graph/Observability/ai.azure.com round trips, ~5-6s) on every single
        # turn even when the previous token was still valid -- caching it here
        # cuts that to zero on cache hits.
        self._token_cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._run_token_cache: dict[tuple[str, str], str] = {}

    # -------------------------------------------------------------------
    # LIFECYCLE
    # -------------------------------------------------------------------

    async def initialize(self) -> None:
        """Confirm the 4 persisted Prompt Agents exist, then build the orchestrator.

        Connection resolution and toolbox/tool wiring now happen once, at
        provisioning time, in `scripts/create_foundry_agents.py` -- each
        Prompt Agent already holds its own `MCPTool(project_connection_id=...)`
        baked into its persisted definition. This app only needs to know each
        agent's NAME to call it, so startup here is a cheap existence check
        per agent (degrade that one specialist, not the whole app, if it's
        missing), matching the same "degrade this one tool" pattern the old
        toolbox-resolution code used.
        """
        logger.info("🔌 Initializing NOC orchestrator against %s", PROJECT_ENDPOINT)

        self._project_client = AIProjectClient(
            endpoint=PROJECT_ENDPOINT, credential=self._service_credential, allow_preview=True
        )
        for key, (agent_name, _) in SPECIALIST_AGENTS.items():
            try:
                self._project_client.agents.get(agent_name)
                self._available_agents[key] = agent_name
                logger.info("✅ Specialist agent '%s' (%s) confirmed", agent_name, key)
            except Exception as exc:  # noqa: BLE001 -- degrade this one specialist, not the whole agent
                logger.warning(
                    "⚠️ Specialist agent '%s' (%s) not found -- run "
                    "scripts/create_foundry_agents.py first (%s tool disabled): %s",
                    agent_name,
                    key,
                    key,
                    exc,
                )

        self._chat_client = FoundryChatClient(
            project_endpoint=PROJECT_ENDPOINT,
            model=MODEL_DEPLOYMENT_NAME,
            credential=self._service_credential,
        )
        orchestrator_tools = self._build_orchestrator_tools()
        self._agent = Agent(
            client=self._chat_client,
            name="NocOrchestrator",
            instructions=ORCHESTRATOR_INSTRUCTIONS,
            tools=orchestrator_tools,
            default_options={"store": False},
        )

        logger.info("✅ NOC orchestrator ready with %d specialist tool(s)", len(orchestrator_tools))

    async def cleanup(self) -> None:
        """Close the long-lived project client."""
        if self._project_client is not None:
            try:
                self._project_client.close()
            except Exception:  # noqa: BLE001
                pass
        logger.info("🧹 Agent cleanup completed")

    # -------------------------------------------------------------------
    # AGENTS-AS-TOOLS -- one plain Python tool per specialist Prompt Agent
    # -------------------------------------------------------------------

    def _get_or_create_run_id(self, conversation_id: str, activity_id: str) -> str:
        seed = f"{conversation_id}\n{activity_id}".encode("utf-8")
        return "teams-" + hashlib.sha256(seed).hexdigest()[:24]

    async def _get_or_create_run_token(self, conversation_id: str, activity_id: str) -> Optional[str]:
        base_url = _run_ledger_base_url()
        if not base_url:
            return None

        cache_key = (conversation_id, activity_id)
        cached = self._run_token_cache.get(cache_key)
        if cached:
            return cached

        try:
            async with httpx.AsyncClient(timeout=_run_ledger_timeout_seconds()) as client:
                response = await client.post(
                    f"{base_url}/v1/runs",
                    headers=_run_ledger_headers(),
                    json={
                        "run_id": self._get_or_create_run_id(conversation_id, activity_id),
                        "budget_micros": _run_ledger_budget_micros(),
                        "policy_set": _run_ledger_policy_set(),
                    },
                )
                response.raise_for_status()
            run_token = response.json().get("run_token")
            if isinstance(run_token, str) and run_token:
                self._run_token_cache[cache_key] = run_token
                return run_token
            logger.warning("⚠️ Run ledger returned no run_token for %s", cache_key)
        except Exception as exc:  # noqa: BLE001 -- best-effort only, never break the turn
            logger.warning("⚠️ Run ledger unavailable, continuing without run headers: %s", exc)
        return None

    def _build_orchestrator_tools(self) -> list:
        """Build the orchestrator's tool list: one FunctionTool per available specialist."""
        specs = [
            ("foundry_iq", "ask_knowledge_agent", "Ask the Foundry IQ knowledge-base specialist (runbooks, equipment/infra specs, past ticket history) a question."),
            ("fabric_iq", "ask_topology_agent", "Ask the Fabric IQ network-topology specialist (live topology graph, blast radius, shared-conduit risk) a question."),
            ("web_iq", "ask_threatintel_agent", "Ask the Web IQ specialist (public vendor advisories, carrier status, outage news) a question."),
            ("work_iq", "ask_comms_agent", "Ask the Work IQ specialist (Teams/Outlook: on-call roster, bridge chatter, change approvals) a question."),
            ("rti_iq", "ask_incident_agent", "Ask the RTI incident-telemetry specialist (Fabric Eventhouse: alert timelines, optical readings, detection/ack latency) a question."),
        ]
        tools = []
        for key, tool_name, description in specs:
            if key not in self._available_agents:
                continue
            tools.append(tool(self._make_ask_specialist(key), name=tool_name, description=description))
        return tools

    def _make_ask_specialist(self, key: str):
        """Return a plain `(question: str) -> str` closure bound to one specialist.

        A true closure (not a default-argument trick) so `key` never appears
        as a parameter in the schema the model sees for this tool.
        """

        async def _ask(question: str) -> str:
            return await self._call_specialist(key, question)

        return _ask

    async def _call_topology_graph(self, question: str) -> Optional[str]:
        """Answer focused link blast-radius questions through Fabric Graph directly."""
        workspace_id = os.getenv("FABRIC_WORKSPACE_ID", "").strip()
        graph_model_id = os.getenv("FABRIC_GRAPH_MODEL_ID", "").strip()
        link_match = re.search(r"\bLINK-[A-Z0-9-]+\b", question.upper())
        if not workspace_id or not graph_model_id or not link_match:
            return None
        normalized = question.casefold()
        if not any(term in normalized for term in ("blast radius", "conduit", "depends", "affected")):
            return None

        link_id = link_match.group(0)
        token = await asyncio.to_thread(
            self._service_credential.get_token,
            "https://api.fabric.microsoft.com/.default",
        )
        endpoint = (
            f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}"
            f"/GraphModels/{graph_model_id}/executeQuery?preview=true"
        )
        escaped_link_id = link_id.replace("'", "''")
        queries = {
            "link": (
                "MATCH (l:TransportLink)-[:ORIGINATES_AT]->(o:CoreRouter), "
                "(l)-[:TERMINATES_AT]->(t:CoreRouter), "
                "(l)-[:RIDES_ON]->(c:PhysicalConduit) "
                f"FILTER l.LinkId = '{escaped_link_id}' "
                "RETURN l.LinkId AS LinkId, o.RouterId AS OriginRouter, "
                "t.RouterId AS TerminatingRouter, c.ConduitId AS ConduitId"
            ),
            "shared": (
                "MATCH (target:TransportLink)-[:RIDES_ON]->(c:PhysicalConduit), "
                "(other:TransportLink)-[:RIDES_ON]->(c) "
                f"FILTER target.LinkId = '{escaped_link_id}' "
                "RETURN other.LinkId AS LinkId, c.ConduitId AS ConduitId ORDER BY LinkId"
            ),
            "services": (
                "MATCH (sla:SLAPolicy)-[:COVERS]->(s:Service)-[:DEPENDS_ON]->"
                "(p:MPLSPath)-[:TRAVERSES_LINK]->(l:TransportLink) "
                f"FILTER l.LinkId = '{escaped_link_id}' "
                "RETURN s.ServiceId AS ServiceId, s.CustomerName AS CustomerName, "
                "s.ActiveUsers AS ActiveUsers, sla.SLAPolicyId AS SLAPolicyId, "
                "sla.Tier AS Tier, sla.PenaltyPerHourUSD AS PenaltyPerHourUSD, "
                "p.PathId AS PathId ORDER BY ServiceId, PathId"
            ),
        }
        results: dict[str, list[dict]] = {}
        async with httpx.AsyncClient(timeout=20) as client:
            for name, query in queries.items():
                response = await client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {token.token}"},
                    json={"query": query},
                )
                response.raise_for_status()
                payload = response.json()
                if not str(payload.get("status", {}).get("code", "")).startswith(("00", "01", "02", "03")):
                    raise RuntimeError(f"Fabric Graph query failed: {payload.get('status')}")
                results[name] = payload.get("result", {}).get("data", [])

        if not results["link"]:
            return f"Fabric IQ found no topology entity matching `{link_id}`."
        link = results["link"][0]
        shared_links = sorted(
            row["LinkId"] for row in results["shared"] if row.get("LinkId") != link_id
        )
        service_lines = [
            f"- `{row['ServiceId']}` ({row['CustomerName']}): {row['ActiveUsers']} active users; "
            f"{row['Tier']} SLA `{row['SLAPolicyId']}`; ${row['PenaltyPerHourUSD']:,}/hour; "
            f"path `{row['PathId']}`"
            for row in results["services"]
        ]
        return "\n".join(
            [
                "**Fabric IQ — live topology graph**",
                f"- Link: `{link_id}` (`{link['OriginRouter']}` → `{link['TerminatingRouter']}`)",
                f"- Physical conduit: `{link['ConduitId']}`",
                "- Other links sharing that conduit: "
                + (", ".join(f"`{item}`" for item in shared_links) if shared_links else "none"),
                "- Direct service/SLA exposure:",
                *(service_lines or ["- No dependent services were returned by the topology graph."]),
                "- Shared-conduit risk: "
                + ("a conduit failure also removes the listed logical alternate link(s)." if shared_links else "none found."),
            ]
        )

    async def _call_specialist(self, key: str, question: str) -> str:
        """Invoke one persisted Prompt Agent and return its text answer.

        fabric_iq/work_iq authenticate as the CALLING USER (OAuth identity
        passthrough); foundry_iq/web_iq authenticate as the service identity.
        See module docstring. A fresh `AIProjectClient` is built per call
        (cheap -- no network round trip at construction) so each specialist
        call carries the right identity for THIS turn/user, never a stale one
        from a previous turn.

        Token governance: all 4 specialists call Foundry directly (never through
        APIM -- routing fabric_iq/work_iq's OAuth-identity-passthrough calls
        through a gateway that substitutes its own managed identity would break
        Agent Service's per-user consent attribution). So this app reports usage
        to the run ledger directly instead, uniformly for all 4 specialists,
        using the SDK-level `response.usage` this call already gets back --
        the same precall/postcall contract APIM's own policies use, just made
        from Python rather than from policy XML. See docs/SEQUENCE.md.
        """
        if key == "fabric_iq":
            try:
                direct_answer = await self._call_topology_graph(question)
                if direct_answer:
                    return direct_answer
            except Exception as exc:  # noqa: BLE001 -- fall back to the persisted specialist
                logger.warning("Direct Fabric Graph query failed; falling back to Data Agent: %s", exc)

        agent_name = self._available_agents[key]
        _, needs_user_identity = SPECIALIST_AGENTS[key]
        if needs_user_identity:
            user_token = _current_user_token.get()
            credential = (
                _StaticTokenCredential(user_token, time.time() + 300)
                if user_token
                else self._service_credential
            )
        else:
            credential = self._service_credential

        run_id = _current_run_id.get()
        run_token = _current_run_token.get()
        next_step = _next_run_step.get()
        step = next_step() if next_step else None
        reservation_id: Optional[str] = None
        max_output_tokens: Optional[int] = None  # None == leave the agent's own default alone
        if run_id:
            decision = await _run_ledger_precall(
                run_id=run_id,
                agent_name=agent_name.removesuffix("-agent"),
                step=step or "0",
                model=agent_name,
                est_input_tokens=len(question) // 4,
                max_output_tokens=1024,  # advisory ceiling offered to the ledger's decision, not applied unless it mutates
                prompt=question,
            )
            reservation_id = _apply_precall_decision(decision)  # raises _RunHaltedError on halt/queue
            if decision and decision.get("action") == "mutate" and decision.get("max_output_tokens"):
                max_output_tokens = int(decision["max_output_tokens"])

        project_client = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=credential, allow_preview=True)
        try:
            openai_client = project_client.get_openai_client(agent_name=agent_name)
            extra_headers = (
                _build_run_headers(run_token, step, agent_name.removesuffix("-agent"))
                if run_token and step
                else None
            )
            create_kwargs: dict = {"input": question, "extra_headers": extra_headers}
            deadline = _current_deadline.get()
            if deadline is not None:
                create_kwargs["timeout"] = max(1.0, deadline - time.monotonic())
            if max_output_tokens is not None:
                # Only set when the run ledger has actually asked for a steer-down --
                # never impose a default cap that the agent didn't have before.
                create_kwargs["max_output_tokens"] = max_output_tokens
            response = await asyncio.to_thread(openai_client.responses.create, **create_kwargs)
        except Exception as exc:  # noqa: BLE001 -- degrade this one specialist call, not the whole turn
            if run_id:
                await _run_ledger_postcall(run_id, reservation_id, failed=True)
            if _extract_run_halt_reason(exc):
                raise
            logger.error("❌ Specialist '%s' call failed: %s", agent_name, exc, exc_info=True)
            return f"({agent_name} is currently unavailable: {exc})"
        finally:
            project_client.close()

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        # ponytail: Responses API nests cached/reasoning under *_details; both are
        # optional per model, default 0 rather than raising when a model omits them.
        cached_tokens = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
        reasoning_tokens = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
        model_name = getattr(response, "model", agent_name) or agent_name
        user_id_ctx, user_name_ctx = _current_user_ctx.get() or ("unknown", "unknown")
        # Structured usage_event -> Application Insights customDimensions (via the
        # configure_azure_monitor logging hook set up above), one row per specialist
        # call. Query with a KQL join against the Cosmos `pricing` doc for $ cost;
        # see gateway/app/config-sync-worker/check_usage_detail.py.
        logger.info(
            "usage_event",
            extra={
                "event": "specialist_usage",
                "run_id": run_id or "",
                "user_id": user_id_ctx,
                "user_name": user_name_ctx,
                "agent": agent_name,
                "model": model_name,
                "query": question[:500],
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
                "reasoning_tokens": reasoning_tokens,
            },
        )

        if run_id:
            await _run_ledger_postcall(
                run_id,
                reservation_id,
                model=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        consent_url = _extract_oauth_consent_url(response)
        if consent_url:
            _pending_consent.set((agent_name, consent_url))
            return f"({agent_name} requires the user to sign in first; a consent link has been sent.)"
        return response.output_text or f"({agent_name} returned no answer.)"

    # -------------------------------------------------------------------
    # MESSAGE PROCESSING
    # -------------------------------------------------------------------

    async def process_user_message(
        self,
        message: str,
        auth: Authorization,
        auth_handler_name: Optional[str],
        context: TurnContext,
    ) -> str:
        """Run one turn of the MAF orchestrator, delegating to specialist Prompt Agents."""
        from_prop = context.activity.from_property
        user_id = getattr(from_prop, "id", "default") if from_prop else "default"
        display_name = getattr(from_prop, "name", None) or "unknown"
        logger.info("📨 Message from %s (%s): %s...", display_name, user_id, message[:80])

        foundry_user_token = await self._exchange_user_token(
            auth, auth_handler_name, context, FOUNDRY_USER_TOKEN_SCOPES, user_id=user_id
        )
        history: list[Message] = self._conversations.get(user_id, [])
        history.append(Message("user", [message]))
        conversation = getattr(context.activity, "conversation", None)
        conversation_id = getattr(conversation, "id", "") or user_id
        activity_id = getattr(context.activity, "id", "") or str(len(history))
        run_id = self._get_or_create_run_id(conversation_id, activity_id)
        run_token = await self._get_or_create_run_token(conversation_id, activity_id)
        step_counter = {"value": 0}

        def _next_step() -> str:
            step_counter["value"] += 1
            return str(step_counter["value"])

        async def _run_turn():
            # Isolated per-Task: only visible to this turn's own agent.run()
            # call and the specialist tool functions it invokes, never to a
            # concurrent turn from another user (see contextvars docstring above).
            _current_user_token.set(foundry_user_token)
            _pending_consent.set(None)
            _current_run_token.set(run_token)
            _current_run_id.set(run_id if run_token else None)
            _next_run_step.set(_next_step if run_token else None)
            _current_user_ctx.set((user_id, display_name))

            # Orchestrator's own model call: same direct-to-ledger governance as
            # each specialist (see _call_specialist docstring) -- not routed
            # through APIM, reported to the run ledger directly instead.
            reservation_id: Optional[str] = None
            step = _next_step() if run_token else None
            if run_token:
                est_input_tokens = sum(len(str(getattr(m, "contents", m))) for m in history) // 4
                decision = await _run_ledger_precall(
                    run_id=run_id,
                    agent_name=RUN_LEDGER_AGENT_NAME,
                    step=step or "0",
                    model=MODEL_DEPLOYMENT_NAME,
                    est_input_tokens=est_input_tokens,
                    prompt="\n".join(str(getattr(m, "contents", m)) for m in history),
                )
                reservation_id = _apply_precall_decision(decision)  # raises _RunHaltedError on halt/queue
            try:
                result = await self._agent.run(history)
            except Exception:
                if run_token:
                    await _run_ledger_postcall(run_id, reservation_id, failed=True)
                raise
            if run_token:
                usage = getattr(result, "usage_details", None) or {}
                await _run_ledger_postcall(
                    run_id,
                    reservation_id,
                    model=MODEL_DEPLOYMENT_NAME,
                    input_tokens=usage.get("input_token_count") or 0,
                    output_tokens=usage.get("output_token_count") or 0,
                )
            return result

        run_task = asyncio.ensure_future(_run_turn())
        try:
            response = await asyncio.wait_for(asyncio.shield(run_task), timeout=AGENT_RUN_TIMEOUT_SECONDS)
            pending = _pending_consent.get()
            if pending:
                agent_name, consent_url = pending
                # Send the sign-in Adaptive Card directly (context is already
                # available here) and return an empty string so the caller's
                # own `send_activity(response)` is a no-op -- avoids sending
                # both a card AND a redundant plain-text message.
                await context.send_activity(_build_consent_activity(consent_url, agent_name))
                return ""
            self._conversations[user_id] = history + [Message("assistant", [response.text])]
            return response.text or "I processed your request but couldn't generate a response."
        except asyncio.TimeoutError:
            if not run_task.done():
                run_task.cancel()

                def _swallow(t: "asyncio.Task") -> None:
                    if not t.cancelled():
                        t.exception()

                run_task.add_done_callback(_swallow)
            logger.error(
                "❌ agent.run() exceeded %.0fs watchdog; abandoning this turn",
                AGENT_RUN_TIMEOUT_SECONDS,
            )
            return (
                "Sorry, that request is taking longer than expected. "
                "Please try again -- one of my tools may be slow to respond."
            )
        except Exception as exc:  # noqa: BLE001
            halt_reason = _extract_run_halt_reason(exc)
            if halt_reason:
                await context.send_activity(_build_run_halt_activity(halt_reason))
                return ""
            logger.error("❌ Error processing message: %s", exc, exc_info=True)
            self._conversations.pop(user_id, None)
            return f"Sorry, I encountered an error: {exc}"

    # -------------------------------------------------------------------
    # NOTIFICATION HANDLING (A365 email / Word-comment triggers)
    # -------------------------------------------------------------------

    async def handle_agent_notification_activity(
        self,
        notification_activity,
        auth: Authorization,
        auth_handler_name: Optional[str],
        context: TurnContext,
    ) -> str:
        """Handle A365 notifications by forwarding to the same MAF agent turn."""
        try:
            notification_type = notification_activity.notification_type
            logger.info("📬 Processing notification: %s", notification_type)

            if notification_type == NotificationTypes.EMAIL_NOTIFICATION:
                email = notification_activity.email
                email_subject = getattr(email, "subject", "") or ""
                email_body = getattr(email, "html_body", "") or getattr(email, "body", "")

                # Event-driven incident-lifecycle trigger: an incident-management
                # system emails the monitored inbox with a "[INCIDENT:<stage>]"
                # subject tag instead of a Teams/webhook call needing a stored
                # conversation_id -- inspired by the OnNewEmailV3 connector
                # trigger pattern in Azure-Samples/m365-inbox-serverless-agent-python
                # (this repo's own EMAIL_NOTIFICATION handler is the equivalent
                # building block: already event-driven on new mail, no prior
                # conversation needed). See docs/OUTBOUND_NOTIFICATIONS.md.
                incident_context = parse_incident_email(email_subject, email_body)
                if incident_context is not None:
                    results = await self.broadcast_incident_update(incident_context, auth, auth_handler_name, context)
                    return f"Incident notification broadcast: {results}"

                message = (
                    "You received the following email about a network incident. "
                    "Please review and summarize the blast radius and next steps.\n\n"
                    f"{email_body}"
                )
            elif notification_type == NotificationTypes.WPX_COMMENT:
                comment_text = notification_activity.text or ""
                message = (
                    f"You were mentioned in a Word document comment: {comment_text}\n"
                    "Please review and respond."
                )
            else:
                message = notification_activity.text or f"Notification received: {notification_type}"

            return await self.process_user_message(message, auth, auth_handler_name, context)
        except Exception as exc:  # noqa: BLE001
            logger.error("❌ Notification error: %s", exc)
            return f"Sorry, I encountered an error processing the notification: {exc}"

    # -------------------------------------------------------------------
    # OUTBOUND MULTI-PERSONA EMAIL (proactive, not turn-initiated)
    # -------------------------------------------------------------------

    async def handle_email_message(
        self,
        subject: str,
        body: str,
        auth: Authorization,
        auth_handler_name: Optional[str],
        context: TurnContext,
        sender_email: Optional[str] = None,
    ) -> Optional[str]:
        """Detect a "[INCIDENT:<stage>]"-tagged email delivered as a plain
        "message" activity (channel_id "agents:email") rather than an
        `AgentNotificationActivity` -- a directly-composed/forwarded email
        to the agent's mailbox arrives this way (see host_agent_server.py's
        `on_message`, which has no NOC-specific knowledge and calls this
        hook first). Returns the broadcast summary if the tag matched, else
        None so the caller falls back to normal conversational handling.

        `sender_email`, if given, is CC'd on every persona email sent --
        so whoever emailed the incident report in gets visibility into the
        stakeholder broadcast without being added as a primary NOTIFY_*
        recipient (which stays the configured stakeholder distribution
        list, not the ad-hoc sender).
        """
        incident_context = parse_incident_email(subject, body)
        if incident_context is None:
            return None
        cc_recipients = [sender_email] if sender_email else None
        results = await self.broadcast_incident_update(
            incident_context, auth, auth_handler_name, context, cc_recipients=cc_recipients
        )
        return f"Incident notification broadcast: {results}"

    async def broadcast_incident_update(
        self,
        incident_context: dict,
        auth: Authorization,
        auth_handler_name: Optional[str],
        context: TurnContext,
        personas: Optional[list] = None,
        cc_recipients: Optional[list] = None,
    ) -> dict:
        """Send an incident-lifecycle update to one or more stakeholder personas.

        Reachable only via a resumed (proactive) conversation -- see
        host_agent_server.py's `POST /api/incidents/notify` route and
        docs/OUTBOUND_NOTIFICATIONS.md. Requires the SAME OBO machinery as
        Fabric IQ/Work IQ (`_exchange_user_token`), just scoped to
        `https://graph.microsoft.com/.default` with delegated `Mail.Send`
        consented instead of `Mail.Read` -- see docs/DEPLOYMENT.md.
        """
        from notifications import broadcast  # local import: optional feature, keep agent.py importable without it

        graph_token = await self._exchange_user_token(auth, auth_handler_name, context, GRAPH_MAIL_TOKEN_SCOPES)
        if not graph_token:
            logger.error("❌ No Graph OBO token -- cannot send outbound notifications this turn")
            return {}
        return await broadcast(incident_context, graph_token, personas, cc_recipients=cc_recipients)

    # -------------------------------------------------------------------
    # OBO TOKEN EXCHANGE (Fabric Data Agent + Work IQ require user delegation)
    # -------------------------------------------------------------------

    async def _exchange_user_token(
        self,
        auth: Authorization,
        auth_handler_name: Optional[str],
        context: TurnContext,
        scope: str,
        user_id: Optional[str] = None,
    ) -> Optional[str]:
        """Get a user-delegated token via the AgenticUserAuthorization handler.

        The A365 platform (with inheritable permissions configured on the
        blueprint) provides a user-delegated token through this exchange,
        scoped to `https://ai.azure.com/.default`. This single token is used
        to build the whole per-turn FoundryChatClient/Agent as the calling
        Teams user; Foundry's own service then performs its own server-side
        OBO exchange from this identity to each UserEntraToken-typed tool
        connection's real audience (Fabric IQ -> api.fabric.microsoft.com,
        Work IQ -> its own audience) -- so only one token exchange is needed
        here, unlike the old local-MCP-client design which needed a separate
        per-tool-audience token (see docs/TROUBLESHOOTING.md).

        ponytail: cached per (user_id, scope) for the token's own lifetime
        (minus a 60s safety margin) when `user_id` is supplied -- see
        `self._token_cache`. Skips the cache entirely (old behaviour) when
        `user_id` is None, e.g. the outbound-notification path.
        """
        if not auth_handler_name:
            logger.warning("⚠️ No auth handler configured — Fabric IQ/Work IQ will be unavailable this turn")
            return None

        cache_key = (user_id, scope) if user_id else None
        if cache_key:
            cached = self._token_cache.get(cache_key)
            if cached and cached[1] > time.time():
                logger.info("✅ Reusing cached OBO token for scope %s (no broker round trip)", scope)
                return cached[0]

        try:
            token_response = await auth.exchange_token(
                context, scopes=[scope], auth_handler_id=auth_handler_name
            )
            if token_response and token_response.token:
                logger.info("✅ User OBO token acquired (len=%d)", len(token_response.token))
                if cache_key:
                    self._token_cache[cache_key] = (
                        token_response.token,
                        _jwt_exp_epoch(token_response.token) - 60,
                    )
                return token_response.token
            logger.warning("⚠️ Token exchange returned empty response")
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ OBO token exchange failed: %s", exc, exc_info=True)
        return None
