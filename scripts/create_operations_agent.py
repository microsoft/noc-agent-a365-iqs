"""Create, capture or update the NOC Fabric Operations Agent.

Ported from pranabpaul-tech/pdm-fabric-foundry (setup/06_ops_agent.py). The
Operations Agent is Fabric's own rule-based monitor over the Eventhouse; it
complements agent/incident_monitor.py (which triggers the MAF investigation)
by notifying on optical-power, BER, silent-sensor and unacknowledged-P1 rules.

Run as a REAL USER (az login), not a service principal: the Operations Agent
runs as its creator. Not available in East US, sovereign clouds, or CMK
workspaces.

    python scripts/create_operations_agent.py --apply            # create from data/ops-agent/OperationsAgent.json
    python scripts/create_operations_agent.py --capture <id>     # pull a portal-edited definition into the file
    python scripts/create_operations_agent.py --update <id>      # push instructions/dataSources onto an existing agent

After --apply, finish in the portal: Generate Playbook, set the Teams recipient, Start.

Env: FABRIC_WORKSPACE_ID, FABRIC_KQL_DB_ID (both written by create_eventhouse.py / create_fabric_ontology.py),
     OPS_AGENT_NAME (default "NOC Operations Monitor").
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
load_dotenv(REPO_ROOT / ".env", override=True)

sys.path.insert(0, str(SCRIPT_DIR))
from create_fabric_ontology import get_credential, log_message  # noqa: E402

FABRIC_API = "https://api.fabric.microsoft.com/v1"
DEFINITION_PATH = REPO_ROOT / "data" / "ops-agent" / "OperationsAgent.json"
PART = "Configurations.json"  # Fabric's own part name for this item type


def _call(method: str, path: str, body: dict | None = None) -> dict:
    token = get_credential().get_token("https://api.fabric.microsoft.com/.default").token
    response = requests.request(method, f"{FABRIC_API}{path}", json=body, timeout=120,
                                headers={"Authorization": f"Bearer {token}"})
    response.raise_for_status()
    return response.json() if response.content else {}


def _encode(payload: dict) -> dict:
    return {"path": PART, "payload": base64.b64encode(json.dumps(payload).encode()).decode(), "payloadType": "InlineBase64"}


def _decode(part: dict) -> dict:
    return json.loads(base64.b64decode(part["payload"]))


def _retarget(config: dict, workspace_id: str, kql_db_id: str) -> None:
    """Point every KustoDatabase data source at this deployment's Eventhouse."""
    sources = {}
    for source in config.get("dataSources", {}).values():
        if source.get("type") == "KustoDatabase":
            sources[kql_db_id] = {"id": kql_db_id, "type": "KustoDatabase", "workspaceId": workspace_id}
        else:
            sources[source["id"]] = source
    config["dataSources"] = sources


def _get_definition(workspace_id: str, agent_id: str) -> list[dict]:
    result = _call("POST", f"/workspaces/{workspace_id}/operationsAgents/{agent_id}/getDefinition")
    return result.get("definition", {}).get("parts", [])


def apply(workspace_id: str, kql_db_id: str) -> None:
    definition = json.loads(DEFINITION_PATH.read_text(encoding="utf-8"))
    _retarget(definition["configuration"], workspace_id, kql_db_id)
    name = os.getenv("OPS_AGENT_NAME", "NOC Operations Monitor")
    result = _call("POST", f"/workspaces/{workspace_id}/operationsAgents",
                   {"displayName": name, "definition": {"parts": [_encode(definition)]}})
    log_message(f"Created Operations Agent '{name}' id={result.get('id')}. "
                "Next: Generate Playbook, set the Teams recipient and Start it in the portal.")


def capture(workspace_id: str, agent_id: str) -> None:
    part = next(p for p in _get_definition(workspace_id, agent_id) if p["path"] == PART)
    DEFINITION_PATH.write_text(json.dumps(_decode(part), indent=2), encoding="utf-8")
    log_message(f"Captured definition into {DEFINITION_PATH}")


def update(workspace_id: str, agent_id: str, kql_db_id: str) -> None:
    """Merge authored config into the live definition, keeping the portal's playbook/shouldRun."""
    local = json.loads(DEFINITION_PATH.read_text(encoding="utf-8"))["configuration"]
    _retarget(local, workspace_id, kql_db_id)
    parts = _get_definition(workspace_id, agent_id)
    live = _decode(next(p for p in parts if p["path"] == PART))
    for key in ("instructions", "dataSources", "actions", "messageDestination"):
        if key in local:
            live["configuration"][key] = local[key]
    platform = [p for p in parts if p["path"] == ".platform"]
    _call("POST", f"/workspaces/{workspace_id}/operationsAgents/{agent_id}/updateDefinition",
          {"definition": {"parts": [_encode(live), *platform]}})
    log_message("Updated. Re-run Generate Playbook in the portal if the rules changed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--capture", metavar="OPS_AGENT_ID")
    mode.add_argument("--update", metavar="OPS_AGENT_ID")
    args = parser.parse_args()

    workspace_id = os.getenv("FABRIC_WORKSPACE_ID", "").strip()
    kql_db_id = os.getenv("FABRIC_KQL_DB_ID", "").strip().strip("'\"")
    if not workspace_id or (not args.capture and not kql_db_id):
        sys.exit("FABRIC_WORKSPACE_ID and FABRIC_KQL_DB_ID are required (run create_eventhouse.py first).")

    if args.capture:
        capture(workspace_id, args.capture)
    elif args.update:
        update(workspace_id, args.update, kql_db_id)
    else:
        apply(workspace_id, kql_db_id)


if __name__ == "__main__":
    main()
