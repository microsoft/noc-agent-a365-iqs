"""Create or reuse a Fabric Eventhouse + default KQL database in the existing workspace.

This script follows scripts/create_fabric_ontology.py's create-or-get shape, but it only
adds a second item to the already-provisioned Fabric workspace used by the ontology.

Environment variables (from the repo-root .env):
  FABRIC_WORKSPACE_ID   - Existing Fabric workspace GUID (preferred)
  FABRIC_TENANT_ID      - Required Microsoft Entra tenant ID for Fabric auth
  FABRIC_EVENTHOUSE_ID  - Existing Eventhouse GUID to reuse/update (optional)
  FABRIC_KQL_DB_ID      - Existing KQL database GUID to reuse if it belongs to the Eventhouse (optional)
"""

import os
import sys
import time
import warnings
import csv
import json
from datetime import datetime
from pathlib import Path

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity import AzureDeveloperCliCredential
from azure.kusto.data import DataFormat, KustoClient, KustoConnectionStringBuilder
from azure.kusto.ingest import IngestionProperties, QueuedIngestClient
from dotenv import load_dotenv, set_key

warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"microsoft_fabric_api\..*")

from microsoft_fabric_api import FabricClient  # noqa: E402
from microsoft_fabric_api.generated.eventhouse.models import (  # noqa: E402
    CreateEventhouseRequest,
    UpdateEventhouseRequest,
)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
ENV_PATH = REPO_ROOT / ".env"
TELEMETRY_DIR = REPO_ROOT / "data" / "telemetry"
load_dotenv(ENV_PATH, override=True)

FABRIC_TENANT_ID = os.getenv("FABRIC_TENANT_ID", "").strip().strip("'\"")
WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "").strip().strip("'\"")
WORKSPACE_NAME = os.getenv("FABRIC_WORKSPACE_NAME", "NOCTopologyWorkspace").strip().strip("'\"")
EVENTHOUSE_ID = os.getenv("FABRIC_EVENTHOUSE_ID", "").strip().strip("'\"")
KQL_DB_ID = os.getenv("FABRIC_KQL_DB_ID", "").strip().strip("'\"")
EVENTHOUSE_NAME = "NOCIncidentEventhouse"
TABLE_SPECS = {
    "OpticalTelemetry": {
        "csv": TELEMETRY_DIR / "OpticalTelemetry.csv",
        "columns": [
            ("Timestamp", "datetime"),
            ("SensorId", "string"),
            ("LinkId", "string"),
            ("PowerDbm", "real"),
            ("Ber", "real"),
            ("UtilizationPct", "real"),
        ],
    },
    "NetworkAlerts": {
        "csv": TELEMETRY_DIR / "NetworkAlerts.csv",
        "columns": [
            ("Timestamp", "datetime"),
            ("AlertId", "string"),
            ("IncidentId", "string"),
            ("EntityId", "string"),
            ("Severity", "string"),
            ("AlertType", "string"),
            ("Suppressed", "bool"),
            ("AckedAt", "datetime"),
            ("AckedBy", "string"),
        ],
    },
    "IncidentEvents": {
        "csv": TELEMETRY_DIR / "IncidentEvents.csv",
        "columns": [
            ("Timestamp", "datetime"),
            ("IncidentId", "string"),
            ("Stage", "string"),
            ("Detail", "string"),
            ("Actor", "string"),
        ],
    },
}

_CREDENTIAL = None
_FABRIC_CLIENT = None


def log_message(message: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")


def update_root_env(values: dict):
    for key, value in values.items():
        set_key(str(ENV_PATH), key, value)


def get_eventhouse_mcp_url(workspace_id: str, item_id: str) -> str:
    return (
        "https://api.fabric.microsoft.com/v1/mcp/dataPlane/"
        f"workspaces/{workspace_id}/items/{item_id}/kqlEndpoint"
    )


def get_credential():
    global _CREDENTIAL
    if not FABRIC_TENANT_ID:
        raise RuntimeError("FABRIC_TENANT_ID is required for Fabric authentication.")
    if _CREDENTIAL is None:
        _CREDENTIAL = AzureDeveloperCliCredential(tenant_id=FABRIC_TENANT_ID)
    return _CREDENTIAL


def get_fabric_client() -> FabricClient:
    global _FABRIC_CLIENT
    if _FABRIC_CLIENT is None:
        _FABRIC_CLIENT = FabricClient(get_credential())
    return _FABRIC_CLIENT


def resolve_workspace() -> dict:
    if not WORKSPACE_ID and not WORKSPACE_NAME:
        raise RuntimeError("Set FABRIC_WORKSPACE_ID (preferred) or FABRIC_WORKSPACE_NAME in .env.")

    for workspace in get_fabric_client().core.workspaces.list_workspaces():
        if WORKSPACE_ID and workspace.id == WORKSPACE_ID:
            return {
                "id": workspace.id,
                "displayName": workspace.display_name,
                "capacityId": getattr(workspace, "capacity_id", ""),
            }
        if not WORKSPACE_ID and workspace.display_name == WORKSPACE_NAME:
            return {
                "id": workspace.id,
                "displayName": workspace.display_name,
                "capacityId": getattr(workspace, "capacity_id", ""),
            }

    raise RuntimeError(
        f"Could not find the existing Fabric workspace (id={WORKSPACE_ID or '<unset>'}, "
        f"name={WORKSPACE_NAME or '<unset>'})."
    )


def get_existing_eventhouse(workspace_id: str, name: str) -> dict | None:
    for eventhouse in get_fabric_client().eventhouse.items.list_eventhouses(workspace_id):
        if eventhouse.display_name == name:
            return {"id": eventhouse.id, "displayName": eventhouse.display_name}
    return None


def create_or_get_eventhouse(workspace_id: str, name: str) -> dict:
    client = get_fabric_client()

    if EVENTHOUSE_ID:
        eventhouse = client.eventhouse.items.get_eventhouse(workspace_id, EVENTHOUSE_ID)
        if eventhouse.display_name != name:
            eventhouse = client.eventhouse.items.update_eventhouse(
                workspace_id,
                EVENTHOUSE_ID,
                UpdateEventhouseRequest(display_name=name),
            )
        return {"id": eventhouse.id, "displayName": eventhouse.display_name}

    existing = get_existing_eventhouse(workspace_id, name)
    if existing:
        log_message(f"Found existing Eventhouse: {existing['id']}")
        return existing

    log_message(f"Creating Eventhouse '{name}'...")
    eventhouse = client.eventhouse.items.create_eventhouse(
        workspace_id,
        CreateEventhouseRequest(
            display_name=name,
            description="NOC incident Eventhouse for real-time operations data.",
        ),
    )
    log_message(f"Eventhouse created: {eventhouse.id}")
    return {"id": eventhouse.id, "displayName": eventhouse.display_name}


def resolve_default_kql_database(workspace_id: str, eventhouse_id: str) -> dict:
    client = get_fabric_client()
    deadline = time.monotonic() + 180

    while time.monotonic() < deadline:
        eventhouse = client.eventhouse.items.get_eventhouse(workspace_id, eventhouse_id)
        db_ids = list(getattr(eventhouse.properties, "databases_item_ids", []) or [])
        if not db_ids:
            time.sleep(3)
            continue

        # ponytail: reuse Fabric's auto-created default ReadWrite DB; add explicit multi-DB creation only if
        # eventhouse-seed needs tenant isolation later.
        preferred_ids = [KQL_DB_ID] + db_ids if KQL_DB_ID else db_ids
        seen = set()
        for db_id in preferred_ids:
            if not db_id or db_id in seen:
                continue
            seen.add(db_id)
            try:
                database = client.kqldatabase.items.get_kql_database(workspace_id, db_id)
            except ResourceNotFoundError:
                continue
            if getattr(database.properties, "parent_eventhouse_item_id", "") != eventhouse_id:
                continue
            return {
                "id": database.id,
                "displayName": database.display_name,
                "queryServiceUri": getattr(database.properties, "query_service_uri", ""),
                "ingestionServiceUri": getattr(database.properties, "ingestion_service_uri", ""),
                "databaseType": getattr(database.properties, "database_type", ""),
            }

        time.sleep(3)

    raise TimeoutError("Timed out waiting for the Eventhouse default KQL database to become available.")


def read_csv_header_and_count(csv_path: Path) -> tuple[list[str], int]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        return header, sum(1 for _ in reader)


def build_csv_mapping(columns: list[tuple[str, str]]) -> str:
    return json.dumps(
        [
            {
                "column": name,
                "datatype": data_type,
                "Properties": {"Ordinal": str(index)},
            }
            for index, (name, data_type) in enumerate(columns)
        ]
    )


def get_table_row_count(kusto_client: KustoClient, database_name: str, table_name: str) -> int:
    result = kusto_client.execute_query(database_name, f"{table_name} | count")
    row = next(iter(result.primary_results[0]), None)
    if row is None:
        return 0
    return int(row[result.primary_results[0].columns[0].column_name])


def ensure_kql_table_and_mapping(
    kusto_client: KustoClient,
    database_name: str,
    table_name: str,
    columns: list[tuple[str, str]],
    csv_header: list[str],
) -> None:
    expected_header = [name for name, _ in columns]
    if csv_header != expected_header:
        raise RuntimeError(
            f"{table_name} header mismatch. Expected {expected_header}, found {csv_header}."
        )

    schema = ", ".join(f"{name}: {data_type}" for name, data_type in columns)
    mapping_name = f"{table_name}_mapping"
    mapping_json = build_csv_mapping(columns)

    kusto_client.execute_mgmt(database_name, f".create-merge table {table_name} ({schema})")
    kusto_client.execute_mgmt(
        database_name,
        f'.create-or-alter table {table_name} ingestion csv mapping "{mapping_name}" \'{mapping_json}\'',
    )


def seed_table(
    kusto_client: KustoClient,
    ingest_client: QueuedIngestClient,
    database_name: str,
    table_name: str,
    csv_path: Path,
    expected_rows: int,
) -> int:
    mapping_name = f"{table_name}_mapping"
    current_rows = get_table_row_count(kusto_client, database_name, table_name)
    if current_rows and os.getenv("FABRIC_EVENTHOUSE_RESEED", "false").lower() != "true":
        log_message(
            f"{table_name} already has {current_rows} rows; preserving data "
            "(set FABRIC_EVENTHOUSE_RESEED=true to replace it)."
        )
        return current_rows

    if current_rows:
        kusto_client.execute_mgmt(database_name, f".clear table {table_name} data")

    ingest_props = IngestionProperties(
        database=database_name,
        table=table_name,
        data_format=DataFormat.CSV,
        ingestion_mapping_reference=mapping_name,
        ignore_first_record=True,
    )
    ingest_client.ingest_from_file(str(csv_path), ingestion_properties=ingest_props)

    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        row_count = get_table_row_count(kusto_client, database_name, table_name)
        if row_count == expected_rows:
            return row_count
        time.sleep(10)

    raise TimeoutError(
        f"Timed out waiting for {table_name} ingestion to reach {expected_rows} rows."
    )


def seed_demo_tables(database: dict) -> dict[str, int]:
    if not database["queryServiceUri"] or not database["ingestionServiceUri"]:
        raise RuntimeError("KQL database is missing query or ingestion service URIs.")

    credential = get_credential()
    query_kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
        database["queryServiceUri"], credential
    )
    ingest_kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
        database["ingestionServiceUri"], credential
    )

    counts: dict[str, int] = {}
    with KustoClient(query_kcsb) as kusto_client, QueuedIngestClient(ingest_kcsb) as ingest_client:
        for table_name, spec in TABLE_SPECS.items():
            csv_path = spec["csv"]
            if not csv_path.exists():
                raise FileNotFoundError(f"Telemetry CSV not found: {csv_path}")

            csv_header, expected_rows = read_csv_header_and_count(csv_path)
            ensure_kql_table_and_mapping(
                kusto_client,
                database["displayName"],
                table_name,
                spec["columns"],
                csv_header,
            )
            counts[table_name] = seed_table(
                kusto_client,
                ingest_client,
                database["displayName"],
                table_name,
                csv_path,
                expected_rows,
            )

    return counts


def self_check():
    workspace_id = "ws"
    item_id = "item"
    expected = "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/ws/items/item/kqlEndpoint"
    assert get_eventhouse_mcp_url(workspace_id, item_id) == expected
    mapping = json.loads(build_csv_mapping(TABLE_SPECS["IncidentEvents"]["columns"]))
    assert mapping[0]["column"] == "Timestamp"
    assert mapping[0]["Properties"]["Ordinal"] == "0"


def main():
    self_check()
    log_message("=" * 60)
    log_message("Fabric Eventhouse + KQL Database Creator")
    log_message("=" * 60)

    workspace = resolve_workspace()
    log_message(
        f"Workspace: {workspace['displayName']} ({workspace['id']}) on capacity {workspace['capacityId'] or 'unknown'}"
    )

    eventhouse = create_or_get_eventhouse(workspace["id"], EVENTHOUSE_NAME)
    database = resolve_default_kql_database(workspace["id"], eventhouse["id"])
    mcp_url = get_eventhouse_mcp_url(workspace["id"], database["id"])

    update_root_env(
        {
            "FABRIC_WORKSPACE_ID": workspace["id"],
            "FABRIC_EVENTHOUSE_ID": eventhouse["id"],
            "FABRIC_KQL_DB_ID": database["id"],
            "FABRIC_KQL_QUERY_URI": database["queryServiceUri"],
            "FABRIC_KQL_DATABASE_NAME": database["displayName"],
            "FABRIC_RTI_MCP_URL": mcp_url,
        }
    )

    table_counts = seed_demo_tables(database)

    log_message(f"Eventhouse: {eventhouse['displayName']} ({eventhouse['id']})")
    log_message(f"KQL database: {database['displayName']} ({database['id']}) [{database['databaseType']}]")
    if database["queryServiceUri"]:
        log_message(f"Query URI: {database['queryServiceUri']}")
    if database["ingestionServiceUri"]:
        log_message(f"Ingestion URI: {database['ingestionServiceUri']}")
    log_message(f"RTI MCP endpoint: {mcp_url}")
    for table_name, row_count in table_counts.items():
        log_message(f"{table_name} rows: {row_count}")


if __name__ == "__main__":
    try:
        main()
    except HttpResponseError as error:
        log_message(f"ERROR: Fabric API request failed: {error}")
        sys.exit(1)
    except Exception as error:  # noqa: BLE001
        log_message(f"ERROR: {error}")
        sys.exit(1)
