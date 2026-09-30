"""Show, open or close public network access on the private Foundry account.

Ported from pranabpaul-tech/pdm-fabric-foundry (scripts/foundry_access.py).
With enablePrivateNetworking=true the account and Search are private, so
laptop-side setup (create_foundry_agents.py, create_foundry_iq_kb.py, tests)
gets 403. Either run from the jumpbox, or open briefly and close again:

    python scripts/foundry_access.py status
    python scripts/foundry_access.py open     # allow public access (takes a few minutes)
    python scripts/foundry_access.py close    # back to private

Teams is unaffected by close: Bot Service -> App Service (VNet-integrated) -> private endpoint.
Uses AZURE_AI_ACCOUNT_ID and AZURE_AI_SEARCH_SERVICE_NAME/AZURE_RESOURCE_GROUP from azd env / .env.
Note: tenant governance policies may force publicNetworkAccess back to Disabled.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

AI_API = "2025-06-01"


def _az(*args: str) -> str:
    result = subprocess.run(["az", *args, "-o", "json"], capture_output=True, text=True, shell=os.name == "nt")
    if result.returncode:
        sys.exit(result.stderr.strip())
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["status", "open", "close"])
    parser.add_argument("--account-only", action="store_true", help="leave Azure AI Search as it is")
    args = parser.parse_args()

    account_id = os.getenv("AZURE_AI_ACCOUNT_ID", "").strip()
    search = os.getenv("AZURE_AI_SEARCH_SERVICE_NAME", "").strip()
    group = os.getenv("AZURE_RESOURCE_GROUP", "").strip()
    if not account_id:
        sys.exit("AZURE_AI_ACCOUNT_ID is required (azd env get-values).")

    if args.action != "status":
        state = "Enabled" if args.action == "open" else "Disabled"
        _az("resource", "update", "--ids", account_id, "--api-version", AI_API,
            "--set", f"properties.publicNetworkAccess={state}")
        if search and group and not args.account_only:
            _az("search", "service", "update", "-n", search, "-g", group,
                "--public-network-access", state.lower())

    account = json.loads(_az("resource", "show", "--ids", account_id, "--api-version", AI_API))
    props = account["properties"]
    print(json.dumps({
        "account.publicNetworkAccess": props.get("publicNetworkAccess"),
        "account.networkInjections": [n.get("scenario") for n in props.get("networkInjections") or []],
        "search.publicNetworkAccess": json.loads(_az("search", "service", "show", "-n", search, "-g", group)).get("publicNetworkAccess")
        if search and group else None,
    }, indent=2))
    if args.action != "status":
        print("Give it a few minutes to take effect.")


if __name__ == "__main__":
    main()
