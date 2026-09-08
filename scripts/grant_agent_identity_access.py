"""Grant the Agent 365 agent identity everything it needs on Fabric + Microsoft Graph.

This automates docs/DEPLOYMENT.md's manual sections 4b ("Grant the agent identity
access to Fabric"), 6a/6b ("Grant Work IQ's Graph delegated-permission consent"),
and 4d ("Grant Teams users Fabric workspace/Eventhouse read access"). None of these
are ARM/Bicep-manageable -- Entra tenant-wide delegated-permission consent grants and
Fabric workspace role assignments are not ARM resource providers -- but every one of
them was previously a hand-typed `az rest`/curl command copy-pasted from the docs.
This script makes the same calls, idempotently (checks existing grants/assignments
before writing, merges scopes instead of clobbering them), so a fresh environment
only needs to run one command instead of four.

Run this once, after `a365 setup all` (docs/DEPLOYMENT.md step 9) has created the
Agent Identity -- its object id and the auto-provisioned agent-user object id are not
knowable before that point.

Required env (from the repo-root .env):
  FABRIC_TENANT_ID          - Microsoft Entra tenant ID
  FABRIC_WORKSPACE_ID       - Fabric workspace GUID (see docs/DEPLOYMENT.md 4b)
  AGENT_IDENTITY_OBJECT_ID  - The Entra Agent Identity's own object id (oauth2PermissionGrants clientId)
  AGENT_USER_OBJECT_ID      - The auto-provisioned agent-user object id (Fabric workspace role assignment principal)

Optional env:
  TEAMS_USERS_GROUP_ID      - AAD group object id to also grant Fabric workspace Viewer
                              access (docs/DEPLOYMENT.md 4d, required for noc-incident-agent).
                              Skipped if unset.
  GRANT_MAIL_SEND                    - "true" to also request Mail.Send in the Graph consent scope.
                                       Default "false".
  TEAMS_APP_SERVICE_PRINCIPAL_ID     - Teams host managed-identity object ID; grants Contributor
                                       for direct Fabric Graph queries. Skipped if unset.
  MCP_HOST_PRINCIPAL_ID              - Cowork MCP UAMI object ID; grants Contributor for direct
                                       Fabric Graph queries. Skipped if unset.

Usage:
  python grant_agent_identity_access.py           # applies every grant above
"""

import os
import sys
from pathlib import Path

import httpx
from azure.identity import AzureDeveloperCliCredential
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env", override=True)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
FABRIC_APP_ID = "00000009-0000-0000-c000-000000000000"  # api://api.fabric.microsoft.com
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"  # Microsoft Graph

FABRIC_SCOPES = (
    "DataAgent.Read.All DataAgent.Execute.All "
    "GraphInstance.Read.All GraphInstance.Execute.All"
)
WORKIQ_GRAPH_SCOPES = [
    "Sites.Read.All",
    "Mail.Read",
    "People.Read.All",
    "OnlineMeetingTranscript.Read.All",
    "Chat.Read",
    "ChannelMessage.Read.All",
    "ExternalItem.Read.All",
]

FABRIC_TENANT_ID = os.getenv("FABRIC_TENANT_ID", "").strip().strip("'\"")
FABRIC_WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "").strip().strip("'\"")
AGENT_IDENTITY_OBJECT_ID = os.getenv("AGENT_IDENTITY_OBJECT_ID", "").strip().strip("'\"")
AGENT_USER_OBJECT_ID = os.getenv("AGENT_USER_OBJECT_ID", "").strip().strip("'\"")
TEAMS_USERS_GROUP_ID = os.getenv("TEAMS_USERS_GROUP_ID", "").strip().strip("'\"")
GRANT_MAIL_SEND = os.getenv("GRANT_MAIL_SEND", "false").strip().lower() == "true"
TEAMS_APP_SERVICE_PRINCIPAL_ID = os.getenv("TEAMS_APP_SERVICE_PRINCIPAL_ID", "").strip().strip("'\"")
MCP_HOST_PRINCIPAL_ID = os.getenv("MCP_HOST_PRINCIPAL_ID", "").strip().strip("'\"")


def log_message(message: str) -> None:
    print(message, flush=True)


def require_env(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"{name} is required -- set it in .env once `a365 setup all` has run.")
    return value


def graph_token(credential: AzureDeveloperCliCredential) -> str:
    return credential.get_token("https://graph.microsoft.com/.default").token


def fabric_token(credential: AzureDeveloperCliCredential) -> str:
    return credential.get_token("https://api.fabric.microsoft.com/.default").token


def resolve_sp_object_id(client: httpx.Client, app_id: str) -> str:
    """Resolve a well-known first-party app's service principal object id."""
    resp = client.get(
        f"{GRAPH_BASE}/servicePrincipals",
        params={"$filter": f"appId eq '{app_id}'", "$select": "id"},
    )
    resp.raise_for_status()
    values = resp.json().get("value", [])
    if not values:
        raise RuntimeError(f"Could not resolve service principal for appId={app_id}")
    return values[0]["id"]


def upsert_oauth2_permission_grant(
    client: httpx.Client, *, client_id: str, resource_id: str, scopes: list[str]
) -> None:
    """Idempotently ensure `client_id` has an AllPrincipals grant covering `scopes`.

    Merges with any existing grant's scope instead of clobbering it, so re-running
    this (e.g. to add Mail.Send later) never drops previously-granted scopes.
    """
    resp = client.get(
        f"{GRAPH_BASE}/oauth2PermissionGrants",
        params={"$filter": f"clientId eq '{client_id}' and resourceId eq '{resource_id}'"},
    )
    resp.raise_for_status()
    existing = resp.json().get("value", [])

    if not existing:
        body = {
            "clientId": client_id,
            "consentType": "AllPrincipals",
            "resourceId": resource_id,
            "scope": " ".join(scopes),
        }
        resp = client.post(f"{GRAPH_BASE}/oauth2PermissionGrants", json=body)
        resp.raise_for_status()
        log_message(f"  Created oauth2PermissionGrant: scope='{body['scope']}'")
        return

    grant = existing[0]
    current_scopes = set(grant.get("scope", "").split())
    merged_scopes = current_scopes | set(scopes)
    if merged_scopes == current_scopes:
        log_message("  oauth2PermissionGrant already covers all required scopes -- skipping.")
        return

    resp = client.patch(
        f"{GRAPH_BASE}/oauth2PermissionGrants/{grant['id']}",
        json={"scope": " ".join(sorted(merged_scopes))},
    )
    resp.raise_for_status()
    log_message(f"  Updated oauth2PermissionGrant: scope='{' '.join(sorted(merged_scopes))}'")


def ensure_fabric_workspace_role(
    client: httpx.Client, *, workspace_id: str, principal_id: str, principal_type: str, role: str
) -> None:
    """Idempotently ensure `principal_id` has at least `role` on the Fabric workspace."""
    resp = client.get(f"{FABRIC_BASE}/workspaces/{workspace_id}/roleAssignments")
    resp.raise_for_status()
    for assignment in resp.json().get("value", []):
        if assignment.get("principal", {}).get("id") == principal_id:
            log_message(f"  {principal_id} already has role '{assignment.get('role')}' -- skipping.")
            return

    resp = client.post(
        f"{FABRIC_BASE}/workspaces/{workspace_id}/roleAssignments",
        json={"principal": {"id": principal_id, "type": principal_type}, "role": role},
    )
    resp.raise_for_status()
    log_message(f"  Granted '{role}' on workspace {workspace_id} to {principal_id}.")


def main() -> None:
    require_env("FABRIC_TENANT_ID", FABRIC_TENANT_ID)
    require_env("FABRIC_WORKSPACE_ID", FABRIC_WORKSPACE_ID)
    require_env("AGENT_IDENTITY_OBJECT_ID", AGENT_IDENTITY_OBJECT_ID)
    require_env("AGENT_USER_OBJECT_ID", AGENT_USER_OBJECT_ID)

    credential = AzureDeveloperCliCredential(tenant_id=FABRIC_TENANT_ID)
    graph_client = httpx.Client(
        base_url="", headers={"Authorization": f"Bearer {graph_token(credential)}"}, timeout=30
    )
    fabric_client = httpx.Client(
        base_url="", headers={"Authorization": f"Bearer {fabric_token(credential)}"}, timeout=30
    )

    log_message("1/6 Fabric tenant admin-consent grant (DataAgent.Read.All/Execute.All) ...")
    fabric_sp_id = resolve_sp_object_id(graph_client, FABRIC_APP_ID)
    upsert_oauth2_permission_grant(
        graph_client,
        client_id=AGENT_IDENTITY_OBJECT_ID,
        resource_id=fabric_sp_id,
        scopes=FABRIC_SCOPES.split(),
    )

    log_message("2/6 Fabric workspace role assignment for the agent-user identity ...")
    ensure_fabric_workspace_role(
        fabric_client,
        workspace_id=FABRIC_WORKSPACE_ID,
        principal_id=AGENT_USER_OBJECT_ID,
        principal_type="User",
        role="Contributor",
    )

    log_message("3/6 Microsoft Graph tenant admin-consent grant (Work IQ scopes) ...")
    graph_sp_id = resolve_sp_object_id(graph_client, GRAPH_APP_ID)
    graph_scopes = list(WORKIQ_GRAPH_SCOPES)
    if GRANT_MAIL_SEND:
        graph_scopes.append("Mail.Send")
    upsert_oauth2_permission_grant(
        graph_client,
        client_id=AGENT_IDENTITY_OBJECT_ID,
        resource_id=graph_sp_id,
        scopes=graph_scopes,
    )

    log_message("4/6 Fabric workspace Viewer for the Teams users group (noc-incident-agent) ...")
    if TEAMS_USERS_GROUP_ID:
        ensure_fabric_workspace_role(
            fabric_client,
            workspace_id=FABRIC_WORKSPACE_ID,
            principal_id=TEAMS_USERS_GROUP_ID,
            principal_type="Group",
            role="Viewer",
        )
    else:
        log_message("  TEAMS_USERS_GROUP_ID not set -- skipping (set it in .env to automate this).")

    log_message("5/6 Fabric workspace Contributor for the Teams App Service identity ...")
    if TEAMS_APP_SERVICE_PRINCIPAL_ID:
        ensure_fabric_workspace_role(
            fabric_client,
            workspace_id=FABRIC_WORKSPACE_ID,
            principal_id=TEAMS_APP_SERVICE_PRINCIPAL_ID,
            principal_type="ServicePrincipal",
            role="Contributor",
        )
    else:
        log_message("  TEAMS_APP_SERVICE_PRINCIPAL_ID not set -- skipping.")

    log_message("6/6 Fabric workspace Contributor for the Cowork MCP managed identity ...")
    if MCP_HOST_PRINCIPAL_ID:
        ensure_fabric_workspace_role(
            fabric_client,
            workspace_id=FABRIC_WORKSPACE_ID,
            principal_id=MCP_HOST_PRINCIPAL_ID,
            principal_type="ServicePrincipal",
            role="Contributor",
        )
    else:
        log_message("  MCP_HOST_PRINCIPAL_ID not set -- skipping.")

    log_message("Done. RBAC propagation can still take a couple of minutes before the next turn succeeds.")


def self_check() -> None:
    # ponytail: the only pure logic here is scope-set merging -- exercise that without
    # a live Graph/Fabric call.
    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, existing_scope):
            self.existing_scope = existing_scope
            self.patched_scope = None

        def get(self, *_args, **_kwargs):
            return _FakeResponse(
                {"value": [{"id": "grant-1", "scope": self.existing_scope}]} if self.existing_scope else {"value": []}
            )

        def post(self, *_args, **kwargs):
            self.posted_scope = kwargs["json"]["scope"]
            return _FakeResponse({})

        def patch(self, *_args, **kwargs):
            self.patched_scope = kwargs["json"]["scope"]
            return _FakeResponse({})

    fake = _FakeClient(existing_scope="Mail.Read Chat.Read")
    upsert_oauth2_permission_grant(fake, client_id="c", resource_id="r", scopes=["Mail.Read", "Mail.Send"])
    assert fake.patched_scope == "Chat.Read Mail.Read Mail.Send"

    fake_noop = _FakeClient(existing_scope="Mail.Read Mail.Send")
    upsert_oauth2_permission_grant(fake_noop, client_id="c", resource_id="r", scopes=["Mail.Read"])
    assert fake_noop.patched_scope is None  # already covered -- no write issued


if __name__ == "__main__":
    self_check()
    try:
        main()
    except httpx.HTTPStatusError as error:
        log_message(f"ERROR: {error.request.method} {error.request.url} -> {error.response.status_code}: {error.response.text}")
        sys.exit(1)
    except Exception as error:  # noqa: BLE001
        log_message(f"ERROR: {error}")
        sys.exit(1)
