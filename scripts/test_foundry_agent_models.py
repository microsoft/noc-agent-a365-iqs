"""Focused self-check for persisted specialist model-profile resolution."""

import os

from create_foundry_agents import SPECIALISTS, specialist_model


def run() -> None:
    original = dict(os.environ)
    try:
        for spec in SPECIALISTS:
            os.environ.pop(spec.model_env, None)
        assert all(specialist_model(spec, "gpt-5.4-mini") == "gpt-5.4-mini" for spec in SPECIALISTS)

        knowledge = next(spec for spec in SPECIALISTS if spec.agent_name == "noc-knowledge-agent")
        os.environ[knowledge.model_env] = "gpt-5.4"
        assert specialist_model(knowledge, "gpt-5.4-mini") == "gpt-5.4"

        os.environ[knowledge.model_env] = "   "
        assert specialist_model(knowledge, "gpt-5.4-mini") == "gpt-5.4-mini"
    finally:
        os.environ.clear()
        os.environ.update(original)


if __name__ == "__main__":
    run()
    print("PASS: test_foundry_agent_models.py self-check passed")
