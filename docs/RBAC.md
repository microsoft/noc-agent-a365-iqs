# RBAC Chart — Roles, Identities, and Resources

Every role assignment this implementation actually needs, split into what's
declared in Bicep (`infra/`, `gateway/infra/`) vs. what had to be granted
manually during live troubleshooting because no ARM/Bicep surface covers it
(Fabric permissions, Entra consent grants, Cosmos data-plane RBAC). Built
directly from `infra/core/ai/rbac.bicep`, `gateway/infra/core/**/*.bicep`,
`docs/DEPLOYMENT.md` §4b/§6/§9c, and every fix logged in
`docs/TROUBLESHOOTING.md`.

## Identity map

| Identity | Type | What it's used for |
|---|---|---|
| App Service system-assigned managed identity | Service | The MAF orchestrator's own credential (`_get_service_credential()`) — used for Foundry IQ / Web IQ connections and its own project-scope Foundry calls. |
| Entra **Agent Identity** + auto-provisioned **agent-user** (`#microsoft.graph.agentUser`, e.g. `nocagent@<tenant>`) | Agent 365 | Created by `a365 setup all`. This is the OBO subject for every Teams turn — Fabric IQ, Work IQ, and `noc-incident-agent` (RTI) all enforce access against *this* identity, not the human Teams user directly. |
| `noc-iq-demo-teams-users` AAD group (recommended) | Group | Assign Foundry project + Fabric roles to this group once; add/remove Teams users as members instead of repeating per-user role assignments. |
| `identities.bicep` worker/admin-ui/run-ledger user-assigned managed identities | Service | Gateway-side (APIM cost-governance stack): `config-sync-worker` job, Admin UI Container App, run-ledger service. |
| `id-mcphost-*` user-assigned managed identity | Service | Cowork MCP Container App: ACR pull, Foundry/Search access, and a federated client assertion for user OBO. It has no client secret. |
| Cowork MCP resource app + OAuth client app | Entra applications | Resource app exposes `noc.invoke`; the client requests it and uses the Teams OAuth redirect. Provisioned idempotently by `scripts/setup_cowork_mcp_entra.py`. |
| Work IQ app registration (classic Entra app, `WORKIQ_ENTRA_APP_ID`) | Service (delegated) | The only classic app registration in this design — needed because Work IQ uses delegated Graph auth, unlike everything else which is UserEntraToken/OBO or managed identity. |

## 1. Foundry project RBAC (`infra/core/ai/rbac.bicep`) — declared in Bicep

All scoped to the **project** sub-resource, never the account — an
account-scope-only assignment is silently not honored by the Agents
data-plane check (`docs/PRIMER_MCP_CANCEL_SCOPE_BUG.md`,
`docs/TROUBLESHOOTING.md` "Foundry project RBAC").

| Principal | Role | Role ID | Scope | Why |
|---|---|---|---|---|
| App Service managed identity | Azure AI Developer | `64702f94-c441-49e6-a78b-ef80e0188fee` | Foundry project | Orchestrator's own tool/agent calls. |
| App Service managed identity | Cognitive Services User | `a97b65f3-24c7-4388-baec-2e87135dc908` | Foundry project | Generic model-invocation permission. |
| App Service managed identity | Search Index Data Reader | `1407120a-92aa-4202-b7e9-c0e197c71c8f` | Azure AI Search service | Foundry IQ's knowledge-base MCP tool queries Search directly with the app's own credential — **missing this caused every session to fail with a masked "Cancelled via cancel scope" error** (see below). |
| MCP host managed identity | Azure AI Developer + Cognitive Services User | `64702f94-c441-49e6-a78b-ef80e0188fee`, `a97b65f3-24c7-4388-baec-2e87135dc908` | Foundry project | Host-level orchestration and model access. |
| MCP host managed identity | Search Index Data Reader | `1407120a-92aa-4202-b7e9-c0e197c71c8f` | Search service named by the full `mcpSearchServiceResourceId`, even when it is in another resource group/subscription | Foundry IQ knowledge retrieval. |
| `teamsUsersPrincipalId` / `mcpCallerPrincipalId` (AAD group, e.g. `noc-iq-demo-teams-users`) | Foundry Agent Consumer | `eed3b665-ab3a-47b6-8f48-c9382fb1dad6` | Foundry project | OAuth identity-passthrough for persisted Prompt Agents (`noc-topology-agent`, `noc-comms-agent`, `noc-incident-agent`) — the calling Teams/Cowork user's own grant is what Agent Service checks. |
| `teamsUsersPrincipalId` (AAD group) | Azure AI Developer | `64702f94-c441-49e6-a78b-ef80e0188fee` | Foundry project | The toolbox **MCP endpoint** (every tool call) — this was the real fix for the "cancel scope" error (§2 below). |
| `teamsUsersPrincipalId` (AAD group) | **Foundry Project Runtime User** ✅ load-bearing | `142bfaed-a13f-4c2d-bed2-6db62c4a1009` | Foundry project | The **only** role whose `dataActions` include `Microsoft.CognitiveServices/accounts/AIServices/responses/*` — the exact action a direct (non-`agent_reference`) Responses API call hits. |
| `teamsUsersPrincipalId` (AAD group) | Cognitive Services OpenAI User | `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd` | Foundry project | Generic model-invocation role, kept as belt-and-suspenders. |
| `teamsUsersPrincipalId` (AAD group) | Cognitive Services User | `a97b65f3-24c7-4388-baec-2e87135dc908` | Foundry project | Generic model-invocation role, kept as belt-and-suspenders. |

> **Automated this session** — all 5 caller role assignments are declared in
> `infra/core/ai/rbac.bicep` for Teams and
> `gateway/infra/core/ai/mcp-host-rbac.bicep` for Cowork. Pass the same Entra
> group to `teamsUsersPrincipalId` and `mcpCallerPrincipalId`; no per-user
> manual Azure role assignments are needed.

## 2. Foundry project RBAC — background/history (why 4 of the 5 roles above exist)

These apply to the **agentic user identity** (the agent-user, or the AAD
group it's added to). Discovered by direct `az role definition list`
inspection of `dataActions` after three roles in a row turned out
insufficient (`docs/TROUBLESHOOTING.md` "Foundry project RBAC: the caller of
the Responses API needs its own role"). Kept here for the historical
"why" — the actual assignments now live in §1's table above, in Bicep.

| Role | Role ID (if resolved) | Scope | Surface it actually covers |
|---|---|---|---|
| Azure AI Developer | `64702f94-c441-49e6-a78b-ef80e0188fee` | Foundry project | The toolbox **MCP endpoint** (every tool call) — this was the real fix for the "cancel scope" error. |
| **Foundry Project Runtime User** ✅ load-bearing | `142bfaed-a13f-4c2d-bed2-6db62c4a1009` | Foundry project | The **only** role whose `dataActions` include `Microsoft.CognitiveServices/accounts/AIServices/responses/*` — the exact action a direct (non-`agent_reference`) Responses API call hits. Everything below was tried first and kept as belt-and-suspenders, not proven necessary. |
| Foundry Agent Consumer | `eed3b665-ab3a-47b6-8f48-c9382fb1dad6` | Foundry project | Scoped to `agent_reference` calls to a *published* Agent — not what this code path uses, tried first. |
| Cognitive Services OpenAI User | — | Foundry project | Generic model-invocation role, tried second, insufficient alone. |
| Cognitive Services User | `a97b65f3-24c7-4388-baec-2e87135dc908` | Foundry project | Generic model-invocation role, tried third, insufficient alone. |

> **Cleanup opportunity**: if a future pass reconfirms `Foundry Project
> Runtime User` alone is sufficient for the Responses API path, the last 3
> rows can be dropped from `rbac.bicep` to keep the RBAC model minimal.

## 3. Fabric — not ARM/Bicep-manageable, granted via `scripts/grant_agent_identity_access.py`

Two independent surfaces, both required, neither expressible in Bicep (Fabric
workspace `roleAssignments` and Entra `oauth2PermissionGrants` are not ARM
resource types). Both are now applied by one idempotent script call instead
of hand-typed `az rest` commands — see `docs/DEPLOYMENT.md` §4b/§4d.

| Grant | Type | Target | Scope | Why |
|---|---|---|---|---|
| `DataAgent.Read.All`, `DataAgent.Execute.All` delegated scopes | Tenant-wide Entra admin consent (`POST /oauth2PermissionGrants`, since Agent Identities have no classic app-registration object) | Agent Identity (`clientId`) → Fabric/Power BI service principal (`resourceId`) | Tenant | Without this: `AADSTS65001: consent_required`, tool silently never added to the turn (no error surfaced). |
| Workspace **Contributor** role | Fabric workspace role assignment (`POST /v1/workspaces/{id}/roleAssignments`) | Agent-user object id | Fabric workspace (`NOCTopologyWorkspace`) | Tenant consent ≠ workspace RBAC — separate check. Without this: valid, correctly-scoped token still gets a Fabric-side authz failure. |
| Fabric workspace **Viewer** (or higher) | Fabric workspace role assignment | Each Teams user, or an AAD group they belong to (`TEAMS_USERS_GROUP_ID`) | Fabric workspace | Required specifically for `noc-incident-agent` (RTI) — Foundry `Foundry Agent Consumer` project RBAC is **not sufficient on its own**; Eventhouse enforces the calling user's own Fabric permission at query time. |

## 4. Microsoft Graph — not ARM/Bicep-manageable, granted via `scripts/grant_agent_identity_access.py`

| Grant | Type | Target | Scope | Why |
|---|---|---|---|---|
| `Sites.Read.All`, `Mail.Read`, `People.Read.All`, `OnlineMeetingTranscript.Read.All`, `Chat.Read`, `ChannelMessage.Read.All`, `ExternalItem.Read.All` | Tenant-wide Entra admin consent (`POST /oauth2PermissionGrants`) | Agent Identity → Microsoft Graph service principal | Tenant | Work IQ calls Graph server-side on the user's behalf; missing consent surfaces only as the generic "cancel scope" MCP error, never a clean 403 in this app's own traces. |
| `WorkIQAgent.Ask` delegated permission | Classic app-registration admin consent (`POST /oauth2PermissionGrants`) | Work IQ Entra app registration | Tenant | Required before the `WorkIQ` connection can be used; automated by `scripts/setup_workiq_entra_app.py`. |
| `Mail.Send` (optional, outbound notifications only) | Merged into the **same** `oauth2PermissionGrants` scope string as `Mail.Read` above (no new app registration) — script does a scope-union PATCH, gated by `GRANT_MAIL_SEND=true` | Agent Identity → Microsoft Graph service principal | Tenant | Enables `agent/notifications.py`'s persona-broadcast email path (`docs/OUTBOUND_NOTIFICATIONS.md`). Only valid **inside a turn** (needs a `TurnContext` for the OBO exchange) — a true zero-touch send with no prior conversation would need a materially different model: `Mail.Send` as an Application permission on a separate client-credential app registration, not yet built. |
| Azure AI Developer | Azure RBAC, applied idempotently by `scripts/setup_workiq_entra_app.py` | Work IQ app's service principal | Foundry project | Lets the OAuth app use the target Foundry project connection. |
| `McpServers.Mail.All`, `McpServers.Teams.All`, `McpServersMetadata.Read.All` (`customBlueprintPermissions` in `a365.config.json`) | Agent 365 Tools app permission | Agent Identity | Tenant | Applied by `a365 setup all`, not a separate manual step, but still not Bicep-managed. |

> **Irreducible manual step**: `scripts/grant_agent_identity_access.py` needs
> `AGENT_IDENTITY_OBJECT_ID`/`AGENT_USER_OBJECT_ID` in `.env`, which can only
> be obtained after `a365 setup all` + a first live Teams turn (no
> pre-deployment lookup path exists for these ids). It also must be run by a
> human holding Global Admin / Fabric admin rights — the capability to grant
> tenant-wide consent is not itself delegable via any script.

## 5. Cosmos DB — data-plane RBAC (separate from ARM/control-plane RBAC)

Declared in `gateway/infra/core/config/cosmos.bicep` via `readerPrincipals`/
`writerPrincipals`/`configWriterPrincipals` parameters. **Automated this
session**: `gateway/infra/main.bicep` now auto-`concat()`s the worker and
admin-ui managed identity principal ids into `writerPrincipals`/
`configWriterPrincipals` — no manual `az cosmosdb sql role assignment
create` needed on a fresh deploy. (It previously had to be run by hand
because `main.bicep` never wired the already-correct `cosmos.bicep` module's
params to anything — a wiring bug, not a missing capability. See
`docs/TROUBLESHOOTING.md` "Cosmos `403` after the identity fixed itself" for
the original discovery, and "RBAC automation" for the fix.)

| Principal | Cosmos built-in role | Role definition GUID | Containers | Notes |
|---|---|---|---|---|
| `config-sync-worker` managed identity | Cosmos DB Built-in Data **Contributor** | `00000000-0000-0000-0000-000000000002` | `config`, `team_subscription_map` | Not Reader — the worker calls `upsert_item` (bootstraps `global`/`pricing` docs, writes downgrades). |
| Admin UI managed identity | Cosmos DB Built-in Data **Contributor** | `00000000-0000-0000-0000-000000000002` | `config` | Writes consumer quota/throttling config from the admin UI. |
| (declared, not yet assigned to anyone) | Cosmos DB Built-in Data **Reader** | `00000000-0000-0000-0000-000000000001` | account-wide | For any future read-only consumer (e.g. a reporting identity). |

Auto-applied via Bicep now; the equivalent manual command (for reference or
an existing environment that can't redeploy immediately) is still:
`az cosmosdb sql role assignment create --account-name <cosmos> --resource-group <rg> --scope /dbs/gateway --principal-id <identity-object-id> --role-definition-id <role-guid>`.

## 6. Gateway container infra — declared in Bicep (`container-apps.bicep`, `run-ledger.bicep`, `redis.bicep`)

| Principal | Role | Role ID | Scope | Why |
|---|---|---|---|---|
| `config-sync-worker` managed identity | AcrPull | `7f951dda-4ed3-4680-a7ca-43fe172d538d` | Resource group (ACR) | Pull the worker's own container image on each job run. |
| `config-sync-worker` managed identity | API Management Service Contributor | `312a565d-c81f-4fd8-895a-4e21e48d571c` | Resource group (APIM) | Push named values / API config dynamically. **Broad by design** — a narrower custom role is a documented future hardening item, not yet done. |
| Admin UI managed identity | AcrPull | `7f951dda-4ed3-4680-a7ca-43fe172d538d` | Resource group (ACR) | Pull the admin UI's own container image. |
| Admin UI managed identity | Container Apps Jobs Operator | `b9a307c4-5aa3-4b52-ba60-2b17c136cd7b` | Resource group (Container Apps env) | Lets the admin UI trigger the `config-sync-worker` job on demand. |
| Admin UI managed identity | Log Analytics Reader | `73c42c96-874c-492b-b04d-ab87d138a893` | Resource group (Log Analytics workspace) | Reads operational logs for its own dashboard. |
| run-ledger managed identity | AcrPull | `7f951dda-4ed3-4680-a7ca-43fe172d538d` | Resource group (ACR) | Pull its own container image. |
| run-ledger managed identity | **Key Vault Secrets User** | `4633458b-17de-408a-b874-0445c86b69e6` | Key Vault | Reads the run-token signing secret (`@secure()` param → Key Vault). |
| run-ledger managed identity | Redis Enterprise access-policy assignment (`accessString`, Entra-token auth) | n/a (not an ARM role — a Redis-native access policy) | Managed Redis database | `accessKeysAuthentication: Disabled` — auth is Entra-token-only, object-id-as-username. |
| APIM system-assigned identity | **Key Vault Secrets User** | `4633458b-17de-408a-b874-0445c86b69e6` | Key Vault | Resolves the `run-token-signing-key` named value from Key Vault at runtime. |
| APIM system-assigned identity | Cognitive Services OpenAI User | `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd` | Azure OpenAI account | Managed-identity backend auth for the `openai-gateway` API's downgrade/rewrite policies. |
| APIM system-assigned identity | Cognitive Services User | `a97b65f3-24c7-4388-baec-2e87135dc908` | Foundry (AI Services) account | Managed-identity backend auth for the `foundry-gateway` API. |
| Cowork MCP UAMI | AcrPull | `7f951dda-4ed3-4680-a7ca-43fe172d538d` | Resource group (ACR) | Pull the MCP host image. |
| Cowork MCP UAMI | Azure AI Developer + Cognitive Services User | `64702f94-c441-49e6-a78b-ef80e0188fee`, `a97b65f3-24c7-4388-baec-2e87135dc908` | Existing Foundry project | Initialize and run the same NOC orchestrator. |
| Cowork MCP UAMI | Search Index Data Reader | `1407120a-92aa-4202-b7e9-c0e197c71c8f` | Existing Azure AI Search service | Preserve Foundry IQ access on the new host. |

The Cowork Entra delegated grants and UAMI federated credential are created by
`scripts/setup_cowork_mcp_entra.py`. The Microsoft Enterprise token-store
`OAuthPluginVault` auth configuration remains a Microsoft 365 operation; its
reference ID is created in Teams Developer Portal and supplied only when
packaging. See [`COWORK_MCP.md`](COWORK_MCP.md).

## 7. Operational/teardown roles (not app runtime, but used this session)

| Principal | Role | Scope | Why |
|---|---|---|---|
| Human operator's own `az`/`azd` login | Contributor (or a dedicated automation SP, see `docs/DEPLOYMENT.md` §0) | Subscription/RG | `azd up`, `az group delete`, Fabric capacity resume (`az resource invoke-action --action resume`), `create_foundry_agents.py`/`create_eventhouse.py`/etc. — all the provisioning/teardown scripts run as this identity, via `AzureCliCredential`/`AzureDeveloperCliCredential`. |
| Human operator's Fabric account | Fabric workspace admin/Contributor + tenant Fabric admin | Fabric tenant/capacity | Required to run `create_fabric_ontology.py`, `create_eventhouse.py`, and `delete_fabric_workspace.py` — Fabric workspace creation/deletion needs Fabric-side admin rights independent of any Azure RBAC role. |

## What's still genuinely manual after this session's automation pass

Every ARM-manageable gap found in this chart (§1, §5) is now Bicep-declared.
The only remaining manual work is inherently non-ARM:

1. Run `scripts/grant_agent_identity_access.py` once per environment, after
   `a365 setup all` + a first live Teams turn (needed to learn
   `AGENT_IDENTITY_OBJECT_ID`/`AGENT_USER_OBJECT_ID` — see §3/§4). This
   replaces what used to be 4 separate hand-typed commands.
2. The human running that script must already hold Global Admin / Fabric
   admin rights — granting tenant-wide consent is not itself delegable via
   any script or Bicep template.
3. `WorkIQAgent.Ask` admin consent on the Work IQ app registration (§4) —
   a one-time, different-principal grant, not folded into the script.
4. Create the Cowork Microsoft Entra SSO registration in Teams Developer
   Portal and copy its auth-config reference ID into the package command.

## Key lesson embedded in this chart

**Four separate authorization planes exist in this system, and none of them
substitute for each other**: (1) Azure ARM/control-plane RBAC, (2) Foundry
project-scope data-plane RBAC, (3) Fabric workspace-level RBAC, (4) Entra
tenant-wide delegated-permission consent. Every "it's not working" bug this
session traced back to assuming one of these covered a gap actually owned by
another — see `docs/TROUBLESHOOTING.md` for the full incident-by-incident
detail behind each row above.
