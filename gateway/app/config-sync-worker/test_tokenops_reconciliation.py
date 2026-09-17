"""Focused self-checks for TokenOps run reconciliation."""

from check_usage_detail import event_cost, summarize_runs


PRICES = {"gpt-5.4": {"prompt": 0.0025, "completion": 0.015}}


def event(mode, input_tokens, output_tokens, *, kind="specialist", model="gpt-5.4"):
    return {
        "run_id": "run-1",
        "usage_kind": kind,
        "accounting_mode": mode,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def test_reconciliation_classifies_without_double_counting():
    rows = [
        event("actual", 1000, 100),
        event("estimate", 400, 40, kind="mcp_outer_estimate"),
        event("no_llm", 0, 0, kind="direct_graph", model=""),
    ]
    summary = summarize_runs(rows, PRICES)["run-1"]
    assert summary["rows"] == 3
    assert summary["actual_tokens"] == 1100
    assert summary["estimated_tokens"] == 440
    assert summary["no_llm_rows"] == 1
    assert round(summary["actual_cost"], 6) == 0.004
    assert round(summary["estimated_cost"], 6) == 0.0016
    assert event_cost(rows[2], PRICES) == 0


if __name__ == "__main__":
    test_reconciliation_classifies_without_double_counting()
    print("PASS: test_tokenops_reconciliation.py self-check passed")
