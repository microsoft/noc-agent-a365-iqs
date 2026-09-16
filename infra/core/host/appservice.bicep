targetScope = 'resourceGroup'

@description('Azure region.')
param location string = resourceGroup().location

@description('Tags applied to all resources.')
param tags object = {}

@description('Name prefix for the App Service plan and web app.')
param resourceToken string

@description('App settings for the web app (non-secret).')
param appSettings object = {}

@description('Optional regional VNet integration subnet resource ID.')
param virtualNetworkSubnetId string = ''

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'asp-${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'B1'
    tier: 'Basic'
  }
  kind: 'linux'
  properties: {
    reserved: true
  }
}

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: 'app-${resourceToken}'
  location: location
  tags: union(tags, { 'azd-service-name': 'web' })
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    virtualNetworkSubnetId: empty(virtualNetworkSubnetId) ? null : virtualNetworkSubnetId
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.13'
      appCommandLine: 'python start_with_generic_host.py'
      alwaysOn: true
      appSettings: [for key in items(appSettings): {
        name: key.key
        value: key.value
      }]
    }
  }
}

output webAppName string = webApp.name
output webAppHostName string = webApp.properties.defaultHostName
output principalId string = webApp.identity.principalId
