"""Provision the minimum Gate A Foundry IQ Toolbox spike.

This creates one native Foundry Toolbox containing only the existing
``kb-mcp-connection``. It reuses the current toolbox version when that exact
single-tool definition already exists, avoiding version churn.

After the Toolbox URL has been placed behind the dedicated APIM route, rerun
with both proxy settings and the ARM metadata below to create/update the
separate RemoteTool connection consumed by ``noc-knowledge-agent``.

Required:
  FOUNDRY_PROJECT_ENDPOINT, AZURE_TENANT_ID

Optional:
  FOUNDRY_IQ_CONNECTION_NAME (default kb-mcp-connection)
  FOUNDRY_IQ_TOOLBOX_NAME (default noc-foundry-iq-gate-a)
  FOUNDRY_IQ_PROXY_CONNECTION_NAME, FOUNDRY_IQ_PROXY_MCP_URL

Required when creating the optional proxy connection:
  AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, AZURE_AI_ACCOUNT_NAME,
  AZURE_AI_PROJECT_NAME, FOUNDRY_IQ_PROXY_APIM_SUBSCRIPTION_KEY
"""

import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPToolboxTool
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureDeveloperCliCredential
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).parents[1]
ARM_API_VERSION = "2025-06-01"
DEFAULT_SOURCE_CONNECTION = "kb-mcp-connection"
DEFAULT_TOOLBOX_NAME = "noc-foundry-iq-gate-a"

load_dotenv(REPO_ROOT / ".env", override=True)
load_dotenv(REPO_ROOT / "agent" / ".env", override=False)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for the Foundry IQ Gate A spike.")
    return value


def log(message: str) -> None:
    print(message, flush=True)


def _as_dict(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "as_dict"):
        return value.as_dict()
    raise TypeError(f"Unsupported SDK model type: {type(value).__name__}")


def _toolbox_definition_matches(
    version: Any, *, connection_id: str, server_url: str
) -> bool:
    tools = _as_dict(version).get("tools") or []
    if len(tools) != 1:
        return False
    tool = _as_dict(tools[0])
    return (
        tool.get("type") == "mcp"
        and tool.get("server_label") == "foundry-iq"
        and tool.get("server_url") == server_url
        and tool.get("project_connection_id") == connection_id
        and tool.get("require_approval") == "never"
    )


def toolbox_mcp_url(endpoint: str, name: str, version: str) -> str:
    return (
        f"{endpoint.rstrip('/')}/toolboxes/{quote(name, safe='')}/versions/"
        f"{quote(str(version), safe='')}/mcp?api-version=v1"
    )


def ensure_toolbox(
    project: AIProjectClient,
    *,
    toolbox_name: str,
    connection_id: str,
    server_url: str,
) -> Any:
    try:
        toolbox = project.toolboxes.get(toolbox_name)
    except ResourceNotFoundError:
        toolbox = None

    if toolbox is not None:
        current = project.toolboxes.get_version(
            toolbox_name, str(toolbox.default_version)
        )
        if _toolbox_definition_matches(
            current, connection_id=connection_id, server_url=server_url
        ):
            log(
                f"[OK] Toolbox '{toolbox_name}' v{current.version} already has "
                "the exact single Foundry IQ tool -- reusing"
            )
            return current

    created = project.toolboxes.create_version(
        toolbox_name,
        tools=[
            MCPToolboxTool(
                server_label="foundry-iq",
                server_url=server_url,
                project_connection_id=connection_id,
                require_approval="never",
            )
        ],
        description="Gate A proof: Foundry IQ only; do not add other specialists",
        metadata={"proof_gate": "gate-a", "iq_surface": "foundry_iq"},
    )
    log(f"[OK] Toolbox '{toolbox_name}' v{created.version} created")
    return created


def _proxy_settings() -> tuple[str, str] | None:
    name = os.getenv("FOUNDRY_IQ_PROXY_CONNECTION_NAME", "").strip()
    url = os.getenv("FOUNDRY_IQ_PROXY_MCP_URL", "").strip()
    if bool(name) != bool(url):
        raise RuntimeError(
            "FOUNDRY_IQ_PROXY_CONNECTION_NAME and FOUNDRY_IQ_PROXY_MCP_URL "
            "must either both be set or both be unset."
        )
    return (name, url) if name else None


def ensure_proxy_connection(
    *,
    credential: AzureDeveloperCliCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
    project_name: str,
    connection_name: str,
    mcp_url: str,
    subscription_key: str,
) -> None:
    token = credential.get_token("https://management.azure.com/.default").token
    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account_name}"
        f"/projects/{project_name}/connections/{quote(connection_name, safe='')}"
        f"?api-version={ARM_API_VERSION}"
    )
    headers = {"Authorization": f"Bearer {token}"}

    existing = httpx.get(url, headers=headers, timeout=60)
    if existing.status_code == 200:
        properties = existing.json().get("properties", {})
        if not (
            properties.get("category") == "RemoteTool"
            and properties.get("authType") == "CustomKeys"
            and properties.get("target") == mcp_url
        ):
            log(f"  Proxy connection '{connection_name}' is stale -- updating")
    elif existing.status_code != 404:
        existing.raise_for_status()

    payload = {
        "properties": {
            "authType": "CustomKeys",
            "category": "RemoteTool",
            "target": mcp_url,
            "group": "GenericProtocol",
            "isSharedToAll": True,
            "credentials": {
                "keys": {"Ocp-Apim-Subscription-Key": subscription_key}
            },
            "metadata": {
                "type": "custom_MCP",
                "proof_gate": "gate-a",
                "backend_auth": "apim-managed-identity",
            },
        }
    }
    response: httpx.Response | None = None
    for attempt in range(5):
        response = httpx.put(url, json=payload, headers=headers, timeout=120)
        if response.status_code < 500:
            break
        log(f"  transient ARM {response.status_code}; retrying ({attempt + 1}/5)")
        time.sleep(5)
    assert response is not None
    response.raise_for_status()
    log(f"[OK] Proxy connection '{connection_name}' created/updated")


def main() -> None:
    endpoint = require_env("FOUNDRY_PROJECT_ENDPOINT")
    tenant_id = require_env("AZURE_TENANT_ID")
    source_name = os.getenv(
        "FOUNDRY_IQ_CONNECTION_NAME", DEFAULT_SOURCE_CONNECTION
    ).strip()
    toolbox_name = os.getenv(
        "FOUNDRY_IQ_TOOLBOX_NAME", DEFAULT_TOOLBOX_NAME
    ).strip()
    proxy = _proxy_settings()

    credential = AzureDeveloperCliCredential(tenant_id=tenant_id, process_timeout=60)
    project = AIProjectClient(
        endpoint=endpoint, credential=credential, allow_preview=True
    )
    try:
        source = project.connections.get(source_name, include_credentials=False)
        toolbox = ensure_toolbox(
            project,
            toolbox_name=toolbox_name,
            connection_id=source.id,
            server_url=source.target,
        )
        consumer_url = toolbox_mcp_url(
            endpoint, toolbox.name, str(toolbox.version)
        )
        log(f"FOUNDRY_IQ_TOOLBOX_MCP_URL={consumer_url}")

        if proxy:
            ensure_proxy_connection(
                credential=credential,
                subscription_id=require_env("AZURE_SUBSCRIPTION_ID"),
                resource_group=require_env("AZURE_RESOURCE_GROUP"),
                account_name=require_env("AZURE_AI_ACCOUNT_NAME"),
                project_name=require_env("AZURE_AI_PROJECT_NAME"),
                connection_name=proxy[0],
                mcp_url=proxy[1],
                subscription_key=require_env(
                    "FOUNDRY_IQ_PROXY_APIM_SUBSCRIPTION_KEY"
                ),
            )
        else:
            log(
                "Proxy connection not requested; existing specialist definitions "
                "remain on their direct connections."
            )
    finally:
        project.close()
        credential.close()


if __name__ == "__main__":
    main()
