targetScope = 'resourceGroup'

@description('Azure region for the Cowork MCP Container App.')
param location string = resourceGroup().location

@description('Tags applied to the Cowork MCP resources.')
param tags object = {}

@description('Deterministic name suffix shared across resources in this stack.')
param nameSuffix string

@description('Existing Container Apps environment resource ID.')
param managedEnvironmentId string

@description('Azure Container Registry resource ID.')
param acrId string

@description('Azure Container Registry login server.')
param acrLoginServer string

@description('Cowork MCP host image reference. Empty skips the app.')
param mcpHostImage string

@description('MCP host managed identity resource ID.')
param mcpHostIdentityId string

@description('MCP host managed identity principal ID.')
param mcpHostPrincipalId string

@description('MCP host managed identity client ID.')
param mcpHostClientId string

@description('Microsoft Entra tenant ID.')
param entraTenantId string

@description('Client ID of the Entra resource application securing the MCP server.')
param mcpServerAppClientId string

@description('Identifier URI accepted as an MCP access-token audience.')
param mcpServerAudience string

@description('Delegated scope value exposed by the MCP resource application.')
param mcpRequiredScope string = 'noc.invoke'

@description('Optional Entra object ID allowed to invoke MCP (normally the Cowork users group).')
param mcpAllowedCallerObjectId string = ''

@description('Type of mcpAllowedCallerObjectId: Group or User.')
param mcpAllowedCallerType string = 'Group'

@description('Public canonical MCP URL exposed through APIM.')
param publicMcpUrl string

@description('Foundry project endpoint used by NocAgent.')
param foundryProjectEndpoint string

@description('Foundry model deployment used by NocAgent.')
param foundryModelDeploymentName string

@description('Internal run-ledger base URL.')
param runLedgerBaseUrl string

@description('Per-run ledger budget in micros.')
param runLedgerBudgetMicros int = 2000000

@description('Run-ledger policy set.')
param runLedgerPolicySet string = 'default'

@minValue(1)
@maxValue(28)
@description('Cowork MCP tool timeout in seconds.')
param mcpToolTimeoutSeconds int = 28

@description('Application Insights connection string.')
param appInsightsConnectionString string

var mcpHostEnabled = !empty(mcpHostImage)
var appName = mcpHostEnabled ? 'ca-mcphost-${nameSuffix}' : ''
var scopeUri = 'api://${mcpServerAppClientId}/${mcpRequiredScope}'
var acrPullRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource mcpHostAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (mcpHostEnabled) {
  name: guid(acrId, mcpHostPrincipalId, 'mcphost-acrpull')
  scope: resourceGroup()
  properties: {
    principalId: mcpHostPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleDefinitionId
  }
}

resource mcpHostApp 'Microsoft.App/containerApps@2024-10-02-preview' = if (mcpHostEnabled) {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${mcpHostIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: managedEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: acrLoginServer
          identity: mcpHostIdentityId
        }
      ]
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
    }
    template: {
      containers: [
        {
          name: 'mcp-host'
          image: mcpHostImage
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: mcpHostClientId
            }
            {
              name: 'AZURE_TENANT_ID'
              value: entraTenantId
            }
            {
              name: 'MCP_SERVER_APP_CLIENT_ID'
              value: mcpServerAppClientId
            }
            {
              name: 'MCP_SERVER_AUDIENCE'
              value: mcpServerAudience
            }
            {
              name: 'MCP_REQUIRED_SCOPE'
              value: mcpRequiredScope
            }
            {
              name: 'MCP_ALLOWED_CALLER_OBJECT_ID'
              value: mcpAllowedCallerObjectId
            }
            {
              name: 'MCP_ALLOWED_CALLER_TYPE'
              value: mcpAllowedCallerType
            }
            {
              name: 'MCP_SCOPE_URI'
              value: scopeUri
            }
            {
              name: 'PUBLIC_MCP_URL'
              value: publicMcpUrl
            }
            {
              name: 'MCP_TOOL_TIMEOUT_SECONDS'
              value: string(mcpToolTimeoutSeconds)
            }
            {
              name: 'FOUNDRY_PROJECT_ENDPOINT'
              value: foundryProjectEndpoint
            }
            {
              name: 'AZURE_AI_MODEL_DEPLOYMENT_NAME'
              value: foundryModelDeploymentName
            }
            {
              name: 'RUN_LEDGER_BASE_URL'
              value: runLedgerBaseUrl
            }
            {
              name: 'RUN_LEDGER_BUDGET_MICROS'
              value: string(runLedgerBudgetMicros)
            }
            {
              name: 'RUN_LEDGER_POLICY_SET'
              value: runLedgerPolicySet
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsightsConnectionString
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
  dependsOn: [
    mcpHostAcrPull
  ]
}

resource mcpHostAuth 'Microsoft.App/containerApps/authConfigs@2024-10-02-preview' = if (mcpHostEnabled) {
  parent: mcpHostApp
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      // The app emits RFC 9728's resource_metadata challenge. It still requires
      // Easy Auth's injected X-MS-CLIENT-PRINCIPAL before trusting JWT claims.
      unauthenticatedClientAction: 'AllowAnonymous'
      excludedPaths: [
        '/healthz'
        '/.well-known/oauth-protected-resource/mcp'
      ]
    }
    identityProviders: {
      azureActiveDirectory: {
        registration: {
          clientId: mcpServerAppClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
        }
        validation: {
          allowedAudiences: union([
            mcpServerAppClientId
          ], [
            mcpServerAudience
          ])
          defaultAuthorizationPolicy: {
            allowedApplications: [
              'ab3be6b7-f5df-413d-ac2d-abf1e3fd9c0b'
            ]
          }
        }
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
    httpSettings: {
      requireHttps: true
    }
  }
}

output appName string = appName
output appFqdn string = mcpHostEnabled ? 'https://${mcpHostApp!.properties.configuration.ingress.fqdn}' : ''
