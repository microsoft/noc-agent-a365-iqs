"""Standalone self-check for run-ledger header wiring.

Run directly: `python agent/test_run_headers.py`.
"""

import asyncio
import hashlib
import os

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://example.invalid/api/projects/dummy")
os.environ.setdefault("AZURE_AI_MODEL_DEPLOYMENT_NAME", "dummy-model")

import agent  # noqa: E402  (import after env setup, by design)


def _check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"{status}: {label}")
    assert condition, label


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def main():
    noc = agent.NocAgent()

    run_id = noc._get_or_create_run_id("conv-1", "activity-1")
    same = noc._get_or_create_run_id("conv-1", "activity-1")
    other = noc._get_or_create_run_id("conv-1", "activity-2")
    _check("same conversation/activity keeps the same run id", run_id == same)
    _check("different activity gets a different run id", run_id != other)

    calls = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json, headers=None):
            calls.append({"url": url, "json": json, "headers": headers, "timeout": self.timeout})
            return _FakeResponse({"run_token": f"token-for:{json['run_id']}"})

    original_client = agent.httpx.AsyncClient
    original_base_url = os.environ.get("RUN_LEDGER_BASE_URL")
    try:
        os.environ["RUN_LEDGER_BASE_URL"] = "https://ledger.example"
        agent.httpx.AsyncClient = FakeAsyncClient

        first = asyncio.run(noc._get_or_create_run_token("conv-1", "activity-1"))
        second = asyncio.run(noc._get_or_create_run_token("conv-1", "activity-1"))
        third = asyncio.run(noc._get_or_create_run_token("conv-1", "activity-2"))

        _check("same conversation/activity reuses the cached run token", first == second)
        _check("same conversation/activity only hits the ledger once", len(calls) == 2)
        _check("cached token stored under the composite key", noc._run_token_cache[("conv-1", "activity-1")] == first)
        _check("different activity triggers a different token", third != first)
        _check("ledger payload uses the deterministic run id", calls[0]["json"]["run_id"] == run_id)

        calls.clear()
        prompt = "Investigate LINK-SYD-MEL-FIBRE-01"
        asyncio.run(
            agent._run_ledger_precall(
                run_id="run-1",
                agent_name="planner",
                step="1",
                model="gpt-5.4",
                est_input_tokens=10,
                prompt=prompt,
            )
        )
        _check(
            "precall sends the required prompt hash",
            calls[0]["json"]["prompt_hash"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        )
        asyncio.run(agent._run_ledger_postcall("run-1", None, failed=True))
        _check("postcall skips when precall produced no reservation", len(calls) == 1)

        class ExplodingAsyncClient:
            def __init__(self, *args, **kwargs):
                raise AssertionError("RUN_LEDGER_BASE_URL unset should skip the ledger call")

        os.environ.pop("RUN_LEDGER_BASE_URL", None)
        agent.httpx.AsyncClient = ExplodingAsyncClient
        skipped = asyncio.run(noc._get_or_create_run_token("conv-2", "activity-1"))
        _check("RUN_LEDGER_BASE_URL unset skips ledger registration", skipped is None)
        _check("run headers are omitted cleanly without a run token", agent._build_run_headers(skipped, "1") == {})
    finally:
        agent.httpx.AsyncClient = original_client
        if original_base_url is None:
            os.environ.pop("RUN_LEDGER_BASE_URL", None)
        else:
            os.environ["RUN_LEDGER_BASE_URL"] = original_base_url

    print("PASS: test_run_headers.py self-check passed (11 checks)")


if __name__ == "__main__":
    main()
