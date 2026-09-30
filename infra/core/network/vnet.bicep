targetScope = 'resourceGroup'

// Agent VNet. Created before the Foundry account so the optional agent subnet
// can be used for Foundry network injection (injection is set at account
// creation time only).

@description('Region for the VNet.')
param location string

@description('Tags applied to network resources.')
param tags object = {}

@description('Deterministic suffix used in resource names.')
param resourceToken string

@description('Add a /24 subnet delegated to Microsoft.App/environments for Foundry Agent Service network injection.')
param enableAgentSubnet bool = false

@description('Add a /27 subnet delegated to Azure Container Instances for the private jumpbox.')
param enableJumpboxSubnet bool = false

var vnetName = 'vnet-agent-${resourceToken}'

var baseSubnets = [
  {
    name: 'appservice-integration'
    properties: {
      addressPrefix: '10.42.0.0/26'
      delegations: [
        {
          name: 'web-app-delegation'
          properties: {
            serviceName: 'Microsoft.Web/serverFarms'
          }
        }
      ]
    }
  }
  {
    name: 'private-endpoints'
    properties: {
      addressPrefix: '10.42.0.64/27'
      privateEndpointNetworkPolicies: 'Disabled'
    }
  }
]
var agentSubnet = [
  {
    name: 'agent'
    properties: {
      addressPrefix: '10.42.1.0/24'
      delegations: [
        {
          name: 'agent-service-delegation'
          properties: {
            serviceName: 'Microsoft.App/environments'
          }
        }
      ]
    }
  }
]
var jumpboxSubnet = [
  {
    name: 'jumpbox'
    properties: {
      addressPrefix: '10.42.2.0/27'
      delegations: [
        {
          name: 'aci-delegation'
          properties: {
            serviceName: 'Microsoft.ContainerInstance/containerGroups'
          }
        }
      ]
    }
  }
]

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: concat(
        ['10.42.0.0/24'],
        enableAgentSubnet ? ['10.42.1.0/24'] : [],
        enableJumpboxSubnet ? ['10.42.2.0/27'] : []
      )
    }
    subnets: concat(baseSubnets, enableAgentSubnet ? agentSubnet : [], enableJumpboxSubnet ? jumpboxSubnet : [])
  }
}

output vnetId string = vnet.id
output vnetName string = vnet.name
output integrationSubnetId string = '${vnet.id}/subnets/appservice-integration'
output privateEndpointSubnetId string = '${vnet.id}/subnets/private-endpoints'
output agentSubnetId string = enableAgentSubnet ? '${vnet.id}/subnets/agent' : ''
output jumpboxSubnetId string = enableJumpboxSubnet ? '${vnet.id}/subnets/jumpbox' : ''
