"""Offline checks for the opt-in direct data tools (no Azure needed)."""

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://example.invalid/api/projects/dummy")
os.environ.setdefault("AZURE_AI_MODEL_DEPLOYMENT_NAME", "dummy-model")

import agent as agent_module  # noqa: E402
from data_tools import DirectDataTools, compare_to_baseline, kql_guard, sql_guard, summarize_optical  # noqa: E402

T0 = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)


def _series(baseline, recent, minutes_between=1):
    points = [(T0 + timedelta(minutes=i * minutes_between), v) for i, v in enumerate(baseline)]
    start = points[-1][0] + timedelta(minutes=60)
    points += [(start + timedelta(minutes=i), v) for i, v in enumerate(recent)]
    return points


def test_kql_guard():
    assert kql_guard("OpticalTelemetry | take 5") is None
    assert kql_guard("let x = 1; OpticalTelemetry | take x") is None
    assert "management" in kql_guard(".drop table OpticalTelemetry")
    assert kql_guard("externaldata (x:string) [@'https://evil']") is not None
    assert kql_guard("OpticalTelemetry | evaluate python(typeof(*), 'x')") is not None
    assert kql_guard("  ") == "empty query"


def test_sql_guard():
    assert sql_guard("SELECT TOP 5 * FROM dbo.DimTransportLink") is None
    assert sql_guard("WITH x AS (SELECT 1 AS a) SELECT a FROM x;") is None
    assert sql_guard("-- note\nSELECT 1") is None
    assert sql_guard("DELETE FROM dbo.DimService") is not None
    assert sql_guard("SELECT 1; DROP TABLE dbo.DimService") is not None
    assert sql_guard("SELECT * INTO dbo.copy FROM dbo.DimService") is not None
    assert sql_guard("EXEC sp_who") is not None


def test_baseline_flags_large_sustained_drop():
    base = [-10.0 + (0.05 if i % 2 else -0.05) for i in range(120)]
    result = compare_to_baseline(_series(base, [-18.0] * 15), signal="PowerDbm")
    assert result.status == "ABNORMAL" and result.pct_change < -5 and result.z_score < -3


def test_baseline_ignores_small_wobble():
    base = [-10.0 + (0.05 if i % 2 else -0.05) for i in range(120)]
    # z is huge on a flat signal but the change is < 5%, so NOT abnormal.
    result = compare_to_baseline(_series(base, [-10.3] * 15), signal="PowerDbm")
    assert result.status == "NORMAL"


def test_baseline_needs_history():
    result = compare_to_baseline(_series([-10.0] * 5, [-18.0] * 15), signal="PowerDbm")
    assert result.status == "INSUFFICIENT_BASELINE"


def test_trend_and_hours_to_threshold():
    base = [-10.0 + (0.05 if i % 2 else -0.05) for i in range(120)]
    falling = [-12.0 - i * 0.1 for i in range(15)]  # -6 dB/hour
    result = compare_to_baseline(_series(base, falling), signal="PowerDbm", alarm_threshold=-20.0)
    assert result.trend_per_hour is not None and result.trend_per_hour < -5
    assert result.hours_to_threshold is not None and 0 < result.hours_to_threshold < 2


def test_summarize_optical_groups_by_sensor_and_reports_age():
    rows = [
        {"Timestamp": ts, "SensorId": "S1", "PowerDbm": v, "Ber": 1e-9}
        for ts, v in _series([-10.0] * 60, [-18.0] * 15)
    ]
    summary = summarize_optical("LINK-X", rows)
    assert summary["sensors"]["S1"]["PowerDbm"]["status"] == "ABNORMAL"
    assert summary["stale"] is True and summary["data_age_hours"] is not None


def test_optical_check_rejects_injection():
    tools = DirectDataTools(credential=None)
    assert tools.optical_check("LINK-1' | take 1").startswith("REJECTED")


def test_direct_tools_are_opt_in(monkeypatch):
    instance = agent_module.NocAgent.__new__(agent_module.NocAgent)
    instance._available_agents = {}
    for name in ("FABRIC_KQL_QUERY_URI", "FABRIC_KQL_DATABASE_NAME", "FABRIC_SQL_ENDPOINT", "FABRIC_SQL_DATABASE"):
        monkeypatch.setenv(name, "x")
    monkeypatch.delenv("DIRECT_KQL_TOOL_ENABLED", raising=False)
    monkeypatch.delenv("DIRECT_SQL_TOOL_ENABLED", raising=False)
    instance._direct_tools = DirectDataTools(credential=None)
    assert instance._build_orchestrator_tools() == []
    assert "query_eventhouse_kql" not in instance._orchestrator_instructions()

    monkeypatch.setenv("DIRECT_KQL_TOOL_ENABLED", "true")
    monkeypatch.setenv("DIRECT_SQL_TOOL_ENABLED", "true")
    instance._direct_tools = DirectDataTools(credential=None)
    names = {t.name for t in instance._build_orchestrator_tools()}
    assert names == {"query_eventhouse_kql", "check_optical_baseline", "query_lakehouse_sql"}
    assert "query_lakehouse_sql" in instance._orchestrator_instructions()
