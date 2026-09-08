# Architecture

## Why these Azure/Fabric components

| Component | Why it was chosen here | Advantage it brings |
|---|---|---|
| **Azure AI Foundry** (persisted Prompt Agents) | Each IQ surface needs its own model, instructions, and single MCP tool, independently versioned — a plain client-side agent can't do that without hand-rolling routing/versioning | Server-side tool execution, per-agent `create_version` history/rollback, and built-in OAuth-consent surfacing (`oauth_consent_request`) instead of a hand-rolled consent flow |
| **Microsoft Fabric** (Lakehouse + Ontology + Data Agent, plus Eventhouse for RTI) | The network topology graph and live incident telemetry are both naturally graph/time-series data that already has a home in Fabric for this org, with per-user RBAC enforced at the data layer | One platform for both the *static* topology graph (Fabric IQ) and the *live* event stream (RTI IQ / Eventhouse+KQL), each enforcing the calling user's own permissions — no separate per-user authZ layer to build |
| **Fabric RTI Eventhouse (KQL DB)** specifically | Alert/optical telemetry is high-volume, time-series, ad-hoc-queryable data — exactly KQL's design target | Sub-second queries over hundreds of thousands of rows, a hosted remote MCP endpoint (no stdio bridge/sidecar to build or run) |
| **Azure AI Search** (Foundry IQ) | Runbooks/tickets/specs are static prose that needs semantic + keyword retrieval, not a live query engine | Managed vector + keyword hybrid search, no infrastructure to run, integrates natively as a Foundry knowledge-base tool |
| **Azure Cosmos DB** (TokenOps `gateway/config` container) | Governance config (quotas, allowed models, per-model pricing) is small, low-write-volume, needs strong read consistency for the worker/BFF, and must stay private | Single source of desired-state truth with a simple per-consumer/document model; private-endpoint-only by default, matching this app's "never expose config data publicly" requirement |
| **Azure Container Apps Jobs** (config-sync-worker) | This is a scheduled batch reconciliation loop (Cosmos → Retail Prices API → APIM), not a long-running service | Pulls `:latest` fresh on every `job start` (no revision-suffix redeploy dance), scales to zero between runs — cheaper than a long-running Container App for a periodic job |
| **Azure Container Apps** (Admin UI, Run Ledger) | Both need a long-running HTTP surface with VNet integration/internal-only ingress, without the overhead of AKS | Internal-only ingress keeps the run ledger unreachable except via the deliberate APIM pass-through; simpler ops than a full cluster for two small services |
| **Azure API Management** (`/ledger` pass-through, `openai-gateway`/`foundry-gateway`) | App Service isn't VNet-integrated and can't reach the internal-only run ledger directly; APIM already is VNet-injected | A thin, already-provisioned reverse-proxy hop reaches the internal Container App with no body inspection — reused for governance transport without inheriting the OAuth-passthrough/body-shape blockers a full policy-enforcement hop would hit (see SEQUENCE.md §3) |
| **Azure Bot Service** | Standard, supported channel connector for getting a Teams/M365 Copilot message into any web app | Carries the A365 teammate identity needed for on-behalf-of user tokens, and Bot Framework Activity JSON is what the Agents SDK already expects |
| **Azure App Service (Linux)** | The orchestrator is a stateless, HTTP-triggered Python web app with no need for containers, scaling policies, or a cluster | Simplest, cheapest compute that satisfies "one always-on endpoint", with `az` /`azd deploy` ergonomics already used across this repo |
| **Application Insights + Log Analytics** | Every specialist call already emits `response.usage`; need queryable, retained telemetry for both tracing and the `usage_event`/cost KQL queries | One place for both distributed tracing (`verify-e2e`) and ad-hoc cost/usage KQL, no separate telemetry pipeline to stand up |
| **Azure Retail Prices API** | Real per-model $/token pricing must come from somewhere authoritative, not be hand-maintained | Free, no-auth public API, always current — removes an entire class of "stale hardcoded price" bugs (the exact bug this session found and fixed) |

The common thread: every choice above is the platform default for its job —
managed/serverless where the workload is bursty or periodic (Jobs, Search,
Retail Prices API), a private data store where the data is sensitive
(Cosmos), and Foundry/Fabric's own native constructs (Prompt Agents, Data
Agent, Eventhouse) wherever the alternative would mean re-implementing
identity passthrough, versioning, or query performance by hand.

## Summary

`noc-agent-a365` is a Teams / M365 Copilot–native NOC (Network Operations
Center) assistant. **Five persisted Microsoft Foundry Prompt Agents** — one
each for **Foundry IQ**, **Fabric IQ**, **Web IQ**, **Work IQ**, and **RTI
IQ** — are
provisioned in the Foundry project and orchestrated by **one ephemeral
Microsoft Agent Framework (MAF) `Agent`** hosted on **Agent 365 (A365)**,
using the **agents-as-tools** pattern: each specialist is exposed to the
orchestrator as a plain Python tool function, and the orchestrator model's
own tool-selection performs the routing across specialists exactly as the
single-agent design routed across IQ tools directly.

The demo scenario is a Sydney↔Melbourne fibre-cut incident. See
[SEQUENCE.md](SEQUENCE.md) §4 for the 6 narrative beats a correct
end-to-end run must surface, and the rest of that doc for the runtime
call sequence.

## Copilot Cowork MCP channel

`agent/mcp_server.py` is an additional channel over the same initialized
`NocAgent._agent`; it is not a second orchestration implementation. A
stateless native MCP server exposes one read-only tool, `noc_investigate`, at
APIM `/mcp`. Container Apps Easy Auth validates Entra tokens, the app performs
defensive claim/scope checks and OBO to `https://ai.azure.com/.default`, and
the existing context variables isolate user token, run token/ID, step counter,
and user attribution per tool call. The application enforces Cowork's
sub-30-second contract with a maximum 28-second timeout. See
[`COWORK_MCP.md`](COWORK_MCP.md).

## Components

```
Teams / M365 Copilot
        │  user message
        ▼
Azure Bot Service (channel connector)
        │  registered via `a365 setup all --messaging-endpoint https://<app>/api/messages`
        │  POSTs Bot Framework Activity JSON, A365 teammate identity
        │  (AgenticUserAuthorization → OBO user token available to the app)
        ▼
┌───────────────────────────────────────────────────────────────────────┐
│ agent/  (Azure App Service, Linux, Python 3.13)                       │
│                                                                        │
│  host_agent_server.py   Generic A365 host: AgentApplication +          │
│                         Authorization + CloudAdapter, message/         │
│                         notification/installationUpdate handlers,      │
│                         typing indicators, /api/health, /api/messages, │
│                         JWT middleware. (Ported verbatim — no          │
│                         NOC-specific logic.)                          │
│                                                                        │
│  agent.py               NocAgent(AgentInterface). Owns ONE ephemeral   │
│                         MAF `Agent` (the "orchestrator"), built once   │
│                         in initialize() from FoundryChatClient,        │
│                         always service-credentialed, with 5 plain      │
│                         Python tool functions bound — one per          │
│                         specialist Prompt Agent (agents-as-tools).      │
│                         Each tool function calls its specialist via     │
│                         `AIProjectClient.get_openai_client(agent_name=  │
│                         ...).responses.create(...)`; fabric_iq/work_iq/ │
│                         rti_iq build that client with the CALLING USER's│
│                         OBO token (`_StaticTokenCredential`), the other │
│                         two with the service credential (see           │
│                         SEQUENCE.md §2).                               │
│                                                                        │
│  token_cache.py,        Generic A365 plumbing (observability token     │
│  agent_interface.py,    cache, the AgentInterface ABC, local dev auth  │
│  local_authentication_  options). Ported verbatim.                    │
│  options.py                                                            │
└───────────────────────────────────────────────────────────────────────┘
        │            (agents-as-tools: ask_knowledge_agent, ask_topology_agent,
        │             ask_threatintel_agent, ask_comms_agent, ask_incident_agent)
        ▼                    ▼                    ▼                  ▼                  ▼
┌───────────────┐   ┌────────────────┐   ┌────────────────┐  ┌───────────────┐  ┌────────────────┐
│noc-knowledge- │   │noc-topology-   │   │noc-threatintel-│  │noc-comms-     │  │noc-incident-   │
│agent          │   │agent           │   │agent           │  │agent          │  │agent           │
│(persisted      │   │(persisted      │   │(persisted      │  │(persisted     │  │(persisted      │
│ Prompt Agent)  │   │ Prompt Agent)  │   │ Prompt Agent)  │  │ Prompt Agent) │  │ Prompt Agent)  │
│ 1 MCPTool ->   │   │ 1 MCPTool ->   │   │ 1 MCPTool ->   │  │ 1 MCPTool ->  │  │ 1 MCPTool ->   │
│ Foundry IQ KB  │   │ Fabric IQ Data │   │ Web IQ MCP     │  │ Work IQ M365  │  │ Fabric RTI     │
│ (Azure AI      │   │ Agent MCP      │   │ server         │  │ toolbox       │  │ Eventhouse MCP │
│  Search)       │   │ (Fabric        │   │                │  │ (Teams,       │  │ (KQL DB:       │
│                │   │  Lakehouse)    │   │                │  │  Outlook)     │  │ OpticalTelemetry,│
│                │   │                │   │                │  │               │  │ NetworkAlerts, │
│                │   │                │   │                │  │               │  │ IncidentEvents)│
└───────────────┘   └────────────────┘   └────────────────┘  └───────────────┘  └────────────────┘
```

Each specialist agent is provisioned idempotently by
`scripts/create_foundry_agents.py` (`project.agents.create_version(...)`),
holding exactly one native `MCPTool(server_url=..., project_connection_id=...)`
pointed directly at its own project Connection — no shared Toolbox layer,
since nothing is shared across these 5 agents anymore.

## IQ auth-type matrix

| IQ surface | Purpose in this demo | Connection auth | Why |
|---|---|---|---|
| **Foundry IQ** | Runbooks, tickets, equipment/infra specs | Service credential (`AzureDeveloperCliCredential` / `ManagedIdentityCredential`) | Not user-scoped data — a shared corpus, safe for a service identity |
| **Fabric IQ** | Live network topology / blast-radius queries | **`UserEntraToken`** (OAuth identity passthrough) | Fabric enforces per-user row/item security; must pass the caller's identity |
| **Web IQ** | Vendor advisories / public carrier status | **`CustomKeys`** (static `x-apikey`) | Public web data, identity-independent — a static key is correct and simpler |
| **Work IQ** | On-call roster, bridge chatter, change approvals | **`UserEntraToken`** (OAuth identity passthrough) | Reads the *user's* Teams/Outlook data; app-only auth is blocked (`AADSTS82001`) |
| **RTI IQ** | Live incident evidence from Fabric Eventhouse | **`UserEntraToken`** (OAuth identity passthrough) | Eventhouse/KQL access is enforced as the calling user; this specialist needs the caller's Fabric read permissions on the workspace/Eventhouse |

Picking the wrong auth type per surface is the single most common integration
mistake in this class of solution (see `docs/TROUBLESHOOTING.md` for the
concrete failure modes hit in this repo). Getting Web IQ onto
`UserEntraToken`, or Work IQ / Fabric IQ onto `CustomKeys`/service
credentials, and putting RTI IQ on anything other than `UserEntraToken`,
both silently degrade to either wrong-user data or hard
failures.

## RTI IQ: evidence, not narrative

`noc-incident-agent` is the fifth specialist: a persisted Prompt Agent whose
single native `MCPTool` points directly at a Fabric-hosted remote MCP endpoint
for a Fabric Eventhouse / KQL database. That Eventhouse exposes exactly three
tables the specialist is instructed to use: `OpticalTelemetry`,
`NetworkAlerts`, and `IncidentEvents`.

Use RTI IQ for machine-generated incident evidence: exact alert timelines,
per-sensor optical readings, detection-vs-acknowledgement latency, and
suppressed alerts. Use Foundry IQ for the written narrative: tickets,
runbooks, post-mortems, and "what the process says we did." The distinction is
intentional: **Foundry IQ is the narrative; RTI IQ is the evidence.** A worked
example this design is meant to surface: *the ticket says time-to-detect was 4
minutes; the Eventhouse says the first optical anomaly was 90 seconds earlier
and got suppressed by an alert-storm rule.*

This specialist intentionally uses a **direct** `MCPTool` instead of a Foundry
Toolbox hop. A spike in this repo showed that a persisted Prompt Agent cannot
authenticate through a Toolbox MCP endpoint, even though the toolbox endpoint
itself responds fine in isolation. See `docs/TROUBLESHOOTING.md` ("Fabric RTI
incident-agent: Foundry Toolbox spike result (B2-c)") for the exact finding.

## Multi-agent design: five persisted Prompt Agents instead of one MAF agent

An earlier version of this repo ran **one** ephemeral MAF `Agent` with all 4
IQ connections bundled into a single Foundry Toolbox, routed by that one
model's native tool-selection. This branch (`feat/foundry-multi-agent`)
replaces that with **five persisted Foundry Prompt Agents**: the original four
IQ surfaces plus RTI IQ for Eventhouse evidence, orchestrated from App Service
via **agents-as-tools**: the orchestrator's own MAF `Agent` gets 5 plain
Python tool functions
(`ask_knowledge_agent`, `ask_topology_agent`, `ask_threatintel_agent`,
`ask_comms_agent`, `ask_incident_agent`), each of which calls one
specialist's own persisted endpoint.

**Honest trade-off record** — this was NOT a strict improvement, both
designs are legitimate for different reasons:

| | One agent, one toolbox (previous) | Five persisted agents, agents-as-tools (this branch) |
|---|---|---|
| Cost per turn | 1 model call + N MCP tool calls | 1 orchestrator call + 1 full model call per specialist invoked (up to 5× for a fan-out question) |
| Latency | 1 model round trip + tool latency | 1 orchestrator round trip + a full sequential/parallel specialist round trip per specialist called |
| Per-specialist tuning | Shared system prompt, one set of instructions for all 4 surfaces | Each specialist has its **own** persisted instructions/model, independently versioned and testable |
| Governance granularity floor | The whole turn (all 4 tools indistinguishable in Log Analytics/token metrics) | **Per specialist-agent call** — Branch B's `run_id`/`x-agent` headers can attribute cost per specialist, not just per turn |
| Consent UX | One client-side MCP error → one Adaptive Card | Agent-Service-managed OAuth, surfaced per specialist as an `oauth_consent_request` item — 3 of 5 specialists (Fabric IQ, Work IQ, RTI IQ) can each independently require a fresh sign-in |
| Operational surface | 1 toolbox, 4 connections | 5 persisted agents (each independently upgradable/rollback-able via `create_version`), 5 connections, no toolbox |

The deciding factor for choosing this branch's design: **Branch B
(`feat/tokenops-gateway`)** needs a governance granularity finer than "the
whole turn" to make per-surface cost/latency trade-offs visible and
governable — see `tokenops-analysis/08-foundry-maf-workflow.md`, which names
exactly this agent-turn-as-governance-unit shape. Fan-out cost (up to ~5×
specialist completions per turn) is accepted and measured, not "optimized
away" here — that is Branch B's job.

## OAuth identity passthrough (the genuine engineering delta vs. the old MCP consent flow)

Tool execution for a persisted Prompt Agent happens **server-side**, inside
Agent Service, not client-side in this app's own process. That changes how
per-user identity for the Fabric IQ / Work IQ / RTI IQ (`UserEntraToken`)
specialists works, and how consent is detected:

1. `NocAgent._exchange_user_token()` calls the A365 `AgenticUserAuthorization`
   handler once per turn, for the `https://ai.azure.com/.default` scope, to
   get the caller's OBO token — unchanged from the previous design.
2. That token is stashed in a per-turn `contextvars.ContextVar`
   (`_current_user_token`), isolated to this turn's own `asyncio` Task tree
   (safe under concurrent turns from different users — see the contextvar
   docstring in `agent.py`).
3. Each specialist tool function (`_call_specialist`) builds a **fresh**
   `AIProjectClient(allow_preview=True)` per call: for
   `fabric_iq`/`work_iq`/`rti_iq`, with a `_StaticTokenCredential` wrapping
   that OBO token (so Agent Service attributes the stored OAuth consent grant
   to the calling user, not the app); for `foundry_iq`/`web_iq`, with the
   app's own service credential.
4. It then calls `project_client.get_openai_client(agent_name=...)
   .responses.create(input=question)` — this binds an `openai.OpenAI` client
   directly to that persisted agent's own endpoint
   (`{project_endpoint}/agents/{name}/endpoint/protocols/openai`); no
   `extra_body={"agent_reference": ...}` needed, the SDK handles that.
5. First-use (or expired-grant) consent surfaces as an **`oauth_consent_request`
   item inside a normal 200 OK `response.output`** (`_extract_oauth_consent_url`
   scans for it, reading `.consent_link`) — structurally different from the
   old design's raised MCP error (`_extract_a2a_consent_url` in the old
   design; see git history). `_call_specialist` sets a second contextvar
   (`_pending_consent`) when this happens; `process_user_message` checks it
   after `agent.run()` returns and, if set, sends the existing Adaptive Card
   (`_build_consent_activity`) instead of the model's own text.

**RBAC requirement**: every Teams user who calls `noc-topology-agent`,
`noc-comms-agent`, or `noc-incident-agent` needs the
**`Foundry Agent Consumer`** role (`eed3b665-ab3a-47b6-8f48-c9382fb1dad6`) at
**project** scope — not account scope — and must be in the project's own
tenant (cross-tenant token exchange is not supported for this feature). This
is the exact same project-vs-account RBAC scoping failure class already
documented in `docs/PRIMER_MCP_CANCEL_SCOPE_BUG.md` for the old toolbox
design; see `infra/core/ai/rbac.bicep`'s `teamsUsersPrincipalId` parameter,
which grants this role to the `noc-iq-demo-teams-users` AAD group.
`noc-incident-agent` also needs the caller's own Fabric workspace/Eventhouse
read access, because Eventhouse query authorization is enforced inside Fabric,
not by Foundry alone.

## Outbound notifications (separate, optional path)

The specialist agents above are read-only and turn-initiated (a user asks,
the orchestrator answers). A separate, additive capability —
`agent/notifications.py` — lets an external event (not a Teams message)
trigger the agent to *originate* a multi-persona email broadcast, via **two
independent triggers** that both call the same `broadcast_incident_update()`
fan-out:

1. `POST /api/incidents/notify` — a webhook that resumes a previously
   stored Teams conversation via the Agents SDK's own proactive-conversation
   feature (`AgentApplication.proactive.continue_conversation`).
2. An `[INCIDENT:<stage>]`-tagged email sent to the agentic mailbox — routed
   through the existing (already event-driven) `EMAIL_NOTIFICATION` handler,
   with no dependency on any prior Teams conversation or stored state.

Both paths make a direct Graph `sendMail` OBO call (bypassing all 5
specialist agents entirely, since Work IQ's connection is read-only).
Trigger 2 also CC's the original inbound email's sender on every persona
email sent, so whoever emailed the incident report in sees the broadcast
content, not just the static `NOTIFY_<PERSONA>_EMAILS` distribution list.
See [`docs/OUTBOUND_NOTIFICATIONS.md`](OUTBOUND_NOTIFICATIONS.md) for the
full design, permission model, trigger comparison, and current limitations.

## Infrastructure (`infra/`)

All resources are provisioned brand-new into a dedicated resource group (no
existing resources are reused), tagged `purpose=noc-iq-demo` plus an optional
`DeleteBy` teardown marker:

| Resource | Bicep module | Notes |
|---|---|---|
| Resource group | `infra/main.bicep` | Subscription-scope entry point |
| AI Foundry account + project | `infra/core/ai/ai-project.bicep` | Hosts the `gpt-5.4` deployment, Foundry IQ KB connection, Web IQ connection |
| Model deployments | `infra/core/ai/ai-project.bicep` (`sequentialDeployments`) | `gpt-5.4` (chat) + `text-embedding-3-small` (KB vectorization) |
| Azure AI Search | `infra/core/search/azure_ai_search.bicep` | Backs the Foundry IQ knowledge base |
| Storage account | `infra/core/storage/storage.bicep` | KB source-document staging |
| Application Insights + Log Analytics | `infra/core/monitor/*.bicep` | End-to-end tracing for `verify-e2e` |
| Fabric capacity (F2) | `infra/core/fabric/fabric-capacity.bicep` | Backs the Fabric IQ workspace/lakehouse/ontology (billable — paused/deleted at teardown) |
| App Service (Linux, B1) | `infra/core/host/appservice.bicep` | Hosts `agent/` via `start_with_generic_host.py` |
| Project-scope RBAC | `infra/core/ai/rbac.bicep` | Grants the App Service's managed identity Azure AI Developer + Cognitive Services User at PROJECT scope; optionally grants `Foundry Agent Consumer` at project scope to `teamsUsersPrincipalId` (the Teams users who call the three OAuth-identity-passthrough specialists) |
| Cowork MCP host + Easy Auth | `gateway/infra/core/host/mcp-host.bicep` | Optional externally-ingressed Container App behind APIM, using its UAMI for image pull, Foundry access, and secretless OBO client assertions |
| Cowork MCP pass-through | `gateway/infra/core/apim/apim.bicep` | Exposes `/mcp` and `/.well-known/oauth-protected-resource/mcp` without model/body policies |

The Fabric **workspace** itself is a Fabric-tenant object, not an ARM
resource, and is created/deleted separately from the resource group (see
`scripts/create_fabric_ontology.py` and `docs/DEPLOYMENT.md` §7 for teardown).

The 5 specialist Prompt Agents themselves are **not** ARM resources — they
are Foundry data-plane objects, provisioned by
`scripts/create_foundry_agents.py` (idempotent: re-running it after an
instruction/model change creates a new version only when the live
definition actually differs; running it against an unchanged definition is
a no-op).
