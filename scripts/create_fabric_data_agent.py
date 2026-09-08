"""Create and publish a Fabric data agent backed by the NOC GraphModel.

Adapted from microsoft/iqdeepdive's infra/create-fabric-data-agent.py. Deliberately
uses the raw Fabric REST API rather than fabric-data-agent-sdk, which pins
conflicting azure-identity/httpx versions and fails outside a Fabric notebook
(see iqdeepdive/AGENTS.md). The GraphModel generated for NOCNetworkOntology is
used directly because it supports NL2GQL instructions and executes against the
same graph that is independently verifiable through Fabric's GQL Query API.
"""

import os
import time
from pathlib import Path

import httpx
from azure.identity import AzureDeveloperCliCredential
from dotenv import load_dotenv, set_key

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"
FABRIC_API_URL = "https://api.fabric.microsoft.com"
FABRIC_SCOPE = f"{FABRIC_API_URL}/.default"
OPERATION_TIMEOUT_SECONDS = 300

AI_INSTRUCTIONS = """Use the NOC topology graph to answer questions about
network topology and blast radius: core routers, transport links, physical
conduits, amplifier sites, services, SLA policies, MPLS paths, and vendor
advisories. Query the graph for every factual answer and never invent entity
identifiers. A TransportLink ORIGINATES_AT and TERMINATES_AT a CoreRouter. A
TransportLink RIDES_ON a PhysicalConduit -- two links can share the same
conduit even if they appear independent, which is the key non-obvious
blast-radius risk to surface. Always report which services and SLA policies
are exposed when a link or conduit fails, and flag any conduit shared by more
than one link explicitly.
"""

GRAPH_DESCRIPTION = (
    "Queryable NOC topology graph for blast-radius, shared-conduit, dependency, "
    "and SLA-exposure analysis."
)
GRAPH_INSTRUCTIONS = """Query this Fabric GraphModel using GQL. Use exact,
case-sensitive labels, edges, and properties. Nodes:
CoreRouter(RouterId,City,Region,Vendor,Model,FirmwareVersion),
TransportLink(LinkId,LinkType,CapacityGbps,SourceRouterId,TargetRouterId),
PhysicalConduit(ConduitId,RouteDescription,MaterialType,InstalledYear),
AmplifierSite(SiteId,Location), Service(ServiceId,ServiceType,CustomerName,
CustomerCount,ActiveUsers), SLAPolicy(SLAPolicyId,ServiceId,AvailabilityPct,
MaxLatencyMs,PenaltyPerHourUSD,Tier), MPLSPath(PathId,PathType), and
Advisory(AdvisoryId,VendorName,Severity,Title). Edges: ORIGINATES_AT,
TERMINATES_AT, RIDES_ON, AMPLIFIES, COVERS, AFFECTS, TRAVERSES_ROUTER, and
TRAVERSES_LINK. Use FILTER rather than WHERE. Preserve each RETURN alias
exactly in ORDER BY. Never invent identifiers or return example values; answer
only from executed query results."""

load_dotenv(ENV_PATH, override=True)


def require_env(name: str) -> str:
    """Return a required environment variable or fail with a useful message."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required to create the Fabric data agent.")
    return value


def get_fabric_token(tenant_id: str) -> str:
    """Acquire a Fabric API token and close the credential immediately."""
    credential = AzureDeveloperCliCredential(tenant_id=tenant_id)
    try:
        return credential.get_token(FABRIC_SCOPE).token
    finally:
        credential.close()


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    expected_statuses: set[int],
    json: dict | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Send a Fabric request and require one of the expected status codes."""
    response = client.request(method, url, json=json, params=params)
    if response.status_code not in expected_statuses:
        response.raise_for_status()
        raise RuntimeError(
            f"Unexpected Fabric API status {response.status_code} for {method} {url}."
        )
    return response


def wait_for_operation(
    client: httpx.Client,
    response: httpx.Response,
    *,
    ignored_error_codes: set[str] | None = None,
) -> None:
    """Wait for a Fabric long-running operation when the API returns 202."""
    if response.status_code != httpx.codes.ACCEPTED:
        return

    operation_url = response.headers.get("Location")
    if not operation_url:
        raise RuntimeError("Fabric returned 202 without an operation Location header.")

    deadline = time.monotonic() + OPERATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        operation = request(
            client,
            "GET",
            operation_url,
            expected_statuses={httpx.codes.OK},
        )
        payload = operation.json()
        status = payload.get("status")
        if status == "Succeeded":
            return
        if status in {"Failed", "Cancelled"}:
            error_code = payload.get("error", {}).get("errorCode")
            if error_code in (ignored_error_codes or set()):
                return
            raise RuntimeError(f"Fabric operation {status.lower()}: {payload}")

        retry_after = float(operation.headers.get("Retry-After", "2"))
        time.sleep(min(max(retry_after, 1), 10))

    raise TimeoutError("Timed out waiting for the Fabric operation to complete.")


def list_data_agents(client: httpx.Client, workspace_id: str) -> list[dict]:
    """List data agents in the configured workspace.

    # ponytail: the dedicated `/v1/workspaces/{id}/dataAgents` list endpoint
    # started 404ing (Fabric API changed/deprecated it) even though the
    # per-item sub-resource endpoints (staging/*, publish) still work fine.
    # The generic Items API with a type filter is the stable equivalent.
    """
    response = request(
        client,
        "GET",
        f"/v1/workspaces/{workspace_id}/items",
        expected_statuses={httpx.codes.OK},
        params={"type": "DataAgent"},
    )
    return response.json().get("value", [])


def find_graph_model(client: httpx.Client, workspace_id: str, ontology_name: str) -> dict:
    """Find the GraphModel generated for the configured ontology."""
    response = request(
        client,
        "GET",
        f"/v1/workspaces/{workspace_id}/items",
        expected_statuses={httpx.codes.OK},
    )
    prefix = f"{ontology_name}_graph_"
    matches = [
        item
        for item in response.json().get("value", [])
        if item.get("type") == "GraphModel"
        and item.get("displayName", "").startswith(prefix)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one GraphModel named '{prefix}*', found {len(matches)}."
        )
    return matches[0]


def get_or_create_data_agent(
    client: httpx.Client,
    name: str,
    workspace_id: str,
) -> dict:
    """Return an existing data agent by name or create it with the Fabric API."""
    for item in list_data_agents(client, workspace_id):
        if item.get("displayName") == name:
            print(f"Reusing Fabric data agent '{name}'.")
            return item

    response = request(
        client,
        "POST",
        f"/v1/workspaces/{workspace_id}/dataAgents",
        expected_statuses={httpx.codes.OK, httpx.codes.CREATED, httpx.codes.ACCEPTED},
        json={"artifactType": "LLMPlugin", "displayName": name},
    )
    wait_for_operation(client, response)

    for _ in range(30):
        for item in list_data_agents(client, workspace_id):
            if item.get("displayName") == name:
                return item
        time.sleep(2)

    raise RuntimeError(f"Fabric data agent '{name}' was created but could not be found.")


def list_staging_datasources(client: httpx.Client, base_url: str) -> list[dict]:
    """List data sources in the data agent's staging configuration."""
    response = request(
        client,
        "GET",
        f"{base_url}/staging/datasources",
        expected_statuses={httpx.codes.OK},
    )
    return response.json().get("value", [])


def remove_other_datasources(
    client: httpx.Client,
    base_url: str,
    retained_item_id: str,
) -> None:
    """Remove stale sources so the agent cannot route to an unqueryable ontology."""
    for source in list_staging_datasources(client, base_url):
        if source.get("itemReference", {}).get("itemId") == retained_item_id:
            continue
        source_id = source["id"]
        print(f"Removing stale data source {source_id}...")
        response = request(
            client,
            "DELETE",
            f"{base_url}/staging/datasources/{source_id}",
            expected_statuses={httpx.codes.OK, httpx.codes.NO_CONTENT, httpx.codes.ACCEPTED},
        )
        wait_for_operation(client, response)


def add_fabric_item_datasource(
    client: httpx.Client,
    base_url: str,
    workspace_id: str,
    item_id: str,
    display_name: str,
) -> None:
    """Add a Fabric item to staging unless that exact item is already present."""
    existing_item_ids = {
        source.get("itemReference", {}).get("itemId")
        for source in list_staging_datasources(client, base_url)
    }
    if item_id in existing_item_ids:
        print(f"Reusing {display_name} data source {item_id}.")
        return

    print(f"Adding {display_name} {item_id} as a data source...")
    response = request(
        client,
        "POST",
        f"{base_url}/staging/datasources",
        expected_statuses={
            httpx.codes.OK,
            httpx.codes.CREATED,
            httpx.codes.ACCEPTED,
        },
        json={
            "type": "FabricItem",
            "itemReference": {
                "referenceType": "ById",
                "itemId": item_id,
                "workspaceId": workspace_id,
            },
        },
    )
    wait_for_operation(client, response)


def get_staging_datasource(
    client: httpx.Client,
    base_url: str,
    item_id: str,
) -> dict:
    """Return the staging data source associated with a Fabric item."""
    for source in list_staging_datasources(client, base_url):
        if source.get("itemReference", {}).get("itemId") == item_id:
            return source
    raise RuntimeError(f"Fabric item {item_id} is not a staging data source.")


def configure_datasource(
    client: httpx.Client,
    base_url: str,
    item_id: str,
    source_name: str,
    description: str,
    instructions: str,
) -> None:
    """Configure source-specific generation guidance for the data source."""
    datasource = get_staging_datasource(client, base_url, item_id)
    datasource_id = datasource["id"]
    response = request(
        client,
        "PATCH",
        f"{base_url}/staging/datasources/{datasource_id}",
        expected_statuses={httpx.codes.OK},
        json={
            "description": description,
            "instructions": instructions,
        },
    )
    wait_for_operation(client, response)


def main() -> None:
    """Create or update the NOC network ontology data agent and publish it."""
    tenant_id = require_env("FABRIC_TENANT_ID")
    workspace_id = require_env("FABRIC_WORKSPACE_ID")
    ontology_name = os.getenv("FABRIC_ONTOLOGY_NAME", "NOCNetworkOntology")
    data_agent_name = os.getenv("FABRIC_DATA_AGENT_NAME", "NOCNetworkDataAgent")

    token = get_fabric_token(tenant_id)
    with httpx.Client(
        base_url=FABRIC_API_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    ) as client:
        print(f"Creating or reusing Fabric data agent '{data_agent_name}'...")
        data_agent = get_or_create_data_agent(client, data_agent_name, workspace_id)
        data_agent_id = data_agent["id"]
        base_url = f"/v1/workspaces/{workspace_id}/dataAgents/{data_agent_id}"
        settings_response = request(
            client,
            "PATCH",
            f"{base_url}/staging/settings",
            expected_statuses={httpx.codes.OK, httpx.codes.ACCEPTED},
            json={"aiInstructions": AI_INSTRUCTIONS},
        )
        wait_for_operation(client, settings_response)

        graph_model = find_graph_model(client, workspace_id, ontology_name)
        graph_model_id = graph_model["id"]
        remove_other_datasources(client, base_url, graph_model_id)
        add_fabric_item_datasource(
            client,
            base_url,
            workspace_id,
            graph_model_id,
            "NOC topology GraphModel",
        )
        configure_datasource(
            client,
            base_url,
            graph_model_id,
            "graph",
            GRAPH_DESCRIPTION,
            GRAPH_INSTRUCTIONS,
        )

        print("Publishing the Fabric data agent...")
        publish_response = request(
            client,
            "POST",
            f"{base_url}/staging/publish",
            expected_statuses={httpx.codes.OK, httpx.codes.CREATED, httpx.codes.ACCEPTED},
            json={
                "publishedDescription": (
                    "NOC network topology data agent (Fabric IQ) for blast-radius, "
                    "shared-conduit, and SLA-exposure analysis."
                )
            },
        )
        wait_for_operation(client, publish_response)

    mcp_url = (
        f"https://api.fabric.microsoft.com/v1/mcp/workspaces/{workspace_id}"
        f"/dataagents/{data_agent_id}/agent"
    )
    values = {
        "FABRIC_GRAPH_MODEL_ID": graph_model_id,
        "FABRIC_DATA_AGENT_ID": data_agent_id,
        "FABRIC_DATA_AGENT_MCP_URL": mcp_url,
        "FABRIC_DATA_AGENT_NAME": data_agent_name,
    }
    ENV_PATH.touch()
    for key, value in values.items():
        set_key(ENV_PATH, key, value, quote_mode="never")

    print(f"Published Fabric data agent: {data_agent_id}")
    print(f"MCP endpoint: {mcp_url}")


if __name__ == "__main__":
    main()
