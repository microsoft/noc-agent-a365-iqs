"""Focused MCP context and metadata self-check.

Run directly: `python agent/test_mcp_server.py`.
"""

import asyncio
import base64
import json
import os
import time
from pathlib import Path

import httpx

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://example.invalid/api/projects/dummy")
os.environ.setdefault("AZURE_AI_MODEL_DEPLOYMENT_NAME", "dummy-model")
os.environ.setdefault("AZURE_TENANT_ID", "tenant-1")
os.environ.setdefault("MCP_SERVER_APP_CLIENT_ID", "server-app")
os.environ.setdefault("MCP_SERVER_AUDIENCE", "api://server-app")
os.environ.setdefault("AZURE_CLIENT_ID", "managed-identity")

from agent_framework_hosting_mcp import AgentMCPTool  # noqa: E402
from mcp import types  # noqa: E402

import mcp_server  # noqa: E402


class FakeAgent:
    name = "ignored-by-explicit-name"
    description = "ignored-by-explicit-description"

    async def run(self, *_args, **_kwargs):
        raise AssertionError("The metadata check must not execute the agent.")


class FakeNocAgent:
    def __init__(self):
        self.run_token_args = None

    def _get_or_create_run_id(self, conversation_id, activity_id):
        return f"run:{conversation_id}:{activity_id}"

    async def _get_or_create_run_token(self, conversation_id, activity_id):
        self.run_token_args = (conversation_id, activity_id)
        return "run-token"


class FakeNocAgentNoLedger(FakeNocAgent):
    async def _get_or_create_run_token(self, conversation_id, activity_id):
        self.run_token_args = (conversation_id, activity_id)
        return None


class FakeDirectNocAgent(FakeNocAgent):
    def __init__(self):
        super().__init__()
        self.specialist_call = None

    async def _call_specialist(self, specialist, question):
        self.specialist_call = (specialist, question)
        return "direct specialist result"


class FakeAgentTool:
    def __init__(self):
        self.observed = None

    async def call_tool(self, name, arguments):
        self.observed = {
            "name": name,
            "arguments": arguments,
            "user_token": mcp_server._current_user_token.get(),
            "run_id": mcp_server._current_run_id.get(),
            "run_token": mcp_server._current_run_token.get(),
            "step": mcp_server._next_run_step.get()(),
            "user": mcp_server._current_user_ctx.get(),
        }
        return [types.TextContent(type="text", text="mock result")]


class SlowAgentTool:
    async def call_tool(self, _name, _arguments):
        await asyncio.sleep(1)


class UnexpectedAgentTool:
    async def call_tool(self, _name, _arguments):
        raise AssertionError("Focused tasks must bypass the outer orchestrator tool.")


async def main():
    original_timeout = os.environ.get("MCP_TOOL_TIMEOUT_SECONDS")
    os.environ["MCP_TOOL_TIMEOUT_SECONDS"] = "60"
    assert mcp_server._timeout_seconds() == 28
    if original_timeout is None:
        os.environ.pop("MCP_TOOL_TIMEOUT_SECONDS")
    else:
        os.environ["MCP_TOOL_TIMEOUT_SECONDS"] = original_timeout

    adapter = AgentMCPTool(
        FakeAgent(),
        name=mcp_server.TOOL_NAME,
        description=mcp_server.TOOL_DESCRIPTION,
        argument_name="task",
        argument_description=mcp_server.TOOL_ARGUMENT_DESCRIPTION,
    )
    listed = await mcp_server._list_noc_tools(adapter)
    assert len(listed) == 1
    tool = listed[0]
    assert tool.name == "noc_investigate"
    assert tool.inputSchema["required"] == ["task"]
    assert tool.inputSchema["additionalProperties"] is False
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    declared_tools = json.loads(
        (Path(__file__).resolve().parent.parent / "cowork" / "tools" / "noc-mcp-tools.json").read_text()
    )["tools"]
    assert len(declared_tools) == 1
    declared_tool = declared_tools[0]
    assert declared_tool["name"] == tool.name
    assert declared_tool["description"] == tool.description
    assert declared_tool["inputSchema"] == tool.inputSchema
    assert mcp_server._direct_specialist_for_task("Assess blast radius and shared conduit") == "fabric_iq"
    assert mcp_server._direct_specialist_for_task("Show optical readings and suppressed alerts") == "rti_iq"
    assert mcp_server._direct_specialist_for_task("What does the runbook say?") == "foundry_iq"
    assert mcp_server._direct_specialist_for_task("Investigate an unspecified incident") is None

    noc_agent = FakeNocAgent()
    agent_tool = FakeAgentTool()
    authenticated = mcp_server.AuthenticatedCall(
        bearer_token="validated-easy-auth-token",
        oid="user-oid",
        display_name="NOC User",
    )

    original_precall = mcp_server._run_ledger_precall
    original_postcall = mcp_server._run_ledger_postcall
    precall_calls = []

    async def fake_precall(**kwargs):
        precall_calls.append(kwargs)
        return {"action": "allow", "reservation_id": "reservation"}

    mcp_server._run_ledger_precall = fake_precall
    mcp_server._run_ledger_postcall = lambda *_args, **_kwargs: asyncio.sleep(0)
    try:
        result = await mcp_server._invoke_noc_tool(
            noc_agent,
            agent_tool,
            "noc_investigate",
            {"task": "Investigate LINK-1"},
            authenticated,
            "request-42",
            "foundry-obo-token",
            1,
        )
        direct_noc_agent = FakeDirectNocAgent()
        direct_result = await mcp_server._invoke_noc_tool(
            direct_noc_agent,
            UnexpectedAgentTool(),
            "noc_investigate",
            {"task": "Assess the blast radius and shared conduit for LINK-1"},
            authenticated,
            "request-direct",
            "foundry-obo-token",
            1,
        )
    finally:
        mcp_server._run_ledger_precall = original_precall
        mcp_server._run_ledger_postcall = original_postcall

    assert result[0].text == "mock result"
    assert direct_result[0].text == "direct specialist result"
    assert direct_noc_agent.specialist_call == (
        "fabric_iq",
        "Assess the blast radius and shared conduit for LINK-1",
    )
    assert noc_agent.run_token_args[0] == "user-oid"
    assert noc_agent.run_token_args[1].startswith("request-42-")
    assert agent_tool.observed["name"] == "noc_investigate"
    assert agent_tool.observed["arguments"] == {"task": "Investigate LINK-1"}
    assert precall_calls[0]["prompt"] == "Investigate LINK-1"
    assert precall_calls[1]["prompt"] == "Assess the blast radius and shared conduit for LINK-1"
    assert agent_tool.observed["user_token"] == "foundry-obo-token"
    assert agent_tool.observed["run_id"].startswith("run:user-oid:request-42-")
    assert agent_tool.observed["run_token"] == "run-token"
    assert agent_tool.observed["step"] == "2"
    assert agent_tool.observed["user"] == ("user-oid", "NOC User")
    assert mcp_server._current_user_token.get() is None
    assert mcp_server._current_run_id.get() is None
    assert mcp_server._current_user_ctx.get() is None

    timeout_result = await mcp_server._invoke_noc_tool(
        FakeNocAgentNoLedger(),
        SlowAgentTool(),
        "noc_investigate",
        {"task": "Too broad"},
        authenticated,
        "request-timeout",
        "foundry-obo-token",
        0.001,
    )
    assert timeout_result.isError is True
    assert "time budget" in timeout_result.content[0].text
    assert mcp_server._current_user_token.get() is None

    payload = {
        "tid": "tenant-1",
        "aud": "server-app",
        "oid": "user-oid",
        "scp": "noc.invoke",
        "exp": time.time() + 300,
    }
    encoded = __import__("jwt").encode(payload, "test-only-key", algorithm="HS256")
    principal = base64.b64encode(
        json.dumps(
            {
                "claims": [
                    {"typ": "oid", "val": "user-oid"},
                    {"typ": "tid", "val": "tenant-1"},
                ]
            }
        ).encode()
    )
    scope = {
        "headers": [
            (b"authorization", f"Bearer {encoded}".encode()),
            (b"x-ms-client-principal", principal),
        ]
    }
    assert mcp_server._validated_call(scope).oid == "user-oid"

    os.environ["MCP_ALLOWED_CALLER_OBJECT_ID"] = "cowork-users-group"
    os.environ["MCP_ALLOWED_CALLER_TYPE"] = "Group"
    assert mcp_server._validated_call(scope) is None
    payload["groups"] = ["cowork-users-group"]
    encoded = __import__("jwt").encode(payload, "test-only-key", algorithm="HS256")
    scope["headers"][0] = (b"authorization", ("Bear" + "er " + encoded).encode())
    assert mcp_server._validated_call(scope).oid == "user-oid"
    os.environ.pop("MCP_ALLOWED_CALLER_OBJECT_ID")
    os.environ.pop("MCP_ALLOWED_CALLER_TYPE")

    transport = httpx.ASGITransport(app=mcp_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
        unauthorized = await client.post("/mcp")
    assert metadata.status_code == 200
    assert metadata.json()["resource"] == "http://localhost:8000/mcp"
    assert unauthorized.status_code == 401
    assert "resource_metadata=" in unauthorized.headers["www-authenticate"]

    print("PASS: MCP context, timeout, claims, metadata route, challenge, and tool metadata")


if __name__ == "__main__":
    asyncio.run(main())
