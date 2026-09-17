"""Provision the 5 persisted Foundry Prompt Agents for the multi-agent design.

Each Prompt Agent replaces one leg of the old shared "noc-iq-toolbox" with a
single native `MCPTool(project_connection_id=...)` baked directly into the
agent's own persisted definition -- see agent/agent.py's module docstring.
This script is the one-time (or "run again after an instruction/model
change") provisioning step; agent.py's NocAgent.initialize() only ever
CONFIRMS these already exist, it never creates or updates them.

Idempotent: `project.agents.create_version(...)` always creates a NEW
version, so this script first fetches the current live version's
definition and skips the create_version call entirely when nothing (model,
instructions, connection id) has actually changed -- otherwise every re-run
(e.g. a redeploy) would pile up an unbounded number of agent versions, the
same "smell" already flagged and fixed for the old toolbox in agent.py's
prior history.

Required env (from `.env` / `azd env get-values`):
  FOUNDRY_PROJECT_ENDPOINT, AZURE_AI_MODEL_DEPLOYMENT_NAME, AZURE_TENANT_ID
Optional model profile:
  AZURE_AI_SPECIALIST_MODEL_DEPLOYMENT_NAME defaults to the orchestrator model
  for backward compatibility. FOUNDRY_IQ_MODEL_DEPLOYMENT_NAME,
  FABRIC_IQ_MODEL_DEPLOYMENT_NAME, WEB_IQ_MODEL_DEPLOYMENT_NAME,
  WORK_IQ_MODEL_DEPLOYMENT_NAME, and RTI_IQ_MODEL_DEPLOYMENT_NAME override one
  specialist each.
Optional connections (defaults match the live connections already set up in
rg-<env-name> -- see agent/.env.template):
  FOUNDRY_IQ_CONNECTION_NAME (default kb-mcp-connection)
  FABRIC_IQ_CONNECTION_NAME (default fabric-iq-connection)
  WEB_IQ_CONNECTION_NAME (default web-iq-connection)
  WORK_IQ_CONNECTION_NAME (default WorkIQ)
  FABRIC_RTI_MCP_URL, FABRIC_RTI_CONNECTION_ID
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureDeveloperCliCredential
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).parents[1]
load_dotenv(REPO_ROOT / ".env", override=True)
load_dotenv(REPO_ROOT / "agent" / ".env", override=False)


@dataclass(frozen=True)
class SpecialistSpec:
    agent_name: str
    model_env: str
    connection_env: str
    default_connection_name: str
    server_label: str
    instructions: str
    server_url_env: Optional[str] = None


SPECIALISTS = [
    SpecialistSpec(
        agent_name="noc-knowledge-agent",
        model_env="FOUNDRY_IQ_MODEL_DEPLOYMENT_NAME",
        connection_env="FOUNDRY_IQ_CONNECTION_NAME",
        default_connection_name="kb-mcp-connection",
        server_label="foundry-iq",
        instructions=(
            "You are the Foundry IQ knowledge-base specialist for a telecom NOC/NOA "
            "operations team. Answer using the runbooks, equipment/infrastructure specs, "
            "and past ticket history exposed by your knowledge-base MCP tool only. Always "
            "cite the specific runbook/ticket/spec you grounded each claim in. If the tool "
            "returns no matching result, say plainly that nothing was found for the "
            "question -- do not invent an answer or claim a connection problem unless the "
            "tool call itself actually errors out."
        ),
    ),
    SpecialistSpec(
        agent_name="noc-topology-agent",
        model_env="FABRIC_IQ_MODEL_DEPLOYMENT_NAME",
        connection_env="FABRIC_IQ_CONNECTION_NAME",
        default_connection_name="fabric-iq-connection",
        server_label="fabric-iq",
        instructions=(
            "You are the Fabric IQ network-topology specialist for a telecom NOC/NOA "
            "operations team. Answer using the live network topology graph exposed by your "
            "Fabric Data Agent MCP tool: core routers, transport links, physical conduits, "
            "amplifier sites, services, and SLA policies. ALWAYS check for shared-conduit "
            "risk when asked about a physical fault -- two logically independent links can "
            "still share one physical conduit, the most common non-obvious root cause of a "
            "'redundant' path also failing. If the tool returns no data for a specific "
            "query, that means no matching entity/relationship exists -- say so plainly "
            "(e.g. 'no data found for X in the network topology') and do NOT claim a "
            "connection/access/technical problem. Only report an actual connection/consent "
            "problem if the tool call itself errors out (e.g. a consent/authorization "
            "requirement) -- relay any consent instructions verbatim."
        ),
    ),
    SpecialistSpec(
        agent_name="noc-threatintel-agent",
        model_env="WEB_IQ_MODEL_DEPLOYMENT_NAME",
        connection_env="WEB_IQ_CONNECTION_NAME",
        default_connection_name="web-iq-connection",
        server_label="web-iq",
        instructions=(
            "You are the Web IQ specialist for a telecom NOC/NOA operations team. Use your "
            "web-search MCP tool ONLY to find public information directly relevant to a "
            "network-operations question for this telecom provider: vendor equipment "
            "advisories, carrier/fibre outage status pages, breaking news about an outage. "
            "If asked about anything outside that scope (general news, unrelated companies, "
            "personal topics), politely decline and explain you're scoped to network-"
            "operations incidents for this provider."
        ),
    ),
    SpecialistSpec(
        agent_name="noc-comms-agent",
        model_env="WORK_IQ_MODEL_DEPLOYMENT_NAME",
        connection_env="WORK_IQ_CONNECTION_NAME",
        default_connection_name="WorkIQ",
        server_label="work-iq",
        instructions=(
            "You are the Work IQ specialist for a telecom NOC/NOA operations team. Use your "
            "Microsoft 365 MCP tool to answer questions about the user's own Teams/Outlook "
            "context: incident-bridge chatter, the on-call roster, and pending change "
            "approvals. Use it to draft a status update when asked. If the tool requires "
            "consent, relay the consent URL/instructions to the user verbatim rather than "
            "failing silently."
        ),
    ),
    SpecialistSpec(
        agent_name="noc-incident-agent",
        model_env="RTI_IQ_MODEL_DEPLOYMENT_NAME",
        connection_env="FABRIC_RTI_CONNECTION_ID",
        default_connection_name="",
        server_label="fabric-rti",
        instructions=(
            "You are the Fabric RTI incident-telemetry specialist for a telecom NOC/NOA "
            "operations team. Use the Fabric Eventhouse KQL data exposed by your MCP tool "
            "to answer what actually happened during a network incident using only these "
            "tables and EXACT column names -- do not guess or invent column names, these "
            "are the complete schemas: "
            "OpticalTelemetry(Timestamp:datetime, SensorId:string, LinkId:string, "
            "PowerDbm:real, Ber:real, UtilizationPct:real); "
            "NetworkAlerts(Timestamp:datetime, AlertId:string, IncidentId:string, "
            "EntityId:string, Severity:string, AlertType:string, Suppressed:bool, "
            "AckedAt:datetime, AckedBy:string); "
            "IncidentEvents(Timestamp:datetime, IncidentId:string, Stage:string, "
            "Detail:string, Actor:string). "
            "There is no LinkId column on NetworkAlerts or IncidentEvents -- filter those by "
            "IncidentId instead (an alert's affected link/sensor is identified via EntityId). "
            "Always state the exact time window your answer covers. Never invent or "
            "estimate a reading you did not actually query; if the Eventhouse has no data "
            "for the requested window or entity, say so plainly. "
            "IMPORTANT query-generation constraint: query ONE table at a time, filtered by "
            "IncidentId/LinkId/SensorId and a time range, then correlate the separate results "
            "yourself in your answer. Do NOT ask for a single query that unions/joins all "
            "three tables together in one shot -- generating a query that unions results in "
            "more than one stage (e.g. union two tables, then union/pipe the third into that "
            "result) fails with 'column named TableName already exists', because each union "
            "stage tries to re-add the same source column. If you ever need rows from more "
            "than one table, issue one flat query per table (e.g. `TableName | where ...`) "
            "and combine the answers in your own reasoning, not in KQL."
        ),
        server_url_env="FABRIC_RTI_MCP_URL",
    ),
]


def log(message: str) -> None:
    print(message, flush=True)


def _definition_matches(
    existing, model: str, instructions: str, connection_id: str, server_url: str
) -> bool:
    """True if the live agent's latest version already matches what we'd create.

    `AgentDetails` (returned by `project.agents.get()`) has no top-level
    `.definition` -- the live definition lives at `.versions["latest"]["definition"]`,
    a plain dict.
    """
    latest = (existing.versions or {}).get("latest") or {}
    definition = latest.get("definition") or {}
    if definition.get("model") != model or (definition.get("instructions") or "").strip() != instructions.strip():
        return False
    tools = definition.get("tools") or []
    return any(
        t.get("type") == "mcp"
        and t.get("project_connection_id") == connection_id
        and t.get("server_url") == server_url
        for t in tools
    )


def _connection_override(spec: SpecialistSpec) -> tuple[str, str] | None:
    """Return the explicitly enabled Foundry IQ APIM proxy, if configured."""
    if spec.agent_name != "noc-knowledge-agent":
        return None
    name = os.getenv("FOUNDRY_IQ_PROXY_CONNECTION_NAME", "").strip()
    url = os.getenv("FOUNDRY_IQ_PROXY_MCP_URL", "").strip()
    if bool(name) != bool(url):
        raise RuntimeError(
            "FOUNDRY_IQ_PROXY_CONNECTION_NAME and FOUNDRY_IQ_PROXY_MCP_URL "
            "must either both be set or both be unset."
        )
    return (name, url) if name else None


def specialist_model(spec: SpecialistSpec, default_model: str) -> str:
    """Resolve one specialist's model without coupling it to the orchestrator."""
    model = os.getenv(spec.model_env, "").strip()
    return model or default_model


def ensure_specialist_agent(project: AIProjectClient, model: str, spec: SpecialistSpec) -> None:
    if spec.server_url_env:
        connection_id = os.environ[spec.connection_env]
        server_url = os.environ[spec.server_url_env]
        log(f"  direct MCP connection -> {connection_id}")
    else:
        proxy = _connection_override(spec)
        connection_name = proxy[0] if proxy else os.getenv(
            spec.connection_env, spec.default_connection_name
        )
        connection = project.connections.get(connection_name, include_credentials=False)
        connection_id = connection.id
        server_url = proxy[1] if proxy else connection.target
        if proxy and connection.target.rstrip("/") != server_url.rstrip("/"):
            raise RuntimeError(
                f"Proxy connection '{connection_name}' targets '{connection.target}', "
                f"not FOUNDRY_IQ_PROXY_MCP_URL '{server_url}'."
            )
        log(f"  connection '{connection_name}' -> {connection_id}")

    definition = PromptAgentDefinition(
        model=model,
        instructions=spec.instructions,
        tools=[
            MCPTool(
                server_label=spec.server_label,
                server_url=server_url,
                project_connection_id=connection_id,
                require_approval="never",
            )
        ],
    )

    try:
        existing = project.agents.get(spec.agent_name)
        if _definition_matches(existing, model, spec.instructions, connection_id, server_url):
            latest_version = ((existing.versions or {}).get("latest") or {}).get("version", "?")
            log(f"[OK] '{spec.agent_name}' already up to date (v{latest_version}) -- skipping")
            return
        log(f"  '{spec.agent_name}' exists but is out of date -- creating a new version")
    except ResourceNotFoundError:
        log(f"  '{spec.agent_name}' not found -- creating")

    created = project.agents.create_version(spec.agent_name, definition=definition)
    log(f"[OK] '{spec.agent_name}' -> v{created.version}")


def main() -> None:
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    orchestrator_model = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    default_specialist_model = os.getenv(
        "AZURE_AI_SPECIALIST_MODEL_DEPLOYMENT_NAME", orchestrator_model
    ).strip() or orchestrator_model
    tenant_id: Optional[str] = os.getenv("AZURE_TENANT_ID")

    credential = AzureDeveloperCliCredential(tenant_id=tenant_id, process_timeout=60)
    try:
        project = AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True)
        log(f"Provisioning {len(SPECIALISTS)} specialist Prompt Agent(s) against {endpoint} ...")
        log(f"  orchestrator model remains '{orchestrator_model}'")
        for spec in SPECIALISTS:
            model = specialist_model(spec, default_specialist_model)
            log(f"  {spec.agent_name} model -> {model}")
            ensure_specialist_agent(project, model, spec)
        project.close()
    finally:
        credential.close()

    log("\nDone. agent/agent.py's SPECIALIST_AGENTS map resolves these by name at startup.")


if __name__ == "__main__":
    main()
