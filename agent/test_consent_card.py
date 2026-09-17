"""Regression check: task-local OAuth consent must reach the Teams parent turn."""

import asyncio
import os
from types import MethodType, SimpleNamespace

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://example.invalid/api/projects/dummy")
os.environ.setdefault("AZURE_AI_MODEL_DEPLOYMENT_NAME", "dummy-model")

import agent as agent_module  # noqa: E402
from agent import NocAgent  # noqa: E402


class Context:
    def __init__(self):
        self.activity = SimpleNamespace(
            from_property=SimpleNamespace(id="user-1", name="Operator"),
            conversation=SimpleNamespace(id="conversation-1"),
            id="activity-1",
        )
        self.sent = []

    async def send_activity(self, activity):
        self.sent.append(activity)


class Orchestrator:
    async def run(self, _history):
        assert agent_module._current_run_id.get() == "run-1"
        assert agent_module._current_run_token.get() is None
        agent_module._pending_consent.set(
            ("noc-comms-agent", "https://consent.example.invalid/single-use")
        )
        return SimpleNamespace(text="a consent link has been sent", usage_details={})


async def run():
    instance = NocAgent.__new__(NocAgent)
    instance._conversations = {}
    instance._agent = Orchestrator()

    async def exchange(self, *_args, **_kwargs):
        return "header.eyJleHAiOjQxMDI0NDQ4MDB9.signature"

    async def run_token(self, *_args, **_kwargs):
        return None

    def run_id(self, *_args, **_kwargs):
        return "run-1"

    instance._exchange_user_token = MethodType(exchange, instance)
    instance._get_or_create_run_token = MethodType(run_token, instance)
    instance._get_or_create_run_id = MethodType(run_id, instance)

    async def unexpected_ledger_call(**_kwargs):
        raise AssertionError("ledger precall must be skipped without a run token")

    original_precall = agent_module._run_ledger_precall
    agent_module._run_ledger_precall = unexpected_ledger_call
    try:
        context = Context()
        response = await instance.process_user_message("Using Work IQ only", object(), "AGENTIC", context)
        assert response == ""
        assert len(context.sent) == 1
        card = context.sent[0].attachments[0].content
        assert card["actions"][0]["url"] == "https://consent.example.invalid/single-use"
        assert "noc-comms-agent" in card["actions"][0]["title"]
    finally:
        agent_module._run_ledger_precall = original_precall


if __name__ == "__main__":
    asyncio.run(run())
    print("PASS: test_consent_card.py self-check passed")
