targetScope = 'resourceGroup'

@description('Azure region for the managed identities.')
param location string = resourceGroup().location

@description('Tags applied to all identities.')
param tags object = {}

@description('Deterministic name suffix shared across resources in this stack.')
param nameSuffix string

resource workerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-worker-${nameSuffix}'
  location: location
  tags: tags
}

resource adminUiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-adminui-${nameSuffix}'
  location: location
  tags: tags
}

resource runLedgerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-runledger-${nameSuffix}'
  location: location
  tags: tags
}

resource mcpHostIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-mcphost-${nameSuffix}'
  location: location
  tags: tags
}

output workerIdentityId string = workerIdentity.id
output workerPrincipalId string = workerIdentity.properties.principalId
output workerClientId string = workerIdentity.properties.clientId
output adminUiIdentityId string = adminUiIdentity.id
output adminUiPrincipalId string = adminUiIdentity.properties.principalId
output adminUiClientId string = adminUiIdentity.properties.clientId
output runLedgerIdentityId string = runLedgerIdentity.id
output runLedgerPrincipalId string = runLedgerIdentity.properties.principalId
output runLedgerClientId string = runLedgerIdentity.properties.clientId
output mcpHostIdentityId string = mcpHostIdentity.id
output mcpHostPrincipalId string = mcpHostIdentity.properties.principalId
output mcpHostClientId string = mcpHostIdentity.properties.clientId
