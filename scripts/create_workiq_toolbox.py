"""Create the Work IQ Foundry connection and toolbox (Work IQ).

Work IQ has no dedicated SDK connection class. An earlier version of this
script used `authType: UserEntraToken` (OBO identity passthrough with no
credential of its own), on the theory that Foundry could forward the
caller's own Entra token and mint a Work IQ-scoped token for it directly.
That is WRONG -- confirmed by live testing, which failed on every call with:

    AADSTS500016: Application 'fdcc1f02-fc51-4226-8753-f668596af7f7' is not
    supported as a resource application to execute the flow.

Work IQ's resource app does not support being the target of a bare OBO/
UserEntraToken flow. Per Microsoft's own quickstart
(https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/mcp/quickstart/foundry),
Work IQ connections in Foundry require a dedicated **OAuth2** connection
backed by a real Entra app registration (client ID + secret) that has been
granted the `WorkIQAgent.Ask` delegated permission with tenant admin
consent. This is heavier than the other three IQ connections (matches
iqdeepdive's create-toolbox-workiq.py `RemoteA2A`/OAuth2 pattern, adapted
here to this project's MCP-based `RemoteTool` connection shape).

Steps:
  1. PUT the Foundry project connection "WorkIQ" with authType=OAuth2,
     pointing at the Entra app registration created via
     `scripts/setup_workiq_entra_app.py` (or manually, see
     docs/DEPLOYMENT.md section 6). This is idempotent -- PUT again to
     update if it already exists.
  2. After the first PUT, Foundry returns an OAuth redirect URL
     (`properties.redirectUrl`/`oauthRedirectUrl`) that MUST be added back
     to the Entra app registration's Authentication > Web platform
     redirect URIs before the connection will work end-to-end -- this
     script prints that URL and does NOT add it automatically (adding a
     redirect URI is a one-time step best done deliberately, since it can't
     be un-discovered/idempotently diffed the same safe way as the other
     properties here).

agent.py resolves Work IQ purely by looking up this connection by name
(`WORK_IQ_CONNECTION_NAME`, default `WorkIQ`) and wrapping it into its own
"noc-iq-toolbox" (see agent/agent.py); it does not use or need any separate,
dedicated Work IQ toolbox.

Required env (from `azd env get-values` / .env):
  AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, AZURE_AI_ACCOUNT_NAME,
  AZURE_AI_PROJECT_NAME, AZURE_TENANT_ID, WORKIQ_ENTRA_APP_ID,
  WORKIQ_ENTRA_APP_SECRET
"""

import os
import time
from pathlib import Path

import httpx
from azure.identity import AzureDeveloperCliCredential
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"
ARM_API_VERSION = "2025-06-01"
CONNECTION_NAME = "WorkIQ"
# Unified Work IQ resource appId -- api://workiq.svc.cloud.microsoft, scope
# WorkIQAgent.Ask. The MCP endpoint Foundry actually calls at runtime.
WORKIQ_TARGET = "https://workiq.svc.cloud.microsoft/mcp"
WORKIQ_SCOPE = "api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask"

load_dotenv(ENV_PATH, override=True)


def require_env(name: str) -> str:
    """Return a required environment variable or fail with a useful message."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required to create the Work IQ toolbox.")
    return value


def log_message(message: str) -> None:
    """Print a progress message immediately (unbuffered)."""
    print(message, flush=True)


def put_workiq_connection(
    *,
    subscription_id: str,
    resource_group: str,
    account_name: str,
    project_name: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    force_update: bool = False,
) -> str | None:
    """Create/update the Work IQ project connection with authType=OAuth2.

    Returns the Foundry-generated OAuth redirect URL when available, including
    for an existing matching connection, so callers can idempotently register it
    on the Entra application.
    """
    credential = AzureDeveloperCliCredential(tenant_id=tenant_id)
    try:
        arm_token = credential.get_token("https://management.azure.com/.default").token
    finally:
        credential.close()

    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account_name}"
        f"/projects/{project_name}/connections/{CONNECTION_NAME}"
        f"?api-version={ARM_API_VERSION}"
    )
    headers = {"Authorization": f"Bearer {arm_token}"}
    existing = httpx.get(url, headers=headers, timeout=60)
    if existing.status_code == 200:
        props = existing.json().get("properties", {})
        # The ARM GET response deliberately redacts OAuth credentials, so the
        # caller must force an update when the app or secret changed.
        if (
            not force_update
            and props.get("authType") == "OAuth2"
            and props.get("target") == WORKIQ_TARGET
        ):
            log_message(
                f"[OK] Foundry connection '{CONNECTION_NAME}' already exists with the correct "
                "OAuth2 configuration -- skipping PUT (the account-rp preview connections API "
                "is flaky on repeat PUTs to an existing connection)."
            )
            return props.get("redirectUrl") or props.get("oauthRedirectUrl")

    authorize_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    payload = {
        "properties": {
            "authType": "OAuth2",
            "category": "RemoteTool",
            "target": WORKIQ_TARGET,
            "group": "GenericProtocol",
            # ponytail: this ARM preview API silently ignores isSharedToAll
            # on both create and update (verified live -- always comes back
            # false regardless of what's sent); leaving it True here is a
            # harmless best-effort in case that changes. If per-user sharing
            # ever actually matters, it must be set from the Foundry portal
            # UI instead.
            "isSharedToAll": True,
            "AuthorizationUrl": authorize_url,
            "TokenUrl": token_url,
            "RefreshUrl": token_url,
            "Scopes": [WORKIQ_SCOPE, "offline_access"],
            "Credentials": {
                "ClientId": client_id,
                "ClientSecret": client_secret,
            },
            "metadata": {"type": "custom_MCP"},
        }
    }
    # NB: the account-rp ARM front-end for this preview connections API is
    # flaky and intermittently returns a bare 500 InternalServerError with no
    # retry-after -- a plain retry with a short backoff clears it.
    last_error: Exception | None = None
    response: httpx.Response | None = None
    for attempt in range(5):
        response = httpx.put(url, json=payload, headers=headers, timeout=120)
        if response.status_code < 500:
            break
        last_error = httpx.HTTPStatusError(
            f"{response.status_code} on attempt {attempt + 1}", request=response.request, response=response
        )
        log_message(f"  ...transient {response.status_code}, retrying in 5s ({attempt + 1}/5)")
        time.sleep(5)
    assert response is not None
    if response.status_code >= 500:
        raise last_error or RuntimeError("Work IQ connection PUT failed after retries.")
    response.raise_for_status()
    body = response.json()
    auth_type = body["properties"]["authType"]
    log_message(f"[OK] Foundry connection '{CONNECTION_NAME}' created/updated (authType={auth_type}).")
    return body["properties"].get("redirectUrl") or body["properties"].get("oauthRedirectUrl")


def main() -> None:
    subscription_id = require_env("AZURE_SUBSCRIPTION_ID")
    resource_group = require_env("AZURE_RESOURCE_GROUP")
    account_name = require_env("AZURE_AI_ACCOUNT_NAME")
    project_name = require_env("AZURE_AI_PROJECT_NAME")
    tenant_id = require_env("AZURE_TENANT_ID")
    client_id = require_env("WORKIQ_ENTRA_APP_ID")
    client_secret = require_env("WORKIQ_ENTRA_APP_SECRET")

    log_message("Creating Work IQ Foundry connection (authType=OAuth2)...")
    redirect_uri = put_workiq_connection(
        subscription_id=subscription_id,
        resource_group=resource_group,
        account_name=account_name,
        project_name=project_name,
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        force_update=True,
    )

    if redirect_uri:
        log_message(
            f"\n[ACTION REQUIRED] Add this redirect URI to the Work IQ Entra app "
            f"registration (Authentication > Web platform > Redirect URIs):\n"
            f"  {redirect_uri}\n"
            "Until this is added, the first-time OAuth consent flow will fail."
        )

    log_message(
        "\nDone. agent.py resolves Work IQ by looking up this 'WorkIQ' project "
        "connection directly (WORK_IQ_CONNECTION_NAME env var, default 'WorkIQ') "
        "-- no separate toolbox or app setting is needed for this connection.\n"
        "NOTE: the first call from each signed-in user will still require a "
        "one-time interactive OAuth consent/sign-in prompt -- this cannot be "
        "automated further (see docs/DEPLOYMENT.md section 6)."
    )


if __name__ == "__main__":
    main()
