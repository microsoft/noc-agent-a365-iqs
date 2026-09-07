"""
Automate the Fabric IQ `GraphModel` node/edge build -- the one step that
docs/DEPLOYMENT.md §4a previously said had to be done manually on the Fabric
portal's graph canvas.

Background: `create_fabric_ontology.py` creates the `Ontology` item (schema +
per-entity data bindings). Creating that item auto-provisions a companion
`GraphModel` item (`<FABRIC_ONTOLOGY_NAME>_graph_<guid>`) in the same
workspace -- this is the item Fabric IQ/the Data Agent actually queries
against, and it starts out completely empty (`graphType.json`:
`{"nodeTypes": [], "edgeTypes": []}`, `graphDefinition.json`:
`{"nodeTables": [], "edgeTables": []}`). Previously this had to be filled in
by hand, once per environment, by adding all 8 node types + 6 edge types on
the portal's graph canvas (see docs/TROUBLESHOOTING.md "RESOLVED: manually
rebuilding all 8 nodes + 6 edges fixed Fabric IQ end-to-end").

This script does that programmatically via the (Preview) Fabric GraphModel
REST API:
  POST /v1/workspaces/{workspaceId}/graphModels/{graphModelId}/updateDefinition
(https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/update-graph-model-definition,
schema: https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/graph-model-definition)

Node/edge specs below mirror the entity/relationship shape in
create_fabric_ontology.py's build_noc_ontology_definition(), but the
source/destination key columns for each edge were re-verified directly
against the real data/ontology_entities/*.csv headers (not copied from that
script's relationship-contextualization wiring, which binds columns to
*property IDs* in a way that doesn't hold up under GraphModel's simpler
"positional FK columns" edge schema).

Environment variables (same repo-root .env as create_fabric_ontology.py):
  FABRIC_WORKSPACE_ID  - required (set by create_fabric_ontology.py)
  FABRIC_TENANT_ID     - required
  LAKEHOUSE_NAME        - default: NOCTopologyLakehouse
  FABRIC_ONTOLOGY_NAME  - default: NOCNetworkOntology
"""

import base64
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
load_dotenv(REPO_ROOT / ".env", override=True)

sys.path.insert(0, str(SCRIPT_DIR))
from create_fabric_ontology import (  # noqa: E402
    get_credential,
    get_fabric_client,
    get_existing_lakehouse,
    log_message,
)

FABRIC_API = "https://api.fabric.microsoft.com/v1"
WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "")
LAKEHOUSE_NAME = os.getenv("LAKEHOUSE_NAME", "NOCTopologyLakehouse")
FABRIC_ONTOLOGY_NAME = os.getenv("FABRIC_ONTOLOGY_NAME", "NOCNetworkOntology")

# --- Node type specs: alias == entity name, key column, (column, GraphModel type) pairs ---
NODE_SPECS = [
    ("CoreRouter", "DimCoreRouter", "RouterId", [
        ("RouterId", "STRING"), ("City", "STRING"), ("Region", "STRING"),
        ("Vendor", "STRING"), ("Model", "STRING"), ("FirmwareVersion", "STRING"),
    ]),
    ("TransportLink", "DimTransportLink", "LinkId", [
        ("LinkId", "STRING"), ("LinkType", "STRING"), ("CapacityGbps", "INT"),
        ("SourceRouterId", "STRING"), ("TargetRouterId", "STRING"),
    ]),
    ("PhysicalConduit", "DimPhysicalConduit", "ConduitId", [
        ("ConduitId", "STRING"), ("RouteDescription", "STRING"),
        ("MaterialType", "STRING"), ("InstalledYear", "INT"),
    ]),
    ("AmplifierSite", "DimAmplifierSite", "SiteId", [
        ("SiteId", "STRING"), ("Location", "STRING"),
        ("InstalledYear", "INT"), ("LastCalibration", "STRING"),
    ]),
    ("Service", "DimService", "ServiceId", [
        ("ServiceId", "STRING"), ("ServiceType", "STRING"), ("CustomerName", "STRING"),
        ("CustomerCount", "INT"), ("ActiveUsers", "INT"),
    ]),
    ("SLAPolicy", "DimSLAPolicy", "SLAPolicyId", [
        ("SLAPolicyId", "STRING"), ("ServiceId", "STRING"), ("AvailabilityPct", "FLOAT"),
        ("MaxLatencyMs", "INT"), ("PenaltyPerHourUSD", "INT"), ("Tier", "STRING"),
    ]),
    ("MPLSPath", "DimMPLSPath", "PathId", [
        ("PathId", "STRING"), ("PathType", "STRING"),
    ]),
    ("Advisory", "DimAdvisory", "AdvisoryId", [
        ("AdvisoryId", "STRING"), ("VendorName", "STRING"),
        ("Severity", "STRING"), ("Title", "STRING"),
    ]),
]

# --- Edge specs: (name, source_alias, target_alias, bridge_table, source_key_cols, dest_key_cols, filter) ---
# Columns verified directly against the real CSV headers under data/ontology_entities/.
EDGE_SPECS = [
    ("ORIGINATES_AT", "TransportLink", "CoreRouter", "DimTransportLink", ["LinkId"], ["SourceRouterId"], None),
    ("TERMINATES_AT", "TransportLink", "CoreRouter", "DimTransportLink", ["LinkId"], ["TargetRouterId"], None),
    ("RIDES_ON", "TransportLink", "PhysicalConduit", "FactConduitMapping", ["LinkId"], ["ConduitId"], None),
    ("AMPLIFIES", "AmplifierSite", "TransportLink", "FactAmplifierMapping", ["SiteId"], ["LinkId"], None),
    ("COVERS", "SLAPolicy", "Service", "DimSLAPolicy", ["SLAPolicyId"], ["ServiceId"], None),
    ("AFFECTS", "Advisory", "CoreRouter", "FactAdvisoryMapping", ["AdvisoryId"], ["RouterId"], None),
    # FactMPLSPathHops is polymorphic (NodeId is either a RouterId or a LinkId, per
    # NodeType) -- split into two filtered edges so MPLSPath actually connects to
    # the graph. Without this, MPLSPath is a fully disconnected node (found via
    # live inspection of the manually-built graph canvas).
    ("TRAVERSES_ROUTER", "MPLSPath", "CoreRouter", "FactMPLSPathHops", ["PathId"], ["NodeId"],
     {"operator": "Equal", "columnName": "NodeType", "value": "CoreRouter"}),
    ("TRAVERSES_LINK", "MPLSPath", "TransportLink", "FactMPLSPathHops", ["PathId"], ["NodeId"],
     {"operator": "Equal", "columnName": "NodeType", "value": "TransportLink"}),
]


def _auth_headers() -> dict:
    token = get_credential().get_token("https://api.fabric.microsoft.com/.default").token
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def find_graph_model_item(workspace_id: str, ontology_name: str) -> dict:
    """Find the auto-provisioned GraphModel item that's paired with the Ontology item."""
    resp = requests.get(f"{FABRIC_API}/workspaces/{workspace_id}/items", headers=_auth_headers(), timeout=30)
    resp.raise_for_status()
    items = resp.json().get("value", [])
    prefix = f"{ontology_name}_graph_"
    for item in items:
        if item.get("type") == "GraphModel" and item.get("displayName", "").startswith(prefix):
            return item
    for item in items:
        if item.get("type") == "GraphModel":
            log_message(f"WARNING: no GraphModel item named '{prefix}*' -- using '{item['displayName']}' instead")
            return item
    raise RuntimeError(
        f"No GraphModel item found in workspace {workspace_id}. "
        "Run create_fabric_ontology.py first to auto-provision it."
    )


def to_b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


def build_definition_parts(workspace_id: str, lakehouse_id: str) -> list[dict]:
    onelake_prefix = f"abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/{lakehouse_id}/Tables"

    tables = sorted({table for _, table, _, _ in NODE_SPECS} | {table for _, _, _, table, _, _, _ in EDGE_SPECS})
    data_sources = [
        {"name": table, "type": "DeltaTable", "properties": {"referenceName": "lakehouse", "path": f"Tables/{table}"}}
        for table in tables
    ]

    graph_type = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/graphIndex/definition/graphType/1.0.0/schema.json",
        "nodeTypes": [
            {
                "alias": alias,
                "labels": [alias],
                "primaryKeyProperties": [key],
                "properties": [{"name": col, "type": col_type} for col, col_type in columns],
            }
            for alias, _table, key, columns in NODE_SPECS
        ],
        "edgeTypes": [
            {
                "alias": name,
                "labels": [name],
                "sourceNodeType": {"alias": src},
                "destinationNodeType": {"alias": tgt},
                "properties": [],
            }
            for name, src, tgt, _table, _src_cols, _tgt_cols, _filter in EDGE_SPECS
        ],
    }

    graph_definition = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/graphIndex/definition/graphDefinition/1.0.0/schema.json",
        "nodeTables": [
            {
                "id": f"node_{alias}",
                "nodeTypeAlias": alias,
                "dataSourceName": table,
                "propertyMappings": [{"propertyName": col, "sourceColumn": col} for col, _t in columns],
            }
            for alias, table, _key, columns in NODE_SPECS
        ],
        "edgeTables": [
            {
                "id": f"edge_{name}",
                "edgeTypeAlias": name,
                "dataSourceName": table,
                "sourceNodeKeyColumns": src_cols,
                "destinationNodeKeyColumns": tgt_cols,
                "propertyMappings": [],
                **({"filter": edge_filter} if edge_filter else {}),
            }
            for name, _src, _tgt, table, src_cols, tgt_cols, edge_filter in EDGE_SPECS
        ],
    }

    styling = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/graphIndex/definition/stylingConfiguration/1.0.0/schema.json",
        "modelLayout": {
            "positions": {
                **{alias: {"x": i * 200, "y": 0} for i, (alias, *_r) in enumerate(NODE_SPECS)},
            },
            "styles": {alias: {"size": 30} for alias, *_r in NODE_SPECS},
            "pan": {"x": 0, "y": 0},
            "zoomLevel": 1,
        },
    }

    return [
        {"path": "dataSources.json", "payload": to_b64({
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/graphIndex/definition/dataSources/1.1.0/schema.json",
            "itemReferences": [{"name": "lakehouse", "item": {"workspaceId": workspace_id, "itemId": lakehouse_id}}],
            "dataSources": data_sources,
        }), "payloadType": "InlineBase64"},
        {"path": "graphType.json", "payload": to_b64(graph_type), "payloadType": "InlineBase64"},
        {"path": "graphDefinition.json", "payload": to_b64(graph_definition), "payloadType": "InlineBase64"},
        {"path": "stylingConfiguration.json", "payload": to_b64(styling), "payloadType": "InlineBase64"},
    ]


def update_graph_model_definition(workspace_id: str, graph_model_id: str, parts: list[dict]) -> None:
    url = f"{FABRIC_API}/workspaces/{workspace_id}/graphModels/{graph_model_id}/updateDefinition"
    resp = requests.post(url, headers=_auth_headers(), json={"definition": {"format": "json", "parts": parts}}, timeout=60)
    if resp.status_code == 200:
        log_message("GraphModel definition updated (200 OK, synchronous).")
        return
    if resp.status_code == 202:
        op_url = resp.headers["Location"]
        retry_after = int(resp.headers.get("Retry-After", 10))
        log_message(f"GraphModel update accepted (async). Polling {op_url} every {retry_after}s...")
        for _ in range(30):
            time.sleep(retry_after)
            poll = requests.get(op_url, headers=_auth_headers(), timeout=30)
            poll.raise_for_status()
            status = poll.json().get("status", "")
            log_message(f"  operation status: {status}")
            if status.lower() == "succeeded":
                return
            if status.lower() == "failed":
                raise RuntimeError(f"GraphModel updateDefinition failed: {poll.json()}")
        raise TimeoutError("Timed out waiting for GraphModel updateDefinition to complete.")
    resp.raise_for_status()


def verify_definition(workspace_id: str, graph_model_id: str) -> None:
    """Read back graphType.json/graphDefinition.json to confirm they're non-empty."""
    url = f"{FABRIC_API}/workspaces/{workspace_id}/items/{graph_model_id}/getDefinition"
    resp = requests.post(url, headers=_auth_headers(), timeout=30)
    if resp.status_code == 202:
        op_url = resp.headers["Location"]
        retry_after = int(resp.headers.get("Retry-After", 5))
        for _ in range(20):
            time.sleep(retry_after)
            poll = requests.get(op_url, headers=_auth_headers(), timeout=30)
            poll.raise_for_status()
            if poll.json().get("status", "").lower() == "succeeded":
                resp = requests.get(f"{op_url}/result", headers=_auth_headers(), timeout=30)
                break
        else:
            raise TimeoutError("Timed out waiting for getDefinition to complete.")
    resp.raise_for_status()
    parts = resp.json().get("definition", {}).get("parts", [])
    by_path = {p["path"]: json.loads(base64.b64decode(p["payload"])) for p in parts}
    node_count = len(by_path.get("graphType.json", {}).get("nodeTypes", []))
    edge_count = len(by_path.get("graphType.json", {}).get("edgeTypes", []))
    log_message(f"Verified: graphType.json now has {node_count} node types, {edge_count} edge types.")
    if node_count != len(NODE_SPECS) or edge_count != len(EDGE_SPECS):
        raise RuntimeError(
            f"Expected {len(NODE_SPECS)} node types / {len(EDGE_SPECS)} edge types, "
            f"got {node_count}/{edge_count}. Definition may not have saved correctly."
        )


def main():
    log_message("=" * 60)
    log_message("Fabric GraphModel node/edge automation (replaces manual canvas step)")
    log_message("=" * 60)

    if not WORKSPACE_ID:
        log_message("ERROR: FABRIC_WORKSPACE_ID not set. Run create_fabric_ontology.py first.")
        sys.exit(1)

    lakehouse = get_existing_lakehouse(WORKSPACE_ID, LAKEHOUSE_NAME)
    lakehouse_id = lakehouse["id"]

    graph_item = find_graph_model_item(WORKSPACE_ID, FABRIC_ONTOLOGY_NAME)
    graph_model_id = graph_item["id"]
    log_message(f"Found GraphModel item: {graph_item['displayName']} ({graph_model_id})")

    parts = build_definition_parts(WORKSPACE_ID, lakehouse_id)
    log_message(f"Built {len(NODE_SPECS)} node types, {len(EDGE_SPECS)} edge types.")

    update_graph_model_definition(WORKSPACE_ID, graph_model_id, parts)
    verify_definition(WORKSPACE_ID, graph_model_id)

    log_message(
        "GraphModel definition populated programmatically. Still required, manually, "
        "once: click Refresh on the graph in the Fabric portal (Job Scheduler REST API "
        "rejects ad-hoc Refresh triggers for this item type -- see docs/DEPLOYMENT.md §4a)."
    )
    log_message("Done.")


if __name__ == "__main__":
    main()
