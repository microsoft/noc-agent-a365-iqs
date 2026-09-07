"""Idempotently provision the Entra topology for the Cowork MCP channel.

The Microsoft 365 Enterprise token-store auth configuration is not exposed by
Microsoft Graph. This script creates everything Entra can own and emits the
remaining OAuthPluginVault referenceId instruction without creating secrets.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path
import uuid
from typing import Any

GRAPH = "https://graph.microsoft.com/v1.0"
AZURE_AI_RESOURCE = "https://ai.azure.com"
TOKEN_EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"
TEAMS_REDIRECT_URI = "https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect"
TEAMS_CONSENT_REDIRECT_URI = "https://teams.microsoft.com/api/platform/v1.0/oAuthConsentRedirect"
M365_TOKEN_STORE_CLIENT_ID = "ab3be6b7-f5df-413d-ac2d-abf1e3fd9c0b"
FEDERATED_CREDENTIAL_NAME = "mcp-uami-client-assertion"
_az_cli = shutil.which("az") or shutil.which("az.cmd") or "az"
# Windows installs Azure CLI as az.cmd; invoke its bundled Python directly so
# Graph URLs containing '&' are not re-parsed by cmd.exe.
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
        raise RuntimeError(f"{' '.join(command[:4])} failed: {detail}")
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


def _single(values: list[dict[str, Any]], description: str) -> dict[str, Any] | None:
    if len(values) > 1:
        raise RuntimeError(f"More than one {description} matched; pass its application ID explicitly.")
    return values[0] if values else None


def _get_application(app_id: str) -> dict[str, Any] | None:
    response = _graph(
        "get",
        "applications",
        query={
            "$filter": f"appId eq '{app_id}'",
            "$select": (
                "id,appId,displayName,identifierUris,api,web,publicClient,"
                "isFallbackPublicClient,requiredResourceAccess"
            ),
        },
    )
    return _single(response.get("value", []), f"application with appId {app_id}")


def _find_application(display_name: str) -> dict[str, Any] | None:
    escaped = display_name.replace("'", "''")
    response = _graph(
        "get",
        "applications",
        query={
            "$filter": f"displayName eq '{escaped}'",
            "$select": (
                "id,appId,displayName,identifierUris,api,web,publicClient,"
                "isFallbackPublicClient,requiredResourceAccess"
            ),
        },
    )
    return _single(response.get("value", []), f"application named {display_name!r}")


def _ensure_application(display_name: str, app_id: str | None) -> dict[str, Any]:
    application = _get_application(app_id) if app_id else _find_application(display_name)
    if application:
        print(f"Reusing Entra app {application['displayName']} ({application['appId']}).")
        return application
    created = _graph(
        "post",
        "applications",
        body={"displayName": display_name, "signInAudience": "AzureADMyOrg"},
    )
    print(f"Created Entra app {display_name} ({created['appId']}).")
    return _get_application(created["appId"]) or created


def _service_principal_for_app(app_id: str) -> dict[str, Any] | None:
    response = _graph(
        "get",
        "servicePrincipals",
        query={
            "$filter": f"appId eq '{app_id}'",
            "$select": "id,appId,displayName,servicePrincipalNames,oauth2PermissionScopes",
        },
    )
    return _single(response.get("value", []), f"service principal with appId {app_id}")


def _ensure_service_principal(app_id: str) -> dict[str, Any]:
    service_principal = _service_principal_for_app(app_id)
    if service_principal:
        return service_principal
    _graph("post", "servicePrincipals", body={"appId": app_id})
    service_principal = _service_principal_for_app(app_id)
    if not service_principal:
        raise RuntimeError(f"Service principal creation did not materialize for appId {app_id}.")
    return service_principal


def _merge_required_access(
    entries: list[dict[str, Any]],
    resource_app_id: str,
    permission_id: str,
) -> list[dict[str, Any]]:
    merged = [dict(entry) for entry in entries]
    entry = next((item for item in merged if item.get("resourceAppId") == resource_app_id), None)
    if entry is None:
        merged.append(
            {
                "resourceAppId": resource_app_id,
                "resourceAccess": [{"id": permission_id, "type": "Scope"}],
            }
        )
        return merged
    access = list(entry.get("resourceAccess", []))
    if not any(item.get("id") == permission_id and item.get("type") == "Scope" for item in access):
        access.append({"id": permission_id, "type": "Scope"})
    entry["resourceAccess"] = access
    return merged


def _resolve_downstream(
    resource_app_id: str | None,
    preferred_scope: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if resource_app_id:
        service_principal = _service_principal_for_app(resource_app_id)
        if not service_principal:
            raise RuntimeError(f"Azure AI service principal {resource_app_id} was not found in this tenant.")
    else:
        response = _graph(
            "get",
            "servicePrincipals",
            query={
                "$filter": f"servicePrincipalNames/any(name:name eq '{AZURE_AI_RESOURCE}')",
                "$select": "id,appId,displayName,servicePrincipalNames,oauth2PermissionScopes",
            },
        )
        service_principal = _single(response.get("value", []), "Azure AI service principal")
        if not service_principal:
            raise RuntimeError(
                "Could not resolve the https://ai.azure.com service principal. "
                "Pass --downstream-resource-app-id explicitly."
            )

    enabled_scopes = [
        scope
        for scope in service_principal.get("oauth2PermissionScopes", [])
        if scope.get("isEnabled", True)
    ]
    scope = next(
        (
            item
            for item in enabled_scopes
            if preferred_scope and item.get("value") == preferred_scope
        ),
        None,
    )
    scope = scope or next(
        (item for item in enabled_scopes if item.get("value") in {"user_impersonation", "access_as_user"}),
        None,
    )
    scope = scope or (enabled_scopes[0] if len(enabled_scopes) == 1 else None)
    if not scope:
        values = ", ".join(sorted(str(item.get("value")) for item in enabled_scopes))
        raise RuntimeError(
            "Could not choose the Azure AI delegated scope. "
            f"Available values: {values or '<none>'}. Pass --downstream-scope-value."
        )
    return service_principal, scope


def _ensure_permission_grant(
    client_sp_id: str,
    resource_sp_id: str,
    scope_value: str,
) -> None:
    response = _graph(
        "get",
        "oauth2PermissionGrants",
        query={"$filter": f"clientId eq '{client_sp_id}' and resourceId eq '{resource_sp_id}'"},
    )
    grants = response.get("value", [])
    grant = next((item for item in grants if item.get("consentType") == "AllPrincipals"), None)
    if grant is None:
        _graph(
            "post",
            "oauth2PermissionGrants",
            body={
                "clientId": client_sp_id,
                "consentType": "AllPrincipals",
                "resourceId": resource_sp_id,
                "scope": scope_value,
            },
        )
        print(f"Granted tenant-wide delegated scope {scope_value}.")
        return
    scopes = set(str(grant.get("scope", "")).split())
    if scope_value in scopes:
        return
    scopes.add(scope_value)
    _graph(
        "patch",
        f"oauth2PermissionGrants/{grant['id']}",
        body={"scope": " ".join(sorted(scopes))},
    )
    print(f"Extended tenant-wide delegated grant with {scope_value}.")


def _ensure_federated_credential(
    resource_app_object_id: str,
    tenant_id: str,
    uami_principal_id: str,
) -> None:
    path = f"applications/{resource_app_object_id}/federatedIdentityCredentials"
    response = _graph("get", path)
    existing = next(
        (item for item in response.get("value", []) if item.get("name") == FEDERATED_CREDENTIAL_NAME),
        None,
    )
    body = {
        "name": FEDERATED_CREDENTIAL_NAME,
        "issuer": f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        "subject": uami_principal_id,
        "audiences": [TOKEN_EXCHANGE_AUDIENCE],
        "description": "Trust the Cowork MCP user-assigned managed identity for OBO client assertions.",
    }
    if existing:
        if all(existing.get(key) == value for key, value in body.items() if key != "description"):
            return
        _graph("patch", f"{path}/{existing['id']}", body=body)
        print("Updated MCP managed-identity federated credential.")
        return
    _graph("post", path, body=body)
    print("Created MCP managed-identity federated credential.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--mcp-uami-client-id", required=True)
    parser.add_argument("--mcp-uami-principal-id", required=True)
    parser.add_argument("--resource-app-name", default="NOC Cowork MCP Resource")
    parser.add_argument("--cowork-client-app-name", default="NOC Cowork OAuth Client")
    parser.add_argument("--resource-app-id")
    parser.add_argument("--cowork-client-app-id")
    parser.add_argument("--scope-value", default="noc.invoke")
    parser.add_argument("--downstream-resource-app-id")
    parser.add_argument("--downstream-scope-value")
    parser.add_argument(
        "--sso-application-id-uri",
        help="Application ID URI emitted by Teams Developer Portal after creating the SSO auth config.",
    )
    parser.add_argument("--skip-admin-consent", action="store_true")
    args = parser.parse_args()

    if args.sso_application_id_uri and not args.sso_application_id_uri.startswith("api://"):
        raise RuntimeError("--sso-application-id-uri must be the api:// URI emitted by Teams Developer Portal.")

    account = _run_az(["account", "show"])
    if not isinstance(account, dict) or account.get("tenantId") != args.tenant_id:
        raise RuntimeError(f"Azure CLI must be signed into tenant {args.tenant_id}.")

    resource_app = _ensure_application(args.resource_app_name, args.resource_app_id)
    cowork_client = _ensure_application(args.cowork_client_app_name, args.cowork_client_app_id)
    resource_uri = f"api://{resource_app['appId']}"
    scope_id = str(
        next(
            (
                item["id"]
                for item in (resource_app.get("api") or {}).get("oauth2PermissionScopes", [])
                if item.get("value") == args.scope_value
            ),
            uuid.uuid5(uuid.NAMESPACE_URL, f"{resource_uri}/{args.scope_value}"),
        )
    )

    resource_api = dict(resource_app.get("api") or {})
    scopes = list(resource_api.get("oauth2PermissionScopes", []))
    if not any(item.get("value") == args.scope_value for item in scopes):
        scopes.append(
            {
                "id": scope_id,
                "adminConsentDescription": "Allow Cowork to perform read-only NOC investigations.",
                "adminConsentDisplayName": "Investigate NOC incidents",
                "isEnabled": True,
                "type": "Admin",
                "userConsentDescription": "Allow Cowork to perform read-only NOC investigations.",
                "userConsentDisplayName": "Investigate NOC incidents",
                "value": args.scope_value,
            }
        )
    resource_api.update(
        {
            "oauth2PermissionScopes": scopes,
            "requestedAccessTokenVersion": 2,
        }
    )
    identifiers = sorted(
        set(
            [
                *(resource_app.get("identifierUris") or []),
                resource_uri,
                *([args.sso_application_id_uri] if args.sso_application_id_uri else []),
            ]
        )
    )
    resource_web = dict(resource_app.get("web") or {})
    resource_web["redirectUris"] = sorted(
        set([*(resource_web.get("redirectUris") or []), TEAMS_CONSENT_REDIRECT_URI])
    )

    downstream_sp, downstream_scope = _resolve_downstream(
        args.downstream_resource_app_id,
        args.downstream_scope_value,
    )
    resource_access = _merge_required_access(
        resource_app.get("requiredResourceAccess") or [],
        downstream_sp["appId"],
        downstream_scope["id"],
    )
    _graph(
        "patch",
        f"applications/{resource_app['id']}",
        body={
            "identifierUris": identifiers,
            "api": resource_api,
            "web": resource_web,
            "groupMembershipClaims": "SecurityGroup",
            "requiredResourceAccess": resource_access,
        },
    )

    # Graph must persist a new permission before pre-authorization can reference it.
    preauthorized = list(resource_api.get("preAuthorizedApplications", []))
    for client_app_id in (cowork_client["appId"], M365_TOKEN_STORE_CLIENT_ID):
        preauth = next(
            (item for item in preauthorized if item.get("appId") == client_app_id),
            None,
        )
        if preauth is None:
            preauthorized.append({"appId": client_app_id, "delegatedPermissionIds": [scope_id]})
        elif scope_id not in preauth.get("delegatedPermissionIds", []):
            preauth["delegatedPermissionIds"] = [*preauth.get("delegatedPermissionIds", []), scope_id]
    resource_api["preAuthorizedApplications"] = preauthorized
    _graph("patch", f"applications/{resource_app['id']}", body={"api": resource_api})

    client_web = dict(cowork_client.get("web") or {})
    client_web["redirectUris"] = sorted(
        set([*(client_web.get("redirectUris") or []), TEAMS_REDIRECT_URI])
    )
    client_access = _merge_required_access(
        cowork_client.get("requiredResourceAccess") or [],
        resource_app["appId"],
        scope_id,
    )
    _graph(
        "patch",
        f"applications/{cowork_client['id']}",
        body={
            "web": client_web,
            "isFallbackPublicClient": True,
            "requiredResourceAccess": client_access,
        },
    )

    resource_sp = _ensure_service_principal(resource_app["appId"])
    client_sp = _ensure_service_principal(cowork_client["appId"])
    _ensure_federated_credential(resource_app["id"], args.tenant_id, args.mcp_uami_principal_id)
    if not args.skip_admin_consent:
        _ensure_permission_grant(client_sp["id"], resource_sp["id"], args.scope_value)
        _ensure_permission_grant(resource_sp["id"], downstream_sp["id"], downstream_scope["value"])

    scope_uri = f"{resource_uri}/{args.scope_value}"
    output = {
        "tenantId": args.tenant_id,
        "mcpResourceAppClientId": resource_app["appId"],
        "mcpResourceAppObjectId": resource_app["id"],
        "coworkOAuthClientAppId": cowork_client["appId"],
        "coworkOAuthClientAppObjectId": cowork_client["id"],
        "mcpManagedIdentityClientId": args.mcp_uami_client_id,
        "mcpManagedIdentityPrincipalId": args.mcp_uami_principal_id,
        "resourceUri": resource_uri,
        "scope": scope_uri,
        "coworkManifestId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{resource_uri}/cowork-manifest")),
        "microsoftEnterpriseTokenStoreClientId": M365_TOKEN_STORE_CLIENT_ID,
        "ssoApplicationIdUri": args.sso_application_id_uri or "<RETURNED-BY-TEAMS-DEVELOPER-PORTAL>",
        "oauthPluginVaultReferenceId": "<CREATE-IN-TEAMS-DEVELOPER-PORTAL>",
        "oauthPluginVaultInstruction": (
            "Register the MCP URL with the MCP resource app client ID (not coworkOAuthClientAppId) "
            "and the full scope above. Copy the registration ID into COWORK_AUTH_CONFIG_REFERENCE_ID, "
            "then rerun this script with --sso-application-id-uri set to the portal-generated URI and "
            "redeploy the MCP host with mcpServerAudience set to that URI."
        ),
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
