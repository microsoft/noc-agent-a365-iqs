targetScope = 'resourceGroup'

// Private jumpbox: an Azure CLI container in the VNet, reached with
// `az container exec` over the control plane (no VM, RDP or Bastion).
// Pattern from pranabpaul-tech/foundry-iq-v2 and pdm-fabric-foundry.

@description('Region; must match the VNet.')
param location string

@description('Tags applied to the container group.')
param tags object = {}

@description('Subnet delegated to Microsoft.ContainerInstance/containerGroups.')
param subnetId string

@description('Container group name.')
param name string

resource jumpbox 'Microsoft.ContainerInstance/containerGroups@2023-05-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    osType: 'Linux'
    sku: 'Standard'
    restartPolicy: 'Always'
    subnetIds: [
      {
        id: subnetId
      }
    ]
    containers: [
      {
        name: 'jumpbox'
        properties: {
          image: 'mcr.microsoft.com/azure-cli:latest'
          command: ['/bin/sh', '-c', 'tail -f /dev/null']
          resources: {
            requests: {
              cpu: 1
              memoryInGB: 2
            }
          }
        }
      }
    ]
  }
}

output name string = jumpbox.name
output principalId string = jumpbox.identity.principalId
