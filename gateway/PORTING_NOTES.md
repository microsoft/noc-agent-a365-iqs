# TokenOps gateway Layer 1 Bicep port

Ported from `C:\Flutter\apim-foundry-governance` into `gateway/infra` as a standalone subscription-scope Bicep stack.

## What ported directly

- Root orchestration pattern: subscription-scope resource-group creation, deterministic `resourceToken`, teardown tags, and resource-per-module layout.
- Network layout: VNet, APIM subnet, private-endpoint subnet, Container Apps delegated subnet, APIM NSG, and the private DNS zones used by Key Vault, Cosmos DB, OpenAI, Cognitive Services, and AI Services.
- Core services: APIM, Azure OpenAI, Azure AI Services / Foundry, Cosmos DB SQL API, Key Vault, ACR, Log Analytics, Application Insights, budgets, user-assigned identities, and Container Apps host resources.
- APIM policy logic: consumer bundle lookup, allowed-model enforcement, named-value-driven rate tiers, managed-identity backend auth, and cross-backend downgrade rewrites between Azure OpenAI and Foundry.

## Adaptations in this port

- Terraform `map(object(...))` inputs became Bicep arrays-of-objects where that keeps loops and parameter files simpler.
- Terraform `templatefile()` policy rendering became `loadTextContent()` + `replace()` in `core/apim/apim.bicep` because Bicep has no native text template engine.
- Terraform `count` / `for_each` became Bicep `if (...)` and resource loops.
- APIM defaults to **StandardV2** for public Cowork ingress plus outbound VNet integration to private backends. Use PremiumV2 only when the gateway itself must be private-inbound; that mode is not reachable by Cowork without another public front door.
- The jumpbox module was intentionally **not** ported. Cosmos seed data should be written later with either:
  - a one-off `az cosmosdb sql` CLI call, or
  - a one-off Container Apps Job run using the worker image.
  This repo already leans on azd + containerized jobs, so keeping jumpbox VM infrastructure out of the Bicep keeps Layer 1 smaller and closer to existing repo patterns.
- Monitoring resources were re-authored in one module instead of reusing the existing root-level `infra/core/monitor/*` modules because this stack also needs Cost Management budget wiring plus Log Analytics keys for the Container Apps environment.

## Known semantic gaps / follow-up checks

- `az bicep build` validates syntax and type-shape, not live regional capability. APIM VNet injection, Foundry model availability, and Container Apps environment behavior still need a deployment-time review.
- APIM runtime-owned named values (`allowed-models`, quotas, consumer bundle) are seeded here only with safe defaults. The app-layer config-sync worker will own updates later.
- The broad built-in roles from Terraform were preserved with the same rationale comments. Custom least-privilege roles are still a hardening follow-up, not part of Layer 1.

## APIM PremiumV2 networking note

- PremiumV2 VNet injection requires a **delegated** APIM subnet (`Microsoft.Web/hostingEnvironments`), a minimum **/27** subnet size (this stack already uses **/24**), and NSG outbound **443** rules to the `Storage` and `AzureKeyVault` service tags.
- The PremiumV2 Learn docs describe injection as the **private inbound + private outbound** path. If a future environment needs public inbound access again, use v2 **integration** instead of trying to recreate the old classic External/Developer behavior.

## How to deploy later

- Raw ARM/Bicep flow:
  - `az deployment sub create --location <region> --template-file gateway\infra\main.bicep --parameters @gateway\infra\main.parameters.json`
- Natural azd fit for a later step:
  - create a second azd environment (for example `noc-iq-gateway`)
  - point that environment's infra path at `gateway/infra`
  - keep it separate from the existing app-host stack instead of reusing `infra/main.bicep`

## Deferred to the next phase

- app code port for the config-sync worker image
- app code port for the admin UI / BFF image
- deploy-time review, secrets/bootstrap, and live Azure validation

## Layer 2 follow-up wiring: run-ledger

### APIM policy splice points

- Completed: spliced the run-ledger inbound / outbound / on-error blocks into both `gateway\infra\policies\openai-pipeline.xml` and `gateway\infra\policies\foundry-pipeline.xml` at the exact `<base />`-anchored insertion points documented above.
- Local validation: both pipeline files now parse as XML via PowerShell `[xml](Get-Content ... -Raw)`.

### APIM placeholder rendering

- Completed in `gateway\infra\core\apim\apim.bicep`.
- Added rendering for:
  - `{{RUN_LEDGER_BASE_URL}}`
  - `{{RUN_TOKEN_SIGNING_KEY_NAMED_VALUE}}`
  - `{{RUN_TOKEN_QUOTA}}`
  - `{{RUN_TOKEN_QUOTA_PERIOD}}`
  - `{{RUN_TOKENS_PER_MINUTE}}`
- Added APIM named values for:
  - `run-ledger-base-url`
  - `run-token-signing-key` (Key Vault-backed)
- Added a Key Vault RBAC assignment so APIM's system-assigned identity can resolve the signing-key named value from Key Vault.

### Container Apps + main wiring

- Completed:
  - added `gateway\infra\core\host\redis.bicep`
  - wired `module redis` and `module runLedger` into `gateway\infra\main.bicep`
  - added a dedicated run-ledger user-assigned identity in `gateway\infra\core\identity\identities.bicep`
  - added the Redis private DNS zone in `gateway\infra\core\network\vnet.bicep`
- `main.bicep` now passes:
  - the shared Container Apps environment id
  - ACR id + login server
  - run-ledger identity id / principal id / client id
  - `keyVaultUrl`
  - `keyVaultResourceId`
  - `runTokenSigningSecretName`
  - Redis hostname + port
  - `modelPricesJson`
  - run-ledger FQDN into APIM policy rendering

### Infra decisions completed in this pass

- **Redis provisioning is now included.** The new `gateway\infra\core\host\redis.bicep` deploys Azure Managed Redis (`Microsoft.Cache/redisEnterprise`) with `publicNetworkAccess: 'Disabled'`, a private endpoint, and `accessKeysAuthentication: 'Disabled'` on the default database.
- **Redis auth model:** the run-ledger identity is granted a Redis access-policy assignment for Microsoft Entra token auth; the service continues to authenticate with object-id-as-username plus `https://redis.azure.com/.default` tokens.
- **Key Vault secret handling:** the run token signing secret is modeled as a Key Vault secret fed from a required `@secure()` deployment parameter, and the run-ledger identity receives `Key Vault Secrets User` RBAC on the vault.

### Still deferred after this pass

- No Azure deployment or live smoke test has been run yet; this pass only validates Bicep compilation and local XML structure.
- `MODEL_PRICES_JSON` remains env-driven. Wiring the ledger directly to the existing Cosmos pricing document is still a small follow-up if we want to remove that duplication.

## Live deployment (post-merge with `feat/foundry-multi-agent`)

Deployed end-to-end to `rg-<env-name>`. Real bugs found and fixed along the way (all
committed on `feat/tokenops-gateway`): a `set-body` JSON-escaping bug, an invalid
`context.Principal` reference (replaced with `output-token-variable-name` + `Jwt` claims
lookup), `rewrite-uri`/`set-backend-service` used inside `<on-error>` (not allowed there),
a base64 format mismatch between APIM's `validate-jwt` `<key>` (always base64-decodes) and
the Python `RunTokenSigner` (fixed by base64-encoding the KV secret and decoding it in
`run_ledger/main.py`), single-statement `if (...) return` inside `@{...}` blocks (APIM's
Razor-like dialect requires braces), and an ambiguous `GetValueOrDefault` overload
(needs a concretely-typed default). See git log on `feat/tokenops-gateway` for the individual
fix commits.

**Known APIM product limitation**: `azure-openai-token-limit` and `quota-by-key` cannot
coexist in the same policy document on this APIM instance/SKU — combining them causes an
unhandled 500 `InternalServerError` on policy PUT, independent of parameters, counter-key
value, or element order (confirmed by isolated REST-API binary-search testing). Resolution:
`quota-by-key` was dropped from `openai-pipeline.xml`; per-consumer governance is still
covered by `azure-openai-token-limit` (TPM) plus the run ledger's run-scoped
`llm-token-limit` and precall/postcall budget decisioning, which made the daily call-count
quota redundant anyway.

**New debugging method for stuck/opaque APIM policy deployments**: `az apim api policy show`
and related `az apim` CLI-extension subcommands are broken in this environment
(`No module named 'rpds.rpds'`), and `az rest` itself crashes on any response containing a
UTF-8 BOM (a Windows colorama/console-encoding bug also seen with `az acr build` log output).
Use `az account get-access-token` to mint a bearer token, then call the ARM/APIM REST API
directly with PowerShell's `Invoke-RestMethod` (GET and PUT) — this fully bypasses both CLI
bugs and lets you binary-search a policy document section-by-section against the live
resource, far faster than round-tripping through full `az deployment sub create` cycles.

### `b-toolbox-route` / `b-tool-caps` — spiked, not implemented

The plan's own risk list (#7) called for spiking `b-toolbox-route` before committing to it,
since it may involve streaming/SSE. That spike happened this session, by reading
`agent/agent.py`'s current (post-`feat/foundry-multi-agent`-merge) design and the four IQ
connection definitions (`infra/core/ai/ai-project.bicep`, `scripts/create_fabric_iq_connection.py`,
`scripts/create_workiq_toolbox.py`). Finding, and why it changes the call:

- Tool execution now happens **server-side inside Foundry Agent Service**, not in this app's
  process. Each specialist Prompt Agent holds its own native `MCPTool(project_connection_id=...)`
  pointed straight at a project Connection — there is no client-side MCP call left in
  `agent.py` to route through APIM, and no toolbox layer at all (see the module docstring in
  `agent/agent.py`).
- The only remaining lever is a Connection's own `target` URL, which *could* be repointed at
  an APIM-fronted proxy. But the four Connections use **three different, backend-specific
  auth types** (`ProjectManagedIdentity` for Foundry IQ against Azure AI Search's own MCP
  endpoint, `CustomKeys` for the external Web IQ vendor, `UserEntraToken` OBO passthrough for
  Fabric IQ and Work IQ), and Agent Service mints/attaches those bearer tokens itself, scoped
  to the **real** backend's audience, before the request ever reaches a proxy. Rehosting those
  calls behind an APIM hostname risks breaking audience/issuer validation on the two
  `UserEntraToken` surfaces and on Azure AI Search's own MI-token check — i.e. it can silently
  break 3 of 4 already-working IQ tools on a demo that is currently deployed and functioning,
  for governance value (call-count/audit visibility) that the LLM-side run ledger and
  `azure-openai-token-limit`/run-scoped `llm-token-limit` already deliver for the cost
  dimension that actually matters (token spend).
- `b-tool-caps` (oversized tool-output capping, malformed-call halting) depends on
  `b-toolbox-route` existing first and adds MCP JSON-RPC/SSE body parsing on top, which is
  its own separate risk the plan flagged.

**Decision**: leave both undone. If per-tool spend/audit visibility becomes a real
requirement later, the safer path is instrumenting `agent.py`'s specialist call sites
(before/after each `get_openai_client(agent_name=...)` call) to log to the run ledger
directly — no proxy, no audience-validation risk — rather than intercepting Agent Service's
own outbound Connection traffic.

## Responses API fix + final mixed-routing decision (this pass)

Two more real bugs found while trying to actually wire App Service through APIM for smoke
testing, plus a scope decision that follows directly from the `b-toolbox-route` finding
above:

- **`openai-pipeline.xml`/`foundry-pipeline.xml` were written for Chat Completions, but
  `agent.py` calls the Responses API.** The precall token-estimate (`b["messages"]`), the
  postcall usage read (`u["prompt_tokens"]`/`u["completion_tokens"]`), and the mutate block's
  message-injection (`((JArray)b["messages"]).Add(...)`) all assumed a `messages` array and
  Chat-Completions usage field names that don't exist on a `responses.create()` body/response.
  Fixed with defensive dual-shape handling (`b["input"] ?? b["messages"]`,
  `u["input_tokens"] ?? u["prompt_tokens"]`, etc.) so either shape works.
- **The run ledger's Container App has internal-only ingress** (confirmed via
  `az containerapp show`) — App Service is not VNet-integrated, so it cannot reach it
  directly. Added a thin pass-through API (`run-ledger-gateway`, path `/ledger`) to
  `apim.bicep`: no body inspection, just `set-backend-service` to the internal ledger FQDN,
  since APIM already sits in the VNet.
- **Final decision, consistent with the `b-toolbox-route` spike above**: this app's own
  model/agent traffic (orchestrator + all 4 specialists) keeps calling Foundry **directly**,
  never through `openai-gateway`/`foundry-gateway` — those two blockers (OAuth passthrough
  substitution, persisted-agent body shape mismatch) apply to this app's SDK-level calls just
  as much as they applied to the toolbox MCP calls. Instead, `agent.py` calls the run ledger's
  `/v1/precall` and `/v1/postcall` directly (via the new `/ledger` APIM pass-through), using
  the real `response.usage`/`response.usage_details` each call already returns. This answers
  "how is fabric_iq/work_iq governed if it doesn't cross APIM?" uniformly for all 5 model
  calls: the OAuth-identity-passthrough question and the token-governance-reporting question
  are orthogonal, and only the latter needed APIM (as a private-network relay, not a policy
  enforcement point) at all. See `docs/SEQUENCE.md` §3 for the full sequence diagram.
- `openai-gateway`/`foundry-gateway` stay deployed, fixed, and available for any *other*
  direct AOAI/Foundry caller that sends REST-shaped, model-in-body traffic — just not in this
  app's own hot path.

