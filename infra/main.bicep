targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the azd environment.')
param environmentName string

@minLength(1)
@maxLength(90)
@description('Name of the resource group to create.')
param resourceGroupName string = 'rg-${environmentName}'

@minLength(1)
@description('Primary Azure region. Must support Foundry hosted agents and the selected models.')
param location string

@description('Object ID of the user or application running azd.')
param principalId string

@description('Principal type of the identity running azd.')
param principalType string

@description('Object ID of the group/user granting Teams users OAuth identity-passthrough access to noc-topology-agent/noc-comms-agent (Foundry Agent Consumer role at project scope). Leave empty to skip -- see docs/PRIMER_MCP_CANCEL_SCOPE_BUG.md.')
param teamsUsersPrincipalId string = ''

@description('Principal type of teamsUsersPrincipalId.')
param teamsUsersPrincipalType string = 'Group'


@description('Model deployments serialized by the azure.ai.agents azd extension.')
param aiProjectDeploymentsJson string = '[]'

@description('Enable Application Insights and Log Analytics.')
param enableMonitoring bool = true

@description('Azure AI Search SKU.')
@allowed([
  'basic'
  'standard'
  'standard2'
  'standard3'
  'storage_optimized_l1'
  'storage_optimized_l2'
])
param searchServiceSku string = 'standard'

@description('Create an F2 Microsoft Fabric capacity for Fabric IQ (network ontology + Data Agent).')
param enableFabricCapacity bool = true

@description('Region for the Fabric capacity, in case the primary region lacks F-SKU quota.')
param fabricCapacityLocation string = ''

@description('Optional user UPN to add as a Fabric capacity administrator.')
param fabricAdminUpn string = ''

@description('Optional service-principal object ID to add as a Fabric capacity administrator.')
param fabricServicePrincipalId string = ''

@description('Fabric workspace GUID used by the direct Graph topology fallback.')
param fabricWorkspaceId string = ''

@description('Fabric GraphModel GUID used by the direct Graph topology fallback.')
param fabricGraphModelId string = ''

@description('Fabric Eventhouse KQL query service URI. Persisted by scripts/create_eventhouse.py.')
param fabricKqlQueryUri string = ''

@description('Fabric Eventhouse KQL database display name. Persisted by scripts/create_eventhouse.py.')
param fabricKqlDatabaseName string = ''

@description('Enable detected-incident polling. Off until a destination subscribes and delegated consent is confirmed.')
param incidentMonitorEnabled bool = false

@description('Microsoft web MCP endpoint for Web IQ.')
param webIqMcpEndpoint string = 'https://api.microsoft.ai/v3/mcp'

@secure()
@description('Web IQ API key (CustomKeys auth).')
param webIqApiKey string = ''

@description('Region for Azure AI Search, in case the primary region lacks capacity.')
param searchServiceLocation string = ''

@description('Region for the App Service plan/web app, in case the primary region lacks compute quota.')
param agentHostLocation string = ''

@description('Create App Service VNet integration and a Blob private endpoint. Required when governance disables Storage public access.')
param enablePrivateStorageAccess bool = true

@description('Deploy the public PoC APIM passthrough for the versioned Foundry IQ Toolbox.')
param enableFoundryIqGateway bool = false

@description('Exact versioned Foundry IQ Toolbox MCP URL.')
param foundryIqToolboxBackendUrl string = ''

@description('APIM publisher display name.')
param apimPublisherName string = 'NOC IQ Demo'

@description('APIM publisher email.')
param apimPublisherEmail string = 'admin@contoso.com'

@description('ISO date after which this resource group should be deleted (demo teardown marker).')
param deleteByDate string = ''

var configuredDeployments = json(aiProjectDeploymentsJson)
var fallbackDeployments = [
  {
    name: 'gpt-5.4'
    model: {
      format: 'OpenAI'
      name: 'gpt-5.4'
      version: '2026-03-05'
    }
    sku: {
      name: 'GlobalStandard'
      // 150K TPM -- raised from the original 50K default after live testing hit
      // 429 rate_limit_exceeded (a turn using all 4 IQ tools can make up to 5
      // LLM calls). See docs/TROUBLESHOOTING.md "429 rate_limit_exceeded".
      capacity: 150
    }
  }
  {
    name: 'gpt-5.4-mini'
    model: {
      format: 'OpenAI'
      name: 'gpt-5.4-mini'
      version: '2026-03-17'
    }
    sku: {
      name: 'GlobalStandard'
      capacity: 150
    }
  }
  {
    name: 'text-embedding-3-small'
    model: {
      format: 'OpenAI'
      name: 'text-embedding-3-small'
      version: '1'
    }
    sku: {
      name: 'GlobalStandard'
      capacity: 50
    }
  }
]
var deployments = empty(configuredDeployments) ? fallbackDeployments : configuredDeployments
var chatDeployments = filter(deployments, deployment => deployment.model.name != 'text-embedding-3-small')
var embeddingDeployments = filter(deployments, deployment => deployment.model.name == 'text-embedding-3-small')
var orchestratorDeployments = filter(chatDeployments, deployment => deployment.name == 'gpt-5.4')
var specialistDeployments = filter(chatDeployments, deployment => deployment.name == 'gpt-5.4-mini')
var orchestratorModelDeploymentName = !empty(orchestratorDeployments) ? string(orchestratorDeployments[0].name) : string(chatDeployments[0].name)
var specialistModelDeploymentName = !empty(specialistDeployments) ? string(specialistDeployments[0].name) : orchestratorModelDeploymentName
var resourceToken = uniqueString(subscription().id, resourceGroupName, location)
var effectiveAgentHostLocation = empty(agentHostLocation) ? location : agentHostLocation
var tags = union(
  {
    'azd-env-name': environmentName
    purpose: 'noc-iq-demo'
  },
  empty(deleteByDate) ? {} : { DeleteBy: deleteByDate }
)

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module aiProject 'core/ai/ai-project.bicep' = {
  scope: rg
  name: 'ai-project'
  params: {
    tags: tags
    location: location
    aiFoundryProjectName: 'ai-project-${environmentName}'
    principalId: principalId
    principalType: principalType
    deployments: deployments
    enableMonitoring: enableMonitoring
    searchServiceSku: searchServiceSku
    searchServiceLocation: searchServiceLocation
    foundryIqKnowledgeBaseName: 'noc-knowledge-kb'
    webIqMcpEndpoint: webIqMcpEndpoint
    webIqApiKey: webIqApiKey
  }
}

module agentStorageNetwork 'core/network/appservice-storage-private.bicep' = if (enablePrivateStorageAccess) {
  scope: rg
  name: 'agent-storage-network'
  params: {
    location: effectiveAgentHostLocation
    tags: tags
    resourceToken: resourceToken
    storageAccountId: aiProject.outputs.storage.accountId
  }
}

module foundryIqGateway 'core/gateway/foundry-iq-apim.bicep' = if (enableFoundryIqGateway) {
  scope: rg
  name: 'foundry-iq-gateway'
  params: {
    location: effectiveAgentHostLocation
    tags: tags
    resourceToken: resourceToken
    publisherName: apimPublisherName
    publisherEmail: apimPublisherEmail
    toolboxBackendUrl: foundryIqToolboxBackendUrl
    aiAccountName: aiProject.outputs.aiServicesAccountName
    aiProjectName: aiProject.outputs.projectName
  }
}

module fabricCapacity 'core/fabric/fabric-capacity.bicep' = if (enableFabricCapacity) {
  scope: rg
  name: 'fabric-capacity'
  params: {
    name: 'fabric${resourceToken}'
    location: empty(fabricCapacityLocation) ? location : fabricCapacityLocation
    adminMember: empty(fabricAdminUpn) ? principalId : fabricAdminUpn
    servicePrincipalId: fabricServicePrincipalId
    tags: tags
  }
}

module agentHost 'core/host/appservice.bicep' = {
  scope: rg
  name: 'agent-host'
  params: {
    location: effectiveAgentHostLocation
    tags: tags
    resourceToken: resourceToken
    virtualNetworkSubnetId: enablePrivateStorageAccess ? agentStorageNetwork!.outputs.integrationSubnetId : ''
    appSettings: {
      SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'
      WEBSITE_VNET_ROUTE_ALL: enablePrivateStorageAccess ? '1' : '0'
      FOUNDRY_PROJECT_ENDPOINT: aiProject.outputs.AZURE_AI_PROJECT_ENDPOINT
      AZURE_AI_MODEL_DEPLOYMENT_NAME: orchestratorModelDeploymentName
      AZURE_AI_SPECIALIST_MODEL_DEPLOYMENT_NAME: specialistModelDeploymentName
      AZURE_AI_SEARCH_SERVICE_ENDPOINT: aiProject.outputs.search.serviceEndpoint
      FOUNDRY_IQ_KNOWLEDGE_BASE_NAME: 'noc-knowledge-kb'
      APPLICATIONINSIGHTS_CONNECTION_STRING: aiProject.outputs.APPLICATIONINSIGHTS_CONNECTION_STRING
      AZURE_TENANT_ID: tenant().tenantId
      FABRIC_TENANT_ID: tenant().tenantId
      FABRIC_WORKSPACE_ID: fabricWorkspaceId
      FABRIC_GRAPH_MODEL_ID: fabricGraphModelId
      FABRIC_KQL_QUERY_URI: fabricKqlQueryUri
      FABRIC_KQL_DATABASE_NAME: fabricKqlDatabaseName
      AZURE_STORAGE_ACCOUNT_NAME: aiProject.outputs.storage.accountName
      AGENT_STATE_CONTAINER_NAME: aiProject.outputs.storage.agentStateContainerName
      INCIDENT_MONITOR_ENABLED: string(incidentMonitorEnabled)
      INCIDENT_MONITOR_POLL_SECONDS: '30'
      INCIDENT_MONITOR_FIRST_LOOKBACK_MINUTES: '15'
      INCIDENT_MONITOR_MAX_CATCHUP_MINUTES: '120'
      INCIDENT_MONITOR_OVERLAP_SECONDS: '30'
      INCIDENT_MONITOR_BATCH_SIZE: '20'
      INCIDENT_MONITOR_MAX_ATTEMPTS: '3'
      INCIDENT_MONITOR_LEASE_SECONDS: '60'
      INCIDENT_MONITOR_SPECIALIST_CONCURRENCY: '3'
      // Must match the handler name used in the
      // AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__<name>__SETTINGS__*
      // settings written by `a365 setup all` (see docs/DEPLOYMENT.md step 9).
      // Without this, host_agent_server.py never passes an auth_handler_name
      // to the agent, _exchange_user_token() silently returns None, and
      // Fabric IQ / Work IQ are skipped for every turn (Foundry IQ + Web IQ
      // only) -- this exact gap was root-caused and fixed in this repo.
      AUTH_HANDLER_NAME: 'AGENTIC'
      // Cold-start latency for the combined Foundry Toolbox (4 bundled MCP
      // connections: Foundry IQ, Web IQ, Fabric IQ, Work IQ) plus the
      // per-turn MCPStreamableHTTPTool handshake can exceed the previous 90s
      // default on a fresh app start / first turn. Raised to 180s after a
      // live Teams turn hit the 90s watchdog with no underlying exception --
      // see docs/TROUBLESHOOTING.md.
      AGENT_RUN_TIMEOUT_SECONDS: '180'
    }
  }
}

module agentHostStorageRbac 'core/storage/rbac.bicep' = {
  scope: rg
  name: 'agent-host-storage-rbac'
  params: {
    storageAccountName: aiProject.outputs.storage.accountName
    principalId: agentHost.outputs.principalId
  }
}

// The App Service system-assigned identity needs PROJECT-scope RBAC to call the Foundry project
// (Azure AI Developer) and Cognitive Services (User) -- project scope, not just account scope.
module agentHostRbac 'core/ai/rbac.bicep' = {
  scope: rg
  name: 'agent-host-rbac'
  params: {
    aiServicesAccountName: aiProject.outputs.aiServicesAccountName
    aiProjectName: aiProject.outputs.projectName
    principalId: agentHost.outputs.principalId
    searchServiceName: aiProject.outputs.search.serviceName
    teamsUsersPrincipalId: teamsUsersPrincipalId
    teamsUsersPrincipalType: teamsUsersPrincipalType
  }
}

output AZURE_RESOURCE_GROUP string = resourceGroupName
output AZURE_AI_ACCOUNT_ID string = aiProject.outputs.accountId
output AZURE_AI_PROJECT_ID string = aiProject.outputs.projectId
output AZURE_AI_ACCOUNT_NAME string = aiProject.outputs.aiServicesAccountName
output AZURE_AI_PROJECT_NAME string = aiProject.outputs.projectName
output AZURE_AI_PROJECT_ENDPOINT string = aiProject.outputs.AZURE_AI_PROJECT_ENDPOINT
output FOUNDRY_PROJECT_ENDPOINT string = aiProject.outputs.AZURE_AI_PROJECT_ENDPOINT

output AZURE_AI_MODEL_DEPLOYMENT_NAME string = orchestratorModelDeploymentName
output AZURE_AI_SPECIALIST_MODEL_DEPLOYMENT_NAME string = specialistModelDeploymentName
output AZURE_OPENAI_CHATGPT_DEPLOYMENT string = orchestratorModelDeploymentName
output AZURE_OPENAI_EMBEDDING_DEPLOYMENT string = string(embeddingDeployments[0].name)
output AZURE_OPENAI_ENDPOINT string = aiProject.outputs.AZURE_OPENAI_ENDPOINT

output AZURE_AI_SEARCH_SERVICE_NAME string = aiProject.outputs.search.serviceName
output AZURE_AI_SEARCH_SERVICE_ENDPOINT string = aiProject.outputs.search.serviceEndpoint

output AZURE_STORAGE_ACCOUNT_NAME string = aiProject.outputs.storage.accountName
output APPLICATIONINSIGHTS_CONNECTION_STRING string = aiProject.outputs.APPLICATIONINSIGHTS_CONNECTION_STRING

output FABRIC_CAPACITY_NAME string = enableFabricCapacity ? fabricCapacity!.outputs.name : ''
output FABRIC_CAPACITY_ID string = enableFabricCapacity ? fabricCapacity!.outputs.id : ''
output FABRIC_TENANT_ID string = tenant().tenantId
output AZURE_TENANT_ID string = tenant().tenantId

output AGENT_HOST_APP_NAME string = agentHost.outputs.webAppName
output AGENT_HOST_HOSTNAME string = agentHost.outputs.webAppHostName
output AGENT_HOST_PRINCIPAL_ID string = agentHost.outputs.principalId
output FOUNDRY_IQ_GATEWAY_URL string = enableFoundryIqGateway ? foundryIqGateway!.outputs.gatewayUrl : ''
output FOUNDRY_IQ_GATEWAY_NAME string = enableFoundryIqGateway ? foundryIqGateway!.outputs.name : ''
