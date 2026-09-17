"""Focused self-check for classified TokenOps telemetry."""

import os

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://example.invalid/api/projects/dummy")
os.environ.setdefault("AZURE_AI_MODEL_DEPLOYMENT_NAME", "dummy-model")

import agent as agent_module  # noqa: E402


class FakeLogger:
    def __init__(self):
        self.records = []

    def info(self, message, *args, **kwargs):
        self.records.append((message, kwargs.get("extra", {})))


def run():
    fake = FakeLogger()
    original_logger = agent_module.logger
    run_handle = agent_module._current_run_id.set("run-1")
    user_handle = agent_module._current_user_ctx.set(("user-1", "Operator"))
    agent_module.logger = fake
    try:
        agent_module._emit_usage_event(
            agent_name="fabric-graph-direct",
            model_name="",
            input_tokens=0,
            output_tokens=0,
            query="blast radius",
            usage_kind="direct_graph",
            accounting_mode="no_llm",
        )
    finally:
        agent_module.logger = original_logger
        agent_module._current_user_ctx.reset(user_handle)
        agent_module._current_run_id.reset(run_handle)

    message, record = fake.records[0]
    assert message == "usage_event"
    assert record["run_id"] == "run-1"
    assert record["user_id"] == "user-1"
    assert record["usage_kind"] == "direct_graph"
    assert record["accounting_mode"] == "no_llm"
    assert record["input_tokens"] == record["output_tokens"] == 0


if __name__ == "__main__":
    run()
    print("PASS: test_tokenops_usage.py self-check passed")
