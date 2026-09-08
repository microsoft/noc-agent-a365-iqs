targetScope = 'resourceGroup'

type principalRef = {
  name: string
  principalId: string
}

@description('Azure region for the Cosmos DB account.')
param location string = resourceGroup().location

@description('Tags applied to all taggable resources in this module.')
param tags object = {}

@description('Deterministic name suffix shared across resources in this stack.')
param nameSuffix string

@description('Unique token used where Azure requires globally-unique names.')
param resourceToken string

@description('Subnet used for the Cosmos DB private endpoint.')
param privateEndpointSubnetId string

@description('Private DNS zone for privatelink.documents.azure.com.')
param privateDnsZoneId string

@description('Managed identities granted Cosmos DB Built-in Data Reader on the gateway account.')
param readerPrincipals principalRef[] = []

@description('Managed identities granted Cosmos DB Built-in Data Contributor on the config and map containers.')
param writerPrincipals principalRef[] = []

@description('Managed identities granted Cosmos DB Built-in Data Contributor on the config container only.')
param configWriterPrincipals principalRef[] = []

var accountName = toLower(take('cos${replace(nameSuffix, '-', '')}${resourceToken}', 44))
var databaseName = 'gateway'
var configContainerName = 'config'
var mapContainerName = 'team_subscription_map'
var dataReaderRoleDefinitionId = '${account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000001'
var dataContributorRoleDefinitionId = '${account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: accountName
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    publicNetworkAccess: 'Disabled'
    disableLocalAuth: true
    enableAutomaticFailover: false
    enableFreeTier: false
    networkAclBypass: 'AzureServices'
    isVirtualNetworkFilterEnabled: true
    minimalTlsVersion: 'Tls12'
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    capabilities: []
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
  }
}

resource sqlDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: account
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
  }
}

resource configContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: sqlDatabase
  name: configContainerName
  properties: {
    resource: {
      id: configContainerName
      partitionKey: {
        paths: [
          '/id'
        ]
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          {
            path: '/*'
          }
        ]
        excludedPaths: [
          {
            path: '/"_etag"/?'
          }
        ]
      }
    }
  }
}

resource mapContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: sqlDatabase
  name: mapContainerName
  properties: {
    resource: {
      id: mapContainerName
      partitionKey: {
        paths: [
          '/teamId'
        ]
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          {
            path: '/*'
          }
        ]
        excludedPaths: [
          {
            path: '/"_etag"/?'
          }
        ]
      }
      uniqueKeyPolicy: {
        uniqueKeys: [
          {
            paths: [
              '/teamId'
              '/subscriptionId'
            ]
          }
        ]
      }
    }
  }
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-cosmos-${nameSuffix}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'psc-cosmos'
        properties: {
          privateLinkServiceId: account.id
          groupIds: [
            'Sql'
          ]
        }
      }
    ]
  }
}

resource privateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'documents'
        properties: {
          privateDnsZoneId: privateDnsZoneId
        }
      }
    ]
  }
}

resource readerRoleAssignments 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = [for principal in readerPrincipals: {
  parent: account
  name: guid(account.id, 'reader', principal.principalId)
  properties: {
    principalId: principal.principalId
    roleDefinitionId: dataReaderRoleDefinitionId
    scope: account.id
  }
}]

resource writerRoleAssignments 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = [for principal in writerPrincipals: {
  parent: account
  name: guid(account.id, 'writer', principal.principalId)
  properties: {
    principalId: principal.principalId
    roleDefinitionId: dataContributorRoleDefinitionId
    // Cosmos RBAC scope is the account ARM ID plus the native data-plane path.
    scope: '${account.id}/dbs/${databaseName}'
  }
}]

resource configWriterRoleAssignments 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = [for principal in configWriterPrincipals: {
  parent: account
  name: guid(account.id, 'config-writer', principal.principalId)
  properties: {
    principalId: principal.principalId
    roleDefinitionId: dataContributorRoleDefinitionId
    scope: '${account.id}/dbs/${databaseName}/colls/${configContainerName}'
  }
}]

output accountId string = account.id
output accountName string = account.name
output endpoint string = account.properties.documentEndpoint
output databaseName string = databaseName
output configContainerName string = configContainerName
output mapContainerName string = mapContainerName
