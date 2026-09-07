targetScope = 'subscription'

type principalRef = {
  name: string
  principalId: string
}

type openAiDeployment = {
  name: string
  modelName: string
  modelVersion: string
  skuName: string
  capacity: int
}

type foundryDeployment = {
  name: string
  modelName: string
  modelFormat: string
  modelVersion: string
  skuName: string
  capacity: int
}

@minLength(1)
@maxLength(64)
@description('Name of the azd environment that owns this gateway stack.')
param environmentName string

@minLength(1)
@maxLength(90)
@description('Name of the resource group to create for this independent gateway stack.')
param resourceGroupName string = 'rg-${environmentName}'

@description('Short workload prefix used in resource names.')
param prefix string = 'aigw'

@description('Environment short name used in deterministic resource names and tags.')
param env string = 'dev'

@description('Primary Azure region for all resources in this stack.')
param location string

@description('Owner tag value.')
param owner string

@description('Cost center tag value.')
param costCenter string

@description('APIM publisher display name.')
param apimPublisherName string

@description('APIM publisher contact email.')
param apimPublisherEmail string

@description('APIM SKU name. StandardV2 is the default for public Cowork ingress with VNet backend integration; PremiumV2 remains available for private-only gateways.')
param apimSkuName string = 'StandardV2'

@description('Expose the admin UI Container App externally. Default false keeps the Container Apps environment internal-only.')
param adminUiPublic bool = false

@description('Monthly Cost Management budget for this resource group.')
param monthlyBudgetAmount int = 200

@description('Email address notified by the Cost Management budget thresholds.')
param budgetAlertEmail string

@description('First-of-month UTC start date for the budget. Override when deploying in a later month.')
param budgetStartDate string = '2026-08-01T00:00:00Z'

@description('Azure OpenAI deployments. Names are the client-facing deployment aliases.')
param openaiDeployments openAiDeployment[] = [
  {
    name: 'gpt-5.4'
    modelName: 'gpt-5.4'
    modelVersion: '2026-03-05'
    skuName: 'GlobalStandard'
    capacity: 200
  }
  {
    name: 'gpt-5.4-mini'
    modelName: 'gpt-5.4-mini'
    modelVersion: '2026-03-17'
    skuName: 'GlobalStandard'
    capacity: 200
  }
]

@description('Azure AI Services / Foundry deployments. Names are the client-facing deployment aliases.')
param foundryDeployments foundryDeployment[] = [
  {
    name: 'grok-4.3'
    modelName: 'grok-4.3'
    modelFormat: 'xAI'
    modelVersion: '1'
    skuName: 'GlobalStandard'
    capacity: 10
  }
  {
    name: 'DeepSeek-V4-Pro'
    modelName: 'DeepSeek-V4-Pro'
    modelFormat: 'DeepSeek'
    modelVersion: '2026-04-23'
    skuName: 'GlobalStandard'
    capacity: 500
  }
]

@description('Azure OpenAI data-plane api-version used when a downgrade rewrites a request to the classic /openai/deployments route.')
param openaiApiVersion string = '2025-01-01-preview'

@description('Azure OpenAI inference OpenAPI document imported into APIM.')
param openaiOpenapiSpecUrl string = 'https://raw.githubusercontent.com/Azure/azure-rest-api-specs/main/specification/cognitiveservices/data-plane/AzureOpenAI/inference/stable/2024-10-21/inference.json'

@description('Fallback global token-per-minute limit used when a consumer has no named tier.')
param tokensPerMinute int = 1000

@description('Fallback global token quota used when a consumer has no named tier.')
param tokenQuota int = 50000

@allowed([
  'Hourly'
  'Daily'
  'Weekly'
  'Monthly'
  'Yearly'
])
@description('Fallback token quota reset period used when a consumer has no named tier.')
param tokenQuotaPeriod string = 'Daily'

@description('Run-scoped token-per-minute limit enforced before the ledger hop.')
param runTokensPerMinute int = 1000

@description('Run-scoped token quota enforced before the ledger hop.')
param runTokenQuota int = 50000

@allowed([
  'Hourly'
  'Daily'
  'Weekly'
  'Monthly'
  'Yearly'
])
@description('Run-scoped token quota reset period enforced before the ledger hop.')
param runTokenQuotaPeriod string = 'Daily'

@description('Model deployment names callers are allowed to request.')
param allowedModels string[] = [
  'gpt-5.4'
  'gpt-5.4-mini'
  'grok-4.3'
  'DeepSeek-V4-Pro'
]

@description('Per-consumer APIM rate tiers. Keys become APIM named values such as tier-small-tpm.')
param rateTiers object = {
  small: {
    tpm: 500
    quota: 20000
    period: 'Daily'
  }
  medium: {
    tpm: 2000
    quota: 100000
    period: 'Daily'
  }
  large: {
    tpm: 10000
    quota: 500000
    period: 'Daily'
  }
}

@description('EXTRA managed identities (beyond the worker/admin-ui identities below, which are always wired in automatically) granted Cosmos DB Built-in Data Reader on the gateway account.')
param readerPrincipals principalRef[] = []

@description('EXTRA managed identities (beyond config-sync-worker, which is always wired in automatically) granted Cosmos DB Built-in Data Contributor on the team_subscription_map and config containers.')
param writerPrincipals principalRef[] = []

@description('EXTRA managed identities (beyond the admin UI, which is always wired in automatically) granted Cosmos DB Built-in Data Contributor on the config container only.')
param configWriterPrincipals principalRef[] = []

@description('Full image reference for the config-sync worker. Empty skips the Container Apps Job.')
param workerImage string = ''

@description('UTC cron expression for the config-sync worker job.')
param configSyncCron string = '*/5 * * * *'

@description('Full image reference for the admin UI container. Empty skips the Container App.')
param adminUiImage string = ''

@description('Full image reference for the run-ledger container. Empty skips the Container App.')
param runLedgerImage string = ''

@description('Expose the run-ledger Container App externally. Default false keeps it internal-only.')
param runLedgerPublic bool = false

@description('Full image reference for the Cowork MCP host. Empty skips the Container App and APIM APIs.')
param mcpHostImage string = ''

@description('Foundry project endpoint used by the Cowork MCP host.')
param mcpFoundryProjectEndpoint string = ''

@description('Foundry model deployment used by the Cowork MCP host.')
param mcpFoundryModelDeploymentName string = ''

@description('Foundry project ARM resource ID used for MCP host managed-identity RBAC.')
param mcpFoundryProjectResourceId string = ''

@description('Azure AI Search ARM resource ID used for MCP host Foundry IQ RBAC.')
param mcpSearchServiceResourceId string = ''

@description('Object ID of the Entra group or user allowed to invoke the Cowork MCP channel and its user-delegated Foundry specialists.')
param mcpCallerPrincipalId string = ''

@description('Principal type of mcpCallerPrincipalId.')
param mcpCallerPrincipalType string = 'Group'

@description('Client ID of the Entra resource application securing the Cowork MCP host.')
param mcpServerAppClientId string = ''

@description('Identifier URI accepted as an MCP access-token audience.')
param mcpServerAudience string = ''

@description('Delegated scope value exposed by the MCP resource application.')
param mcpRequiredScope string = 'noc.invoke'

@minValue(1)
@maxValue(28)
@description('Cowork MCP tool-call timeout in seconds.')
param mcpToolTimeoutSeconds int = 28

@description('Serialized per-model prices injected into the run-ledger service.')
param modelPricesJson string = '{}'

@description('Key Vault secret name holding the run-token signing key.')
param runTokenSigningSecretName string = 'run-token-signing-key'

@description('Run token signing secret value. Supply at deploy time; do not commit literal values.')
@secure()
param runTokenSigningSecretValue string

@allowed([
  'subscription-key'
  'entra-id'
])
@description('Client authentication mode enforced by APIM.')
param clientAuthMode string = 'subscription-key'

@description('Entra tenant ID used by APIM validate-jwt and the admin UI.')
param entraTenantId string = ''

@description('Expected audience claim for APIM validate-jwt and the admin UI BFF.')
param entraApiAudience string = ''

@description('JWT claim used to derive the consumer/team identifier in APIM entra-id mode.')
param entraTeamClaim string = 'groups'

@description('Expected audience for the admin UI BFF API.')
param bffApiAudience string = ''

@description('SPA client ID surfaced by the admin UI BFF.')
param spaClientId string = ''

@description('Entra ID group object ID whose members can administer the gateway.')
param adminGroupObjectId string = ''

@description('ISO date after which this resource group should be deleted. Empty disables the teardown marker.')
param deleteByDate string = ''

var regionShortMap = {
  koreacentral: 'krc'
  koreasouth: 'krs'
  eastus: 'eus'
  eastus2: 'eus2'
  westeurope: 'weu'
}
var locationShort = regionShortMap[?location] ?? take(replace(location, ' ', ''), 6)
var nameSuffix = '${prefix}-${env}-${locationShort}'
var resourceToken = uniqueString(subscription().id, resourceGroupName, location)
var apimResourceName = toLower(take('apim${replace(nameSuffix, '-', '')}${resourceToken}', 50))
var apimResourceId = resourceId(subscription().subscriptionId, resourceGroupName, 'Microsoft.ApiManagement/service', apimResourceName)
var mcpFoundryProjectParts = split(mcpFoundryProjectResourceId, '/')
var mcpSearchServiceParts = split(mcpSearchServiceResourceId, '/')
var openAiAliasNames = [for deployment in openaiDeployments: deployment.name]
var foundryAliasNames = [for deployment in foundryDeployments: deployment.name]
var aliasModelsJson = string({
  openai: openAiAliasNames
  foundry: foundryAliasNames
})
var tags = union(
  {
    'azd-env-name': environmentName
    purpose: 'noc-iq-gateway'
    env: env
    workload: prefix
    owner: owner
    costCenter: costCenter
  },
  empty(deleteByDate) ? {} : { DeleteBy: deleteByDate }
)

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module network 'core/network/vnet.bicep' = {
  name: 'network'
  scope: rg
  params: {
    location: location
    tags: tags
    nameSuffix: nameSuffix
    apimDelegationService: (split(apimSkuName, '_')[0] == 'StandardV2' || split(apimSkuName, '_')[0] == 'BasicV2') ? 'Microsoft.Web/serverFarms' : 'Microsoft.Web/hostingEnvironments'
  }
}

module identities 'core/identity/identities.bicep' = {
  name: 'identities'
  scope: rg
  params: {
    location: location
    tags: tags
    nameSuffix: nameSuffix
  }
}

module observability 'core/monitor/observability.bicep' = {
  name: 'observability'
  scope: rg
  params: {
    location: location
    tags: tags
    nameSuffix: nameSuffix
    budgetAmount: monthlyBudgetAmount
    budgetAlertEmail: budgetAlertEmail
    budgetStartDate: budgetStartDate
  }
}

module openai 'core/ai/openai.bicep' = {
  name: 'openai'
  scope: rg
  params: {
    location: location
    tags: tags
    nameSuffix: nameSuffix
    resourceToken: resourceToken
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    privateDnsZoneId: network.outputs.openAiPrivateDnsZoneId
    deployments: openaiDeployments
  }
}

module foundry 'core/ai/foundry.bicep' = {
  name: 'foundry'
  scope: rg
  params: {
    location: location
    tags: tags
    nameSuffix: nameSuffix
    resourceToken: resourceToken
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    privateDnsZoneIds: [
      network.outputs.cognitiveServicesPrivateDnsZoneId
      network.outputs.aiServicesPrivateDnsZoneId
      network.outputs.openAiPrivateDnsZoneId
    ]
    deployments: foundryDeployments
  }
}

module cosmos 'core/config/cosmos.bicep' = {
  name: 'cosmos'
  scope: rg
  params: {
    location: location
    tags: tags
    nameSuffix: nameSuffix
    resourceToken: resourceToken
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    privateDnsZoneId: network.outputs.cosmosPrivateDnsZoneId
    readerPrincipals: readerPrincipals
    // config-sync-worker upserts into `config`/`team_subscription_map`; the admin UI writes
    // `config` only -- both identities always exist (identities.bicep is unconditional), so
    // wire them in automatically instead of requiring a manual `az cosmosdb sql role
    // assignment create` after every fresh deploy.
    writerPrincipals: concat(writerPrincipals, [
      { name: 'config-sync-worker', principalId: identities.outputs.workerPrincipalId }
    ])
    configWriterPrincipals: concat(configWriterPrincipals, [
      { name: 'admin-ui', principalId: identities.outputs.adminUiPrincipalId }
    ])
  }
}

module keyVault 'core/security/keyvault.bicep' = {
  name: 'keyvault'
  scope: rg
  params: {
    location: location
    tags: tags
    nameSuffix: nameSuffix
    resourceToken: resourceToken
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    privateDnsZoneId: network.outputs.keyVaultPrivateDnsZoneId
    runTokenSigningSecretName: runTokenSigningSecretName
    runTokenSigningSecretValue: runTokenSigningSecretValue
  }
}

module registry 'core/registry/acr.bicep' = {
  name: 'acr'
  scope: rg
  params: {
    location: location
    tags: tags
    prefix: prefix
    resourceToken: resourceToken
  }
}

module redis 'core/host/redis.bicep' = {
  name: 'redis'
  scope: rg
  params: {
    location: location
    tags: tags
    nameSuffix: nameSuffix
    resourceToken: resourceToken
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    privateDnsZoneId: network.outputs.redisPrivateDnsZoneId
    runLedgerPrincipalId: identities.outputs.runLedgerPrincipalId
  }
}

module runLedger 'core/host/run-ledger.bicep' = {
  name: 'run-ledger'
  scope: rg
  params: {
    location: location
    tags: tags
    nameSuffix: nameSuffix
    managedEnvironmentId: containerApps.outputs.environmentId
    acrId: registry.outputs.registryId
    acrLoginServer: registry.outputs.loginServer
    runLedgerImage: runLedgerImage
    runLedgerIdentityId: identities.outputs.runLedgerIdentityId
    runLedgerPrincipalId: identities.outputs.runLedgerPrincipalId
    runLedgerClientId: identities.outputs.runLedgerClientId
    keyVaultUrl: keyVault.outputs.vaultUri
    keyVaultResourceId: keyVault.outputs.vaultId
    runTokenSigningSecretName: runTokenSigningSecretName
    redisHostName: redis.outputs.hostName
    redisPort: redis.outputs.port
    modelPricesJson: modelPricesJson
    runLedgerPublic: runLedgerPublic
  }
}

module mcpHost 'core/host/mcp-host.bicep' = {
  name: 'mcp-host'
  scope: rg
  params: {
    location: location
    tags: tags
    nameSuffix: nameSuffix
    managedEnvironmentId: containerApps.outputs.environmentId
    acrId: registry.outputs.registryId
    acrLoginServer: registry.outputs.loginServer
    mcpHostImage: mcpHostImage
    mcpHostIdentityId: identities.outputs.mcpHostIdentityId
    mcpHostPrincipalId: identities.outputs.mcpHostPrincipalId
    mcpHostClientId: identities.outputs.mcpHostClientId
    entraTenantId: entraTenantId
    mcpServerAppClientId: mcpServerAppClientId
    mcpServerAudience: mcpServerAudience
    mcpRequiredScope: mcpRequiredScope
    mcpAllowedCallerObjectId: mcpCallerPrincipalId
    mcpAllowedCallerType: mcpCallerPrincipalType
    publicMcpUrl: 'https://${apimResourceName}.azure-api.net/mcp'
    foundryProjectEndpoint: mcpFoundryProjectEndpoint
    foundryModelDeploymentName: mcpFoundryModelDeploymentName
    runLedgerBaseUrl: runLedger.outputs.appFqdn
    runLedgerBudgetMicros: 2000000
    runLedgerPolicySet: 'default'
    mcpToolTimeoutSeconds: mcpToolTimeoutSeconds
    appInsightsConnectionString: observability.outputs.appInsightsConnectionString
  }
  dependsOn: [
    mcpHostRbac
    mcpSearchRbac
  ]
}

module mcpHostRbac 'core/ai/mcp-host-rbac.bicep' = if (!empty(mcpHostImage)) {
  name: 'mcp-host-rbac'
  scope: resourceGroup(mcpFoundryProjectParts[2], mcpFoundryProjectParts[4])
  params: {
    aiServicesAccountName: mcpFoundryProjectParts[8]
    aiProjectName: mcpFoundryProjectParts[10]
    principalId: identities.outputs.mcpHostPrincipalId
    callerPrincipalId: mcpCallerPrincipalId
    callerPrincipalType: mcpCallerPrincipalType
  }
}

module mcpSearchRbac 'core/ai/mcp-search-rbac.bicep' = if (!empty(mcpHostImage)) {
  name: 'mcp-search-rbac'
  scope: resourceGroup(mcpSearchServiceParts[2], mcpSearchServiceParts[4])
  params: {
    searchServiceName: mcpSearchServiceParts[8]
    principalId: identities.outputs.mcpHostPrincipalId
  }
}

module apim 'core/apim/apim.bicep' = {
  name: 'apim'
  scope: rg
  params: {
    location: location
    tags: tags
    nameSuffix: nameSuffix
    resourceToken: resourceToken
    skuName: apimSkuName
    publisherName: apimPublisherName
    publisherEmail: apimPublisherEmail
    apimSubnetId: network.outputs.apimSubnetId
    openaiAccountId: openai.outputs.accountId
    foundryAccountId: foundry.outputs.accountId
    openaiPathBase: '${openai.outputs.endpoint}/openai'
    foundryV1Base: foundry.outputs.endpointOpenAiV1
    openaiApiVersion: openaiApiVersion
    openaiOpenapiSpecUrl: openaiOpenapiSpecUrl
    appInsightsId: observability.outputs.appInsightsId
    appInsightsInstrumentationKey: observability.outputs.appInsightsInstrumentationKey
    tokensPerMinute: tokensPerMinute
    tokenQuota: tokenQuota
    tokenQuotaPeriod: tokenQuotaPeriod
    runLedgerBaseUrl: runLedger.outputs.appFqdn
    adminUiBaseUrl: containerApps.outputs.adminUiFqdn
    mcpHostBaseUrl: mcpHost.outputs.appFqdn
    runTokensPerMinute: runTokensPerMinute
    runTokenQuota: runTokenQuota
    runTokenQuotaPeriod: runTokenQuotaPeriod
    keyVaultResourceId: keyVault.outputs.vaultId
    runTokenSigningSecretIdentifier: keyVault.outputs.runTokenSigningSecretIdentifier
    allowedModels: allowedModels
    rateTiers: rateTiers
    clientAuthMode: clientAuthMode
    entraTenantId: entraTenantId
    entraApiAudience: entraApiAudience
    entraTeamClaim: entraTeamClaim
  }
}

module containerApps 'core/host/container-apps.bicep' = {
  name: 'container-apps'
  scope: rg
  params: {
    location: location
    tags: tags
    nameSuffix: nameSuffix
    infrastructureSubnetId: network.outputs.containerAppsSubnetId
    logAnalyticsWorkspaceId: observability.outputs.logAnalyticsWorkspaceId
    logAnalyticsCustomerId: observability.outputs.logAnalyticsWorkspaceCustomerId
    logAnalyticsSharedKey: listKeys(resourceId(subscription().subscriptionId, resourceGroupName, 'Microsoft.OperationalInsights/workspaces', 'law-${nameSuffix}'), '2023-09-01').primarySharedKey
    acrId: registry.outputs.registryId
    acrLoginServer: registry.outputs.loginServer
    apimId: apimResourceId
    apimName: apimResourceName
    workerIdentityId: identities.outputs.workerIdentityId
    workerPrincipalId: identities.outputs.workerPrincipalId
    workerClientId: identities.outputs.workerClientId
    workerImage: workerImage
    configSyncCron: configSyncCron
    cosmosEndpoint: cosmos.outputs.endpoint
    cosmosDatabaseName: cosmos.outputs.databaseName
    cosmosConfigContainerName: cosmos.outputs.configContainerName
    adminUiImage: adminUiImage
    adminUiPublic: adminUiPublic
    adminUiIdentityId: identities.outputs.adminUiIdentityId
    adminUiPrincipalId: identities.outputs.adminUiPrincipalId
    adminUiClientId: identities.outputs.adminUiClientId
    entraTenantId: entraTenantId
    bffApiAudience: bffApiAudience
    spaClientId: spaClientId
    adminGroupObjectId: adminGroupObjectId
    cosmosMapContainerName: cosmos.outputs.mapContainerName
    rateTiersJson: string(rateTiers)
    aliasModelsJson: aliasModelsJson
  }
}

// ponytail: separate module because managedEnvironment.properties.defaultDomain is
// only known after the environment deploys -- can't be a same-scope resource `name`
// (BCP120). Module-output -> next-module-param is the accepted way to sequence it.
// Without this zone, APIM (same VNet, different subnet) can't resolve internal-ingress
// Container App FQDNs and every backend call fails "No such host is known" -> 500.
module containerAppsDns 'core/network/container-apps-dns.bicep' = {
  name: 'container-apps-dns'
  scope: rg
  params: {
    tags: tags
    vnetId: network.outputs.vnetId
    defaultDomain: containerApps.outputs.environmentDefaultDomain
    staticIp: containerApps.outputs.environmentStaticIp
  }
}

output resourceGroupName string = resourceGroupName
output resourceGroupId string = rg.id
output apimGatewayUrl string = apim.outputs.gatewayUrl
output ledgerGatewayUrl string = apim.outputs.ledgerGatewayUrl
output appServiceSubscriptionKeySecretUri string = apim.outputs.appServiceSubscriptionKeySecretUri
output apimName string = apim.outputs.apimName
output openAiEndpoint string = openai.outputs.endpoint
output foundryEndpoint string = foundry.outputs.endpointOpenAiV1
output cosmosEndpoint string = cosmos.outputs.endpoint
output acrLoginServer string = registry.outputs.loginServer
output keyVaultUri string = keyVault.outputs.vaultUri
output containerAppsEnvironmentId string = containerApps.outputs.environmentId
output configSyncJobName string = containerApps.outputs.jobName
output adminUiFqdn string = containerApps.outputs.adminUiFqdn
output adminUiGatewayUrl string = apim.outputs.adminUiGatewayUrl
output runLedgerFqdn string = runLedger.outputs.appFqdn
output mcpHostFqdn string = mcpHost.outputs.appFqdn
output mcpHostIdentityClientId string = identities.outputs.mcpHostClientId
output mcpHostIdentityPrincipalId string = identities.outputs.mcpHostPrincipalId
output mcpGatewayUrl string = apim.outputs.mcpGatewayUrl
output mcpProtectedResourceMetadataUrl string = apim.outputs.mcpProtectedResourceMetadataUrl
