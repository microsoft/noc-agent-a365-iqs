"""Standalone self-check for strict automatic five-specialist fan-out."""

import asyncio
import os
from types import MethodType, SimpleNamespace

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://example.invalid/api/projects/dummy")
os.environ.setdefault("AZURE_AI_MODEL_DEPLOYMENT_NAME", "dummy-model")

import agent as agent_module  # noqa: E402
from agent import NocAgent, SPECIALIST_AGENTS  # noqa: E402


class Context:
    def __init__(self):
        self.activity = SimpleNamespace(
            from_property=SimpleNamespace(id="authorized-user", name="Operator")
        )


def build_agent(mode="success"):
    instance = NocAgent.__new__(NocAgent)
    calls = []

    async def exchange(self, *_args, **_kwargs):
        return "header.eyJleHAiOjQxMDI0NDQ4MDB9.signature"

    async def call(self, key, _question):
        calls.append(key)
        if mode == "partial" and key == "web_iq":
            raise RuntimeError("mock failure")
        if mode == "consent" and key == "work_iq":
            agent_module._pending_consent.set(("noc-comms-agent", "https://consent.invalid"))
            return "(consent required)"
        return f"{key} evidence"

    async def synthesize(self, _result):
        return "enriched response"

    instance._exchange_user_token = MethodType(exchange, instance)
    instance._call_specialist = MethodType(call, instance)
    instance._synthesize_automatic_investigation = MethodType(synthesize, instance)
    return instance, calls


async def invoke(instance):
    return await instance.investigate_detected_incident(
        "INC-001", "2026-09-16T12:00:00Z", object(), "AGENTIC", Context(), "authorized-user"
    )


async def run():
    instance, calls = build_agent()
    result = await invoke(instance)
    assert result["status"] == "completed"
    assert result["response"] == "enriched response"
    assert set(calls) == set(SPECIALIST_AGENTS) and len(calls) == 5
    assert all(calls.count(key) == 1 for key in SPECIALIST_AGENTS)
    assert all(item["status"] == "completed" for item in result["families"].values())

    instance, calls = build_agent("partial")
    result = await invoke(instance)
    assert result["status"] == "retry_required"
    assert result["families"]["web_iq"]["status"] == "unavailable"
    assert len(calls) == 5

    instance, calls = build_agent("consent")
    result = await invoke(instance)
    assert result["status"] == "retry_required"
    assert result["families"]["work_iq"]["status"] == "consent_required"
    assert len(calls) == 5

    instance, calls = build_agent()

    async def no_token(self, *_args, **_kwargs):
        return None

    instance._exchange_user_token = MethodType(no_token, instance)
    try:
        await invoke(instance)
        raise AssertionError("missing delegated identity must fail")
    except PermissionError:
        pass
    assert calls == []


if __name__ == "__main__":
    asyncio.run(run())
    print("PASS: test_automatic_investigation.py self-check passed")
