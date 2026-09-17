targetScope = 'resourceGroup'

param location string
param tags object = {}
param resourceToken string
param publisherName string
param publisherEmail string
param toolboxBackendUrl string
param aiAccountName string
param aiProjectName string

var apimName = 'apim-noc-${resourceToken}'
var policyTemplate = loadTextContent('../../../gateway/infra/policies/foundry-iq-mcp-passthrough.xml')
var backendUrlXmlSafe = replace(replace(toolboxBackendUrl, '&', '&amp;'), '"', '&quot;')
var policy = replace(policyTemplate, '{{FOUNDRY_IQ_TOOLBOX_BACKEND_URL}}', backendUrlXmlSafe)

resource aiAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: aiAccountName

  resource aiProject 'projects' existing = {
    name: aiProjectName
  }
}

resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: apimName
  location: location
  tags: tags
  sku: {
    name: 'BasicV2'
    capacity: 1
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherName: publisherName
    publisherEmail: publisherEmail
    publicNetworkAccess: 'Enabled'
  }
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'foundry-iq-mcp'
  properties: {
    path: 'specialists/foundry-iq/mcp'
    displayName: 'Foundry IQ MCP'
    protocols: [
      'https'
    ]
    subscriptionRequired: true
  }
}

var methods = [
  'GET'
  'POST'
  'DELETE'
  'OPTIONS'
]

resource operations 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [for method in methods: {
  parent: api
  name: toLower(method)
  properties: {
    displayName: method
    method: method
    urlTemplate: '/'
  }
}]

resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: {
    format: 'xml'
    value: policy
  }
}

resource subscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apim
  name: 'foundry-iq'
  properties: {
    displayName: 'foundry-iq'
    scope: api.id
    state: 'active'
    allowTracing: false
  }
}

resource apimProjectRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiAccount::aiProject.id, apim.id, 'azure-ai-developer')
  scope: aiAccount::aiProject
  properties: {
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '64702f94-c441-49e6-a78b-ef80e0188fee')
  }
}

resource apimCognitiveServicesUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiAccount::aiProject.id, apim.id, 'cognitive-services-user')
  scope: aiAccount::aiProject
  properties: {
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
  }
}

output name string = apim.name
output principalId string = apim.identity.principalId
output gatewayUrl string = 'https://${apim.name}.azure-api.net/specialists/foundry-iq/mcp'
output subscriptionName string = subscription.name
