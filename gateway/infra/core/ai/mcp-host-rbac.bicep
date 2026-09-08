targetScope = 'resourceGroup'

@description('Foundry AI Services account name.')
param aiServicesAccountName string

@description('Foundry project name.')
param aiProjectName string

@description('MCP host managed identity principal ID.')
param principalId string

@description('Object ID of the group or user allowed to call the Cowork MCP channel. Leave empty to skip caller RBAC.')
param callerPrincipalId string = ''

@description('Principal type of callerPrincipalId.')
param callerPrincipalType string = 'Group'

resource aiAccount 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: aiServicesAccountName

  resource project 'projects' existing = {
    name: aiProjectName
  }
}

var callerRoles = [
  {
    name: 'Foundry Agent Consumer'
    id: 'eed3b665-ab3a-47b6-8f48-c9382fb1dad6'
  }
  {
    name: 'Azure AI Developer'
    id: '64702f94-c441-49e6-a78b-ef80e0188fee'
  }
  {
    name: 'Foundry Project Runtime User'
    id: '142bfaed-a13f-4c2d-bed2-6db62c4a1009'
  }
  {
    name: 'Cognitive Services OpenAI User'
    id: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  }
  {
    name: 'Cognitive Services User'
    id: 'a97b65f3-24c7-4388-baec-2e87135dc908'
  }
]

resource aiDeveloperRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aiAccount::project
  name: guid(aiAccount::project.id, principalId, 'mcphost-ai-developer')
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '64702f94-c441-49e6-a78b-ef80e0188fee')
  }
}

resource cognitiveServicesUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aiAccount::project
  name: guid(aiAccount::project.id, principalId, 'mcphost-cognitive-services-user')
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
  }
}

resource callerProjectRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in callerRoles: if (!empty(callerPrincipalId)) {
  scope: aiAccount::project
  name: guid(aiAccount::project.id, callerPrincipalId, role.id)
  properties: {
    principalId: callerPrincipalId
    principalType: callerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role.id)
  }
}]
