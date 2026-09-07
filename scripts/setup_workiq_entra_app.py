"""Provision the Work IQ OAuth app, Foundry connection, and redirect URI.

This removes the repeatable portal/CLI setup described in docs/DEPLOYMENT.md.
The generated client secret is written only to the gitignored repository .env
and to the encrypted Foundry project connection.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv, set_key

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"
GRAPH = "https://graph.microsoft.com/v1.0"
DISPLAY_NAME = "noc-agent-workiq"
WORKIQ_RESOURCE_APP_ID = "fdcc1f02-fc51-4226-8753-f668596af7f7"
WORKIQ_SCOPE_ID = "0b1715fd-f4bf-4c63-b16d-5be31f9847c2"
WORKIQ_SCOPE_VALUE = "WorkIQAgent.Ask"
AZURE_AI_DEVELOPER_ROLE = "Azure AI Developer"

load_dotenv(ENV_PATH, override=True)

_az_cli = shutil.which("az") or shutil.which("az.cmd") or "az"
_bundled_python = Path(_az_cli).parent.parent / "python.exe"
AZ_CLI_COMMAND = (
    [str(_bundled_python), "-IBm", "azure.cli"]
    if str(_az_cli).lower().endswith(".cmd") and _bundled_python.exists()
    else [_az_cli]
)


def _run_az(arguments: list[str]) -> dict[str, Any] | list[Any] | None:
    command = [*AZ_CLI_COMMAND, *arguments, "--only-show-errors", "--output", "json"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Azure CLI failed: {detail}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def _graph_url(path: str, **query: str) -> str:
    encoded = urllib.parse.urlencode(query, safe="'(),:$")
    return f"{GRAPH}/{path}{'?' + encoded if encoded else ''}"


def _graph(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    arguments = ["rest", "--method", method, "--url", _graph_url(path, **(query or {}))]
    if body is not None:
        arguments.extend(["--headers", "Content-Type=application/json", "--body", json.dumps(body)])
    result = _run_az(arguments)
    return result if isinstance(result, dict) else {}


def _find_application(app_id: str | None) -> dict[str, Any] | None:
    filter_value = f"appId eq '{app_id}'" if app_id else f"displayName eq '{DISPLAY_NAME}'"
    response = _graph(
        "get",
        "applications",
        query={
            "$filter": filter_value,
            "$select": "id,appId,displayName,requiredResourceAccess,web,passwordCredentials",
        },
    )
    values = response.get("value", [])
    if len(values) > 1:
        raise RuntimeError(f"More than one Entra application matched {filter_value}.")
    return values[0] if values else None


def _service_principal(app_id: str) -> dict[str, Any] | None:
    response = _graph(
        "get",
        "servicePrincipals",
        query={"$filter": f"appId eq '{app_id}'", "$select": "id,appId,displayName"},
    )
    values = response.get("value", [])
    if len(values) > 1:
        raise RuntimeError(f"More than one service principal matched appId {app_id}.")
    return values[0] if values else None


def _ensure_application() -> dict[str, Any]:
    existing_id = os.getenv("WORKIQ_ENTRA_APP_ID", "").strip() or None
    application = _find_application(existing_id)
    if not application:
        application = _graph(
            "post",
            "applications",
            body={"displayName": DISPLAY_NAME, "signInAudience": "AzureADMyOrg"},
        )
        print(f"Created Entra app {DISPLAY_NAME} ({application['appId']}).")
    else:
        print(f"Reusing Entra app {application['displayName']} ({application['appId']}).")

    required = list(application.get("requiredResourceAccess") or [])
    entry = next((item for item in required if item.get("resourceAppId") == WORKIQ_RESOURCE_APP_ID), None)
    if entry is None:
        required.append(
            {
                "resourceAppId": WORKIQ_RESOURCE_APP_ID,
                "resourceAccess": [{"id": WORKIQ_SCOPE_ID, "type": "Scope"}],
            }
        )
    elif not any(item.get("id") == WORKIQ_SCOPE_ID for item in entry.get("resourceAccess", [])):
        entry.setdefault("resourceAccess", []).append({"id": WORKIQ_SCOPE_ID, "type": "Scope"})
    _graph("patch", f"applications/{application['id']}", body={"requiredResourceAccess": required})
    return _find_application(application["appId"]) or application


def _ensure_service_principal(app_id: str) -> dict[str, Any]:
    principal = _service_principal(app_id)
    if principal:
        return principal
    _graph("post", "servicePrincipals", body={"appId": app_id})
    principal = _service_principal(app_id)
    if not principal:
        raise RuntimeError(f"Service principal creation did not materialize for {app_id}.")
    return principal


def _ensure_admin_consent(client_sp_id: str) -> None:
    resource_sp = _service_principal(WORKIQ_RESOURCE_APP_ID)
    if not resource_sp:
        resource_sp = _graph("post", "servicePrincipals", body={"appId": WORKIQ_RESOURCE_APP_ID})
    response = _graph(
        "get",
        "oauth2PermissionGrants",
        query={"$filter": f"clientId eq '{client_sp_id}' and resourceId eq '{resource_sp['id']}'"},
    )
    grant = next(
        (item for item in response.get("value", []) if item.get("consentType") == "AllPrincipals"),
        None,
    )
    if grant:
        scopes = set(str(grant.get("scope", "")).split())
        if WORKIQ_SCOPE_VALUE not in scopes:
            scopes.add(WORKIQ_SCOPE_VALUE)
            _graph("patch", f"oauth2PermissionGrants/{grant['id']}", body={"scope": " ".join(sorted(scopes))})
    else:
        _graph(
            "post",
            "oauth2PermissionGrants",
            body={
                "clientId": client_sp_id,
                "consentType": "AllPrincipals",
                "resourceId": resource_sp["id"],
                "scope": WORKIQ_SCOPE_VALUE,
            },
        )
    print(f"Granted tenant-wide delegated scope {WORKIQ_SCOPE_VALUE}.")


def _ensure_secret(application: dict[str, Any]) -> tuple[str, str, str, bool]:
    existing_secret = os.getenv("WORKIQ_ENTRA_APP_SECRET", "").strip()
    existing_app_id = os.getenv("WORKIQ_ENTRA_APP_ID", "").strip()
    existing_key_id = os.getenv("WORKIQ_ENTRA_SECRET_KEY_ID", "").strip()
    existing_expiry = os.getenv("WORKIQ_ENTRA_SECRET_EXPIRES_AT", "").strip()
    live_keys = {item.get("keyId") for item in application.get("passwordCredentials") or []}
    try:
        expiry = datetime.fromisoformat(existing_expiry.replace("Z", "+00:00"))
    except ValueError:
        expiry = datetime.min.replace(tzinfo=timezone.utc)
    if (
        existing_secret
        and existing_app_id == application["appId"]
        and existing_key_id in live_keys
        and expiry > datetime.now(timezone.utc) + timedelta(days=30)
    ):
        return existing_secret, existing_key_id, existing_expiry, False

    end = datetime.now(timezone.utc) + timedelta(days=365)
    result = _graph(
        "post",
        f"applications/{application['id']}/addPassword",
        body={
            "passwordCredential": {
                "displayName": "noc-agent-workiq-secret",
                "endDateTime": end.isoformat().replace("+00:00", "Z"),
            }
        },
    )
    secret = result.get("secretText")
    key_id = result.get("keyId")
    expires_at = result.get("endDateTime")
    if not secret or not key_id or not expires_at:
        raise RuntimeError("Microsoft Graph did not return the complete new credential.")
    print("Created a new one-year Work IQ client credential.")
    return secret, key_id, expires_at, True


def _ensure_project_role(principal_id: str, project_id: str) -> None:
    assignments = _run_az(
        [
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            principal_id,
            "--scope",
            project_id,
            "--query",
            f"[?roleDefinitionName=='{AZURE_AI_DEVELOPER_ROLE}']",
        ]
    )
    if isinstance(assignments, list) and assignments:
        return
    _run_az(
        [
            "role",
            "assignment",
            "create",
            "--assignee-object-id",
            principal_id,
            "--assignee-principal-type",
            "ServicePrincipal",
            "--role",
            AZURE_AI_DEVELOPER_ROLE,
            "--scope",
            project_id,
        ]
    )
    print(f"Granted {AZURE_AI_DEVELOPER_ROLE} at Foundry project scope.")


def _ensure_redirect_uri(application: dict[str, Any], redirect_uri: str | None) -> None:
    if not redirect_uri:
        print("WARNING: Foundry did not return an OAuth redirect URI; inspect the WorkIQ connection in the portal.")
        return
    redirect_uris = list((application.get("web") or {}).get("redirectUris") or [])
    if redirect_uri not in redirect_uris:
        redirect_uris.append(redirect_uri)
        _graph("patch", f"applications/{application['id']}", body={"web": {"redirectUris": redirect_uris}})
        print("Registered the Foundry OAuth redirect URI on the Entra app.")


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required.")
    return value


def main() -> None:
    application = _ensure_application()
    principal = _ensure_service_principal(application["appId"])
    _ensure_admin_consent(principal["id"])
    secret, secret_key_id, secret_expires_at, secret_rotated = _ensure_secret(application)

    project_id = require_env("AZURE_AI_PROJECT_ID")
    _ensure_project_role(principal["id"], project_id)

    sys.path.insert(0, str(Path(__file__).parent))
    from create_workiq_toolbox import put_workiq_connection

    redirect_uri = put_workiq_connection(
        subscription_id=require_env("AZURE_SUBSCRIPTION_ID"),
        resource_group=require_env("AZURE_RESOURCE_GROUP"),
        account_name=require_env("AZURE_AI_ACCOUNT_NAME"),
        project_name=require_env("AZURE_AI_PROJECT_NAME"),
        tenant_id=require_env("AZURE_TENANT_ID"),
        client_id=application["appId"],
        client_secret=secret,
        force_update=secret_rotated,
    )

    # Persist rotation state only after Foundry accepted the credential. If the
    # PUT fails, the next run must still force the same pending secret update.
    ENV_PATH.touch()
    set_key(ENV_PATH, "WORKIQ_ENTRA_APP_ID", application["appId"], quote_mode="never")
    set_key(ENV_PATH, "WORKIQ_ENTRA_APP_SECRET", secret, quote_mode="always")
    set_key(ENV_PATH, "WORKIQ_ENTRA_SECRET_KEY_ID", secret_key_id, quote_mode="never")
    set_key(ENV_PATH, "WORKIQ_ENTRA_SECRET_EXPIRES_AT", secret_expires_at, quote_mode="always")

    _ensure_redirect_uri(_find_application(application["appId"]) or application, redirect_uri)
    print("[OK] Work IQ Entra app and Foundry OAuth2 connection are ready.")


if __name__ == "__main__":
    main()
