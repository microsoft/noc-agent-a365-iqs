"""
Create a Fabric Lakehouse + Fabric IQ ontology over the NOC network topology tables.

Adapted from microsoft/iqdeepdive's infra/create-lakehouse.py: reuses the same
Fabric API mechanics (workspace/lakehouse creation, OneLake upload, Delta table
load, ontology entity/relationship definition parts) but points them at our own
NOC CSVs under data/ontology_entities/ instead of generating Contoso DIY data.

This script:
1. Creates (or reuses) a Fabric workspace on the provisioned F2 capacity.
2. Creates a lakehouse and loads every data/ontology_entities/*.csv as a Delta table.
3. Creates or updates a Fabric IQ ontology (EntityTypes: CoreRouter, TransportLink,
   PhysicalConduit, AmplifierSite, Service, SLAPolicy, MPLSPath, Advisory;
   Relationships: ORIGINATES_AT, TERMINATES_AT, RIDES_ON, AMPLIFIES, DEPENDS_ON,
   COVERS, AFFECTS) modelled on microsoft-iq-solution-accelerator's
   RetailSupplyChainOntologyModel.Ontology shape.

Environment variables (from the repo-root .env -- see docs/DEPLOYMENT.md §2b
for how to generate it via `azd env get-values`; there is no azd
postprovision hook in this repo):
  FABRIC_WORKSPACE_ID    - Existing Fabric workspace GUID (optional; created if absent)
  FABRIC_CAPACITY_ID     - Fabric capacity GUID or ARM resource ID (for workspace creation)
  FABRIC_TENANT_ID       - Required Microsoft Entra tenant ID for Fabric auth
  FABRIC_PORTAL_BASE_URL - Fabric UI host (default: https://msit.powerbi.com)
  LAKEHOUSE_NAME         - default: NOCTopologyLakehouse
  FABRIC_ONTOLOGY_ID     - Existing ontology GUID to update, if known
  FABRIC_ONTOLOGY_NAME   - default: NOCNetworkOntology
  DATA_DIR               - default: <repo>/data/ontology_entities
"""

import base64
import csv
import io
import json
import os
import sys
import uuid
import warnings
from datetime import datetime
from pathlib import Path

from azure.core.exceptions import HttpResponseError
from azure.identity import AzureDeveloperCliCredential
from azure.storage.filedatalake import DataLakeServiceClient
from dotenv import load_dotenv, set_key

warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"microsoft_fabric_api\..*")

from microsoft_fabric_api import FabricClient  # noqa: E402
from microsoft_fabric_api.generated.core.models import (  # noqa: E402
    CreateWorkspaceRequest,
)
from microsoft_fabric_api.generated.lakehouse.models import (  # noqa: E402
    CreateLakehouseRequest,
    Csv,
    LoadTableRequest,
)
from microsoft_fabric_api.generated.ontology.models import (  # noqa: E402
    CreateOntologyRequest,
    OntologyDefinition,
    OntologyDefinitionPart,
    UpdateOntologyDefinitionRequest,
    UpdateOntologyRequest,
)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
load_dotenv(REPO_ROOT / ".env", override=True)

ONELAKE_DFS_URL = "https://onelake.dfs.fabric.microsoft.com"

WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "")
WORKSPACE_NAME = os.getenv("FABRIC_WORKSPACE_NAME", "NOCTopologyWorkspace")
LAKEHOUSE_NAME = os.getenv("LAKEHOUSE_NAME", "NOCTopologyLakehouse")
FABRIC_CAPACITY_ID = os.getenv("FABRIC_CAPACITY_ID", "")
FABRIC_TENANT_ID = os.getenv("FABRIC_TENANT_ID", "").strip()
FABRIC_PORTAL_BASE_URL = os.getenv("FABRIC_PORTAL_BASE_URL", "https://msit.powerbi.com").rstrip("/")
FABRIC_ONTOLOGY_ID = os.getenv("FABRIC_ONTOLOGY_ID", "")
FABRIC_ONTOLOGY_NAME = os.getenv("FABRIC_ONTOLOGY_NAME", "NOCNetworkOntology")
DATA_DIR = Path(os.getenv("DATA_DIR", REPO_ROOT / "data" / "ontology_entities"))

_CREDENTIAL = None
_FABRIC_CLIENT = None


def log_message(message: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")


def _wait_for_extractor_result(extractor, timeout_seconds: int = 180, poll_seconds: float = 2.0):
    """Block until an _LROResultExtractor (this SDK's begin_create_*/
    begin_update_*/begin_load_* return type, across lakehouse/ontology
    operations) has a populated `.result` property.

    Unlike a normal azure-core LROPoller, _LROResultExtractor has no blocking
    `.result()` method -- it is populated asynchronously via
    `poller.add_done_callback(extractor)`, so we must poll the `.result`
    property ourselves until the underlying poller's callback has fired.
    """
    import time

    elapsed = 0.0
    while extractor.result is None and elapsed < timeout_seconds:
        time.sleep(poll_seconds)
        elapsed += poll_seconds
    if extractor.result is None:
        raise TimeoutError(
            f"Timed out after {timeout_seconds}s waiting for ontology long-running operation to complete."
        )
    return extractor.result


def update_root_env(values: dict):
    env_path = REPO_ROOT / ".env"
    for key, val in values.items():
        set_key(str(env_path), key, val)


def get_ontology_ui_url(workspace_id: str, ontology_id: str) -> str:
    return f"{FABRIC_PORTAL_BASE_URL}/groups/{workspace_id}/ontologies/{ontology_id}?experience=fabric-developer"


def get_ontology_mcp_url(workspace_id: str, ontology_id: str) -> str:
    return (
        "https://api.fabric.microsoft.com/v1/mcp/dataPlane/"
        f"workspaces/{workspace_id}/items/{ontology_id}/ontologyEndpoint"
    )


def get_credential():
    global _CREDENTIAL
    if not FABRIC_TENANT_ID:
        raise RuntimeError("FABRIC_TENANT_ID is required for Fabric authentication.")
    if _CREDENTIAL is None:
        _CREDENTIAL = AzureDeveloperCliCredential(tenant_id=FABRIC_TENANT_ID)
    return _CREDENTIAL


def get_fabric_client() -> FabricClient:
    global _FABRIC_CLIENT
    if _FABRIC_CLIENT is None:
        _FABRIC_CLIENT = FabricClient(get_credential())
    return _FABRIC_CLIENT


def is_http_status(error: HttpResponseError, status_code: int) -> bool:
    return error.status_code == status_code


def resolve_capacity_id(capacity_id_or_arm: str) -> str:
    if "/" not in capacity_id_or_arm:
        return capacity_id_or_arm
    log_message("Resolving ARM capacity ID to Fabric GUID...")
    arm_name = capacity_id_or_arm.rstrip("/").split("/")[-1]
    for capacity in get_fabric_client().core.capacities.list_capacities():
        if capacity.display_name == arm_name:
            log_message(f"Resolved capacity: {capacity.id} ({capacity.display_name})")
            return capacity.id
    log_message(f"ERROR: Could not find Fabric capacity matching '{arm_name}'")
    sys.exit(1)


def create_workspace(name: str, capacity_id: str) -> dict:
    log_message(f"Creating workspace '{name}' on capacity {capacity_id[:12]}...")
    try:
        workspace = get_fabric_client().core.workspaces.create_workspace(
            CreateWorkspaceRequest(display_name=name, capacity_id=capacity_id)
        )
        log_message(f"Workspace created: {workspace.id}")
        return {"id": workspace.id, "displayName": workspace.display_name}
    except HttpResponseError as error:
        if is_http_status(error, 409):
            log_message(f"Workspace '{name}' already exists. Fetching existing...")
            return get_existing_workspace(name)
        raise


def get_existing_workspace(name: str) -> dict:
    for workspace in get_fabric_client().core.workspaces.list_workspaces():
        if workspace.display_name == name:
            log_message(f"Found existing workspace: {workspace.id}")
            return {"id": workspace.id, "displayName": workspace.display_name}
    log_message(f"ERROR: Workspace '{name}' not found.")
    sys.exit(1)


def create_lakehouse(workspace_id: str, name: str) -> dict:
    log_message(f"Creating lakehouse '{name}'...")
    try:
        extractor = get_fabric_client().lakehouse.items.begin_create_lakehouse(
            workspace_id, CreateLakehouseRequest(display_name=name)
        )
        lakehouse = _wait_for_extractor_result(extractor)
        log_message(f"Lakehouse created: {lakehouse.id}")
        return {"id": lakehouse.id, "displayName": lakehouse.display_name}
    except HttpResponseError as error:
        if is_http_status(error, 409):
            log_message(f"Lakehouse '{name}' already exists. Fetching existing...")
            return get_existing_lakehouse(workspace_id, name)
        if is_http_status(error, 401) and "UserNotLicensed" in str(error):
            log_message(
                "Your signed-in Microsoft Entra account is not licensed for Fabric. "
                "Assign it a Fabric/Power BI license, then retry."
            )
        raise


def get_existing_lakehouse(workspace_id: str, name: str) -> dict:
    for lakehouse in get_fabric_client().lakehouse.items.list_lakehouses(workspace_id):
        if lakehouse.display_name == name:
            log_message(f"Found existing lakehouse: {lakehouse.id}")
            return {"id": lakehouse.id, "displayName": lakehouse.display_name}
    log_message(f"ERROR: Lakehouse '{name}' not found in workspace.")
    sys.exit(1)


def upload_to_onelake(workspace_id: str, lakehouse_id: str, filename: str, data: bytes):
    service_client = DataLakeServiceClient(account_url=ONELAKE_DFS_URL, credential=get_credential())
    file_system_client = service_client.get_file_system_client(workspace_id)
    directory_client = file_system_client.get_directory_client(f"{lakehouse_id}/Files")
    file_client = directory_client.get_file_client(filename)
    log_message(f"Uploading {filename} ({len(data):,} bytes)...")
    file_client.upload_data(data, overwrite=True)


def load_table(workspace_id: str, lakehouse_id: str, table_name: str, filename: str) -> bool:
    log_message(f"Loading table '{table_name}' from {filename}...")
    try:
        extractor = get_fabric_client().lakehouse.tables.begin_load_table(
            workspace_id,
            lakehouse_id,
            table_name,
            LoadTableRequest(
                relative_path=f"Files/{filename}",
                path_type="File",
                mode="Overwrite",
                format_options=Csv(header=True, delimiter=","),
            ),
        )
        _wait_for_extractor_result(extractor)
        log_message(f"Load completed for '{table_name}'")
        return True
    except HttpResponseError as error:
        log_message(f"ERROR: Load failed for '{table_name}': {error}")
        return False


def create_definition_part(path: str, payload: dict) -> OntologyDefinitionPart:
    encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return OntologyDefinitionPart(path=path, payload=encoded, payload_type="InlineBase64")


def make_entity_parts(entity_id, entity_name, table_name, columns, key_property, display_property, workspace_id, lakehouse_id):
    """Build ontology entity + lakehouse table binding definition parts."""
    properties, property_bindings = [], []
    for offset, column in enumerate(columns, start=1):
        property_id = str((entity_id * 100) + offset)
        properties.append({"id": property_id, "name": column["name"], "valueType": column["type"]})
        property_bindings.append({"sourceColumnName": column["source"], "targetPropertyId": property_id})

    property_ids = {prop["name"]: prop["id"] for prop in properties}
    definition = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/ontology/entityType/1.0.0/schema.json",
        "id": str(entity_id),
        "namespace": "usertypes",
        "baseEntityTypeId": None,
        "name": entity_name,
        "entityIdParts": [property_ids[key_property]],
        "displayNamePropertyId": property_ids[display_property],
        "namespaceType": "Custom",
        "visibility": "Visible",
        "properties": properties,
        "timeseriesProperties": [],
    }
    binding_id = str(uuid.uuid4())
    binding = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/ontology/dataBinding/1.0.0/schema.json",
        "id": binding_id,
        "dataBindingConfiguration": {
            "dataBindingType": "NonTimeSeries",
            "timestampColumnName": None,
            "propertyBindings": property_bindings,
            "sourceTableProperties": {
                "sourceType": "LakehouseTable",
                "workspaceId": workspace_id,
                "itemId": lakehouse_id,
                "sourceTableName": table_name,
            },
        },
    }
    return [
        create_definition_part(f"EntityTypes/{entity_id}/definition.json", definition),
        create_definition_part(f"EntityTypes/{entity_id}/DataBindings/{binding_id}.json", binding),
    ], property_ids


def make_relationship_parts(relationship_id, relationship_name, source_entity_id, source_column, source_property_id,
                             target_entity_id, target_column, target_property_id, table_name, workspace_id, lakehouse_id):
    """Build a relationship type + its lakehouse contextualization."""
    definition = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/ontology/relationshipType/1.0.0/schema.json",
        "namespace": "usertypes",
        "id": str(relationship_id),
        "name": relationship_name,
        "namespaceType": "Custom",
        "source": {"entityTypeId": str(source_entity_id)},
        "target": {"entityTypeId": str(target_entity_id)},
    }
    contextualization_id = str(uuid.uuid4())
    contextualization = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/ontology/contextualization/1.0.0/schema.json",
        "id": contextualization_id,
        "dataBindingTable": {
            "workspaceId": workspace_id,
            "itemId": lakehouse_id,
            "sourceTableName": table_name,
            "sourceType": "LakehouseTable",
        },
        "sourceKeyRefBindings": [{"sourceColumnName": source_column, "targetPropertyId": source_property_id}],
        "targetKeyRefBindings": [{"sourceColumnName": target_column, "targetPropertyId": target_property_id}],
    }
    return [
        create_definition_part(f"RelationshipTypes/{relationship_id}/definition.json", definition),
        create_definition_part(
            f"RelationshipTypes/{relationship_id}/Contextualizations/{contextualization_id}.json", contextualization
        ),
    ]


def build_noc_ontology_definition(workspace_id: str, lakehouse_id: str) -> OntologyDefinition:
    """Build the Fabric IQ ontology over the NOC lakehouse tables.

    Entities mirror microsoft-iq-solution-accelerator's
    RetailSupplyChainOntologyModel.Ontology shape (EntityTypes + RelationshipTypes),
    scoped to what the blast-radius / shared-conduit NOC story needs.
    """
    parts: list[OntologyDefinitionPart] = [
        # Root manifest part required by the Fabric item-definition import API --
        # matches the (empty-object) definition.json seen at the root of
        # microsoft-iq-solution-accelerator's RetailSupplyChainOntologyModel.Ontology.
        # Without this, import fails with "no definition.json entry in the
        # unzipped definition parts."
        create_definition_part("definition.json", {}),
    ]

    router_parts, router_props = make_entity_parts(
        1, "CoreRouter", "DimCoreRouter",
        [
            {"name": "RouterId", "type": "String", "source": "RouterId"},
            {"name": "City", "type": "String", "source": "City"},
            {"name": "Region", "type": "String", "source": "Region"},
            {"name": "Vendor", "type": "String", "source": "Vendor"},
            {"name": "Model", "type": "String", "source": "Model"},
            {"name": "FirmwareVersion", "type": "String", "source": "FirmwareVersion"},
        ],
        "RouterId", "RouterId", workspace_id, lakehouse_id,
    )
    link_parts, link_props = make_entity_parts(
        2, "TransportLink", "DimTransportLink",
        [
            {"name": "LinkId", "type": "String", "source": "LinkId"},
            {"name": "LinkType", "type": "String", "source": "LinkType"},
            {"name": "CapacityGbps", "type": "BigInt", "source": "CapacityGbps"},
            {"name": "SourceRouterId", "type": "String", "source": "SourceRouterId"},
            {"name": "TargetRouterId", "type": "String", "source": "TargetRouterId"},
        ],
        "LinkId", "LinkId", workspace_id, lakehouse_id,
    )
    conduit_parts, conduit_props = make_entity_parts(
        3, "PhysicalConduit", "DimPhysicalConduit",
        [
            {"name": "ConduitId", "type": "String", "source": "ConduitId"},
            {"name": "RouteDescription", "type": "String", "source": "RouteDescription"},
            {"name": "MaterialType", "type": "String", "source": "MaterialType"},
            {"name": "InstalledYear", "type": "BigInt", "source": "InstalledYear"},
        ],
        "ConduitId", "ConduitId", workspace_id, lakehouse_id,
    )
    amp_parts, amp_props = make_entity_parts(
        4, "AmplifierSite", "DimAmplifierSite",
        [
            {"name": "SiteId", "type": "String", "source": "SiteId"},
            {"name": "Location", "type": "String", "source": "Location"},
            {"name": "InstalledYear", "type": "BigInt", "source": "InstalledYear"},
            {"name": "LastCalibration", "type": "String", "source": "LastCalibration"},
        ],
        "SiteId", "SiteId", workspace_id, lakehouse_id,
    )
    service_parts, service_props = make_entity_parts(
        5, "Service", "DimService",
        [
            {"name": "ServiceId", "type": "String", "source": "ServiceId"},
            {"name": "ServiceType", "type": "String", "source": "ServiceType"},
            {"name": "CustomerName", "type": "String", "source": "CustomerName"},
            {"name": "CustomerCount", "type": "BigInt", "source": "CustomerCount"},
            {"name": "ActiveUsers", "type": "BigInt", "source": "ActiveUsers"},
        ],
        "ServiceId", "ServiceId", workspace_id, lakehouse_id,
    )
    sla_parts, sla_props = make_entity_parts(
        6, "SLAPolicy", "DimSLAPolicy",
        [
            {"name": "SLAPolicyId", "type": "String", "source": "SLAPolicyId"},
            {"name": "ServiceId", "type": "String", "source": "ServiceId"},
            {"name": "AvailabilityPct", "type": "Double", "source": "AvailabilityPct"},
            {"name": "MaxLatencyMs", "type": "BigInt", "source": "MaxLatencyMs"},
            {"name": "PenaltyPerHourUSD", "type": "BigInt", "source": "PenaltyPerHourUSD"},
            {"name": "Tier", "type": "String", "source": "Tier"},
        ],
        "SLAPolicyId", "SLAPolicyId", workspace_id, lakehouse_id,
    )
    mpls_parts, mpls_props = make_entity_parts(
        7, "MPLSPath", "DimMPLSPath",
        [
            {"name": "PathId", "type": "String", "source": "PathId"},
            {"name": "PathType", "type": "String", "source": "PathType"},
        ],
        "PathId", "PathId", workspace_id, lakehouse_id,
    )
    advisory_parts, advisory_props = make_entity_parts(
        8, "Advisory", "DimAdvisory",
        [
            {"name": "AdvisoryId", "type": "String", "source": "AdvisoryId"},
            {"name": "VendorName", "type": "String", "source": "VendorName"},
            {"name": "Severity", "type": "String", "source": "Severity"},
            {"name": "Title", "type": "String", "source": "Title"},
        ],
        "AdvisoryId", "AdvisoryId", workspace_id, lakehouse_id,
    )

    for entity_parts in (router_parts, link_parts, conduit_parts, amp_parts, service_parts, sla_parts, mpls_parts, advisory_parts):
        parts.extend(entity_parts)

    relationships = [
        # TransportLink originates/terminates at CoreRouter (self-contained on DimTransportLink)
        (101, "ORIGINATES_AT", 2, "SourceRouterId", link_props["LinkId"], 1, "RouterId", router_props["RouterId"], "DimTransportLink"),
        (102, "TERMINATES_AT", 2, "TargetRouterId", link_props["LinkId"], 1, "RouterId", router_props["RouterId"], "DimTransportLink"),
        # Link rides on a physical conduit -- the shared-conduit blast-radius finding
        (103, "RIDES_ON", 2, "LinkId", link_props["LinkId"], 3, "ConduitId", conduit_props["ConduitId"], "FactConduitMapping"),
        # Amplifier site amplifies a link
        (104, "AMPLIFIES", 4, "LinkId", amp_props["SiteId"], 2, "LinkId", link_props["LinkId"], "FactAmplifierMapping"),
        # SLA policy covers a service
        (105, "COVERS", 6, "ServiceId", sla_props["SLAPolicyId"], 5, "ServiceId", service_props["ServiceId"], "DimSLAPolicy"),
        # Advisory affects a router
        (106, "AFFECTS", 8, "RouterId", advisory_props["AdvisoryId"], 1, "RouterId", router_props["RouterId"], "FactAdvisoryMapping"),
        # Enterprise services depend on MPLS paths; non-MPLS rows do not match a PathId.
        (107, "DEPENDS_ON", 5, "ServiceId", service_props["ServiceId"], 7, "DependsOnId", mpls_props["PathId"], "FactServiceDependency"),
    ]
    for rel_id, rel_name, src_id, src_col, src_prop, tgt_id, tgt_col, tgt_prop, table in relationships:
        parts.extend(
            make_relationship_parts(rel_id, rel_name, src_id, src_col, src_prop, tgt_id, tgt_col, tgt_prop, table, workspace_id, lakehouse_id)
        )

    return OntologyDefinition(parts=parts)


def get_existing_ontology(workspace_id: str, name: str) -> dict | None:
    for ontology in get_fabric_client().ontology.items.list_ontologies(workspace_id):
        if ontology.display_name == name:
            return {"id": ontology.id, "displayName": ontology.display_name}
    return None


def create_or_get_ontology(workspace_id: str, name: str) -> dict:
    if FABRIC_ONTOLOGY_ID:
        ontology = get_fabric_client().ontology.items.get_ontology(workspace_id, FABRIC_ONTOLOGY_ID)
        if ontology.display_name != name:
            ontology = get_fabric_client().ontology.items.update_ontology(
                workspace_id, FABRIC_ONTOLOGY_ID, UpdateOntologyRequest(display_name=name)
            )
        return {"id": ontology.id, "displayName": ontology.display_name}

    existing = get_existing_ontology(workspace_id, name)
    if existing:
        log_message(f"Found existing ontology: {existing['id']}")
        return existing

    log_message(f"Creating ontology '{name}'...")
    extractor = get_fabric_client().ontology.items.begin_create_ontology(
        workspace_id,
        CreateOntologyRequest(display_name=name, description="Fabric IQ ontology for the NOC network topology."),
    )
    ontology = _wait_for_extractor_result(extractor)
    log_message(f"Ontology created: {ontology.id}")
    return {"id": ontology.id, "displayName": ontology.display_name}


def update_ontology_definition(workspace_id: str, ontology_id: str, lakehouse_id: str) -> bool:
    log_message("Updating ontology definition with NOC entity bindings...")
    try:
        extractor = get_fabric_client().ontology.items.begin_update_ontology_definition(
            workspace_id,
            ontology_id,
            UpdateOntologyDefinitionRequest(definition=build_noc_ontology_definition(workspace_id, lakehouse_id)),
            update_metadata=False,
        )
        _wait_for_extractor_result(extractor)
        return True
    except HttpResponseError as error:
        log_message(f"ERROR: Failed to update ontology definition: {error}")
        return False


def main():
    log_message("=" * 60)
    log_message("Fabric Lakehouse + Ontology Creator - NOC Network Topology")
    log_message("=" * 60)

    workspace_id = WORKSPACE_ID
    if not workspace_id and FABRIC_CAPACITY_ID:
        capacity_guid = resolve_capacity_id(FABRIC_CAPACITY_ID)
        update_root_env({"FABRIC_CAPACITY_ID": capacity_guid})
        workspace_name = f"NOC-Topology-{uuid.uuid4().hex[:8]}"
        ws = create_workspace(workspace_name, capacity_guid)
        workspace_id = ws["id"]
        update_root_env({"FABRIC_WORKSPACE_ID": workspace_id})
    elif not workspace_id:
        workspace_id = input("Enter your Fabric Workspace ID: ").strip()
        if not workspace_id:
            log_message("ERROR: Workspace ID is required (or set FABRIC_CAPACITY_ID to auto-create).")
            sys.exit(1)
        update_root_env({"FABRIC_WORKSPACE_ID": workspace_id})
    else:
        update_root_env({"FABRIC_WORKSPACE_ID": workspace_id})

    log_message(f"Workspace ID: {workspace_id}")
    log_message(f"Lakehouse Name: {LAKEHOUSE_NAME}")
    log_message(f"Ontology Name: {FABRIC_ONTOLOGY_NAME}")

    lakehouse = create_lakehouse(workspace_id, LAKEHOUSE_NAME)
    lakehouse_id = lakehouse["id"]

    csv_files = sorted(DATA_DIR.glob("*.csv"))
    if not csv_files:
        log_message(f"ERROR: No CSVs found under {DATA_DIR}")
        sys.exit(1)

    log_message(f"Loading {len(csv_files)} tables from {DATA_DIR}...")
    for csv_path in csv_files:
        table_name = csv_path.stem
        data = csv_path.read_bytes()
        upload_to_onelake(workspace_id, lakehouse_id, csv_path.name, data)
        load_table(workspace_id, lakehouse_id, table_name, csv_path.name)

    ontology = create_or_get_ontology(workspace_id, FABRIC_ONTOLOGY_NAME)
    ontology_id = ontology["id"]
    if update_ontology_definition(workspace_id, ontology_id, lakehouse_id):
        update_root_env({
            "FABRIC_ONTOLOGY_ID": ontology_id,
            "FABRIC_ONTOLOGY_UI_URL": get_ontology_ui_url(workspace_id, ontology_id),
            "FABRIC_ONTOLOGY_MCP_URL": get_ontology_mcp_url(workspace_id, ontology_id),
        })
        log_message(f"Ontology UI: {get_ontology_ui_url(workspace_id, ontology_id)}")
        log_message(f"Ontology MCP endpoint: {get_ontology_mcp_url(workspace_id, ontology_id)}")
    else:
        sys.exit(1)

    log_message("Done.")


if __name__ == "__main__":
    main()
