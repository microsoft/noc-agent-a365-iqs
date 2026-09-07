targetScope = 'resourceGroup'

@description('Azure region for the API Management service.')
param location string = resourceGroup().location

@description('Tags applied to all taggable resources in this module.')
param tags object = {}

@description('Deterministic name suffix shared across resources in this stack.')
param nameSuffix string

@description('Unique token used where Azure requires globally-unique names.')
param resourceToken string

@description('APIM SKU name. Supports v2 names such as PremiumV2 and legacy classic Name_Capacity values such as Developer_1.')
param skuName string

@description('Publisher display name.')
param publisherName string

@description('Publisher contact email.')
param publisherEmail string

@description('Subnet for APIM PremiumV2 VNet injection.')
param apimSubnetId string

@description('Azure OpenAI account resource ID.')
param openaiAccountId string

@description('Azure AI Services / Foundry account resource ID.')
param foundryAccountId string

@description('OpenAI backend base URL for APIM.')
param openaiPathBase string

@description('Foundry backend base URL for APIM.')
param foundryV1Base string

@description('Azure OpenAI api-version used by downgrade rewrites.')
param openaiApiVersion string

@description('OpenAPI spec URL imported into APIM for Azure OpenAI compatibility.')
param openaiOpenapiSpecUrl string

@description('Application Insights resource ID used for APIM diagnostics.')
param appInsightsId string

@description('Application Insights instrumentation key used by the APIM logger.')
param appInsightsInstrumentationKey string

@description('Fallback token-per-minute limit used when a consumer has no explicit tier.')
param tokensPerMinute int

@description('Fallback token quota used when a consumer has no explicit tier.')
param tokenQuota int

@description('Fallback token quota reset period used when a consumer has no explicit tier.')
param tokenQuotaPeriod string

@description('Client-requestable deployment aliases.')
param allowedModels string[]

@description('Per-consumer rate tiers that become APIM named values.')
param rateTiers object

@allowed([
  'subscription-key'
  'entra-id'
])
@description('Authentication mode enforced by APIM.')
param clientAuthMode string

@description('Entra tenant ID used by validate-jwt when entra-id auth mode is enabled.')
param entraTenantId string

@description('Entra audience claim expected by validate-jwt when entra-id auth mode is enabled.')
param entraApiAudience string

@description('JWT claim used to derive the consumer identifier in entra-id mode.')
param entraTeamClaim string

@description('Base URL of the internal run-ledger service.')
param runLedgerBaseUrl string

@description('Internal-ingress base URL (including https://) of the Admin UI Container App. Empty skips the proxy API.')
param adminUiBaseUrl string = ''

@description('Base URL of the Cowork MCP Container App. Empty skips the MCP APIs.')
param mcpHostBaseUrl string = ''

@description('Run-scoped token-per-minute limit enforced before the ledger hop.')
param runTokensPerMinute int

@description('Run-scoped token quota enforced before the ledger hop.')
param runTokenQuota int

@description('Run-scoped token quota reset period enforced before the ledger hop.')
param runTokenQuotaPeriod string

@description('Resource ID of the Key Vault that stores the run-token signing secret.')
param keyVaultResourceId string

@description('Unversioned Key Vault secret identifier for the run-token signing key.')
@secure()
param runTokenSigningSecretIdentifier string

var apimName = toLower(take('apim${replace(nameSuffix, '-', '')}${resourceToken}', 50))
var skuParts = split(skuName, '_')
var skuBaseName = length(skuParts) > 1 ? skuParts[0] : skuName
var skuCapacity = length(skuParts) > 1 ? int(skuParts[1]) : 1
var openaiPolicyTemplate = loadTextContent('../../policies/openai-pipeline.xml')
var foundryPolicyTemplate = loadTextContent('../../policies/foundry-pipeline.xml')
var jwtBlock = clientAuthMode == 'entra-id' ? '<validate-jwt header-name="Authorization" output-token-variable-name="entraJwt" failed-validation-httpcode="401" failed-validation-error-message="Unauthorized"><openid-config url="${environment().authentication.loginEndpoint}${entraTenantId}/v2.0/.well-known/openid-configuration" /><audiences><audience>${entraApiAudience}</audience></audiences><require-scheme>Bearer</require-scheme></validate-jwt>' : ''
var allowedModelsJson = string(allowedModels)
// ponytail: allowedModelsJson is substituted into an XML attribute (value="...") in the
// policy templates below; its own double quotes must be XML-entity-escaped or they
// terminate the attribute early and corrupt the policy document (e.g. a bare `gpt-5.4`
// token left dangling where APIM then fails policy validation).
var allowedModelsJsonXmlSafe = replace(allowedModelsJson, '"', '&quot;')
var runLedgerBaseUrlNamedValueName = 'run-ledger-base-url'
// Bootstrap deployments intentionally omit the run-ledger image. APIM still requires
// syntactically valid non-empty backend/named-value URLs until the image pass updates it.
var effectiveRunLedgerBaseUrl = empty(runLedgerBaseUrl) ? 'https://127.0.0.1' : runLedgerBaseUrl
var runTokenSigningKeyNamedValueName = 'run-token-signing-key'
var runLedgerBaseUrlNamedValueReference = '{{${runLedgerBaseUrlNamedValueName}}}'
var runTokenSigningKeyNamedValueReference = '{{${runTokenSigningKeyNamedValueName}}}'
var adminUiEnabled = !empty(adminUiBaseUrl)
var mcpHostEnabled = !empty(mcpHostBaseUrl)
// ponytail: container-apps.bicep's adminUiFqdn output already includes the https:// scheme
// (unlike runLedger's bare-FQDN appFqdn output) -- use it as-is, don't double-prefix it.
var adminUiBackendUrl = adminUiBaseUrl
var openaiPolicy = replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(openaiPolicyTemplate, '{{JWT_BLOCK}}', jwtBlock), '{{CLIENT_AUTH_MODE}}', clientAuthMode), '{{ENTRA_TEAM_CLAIM}}', entraTeamClaim), '{{ALLOWED_MODELS_JSON}}', allowedModelsJsonXmlSafe), '{{DEFAULT_TPM}}', string(tokensPerMinute)), '{{DEFAULT_QUOTA}}', string(tokenQuota)), '{{DEFAULT_QUOTA_PERIOD}}', tokenQuotaPeriod), '{{OPENAI_BASE_URL}}', openaiPathBase), '{{RUN_LEDGER_BASE_URL}}', runLedgerBaseUrlNamedValueReference), '{{RUN_TOKEN_SIGNING_KEY_NAMED_VALUE}}', runTokenSigningKeyNamedValueReference), '{{RUN_TOKEN_QUOTA}}', string(runTokenQuota)), '{{RUN_TOKEN_QUOTA_PERIOD}}', runTokenQuotaPeriod), '{{RUN_TOKENS_PER_MINUTE}}', string(runTokensPerMinute))
var openaiPolicyRendered = replace(replace(openaiPolicy, '{{FOUNDRY_BASE_URL}}', foundryV1Base), '{{OPENAI_API_VERSION}}', openaiApiVersion)
var foundryPolicy = replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(foundryPolicyTemplate, '{{JWT_BLOCK}}', jwtBlock), '{{CLIENT_AUTH_MODE}}', clientAuthMode), '{{ENTRA_TEAM_CLAIM}}', entraTeamClaim), '{{ALLOWED_MODELS_JSON}}', allowedModelsJsonXmlSafe), '{{FOUNDRY_BASE_URL}}', foundryV1Base), '{{OPENAI_BASE_URL}}', openaiPathBase), '{{RUN_LEDGER_BASE_URL}}', runLedgerBaseUrlNamedValueReference), '{{RUN_TOKEN_SIGNING_KEY_NAMED_VALUE}}', runTokenSigningKeyNamedValueReference), '{{RUN_TOKEN_QUOTA}}', string(runTokenQuota)), '{{RUN_TOKEN_QUOTA_PERIOD}}', runTokenQuotaPeriod), '{{RUN_TOKENS_PER_MINUTE}}', string(runTokensPerMinute))
var foundryPolicyRendered = replace(foundryPolicy, '{{OPENAI_API_VERSION}}', openaiApiVersion)
var defaultConsumerConfig = base64('{}')
var keyVaultSecretsUserRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var coreNamedValueEntries = [
  {
    name: 'consumer-config-json'
    value: defaultConsumerConfig
  }
  {
    name: 'allowed-models'
    value: allowedModelsJson
  }
  {
    name: 'token-tpm-default'
    value: string(tokensPerMinute)
  }
  {
    name: 'token-quota-default'
    value: string(tokenQuota)
  }
  {
    name: 'token-quota-period-default'
    value: tokenQuotaPeriod
  }
  {
    name: runLedgerBaseUrlNamedValueName
    value: effectiveRunLedgerBaseUrl
  }
]
var tierTpmNamedValues = [for tierName in items(rateTiers): {
  name: 'tier-${tierName.key}-tpm'
  value: string(tierName.value.tpm)
}]
var tierQuotaNamedValues = [for tierName in items(rateTiers): {
  name: 'tier-${tierName.key}-quota'
  value: string(tierName.value.quota)
}]
var tierPeriodNamedValues = [for tierName in items(rateTiers): {
  name: 'tier-${tierName.key}-period'
  value: string(tierName.value.period)
}]
var namedValueEntries = concat(coreNamedValueEntries, tierTpmNamedValues, tierQuotaNamedValues, tierPeriodNamedValues)

resource openAiAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  scope: resourceGroup()
  name: last(split(openaiAccountId, '/'))
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  scope: resourceGroup()
  name: last(split(foundryAccountId, '/'))
}

resource keyVault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  scope: resourceGroup()
  name: last(split(keyVaultResourceId, '/'))
}

resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: apimName
  location: location
  tags: tags
  sku: {
    name: skuBaseName
    capacity: skuCapacity
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherName: publisherName
    publisherEmail: publisherEmail
    // ponytail: PremiumV2/classic Internal = full injection (private in+out). StandardV2/BasicV2 External = outbound-only integration (public inbound).
    virtualNetworkType: (skuBaseName == 'StandardV2' || skuBaseName == 'BasicV2') ? 'External' : 'Internal'
    virtualNetworkConfiguration: {
      subnetResourceId: apimSubnetId
    }
    publicNetworkAccess: 'Enabled'
  }
}

resource apimKeyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, apim.id, 'apim-kv-secrets-user')
  scope: keyVault
  properties: {
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRoleDefinitionId
  }
}

resource appInsightsLogger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'appinsights'
  properties: {
    loggerType: 'applicationInsights'
    resourceId: appInsightsId
    credentials: {
      instrumentationKey: appInsightsInstrumentationKey
    }
  }
}

resource namedValues 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = [for entry in namedValueEntries: {
  parent: apim
  name: entry.name
  properties: {
    displayName: entry.name
    value: entry.value
    secret: false
  }
}]

resource runTokenSigningKeyNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: runTokenSigningKeyNamedValueName
  properties: {
    displayName: runTokenSigningKeyNamedValueName
    keyVault: {
      secretIdentifier: runTokenSigningSecretIdentifier
    }
    secret: true
  }
  dependsOn: [
    apimKeyVaultSecretsUser
  ]
}

resource openAiApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'openai-gateway'
  properties: {
    path: 'openai'
    displayName: 'Azure OpenAI gateway'
    format: 'openapi-link'
    value: openaiOpenapiSpecUrl
    protocols: [
      'https'
    ]
    subscriptionRequired: clientAuthMode == 'subscription-key'
  }
}

resource runLedgerApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'run-ledger-gateway'
  properties: {
    // ponytail: the run-ledger Container App is internal-ingress-only (VNet-reachable only).
    // App Service is not VNet-integrated, so it cannot call it directly; this API is a thin
    // pass-through that lets App Service reach the ledger via APIM, which already sits in
    // the VNet. This app's model/agent traffic itself stays direct to Foundry (see
    // gateway/PORTING_NOTES.md) - only the ledger precall/postcall/run-mint calls route here.
    path: 'ledger'
    displayName: 'Run ledger gateway'
    format: 'openapi+json'
    value: '{"openapi":"3.0.1","info":{"title":"Run ledger proxy","version":"1.0.0"},"paths":{"/v1/runs":{"post":{"responses":{"200":{"description":"OK"}}}},"/v1/precall":{"post":{"responses":{"200":{"description":"OK"}}}},"/v1/postcall":{"post":{"responses":{"200":{"description":"OK"}}}}}}'
    protocols: [
      'https'
    ]
    subscriptionRequired: clientAuthMode == 'subscription-key'
  }
}

resource runLedgerApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: runLedgerApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: '<policies><inbound><base />${jwtBlock}<set-backend-service base-url="${effectiveRunLedgerBaseUrl}" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'
  }
}

resource runLedgerDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = {
  parent: runLedgerApi
  name: 'applicationinsights'
  properties: {
    loggerId: appInsightsLogger.id
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    metrics: true
    verbosity: 'information'
    httpCorrelationProtocol: 'W3C'
  }
}

resource mcpApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = if (mcpHostEnabled) {
  parent: apim
  name: 'cowork-mcp'
  properties: {
    path: 'mcp'
    displayName: 'Cowork MCP'
    protocols: [
      'https'
    ]
    subscriptionRequired: false
  }
}

var mcpPassthroughMethods = [
  'GET'
  'POST'
  'DELETE'
  'OPTIONS'
]

resource mcpApiOperations 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [for method in mcpPassthroughMethods: if (mcpHostEnabled) {
  parent: mcpApi
  name: 'mcp-${toLower(method)}'
  properties: {
    displayName: 'MCP ${method}'
    method: method
    urlTemplate: '/'
  }
}]

resource mcpApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = if (mcpHostEnabled) {
  parent: mcpApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: '<policies><inbound><base /><set-backend-service base-url="${mcpHostBaseUrl}" /><rewrite-uri template="/mcp/" copy-unmatched-params="true" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'
  }
}

resource mcpMetadataApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = if (mcpHostEnabled) {
  parent: apim
  name: 'cowork-mcp-protected-resource'
  properties: {
    path: '.well-known/oauth-protected-resource'
    displayName: 'Cowork MCP protected resource metadata'
    protocols: [
      'https'
    ]
    subscriptionRequired: false
  }
}

resource mcpMetadataGetOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = if (mcpHostEnabled) {
  parent: mcpMetadataApi
  name: 'mcp-metadata-get'
  properties: {
    displayName: 'MCP protected resource metadata'
    method: 'GET'
    urlTemplate: '/mcp'
  }
}

resource mcpMetadataOptionsOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = if (mcpHostEnabled) {
  parent: mcpMetadataApi
  name: 'mcp-metadata-options'
  properties: {
    displayName: 'MCP protected resource metadata preflight'
    method: 'OPTIONS'
    urlTemplate: '/mcp'
  }
}

resource mcpMetadataApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = if (mcpHostEnabled) {
  parent: mcpMetadataApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: '<policies><inbound><base /><set-backend-service base-url="${mcpHostBaseUrl}" /><rewrite-uri template="/.well-known/oauth-protected-resource/mcp" copy-unmatched-params="false" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'
  }
}

resource mcpDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = if (mcpHostEnabled) {
  parent: mcpApi
  name: 'applicationinsights'
  properties: {
    loggerId: appInsightsLogger.id
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    metrics: true
    verbosity: 'information'
    httpCorrelationProtocol: 'W3C'
  }
}

resource adminUiApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = if (adminUiEnabled) {
  parent: apim
  name: 'admin-ui-gateway'
  properties: {
    // ponytail: same internal-ingress-only situation as run-ledger above -- the Admin UI
    // Container App stays VNet-private (governance requirement), and APIM (already in the
    // VNet, already externally reachable) is the one public door onto it. This is a
    // wildcard reverse-proxy rather than a small fixed operation set (run-ledger's 3 POST
    // routes) because the Admin UI serves a SPA plus a multi-route FastAPI BFF (auth
    // callback, static assets, /api/* data routes) that we do not want to keep enumerating
    // here every time a route is added.
    path: 'admin'
    displayName: 'Admin UI gateway'
    subscriptionRequired: false
    protocols: [
      'https'
    ]
  }
}

// ponytail: APIM operations don't support a literal '*' method or '/*' urlTemplate -- that
// was never a deployable shape, just a placeholder that got live-patched via CLI into these
// 8 explicit operations (7 verbs against the {*path} catch-all, plus a dedicated root GET,
// since {*path} alone doesn't match the bare '/'). Reconciled here to match what's live.
var adminUiPassthroughMethods = [
  'GET'
  'POST'
  'PUT'
  'DELETE'
  'PATCH'
  'HEAD'
  'OPTIONS'
]

resource adminUiApiPassthroughOperations 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [for method in adminUiPassthroughMethods: if (adminUiEnabled) {
  parent: adminUiApi
  name: 'passthrough-${toLower(method)}'
  properties: {
    displayName: 'Passthrough ${method}'
    method: method
    urlTemplate: '/{*path}'
    templateParameters: [
      {
        name: 'path'
        required: true
        type: 'string'
      }
    ]
  }
}]

resource adminUiApiRootGetOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = if (adminUiEnabled) {
  parent: adminUiApi
  name: 'passthrough-root-get'
  properties: {
    displayName: 'Passthrough root GET'
    method: 'GET'
    urlTemplate: '/'
  }
}

resource adminUiApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = if (adminUiEnabled) {
  parent: adminUiApi
  name: 'policy'
  properties: {
    format: 'xml'
    // No validate-jwt here: the Admin UI BFF already performs its own Entra auth-code
    // flow with session cookies (Entra-gated, admin-group scoped) -- an APIM-level bearer
    // check would conflict with that browser/cookie based flow.
    value: '<policies><inbound><base /><set-backend-service base-url="${adminUiBackendUrl}" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'
  }
}

resource adminUiDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = if (adminUiEnabled) {
  parent: adminUiApi
  name: 'applicationinsights'
  properties: {
    loggerId: appInsightsLogger.id
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    metrics: true
    verbosity: 'information'
    httpCorrelationProtocol: 'W3C'
  }
}

resource foundryApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'foundry-gateway'
  properties: {
    path: 'foundry'
    displayName: 'Foundry gateway'
    format: 'openapi+json'
    value: '{"openapi":"3.0.1","info":{"title":"Foundry proxy","version":"1.0.0"},"paths":{"/responses":{"post":{"responses":{"200":{"description":"OK"}}}}}}'
    protocols: [
      'https'
    ]
    subscriptionRequired: clientAuthMode == 'subscription-key'
  }
}

resource openAiApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: openAiApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: openaiPolicyRendered
  }
  dependsOn: [
    namedValues
    runTokenSigningKeyNamedValue
  ]
}

resource foundryApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: foundryApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: foundryPolicyRendered
  }
  dependsOn: [
    namedValues
    runTokenSigningKeyNamedValue
  ]
}

resource openAiDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = {
  parent: openAiApi
  name: 'applicationinsights'
  properties: {
    loggerId: appInsightsLogger.id
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    frontend: {
      request: {
        headers: [
          '*'
        ]
        body: {
          bytes: 0
        }
      }
      response: {
        headers: [
          '*'
        ]
        body: {
          bytes: 0
        }
      }
    }
    backend: {
      request: {
        headers: [
          '*'
        ]
        body: {
          bytes: 0
        }
      }
      response: {
        headers: [
          '*'
        ]
        body: {
          bytes: 0
        }
      }
    }
    metrics: true
    verbosity: 'information'
    httpCorrelationProtocol: 'W3C'
  }
}

resource foundryDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = {
  parent: foundryApi
  name: 'applicationinsights'
  properties: {
    loggerId: appInsightsLogger.id
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    frontend: {
      request: {
        headers: [
          '*'
        ]
        body: {
          bytes: 0
        }
      }
      response: {
        headers: [
          '*'
        ]
        body: {
          bytes: 0
        }
      }
    }
    backend: {
      request: {
        headers: [
          '*'
        ]
        body: {
          bytes: 0
        }
      }
      response: {
        headers: [
          '*'
        ]
        body: {
          bytes: 0
        }
      }
    }
    metrics: true
    verbosity: 'information'
    httpCorrelationProtocol: 'W3C'
  }
}

resource openAiUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apim.id, openAiAccount.id, 'openai-user')
  scope: openAiAccount
  properties: {
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  }
}

resource foundryUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apim.id, foundryAccount.id, 'foundry-user')
  scope: foundryAccount
  properties: {
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
  }
}

// ponytail: allApis subscription so App Service (the only intended caller today) can present
// an Ocp-Apim-Subscription-Key under clientAuthMode=subscription-key. Its key is written into
// the existing Key Vault so it never needs to be pasted into an app setting by hand.
resource appServiceSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apim
  name: 'noc-app-service'
  properties: {
    displayName: 'noc-app-service'
    scope: '/apis'
    state: 'active'
    allowTracing: false
  }
}

resource appServiceSubscriptionKeySecret 'Microsoft.KeyVault/vaults/secrets@2024-11-01' = {
  parent: keyVault
  name: 'apim-app-service-subscription-key'
  properties: {
    value: appServiceSubscription.listSecrets().primaryKey
  }
}

output apimId string = apim.id
output apimName string = apim.name
output gatewayUrl string = 'https://${apim.name}.azure-api.net'
output ledgerGatewayUrl string = 'https://${apim.name}.azure-api.net/ledger'
output adminUiGatewayUrl string = adminUiEnabled ? 'https://${apim.name}.azure-api.net/admin' : ''
output mcpGatewayUrl string = mcpHostEnabled ? 'https://${apim.name}.azure-api.net/mcp' : ''
output mcpProtectedResourceMetadataUrl string = mcpHostEnabled ? 'https://${apim.name}.azure-api.net/.well-known/oauth-protected-resource/mcp' : ''
output appServiceSubscriptionKeySecretUri string = appServiceSubscriptionKeySecret.properties.secretUri
output privateIpAddress string = !empty(apim.properties.privateIPAddresses) ? apim.properties.privateIPAddresses[0] : ''
output identityPrincipalId string = apim.identity.principalId
