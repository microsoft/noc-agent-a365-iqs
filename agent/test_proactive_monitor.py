"""Focused self-check for proactive monitor OAuth restoration."""

import asyncio
from types import SimpleNamespace

from host_agent_server import GenericAgentHost


class FakeContext:
    def __init__(self):
        self.activity = SimpleNamespace(
            from_property=SimpleNamespace(id="user-1")
        )
        self.sent = []

    async def send_activity(self, activity):
        self.sent.append(activity)


class FakeProactive:
    def __init__(self):
        self.token_handlers = None

    async def continue_conversation(
        self, adapter, conversation_id, handler, *, token_handlers=None
    ):
        assert adapter == "adapter"
        assert conversation_id == "conversation-1"
        self.token_handlers = token_handlers
        await handler(FakeContext(), SimpleNamespace())


class FakeAgent:
    async def investigate_detected_incident(self, *args, **kwargs):
        assert args[0] == "INC-1"
        assert kwargs["event_detail"] == "optical loss"
        return {"status": "completed", "response": "investigation complete"}


async def check_proactive_monitor_auth_handler():
    server = GenericAgentHost.__new__(GenericAgentHost)
    proactive = FakeProactive()
    server.agent_app = SimpleNamespace(
        proactive=proactive,
        adapter="adapter",
        auth="auth",
    )
    server.agent_instance = FakeAgent()
    server.auth_handler_name = "agentic-user-auth"
    server.monitor_config = SimpleNamespace(specialist_concurrency=3)

    result = await server._handle_detected_incident(
        {
            "incident_id": "INC-1",
            "timestamp": "2026-09-17T00:00:00Z",
            "detail": "optical loss",
        },
        {
            "conversation_id": "conversation-1",
            "authorized_user_id": "user-1",
        },
    )

    assert result is True
    assert proactive.token_handlers == ["agentic-user-auth"]


if __name__ == "__main__":
    asyncio.run(check_proactive_monitor_auth_handler())
    print("PASS: test_proactive_monitor.py self-check passed")
