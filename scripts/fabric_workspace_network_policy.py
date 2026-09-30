"""Show, deny or allow public access to the NOC Fabric workspace.

Ported from pranabpaul-tech/pdm-fabric-foundry (setup/05_network_policy.py).
Run ONLY after infra/fabric-privatelink.bicep is deployed AND private DNS for
the workspace resolves to a private IP from inside the VNet (jumpbox):

    nslookup <workspaceId-without-dashes>.z<first-2-chars>.w.api.fabric.microsoft.com

    python scripts/fabric_workspace_network_policy.py --status
    python scripts/fabric_workspace_network_policy.py --deny     # public access off
    python scripts/fabric_workspace_network_policy.py --allow    # undo

Takes up to 30 minutes to apply. If tenant-level public access is also
restricted, --allow cannot be run from a workspace-private-link-only client:
keep a tenant-level private link or public client available to undo.
After --deny, the App Service must call the workspace-specific FQDN
(set FABRIC_API_BASE_URL, see docs/PRIVATE_NETWORKING.md).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR.parent / ".env", override=True)
sys.path.insert(0, str(SCRIPT_DIR))
from create_fabric_ontology import get_credential  # noqa: E402

FABRIC_API = os.getenv("FABRIC_API_BASE_URL", "https://api.fabric.microsoft.com").rstrip("/") + "/v1"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true")
    action.add_argument("--deny", action="store_true")
    action.add_argument("--allow", action="store_true")
    args = parser.parse_args()

    workspace_id = os.getenv("FABRIC_WORKSPACE_ID", "").strip()
    if not workspace_id:
        sys.exit("FABRIC_WORKSPACE_ID is required.")
    url = f"{FABRIC_API}/workspaces/{workspace_id}/networking/communicationPolicy"
    headers = {"Authorization": f"Bearer {get_credential().get_token('https://api.fabric.microsoft.com/.default').token}"}

    if args.status:
        response = requests.get(url, headers=headers, timeout=60)
    else:
        rule = "Deny" if args.deny else "Allow"
        response = requests.put(url, headers=headers, timeout=60,
                                json={"inbound": {"publicAccessRules": {"defaultAction": rule}}})
    response.raise_for_status()
    print(response.text or f"Public access set to {'Deny' if args.deny else 'Allow'} (applies within ~30 min).")


if __name__ == "__main__":
    main()
