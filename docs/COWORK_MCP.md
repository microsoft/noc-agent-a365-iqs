# Copilot Cowork MCP channel

This channel exposes the existing in-process `NocAgent._agent` as one
read-only MCP tool, `noc_investigate`, without changing the Teams/A365
channel. The canonical public endpoint is APIM `/mcp`; APIM is a transparent
pass-through to an Azure Container App protected by Container Apps built-in
authentication (Easy Auth).

## Target configuration

These values are deployment inputs only. Nothing in this repository deploys
until an operator runs the commands.

| Setting | Value |
|---|---|
| Subscription | `<subscription-id>` |
| Tenant | `<tenant-id>` |
| Region | `<azure-region>` |
| Resource group | `<resource-group>` |
| MCP route | `https://<apim-name>.azure-api.net/mcp` |
| Protected-resource metadata | `https://<apim-name>.azure-api.net/.well-known/oauth-protected-resource/mcp` |
| MCP scope value | `noc.invoke` |

## Runtime and security contract

- `StreamableHTTPSessionManager(json_response=True, stateless=True)` serves
  `/mcp`. One `NocAgent` is initialized for the process lifespan.
- Every `tools/call` reads the Easy Auth-validated bearer token, defensively
  checks `tid`, `aud`, `oid`, `scp`, `exp`, and `nbf` without reimplementing
  JWT signature validation, then resets all per-call context variables in a
  `finally` block.
- Easy Auth uses `AllowAnonymous` only so the ASGI app can emit the exact RFC
  9728 challenge for missing credentials. A call is accepted only when Easy
  Auth injected `X-MS-CLIENT-PRINCIPAL` and its tenant/object claims match the
  bearer payload; an unvalidated bearer token is rejected.
- The run ID is derived from the caller `oid` and MCP JSON-RPC request ID.
  Run-token minting, precall/postcall decisions, specialist routing, and usage
  logging reuse `NocAgent` and the existing run ledger. Specialist usage stays
  exact; the MCP adapter closes the orchestrator reservation with a text-token
  estimate because `AgentMCPTool` returns MCP content blocks rather than the
  underlying `AgentResponse` usage object.
- Downstream user delegation uses `azure.identity.aio.OnBehalfOfCredential`.
  Its client assertion is a token minted for
  `api://AzureADTokenExchange` by the MCP user-assigned managed identity; the
  OBO target is `https://ai.azure.com/.default`. No client secret is stored.
- APIM performs no model, token, or body policy on `/mcp`. It preserves the
  MCP request/response, authorization, protocol headers, and
  `WWW-Authenticate` challenge.

## 1. Prerequisites

Install Azure CLI with Bicep, Python 3.11+, Docker or ACR Tasks, and the
Microsoft 365 Agents Toolkit CLI:

```powershell
npm install -g @microsoft/m365agentstoolkit-cli
az login --tenant <tenant-id>
az account set --subscription <subscription-id>
```

The operator needs rights to deploy the gateway stack and assign RBAC. The
Entra setup requires application administrator rights; tenant-wide delegated
admin consent requires an appropriately privileged administrator.

Capture these existing agent-stack values before deploying the MCP host:

```powershell
$foundryEndpoint = "<FOUNDRY_PROJECT_ENDPOINT>"
$foundryProjectResourceId = "<Foundry account/project ARM resource ID>"
$searchServiceResourceId = "<Azure AI Search ARM resource ID>"
$model = "<AZURE_AI_MODEL_DEPLOYMENT_NAME>"
```

## 2. Bootstrap the gateway identity

The Entra federated credential needs the MCP UAMI principal ID, while the full
MCP Container App needs the Entra resource-app ID. Use two idempotent Bicep
passes for a fresh environment:

1. Deploy `gateway/infra/main.bicep` with `mcpHostImage=''`. This recreates the
   complete gateway base, including APIM, the Container Apps environment,
   run ledger, ACR, and `id-mcphost-*`, but intentionally skips the MCP app and
   APIs.
2. Capture deployment outputs `mcpHostIdentityClientId`,
   `mcpHostIdentityPrincipalId`, `apimName`, and `acrLoginServer`.

Supply the existing required gateway parameters, including the secure
`runTokenSigningSecretValue`; do not put that value in a parameter file or
source control. Use `az deployment sub what-if` before the first create.

## 3. Create the Entra applications and trust

Run the idempotent setup script with the UAMI outputs:

```powershell
python scripts/setup_cowork_mcp_entra.py `
  --tenant-id <tenant-id> `
  --mcp-uami-client-id "<mcpHostIdentityClientId>" `
  --mcp-uami-principal-id "<mcpHostIdentityPrincipalId>"
```

The script creates or updates:

- the MCP server/resource application, `requestedAccessTokenVersion=2`, with
  exposed delegated scope `noc.invoke`;
- a separate Cowork OAuth client retained for direct delegated-flow diagnostics;
  the Teams Developer Portal auth configuration itself must use the MCP
  resource application's client ID;
- client-to-resource delegated permission and admin consent for that diagnostic
  client;
- the resource application's delegated Azure AI permission needed by the OBO
  exchange, plus admin consent;
- a federated identity credential that trusts the MCP UAMI as the resource
  application's client assertion.

It emits both app IDs, the resource URI, full scope, deterministic Cowork
manifest GUID, and an explicit placeholder for the external
`OAuthPluginVault` auth configuration. Re-running it reuses exact app display
names or IDs and merges permissions; it never creates a client secret.

### Create the external auth configuration

The Microsoft Enterprise token-store record is a Microsoft 365 operation and
is not exposed by the available Microsoft Graph APIs, so the script cannot
create it. In the [Teams Developer Portal](https://dev.teams.microsoft.com/):

1. Open **Tools > Microsoft Entra SSO client ID registration** and select
   **New client registration**.
2. Set the base URL to the APIM MCP URL. Set **Client ID** to the emitted
   `mcpResourceAppClientId` (the app securing the MCP server), **not**
   `coworkOAuthClientAppId`. Set **Scope** to the emitted full
   `api://<resource-app-id>/noc.invoke` scope.
3. Restrict the registration to the target organization. If the final Teams
   app ID is not yet registered, select **Any Teams app** temporarily. After
   installation, bind it to the app manifest's stable `id` (`coworkManifestId`),
   not an Agent Identity, Entra object ID, OAuth client ID, or installation
   `TitleId`.
4. Save and copy both generated values:
   - **Microsoft Entra SSO registration ID** -> `COWORK_AUTH_CONFIG_REFERENCE_ID`.
   - **Application ID URI** -> the additional audience required below.

If the generated Application ID URI ends with `coworkOAuthClientAppId`, the
wrong client ID was registered. Delete or replace that auth configuration and
repeat step 2 with `mcpResourceAppClientId`.

The portal registration is not the final authentication step. Per the
[Microsoft Entra SSO guidance](https://learn.microsoft.com/microsoft-365/copilot/extensibility/plugin-authentication-entra-sso), rerun the setup script with the generated URI:

```powershell
python scripts/setup_cowork_mcp_entra.py `
  --tenant-id <tenant-id> `
  --mcp-uami-client-id "<mcpHostIdentityClientId>" `
  --mcp-uami-principal-id "<mcpHostIdentityPrincipalId>" `
  --resource-app-id "<mcpResourceAppClientId>" `
  --cowork-client-app-id "<coworkOAuthClientAppId>" `
  --sso-application-id-uri "<portal-generated-Application-ID-URI>"
```

This preserves the original `api://<resource-app-id>` identifier, adds the
portal-generated identifier URI, adds the Teams OAuth consent redirect URI,
and preauthorizes the Microsoft Enterprise token store client
`ab3be6b7-f5df-413d-ac2d-abf1e3fd9c0b` for `noc.invoke`. Finally, redeploy the
MCP host with `mcpServerAudience` set to the portal-generated Application ID
URI so both Easy Auth and the application accept the token audience.

## 4. Build and deploy the MCP host

Build the dedicated image from the `agent` context:

```powershell
az acr build `
  --registry "<acr-name>" `
  --image "noc-mcp:1.0.0" `
  --file agent/Dockerfile.mcp `
  agent
```

Run the second `gateway/infra/main.bicep` deployment with:

```text
mcpHostImage=<acr-login-server>/noc-mcp:1.0.0
mcpFoundryProjectEndpoint=<existing Foundry project endpoint>
mcpFoundryModelDeploymentName=<existing model deployment>
mcpFoundryProjectResourceId=<existing Foundry project ARM resource ID>
mcpSearchServiceResourceId=<existing Search service ARM resource ID>
mcpCallerPrincipalId=<Entra group object ID containing authorized Cowork users>
mcpCallerPrincipalType=Group
mcpServerAppClientId=<script mcpResourceAppClientId>
mcpServerAudience=<portal-generated Application ID URI>
mcpRequiredScope=noc.invoke
mcpToolTimeoutSeconds=28
entraTenantId=<tenant-id>
```

This pass creates the externally-ingressed MCP Container App inside the
internal Container Apps environment, Easy Auth, managed-identity Foundry/Search
RBAC, all five project-scoped caller roles required by the OBO specialist path,
and both APIM routes. The same caller group also needs Fabric workspace Viewer
(or higher) for RTI/Fabric IQ data-plane access. Validate the metadata route:

```powershell
curl.exe "https://<apim-name>.azure-api.net/.well-known/oauth-protected-resource/mcp"
curl.exe -i -X POST "https://<apim-name>.azure-api.net/mcp"
```

The first call returns RFC 9728 metadata. The unauthenticated MCP call returns
`401` and a `WWW-Authenticate` header containing the same metadata URL.

## 5. Package and sideload Cowork

The committed icons are deterministic valid PNG files. Regenerate them only
when intentionally changing icon code:

```powershell
python cowork/generate_icons.py
```

Build the package with the values emitted above:

```powershell
python cowork/package.py `
  --mcp-url "https://<apim-name>.azure-api.net/mcp" `
  --auth-config-reference-id "<Microsoft-Entra-SSO-registration-ID>" `
  --manifest-id "<coworkManifestId>"
```

The output is `cowork/build/noc-cowork.zip`. The script resolves placeholders
in memory, validates the URL/GUID, and packages `manifest.json`, both icons,
the matching tool description, and all three `SKILL.md` folders. The tool
description is written as `noc-mcp-tools.json` at the ZIP root (not under a
`tools/` directory), and must contain a non-empty top-level `tools` array.

For a personal sideload:

```powershell
npm install -g @microsoft/m365agentstoolkit-cli
atk auth login m365
atk install --file-path "C:/Flutter/noc-agent-a365/cowork/build/noc-cowork.zip" --scope Personal
```

For tenant testing, open **Microsoft 365 admin center > Manage apps > Upload
custom app**, upload the same ZIP, choose **Add agent**, and assign it to the
test users. Save the returned `TitleId` and `AppId` from either path.

## 6. Test in Cowork

1. Open Microsoft 365 Copilot **Cowork > Sources & Skills > Plugins**.
2. Find **NOC Investigation** in **Discover**, enable it, and complete the
   one-time Entra consent prompt.
3. Start a new Cowork task: `Triage incident INC-001. Give me confirmed
   telemetry, affected services, and the next runbook action.`
4. Test blast radius separately: `For LINK-SYD-MEL-FIBRE-01, identify affected
   services, SLA exposure, alternate paths, and shared-conduit risk.`
5. Test drafting separately: `Draft a customer status update for INC-001 using
   confirmed impact only. Do not send it.`
6. Confirm the tool shown in the run is `noc_investigate`, results identify
   specialist grounding, and no action is represented as executed.

**Cowork requires each tool call to complete in less than 30 seconds.** The
host enforces `MCP_TOOL_TIMEOUT_SECONDS` with a default and hard ceiling of
28 seconds. A full NOC investigation across all five specialists can exceed
that limit; use the three focused skill workflows or retry with a narrower
asset/evidence question. The timeout is enforced in the application, not
simulated by an APIM policy.
