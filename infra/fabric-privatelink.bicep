targetScope = 'resourceGroup'

// Workspace-level Fabric private link for the NOC workspace (Lakehouse,
// GraphModel, Eventhouse). Deploy separately AFTER the workspace exists:
//
//   az provider register --namespace Microsoft.Fabric
//   az deployment group create -g <rg> -f infra/fabric-privatelink.bicep \
//     -p workspaceId=<FABRIC_WORKSPACE_ID> location=<region> \
//        vnetId=<vnet id> peSubnetId=<private-endpoints subnet id>
//
// Then verify private DNS from inside the VNet (jumpbox) BEFORE denying public
// access with scripts/fabric_workspace_network_policy.py. A new capacity can
// take up to 24h to appear in the private DNS zone.

@description('Fabric workspace GUID.')
param workspaceId string

@description('Region for the private endpoint NIC; match the VNet.')
param location string

@description('Agent VNet resource ID (output of core/network/vnet.bicep).')
param vnetId string

@description('private-endpoints subnet resource ID.')
param peSubnetId string

@description('Microsoft Entra tenant ID.')
param tenantId string = tenant().tenantId

param tags object = {}

resource workspaceLink 'Microsoft.Fabric/privateLinkServicesForFabric@2024-06-01' = {
  name: 'pl-fabric-${take(workspaceId, 8)}'
  location: 'global'
  properties: {
    tenantId: tenantId
    workspaceId: workspaceId
  }
}

module endpoint 'core/network/private-endpoint.bicep' = {
  name: 'fabric-workspace-pe'
  params: {
    name: 'pe-fabric-${take(workspaceId, 8)}'
    location: location
    tags: tags
    subnetId: peSubnetId
    vnetId: vnetId
    targetResourceId: workspaceLink.id
    groupId: 'workspace'
    dnsZoneNames: [
      'privatelink.fabric.microsoft.com'
    ]
  }
}

output privateLinkServiceId string = workspaceLink.id
output privateEndpointId string = endpoint.outputs.privateEndpointId
