"""Append one synthetic, current-timestamp fibre anomaly for the Teams monitor demo.

The script is non-destructive: it never clears or replaces Eventhouse data. It
prints a preview unless --execute is supplied, and refuses to reuse an existing
incident ID unless --allow-duplicate is explicitly set.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from azure.identity import AzureDeveloperCliCredential
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env", override=True)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _scalar(response) -> int:
    table = response.primary_results[0]
    row = next(iter(table), None)
    return int(row[0]) if row is not None else 0


def build_commands(incident_id: str, now: datetime) -> list[tuple[str, str]]:
    sensor_id = "OPT-SYD-MEL-01"
    link_id = "LINK-SYD-MEL-FIBRE-01"
    baseline_at = _iso(now - timedelta(seconds=60))
    anomaly_at = _iso(now - timedelta(seconds=5))
    alert_at = _iso(now - timedelta(seconds=3))
    detected_at = _iso(now)
    alert_id = f"ALERT-{incident_id}"

    return [
        (
            "OpticalTelemetry baseline and anomaly",
            ".set-or-append OpticalTelemetry <| "
            "datatable(Timestamp:datetime, SensorId:string, LinkId:string, "
            "PowerDbm:real, Ber:real, UtilizationPct:real)["
            f"datetime({baseline_at}), {_string(sensor_id)}, {_string(link_id)}, -7.2, 1.9e-12, 71.0, "
            f"datetime({anomaly_at}), {_string(sensor_id)}, {_string(link_id)}, -39.5, 0.0046, 96.0]",
        ),
        (
            "NetworkAlerts critical optical-loss alert",
            ".set-or-append NetworkAlerts <| "
            "datatable(Timestamp:datetime, AlertId:string, IncidentId:string, EntityId:string, "
            "Severity:string, AlertType:string, Suppressed:bool, AckedAt:datetime, AckedBy:string)["
            f"datetime({alert_at}), {_string(alert_id)}, {_string(incident_id)}, {_string(link_id)}, "
            f"'Critical', 'OpticalSignalLoss', false, datetime(null), '']",
        ),
        (
            "IncidentEvents detected trigger",
            ".set-or-append IncidentEvents <| "
            "datatable(Timestamp:datetime, IncidentId:string, Stage:string, Detail:string, Actor:string)["
            f"datetime({detected_at}), {_string(incident_id)}, 'Detected', "
            "'Synthetic PoC replay: optical power collapse and BER spike on the Sydney-Melbourne primary fibre', "
            "'demo.replay']",
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--incident-id",
        default=f"INC-DEMO-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        help="Unique incident ID. Defaults to a UTC timestamp-based demo ID.",
    )
    parser.add_argument("--execute", action="store_true", help="Append the synthetic rows.")
    parser.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="Allow an incident ID already present in IncidentEvents (normally refused).",
    )
    args = parser.parse_args()

    query_uri = os.getenv("FABRIC_KQL_QUERY_URI", "").strip()
    database = os.getenv("FABRIC_KQL_DATABASE_NAME", "").strip()
    tenant_id = os.getenv("FABRIC_TENANT_ID", "").strip() or os.getenv("AZURE_TENANT_ID", "").strip()
    missing = [
        name
        for name, value in (
            ("FABRIC_KQL_QUERY_URI", query_uri),
            ("FABRIC_KQL_DATABASE_NAME", database),
            ("FABRIC_TENANT_ID or AZURE_TENANT_ID", tenant_id),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("Missing required .env settings: " + ", ".join(missing))

    now = datetime.now(timezone.utc).replace(microsecond=0)
    commands = build_commands(args.incident_id, now)
    print(f"Incident ID: {args.incident_id}")
    print(f"Detected timestamp: {_iso(now)}")
    print("Rows: 2 OpticalTelemetry, 1 NetworkAlerts, 1 IncidentEvents(Detected)")

    if not args.execute:
        print("Preview only; no data written. Re-run with --execute to append the anomaly.")
        return

    credential = AzureDeveloperCliCredential(tenant_id=tenant_id, process_timeout=60)
    builder = KustoConnectionStringBuilder.with_azure_token_credential(query_uri, credential)
    try:
        with KustoClient(builder) as client:
            existing = _scalar(
                client.execute_query(
                    database,
                    f"IncidentEvents | where IncidentId == {_string(args.incident_id)} | count",
                )
            )
            if existing and not args.allow_duplicate:
                raise RuntimeError(
                    f"Incident ID {args.incident_id!r} already has {existing} IncidentEvents row(s); "
                    "choose another ID or pass --allow-duplicate."
                )

            for label, command in commands:
                client.execute_mgmt(database, command)
                print(f"Appended: {label}")

            verification = client.execute_query(
                database,
                "union "
                f"(OpticalTelemetry | where Timestamp between (datetime({_iso(now - timedelta(minutes=2))}) "
                f".. datetime({_iso(now + timedelta(minutes=1))})) and LinkId == 'LINK-SYD-MEL-FIBRE-01' "
                "| summarize Rows=count() | extend Table='OpticalTelemetry'), "
                f"(NetworkAlerts | where IncidentId == {_string(args.incident_id)} "
                "| summarize Rows=count() | extend Table='NetworkAlerts'), "
                f"(IncidentEvents | where IncidentId == {_string(args.incident_id)} "
                "| summarize Rows=count() | extend Table='IncidentEvents') "
                "| project Table, Rows",
            )
            print("Verification:")
            for row in verification.primary_results[0]:
                print(f"  {row['Table']}: {row['Rows']}")
    finally:
        credential.close()

    print("Replay appended. With the monitor enabled, allow up to one poll interval plus investigation time.")


if __name__ == "__main__":
    main()
