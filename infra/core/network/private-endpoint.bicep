targetScope = 'resourceGroup'

// One private endpoint plus its private DNS zone(s), each linked to the VNet.

@description('Private endpoint name.')
param name string

@description('Region for the private endpoint NIC.')
param location string

@description('Tags applied to the endpoint and DNS resources.')
param tags object = {}

@description('Subnet hosting the private endpoint.')
param subnetId string

@description('VNet the private DNS zones are linked to.')
param vnetId string

@description('ARM resource ID of the Private Link target.')
param targetResourceId string

@description('Private Link group ID, e.g. blob, account, searchService, workspace.')
param groupId string

@description('Private Link connection name.')
param connectionName string = groupId

@description('Private DNS zones to create, link and register the endpoint in.')
param dnsZoneNames array

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    subnet: {
      id: subnetId
    }
    privateLinkServiceConnections: [
      {
        name: connectionName
        properties: {
          privateLinkServiceId: targetResourceId
          groupIds: [
            groupId
          ]
        }
      }
    ]
  }
}

resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for zone in dnsZoneNames: {
  name: zone
  location: 'global'
  tags: tags
}]

resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (zone, i) in dnsZoneNames: {
  parent: zones[i]
  name: 'link-${last(split(vnetId, '/'))}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}]

resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [for (zone, i) in dnsZoneNames: {
      name: replace(zone, '.', '-')
      properties: {
        privateDnsZoneId: zones[i].id
      }
    }]
  }
}

output privateEndpointId string = privateEndpoint.id
