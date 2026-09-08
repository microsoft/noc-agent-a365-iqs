"""Authenticated, stateless MCP channel for the existing NOC orchestrator."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import jwt
from agent_framework_hosting_mcp import AgentMCPTool
from azure.identity.aio import ManagedIdentityCredential, OnBehalfOfCredential
from mcp import types
from mcp.server.auth.routes import build_resource_metadata_url, create_protected_resource_routes
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from agent import (
    MODEL_DEPLOYMENT_NAME,
    RUN_LEDGER_AGENT_NAME,
    NocAgent,
    _apply_precall_decision,
    _current_deadline,
    _current_run_id,
    _current_run_token,
    _current_user_ctx,
    _current_user_token,
    _next_run_step,
    _pending_consent,
    _run_ledger_postcall,
    _run_ledger_precall,
)

TOOL_NAME = "noc_investigate"
TOOL_DESCRIPTION = (
    "Investigate a network operations incident using the NOC orchestrator and its "
    "Foundry IQ, Fabric IQ, Web IQ, Work IQ, and real-time telemetry specialists."
)
TOOL_ARGUMENT_DESCRIPTION = (
    "The incident, alert, outage, blast-radius, runbook, or stakeholder-communication task to investigate."
)
DOWNSTREAM_FOUNDRY_SCOPE = "https://ai.azure.com/.default"
MAX_COWORK_TIMEOUT_SECONDS = 28.0


def _timeout_seconds() -> float:
    try:
        configured = float(os.getenv("MCP_TOOL_TIMEOUT_SECONDS", "28"))
    except ValueError as exc:
        raise RuntimeError("MCP_TOOL_TIMEOUT_SECONDS must be a number.") from exc
    if configured <= 0:
        raise RuntimeError("MCP_TOOL_TIMEOUT_SECONDS must be greater than zero.")
    return min(configured, MAX_COWORK_TIMEOUT_SECONDS)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required.")
    return value


def _metadata_url() -> str:
    return str(build_resource_metadata_url(AnyHttpUrl(os.getenv("PUBLIC_MCP_URL", "http://localhost:8000/mcp"))))


@dataclass(frozen=True)
class AuthenticatedCall:
    bearer_token: str
    oid: str
    display_name: str


def _unauthorized(message: str) -> JSONResponse:
    challenge = (
        f'Bearer resource_metadata="{_metadata_url()}", '
        f'error="invalid_token", error_description="{message}"'
    )
    return JSONResponse(
        {"error": "unauthorized", "error_description": message},
        status_code=401,
        headers={"WWW-Authenticate": challenge},
    )


def _validated_call(scope: Scope) -> AuthenticatedCall | None:
    headers = {key.lower(): value for key, value in scope.get("headers", [])}
    authorization = headers.get(b"authorization", b"").decode("latin-1")
    easy_auth_principal = headers.get(b"x-ms-client-principal", b"").decode("latin-1")
    if not authorization.startswith("Bearer ") or not easy_auth_principal:
        return None

    token = authorization[7:].strip()
    try:
        principal_bytes = base64.b64decode(
            easy_auth_principal + "=" * (-len(easy_auth_principal) % 4),
            validate=True,
        )
        principal_payload = json.loads(principal_bytes)
        if not isinstance(principal_payload, dict) or not isinstance(principal_payload.get("claims"), list):
            return None
        principal_claims = {
            str(item.get("typ")): str(item.get("val"))
            for item in principal_payload.get("claims", [])
            if isinstance(item, dict) and item.get("typ") and item.get("val")
        }
        claims = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
                "verify_nbf": False,
            },
        )
    except (ValueError, json.JSONDecodeError, jwt.PyJWTError):
        return None

    tenant_id = os.getenv("AZURE_TENANT_ID", "").strip()
    client_id = os.getenv("MCP_SERVER_APP_CLIENT_ID", "").strip()
    audience = os.getenv("MCP_SERVER_AUDIENCE", "").strip()
    allowed_audiences = {value for value in (client_id, audience) if value}
    token_audiences = claims.get("aud", [])
    if isinstance(token_audiences, str):
        token_audiences = [token_audiences]
    if not isinstance(token_audiences, list) or not all(isinstance(value, str) for value in token_audiences):
        return None
    scopes = set(str(claims.get("scp", "")).split())
    required_scope = os.getenv("MCP_REQUIRED_SCOPE", "noc.invoke").strip()
    now = time.time()

    if not tenant_id or claims.get("tid") != tenant_id:
        return None
    if not allowed_audiences or not allowed_audiences.intersection(token_audiences):
        return None
    if not required_scope or required_scope not in scopes:
        return None
    if not isinstance(claims.get("oid"), str) or not claims["oid"]:
        return None
    allowed_caller_id = os.getenv("MCP_ALLOWED_CALLER_OBJECT_ID", "").strip()
    allowed_caller_type = os.getenv("MCP_ALLOWED_CALLER_TYPE", "Group").strip().lower()
    if allowed_caller_id:
        if allowed_caller_type == "user":
            if claims["oid"] != allowed_caller_id:
                return None
        elif allowed_caller_type == "group":
            groups = claims.get("groups", [])
            if isinstance(groups, str):
                groups = [groups]
            if not isinstance(groups, list) or allowed_caller_id not in groups:
                return None
        else:
            return None
    easy_auth_oid = principal_claims.get("oid") or principal_claims.get(
        "http://schemas.microsoft.com/identity/claims/objectidentifier"
    )
    easy_auth_tid = principal_claims.get("tid") or principal_claims.get(
        "http://schemas.microsoft.com/identity/claims/tenantid"
    )
    if easy_auth_oid != claims["oid"] or easy_auth_tid != claims["tid"]:
        return None
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= now:
        return None
    if isinstance(claims.get("nbf"), (int, float)) and claims["nbf"] > now:
        return None

    display_name = str(claims.get("name") or claims.get("preferred_username") or claims["oid"])
    return AuthenticatedCall(token, claims["oid"], display_name)


class EasyAuthClaimsMiddleware:
    """Trust Easy Auth for signature validation, then enforce required claims."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            authenticated_call = _validated_call(scope)
            if authenticated_call is None:
                await _unauthorized("A valid bearer token with the required audience and scope is required.")(
                    scope, receive, send
                )
                return
            scope.setdefault("state", {})["mcp_authenticated_call"] = authenticated_call
        await self.app(scope, receive, send)


async def _acquire_foundry_obo_token(
    authenticated_call: AuthenticatedCall,
    managed_identity: ManagedIdentityCredential,
) -> str:
    assertion = await managed_identity.get_token("api://AzureADTokenExchange")
    async with OnBehalfOfCredential(
        tenant_id=_required_env("AZURE_TENANT_ID"),
        client_id=_required_env("MCP_SERVER_APP_CLIENT_ID"),
        client_assertion_func=lambda: assertion.token,
        user_assertion=authenticated_call.bearer_token,
    ) as credential:
        token = await credential.get_token(DOWNSTREAM_FOUNDRY_SCOPE)
    return token.token


async def _list_noc_tools(agent_tool: AgentMCPTool) -> list[types.Tool]:
    annotations = types.ToolAnnotations(
        title="Investigate NOC incident",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    return [tool.model_copy(update={"annotations": annotations}) for tool in await agent_tool.list_tools()]


def _direct_specialist_for_task(task: str) -> str | None:
    """Route focused Cowork skills directly, avoiding an extra orchestrator turn."""
    normalized = task.casefold()
    routes = (
        ("rti_iq", ("optical reading", "loss of light", "sensor", "suppressed", "telemetry timeline")),
        ("work_iq", ("on-call", "on call", "incident bridge", "bridge chatter")),
        ("web_iq", ("public vendor advisory", "status-page", "status page", "carrier advisory")),
        ("foundry_iq", ("runbook", "prior ticket", "happened before", "ticket history")),
        ("fabric_iq", ("blast radius", "shared conduit", "physical conduit", "topology")),
    )
    for specialist, phrases in routes:
        if any(phrase in normalized for phrase in phrases):
            return specialist
    return None


def _timeout_result() -> types.CallToolResult:
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=(
                    "The NOC investigation exceeded Cowork's tool-call time budget. "
                    "Retry with a narrower incident, service, link, or evidence question."
                ),
            )
        ],
        isError=True,
    )


async def _invoke_noc_tool(
    noc_agent: NocAgent,
    agent_tool: AgentMCPTool,
    name: str,
    arguments: Mapping[str, Any] | None,
    authenticated_call: AuthenticatedCall,
    request_id: str,
    foundry_user_token: str,
    timeout_seconds: float,
) -> list[types.ContentBlock] | types.CallToolResult:
    invocation_id = f"{request_id}-{uuid.uuid4().hex}"
    run_id = noc_agent._get_or_create_run_id(authenticated_call.oid, invocation_id)
    run_token = await noc_agent._get_or_create_run_token(authenticated_call.oid, invocation_id)
    step_counter = 0

    def next_step() -> str:
        nonlocal step_counter
        step_counter += 1
        return str(step_counter)

    context_tokens = [
        (_current_user_token, _current_user_token.set(foundry_user_token)),
        (_pending_consent, _pending_consent.set(None)),
        (_current_run_token, _current_run_token.set(run_token)),
        (_current_run_id, _current_run_id.set(run_id if run_token else None)),
        (_current_deadline, _current_deadline.set(time.monotonic() + timeout_seconds)),
        (_next_run_step, _next_run_step.set(next_step if run_token else None)),
        (
            _current_user_ctx,
            _current_user_ctx.set((authenticated_call.oid, authenticated_call.display_name)),
        ),
    ]

    reservation_id: str | None = None
    call_started = False
    task = arguments.get("task", "") if arguments else ""
    estimated_input_tokens = len(str(task)) // 4
    try:
        if run_token:
            decision = await _run_ledger_precall(
                run_id=run_id,
                agent_name=RUN_LEDGER_AGENT_NAME,
                step=next_step(),
                model=MODEL_DEPLOYMENT_NAME,
                est_input_tokens=estimated_input_tokens,
                prompt=str(task),
            )
            reservation_id = _apply_precall_decision(decision)

        call_started = True
        specialist = _direct_specialist_for_task(str(task))
        try:
            if specialist:
                answer = await asyncio.wait_for(
                    noc_agent._call_specialist(specialist, str(task)),
                    timeout=timeout_seconds,
                )
                result: list[types.ContentBlock] | types.CallToolResult = [
                    types.TextContent(type="text", text=answer)
                ]
            else:
                result = await asyncio.wait_for(
                    agent_tool.call_tool(name, arguments),
                    timeout=timeout_seconds,
                )
        except asyncio.TimeoutError:
            if run_token:
                await _run_ledger_postcall(run_id, reservation_id, failed=True)
            return _timeout_result()

        if run_token:
            # AgentMCPTool returns protocol blocks, not the AgentResponse usage object.
            output_blocks = result.content if isinstance(result, types.CallToolResult) else result
            estimated_output_tokens = sum(
                len(block.text) // 4
                for block in output_blocks
                if isinstance(block, types.TextContent)
            )
            await _run_ledger_postcall(
                run_id,
                reservation_id,
                model=MODEL_DEPLOYMENT_NAME,
                input_tokens=estimated_input_tokens,
                output_tokens=estimated_output_tokens,
            )
        pending_consent = _pending_consent.get()
        if pending_consent:
            agent_name, consent_url = pending_consent
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=f"{agent_name} requires additional user consent. Open this sign-in link: {consent_url}",
                    )
                ]
            )
        return result
    except asyncio.CancelledError:
        if run_token and call_started:
            await _run_ledger_postcall(run_id, reservation_id, failed=True)
        raise
    except Exception:
        if run_token and call_started:
            await _run_ledger_postcall(run_id, reservation_id, failed=True)
        raise
    finally:
        for context_var, token in reversed(context_tokens):
            context_var.reset(token)


server = Server(
    "noc-agent-cowork",
    version="1.0.0",
    instructions="Use noc_investigate for read-only NOC incident investigation.",
)
session_manager = StreamableHTTPSessionManager(
    app=server,
    event_store=None,
    json_response=True,
    stateless=True,
)
_noc_agent: NocAgent | None = None
_agent_tool: AgentMCPTool | None = None
_managed_identity: ManagedIdentityCredential | None = None


def _runtime() -> tuple[NocAgent, AgentMCPTool, ManagedIdentityCredential]:
    if _noc_agent is None or _agent_tool is None or _managed_identity is None:
        raise RuntimeError("MCP runtime has not initialized.")
    return _noc_agent, _agent_tool, _managed_identity


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    _, agent_tool, _ = _runtime()
    return await _list_noc_tools(agent_tool)


@server.call_tool()
async def call_tool(
    name: str,
    arguments: dict[str, object] | None,
) -> list[types.ContentBlock] | types.CallToolResult:
    noc_agent, agent_tool, managed_identity = _runtime()
    request = server.request_context.request
    if not isinstance(request, Request):
        raise RuntimeError("MCP HTTP request context is unavailable.")
    authenticated_call = getattr(request.state, "mcp_authenticated_call", None)
    if not isinstance(authenticated_call, AuthenticatedCall):
        raise RuntimeError("Authenticated MCP caller context is unavailable.")

    timeout = _timeout_seconds()

    async def invoke() -> list[types.ContentBlock] | types.CallToolResult:
        foundry_user_token = await _acquire_foundry_obo_token(authenticated_call, managed_identity)
        return await _invoke_noc_tool(
            noc_agent,
            agent_tool,
            name,
            arguments,
            authenticated_call,
            str(server.request_context.request_id),
            foundry_user_token,
            timeout,
        )

    try:
        return await asyncio.wait_for(invoke(), timeout=timeout)
    except asyncio.TimeoutError:
        return _timeout_result()


async def health(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


@asynccontextmanager
async def lifespan(_app: Starlette) -> AsyncIterator[None]:
    global _agent_tool, _managed_identity, _noc_agent

    _required_env("AZURE_TENANT_ID")
    _required_env("MCP_SERVER_APP_CLIENT_ID")
    _required_env("MCP_SERVER_AUDIENCE")
    noc_agent = NocAgent()
    managed_identity = ManagedIdentityCredential(client_id=_required_env("AZURE_CLIENT_ID"))
    try:
        await noc_agent.initialize()
        if noc_agent._agent is None:
            raise RuntimeError("NocAgent initialized without an orchestrator agent.")
        agent_tool = AgentMCPTool(
            noc_agent._agent,
            name=TOOL_NAME,
            description=TOOL_DESCRIPTION,
            argument_name="task",
            argument_description=TOOL_ARGUMENT_DESCRIPTION,
        )
        _noc_agent = noc_agent
        _agent_tool = agent_tool
        _managed_identity = managed_identity
        async with session_manager.run():
            yield
    finally:
        _noc_agent = None
        _agent_tool = None
        _managed_identity = None
        await managed_identity.close()
        await noc_agent.cleanup()


public_mcp_url = AnyHttpUrl(os.getenv("PUBLIC_MCP_URL", "http://localhost:8000/mcp"))
tenant_id = os.getenv("AZURE_TENANT_ID", "common")
scope_uri = os.getenv("MCP_SCOPE_URI", "api://localhost/noc.invoke")
routes = [
    Route("/healthz", endpoint=health, methods=["GET"]),
    *create_protected_resource_routes(
        resource_url=public_mcp_url,
        authorization_servers=[AnyHttpUrl(f"https://login.microsoftonline.com/{tenant_id}/v2.0")],
        scopes_supported=[scope_uri],
        resource_name="NOC Agent Cowork MCP",
    ),
    Mount("/mcp", app=session_manager.handle_request),
]
starlette_app = Starlette(routes=routes, lifespan=lifespan)
app = EasyAuthClaimsMiddleware(starlette_app)
