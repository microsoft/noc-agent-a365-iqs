# Sequence Diagrams

## 1. End-to-end deployment sequence

```mermaid
sequenceDiagram
    autonumber
    participant Op as Operator (you)
    participant Az as Azure (az / azd)
    participant RG as New Resource Group
    participant Fab as Fabric (tenant)
    participant Agent as App Service (NocAgent)
    participant A365 as A365 CLI

    Op->>Az: az login / azd auth login (target subscription)
    Op->>Az: azd up  (infra/main.bicep)
    Az->>RG: Create RG (tags: purpose=noc-iq-demo, DeleteBy=<date>)
    Az->>RG: Create AI Foundry account + project + gpt-5.4/text-embedding-3-small deployments
    Az->>RG: Create Azure AI Search + Storage + App Insights + Log Analytics
    Az->>RG: Create Fabric capacity (F2)
    Az->>RG: Create App Service (agent host) + project-scope RBAC
    Az-->>Op: outputs (project endpoint, search endpoint, capacity name, app host name)

    Op->>Az: python scripts/create_foundry_iq_kb.py
    Az-->>Op: Foundry IQ knowledge base MCP endpoint

    Op->>Fab: python scripts/create_fabric_ontology.py
    Fab->>Fab: Create workspace on F2 capacity, lakehouse, load NOC CSVs as Delta tables
    Fab->>Fab: Create ontology (CoreRouter/TransportLink/PhysicalConduit/... + relationships)
    Fab-->>Op: FABRIC_ONTOLOGY_ID, ontology UI + MCP URLs

    Op->>Fab: python scripts/create_fabric_graph.py
    Fab->>Fab: Populate the ontology's GraphModel with 8 nodes + 9 edges
    Op->>Fab: python scripts/create_fabric_data_agent.py
    Fab->>Fab: Create/publish Fabric Data Agent over the ontology
    Fab-->>Op: FABRIC_DATA_AGENT_MCP_URL

    Op->>Az: Portal — add Web IQ connection (CustomKeys/x-apikey) [done via Bicep if key supplied]
    Op->>Az: python scripts/setup_workiq_entra_app.py
    Az->>Az: Create/reuse Work IQ Entra app + secret + WorkIQAgent.Ask consent +<br/>Azure AI Developer RBAC + OAuth2 Foundry connection + redirect URI
    Op->>Az: python scripts/create_fabric_iq_connection.py  (authType=UserEntraToken)
    Op->>Az: python scripts/create_rti_connection.py  (fabric-rti-connection, authType=UserEntraToken,<br/>target = Fabric Eventhouse MCP endpoint)
    Op->>Az: python scripts/create_foundry_agents.py
    Az->>Az: For each of 5 IQ connections: resolve connection, project.agents.create_version(<br/>  name, PromptAgentDefinition(model, instructions, tools=[MCPTool(project_connection_id)]))
    Az-->>Op: noc-knowledge-agent, noc-topology-agent, noc-threatintel-agent,<br/>noc-comms-agent, noc-incident-agent — 5 persisted Prompt Agents,<br/>idempotent (re-run is a no-op unless the definition actually changed)

    Op->>Az: azd deploy  (pushes agent/ to the App Service)
    Az->>Agent: App Service cold-starts NocAgent.initialize()
    Agent->>Az: confirm each of the 5 specialist agents exists (project.agents.get(name))
    Agent->>Agent: build the orchestrator MAF Agent, binding 5 agents-as-tools<br/>(ask_knowledge_agent, ask_topology_agent, ask_threatintel_agent, ask_comms_agent, ask_incident_agent)
    Note over Agent,Az: No shared Toolbox anymore — each specialist agent already holds<br/>its own single MCPTool, created once by create_foundry_agents.py.

    Op->>A365: a365 setup all  (mint teammate identity + blueprint permissions)
    A365->>A365: Register app, create agentic user, apply customBlueprintPermissions
    A365-->>Op: agent published to Teams / M365 Copilot (tenant admin consent required)
    Op->>Az: az role assignment create --role "Foundry Agent Consumer" --scope <project><br/>--assignee-object-id <noc-iq-demo-teams-users group> (infra/core/ai/rbac.bicep<br/>teamsUsersPrincipalId param) — required for Fabric IQ/Work IQ/RTI IQ OAuth passthrough
```

## 2. Runtime turn — Teams user asks about the Sydney fibre cut

```mermaid
sequenceDiagram
    autonumber
    participant User as Teams user
    participant Bot as Azure Bot Service<br/>(channel connector)
    participant A365 as App Service — host_agent_server.py<br/>(/api/messages, CloudAdapter)
    participant Orc as NocAgent orchestrator (agent.py, MAF, ephemeral)
    participant KA as noc-knowledge-agent<br/>(persisted, Foundry IQ)
    participant TA as noc-topology-agent<br/>(persisted, Fabric IQ)
    participant TI as noc-threatintel-agent<br/>(persisted, Web IQ)
    participant CA as noc-comms-agent<br/>(persisted, Work IQ)
    participant IA as noc-incident-agent<br/>(persisted, RTI IQ / Eventhouse)

    User->>Bot: "What's the blast radius of the SYD-MEL fibre cut?"
    Bot->>A365: POST /api/messages (Bot Framework Activity JSON)
    A365->>A365: on_message handler, start typing indicator
    A365->>Orc: process_user_message(message, auth, auth_handler_name, context)
    Orc->>A365: auth.exchange_token(scopes=[ai.azure.com/.default], AGENTIC)
    A365-->>Orc: user OBO token
    Orc->>Orc: _current_user_token.set(token) — per-turn contextvar,<br/>isolated to this asyncio Task tree

    Orc->>Orc: self._agent.run(history) — orchestrator model call,<br/>5 specialist tool functions bound as tools
    Note over Orc,CA: The ORCHESTRATOR's model decides which specialists to call and how many times.<br/>Each call is a full Prompt Agent turn (its own model + its own MCP tool), not a raw MCP tool call.

    par Orchestrator model decides which specialists to call, in whatever order/count it needs
        Orc->>TA: ask_topology_agent("blast radius for LINK-SYD-MEL-FIBRE-01")
        Orc->>TA: get_openai_client(agent_name="noc-topology-agent") — credential =<br/>_StaticTokenCredential(user OBO token) (UserEntraToken passthrough)
        TA-->>Orc: dependent services (VPN-ACME-CORP, VPN-BIGBANK),<br/>shared conduit (CONDUIT-SYD-MEL-INLAND)
    and
        Orc->>KA: ask_knowledge_agent("fibre-cut runbook + SLA policy terms")
        Orc->>KA: get_openai_client(agent_name="noc-knowledge-agent") — service credential
        KA-->>Orc: runbook steps, SLA $/hr penalty terms
    and
        Orc->>TI: ask_threatintel_agent("vendor/carrier advisory for the affected route")
        Orc->>TI: get_openai_client(agent_name="noc-threatintel-agent") — service credential
        TI-->>Orc: live advisory (if any)
    and
        Orc->>CA: ask_comms_agent("on-call roster / bridge chatter")
        Orc->>CA: get_openai_client(agent_name="noc-comms-agent") — credential =<br/>_StaticTokenCredential(user OBO token) (UserEntraToken passthrough)
        CA-->>Orc: on-call engineer, active bridge summary
    and
        Orc->>IA: ask_incident_agent("alert timeline / optical evidence for the fibre cut")
        Orc->>IA: get_openai_client(agent_name="noc-incident-agent") — credential =<br/>_StaticTokenCredential(user OBO token) (UserEntraToken passthrough)<br/>to the Fabric Eventhouse MCP endpoint
        IA-->>Orc: exact Eventhouse evidence from OpticalTelemetry / NetworkAlerts / IncidentEvents
    end

    Orc->>Orc: check each specialist's response.output for an oauth_consent_request item<br/>(_extract_oauth_consent_url) — if found, set the _pending_consent contextvar<br/>instead of returning text (see step below)
    Orc->>Orc: orchestrator model synthesizes ONE response from whatever specialist<br/>results came back — cite each source, in order: blast radius → incident evidence →<br/>shared-conduit finding → runbook → advisory → on-call
    Orc-->>A365: response text (or, if _pending_consent was set by any specialist,<br/>an empty string — the consent Adaptive Card is sent directly instead)
    A365-->>Bot: response text
    Bot-->>User: "Blast radius: VPN-ACME-CORP + VPN-BIGBANK ($75k/hr exposure)...<br/>⚠️ Non-obvious: FIBRE-02 shares CONDUIT-SYD-MEL-INLAND...<br/>Runbook: reroute via Brisbane... On-call: ..."
```

**Key point this diagram must make clear**: there are **five persisted Foundry
Prompt Agents**, each with its own single MCP tool baked into its own
definition, called via **agents-as-tools** from one ephemeral MAF
orchestrator `Agent`. The `par` block above shows the orchestrator model's
own tool-selection deciding which (and how many) specialists to invoke;
`agent.py` still does not iterate over the 5 IQ surfaces or hand-aggregate
their results itself — only the granularity of what's being routed to has
changed, from a raw MCP tool call to a whole persisted-agent turn.

RTI IQ is the deliberate "evidence" specialist in that set: live Eventhouse
facts from `OpticalTelemetry`, `NetworkAlerts`, and `IncidentEvents`. Foundry
IQ stays the "narrative" specialist: written tickets, runbooks, and
post-mortems. When both are called on the same incident, the orchestrator is
expected to reconcile them, not blur them together.

## 3. Run-scoped token governance (TokenOps) — mixed routing, not a blanket APIM hop

**Why not simply route all 6 model calls (orchestrator + 5 specialists) through
APIM?** Two hard blockers, both discovered while wiring this up:

- **OAuth identity passthrough breaks.** `fabric_iq`, `work_iq`, and `rti_iq` authenticate
  each `responses.create()` call as the *calling Teams user* (a
  `_StaticTokenCredential` wrapping their OBO token), so Agent Service attributes
  the stored per-user consent grant correctly. APIM's `authentication-managed-identity`
  policy substitutes **its own** identity before the call reaches Foundry —
  fine for the 2 service-identity specialists, but it would silently break
  consent attribution for these 3.
- **Persisted-agent request bodies don't fit the gateway's assumptions.**
  `openai-gateway`/`foundry-gateway`'s policies parse a `model` field and a
  `messages`/`input` field out of the request body for token estimation and
  the allowed-model check. A persisted Prompt Agent's model is baked into its
  *own* definition — the client-side `responses.create(input=question)` call
  often carries neither a `model` field nor a `messages` array, so those
  policies' assumptions (now fixed for Responses-API field names, see below)
  still don't fully describe what this app actually sends per specialist.

**What actually ships**: the orchestrator and all 5 specialists keep calling
Foundry **directly** (unchanged data path, so consent passthrough keeps
working for all 5 uniformly). Token governance is achieved by having
`agent.py` itself call the run ledger's `/v1/precall` and `/v1/postcall`
directly — the same decision contract (`allow` / `mutate` / `queue` / `halt`)
APIM's own policies use, just invoked from Python instead of from policy XML,
using the real `response.usage` each call already returns. Since the run
ledger's Container App has **internal-only ingress** (unreachable from the
non-VNet-integrated App Service), these calls go through a thin `/ledger`
pass-through API added to APIM (`run-ledger-gateway` in `apim.bicep`) — APIM
is already VNet-injected and can reach it; this is a plain reverse-proxy hop
with no body inspection, so it doesn't inherit either blocker above.

The deployed `openai-gateway`/`foundry-gateway` APIs (now fixed for
Responses-API field names: `input`/`max_output_tokens`/`input_tokens`/
`output_tokens`, with defensive fallback to the old Chat-Completions field
names) remain live and available for any *other* direct AOAI/Foundry caller
that does send REST-shaped, model-in-body traffic — they are just not in this
app's own hot path.

```mermaid
sequenceDiagram
    autonumber
    participant Orc as NocAgent orchestrator (agent.py)
    participant Ledger as APIM /ledger pass-through<br/>→ Run Ledger (internal Container App)
    participant KA as noc-knowledge-agent (Foundry IQ)
    participant TA as noc-topology-agent (Fabric IQ, OBO)
    participant TI as noc-threatintel-agent (Web IQ)
    participant CA as noc-comms-agent (Work IQ, OBO)
    participant IA as noc-incident-agent (RTI IQ / Eventhouse, OBO)

    Note over Orc,Ledger: Once per turn: mint/reuse a run token (POST /v1/runs),<br/>run_id derived deterministically from conversation_id + activity_id

    Orc->>Ledger: POST /v1/precall (run_id, agent=noc-agent, step, model,<br/>est_input_tokens) — orchestrator's own model call
    Ledger-->>Orc: {action: allow|mutate|queue|halt, reservation_id}
    alt halt or queue
        Orc-->>Orc: raise _RunHaltedError → Teams Adaptive Card ("Run paused by policy")
    else allow / mutate
        Orc->>Orc: self._agent.run(history) (direct to Foundry, unchanged)
        Orc->>Ledger: POST /v1/postcall (reservation_id, real usage_details<br/>input_token_count/output_token_count)
    end

    par For each specialist the orchestrator's model decides to call
        Orc->>Ledger: POST /v1/precall (run_id, agent=knowledge, step, model=noc-knowledge-agent, est_input_tokens)
        Ledger-->>Orc: decision
        Orc->>KA: responses.create(input=question) — service credential, direct to Foundry
        KA-->>Orc: response (usage.input_tokens/output_tokens)
        Orc->>Ledger: POST /v1/postcall (real usage)
    and
        Orc->>Ledger: POST /v1/precall (run_id, agent=topology, step, model=noc-topology-agent, ...)
        Ledger-->>Orc: decision
        Orc->>TA: responses.create(input=question) — _StaticTokenCredential(user OBO token), direct to Foundry
        TA-->>Orc: response (or oauth_consent_request item — unaffected by any of this)
        Orc->>Ledger: POST /v1/postcall (real usage, or failed=true on error)
    and
        Orc->>Ledger: POST /v1/precall (agent=threatintel, model=noc-threatintel-agent, ...)
        Orc->>TI: responses.create(input=question) — service credential, direct to Foundry
        TI-->>Orc: response
        Orc->>Ledger: POST /v1/postcall (real usage)
    and
        Orc->>Ledger: POST /v1/precall (agent=comms, model=noc-comms-agent, ...)
        Orc->>CA: responses.create(input=question) — _StaticTokenCredential(user OBO token), direct to Foundry
        CA-->>Orc: response
        Orc->>Ledger: POST /v1/postcall (real usage, or failed=true on error)
    and
        Orc->>Ledger: POST /v1/precall (agent=incident, model=noc-incident-agent, ...)
        Orc->>IA: responses.create(input=question) — _StaticTokenCredential(user OBO token),<br/>direct to Foundry → direct MCPTool to the Fabric Eventhouse endpoint
        IA-->>Orc: response
        Orc->>Ledger: POST /v1/postcall (real usage, or failed=true on error)
    end
```

**Answering "how is fabric_iq/work_iq/rti_iq's usage governed if it never goes through
APIM?"**: identically to `foundry_iq`/`web_iq`, via this direct precall/postcall pair —
the OAuth-passthrough identity used for the Foundry call is completely
orthogonal to which channel reports the resulting token usage to the ledger.
The run ledger enforces the same run-scoped budget/halt/steer decisions either
way; only the transport of the *governed* call itself (direct vs. through
APIM) differs, and that choice is driven purely by which of the two blockers
above applies to that specialist.

**Latency contract, verified against `agent.py` (`_run_ledger_precall` /
`_run_ledger_postcall`, `_call_specialist`, `process_user_message`)**:

- Both calls are `async def`, made with `httpx.AsyncClient`, and are `await`ed
  on Python's event loop rather than blocking a thread — so while one
  specialist's precall/postcall is in flight, every other concurrently
  running specialist coroutine (and the orchestrator itself) keeps making
  progress. This is what makes the `par` block above genuinely parallel: the
  5 precall→call→postcall triplets don't serialize against each other.
- `precall` is a real, intentional gate: it is awaited **before** the model
  call and its `allow`/`mutate`/`queue`/`halt` decision can change what
  happens next (steer down `max_output_tokens`, or raise `_RunHaltedError`) —
  so it does add its own small latency to that specific call's critical
  path, by design.
- `postcall` is "best-effort" **only in its error handling** — on any
  exception it logs a warning and swallows it (`_run_ledger_postcall`'s
  docstring: "never raises"), so a down run ledger degrades accounting
  accuracy, not the turn. It is still `await`ed inline before
  `_call_specialist`/`process_user_message` returns, so it is not a
  fire-and-forget `asyncio.create_task` — it is a second small,
  in-VNet HTTP hop per call (`RUN_LEDGER_CREATE_TIMEOUT_SECONDS`, default
  2s ceiling, typically far faster).
- Net effect on what the user actually waits for: negligible. Each Foundry
  `responses.create()` call this wraps runs tens of seconds; the precall +
  postcall pair adds at most a couple of fast internal HTTP round-trips
  around it, and — because the 5 specialists run concurrently — that
  overhead never stacks up across specialists, only (trivially) within each
  one's own path.

## 4. The 6 narrative beats this sequence must produce


A correct end-to-end run must surface all six, each traceable to a real
tool call (verified via App Insights traces in the `verify-e2e` step, not
asserted from the model's unaided knowledge):

1. **Blast radius** — `VPN-ACME-CORP` + `VPN-BIGBANK` depend on the cut link
   (Fabric IQ).
2. **SLA exposure** — `$75,000/hr` = ACME (GOLD, `$50k/hr`) + BigBank (SILVER,
   `$25k/hr`) (Foundry IQ — SLA policy docs / Fabric IQ — SLA policy entity).
3. **Bounded exclusion** — `OzMine` (GOLD, `$40k/hr`) is **not** affected;
   the blast radius must be shown as bounded, not maximal (Fabric IQ).
4. **Non-obvious finding** — `LINK-SYD-MEL-FIBRE-02` (the "backup") shares
   `CONDUIT-SYD-MEL-INLAND` with the cut primary link — fake redundancy
   (Fabric IQ `RIDES_ON` relationship).
5. **Evidence beats narrative when they diverge** — RTI IQ's Eventhouse query
   can show the first optical anomaly earlier than the written incident ticket,
   for example because an intermediate alert was suppressed by an alert-storm
   rule (`OpticalTelemetry` + `NetworkAlerts` + `IncidentEvents` vs. Foundry IQ
   ticket narrative).
6. **Reroute** — the runbook-prescribed mitigation is a reroute via Brisbane
   (Foundry IQ knowledge base).

## 5. Merged end-to-end: the live Teams turn + TokenOps governance + the async gateway loop

Sections 2 and 3 above are two separate mermaid diagrams over the *same*
turn — this section interleaves them into one flat, numbered record, then
appends the two asynchronous flows (config-sync-worker's job cycle, and the
Admin UI's write path) that are not part of any single Teams turn but
directly feed and are fed by it. Recorded here for reference; not itself a
new diagram.

### Part 1 — the live Teams turn (steps 1-37)

| # | Description | Azure component |
|---|---|---|
| 1 | Teams user asks: "What's the blast radius of the SYD-MEL fibre cut?" | Microsoft Teams |
| 2 | Bot Service POSTs the Activity to `/api/messages` | Azure Bot Service |
| 3 | `on_message` handler fires, starts typing indicator | App Service |
| 4 | App Service calls `process_user_message(...)` on the orchestrator | App Service |
| 5 | Orchestrator requests the caller's OBO token (`ai.azure.com/.default`, AGENTIC) | A365 auth handler |
| 6 | OBO token returned, stashed in a per-turn contextvar | App Service → orchestrator |
| 7 | Orchestrator sends `POST /v1/precall` for its own model call | APIM `/ledger` → Run Ledger Container App |
| 8 | Run ledger returns `{allow\|mutate\|queue\|halt, reservation_id}` | Run Ledger + Cosmos DB (budget state) |
| 9 | *(if halted)* raises `_RunHaltedError` → "Run paused" card sent to Teams | App Service |
| 10 | *(if allowed)* orchestrator runs its model call, with all 5 specialist tools bound | Azure AI Foundry (`gpt-5.4`) |
| 11 | Orchestrator sends `POST /v1/postcall` with real usage for its own call | Run Ledger |
| 12 | Orchestrator's model decides which specialists to call — dispatches up to 5 in parallel | Foundry (tool selection) |
| 13 | `POST /v1/precall` for `noc-knowledge-agent` | Run Ledger |
| 14 | Calls `noc-knowledge-agent` (service credential) | Foundry Prompt Agent → Azure AI Search |
| 15 | Returns runbook steps + SLA terms; `POST /v1/postcall` with real usage | Run Ledger |
| 16 | `POST /v1/precall` for `noc-topology-agent` | Run Ledger |
| 17 | Calls `noc-topology-agent` (user OBO credential) | Foundry Prompt Agent → Fabric Data Agent → Lakehouse |
| 18 | Returns blast radius + shared-conduit finding; `POST /v1/postcall` | Run Ledger |
| 19 | `POST /v1/precall` for `noc-threatintel-agent` | Run Ledger |
| 20 | Calls `noc-threatintel-agent` (service credential) | Foundry Prompt Agent → Web IQ MCP |
| 21 | Returns any live advisory; `POST /v1/postcall` | Run Ledger |
| 22 | `POST /v1/precall` for `noc-comms-agent` | Run Ledger |
| 23 | Calls `noc-comms-agent` (user OBO credential) | Foundry Prompt Agent → Work IQ MCP → Teams/Outlook |
| 24 | Returns on-call + bridge chatter; `POST /v1/postcall` | Run Ledger |
| 25 | `POST /v1/precall` for `noc-incident-agent` | Run Ledger |
| 26 | Calls `noc-incident-agent` (user OBO credential) | Foundry Prompt Agent → Fabric RTI MCP → Eventhouse KQL DB |
| 27 | Returns alert timeline/optical evidence; `POST /v1/postcall` | Run Ledger |
| 28 | Orchestrator checks all specialist outputs for an `oauth_consent_request` (sets `_pending_consent` if found) | App Service |
| 29 | Orchestrator model synthesizes one answer, citing every source that responded | Foundry (`gpt-5.4`) |
| 30 | Returns response text (or empty string if a consent card takes its place) | App Service |
| 31 | App Service returns the response to Bot Service | App Service |
| 32 | Bot Service delivers the final answer to the Teams user | Microsoft Teams |
| 33 | Each specialist call also emits a structured `usage_event` log line, independent of the ledger calls above | Azure Monitor (App Insights) |
| 34-37 | *(governance side-effects, not user-visible)*: run ledger persists reservation/usage deltas against the run-scoped budget in Cosmos; `config-sync-worker`'s next cycle will read these updated numbers | Run Ledger + Cosmos DB |

Steps 13-27 (5 precall→call→postcall triplets) run in parallel — governed
identically whether the specialist authenticates as the service or via OBO
passthrough; only the transport of the *governed* call differs (direct to
Foundry either way), never the ledger accounting.

All precall/postcall calls (steps 7, 11, 13, 15, 16, 18, 19, 21, 22, 24, 25,
27) are `async`/`await`ed `httpx` calls, never blocking threads — so they
don't serialize the 5 parallel specialist paths against each other. `precall`
is an intentional gate awaited before its model call (it can steer/halt that
call); `postcall` is best-effort only in its *error handling* (never fails
the turn on a ledger outage), but is still awaited inline as a fast in-VNet
hop (≤2s timeout, usually much faster) before the specialist returns — a
rounding error next to the tens-of-seconds Foundry call it wraps. See §3's
"Latency contract" note for the full detail.

### Part 2 — asynchronous, out-of-band (not tied to any single Teams turn)

| # | Description | Azure component |
|---|---|---|
| 38 | Admin sets throttling / max-token quota for a consumer in the Admin UI SPA | Admin UI SPA (Container App) |
| 39 | SPA calls the FastAPI BFF, which `upsert_item`s the change into the Cosmos `config` container (per-consumer doc or `global`) | Admin UI BFF → Cosmos DB (data-plane RBAC: Contributor) |
| 40 | On its next scheduled tick, `config-sync-worker` job wakes up | Container Apps Job |
| 41 | Worker reads the `global` doc + all per-consumer config docs from Cosmos (self-bootstraps `global` on first run if missing) | Cosmos DB |
| 42 | Worker calls the Azure Retail Prices API and refreshes the `pricing` doc (per-model $/token) in Cosmos — fail-safe, never fails the job | Retail Prices API → Cosmos DB |
| 43 | Worker evaluates real usage (from App Insights/`usage_event` telemetry, aggregated) against each consumer's budget, deciding any model downgrades | Log Analytics / App Insights |
| 44 | Worker pushes the 4 allowed-model/quota named values, plus any downgrade decisions, into APIM | APIM named values |
| 45 | Next Teams turn's `/v1/precall` decisions (steps 8/13/16/19/22/25 above) now reflect the freshly-synced quotas/pricing | Run Ledger ↔ APIM named values ↔ Cosmos |
| 46 | Anyone running `check_usage.py`/`check_usage_detail.py` reads `usage_event` traces + the `pricing` doc to render real $ cost per turn | Log Analytics + Cosmos DB |

The loop that ties Part 1 and Part 2 together: every live Teams turn writes
usage into App Insights and ledger reservations into Cosmos; every worker
cycle reads that usage back out, refreshes pricing, and re-tightens the
named values the *next* turn's precall checks against. Cosmos is the
desired-state source of truth throughout; APIM named values are only the
runtime-enforced mirror.

## 6. Copilot Cowork MCP channel

```mermaid
sequenceDiagram
    autonumber
    participant User as Cowork user
    participant Cowork as Copilot Cowork
    participant APIM as APIM /mcp
    participant Auth as Container Apps Easy Auth
    participant MCP as MCP host / AgentMCPTool
    participant NOC as Existing NocAgent._agent
    participant Ledger as Run ledger
    participant Foundry as Foundry specialists

    Cowork->>APIM: GET /.well-known/oauth-protected-resource/mcp
    APIM->>MCP: Transparent metadata pass-through
    MCP-->>Cowork: RFC 9728 resource, authorization server, noc.invoke scope
    User->>Cowork: Focused incident investigation
    Cowork->>APIM: POST /mcp (Bearer token, tools/call noc_investigate)
    APIM->>Auth: Preserve authorization, MCP headers, and body
    Auth->>Auth: Validate Entra signature, issuer, and audience
    Auth->>MCP: Validated request
    MCP->>MCP: Check trusted claims/scope; OBO to ai.azure.com
    MCP->>Ledger: Mint run from oid + MCP request id; precall
    MCP->>NOC: AgentMCPTool.call_tool(task), per-call contextvars set
    NOC->>Foundry: Existing orchestrator and specialist calls
    Foundry-->>NOC: Grounded response
    NOC-->>MCP: Final MCP content
    MCP->>Ledger: Postcall
    MCP->>MCP: Reset contextvars in finally
    MCP-->>Cowork: Result, bounded to 28 seconds
```

This is a channel adapter, not a fork of the orchestration. It initializes one
`NocAgent`, exposes the existing `_agent`, and retains the same specialist and
run-ledger behavior. Cowork's tool-call limit is less than 30 seconds, so the
application returns a useful timeout error by 28 seconds; broad five-specialist
investigations should be split into focused triage, blast-radius, and
communications tasks.
