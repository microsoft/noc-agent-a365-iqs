<#
.SYNOPSIS
  Deploy the NOC agent: default mode, or network-isolated with -Network yes.

.EXAMPLE
  ./scripts/deploy.ps1                # current default deployment (NETWORK_ISOLATION=false)
  ./scripts/deploy.ps1 -Network yes   # private endpoints + VNet injection + jumpbox

  Extra arguments are passed to `azd up`. See docs/PRIVATE_NETWORKING.md.
#>
param(
    [ValidateSet('yes', 'no')] [string] $Network = 'no',
    [Parameter(ValueFromRemainingArguments = $true)] $AzdArgs
)
$ErrorActionPreference = 'Stop'

azd env set NETWORK_ISOLATION $(if ($Network -eq 'yes') { 'true' } else { 'false' })
azd up @AzdArgs
exit $LASTEXITCODE
