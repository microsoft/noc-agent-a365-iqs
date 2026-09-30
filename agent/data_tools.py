"""Direct, read-only data tools for the NOC orchestrator.

Ported from pranabpaul-tech/foundry-iq-v2 (direct Lakehouse T-SQL) and
pranabpaul-tech/pdm-fabric-foundry (guarded KQL, "model narrates, code computes").

All three tools run as the app's SERVICE identity, not the Teams user. That
bypasses per-user Fabric security, so each one is opt-in (env-gated in
agent.py) and the identity must hold read-only grants only: Eventhouse
database *viewer*, Fabric workspace *Viewer*. The guards below are cheap
belt-and-braces in front of those grants, not the real control.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import struct
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

MAX_ROWS = 200
QUERY_TIMEOUT_SECONDS = 60
SQL_COPT_SS_ACCESS_TOKEN = 1256  # pyodbc attribute for an Entra access token

_KQL_BANNED = ("externaldata", "evaluate ", "http_request", "sql_request", "cosmosdb_request", ".ingest", ".set", ".append", ".drop")
_SQL_WRITE = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute|grant|revoke|deny|into|openrowset|opendatasource)\b",
    re.IGNORECASE,
)
_ENTITY_ID = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,63}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def kql_guard(kql: str) -> Optional[str]:
    """Return a rejection reason, or None if the query may run."""
    stripped = kql.strip()
    if not stripped:
        return "empty query"
    if stripped.startswith("."):
        return "management commands (leading '.') are not allowed; this tool is read-only"
    lowered = stripped.lower()
    for banned in _KQL_BANNED:
        if banned in lowered:
            return f"'{banned.strip()}' is not allowed in this tool"
    return None


def sql_guard(sql: str) -> Optional[str]:
    """Allow exactly one SELECT (optionally WITH ... SELECT) statement."""
    body = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.DOTALL).strip().rstrip(";").strip()
    if not body:
        return "empty query"
    if not re.match(r"^(select|with)\b", body, re.IGNORECASE):
        return "only a single read-only SELECT statement is allowed"
    if ";" in body:
        return "only one statement per call is allowed"
    match = _SQL_WRITE.search(body)
    if match:
        return f"'{match.group(0)}' is not allowed in this tool"
    return None


# ---------------------------------------------------------------------------
# Deterministic analysis ("the model narrates, the numbers are computed in code")
# ---------------------------------------------------------------------------


@dataclass
class BaselineResult:
    signal: str
    status: str  # NORMAL | ABNORMAL | INSUFFICIENT_BASELINE | NO_RECENT_DATA
    recent_mean: Optional[float]
    baseline_mean: Optional[float]
    baseline_stdev: Optional[float]
    z_score: Optional[float]
    pct_change: Optional[float]
    baseline_points: int
    recent_points: int
    trend_per_hour: Optional[float]
    hours_to_threshold: Optional[float]


def compare_to_baseline(
    points: Sequence[tuple[datetime, float]],
    *,
    signal: str,
    recent_minutes: int = 15,
    gap_minutes: int = 30,
    min_baseline_points: int = 30,
    z_threshold: float = 3.0,
    pct_threshold: float = 5.0,
    alarm_threshold: Optional[float] = None,
) -> BaselineResult:
    """Compare the newest `recent_minutes` of a signal with its own history.

    ABNORMAL needs both |z| >= z_threshold AND |pct change| >= pct_threshold,
    so tiny-but-statistically-odd wobbles on a very flat signal are not flagged.
    The baseline excludes the `gap_minutes` before the recent window so a slow
    onset does not contaminate its own baseline.
    """
    clean = sorted((ts, float(v)) for ts, v in points if v is not None)
    if not clean:
        return BaselineResult(signal, "NO_RECENT_DATA", None, None, None, None, None, 0, 0, None, None)
    anchor = clean[-1][0]
    recent_start = anchor - timedelta(minutes=recent_minutes)
    baseline_end = recent_start - timedelta(minutes=gap_minutes)
    recent = [(ts, v) for ts, v in clean if ts > recent_start]
    baseline = [v for ts, v in clean if ts <= baseline_end]

    recent_values = [v for _, v in recent]
    recent_mean = statistics.fmean(recent_values)
    trend = _slope_per_hour(recent) if len(recent) >= 3 else None
    hours_to = None
    if alarm_threshold is not None and trend:
        remaining = alarm_threshold - recent_values[-1]
        if remaining * trend > 0:  # moving towards the threshold
            hours_to = round(remaining / trend, 2)

    if len(baseline) < min_baseline_points:
        return BaselineResult(signal, "INSUFFICIENT_BASELINE", round(recent_mean, 4), None, None, None, None,
                              len(baseline), len(recent), trend, hours_to)

    base_mean = statistics.fmean(baseline)
    base_sd = statistics.pstdev(baseline)
    z = (recent_mean - base_mean) / base_sd if base_sd > 0 else (0.0 if recent_mean == base_mean else float("inf"))
    pct = (recent_mean - base_mean) / abs(base_mean) * 100 if base_mean else 0.0
    abnormal = abs(z) >= z_threshold and abs(pct) >= pct_threshold
    return BaselineResult(
        signal,
        "ABNORMAL" if abnormal else "NORMAL",
        round(recent_mean, 4),
        round(base_mean, 4),
        round(base_sd, 4),
        round(z, 2) if z != float("inf") else z,
        round(pct, 2),
        len(baseline),
        len(recent),
        trend,
        hours_to,
    )


def _slope_per_hour(points: Sequence[tuple[datetime, float]]) -> Optional[float]:
    t0 = points[0][0]
    xs = [(ts - t0).total_seconds() / 3600 for ts, _ in points]
    ys = [v for _, v in points]
    if len(set(xs)) < 2:
        return None
    return round(statistics.linear_regression(xs, ys).slope, 4)


# ---------------------------------------------------------------------------
# Azure-backed runners (thin; everything testable lives above)
# ---------------------------------------------------------------------------


class DirectDataTools:
    """Holds lazily-built Kusto/SQL clients for the orchestrator's direct tools."""

    def __init__(self, credential: Any):
        self._credential = credential
        self._kusto = None
        self.kql_uri = os.getenv("FABRIC_KQL_QUERY_URI", "").strip()
        self.kql_database = os.getenv("FABRIC_KQL_DATABASE_NAME", "").strip()
        self.sql_endpoint = os.getenv("FABRIC_SQL_ENDPOINT", "").strip()
        self.sql_database = os.getenv("FABRIC_SQL_DATABASE", "").strip()

    @property
    def kql_enabled(self) -> bool:
        return bool(self.kql_uri and self.kql_database) and _flag("DIRECT_KQL_TOOL_ENABLED")

    @property
    def sql_enabled(self) -> bool:
        return bool(self.sql_endpoint and self.sql_database) and _flag("DIRECT_SQL_TOOL_ENABLED")

    # --- KQL -------------------------------------------------------------

    def _kusto_client(self):
        if self._kusto is None:
            from azure.kusto.data import KustoClient, KustoConnectionStringBuilder

            builder = KustoConnectionStringBuilder.with_azure_token_credential(self.kql_uri, self._credential)
            self._kusto = KustoClient(builder)
        return self._kusto

    def _kql_rows(self, kql: str, limit: int) -> tuple[list[dict], int]:
        from azure.kusto.data import ClientRequestProperties

        props = ClientRequestProperties()
        props.set_option("servertimeout", timedelta(seconds=QUERY_TIMEOUT_SECONDS))  # SDK needs a timedelta
        # execute_query, never execute: execute() auto-routes '.' management commands.
        table = self._kusto_client().execute_query(self.kql_database, kql, props).primary_results[0]
        columns = [c.column_name for c in table.columns]
        return [dict(zip(columns, row)) for row in table.rows[:limit]], len(table.rows)

    def run_kql(self, kql: str) -> str:
        reason = kql_guard(kql)
        if reason:
            return f"REJECTED: {reason}"
        try:
            rows, total = self._kql_rows(kql.strip(), MAX_ROWS)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the model verbatim
            return f"QUERY FAILED: {exc}"
        return json.dumps(
            {"as_of_utc": _now_iso(), "rows": rows, "row_count": total, "truncated": total > MAX_ROWS},
            default=str,
        )

    def optical_check(self, link_id: str, alarm_threshold_dbm: Optional[float] = None) -> str:
        link = link_id.strip().upper()
        if not _ENTITY_ID.match(link):
            return "REJECTED: link_id must look like LINK-SYD-MEL-FIBRE-01"
        kql = (
            f"let anchor = toscalar(OpticalTelemetry | where LinkId == '{link}' | summarize max(Timestamp));\n"
            f"OpticalTelemetry | where LinkId == '{link}' and Timestamp between ((anchor - 14d) .. anchor)\n"
            "| project Timestamp, SensorId, PowerDbm, Ber | order by Timestamp asc | take 20000"
        )
        try:
            rows, _ = self._kql_rows(kql, 20000)
        except Exception as exc:  # noqa: BLE001
            return f"QUERY FAILED: {exc}"
        return json.dumps(summarize_optical(link, rows, alarm_threshold_dbm), default=str)

    # --- SQL -------------------------------------------------------------

    def run_sql(self, sql: str) -> str:
        reason = sql_guard(sql)
        if reason:
            return f"REJECTED: {reason}"
        try:
            import pyodbc  # lazy: only needed when the SQL tool is enabled
        except ImportError:
            return "QUERY FAILED: pyodbc is not installed on this host"
        token = self._credential.get_token("https://database.windows.net/.default").token
        raw = token.encode("utf-16-le")
        packed = struct.pack(f"<I{len(raw)}s", len(raw), raw)
        conn_str = (
            "Driver={ODBC Driver 18 for SQL Server};"
            f"Server={self.sql_endpoint},1433;Database={self.sql_database};Encrypt=yes;TrustServerCertificate=no;"
        )
        try:
            with pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: packed}, timeout=30) as conn:
                cursor = conn.cursor()
                cursor.execute(sql.strip().rstrip(";"))
                columns = [c[0] for c in cursor.description]
                fetched = cursor.fetchmany(MAX_ROWS + 1)
        except Exception as exc:  # noqa: BLE001
            return f"QUERY FAILED: {exc}"
        rows = [dict(zip(columns, row)) for row in fetched[:MAX_ROWS]]
        return json.dumps({"as_of_utc": _now_iso(), "rows": rows, "truncated": len(fetched) > MAX_ROWS}, default=str)


def summarize_optical(link_id: str, rows: list[dict], alarm_threshold_dbm: Optional[float] = None) -> dict:
    """Per-sensor PowerDbm/Ber baseline comparison for one link."""
    by_sensor: dict[str, dict[str, list]] = {}
    newest: Optional[datetime] = None
    for row in rows:
        ts = row["Timestamp"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        newest = ts if newest is None or ts > newest else newest
        sensor = by_sensor.setdefault(str(row.get("SensorId")), {"PowerDbm": [], "Ber": []})
        sensor["PowerDbm"].append((ts, row.get("PowerDbm")))
        sensor["Ber"].append((ts, row.get("Ber")))
    sensors = {
        sensor_id: {
            "PowerDbm": asdict(compare_to_baseline(series["PowerDbm"], signal="PowerDbm", alarm_threshold=alarm_threshold_dbm)),
            "Ber": asdict(compare_to_baseline(series["Ber"], signal="Ber")),
        }
        for sensor_id, series in sorted(by_sensor.items())
    }
    age_hours = None
    if newest is not None:
        aware = newest if newest.tzinfo else newest.replace(tzinfo=timezone.utc)
        age_hours = round((datetime.now(timezone.utc) - aware).total_seconds() / 3600, 1)
    return {
        "as_of_utc": _now_iso(),
        "link_id": link_id,
        "newest_reading_utc": str(newest) if newest else None,
        "data_age_hours": age_hours,
        "stale": age_hours is None or age_hours > 1,
        "sensors": sensors,
        "note": "Baseline statistics are computed in code; ABNORMAL needs |z|>=3 and |change|>=5%. "
        "This is evidence, not a failure prediction.",
    }


def _flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes"}
