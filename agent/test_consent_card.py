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

    context = Context()
    response = await instance.process_user_message("Using Work IQ only", object(), "AGENTIC", context)
    assert response == ""
    assert len(context.sent) == 1
    card = context.sent[0].attachments[0].content
    assert card["actions"][0]["url"] == "https://consent.example.invalid/single-use"
    assert "noc-comms-agent" in card["actions"][0]["title"]


if __name__ == "__main__":
    asyncio.run(run())
    print("PASS: test_consent_card.py self-check passed")
