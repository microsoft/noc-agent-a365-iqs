# Deployment Guide

Reproducible, exact steps to deploy `noc-agent-a365` into a **brand-new**
resource group in a target Azure subscription, wire all four IQ surfaces, and
publish to Teams / M365 Copilot via Agent 365 (A365). Every resource is new —
nothing is reused from any other deployment.

Prerequisites: `az` (Azure CLI), `azd` (Azure Developer CLI), `a365` (Agent
365 CLI), Python 3.11+, and a POSIX-ish shell for the `bash` code blocks below
(Git Bash on Windows, or a native shell on macOS/Linux). There are no `.sh`
helper scripts in this repo — every step below is either a CLI command or a
`python scripts/*.py` script; the `bash` code fences are just for copy-paste
convenience.

The optional Copilot Cowork MCP channel has a separate, non-deploying runbook
in [`COWORK_MCP.md`](COWORK_MCP.md). It reuses this deployment's Foundry
project, specialists, `NocAgent`, run ledger, and Search corpus.

> **Optional: automation service principal.** Every step below assumes an
> interactive `az login`/`azd auth login` session. If you are instead driving
> this guide from an unattended script/pipeline where an interactive browser
> login isn't available, create your own service principal
> (`az ad sp create-for-rbac --name "<your-name>-automation" --role Contributor
> --scopes /subscriptions/<sub-id>/resourceGroups/rg-<AZURE_ENV_NAME>`,
> created **after** step 2 provisions the resource group) and authenticate
> `az`/`azd` with its credentials instead. This is not part of the deployment
> architecture itself — delete the SP at teardown (§11.4) if you created one.

## 0. Configuration

```bash
export AZURE_SUBSCRIPTION_ID="<your-subscription-id>"      # subscription id or name, e.g. from `az account list -o table`
export AZURE_LOCATION="eastus2"
export AZURE_ENV_NAME="noc-iq-demo"
export WEB_IQ_API_KEY="<your Web IQ x-apikey value>"        # never commit this
```

## 1. Sign in

```bash
az login --tenant "<your-tenant-id>"
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
azd auth login
```

## 2. Provision infrastructure (`azd up`)

```bash
cd noc-agent-a365
azd env new "$AZURE_ENV_NAME"
azd env set AZURE_LOCATION "$AZURE_LOCATION"
azd env set AZURE_SUBSCRIPTION_ID "$AZURE_SUBSCRIPTION_ID"
azd env set webIqApiKey "$WEB_IQ_API_KEY"
azd up
```

This creates, in a new resource group (`rg-<AZURE_ENV_NAME>` by default):

- AI Foundry account + project with `gpt-5.4` + `text-embedding-3-small`
  deployments
- Azure AI Search, Storage account, Application Insights + Log Analytics
- Fabric capacity (F2 SKU — billable, see `README.md` cost note)
- Linux App Service (B1) for the agent host, with project-scope RBAC on its
  managed identity
- A Web IQ `CustomKeys` connection (only if `webIqApiKey` was set)

Capture the outputs — `azd env get-values` prints them all, including
`AZURE_AI_PROJECT_ENDPOINT`, the Search endpoint, and the agent host name.

## 2b. Export outputs to a root `.env` file (required before steps 3-6)

**There is no `azd postprovision` hook in this repo** — `azd up` alone does
**not** write any `.env` file. `scripts/create_fabric_ontology.py`,
`create_fabric_data_agent.py`, and `create_workiq_toolbox.py` all
`load_dotenv()` a root-level `.env`; `create_foundry_iq_kb.py` reads straight
from `os.environ` and needs the values actually exported into the shell, not
just present in a file. Do both in one step, from the repo root:

```bash
azd env get-values > .env
set -a; source .env; set +a
```

Re-run this after any `azd up`/`azd provision` that changes infra outputs
(e.g. if you re-run `azd up` in a later session), and before running any
script in steps 3-6 in a fresh shell.

## 3. Foundry IQ — build the knowledge base

```bash
cd scripts
pip install -r requirements.txt
python create_foundry_iq_kb.py
```

Populates `noc-knowledge-kb` from `data/{runbooks,tickets,equipment_specs,infra_specs}`
and prints the KB MCP endpoint (`{search}/knowledgebases/noc-knowledge-kb/mcp?api-version=...`).

## 4. Fabric IQ — ontology + Data Agent

Requires the signed-in account to have a Fabric/Power BI license and be an
admin (or Contributor) on the target capacity.

```bash
cd scripts
python create_fabric_ontology.py
python create_fabric_data_agent.py
```

The first script creates a Fabric workspace on the F2 capacity, a lakehouse,
loads every `data/ontology_entities/*.csv` as a Delta table, and builds the
`NOCNetworkOntology` ontology. The second publishes a Fabric Data Agent over
that ontology and prints `FABRIC_DATA_AGENT_MCP_URL`.

> **Note — Fabric workspace lifetime.** The workspace is a Fabric-tenant
> object, not an ARM resource in the resource group. It must be deleted
> separately at teardown (§7) or it will orphan after `az group delete`.

### 4a. Manually build the graph canvas (required — not automated)

`create_fabric_ontology.py` only creates the `Ontology` item's schema and
data-source **bindings**. Creating the `Ontology` item auto-provisions a
companion `GraphModel` item in the same workspace (visible in the workspace's
item list as `<FABRIC_ONTOLOGY_NAME>_graph_<guid>`, distinct from the
`Ontology` item itself) — this is the item Fabric IQ/the Data Agent actually
queries against, and it is **not** populated by the script, by loading data
sources, or by running Refresh. Its node/edge types must be created
**manually, once**, on the Fabric portal's graph canvas, or every Fabric IQ
query will fail with `GraphNotRefreshable` /
`"Graph doesn't have valid content and cannot be refreshed."` This was the
deepest root cause found during this deployment's troubleshooting (see
`docs/TROUBLESHOOTING.md` "CONFIRMED, CONCRETE GAP" / "RESOLVED" entries).

1. Fabric portal → the `NOCNetworkOntology` item → open its **graph/canvas**
   view (not the query view).
2. **Add node** x8, using the "Reference: manual node/edge build table" at
   the top of `docs/TROUBLESHOOTING.md` for the exact source table/key/
   property mapping for each of `CoreRouter, TransportLink, PhysicalConduit,
   AmplifierSite, Service, SLAPolicy, MPLSPath, Advisory`.
3. **Save**, then **Add edge** x6 (`ORIGINATES_AT, TERMINATES_AT, RIDES_ON,
   AMPLIFIES, COVERS, AFFECTS`), using the same table's Origin/Target key
   columns — verified against real Delta table schemas, not just the raw
   ontology JSON (see `docs/TROUBLESHOOTING.md` for why that distinction
   matters).
4. **Save**, then trigger **Refresh** from the portal (the Job Scheduler
   REST API rejects ad-hoc `Refresh` triggers for this item type — use the
   portal button). Confirm the job reaches `status: Completed`.

> If the target capacity has auto-paused (Fabric capacities pause after a
> period of inactivity), Refresh fails with `GraphNotRefreshable` even with a
> correctly-built graph. Check `az resource show --ids <capacity resource
> id> --query properties.state` and resume with `az resource invoke-action
> --action resume --ids <capacity resource id>` first.

### 4b. Grant the agent identity access to Fabric (required, one-time per environment)

`a365 setup all` (step 9) creates an Entra **Agent Identity** with an
auto-provisioned agent-user child identity, but that identity starts with
**zero access to Fabric** — every Fabric IQ call will fail (`AADSTS65001:
consent_required`, then, once consent is fixed, a Fabric workspace-RBAC
403) until this is granted. None of this is ARM/Bicep-manageable (Entra
tenant-wide consent grants and Fabric workspace role assignments are not ARM
resource types), so it's automated instead as a single idempotent script,
`scripts/grant_agent_identity_access.py` (see §4d/§6a/§6b — it also covers
those). Set the two required env vars once the Agent Identity exists (after
step 9), using an account with Global Admin / Fabric admin rights:

```bash
# AGENT_IDENTITY_OBJECT_ID / AGENT_USER_OBJECT_ID -- see docs/TROUBLESHOOTING.md's
# "Auth-type mistakes" section for how to find them (visible as `agentic_user_id`
# in Application Insights `traces` once a user has messaged the agent once).
echo "AGENT_IDENTITY_OBJECT_ID=<agent identity object id>" >> .env
echo "AGENT_USER_OBJECT_ID=<agent-user object id>" >> .env
python scripts/grant_agent_identity_access.py
```

This grants the Fabric tenant admin-consent (`DataAgent.Read.All`/
`DataAgent.Execute.All`) and adds the agent-user identity as a `Contributor`
on the Fabric workspace — safely re-runnable any time (it checks existing
grants/role assignments before writing).

### 4c. Create the Fabric IQ + Work IQ Foundry project connections (automated)

**Neither `create_fabric_ontology.py` nor `create_fabric_data_agent.py`
creates a Foundry project connection.** They only touch Fabric-side
resources (workspace, lakehouse, ontology, Data Agent). `agent.py` resolves
Fabric IQ via `self._project_client.connections.get("fabric-iq-connection")`
and Work IQ via `.get("WorkIQ")` -- if either doesn't exist, it's silently
caught and skipped (that tool just never appears in the toolbox, no error at
startup; you'll see `Could not resolve connection '<name>' (<key> tool
disabled)` in the logs). Foundry IQ's `kb-mcp-connection` and Web IQ's
`web-iq-connection` are already provisioned declaratively by
`infra/core/ai/ai-project.bicep` at `azd provision` time -- only these two
have no Bicep resource type and must be created imperatively, every time
against a fresh RG (and again if the Fabric Data Agent is ever recreated,
since fabric-iq-connection's `target` must track its MCP URL).

Run the single combined script instead of the raw ARM `curl` call this used
to require:

```bash
cd scripts
python refresh_iq_connections.py
```

This runs, in order: `create_fabric_data_agent.py` (Fabric Data Agent, needed
first since its MCP URL is the Fabric IQ connection's `target`),
`create_fabric_iq_connection.py` (the `fabric-iq-connection` project
connection, `authType: UserEntraToken`, `audience: https://api.fabric.microsoft.com`
-- must match the scope consented in section 4b step 1), and
`create_workiq_toolbox.py` (the `WorkIQ` project connection, also
`authType: UserEntraToken`). All three are idempotent -- safe to re-run any
time a connection is missing or stale; each step no-ops if already correct.
(The account-rp connections API is intermittently flaky and can return a
bare `500`; both connection scripts retry automatically.)

After running it, **restart the app** (`az webapp restart`) so `agent.py`'s
`initialize()` re-reads the connections and rebuilds the toolbox with all 4
tools -- it only resolves connections once, at process startup.

### 4d. Grant Teams users Fabric workspace/Eventhouse read access (required for `noc-incident-agent`)

`noc-incident-agent` uses the same per-user OBO identity passthrough pattern
as the other Fabric-backed specialists (`needs_user_identity=True` in
`agent/agent.py`), so the calling Teams user's own Fabric permissions are
what Eventhouse enforces at query time. The Foundry project role assignments
above (for example `Foundry Agent Consumer`) are **not** enough on their own:
after deployment, a Fabric admin must also grant each Teams user -- or, more
typically, an AAD group they belong to -- at least **Viewer** (or the
equivalent read role your tenant uses) on the Fabric workspace that contains
the Eventhouse/item the agent queries.

Fabric workspace RBAC is not an ARM resource type, so it's still not
Bicep-managed, but it's now scripted (same script as §4b): set
`TEAMS_USERS_GROUP_ID` to the AAD group your Teams users belong to (the same
group id passed as `teamsUsersPrincipalId` to `infra/core/ai/rbac.bicep`) and
re-run `python scripts/grant_agent_identity_access.py` -- it grants that group
`Viewer` on the Fabric workspace, skipping if already granted. If
`TEAMS_USERS_GROUP_ID` is unset, the script skips this step and you must grant
it manually in the Fabric portal (**Workspace -> Manage access**) instead.

### 4e. Fabric RTI — Eventhouse, incident telemetry, and the RTI connection (required for `noc-incident-agent`)

`noc-incident-agent` is the 5th specialist (real-time incident evidence: alert
timelines, per-sensor optical readings, suppressed alerts). It needs its own
Eventhouse (KQL database), seeded telemetry, and its own project connection.
None of this is Bicep-managed -- run these against a fresh environment:

```bash
cd scripts
python generate_incident_telemetry.py   # writes data/telemetry/*.csv (already committed; re-run only if tickets/sensors change)
python create_eventhouse.py             # creates NOCIncidentEventhouse + KQL DB in the same Fabric workspace as §4, ingests the 3 CSVs, writes FABRIC_EVENTHOUSE_ID/FABRIC_KQL_DB_ID/FABRIC_RTI_MCP_URL to .env
python create_rti_connection.py         # creates the fabric-rti-connection project connection (authType=UserEntraToken), target = FABRIC_RTI_MCP_URL
```

`create_eventhouse.py` is idempotent (create-or-get eventhouse/DB, then
`.clear table` + re-ingest each of the 3 demo tables, so reruns don't
duplicate rows). Grant Teams users Fabric access per §4d before expecting the
agent to return data for a real user.

> **Gotcha — Fabric capacity can auto-pause.** If RTI queries fail with an
> opaque MCP `HTTP 404 (Not Found)` (not a KQL/semantic error), the *first*
> thing to check is whether the backing Fabric capacity has been paused
> (billing/inactivity):
> ```bash
> az resource show --resource-type Microsoft.Fabric/capacities --name <capacity-name> -g <rg> --query properties.state -o tsv
> az resource invoke-action --action resume --resource-type Microsoft.Fabric/capacities --name <capacity-name> -g <rg>
> ```
> Poll `properties.state` until `Active` (~60-90s) before retrying. See
> `docs/TROUBLESHOOTING.md` for the full root-cause writeup (this 404 has no
> obvious connection to capacity state in the error text itself).

### 4f. Create the 5 Foundry Prompt Agent specialists (required, run after all connections above exist)

```bash
cd scripts
python create_foundry_agents.py
```

Creates/updates `noc-knowledge-agent`, `noc-topology-agent`,
`noc-threatintel-agent`, `noc-comms-agent`, and `noc-incident-agent` as
persisted Foundry Prompt Agents, each with exactly one MCP tool bound to the
connection created above (`kb-mcp-connection`, `fabric-iq-connection`,
`web-iq-connection`, `WorkIQ`, `fabric-rti-connection` respectively). It's
idempotent: it diffs each agent's live latest-version definition
(instructions/model/tool) against what it would create and skips any agent
that's already up to date, only publishing a new version where something
actually changed. `agent/agent.py`'s `SPECIALIST_AGENTS` map resolves these
five by name at startup -- run this **before** step 8 on a fresh environment,
or the orchestrator's tool calls will fail with "agent not found".

## 5. Web IQ

Provisioned automatically by Bicep if `webIqApiKey` was set in step 2 -- no
action needed. Otherwise, add it manually in the Foundry portal:
**Tools -> + Add tool -> Custom MCP**, auth type `CustomKeys`, header
`x-apikey`, target `https://api.microsoft.ai/v3/mcp`.

## 6. Work IQ

Work IQ has no dedicated SDK connection class or Bicep resource type. An
earlier attempt used `authType: UserEntraToken` (OBO identity passthrough,
no client secret required) on the theory that Foundry could mint a
Work IQ-scoped token directly from the caller's own Entra token -- **this
does not work**: it fails on every call with `AADSTS500016: Application
'fdcc1f02-fc51-4226-8753-f668596af7f7' is not supported as a resource
application to execute the flow`. Work IQ's resource app does not support
being the target of a bare OBO flow.

**Resolved 2026-08-21**: Work IQ requires a dedicated **OAuth2** connection
per Microsoft's own quickstart
(<https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/mcp/quickstart/foundry>).
This is now automated by `scripts/create_workiq_toolbox.py` (called from
`refresh_iq_connections.py`), which `PUT`s the Foundry project connection
`WorkIQ` against ARM with `authType: OAuth2`, target
`https://workiq.svc.cloud.microsoft/mcp`, Authorization/Token/Refresh URLs
`https://login.microsoftonline.com/{tenant-id}/oauth2/v2.0/{authorize,token}`,
and scopes `api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask,offline_access`.

Unlike the other three IQ connections, this one needs a **dedicated Entra
app registration with a client secret**. Provision the entire admin-side chain
idempotently with:

```bash
python scripts/setup_workiq_entra_app.py
```

The script creates or reuses `noc-agent-workiq`, grants the delegated
`WorkIQAgent.Ask` permission tenant-wide, creates its service principal and
one-year credential when needed, assigns `Azure AI Developer` at Foundry
project scope, creates/updates the `WorkIQ` OAuth2 project connection, and
registers Foundry's generated OAuth redirect URI back on the app. The client
ID and secret are written only to the gitignored repo `.env`; the secret is
also held by the encrypted Foundry connection. Running
`scripts/create_workiq_toolbox.py` directly remains supported when those two
environment values are already supplied.

That connection is the *only* Work IQ resource `agent/agent.py` needs: it
resolves Work IQ purely by looking up the `WorkIQ` project connection by name
(`WORK_IQ_CONNECTION_NAME`, default `WorkIQ`) and wraps it into its own
`noc-iq-toolbox` alongside the other three IQ connections (see
`docs/ARCHITECTURE.md`) — there is no separate, dedicated Work IQ toolbox to
create or reference.

The account-rp preview connections API is intermittently flaky and returns a
bare `500 InternalServerError`; the script retries with a short backoff and is
otherwise idempotent (re-running skips the connection PUT if it already has
the correct config).

`customBlueprintPermissions` in `a365.config.json` still needs the
`ea9ffc3e-8a23-4a7d-836d-234d7c7565c1` (Agent 365 Tools) app's
`McpServers.Mail.All`, `McpServers.Teams.All`, `McpServersMetadata.Read.All`
scopes — applied by `a365 setup all` (step 8), separate from the Foundry
connection above.

> **Still not fully automatable**: the OAuth2 connection above satisfies
> every step Microsoft's quickstart marks as admin-side setup, but Work IQ
> connections use **delegated** Entra auth -- the **first call from each
> signed-in user** still requires a one-time interactive OAuth consent/
> sign-in prompt in a browser. This does not fit cleanly into a headless
> Teams/A365 turn; `agent/agent.py` has a `_WORK_IQ_CONSENT_PREFIX` constant
> intended to relay a consent link through Teams chat for exactly this case,
> but end-to-end behavior in Teams has not yet been verified this session.
> See `docs/PRIMER_MCP_CANCEL_SCOPE_BUG.md` for the full investigation.


### 6a. Grant Work IQ's Graph delegated-permission consent (required, one-time)

Beyond the `authType: UserEntraToken` connection above, Work IQ calls
**Microsoft Graph** on the caller's behalf internally, and the Agent Identity
needs tenant admin consent for the 7 Graph delegated scopes Work IQ requires —
without this, every Work IQ call fails with a generic MCP `"Cancelled via
cancel scope"` error (the real `AADSTS65001` never surfaces in this app's own
traces, since the failure happens server-side inside Work IQ). This is the
same `scripts/grant_agent_identity_access.py` script as §4b — if you already
ran it there, this is already done:

```bash
python scripts/grant_agent_identity_access.py
```

### 6b. (Optional) Grant `Mail.Send` for outbound persona notifications

**Only needed if you use `agent/notifications.py`'s outbound broadcast**
(`POST /api/incidents/notify` — see docs/OUTBOUND_NOTIFICATIONS.md). This is
a separate, additive consent — set `GRANT_MAIL_SEND=true` in `.env` and
re-run the same script; it merges `Mail.Send` into the existing grant's
scope instead of clobbering the 7 scopes already granted in §6a:

```bash
echo "GRANT_MAIL_SEND=true" >> .env
python scripts/grant_agent_identity_access.py
```

The agentic user identity (`nocagent@<tenant>`) has a real mailbox, so
delegated `Mail.Send` works the same way delegated `Mail.Read` already does —
no separate app registration or client-credential grant is required for this
path. See docs/OUTBOUND_NOTIFICATIONS.md for the full permission-model
explanation and its limits.

Also set at least one `NOTIFY_<PERSONA>_EMAILS` app setting (personas with
no recipients are silently skipped, by design) — **the names are all
singular `PERSONA`**, e.g. `NOTIFY_PARTNER_EMAILS` not
`NOTIFY_PARTNERS_EMAILS` (a live typo here cost an entire test session
before it was caught — `az webapp config appsettings list ... | grep -i
notify` is the fastest way to spot it):

```bash
az webapp config appsettings set -g "rg-$AZURE_ENV_NAME" -n "<webAppName>" --settings \
  NOTIFY_EXECUTIVES_EMAILS="you@yourtenant.com" \
  NOTIFY_TECHNICAL_EMAILS="you@yourtenant.com" \
  NOTIFY_VENUE_EMAILS="you@yourtenant.com" \
  NOTIFY_PARTNER_EMAILS="you@yourtenant.com"
az webapp restart -g "rg-$AZURE_ENV_NAME" -n "<webAppName>"
```

## 7. App Service configuration — nothing extra to push

Steps 3-6 only create Foundry **project connections**
(`kb-mcp-connection`, `web-iq-connection`, `fabric-iq-connection`, `WorkIQ`)
and populate the Fabric ontology/Data Agent. `agent/agent.py` does **not**
read `FABRIC_DATA_AGENT_MCP_URL`, `AZURE_AI_SEARCH_KNOWLEDGE_BASE_NAME`,
`WEB_IQ_MCP_ENDPOINT`/`WEB_IQ_API_KEY`, or any Work IQ toolbox name as
environment variables — it resolves all four IQ surfaces at startup purely by
looking up the four **connection names** above through the Foundry project
client (`FOUNDRY_IQ_CONNECTION_NAME`, `WEB_IQ_CONNECTION_NAME`,
`FABRIC_IQ_CONNECTION_NAME`, `WORK_IQ_CONNECTION_NAME`, see
`agent/.env.template`), all of which already default to the exact names
created in steps 3-6. `infra/main.bicep` already sets every app setting the
agent actually needs (`FOUNDRY_PROJECT_ENDPOINT`,
`AZURE_AI_MODEL_DEPLOYMENT_NAME`, `AUTH_HANDLER_NAME`,
`AGENT_RUN_TIMEOUT_SECONDS`, etc.) at provision time in step 2 — there is
nothing left to push to the App Service after steps 3-6.

Only if you deliberately named any connection differently than the defaults
above do you need to override the corresponding `*_CONNECTION_NAME` app
setting:

```bash
az webapp config appsettings set -g "rg-$AZURE_ENV_NAME" -n "<webAppName>" --settings \
  FABRIC_IQ_CONNECTION_NAME="<your-custom-connection-name>"
```

## 8. Deploy the agent code

```bash
cd ..
azd deploy
```

Pushes `agent/` to the App Service created in step 2, running
`python start_with_generic_host.py`.

> **Managed identity detection.** `agent.py`'s `_get_service_credential()`
> uses `ManagedIdentityCredential` when the `WEBSITE_INSTANCE_ID` env var is
> present (always set by the App Service platform) and falls back to
> `AzureDeveloperCliCredential` otherwise (local dev, where `azd` is on PATH).
> If this check is ever changed to something not guaranteed to be set inside
> the container, every service-credential call fails at startup with
> `CredentialUnavailableError: Azure Developer CLI could not be found` — the
> container has no `azd` binary.

## 9. Publish to Teams / M365 Copilot (Agent 365)

```bash
cp a365.config.template.json a365.config.json   # gitignored — fill in real values
a365 setup all --aiteammate --m365 --agent-name <agent-name> \
  --messaging-endpoint "https://<webAppName>.azurewebsites.net/api/messages"
```

This mints the agentic teammate identity, registers the blueprint, applies
`customBlueprintPermissions` (tenant admin consent required), and registers
the messaging endpoint. In a headless/non-interactive shell (stdin
redirected), the browser-based admin-consent flow can't be detected, and the
CLI automatically falls back to granting delegated permissions
programmatically instead — confirm with `y` if prompted.

`a365 setup all` writes secrets into `a365.generated.config.json` and
`agent/.env` (both gitignored): `AGENT_ID`,
`CONNECTIONS__SERVICE_CONNECTION__SETTINGS__{CLIENTID,CLIENTSECRET,TENANTID,SCOPES}`,
and `AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__AGENTIC__SETTINGS__*`.
Push these into the App Service's application settings the same way as step 7
(`az webapp config appsettings set --settings @<file>` with a JSON array of
`{name, value}` built from `agent/.env`), then restart the app
(`az webapp restart`) so the `microsoft_agents` SDK picks up real service
connection credentials instead of failing at startup with
`ValueError: No service connection configuration provided.`

**Also push `AUTH_HANDLER_NAME=AGENTIC`** in this same step (it is already the
default in `infra/main.bicep`, but re-applying app settings from a `.env` file
can overwrite it if that name is not included). Without it,
`host_agent_server.py`'s `self.auth_handler_name` stays `None`, so the agent
never attempts the OBO user-token exchange, and `_exchange_user_token()`
silently degrades every turn to Foundry IQ + Web IQ only, logging just
`" No auth handler configured — Fabric IQ/Work IQ will be unavailable this
turn"` (a WARNING, not an error, easy to miss). This is a distinct root cause
from the earlier "Cancelled via cancel scope" RBAC bug — that one broke
Foundry IQ outright; this one silently disables Fabric IQ/Work IQ while
Foundry IQ and Web IQ keep working, which is why the agent still returns a
plausible, well-cited answer that just quietly omits two of the four IQ
surfaces. Verify the fix by checking Application Insights `traces` for
`"🔐 Using auth handler: AGENTIC"` at startup and the absence of the "No auth
handler configured" warning on subsequent turns.

## 9a. Generate the Teams app manifest package

```bash
a365 publish --aiteammate
```

Generates `agent/manifest/manifest.zip` (gitignored — contains tenant-specific
IDs baked in from step 9's `a365 setup all` run, so it must be regenerated,
not reused, for each new tenant/environment). Run this after step 9
completes successfully, before proceeding to §9b.

> **Do not pass `--agent-name` here.** The CLI's own help text says
> `--agent-name` means "no config file is required" — in practice this makes
> `a365 publish` skip `a365.config.json` entirely, including its
> `deploymentProjectPath: "agent"` setting, so it looks for the manifest
> template at `<repo-root>/manifest/manifest.json` instead of the real
> location `agent/manifest/manifest.json` and fails with `ERROR: Manifest not
> found`. Since `a365.config.json` already exists in this repo (created in
> step 9), omit `--agent-name` so the CLI reads it and resolves the correct
> path.

## 9b. Publish the Teams app package (manual, one-time, requires Global/Teams Admin)

`agent/manifest/manifest.zip` (from §9a) is a ready-to-upload custom Teams
app package. Uploading it to the org catalog via Microsoft Graph
(`POST /appCatalogs/teamsApps`) was investigated and found to be **not fully
automatable**:

- A delegated token (e.g. via `az`/`azd`'s cached CLI login) needs the
  `AppCatalog.ReadWrite.All` scope, which Microsoft blocks for its own
  first-party CLI app registrations (`AADSTS65002: ... must be configured via
  preauthorization`) — granting tenant admin consent for that scope on the
  Azure CLI/azd app id does not work.
- An application-only (client-credentials) token with the
  `AppCatalog.ReadWrite.All` **application** permission granted directly via
  Graph still returns `403 Forbidden — User not authorized to perform this
  operation` for this endpoint, which appears to additionally require the
  calling identity to hold the **Teams Administrator** directory role — a
  role that cannot be cleanly assigned to a service principal for this
  legacy catalog endpoint.

**Do this instead (~30 seconds, one time) — use the dedicated Agents admin
surface, NOT the classic Teams app catalog:**

The manifest here uses the Agent 365 agentic schema (`manifestVersion:
devPreview` + `agenticUserTemplates`), which the classic **Teams apps → Manage
apps → Upload a custom app** page does not understand — uploading there fails
with a generic "We can't upload the app" error with no useful diagnostics.
Use the dedicated Agents surface instead:

1. Go to `https://admin.microsoft.com` → **Settings → Integrated apps →
   Agents** (or **Agents → All agents**, depending on tenant UI version).
2. Click **Upload custom agent** and upload `agent/manifest/manifest.zip`.
3. In the upload wizard, choose the deployment scope — **"Just me"** (assign
   to yourself only, fastest for a first smoke test) or **"Specific
   users/groups"** / **"Everyone"** if the whole demo audience needs it
   pre-installed. Complete the wizard to **create an instance** — this is
   the step that actually provisions the agentic teammate user
   (`<agent-name>@<yourtenant>.onmicrosoft.com`) as a real M365 principal,
   not just a catalog entry. `a365 setup all` already created the blueprint
   and messaging endpoint; this step is what makes it installable/chattable
   in Teams. Provisioning/propagation can take a few minutes.
4. **Find and open it in Teams**: for users the agent was deployed to
   directly (not "Everyone"), it appears automatically in the Teams left
   rail under **Apps → Built for your org** (may need a Teams client
   restart/refresh) — no separate manual "install" step is needed for
   directly-assigned users. If it doesn't appear, use Teams' **Apps → search
   `<agent-name>`** and click **Add/Open** to trigger installation
   explicitly.
5. **First-message consent**: the very first message a given human user
   sends will likely trigger a one-time OAuth/consent card (for the
   `AGENTIC` auth handler's OBO token exchange) — approve it. Only after
   this does that user's own agentic identity (`agentic_user_id`) get
   created, which is the prerequisite for §9c's RBAC grants.

## 9c. Grant the agentic user identity the Foundry project RBAC roles

**Now automated by `infra/core/ai/rbac.bicep`** — pass the AAD group your
Teams users belong to (e.g. `noc-iq-demo-teams-users`) as
`teamsUsersPrincipalId` (with `teamsUsersPrincipalType=Group`) to `azd
provision`/`azd up`, and all 5 project-scoped roles below are assigned
automatically, every deploy, with no manual `az role assignment create`
needed:

- `Foundry Agent Consumer`
- `Azure AI Developer`
- `Foundry Project Runtime User`
- `Cognitive Services OpenAI User`
- `Cognitive Services User`

This remains **required** without which every turn fails, either with a 403
on the Responses API call or with a misleading `"Cancelled via cancel scope
..."` error on the toolbox MCP call (an anyio/MCP-library quirk that
mis-surfaces a plain data-plane 403 as a task-cancellation error — see
`docs/TROUBLESHOOTING.md`'s "Auth-type mistakes" entry and
`docs/PRIMER_MCP_CANCEL_SCOPE_BUG.md`). The chat client/agent are built with
a fixed **service** credential (the app's managed identity), but Fabric
IQ/Work IQ/RTI's `UserEntraToken` connections still need OBO identity
passthrough for the calling Teams user — handled one layer down, at the
per-turn MCP tool's `header_provider` (see `docs/ARCHITECTURE.md`). That
means the calling Teams user's own agentic identity also needs RBAC **on
the Foundry project** — a separate surface from anything the app's managed
identity holds on the Cognitive Services account, AND separate from
account-scope RBAC (an assignment scoped only to the parent account is
**not** honored; it must be scoped to the project resource itself, which is
exactly how `rbac.bicep` scopes these — see `aiAccount::project` in that
file).

**`Foundry Project Runtime User` is the role that actually matters for this
call pattern** — it is the only one of the four whose `dataActions` include
`Microsoft.CognitiveServices/accounts/AIServices/responses/*`, the exact API
surface a direct (non-`agent_reference`) Responses API call hits. The other
three are kept as belt-and-suspenders but were confirmed, by direct
inspection of each role's `dataActions` via `az role definition list`, not to
cover this action alone.

**Always assign `teamsUsersPrincipalId` to an AAD group, never an
individual user** — new users then just need adding to the group; no
redeploy or new role assignment is needed. RBAC propagation can still take
a couple of minutes before the next turn succeeds either way. See
`docs/TROUBLESHOOTING.md`'s "Foundry project RBAC" entry for the full
symptom/diagnosis of the Responses API case.

If you can't redeploy immediately and need a stop-gap for an existing
environment, the equivalent one-off command per role is still:

```bash
az role assignment create \
  --assignee-object-id <group_or_agentic_user_id> \
  --assignee-principal-type Group \
  --role "<role name from the list above>" \
  --scope <Foundry project ARM resource id>
```

## 10. Verify end-to-end

In Teams, message the agent and drive the Sydney fibre-cut scenario (see
`docs/SEQUENCE.md` §3 for the 5 narrative beats to confirm). Cross-check in
Application Insights (`Transaction search` / `Logs`) that all four IQ tools
were genuinely invoked for that conversation — not just cited from the
model's general knowledge. This step requires a real Teams client signed in
as a user the teammate app has been installed for — it cannot be simulated by
posting synthetic activities to `/api/messages` directly, since those lack a
valid Bot Framework JWT and a real `serviceUrl` to deliver the reply to.
**This is the one step in this whole deployment that genuinely cannot be
automated or delegated — it requires a human driving a real Teams client.**

### 10a. (Optional) Verify the outbound email-trigger notification path

Only relevant if you completed step 6b (`Mail.Send` consent) and set
`NOTIFY_<PERSONA>_EMAILS`. Send an email to the agentic mailbox
(`nocagent@<tenant>`) with the tag as the **first line of the body** (not
the Subject — see `docs/OUTBOUND_NOTIFICATIONS.md` §5 for why), followed by
one `Field: value` line per field, using the **exact** field names the
persona templates substitute (case-sensitive — arbitrary names render as
literal unsubstituted `{Placeholder}` text, this is by design not an error):

```
[INCIDENT:ESCALATION]
ServiceName: Sydney-Melbourne Fibre
ServiceId: VPN-ACME-CORP
IncidentId: INC-TEST-001
CustomerFacingImpactCount: 12
CurrentStatus: Mitigating
BusinessImpactSummary: Enterprise VPN customers degraded
ETR: 45 minutes
RootCauseSummary: Physical fibre cut on LINK-SYD-MEL-FIBRE-01
TelemetrySummary: Link down, backup path active
ActionSummary: Rerouting via backup path
RunbookReference: fibre_cut_runbook.md
VenueName: Sydney DC1
VenueImpactDescription: Backup link active, no customer impact
SLAStatus: At risk
```

Confirm: (1) each of the 4 `NOTIFY_<PERSONA>_EMAILS` recipients gets an
email with real values substituted, no `{Placeholder}` text remaining; (2)
**you (the sender) are Cc'd** on every one of those emails, in addition to
the brief in-thread reply (`"Incident notification broadcast: {'executives':
True, ...}"`). See `docs/OUTBOUND_NOTIFICATIONS.md`'s full "Live E2E test
results" section for the 5 real bugs found doing exactly this test, in case
any of them recur on a different tenant.

## 11. Teardown (after E2E passes)

**Confirm the resource group name explicitly before deleting — this is
destructive and irreversible.**

```bash
# 1. Delete the Fabric workspace (tenant object, not part of the RG -- az group
#    delete never touches it, and it will keep billing the capacity if skipped)
cd scripts
python delete_fabric_workspace.py          # dry-run: prints what it would delete
python delete_fabric_workspace.py --yes    # actually deletes the workspace
#    (deleting the workspace cascades to the lakehouse, ontology, Data Agent,
#    and the RTI Eventhouse + KQL database in one call -- no per-item cleanup needed)
cd ..

# 2. Decommission the A365 teammate account
a365 teardown   # or remove the agentic user + blueprint via the M365 admin center

# 3. Delete the resource group (removes the Fabric F2 capacity, App Service,
#    Foundry account/project, Search, Storage, App Insights -- everything)
az group show --name "rg-$AZURE_ENV_NAME"   # confirm this is the right RG
az group delete --name "rg-$AZURE_ENV_NAME" --yes --no-wait

# 4. OPTIONAL -- only applies if you created a service principal for
#    non-interactive `az` CLI automation (see "Optional: automation SP"
#    note above §0). Not part of the deployment steps themselves --
#    skip if you don't have one. If you do, remove it (search Entra ID →
#    App registrations for its display name and delete it, or):
az ad app delete --id "<automation-sp-app-id>"
```

## Cost note

The Fabric **F2** capacity is billable (~US$0.36/hr, ~US$260/mo if left
running). Pause it (Fabric admin portal → Capacity settings → Pause) whenever
not actively demoing, and delete it with the resource group at teardown
(step 11.3).
