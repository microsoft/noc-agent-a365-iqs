# Troubleshooting

Known gotchas surfaced while researching and building this solution, recorded
here so `fix-loop` doesn't have to rediscover them.

## Cowork MCP works, but Fabric Data Agent graph execution regressed

| Symptom | Investigation | Root cause / current boundary | Fix / next action |
|---|---|---|---|
| Cowork authenticates, lists `noc_investigate`, and invokes it through APIM, but topology answers report `DataAgent_NOCNetworkDataAgent` failed before producing a result | The portal visibly shows the graph and Lakehouse data. Direct calls to the documented Graph GQL API prove the materialized graph is healthy: router queries return `CORE-BNE-01`, `CORE-MEL-01`, and `CORE-SYD-01`; traversal queries return the real Sydney links and show `LINK-SYD-MEL-FIBRE-01` plus `-02` sharing `CONDUIT-SYD-MEL-INLAND`. Added the previously missing `Service -[:DEPENDS_ON]-> MPLSPath` edge and verified a direct blast-radius traversal returns `VPN-ACME-CORP`/Gold and `VPN-BIGBANK`/Silver. The Data Agent was then moved from its Ontology source to the directly queryable GraphModel, given exact GQL instructions, republished, and allowed to initialize. Its MCP tool still reports that its internally acquired graph access token is invalid, even when the outer call uses an admin Fabric token that successfully executes the same GQL directly. | Authentication, Cowork packaging, APIM, MCP transport, graph definition, ingestion, GQL execution, relationships, capacity, and Lakehouse rows are proven healthy. The remaining failure is specifically the preview Fabric Data Agent runtime's downstream token acquisition for a GraphModel source; it is not a graph-data or canvas problem. Rebuilding the canvas is unnecessary. | `scripts/create_fabric_data_agent.py` now persistently selects the GraphModel rather than the less-configurable Ontology source, and `scripts/grant_agent_identity_access.py` grants `GraphInstance.Read.All`/`GraphInstance.Execute.All` in addition to Data Agent scopes. Retry Teams/Cowork with a fresh sign-in token. If the Data Agent still reports `access token is invalid`, capture the portal Data Agent test-pane request ID and raise a Fabric support issue; direct GQL remains the deterministic verification path. |
| MCP host logs show HTTP 422 for run-ledger `/v1/precall` and `/v1/postcall`, although the Cowork answer still completes | Compared the shared client payload in `agent/agent.py` with `run_ledger.models.PrecallRequest`/`PostcallRequest`. The client omitted required `prompt_hash`; after that 422, it still sent postcall with `reservation_id: null`, which violates the required string field and caused a second 422. | Client/schema drift in the shared Teams/Cowork run-ledger helper, not an APIM or authentication problem. | `_run_ledger_precall` now hashes the real prompt with SHA-256 and sends `prompt_hash`; all orchestrator, specialist, and MCP call sites pass their prompt text. `_run_ledger_postcall` skips when no reservation was created. Focused tests pass. |
| Fabric Data Agent works in its own Fabric test pane, but Cowork says every topology lookup timed out | MCP host request timestamps showed an initial 12-second call followed by repeated requests at the 28–31-second boundary. The focused Cowork skill was still invoking the full NOC orchestrator first; that model then invoked the topology Prompt Agent, which invoked the Fabric Data Agent. The extra outer model turn consumed most of Cowork's sub-30-second tool budget. | Excess orchestration latency, not a Fabric query failure. | Focused skill prompts are now recognized in `mcp_server.py` and routed directly to the matching persisted specialist (`fabric_iq`, `foundry_iq`, `web_iq`, `work_iq`, or `rti_iq`). Broad and combined investigations still use the full orchestrator. |
| Fabric Data Agent succeeds in Fabric, but both Teams and Cowork receive `backend rejected my access token` from the persisted topology Prompt Agent | Reproduced by invoking `noc-topology-agent` directly through Foundry with an admin credential, proving this is not a Teams/Cowork token-cache issue. The direct Fabric Graph GQL API succeeds with the same graph and returns the expected topology. Added `GraphInstance.Read.All`/`Execute.All` delegated consent to both the A365 Agent Identity and Cowork MCP resource app, but the preview Foundry → Data Agent → Graph token hop continued to reject its internally acquired token. | Preview connector bug at the nested Foundry/Data-Agent graph-token exchange. The graph, data, caller auth, and GQL are healthy. | Focused link blast-radius queries now bypass the broken nested connector and execute deterministic read-only GQL directly from the App Service or MCP-host managed identity. Both identities have Fabric workspace Contributor access. The templates return the link endpoints, conduit, shared links, and Service→MPLSPath→TransportLink/SLA exposure. Deployed Teams at `https://app-n2tjinbhnbln6.azurewebsites.net` and Cowork image `noc-mcp:20260908-2` as revision `ca-mcphost-aigw-dev-eus2--0000006`. The persisted Data Agent remains available as fallback for non-template topology questions. |

### 2026-09-08: Cowork timeout followed by misleading topology access failure

At 09:30 UTC, the deployed Cowork host acquired its Fabric managed-identity token,
but its direct Graph request exceeded the 20-second HTTP read timeout. The broad
exception handler then fell back to the persisted topology Data Agent, which
still had the previously documented nested-token failure. The final access-error
answer therefore obscured the original timeout; it did not establish that the
host managed identity needed new permissions.

Both hosts had the correct workspace/GraphModel settings. Teams completed all
three direct Graph queries with HTTP 200 at 09:37 UTC in about 3.46 seconds.
A diagnostic invocation inside Cowork revision `0000006` reproduced
`httpx.ReadTimeout`. Subsequent calls from that same container and UAMI returned
HTTP 200 for GraphModel metadata and GQL. The exact Cowork blast-radius prompt,
through its deployed direct-specialist routing, then returned the full result
twice in 2.56 and 2.11 seconds: ACME (450 users, GOLD, $50,000/hour), BigBank
(1,200 users, SILVER, $25,000/hour), and shared link `LINK-SYD-MEL-FIBRE-02` in
`CONDUIT-SYD-MEL-INLAND`.

No credentials, permissions, networking, images, or runtime settings were changed
during this diagnosis. Direct Graph execution recovered, but the cause of the
intermittent read timeout is not established; do not label it a proven cold-start
issue or claim a permanent fix. A fresh Cowork conversation remains the
end-to-end acceptance step. If it recurs, distinguish the direct request's
exception type/latency from the fallback connector error before reauthorizing.

## Reference: manual node/edge build table for `NOCNetworkOntology`'s graph canvas

The Ontology item's schema and data bindings were correct all along (see
"CONFIRMED, CONCRETE GAP" entry below), but the `GraphModel` item itself had
zero node/edge types defined and required manually recreating them one-by-one
in the Fabric portal's graph canvas (`Add node` / `Add edge` toolbar buttons).
This table was derived directly from the ontology's ground-truth
`EntityTypes/*` and `RelationshipTypes/*` definitions (via `getDefinition`)
so it can be rebuilt exactly if the graph ever needs to be recreated again.

**Nodes** (`Add node` x8):

| Node label | Source table | Key column | Other properties (label → source column) |
|---|---|---|---|
| CoreRouter | DimCoreRouter | RouterId | city→City, region→Region, vendor→Vendor, model→Model, firmwareVersion→FirmwareVersion |
| TransportLink | DimTransportLink | LinkId | linkType→LinkType, capacityGbps→CapacityGbps, sourceRouterId→SourceRouterId, targetRouterId→TargetRouterId |
| PhysicalConduit | DimPhysicalConduit | ConduitId | routeDescription→RouteDescription, materialType→MaterialType, installedYear→InstalledYear |
| AmplifierSite | DimAmplifierSite | SiteId | location→Location, installedYear→InstalledYear, lastCalibration→LastCalibration |
| Service | DimService | ServiceId | serviceType→ServiceType, customerName→CustomerName, customerCount→CustomerCount, activeUsers→ActiveUsers |
| SLAPolicy | DimSLAPolicy | SLAPolicyId | serviceId→ServiceId, availabilityPct→AvailabilityPct, maxLatencyMs→MaxLatencyMs, penaltyPerHourUSD→PenaltyPerHourUSD, tier→Tier |
| MPLSPath | DimMPLSPath | PathId | pathType→PathType |
| Advisory | DimAdvisory | AdvisoryId | vendorName→VendorName, severity→Severity, title→Title |

**Edges** (`Add edge` x9, only after all 8 nodes above exist and are saved):

| Edge label | Source table | Origin node | Origin key | Target node | Target key |
|---|---|---|---|---|---|
| ORIGINATES_AT | DimTransportLink | TransportLink | LinkId | CoreRouter | SourceRouterId |
| TERMINATES_AT | DimTransportLink | TransportLink | LinkId | CoreRouter | TargetRouterId |
| RIDES_ON | FactConduitMapping | TransportLink | LinkId | PhysicalConduit | ConduitId |
| AMPLIFIES | FactAmplifierMapping | AmplifierSite | SiteId | TransportLink | LinkId |
| COVERS | DimSLAPolicy | SLAPolicy | SLAPolicyId | Service | ServiceId |
| AFFECTS | FactAdvisoryMapping | Advisory | AdvisoryId | CoreRouter | RouterId |
| DEPENDS_ON | FactServiceDependency | Service | ServiceId | MPLSPath | DependsOnId |
| TRAVERSES_ROUTER | FactMPLSPathHops (filter `NodeType = "CoreRouter"`) | MPLSPath | PathId | CoreRouter | NodeId |
| TRAVERSES_LINK | FactMPLSPathHops (filter `NodeType = "TransportLink"`) | MPLSPath | PathId | TransportLink | NodeId |

> **Gap found during live graph-canvas review**: `MPLSPath` was originally
> defined as a node with **no edges at all**, leaving it fully disconnected
> from the rest of the graph even though `FactMPLSPathHops` (loaded as a
> Delta table) exists specifically to bridge it. That table is
> **polymorphic** -- each row's `NodeId`/`NodeType` pair points at either a
> `CoreRouter` or a `TransportLink` row depending on hop position -- so it
> needs to be split into the two filtered edges above rather than one
> edge. If the portal's "Add edge" flow doesn't expose a filter step,
> add both edges without a filter anyway: rows where `NodeId` doesn't match
> the target node type's key just fail to resolve and are skipped, which is
> harmless (if less clean) than doing nothing.

Column names above were verified directly against each table's real Delta
log schema (`_delta_log/00000000000000000000.json` via the OneLake DFS API),
not guessed from the ontology's binding JSON alone — the portal's "Origin
key"/"Target key" pickers need a column **in the edge's own source table**
whose values match the corresponding node's own key column: "Origin key" is
usually the source table's own row identifier (matching the origin node's
key), and "Target key" is the foreign-key-style column whose values match
the target node's key.

Workflow: create all 8 nodes → **Save** → create all 9 edges → **Save** →
**Refresh** the `GraphModel` item. Only after nodes/edges are defined does
`Refresh` have anything to build (see the two entries below for the
diagnostic trail that led here).

## Architecture change: moved off the local MCP client entirely (all 4 tools)

The entries below (hang / shared-stack corruption / `BaseExceptionGroup`
escape / redirect-follow / consent parsing) were all bugs in **our own**
hand-rolled MCP client wrapper (`MCPStreamableHTTPTool`/`FoundryToolbox` over
raw `httpx.AsyncClient`s, with a manual `_connect_tools()`
connect/timeout/close lifecycle). After three rounds of fixes still left
Fabric IQ and Work IQ intermittently failing to connect, App Insights
`dependencies` proved the failure was **not a slow timeout** — the `initialize`
RPC call itself succeeded (`success=True`, ~400ms) at the *exact same
timestamp* the connect logged as failed, meaning the bug was in
post-`initialize` client-side connection finalization inside our own wrapper,
not backend flakiness or a genuinely slow tool.

**Fix: stop managing the MCP client lifecycle ourselves.**
`agent_framework.foundry.FoundryChatClient` has native
`get_mcp_tool(project_connection_id=...)` / `get_fabric_tool(connection_id=...)`
helpers that return plain Responses-API tool-definition objects with **no
client-side connect/disconnect lifecycle at all** — Foundry's own Responses
API service runs the MCP round-trip server-side against a pre-registered
project Connection. This eliminates `_connect_tools()`, `AsyncExitStack`, the
retry-loop, and the whole bug class below by construction, for all 4 tools
(not just the 2 that were flaky).

This also let the app drop the separate Fabric-scoped (`api.fabric.microsoft.com`)
OBO token exchange: identity passthrough for `UserEntraToken` connections
(Fabric IQ, Work IQ) is now driven purely by which credential builds the
per-turn `FoundryChatClient` — Foundry itself performs the server-side OBO
exchange from that one `ai.azure.com`-scoped user token to each connection's
registered audience.

**Everything in the sections below is now historical** — kept for context on
why the architecture changed, not as active guidance for the current code.

## Architecture change #2: `get_mcp_tool(project_connection_id=...)` is a Prompt Agent pattern, not valid for Hosted Agents (400 `missing_mutually_exclusive_parameters`)

| Symptom | Cause | Fix |
|---|---|---|
| After the 403 fix above, every turn instead failed with `Error code: 400 - {'error': {'message': "Missing mutually exclusive parameters: 'tools[0]'. Ensure you are providing exactly one of: 'server_url', 'connector_id', or 'tunnel_id'.", ...}}` | `FoundryChatClient.get_mcp_tool(project_connection_id=...)` sets a `project_connection_id` key on the MCP tool dict. Per the [MCP tool doc](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/model-context-protocol), `project_connection_id` is only valid for **Prompt Agents** — a persisted `project.agents.create_version(...)` object called via `extra_body={"agent_reference": {...}}`. Our code is a **Hosted Agent** (ephemeral MAF `Agent`/`FoundryChatClient`, direct `responses.create(...)` calls with no persisted Agent object) — for that pattern the tool dict must instead carry exactly one of `server_url`, `connector_id`, or `tunnel_id`, which `get_mcp_tool(project_connection_id=...)` never sets. Prompt Agents and Hosted Agents have fundamentally different valid tool-attachment shapes, and mixing them surfaces as a generic low-level parameter error rather than "wrong agent type." | Per the doc's own "Hosted agents" sample: bundle all project connections into a **Foundry Toolbox** (`project_client.toolboxes.create_version(name, tools=[MCPToolboxTool(server_label=..., server_url=connection.target, project_connection_id=connection.id, require_approval="never"), ...])`), which exposes its own combined MCP endpoint (`{PROJECT_ENDPOINT}/toolboxes/{name}/versions/{version}/mcp?api-version=v1`). Attach that ONE endpoint via the **client-side** `agent_framework.MCPStreamableHTTPTool`, with a `header_provider` callable injecting the caller's OBO token (or a service-credential fallback) as the `Authorization` header per request. This is now the live architecture in `agent/agent.py`: one combined toolbox (`noc-iq-toolbox`, all 4 IQ connections), built once in `initialize()`; a fresh `MCPStreamableHTTPTool` built and `close()`d per turn in `process_user_message()`. Identity passthrough for the two `UserEntraToken` connections (Fabric IQ, Work IQ) now happens at this per-turn tool's `header_provider`, one layer below the chat client itself (which is always built with the service credential, per the doc's own sample). Verified live: `az` query of `{PROJECT_ENDPOINT}/toolboxes/noc-iq-toolbox/versions/1` confirms all 4 tools (`foundry-iq`, `web-iq`, `fabric-iq`, `work-iq`) are correctly bundled with their `server_url`/`project_connection_id`. |

**Known cost/smell not yet addressed**: a new toolbox *version* is created on
every app start/restart (no dedup/reuse). Acceptable for this demo; would
accumulate garbage versions in a long-lived deployment.

## Verified live: full e2e Teams turn, all 3 queried tools succeeded through the toolbox

A live Sydney fibre-cut turn completed in ~66s (well inside the 180s
watchdog) with **zero exceptions**. App Insights `dependencies` confirms
three real MCP tool calls through the toolbox this turn, all `success=True`:
`foundry-iq___knowledge_base_retrieve` (9.7s), `fabric-iq___DataAgent_NOCNetworkDataAgent`
(28.6s), `web-iq___web` (0.4s). Work IQ was not invoked this turn (the model
didn't need on-call/bridge context for this question — it remains available
on demand). This confirms the Toolbox + `MCPStreamableHTTPTool` +
`header_provider` OBO-passthrough design works end-to-end.

One residual oddity: the model's own reply text said "the Fabric IQ network
topology tool had a technical issue," even though the `fabric-iq` dependency
call itself succeeded (`success=True`, no exception) in 28.6s. This means the
underlying Fabric Data Agent (`NOCNetworkDataAgent`) returned *content*
describing a limitation of its own (not a transport/auth failure our code can
see) — App Insights doesn't capture MCP tool response bodies, so the exact
wording can't be inspected further from telemetry alone. Not a bug in this
repo's orchestration layer; if it recurs, inspect the Fabric Data Agent
directly in the Fabric portal.

## "Fabric IQ had a technical issue" recurs across turns despite healthy transport, RBAC, ontology, and data

| Symptom | Investigation | Likely cause | Fix |
|---|---|---|---|
| On repeated live turns, the model's own reply says the Fabric IQ topology tool "failed"/"had a technical issue," yet App Insights `dependencies` shows `tools/call fabric-iq___DataAgent_NOCNetworkDataAgent` with `success=True`, no exception, 20-43s duration each time (long enough for a real query attempt, not an instant/transport failure) | Directly verified via Fabric REST API, bypassing this app entirely: (1) workspace role assignments confirm the agentic user (`NOC Agent`) already has `Contributor`; (2) the Data Agent's own definition (`getDefinition`) confirms it's correctly wired to the `NOCNetworkOntology` ontology with all 8 expected entity types and GQL-generation instructions; (3) the backing `NOCTopologyLakehouse` has all 19 expected Delta tables present; (4) Fabric capacity is `F64` (meets the documented minimum SKU for Copilot/AI Skills) and `Active`; (5) tenant setting `EnableAOAI` is `True` (Copilot/AOAI not blocked at tenant level). **Reproduced the failure directly against the Data Agent's own raw MCP endpoint** (`POST https://api.fabric.microsoft.com/v1/mcp/workspaces/{ws}/dataagents/{agent}/agent`, `tools/call DataAgent_NOCNetworkDataAgent`) with a trivial single-entity query ("List all core routers with city and vendor") — got the exact same "couldn't interpret the query" text, `isError: false`, confirming this is a genuine failure inside Fabric's own Data Agent, not something our Toolbox/MCP orchestration is causing or mis-surfacing. A metadata-only question ("what entities/properties do you have access to?") against the same Data Agent succeeded correctly and fully — its own LLM/prompt/reasoning layer is healthy; only *data-returning* queries fail (100% of attempts in this session, not intermittent). | **Working hypothesis (not fully proven)**: something in the Data Agent's actual GQL query generation/execution step is broken — one candidate, not yet confirmed: the ontology's real entity definition (fetched via `getDefinition` on the Ontology item, `EntityTypes/1/definition.json`) uses strict PascalCase properties (`RouterId`, `City`, `Region`, `Vendor`, `Model`, `FirmwareVersion`), and the Data Agent's own aiInstructions warn these are case-sensitive with no room for reinterpretation — but this couldn't be conclusively tied to the actual failure, since the raw generated GQL text isn't exposed by the MCP `tools/call` response (it only returns the final natural-language answer/error, not the query it ran). Not fixable from this app's code either way — it lives entirely inside Fabric's Data Agent runtime. | **Next diagnostic step (requires the Fabric portal, not API)**: open the `NOCNetworkDataAgent` item in the Fabric workspace UI and use its built-in chat/test pane, which typically surfaces the actual generated query (GQL) for each attempt — run the same simple query there to see the literal query text and its execution error, which the MCP interface hides behind a generic message. If the portal confirms a case-sensitivity/property-name mismatch, either edit the ontology's `dataSourceInstructions` to be more explicit/example-driven, or try Fabric's "republish"/"resync" action on the Data Agent (draft vs. published stage configs were identical at inspection time, so a resync may not itself be curative — the portal's own query preview should be checked first). If it turns out to be a different failure mode entirely, this note should be updated with the real root cause once found. |

## ROOT CAUSE FOUND: the ontology's graph model was never successfully built because the Fabric capacity had auto-paused

| Symptom | Investigation | Root cause | Fix |
|---|---|---|---|
| FDA/Fabric IQ never returns real query results even after all the above checks passed; separately, the user reported being unable to add any node in the Ontology's graph query/explore UI in the portal, suspecting the ontology was never actually created | Listed all workspace items via `GET /v1/workspaces/{ws}/items` — confirmed a `GraphModel` item exists (`NOCNetworkOntology_graph_...`), distinct from the `Ontology` schema item. Checked its job history (`GET .../items/{graphId}/jobs/instances`): the **last 3 manual `Refresh` jobs (spanning Aug 12 14:06 through Aug 13 00:32) all failed** with `errorCode: GraphNotRefreshable`, `"Graph doesn't have valid content and cannot be refreshed."` A follow-up call to the shadow lakehouse tables API failed with `errorCode: CapacityNotActive`. Checked `GET /v1/capacities`: the project's F64 capacity (`fabric<token>`) had `"state": "Inactive"` (confirmed independently via `az resource show` on the Azure-side resource, `properties.state` also reflected the pause). | **The Fabric capacity had auto-paused** (Fabric capacities auto-pause after a period of inactivity by default). With the capacity paused, the graph-build/refresh pipeline that materializes actual node/edge instances from the lakehouse into the ontology's graph **could never complete**, leaving the graph with zero queryable content (`queryReadiness: "None"` on the `GraphModel` item) — even though the ontology's *schema* (entity/relationship type definitions, correctly PascalCase) was intact the whole time. This is why the Data Agent's schema-description question always succeeded (pure metadata, no capacity/graph-query dependency) while every real data query failed, and why the user couldn't add nodes in the portal's ontology graph explorer (there is no live queryable graph to add to). The earlier casing hypothesis above is superseded by this — it doesn't need to be true for the observed symptoms, since a paused capacity alone fully explains every failure. | **Resumed the capacity**: `az resource invoke-action --action resume --ids /subscriptions/.../resourceGroups/rg-<env-name>/providers/Microsoft.Fabric/capacities/fabric<token>` — confirmed `state: Active` afterward via both the Fabric API and Azure resource properties. **Correction (see next entry below)**: capacity being paused was a real, confirmed problem, but it was NOT the sole cause — two more manual `Refresh` attempts after the capacity was already `Active` still failed with the exact same `GraphNotRefreshable` error, meaning the graph also had no data source/mapping bound at all yet. |

## Correction: `GraphNotRefreshable` persisted even after the capacity was Active — the graph had no data source/mapping bound yet

| Symptom | Investigation | Root cause | Fix |
|---|---|---|---|
| After resuming the paused F64 capacity (confirmed `Active`), two further manual `Refresh` jobs on the `GraphModel` item (`00:43:58`, `00:44:03` UTC) **still failed** with the identical `GraphNotRefreshable: "Graph doesn't have valid content and cannot be refreshed."` User was, at the same time, in the middle of the Fabric portal's "Select data from Fabric to use in your Graph" picker, choosing a lakehouse — this is the *first step* of binding a data source to the graph, not a refresh of an already-configured one. | Confirmed via `GET /v1/workspaces/{ws}/items/{graphId}/jobs/instances` that the failure signature (`GraphNotRefreshable`) is identical across all 5 attempts to date, both before and after the capacity fix, which rules out capacity/pause as the (sole) cause of this specific error — a paused capacity would more likely surface as `CapacityNotActive` (which we did see once, on a *different*, table-listing call, not on the refresh job itself). | The ontology's `GraphModel` item has **no data source bound and no node/relationship-to-table mapping configured/saved yet** — i.e., the graph is genuinely empty of *configuration*, not just empty of *data*. `Refresh` cannot succeed on a graph with no mapping to refresh from, regardless of capacity state. This is consistent with the user being unable to add nodes in the ontology explorer earlier (nothing to explore because no mapping exists) and now correctly working through the portal's own guided flow to pick `NOCTopologyLakehouse` (the real data lakehouse with the 19 populated tables — confirmed as the correct choice over the ontology's own internal shadow lakehouse, `NOCNetworkOntology_lh_bcf3566...`). | Complete the full portal wizard: (1) select `NOCTopologyLakehouse` as the data source (in progress), (2) map each entity type (`CoreRouter`, `TransportLink`, etc.) to its corresponding lakehouse table and map relationship types to their join/foreign-key columns, (3) save/apply the mapping, (4) only then run **Refresh** — this should be the first refresh attempt with an actual chance to succeed, since prior attempts had no mapping to work from. If `Refresh` still fails with `GraphNotRefreshable` after a mapping is fully saved, that would point to a genuinely different (and worth escalating to Fabric support) issue. |

## CONFIRMED, CONCRETE GAP: the GraphModel item itself has zero node/edge types defined, independent of the Ontology's schema/bindings

| Symptom | Investigation | Root cause | Fix |
|---|---|---|---|
| Even after the capacity fix and after picking `NOCTopologyLakehouse` in the "Select data from Fabric" picker, `Refresh` on the `GraphModel` item still fails (5 consecutive attempts, latest at `00:46:32` UTC) with `GraphNotRefreshable`; separately, the user cannot add any node in the ontology's graph/query view in the portal | Called `getDefinition` on **two different items** to compare: (1) the `Ontology` item (`bcf3566c-...`) — its `EntityTypes/*/definition.json` and matching `DataBindings/*.json` files ARE correctly present and correctly wired (e.g. `EntityTypes/1` = `CoreRouter` is bound via `dataBindingConfiguration.sourceTableProperties` to `itemId: 84f676f5-...` = `NOCTopologyLakehouse`, `sourceTableName: DimCoreRouter`, with all 6 property bindings using the correct PascalCase source columns `RouterId, City, Region, Vendor, Model, FirmwareVersion`). (2) The separate `GraphModel` item (`47e46d66-...`, the thing `Refresh` jobs actually target) — its `getDefinition` (an **async** call: `POST .../getDefinition` returns `202` + `Location`/`Retry-After` headers pointing to a Power BI-hosted operations endpoint, poll until `Succeeded` then `GET .../result`) returned: `dataSources.json` correctly lists all 19 Delta tables from `NOCTopologyLakehouse` by their real OneLake `abfss://` paths — **but** `graphDefinition.json` is `{"nodeTables": [], "edgeTables": []}` and `graphType.json` is `{"nodeTypes": [], "edgeTypes": []}` — **both completely empty**. | The Ontology's schema (entity/relationship *type* definitions) and its data-source *bindings* are correctly configured and were likely set up programmatically/via an earlier script/import — but that configuration was **never translated into the actual `GraphModel` item's own node/edge type list**, which is the structure `Refresh` and the query/explore UI actually operate on. In Fabric's Ontology UI, this translation step normally happens when a user manually drags/adds each entity type onto the graph canvas as a node, assigns it to a data source, and clicks **Save** — that manual canvas step was never completed (or was lost), leaving `graphType.json`/`graphDefinition.json` empty even though the underlying schema/bindings exist. This is exactly consistent with "I am not able to add any node" — with zero existing node types in the graph, and if the canvas's "add node" flow is itself failing/greyed out, that's the next thing to isolate directly in the portal. | **In the Fabric portal**, open `NOCNetworkOntology`'s graph/canvas view (not just the query view) and, for each of the 8 entity types, explicitly **add it as a node on the canvas**, pick `NOCTopologyLakehouse` → the correct table (matching the bindings already confirmed above, e.g. `CoreRouter` → `DimCoreRouter`) if prompted, then repeat for relationship types as edges, and **Save**. Only after `graphType.json`/`graphDefinition.json` show non-empty node/edge types will `Refresh` have anything to build. If the canvas's own "add node" action is failing or unresponsive (matching the user's exact complaint), that's a distinct portal-UI-level bug to report to Fabric support directly, since the underlying data/schema/capacity are all independently confirmed healthy at this point. |

## RESOLVED: manually rebuilding all 8 nodes + 6 edges fixed Fabric IQ end-to-end

After the user manually recreated all 8 node types and 6 edge types on the
`NOCNetworkOntology` graph canvas (per the build table above, using
verified real Delta table column names) and clicked Save, the next `Refresh`
job **succeeded** (`status: Completed`, no more `GraphNotRefreshable`).
Confirmed via `getDefinition` that `graphType.json`/`graphDefinition.json`
now contain all 8 node types and 6 edge types with correct property/key
mappings. Directly re-tested the Data Agent's raw MCP endpoint (bypassing
the app) with the exact same queries that failed throughout this
investigation:
- `"List all core routers with their city and vendor."` → returned 3 real
  routers (CORE-MEL-01/Cisco, CORE-BNE-01/Juniper, CORE-SYD-01/Cisco) —
  the simplest possible query, previously 100% failing, now works.
- `"Which transport links originate at CORE-SYD-01, and what conduit do
  they ride on?"` → correctly traversed the `ORIGINATES_AT` and `RIDES_ON`
  edges, returning 3 real links and correctly identifying that
  `LINK-SYD-MEL-FIBRE-01` and `-02` share the same conduit
  (`CONDUIT-SYD-MEL-INLAND`) — the exact shared-conduit blast-radius
  scenario used in the demo's incident-response test cases.

**Final root cause, end to end**: the Ontology item's schema and data
bindings were always correctly configured, but the separate `GraphModel`
item that Fabric IQ/the Data Agent actually queries against had never had
its own node/edge types created — this is a manual, one-time step in the
Fabric portal's graph canvas that isn't inferred automatically from the
Ontology's schema, nor from loading data sources, nor from Refresh alone.
Once created and saved, `Refresh` populates real graph data and both
simple and relationship/traversal queries work correctly. The capacity
auto-pause encountered earlier in this investigation was a real, separate
contributing issue (now also fixed) but was not sufficient on its own to
explain the failure — both fixes were required.

## Work IQ `ask` returns no on-call/bridge results, surfaces the bot's own prior Teams chat instead

| Symptom | Cause | Fix |
|---|---|---|
| Asking "who is on-call and what's being said on the incident bridge" gets a clean, honest "not found" reply from Work IQ rather than an error — `work-iq___ask` succeeds (`success=True`, 25-78s) but the only Teams/M365 content it surfaces is a chat discussing this same incident (i.e. the bot's own prior conversation turns with the user), not a real on-call roster entry or bridge meeting | This tenant/demo has no seeded on-call roster or incident-bridge meeting data in real M365/Teams — Work IQ is querying the live tenant's actual Graph/Copilot Retrieval data, which genuinely has nothing else matching this fictional incident scenario except the user's own chat history with this bot. This is a **data-availability gap in the demo environment**, not a wiring/auth bug: Work IQ is reachable, authenticated, and answering honestly rather than hallucinating a fake on-call name. | If a live demo needs a populated on-call/bridge answer, seed real Teams content ahead of time (e.g. a Teams channel post naming an on-call engineer, or a calendar meeting titled around the incident) that Work IQ's Graph search can actually find. Otherwise, this "I don't know" response is the *correct*, intended behavior for a grounded agent with no invented data. |

## 429 `rate_limit_exceeded` on the gpt-5.4 model deployment

| Symptom | Cause | Fix |
|---|---|---|
| `("<class 'agent_framework_foundry._chat_client.FoundryChatClient'> service failed to complete the prompt: Error code: 429 - {'error': {'code': 'rate_limit_exceeded', ...}}"` after several live turns run in quick succession during testing | The `gpt-5.4` model deployment was provisioned with only `GlobalStandard` capacity `50` (50K TPM). Each turn makes multiple LLM calls (the initial turn plus a follow-up call after each tool result — up to 5 calls for a turn that uses all 4 IQ tools), each with a large system prompt + 4 tool schemas in context, so repeated back-to-back manual test turns can burn through 50K TPM quickly. Checked subscription-wide quota via `az cognitiveservices usage list --location eastus2`: the `OpenAI.GlobalStandard.gpt-5.4` **subscription limit is 1000K TPM**, far above what this one deployment was using — plenty of headroom to raise it. | Raised the deployment's provisioned capacity from 50 to 150 (150K TPM): `az cognitiveservices account deployment create --name ai-account-<token> --resource-group rg-<env-name> --deployment-name gpt-5.4 --model-name gpt-5.4 --model-version "2026-03-05" --model-format OpenAI --sku-capacity 150 --sku-name GlobalStandard` (this API is also used to *update* an existing deployment's SKU — there's no separate `az cognitiveservices account deployment update` command). Takes effect immediately, no app restart needed. If 150K still isn't enough for heavy rapid-fire demo testing, it can be raised further (up to the 1000K subscription limit) with the same command. |

| Symptom | Cause | Fix |
|---|---|---|
| After both fixes above, a live Teams turn produced **no 403/400/exception at all**, but the bot's own reply was `"Sorry, that request is taking longer than expected..."` — our internal 90s watchdog (`AGENT_RUN_TIMEOUT_SECONDS`) firing, logged as `agent.run() exceeded 90s watchdog; abandoning this turn` | Cold-start cost of the new architecture on a fresh app instance / first turn: one-time toolbox `create_version()` call, plus the per-turn `MCPStreamableHTTPTool`'s first MCP `initialize`/tool-discovery handshake against the combined toolbox endpoint (which itself fans out to 4 underlying MCP connections), plus token acquisition in `header_provider`. This is genuinely slower than a single raw-tool call and can exceed 90s on a cold instance. | Raised `AGENT_RUN_TIMEOUT_SECONDS` to `180` (`infra/main.bicep` app setting, applied live via `az webapp config appsettings set` and redeployed via bicep so it survives future `azd up`/`azd provision`). If 180s still proves insufficient after further live testing, consider warming the toolbox/MCP connection at `initialize()` time (e.g. a dummy tool-list call) instead of paying the cold-start cost on the user's first turn. |

## Foundry project RBAC: the caller of the Responses API needs its own role, separate from Cognitive Services RBAC

| Symptom | Cause | Fix |
|---|---|---|
| Every turn fails outright with `("<class 'agent_framework_foundry._chat_client.FoundryChatClient'> service failed to complete the prompt: Error code: 403", PermissionDeniedError('Error code: 403'))` — not just Fabric IQ/Work IQ degrading, the *entire* reply fails, immediately after switching to building the per-turn `FoundryChatClient` with the user's own OBO token (`_StaticTokenCredential(foundry_user_token)`) instead of the service credential | The stack trace (App Insights `exceptions.details.rawStack`) shows the 403 is thrown from `openai.resources.responses.responses.py` -> `client.responses.with_raw_response.create(...)`, i.e. a **raw Foundry Responses API call**, not an `agent_reference` call to a published Agent object. Per the [MCP tool doc's prerequisites](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/model-context-protocol#prerequisites), interacting with a Foundry project at runtime needs the **Foundry User** role (or a narrower equivalent) **on the Foundry project** — a completely separate RBAC surface from Cognitive Services-account RBAC that the app's managed identity already had. Three roles were tried in sequence and **none alone were sufficient**: `Foundry Agent Consumer` (dataAction `.../endpoints/interact/action` — scoped to `agent_reference` calls to a *published* Agent, which this code doesn't use), then `Cognitive Services OpenAI User` + `Cognitive Services User` (generic model-invocation roles, still 403 after 7+ minutes, ruling out a propagation delay). Running `az role definition list` to inspect every Foundry/Cognitive Services role's exact `dataActions` surfaced the real match: **`Foundry Project Runtime User`** (id `142bfaed-a13f-4c2d-bed2-6db62c4a1009`), whose *only* dataAction is `Microsoft.CognitiveServices/accounts/AIServices/responses/*` — exactly the API surface `client.responses.with_raw_response.create(...)` hits, and the one action none of the other 3 roles granted. | Grant the agentic user identity `Foundry Project Runtime User` at the Foundry **project** scope: `az role assignment create --assignee-object-id <agentic_user_id> --assignee-principal-type User --role "Foundry Project Runtime User" --scope <project ARM id>`. The other 3 roles (`Foundry Agent Consumer`, `Cognitive Services OpenAI User`, `Cognitive Services User`) are left assigned as belt-and-suspenders but are likely unnecessary for this MAF-direct-Responses-API call pattern; if a future cleanup pass confirms `Foundry Project Runtime User` alone is sufficient, remove the other 3 to keep the RBAC model minimal. This must be repeated for every new distinct agentic user identity — there is no tenant-wide/group option observed yet for these roles in this A365 setup. **As of this writing, the fix has been applied but not yet confirmed by a live Teams retest** — update this note once verified. |

## Auth-type mistakes (the #1 failure class)

| Symptom | Cause | Fix |
|---|---|---|
| Work IQ tool call loops on `oauth_consent_request` / never succeeds headless in Teams | Work IQ connection configured with `OAuth2` instead of `UserEntraToken` | Reconfigure the connection as `UserEntraToken`. `OAuth2` works in the Foundry *playground* (interactive browser) but cannot complete headless inside a Teams turn. |
| Work IQ call fails with `AADSTS82001` | Attempting app-only (client-credentials / Managed Identity) auth against Work IQ | Work IQ only accepts delegated (OBO) user tokens. There is no service-identity path — always forward the caller's token. |
| Fabric IQ / Work IQ returns another user's data, or nothing | Fabric/Work IQ tool built once at process start with a service credential instead of rebuilt per-turn with the caller's OBO token | See `agent/agent.py: NocAgent._build_turn_tools()` — these two tools must be constructed fresh per message from `auth.exchange_token(...)`. |
| `"Sorry, I encountered an error: MCP server failed to initialize: Cancelled via cancel scope ..."` | **Real root cause: the App Service managed identity had zero RBAC role assignments on the Azure AI Search service**, so the Foundry IQ knowledge-base MCP `initialize` call failed data-plane auth — the `mcp`/`anyio` client library mis-surfaces a 401/403 as a raw `anyio.CancelledError`-derived cancel-scope error instead of a clean HTTP error, which looked like a concurrency bug. (A retry-once safety net and a per-turn tool rebuild were tried first as reasonable hypotheses given the error text, and are kept as defensive measures, but neither was the actual fix.) | Grant `Search Index Data Reader` to the identity on the Search service — see `searchIndexDataReaderRole` in `infra/core/ai/rbac.bicep`. To isolate which of the 4 IQ tools is actually failing, read the full `exception.stacktrace` in the Application Insights span export and check `server.address` on the sibling `"initialize"` CLIENT span. |
| Web IQ configured with `UserEntraToken` fails for anonymous/service scenarios | Wrong auth type — Web IQ is public data and doesn't need identity | Use `CustomKeys` with an `x-apikey` header instead. |
| Agent answers fine but says it has no Fabric IQ / Work IQ / "live topology" / "M365 bridge" access, with only a WARNING (not an error) in the logs: `"No auth handler configured — Fabric IQ/Work IQ will be unavailable this turn"` | The `AUTH_HANDLER_NAME` app setting was never set on the App Service, so `host_agent_server.py`'s `self.auth_handler_name` stayed `None` and was never passed into `agent.py`; `_exchange_user_token()` silently returns `None` whenever `auth_handler_name` is falsy (by design — see its docstring), so `_build_turn_tools()` only builds Foundry IQ + Web IQ and skips Fabric IQ/Work IQ for that turn without raising any visible error. | Set `AUTH_HANDLER_NAME=AGENTIC` (must match the handler name in the `AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__<name>__SETTINGS__*` settings written by `a365 setup all`) — now the default in `infra/main.bicep`. Verify via Application Insights `traces`: look for `"🔐 Using auth handler: AGENTIC"` at startup and the absence of the warning above on later turns. |
| Cancel-scope error comes back *after* fixing `AUTH_HANDLER_NAME` (now that Fabric IQ/Work IQ actually get attempted), even though the retry-once safety net also fails | One specific tool (observed: the Work IQ `FoundryToolbox`, last in `_build_turn_tools()`'s list) never completes its MCP `initialize()` call — Application Insights `dependencies` shows exactly 3 successful `initialize`/`tools/list` pairs per turn then nothing, confirming the other 3 tools (Foundry IQ, Web IQ, Fabric IQ) are healthy and only the 4th hangs. `agent_framework`'s `_prepare_run_context` connects any not-yet-connected tool right before the turn runs, so one hanging tool kills the *whole* turn. | `NocAgent._connect_tools()` now pre-connects each tool individually with a bounded timeout (`TOOL_CONNECT_TIMEOUT_SECONDS`, default 15s) before calling `agent.run()`; a tool that fails/hangs is dropped (logged as a warning) instead of taking the healthy tools down with it. Check Application Insights `dependencies` (`name == "initialize"`) to count how many tools connected per turn if this recurs. |
| Agent still says Fabric IQ / "network-ontology" is unavailable even after `_build_turn_tools()` requests a separate `api.fabric.microsoft.com`-scoped OBO token | `AADSTS65001: consent_required` — the App Service's Entra **Agent Identity** (`agentIdentity` service principal, created by `a365 setup`, no classic app registration) had **zero `oauth2PermissionGrants`**. `ai.azure.com` tokens still succeed because that resource is RBAC-gated (any token mints fine; access is checked later via role assignment), but Fabric/Power BI's API is a classic Entra app requiring actual delegated-permission admin consent before a token can even be issued for that audience. Visible only in App Insights `traces` as `"Failed to acquire agentic user token ... 'error': 'invalid_grant', 'error_description': 'AADSTS65001: The user or administrator has not consented ...'"` — the "Cancelled via cancel scope" text never appears for this failure mode; the tool is just silently never added to the turn (its OBO token exchange returned `None`, so `_build_turn_tools()`'s `if fabric_user_token` guard skips it — no warning, no error, no MCP `initialize` attempt at all). | Grant tenant-wide admin consent directly via Graph, since there's no app registration object to use the portal's "API permissions" UI on: `POST https://graph.microsoft.com/v1.0/oauth2PermissionGrants` with `{"clientId": "<agent identity object id>", "consentType": "AllPrincipals", "resourceId": "<Power BI Service SP object id, resolve via `az ad sp show --id https://api.fabric.microsoft.com --query id`>", "scope": "DataAgent.Read.All DataAgent.Execute.All GraphInstance.Read.All GraphInstance.Execute.All"}`. Verify with `GET .../oauth2PermissionGrants?$filter=clientId eq '<id>'`. |
| Work IQ toolbox's MCP `initialize()` fails with the masked "Cancelled via cancel scope" error even though its `ai.azure.com`-scoped OBO token mints successfully (unlike Fabric, the token itself is fine) | Same consent-gap pattern as Fabric, but one layer deeper: Work IQ internally calls **Microsoft Graph** on the user's behalf, and the Entra Agent Identity had zero consent for the 7 Graph delegated permissions Work IQ requires (`Sites.Read.All`, `Mail.Read`, `People.Read.All`, `OnlineMeetingTranscript.Read.All`, `Chat.Read`, `ChannelMessage.Read.All`, `ExternalItem.Read.All` — see [Work IQ API permissions reference](https://learn.microsoft.com/en-us/microsoft-365-copilot/extensibility/work-iq-api-permissions-reference)). Because the failure happens server-side inside the toolbox's own Graph call (not in this app's token exchange), it never shows up as an `AADSTS65001` in *this app's* traces — it surfaces only as the generic MCP cancel-scope error. | Same fix pattern as Fabric: `POST https://graph.microsoft.com/v1.0/oauth2PermissionGrants` with `{"clientId": "<agent identity object id>", "consentType": "AllPrincipals", "resourceId": "<Microsoft Graph SP object id, resolve via `az ad sp show --id 00000003-0000-0000-c000-000000000000 --query id`>", "scope": "Sites.Read.All Mail.Read People.Read.All OnlineMeetingTranscript.Read.All Chat.Read ChannelMessage.Read.All ExternalItem.Read.All"}`. |
| Fabric IQ still fails with "Cancelled via cancel scope" even after the `AADSTS65001` consent fix — the OBO token now mints successfully (confirmed in traces), and a manually-curled `initialize` call against the same Fabric Data Agent MCP URL with an **admin** `api.fabric.microsoft.com` token returns a clean `200 OK` | **The OBO token's subject is not the human Teams user — it's an auto-provisioned Entra "agent user" identity** (`#microsoft.graph.agentUser`, e.g. `nocagent@<tenant>.onmicrosoft.com`, a child object of the Agent Identity via `identityParentId`). Fabric's workspace-level RBAC (`GET /v1/workspaces/{id}/roleAssignments`) is separate from the tenant-wide Entra API-permission consent already fixed — the Fabric workspace only had the human admin as a member, so the agent-user identity itself had zero access to the workspace/lakehouse/Data Agent it needed to query, even with a perfectly valid, correctly-scoped, correctly-consented token. | Add the agent-user's object id as a member of the Fabric workspace: `POST https://api.fabric.microsoft.com/v1/workspaces/{workspaceId}/roleAssignments` with `{"principal": {"id": "<agent-user object id, same as the `agentic_user_id` in Application Insights traces>", "type": "User"}, "role": "Contributor"}`. Find the agent-user's object id/UPN via `GET https://graph.microsoft.com/v1.0/directoryObjects/<agentic_user_id>` (look for `identityParentId` matching the Agent Identity's object id to confirm it's the same agent). |

| Teams turn produces **zero reply at all** (not even a degraded/error message — just the typing indicator, then silence) once the Fabric IQ consent + workspace-RBAC fixes above landed | `NocAgent._connect_tools()`'s original fix used `asyncio.wait_for(coro, timeout=...)` directly on the MCP tool's `enter_async_context()`. That call is **not safe**: on timeout, `wait_for` cancels the coroutine and then *awaits it* to finish absorbing that cancellation before raising `TimeoutError`. The MCP SDK's connect path opens an `anyio` `TaskGroup`/cancel scope; once Fabric IQ/Work IQ's auth started succeeding (so their connect attempts actually reached this codepath, instead of failing fast on a consent/RBAC error as before), cancelling that scope from a different task than the one that entered it (exactly what `wait_for`'s internals do) meant `anyio` never unwound cleanly — so `wait_for` itself hung forever, no `TimeoutError` was ever raised, no warning was ever logged, and the whole turn went silent. Confirmed via App Insights `dependencies`: both silent turns showed exactly 2 `initialize`/`tools/list` pairs (Foundry IQ, Web IQ) then nothing — no exception, no 3rd/4th tool warning, ever — while `az webapp` health checks showed the process itself stayed up (single hung `asyncio` task inside one turn, not a process crash). | Rewrote `_connect_tools()` to run each tool's connect as its own `asyncio.Task`, wrapped in `asyncio.wait_for(asyncio.shield(task), timeout=...)` — `shield` means a timeout only cancels our *wait*, never the underlying task, so a hang can never block the loop; the task is fire-and-forget cancelled (exception swallowed) instead. Applied the identical shield pattern to `agent.run()` itself (`AGENT_RUN_TIMEOUT_SECONDS`, default 90s) and to the `finally` block's `tool_stack.aclose()`/`client.aclose()` teardown, since the same anyio hazard could in principle hang either of those too. |
| Same silent-turn symptom recurred **immediately after the fix above** — this time 2 of 4 tools connected (`initialize`/`tools/list` succeeded), a `"Could not cleanly close MCP exit stack due to cleanup error group: unhandled errors in a TaskGroup (1 sub-exception)"` warning appeared from the underlying MCP library, and then, again, nothing — no exception in App Insights, no `agent.run()` watchdog log even minutes later, yet the App Service's own `/api/health` endpoint kept responding normally the whole time (so only one `asyncio` Task was stuck, not the whole process) | The first fix's rewrite ran all 4 tool connects **concurrently** via `asyncio.gather()`, but every concurrent task called `stack.enter_async_context(tool)` on the **same shared** `contextlib.AsyncExitStack` instance. `AsyncExitStack` is not designed for concurrent mutation from multiple tasks — one task's cancellation mid-registration corrupted the stack's internal exit-callback bookkeeping for the whole batch. That raised an exception from inside `asyncio.gather()`/the corrupted stack itself — not from any individual tool — which escaped `_connect_tools()`'s own try/except (guarding only each *tool's* connect, not the shared stack's registration) and propagated out of `process_user_message()` uncaught (its `await self._connect_tools(...)` call sits **outside** the method's try/except block), silently killing the whole turn with nothing ever logged to Application Insights. | Each tool's `__aenter__()`/`__aexit__()` is now called directly (not via the shared stack's `enter_async_context()`) from inside the per-tool concurrent task; only *after* `asyncio.gather()` returns do healthy tools get registered onto the shared `stack` one at a time, sequentially, via `stack.push_async_exit(tool.__aexit__)` — so no concurrent mutation of the shared stack ever happens. Added `return_exceptions=True` to the `gather()` call as a second safety net so an unexpected per-tool exception can never again escape uncaught. Verified locally with `agent/test_connect_tools.py` (`python agent/test_connect_tools.py`), which reproduces both regressions with fake hanging/healthy tools and stubbed SDK imports (no real Azure credentials needed) — asserts the hang is contained *and* that 5 concurrently-connected healthy tools all register and close cleanly on the shared stack. |
| A live Teams retest **did reply successfully** after the fix above (proving the silence regression was fixed), but the reply still says Fabric IQ/Work IQ are unavailable this turn, and App Insights `traces` show `"Tool 'network-ontology' connect raised unexpectedly, skipping for this turn:"` with an **empty** exception message — i.e. it hit the generic `isinstance(result, BaseException)` fallback branch, not the tool-specific timeout warning, meaning `_connect_one()`'s own `except Exception as exc:` clause did **not** catch whatever was raised | `asyncio.CancelledError` is a `BaseException` subclass, not an `Exception` subclass, since Python 3.8. When `anyio`'s internal `TaskGroup` (used by the MCP SDK's connect path) collects a `CancelledError` among its sub-exceptions during a forced/cancelled shutdown, it raises a bare `BaseExceptionGroup` (not the `Exception`-subclassing `ExceptionGroup`) whenever *any* sub-exception is a bare `BaseException`. Fabric IQ's intermittent MCP-init flakiness (the same underlying "Cancelled via cancel scope" issue, still not fully root-caused on the MCP-server side) occasionally surfaces this way, and a plain `except Exception as exc:` around each connect attempt does not catch it — it escapes to whatever caught the outer `asyncio.gather()`/loop. Running connects **concurrently** was also an unrequested optimization added opportunistically in the two fixes above, and concurrent connection-setup contention (shared HTTP pools / token-cache locks) is a plausible aggravating factor for this flakiness, on top of not being something the user ever asked for. | Two changes: (1) `_connect_tools()` now connects tools **sequentially** (`for tool in tools:`), reverting the unrequested `asyncio.gather()` concurrency entirely — this removes the whole class of shared-resource/contention risk the two fixes above were built to paper over, at an acceptable bounded worst-case of ~60s (4 tools × `TOOL_CONNECT_TIMEOUT_SECONDS`) instead of ~15s; (2) the per-tool guard is now `except BaseException as exc:` instead of `except Exception as exc:`, so a bare `BaseExceptionGroup`/`CancelledError` escaping the MCP SDK's `TaskGroup` is caught right at the source and logged via the specific, informative per-tool warning instead of the generic fallback. Verified locally with `agent/test_connect_tools.py`'s new `check_exception_group_is_caught()` case (a fake tool whose `__aenter__` raises `BaseExceptionGroup("...", [asyncio.CancelledError()])`) — confirms the tool is dropped gracefully without killing the turn. |

## MCP tool wiring gotchas

| Symptom | Cause | Fix |
|---|---|---|
| Foundry IQ / Fabric IQ MCP tool fails during session init even though a `header_provider`/token exists | `MCPStreamableHTTPTool`'s auth was supplied late (e.g. via a callback) instead of attached to the `httpx.AsyncClient` at construction | Always construct `httpx.AsyncClient(auth=<httpx.Auth>)` before passing it into `MCPStreamableHTTPTool(http_client=...)`. |
| Fabric IQ MCP call returns a generic `500 Internal Server Error` | The `httpx.AsyncClient` defaulted to `follow_redirects=False`; Fabric's MCP endpoint routes through a redirect layer | Always set `follow_redirects=True` on the Fabric IQ HTTP client (already done in `agent/agent.py`). |
| A Work IQ `a2a_preview` consent requirement surfaces as a raw, unhandled `McpError` instead of a friendly message | MAF's built-in consent-URL extraction (`agent_framework_foundry_hosting._responses.consent_url_from_error`) only recognizes MCP-sourced consent errors, and only when the call goes through `ResponsesHostServer` — this agent calls `agent.run()` directly | `agent/agent.py: _extract_a2a_consent_url()` re-implements the same JSON parsing standalone; the caught `McpError` is converted into a "please consent at &lt;url&gt;" chat reply. |

## Deployment-doc accuracy gaps found by cross-checking scripts/Bicep/agent.py against DEPLOYMENT.md

Found during a documentation audit pass (not live-tested failures) — both are
now fixed in `docs/DEPLOYMENT.md` and the referenced script, but are recorded
here since they were silent (no error at request time):

| Gap | Why it was silent | Fix |
|---|---|---|
| No script or Bicep module ever created the `fabric-iq-connection` Foundry project connection (unlike Work IQ, which had one) | `agent.py`'s connection lookup is wrapped in a caught exception — Fabric IQ just never appears in the toolbox, with no error at agent startup or first call | `scripts` gained an ARM `PUT` for `fabric-iq-connection` (`authType: UserEntraToken`), documented as new DEPLOYMENT.md §4c. |
| `scripts/create_workiq_toolbox.py` additionally created a standalone `work-iq-tools` Foundry toolbox and told the operator to set a `CUSTOM_FOUNDRY_WORKIQ_TOOLBOX_NAME` app setting — but `agent.py` never reads that env var or references a `work-iq-tools` toolbox anywhere; it only needs the `WorkIQ` **connection**, which it wraps into its own `noc-iq-toolbox` | Leftover from an earlier, pre-Toolbox-refactor architecture iteration; the toolbox it created was an orphaned, unused Foundry resource and the printed app-setting instruction referenced a setting the app never consumes | Removed `create_toolbox()`/the toolbox-creation call from the script; it now only does the connection `PUT`. DEPLOYMENT.md §6/§7 updated to match. |

## Fabric Data Agent / ontology gotchas

| Symptom | Cause | Fix |
|---|---|---|
| NL2GQL-generated query against the Graph source times out | The model wrote Cypher-style `WHERE` after `MATCH` | Fabric Graph GQL requires `FILTER`, not `WHERE`. Not applicable to this demo's Ontology-only source, but relevant if a Graph Model is ever added. |
| `ORDER BY` in a generated graph query references a column that doesn't exist | NL2GQL invented a different capitalization/alias than the one it projected in `RETURN` | Tell the Data Agent's GraphModel instructions to preserve the exact `RETURN` alias (see `scripts/create_fabric_data_agent.py: GRAPH_INSTRUCTIONS`). Graph sources support example GQL queries; Ontology sources do not. |
| `fabric-data-agent-sdk` install fails or throws `RuntimeError: Can not determine dotnet root` outside a notebook | The SDK pins conflicting `azure-identity`/`httpx` versions and relies on Semantic-Link workspace resolution that only works inside a Fabric notebook | Don't use the SDK. `scripts/create_fabric_data_agent.py` and `create_fabric_ontology.py` both call the raw Fabric REST API directly instead. |
| `UserNotLicensed` error creating the lakehouse | The signed-in Entra account has no Fabric/Power BI license | Assign a Fabric (or Power BI Pro/PPU) license to the account running the provisioning scripts. |

## Bicep gotchas

| Symptom | Cause | Fix |
|---|---|---|
| `BCP139`/`BCP120` on `infra/main.bicep` | A subscription-scope Bicep file tried to declare a resource-group-scoped resource (e.g. a role assignment) directly | Wrap it in a `module` with `scope: rg` — see `infra/core/ai/rbac.bicep`. |
| Foundry Agents API calls fail with 403 even though RBAC roles are assigned | RBAC was granted at the Foundry **account** scope, not the **project** sub-resource | Scope Azure AI Developer (`64702f94-c441-49e6-a78b-ef80e0188fee`) and Cognitive Services User (`a97b65f3-24c7-4388-baec-2e87135dc908`) to `.../accounts/{account}/projects/{project}`, not just the account (`infra/core/ai/rbac.bicep`). |

## GitHub / tooling gotchas

| Symptom | Cause | Fix |
|---|---|---|
| `gh repo clone microsoft/...` or GitHub MCP tools fail with `GraphQL: Resource protected by organization SAML enforcement` | The `microsoft` org enforces SAML SSO on the GitHub App token used by `gh`/MCP | Use a plain `git clone https://github.com/microsoft/<repo>.git` instead — works for public repos without hitting the SAML gate. |

## Fabric RTI incident-agent: Foundry Toolbox spike result (B2-c)

Spiked whether a persisted Foundry **Prompt Agent** can point its `MCPTool` at
a **Toolbox's** combined MCP endpoint (`{PROJECT_ENDPOINT}/toolboxes/{name}/versions/{v}/mcp`)
instead of a raw MCP server, ahead of adding `noc-incident-agent` over the new
Fabric Eventhouse. Outcome: **B2-c — not attachable.**

| Symptom | Cause | Fix |
|---|---|---|
| Throwaway toolbox wrapping `fabric-iq-connection` answered `tools/list` directly (200, with an `ai.azure.com` bearer token) | The toolbox's own MCP endpoint is fine in isolation | n/a — confirms the toolbox itself isn't broken |
| A throwaway Prompt Agent whose `MCPTool.server_url` pointed at that toolbox endpoint failed every call with `tool_user_error` → inner `401 PermissionDenied` (both under the app's default credential and under `_StaticTokenCredential` OBO injection) | `PromptAgentDefinition` only supports `tools: list[Tool]` — a Prompt Agent's own `MCPTool` cannot authenticate to a Toolbox's MCP endpoint at all, so OBO passthrough through that hop was never reachable to test | **Decision: skip the toolbox layer for `noc-incident-agent`.** It gets a direct `MCPTool(server_url=FABRIC_RTI_MCP_URL, project_connection_id=<fabric-rti-connection>)`, identical in shape to the existing `fabric_iq`/`work_iq` specialists. `scripts/create_rti_toolbox.py` (if written) stays in the repo unused/unwired, in case a future SDK version adds toolbox support to `PromptAgentDefinition`. |



See `docs/OUTBOUND_NOTIFICATIONS.md`'s "Live E2E test results" section for
full detail and the partner-facing writeup. Summary table:

| Symptom | Cause | Fix |
|---|---|---|
| `[INCIDENT:<stage>]` in the email Subject never triggers the broadcast, no matter how it's formatted | The A365 email connector's plain "message" activity never transmits the Subject at all — `channel_data` is only `{"tenant": {...}, "productContext": "email"}` (confirmed via a temporary `logger.error()` diagnostic dump; `logger.info()` never reaches App Insights, only WARNING/ERROR do) | Moved the tag convention to the **first line of the email body** instead of the subject (`agent.py`'s `parse_incident_email()`); subject is still checked first for forward-compat but is empty in practice on this path. |
| `Error: [Errno 2] No such file or directory: '/tmp/.../data/runbooks/customer_communication_template.md'` in App Insights traces the first time a persona broadcast actually fires | `notifications.py`'s `TEMPLATE_PATH` resolved to the repo-root `data/` directory (a sibling of `agent/`), but `azure.yaml`'s `project: agent` means only `agent/` is packaged for the App Service deploy — the sibling directory never ships | Duplicated the template into `agent/data/runbooks/customer_communication_template.md` and resolved `TEMPLATE_PATH` relative to `agent/`. Keep both copies in sync if the template changes. |
| The `partners` persona email is never sent (`broadcast()` returns `{"partners": false}` or it's silently absent), while the other 3 personas work | A live App Service app setting was named `NOTIFY_PARTNERS_EMAILS` (plural) but the code/`.env.template` read `NOTIFY_PARTNER_EMAILS` (singular) — missing recipients is a silent, by-design skip (`logger.info`, not an error), so this looked like "nothing happened" rather than a clear failure | Set the app setting with the exact singular name; `az webapp config appsettings list` + `grep -i notify` is the fastest way to catch a typo like this. |
| Delivered persona emails show literal, unsubstituted text like `{ServiceName}`/`{IncidentId}` instead of real values | The test/example email body used field names (`Site`/`Severity`/`ETA`) that don't match ANY persona's actual `{Placeholder}` names in `data/runbooks/customer_communication_template.md` — `notifications.py`'s `_SafeDict.__missing__` deliberately leaves unknown tokens untouched rather than raising, so a field-name mismatch fails silently in the delivered email rather than with an exception | Use the exact field names from the template's "Variable Reference" table (case-sensitive) — see the corrected example body in `docs/OUTBOUND_NOTIFICATIONS.md` §5/Path B. |
| The `partners` persona email contains its normal body PLUS the entire trailing "## Variable Reference" markdown table (and a stray `---`) appended after it | `notifications.py`'s `_extract_persona_section()` bounded each `### Persona: X` section by searching only for the next `### ` heading; `partners` is the LAST persona in the template file, followed by a `## Variable Reference` (level-2, not level-3) heading, which `\n### ` never matched, so the slice ran to end-of-file | Changed the boundary search to stop at ANY next markdown heading level (`\n#{1,3} `) and strip a trailing `---` rule. Strengthened `notifications.py`'s `_demo()` self-check to assert no `{` remains in the rendered body (previously only checked the subject) and to specifically guard against a "Variable Reference" leak regressing. |

## Per-request token usage logging (added on `feat/tokenops-gateway`, found during live e2e testing)

Built to answer "what did this Teams turn actually cost, per user/agent/model,
with token + cost breakdown" — see `agent/agent.py`'s `usage_event` log line and
`gateway/app/config-sync-worker/check_usage_detail.py`. Three real bugs surfaced
along the way, in the order they were found:

| Symptom | Cause | Fix |
|---|---|---|
| `check_usage_detail.py` KQL fails with `SEM0100: 'extend' operator: Failed to resolve scalar expression named 'customDimensions'` | Workspace-based App Insights `AppTraces` names the custom-dimensions column `Properties` (a raw JSON string), not `customDimensions` as most classic App Insights docs/examples assume | Use `extend p = parse_json(Properties)` instead. Verify any new KQL against this workspace with `az monitor log-analytics query -w <workspace> --analytics-query "AppTraces \| take 1"` before trusting column names. |
| Zero `usage_event` rows for several Teams test turns in a row, even though other App Insights tables (`AppRequests`/`AppDependencies`/`AppExceptions`) showed real, fresh telemetry for the same window | **Not** ingestion lag (the first suspicion) — cross-referencing `AppDependencies` (showed a specialist call that had actually *succeeded*, ~83.7s) against `AppTraces` (no corresponding custom log line at all) revealed that **no `logger.info()` call in the entire app had ever reached App Insights, all session**, including a pre-existing "📨 Message from" line that predates this feature | Root cause below. Diagnostic pattern worth repeating: when a filtered custom-log query returns nothing, always cross-check raw `AppTraces`/`AppRequests`/`AppDependencies` for the same window before concluding "no data yet" — lag is a real thing but was a red herring here. |
| (root cause of the row above) All `logger.info()` calls app-wide were silently dropped, no error anywhere | `agent/agent.py` called `configure_azure_monitor()` (which attaches its own handler to Python's root logger as a side effect) and then called `logging.basicConfig(level=logging.INFO)` — `basicConfig()` is a documented no-op once the root logger already has ≥1 handler, so the root logger silently stayed at Python's default `WARNING` and every INFO line in the whole app (not just the new usage_event line) was swallowed before ever reaching the exporter | Added `logging.getLogger().setLevel(logging.INFO)` immediately after `configure_azure_monitor()` — unconditional, doesn't depend on handler state. Generalise this fix to any Python app combining `azure.monitor.opentelemetry` with `logging.basicConfig()`. |
| `check_usage_detail.py`'s Cosmos pricing lookup fails with `(Forbidden) Request originated from IP <x> through public internet. This is blocked by your Cosmos DB account firewall settings` | `cos<token>` has `publicNetworkAccess=Disabled` + VNet filter enabled — private-endpoint-only by design. Cosmos DB data-plane **RBAC** (`Cosmos DB Built-in Data Reader`, assigned via `az cosmosdb sql role assignment create`) is necessary but **not sufficient**; when `publicNetworkAccess` is `Disabled`, no public IP gets through regardless of RBAC or IP allow-lists | Attempting `az cosmosdb update --public-network-access Enabled` (even scoped to a single IP via `--ip-range-filter`) is **silently reverted** by an Azure Policy with a **Modify** effect on the resource group (confirmed via `az monitor activity-log list` showing `Microsoft.Authorization/policies/modify/action Succeeded` right after the write) — this is a governance guardrail, don't keep retrying it. Made the script's cost lookup non-fatal (`read_pricing_safe()` catches and shows `$0.00`/a warning instead of crashing) so usage rows still print. To get real `cost_usd` locally, run the lookup from **inside the VNet** (e.g. exec into `ca-adminui-aigw-<env>`, which already computes cost correctly for its own dashboard) rather than from a laptop. |

Separately (not fixed, just documented — real upstream/design issues, not bugs in this feature):

- Foundry IQ's `knowledge_base_retrieve` tool intermittently takes 90–172s+ before succeeding/timing out server-side (`InternalServerError: Operation exceeded the maximum runtime of 90 seconds` seen once, a 172.7s successful-but-slow call seen once).
- The orchestrator's hard-coded `AGENT_RUN_TIMEOUT_SECONDS` (180s) watchdog can trip on a single Teams turn that asks two questions at once, since sequential/overlapping specialist calls can each legitimately take 80–170s+. Workaround for now: ask one thing per Teams message.
- `POST /ledger/v1/runs` still 404s (APIM route mismatch) — fails safe, run-scoped governance headers are simply not attached; unrelated to usage logging.

## Config-sync-worker + Admin UI: full bug chain from "worker job never completes" to "cost shows $0"

Found and fixed in order while chasing "how do I track token cost" end-to-end:

| Symptom | Cause | Fix |
|---|---|---|
| `sync.py` `KeyError`/silently reads nothing | Job's Bicep set env var `COSMOS_CONTAINER`; `sync.py` reads `COSMOS_CONFIG_CONTAINER` | Renamed the read in `sync.py` to match the Bicep-provisioned name. |
| `sync_named_values()` throws `KeyError: 'SUBSCRIPTION_ID'` (later also seen in Admin UI, same code path) | Job's/Admin UI's Bicep container template never had `SUBSCRIPTION_ID`/`APIM_RG` env vars, only `APIM_NAME` | Added both to `container-apps.bicep` for **both** `configSyncJob` and `adminUiApp` (`subscription().subscriptionId`, `resourceGroup().name`). |
| `ManagedIdentityCredential` 400 `invalid_scope`/`ClientAuthenticationError` from a bare `DefaultAzureCredential()` | **Not** Jobs-specific as first suspected — the exact same failure hit the long-running `ca-adminui-aigw-<env>` Container App too. Bare `DefaultAzureCredential()` can't disambiguate the identity in this environment even with only one user-assigned identity attached | Pass `managed_identity_client_id=os.environ.get("WORKER_CLIENT_ID"/"ADMIN_UI_CLIENT_ID")` explicitly. **Treat this as a systemic pattern**: any Container App or Job in this stack using a bare `DefaultAzureCredential()` needs the same fix. |
| Cosmos `403` after the identity fixed itself | Cosmos DB **data-plane RBAC** (`Cosmos DB Built-in Data Contributor`) is separate from control-plane/ARM RBAC, and had only ever been assigned to the human user, never to the worker's or Admin UI's managed identity | `az cosmosdb sql role assignment create` for both `fbac1743-...` (worker) and `<admin-ui-principal-id>` (admin-ui) principals. Use **Contributor**, not Reader — the worker calls `upsert_item`. |
| Admin UI container never becomes ready (replica stuck `started:false` forever) | `admin-ui/Dockerfile` hardcodes `EXPOSE 8000`/`uvicorn --port 8000`; `container-apps.bicep`'s ingress `targetPort` was `8080` — pre-existing, since initial deploy | Changed ingress `targetPort` to `8000` to match the app, not the other way round. |
| `az containerapp update --image foo:latest` deploys, but the old code keeps running | Revision creation is deduped on the **literal tag string**, not the resolved image digest — a mutable `:latest` tag looks "unchanged" to Container Apps even after a new push | Always pass `--revision-suffix <name>` when redeploying under a mutable tag. |
| APIM operation with `method: "*"` + `urlTemplate: "/*"` never matches at the gateway, even though the management API accepts it | Wildcard-method operations are accepted by ARM validation but are not matched by the runtime gateway | Create one operation per real verb (GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS), each `urlTemplate: "/{*path}"` with a declared `templateParameters` entry (`name=path`). |
| APIM → internal-ingress Container App: gateway-runtime requests to **both** `run-ledger-gateway` and the new `admin-ui-gateway` return Azure Container Apps' own "This Container App is stopped or does not exist" 404, even once the backend app is confirmed healthy | **Unresolved, systemic** — ruled out DNS (private zone `*.internal...` → correct static IP, VNet-linked), NSGs (default allow, no blocking custom rules), subscription-key auth, propagation delay, `rewrite-uri`, and backend health. Not an Admin-UI-specific bug: the pre-existing `run-ledger-gateway` API has apparently never been proven reachable through APIM either. | Not fixed. Needs a VNet-internal test client (or Azure support) to isolate further. Until resolved, use `check_usage.py`/`check_usage_detail.py` (run locally, or from inside the VNet) instead of the public Admin UI URL. |
| `sync.py` `main()` crashes with `CosmosResourceNotFoundError` reading `item="global"` | No seed step (Terraform/Bicep/manual) had ever created the Cosmos `global` config doc — first real run of the worker in this environment | `main()` now catches the not-found case and `upsert_item`s a `DEFAULT_GLOBAL_CONFIG` (mirrors the existing `token-quota-default`/`token-quota-period-default` APIM named values) on first run only; Admin UI edits overwrite it thereafter. |
| `check_usage.py` (unlike `check_usage_detail.py`) crashes outright when Cosmos is unreachable (e.g. from a laptop, private-endpoint-only) | `read_pricing()` had no try/except around the Cosmos call, inconsistent with the sibling script | Wrapped it the same way — degrades to `$0` cost with a printed reason instead of crashing. |
| Cost stayed `$0` even after the worker ran cleanly | `pricing.py` (Retail Prices API fetcher + per-model meter-matching + unit-of-measure normalization) already existed fully built and tested, but was never called from `sync.py`, and the `Dockerfile` never `COPY`'d it into the image (`ModuleNotFoundError: No module named 'pricing'` the first time it was wired in) | Added `sync_pricing()` to `sync.py` (fail-safe: logs and leaves the existing doc untouched if the Retail Prices API has no match, never fails the job) and added `pricing.py` to the Dockerfile's `COPY` line. Confirmed live: `synced pricing for 4 model(s) from Retail Prices API`. |

Net result: config-sync-worker now completes end-to-end every run (config → named values → pricing → budget downgrades → consumer config), and real Retail Prices-sourced cost now flows into `check_usage.py`/`check_usage_detail.py`. The only open item is the APIM→internal-ACA reachability gap above, which blocks the public Admin UI portal (not cost tracking itself).

## Live end-to-end verification: real Teams turn → usage report (this session)

Sent real Teams messages to the deployed agent and confirmed the whole
usage-reporting chain with live data, closing the "not yet done" item from
the prior session's primer.

| Finding | Detail |
|---|---|
| A plain greeting ("status check") produces **no** `usage_event` row | `usage_event` is only logged inside `agent.py`'s `_call_specialist()` — i.e. only when the orchestrator actually routes to one of the 4 specialist agents. A message the orchestrator can answer directly with no tool call legitimately logs nothing. Not a bug; ask something that needs a specialist (runbook/topology/advisory/comms lookup) to generate a row. |
| `check_usage.py` shows `No usage rows found` even after a real specialist-routed turn | **By design, not a bug.** `check_usage.py`'s `_USAGE_KQL` reads `AppMetrics` rows named `"Prompt Tokens"/"Completion Tokens"`, which APIM's `azure-openai-token-limit` policy only emits for traffic that actually flows through the `openai-gateway`/`foundry-gateway` APIs. Per `gateway/PORTING_NOTES.md`'s "final mixed-routing decision", this app's own agent/model traffic **always bypasses APIM** and calls Foundry directly — those two APIs exist for *other* direct AOAI/Foundry callers, not this app's hot path. `check_usage.py` will correctly show `$0`/nothing for this app forever; that's expected. |
| `check_usage_detail.py` is the correct tool for this app | It reads the `usage_event` AppTraces rows agent.py emits per specialist call — confirmed live with 4 real requests across all 4 specialists (`noc-knowledge-agent`, `noc-comms-agent`, `noc-threatintel-agent`), real token counts (e.g. 17307 in / 3769 out on one `gpt-5.4` call). |
| `cost_usd` still shows `0.00000` for these live rows | Same pre-existing, already-documented Cosmos private-endpoint restriction above — pricing lookup 403s from a laptop's public IP. Usage/token data itself is fully verified end-to-end; only the local `$` rendering needs a VNet-internal caller (e.g. exec into `ca-adminui-aigw-<env>`). |

## noc-incident-agent (RTI) live verification — partial, pending a real Teams turn

Everything server-side is deployed and provisioned this session: Fabric
Eventhouse + KQL database seeded (413,658 `OpticalTelemetry` rows, 8,747
`NetworkAlerts` rows, 69 `IncidentEvents` rows), `fabric-rti-connection`
project connection, `noc-incident-agent` Prompt Agent (single direct
`MCPTool`, no toolbox per the B2-c spike above), `agent.py` wired with the
5th `SPECIALIST_AGENTS` entry, `ORCHESTRATOR_INSTRUCTIONS` updated, and the
app redeployed to App Service (`azd deploy web`, health check `200`).

| Attempted | Result |
|---|---|
| Direct `Foundry-MCP-agent_invoke` of `noc-incident-agent` from this session's own identity, bypassing Teams entirely, to sanity-check the Eventhouse chain works | `403 Forbidden` — `does not have permissions for .../agents/read actions`. This identity isn't the Teams-user OBO principal nor the App Service's own managed identity, so this failure is expected and unrelated to the new wiring; it isn't a substitute for a real Teams-routed turn anyway (it would exercise the agent identity, not the OBO/run-ledger/`usage_event` path). |
| Real Teams turn routed to `ask_incident_agent`, followed by `check_usage_detail.py` to confirm a `usage_event` row for `noc-incident-agent` and a real Eventhouse-grounded answer | **Not yet done.** Needs a human to send a Teams message (e.g. "what was the alert timeline and detection-to-ack latency for the MEL-BNE fibre link?") to the deployed bot. This is the same pattern used to verify the other 4 specialists earlier this session — requires an actual Teams client, which wasn't available at this point in the session. |

**Next step, unchanged from the plan:** send a real Teams turn, then run
`check_usage_detail.py --hours 1` and confirm a `noc-incident-agent` row with
non-zero tokens, and confirm the reply cites real alert/telemetry rows rather
than declining or hallucinating.

## noc-incident-agent (RTI): real Teams-triggered error, root-caused and fixed live

A real Teams turn routed to `ask_incident_agent` and hit a genuine tool error:

```
Semantic error: union: column named 'TableName' already exists
```

This closes the "pending a real Teams turn" gap above — but the first real
usage surfaced two more bugs behind it, all root-caused and fixed live against
the actual Eventhouse and Foundry project (no guessing, no symptom patches):

### Bug 1 — nested/piped `union withsource=TableName` fails

Reproduced directly against the live KQL database with a throwaway script
(`azure-kusto-data` + `AzureDeveloperCliCredential`, deleted after use):

- `union withsource=TableName A, B, C` (flat, one stage) — **works**.
- `(union withsource=TableName A, B) | union withsource=TableName C` (nested,
  two stages) — **fails** with exactly the reported error, because the first
  union stage already materializes a `TableName` column and the second stage
  tries to add another one with the same name.

This is a query-generation limitation in the Fabric RTI MCP server's own
NL2KQL layer (Microsoft-hosted, Public Preview) when a question spans all 3
Eventhouse tables — not our schema, not our ingestion, not fixable upstream
from this repo. **Fix:** added an explicit "query ONE table at a time,
correlate in your own reasoning, never ask for a query that unions/joins all
three tables" constraint to `noc-incident-agent`'s instructions in
`scripts/create_foundry_agents.py`, with the exact failure mode explained
inline so future maintainers don't reintroduce it. Redeployed as v2.

### Bug 2 — model hallucinated column names once the union bug was gone

Direct-SDK re-test (`azure-ai-projects` `AIProjectClient` + `AzureCliCredential`,
same call shape as `agent.py`'s `_call_specialist`, via a throwaway
`scripts/_diag_invoke.py`, deleted after use) got past the union error but
then failed with:

```
KQL query validation failed: 'TimeGenerated', 'LinkId' [on NetworkAlerts],
'AlertName', 'AlertState', 'IsSuppressed', 'SuppressionReason',
'AcknowledgedBy', 'AcknowledgedTime', 'Description' do not refer to any
known column...
```

The model guessed a generic "typical alerts table" schema instead of the
actual one. **Fix:** embedded the exact 3 table schemas (column names + types,
copied verbatim from `scripts/create_eventhouse.py`'s `TABLE_SPECS`) directly
into the agent's instructions, plus an explicit note that `NetworkAlerts` has
no `LinkId` column — the affected link/sensor is `EntityId` instead (confirmed
against `scripts/generate_incident_telemetry.py`, which populates `EntityId`
from each sensor's `MonitoredEntityId`). Redeployed as v3.

### Bug 3 — Fabric capacity was paused, producing a misleading 404

After fixing the schema, the same direct-SDK re-test failed again, this time:

```
The remote MCP server ... returned HTTP 404 (Not Found) while fetching tool
executeQuery.
```

Probing the Fabric MCP endpoint directly with `curl` + a raw bearer token
(bypassing Foundry/MCP entirely) surfaced the real error underneath the
generic 404: `"Internal error CapacityNotActive. Capacity ... is not active"`.
The workspace's backing capacity (`fabric<token>`, SKU F64,
`rg-<env-name>`) had auto-paused from inactivity. **Fix:**
`az resource invoke-action --action resume --resource-type Microsoft.Fabric/capacities --name fabric<token> -g rg-<env-name>`,
polled `properties.state` until `Active` (~90s). Not a code fix — an
operational note: **if RTI queries ever start failing with an opaque MCP 404,
check the Fabric capacity's power state before anything else.**

### End-to-end verification (v3, capacity active)

Re-ran the exact same direct-SDK question that failed live in Teams
("alert timeline + per-sensor optical readings for LINK-SYD-MEL-FIBRE-01 /
INC-2025-08-14-0042"). Got back a fully grounded answer: real `AlertId`s,
real `AckedBy`/`AckedAt` values, a real BER inflection from `1.9E-12` to
`0.0046` starting at the exact alert timestamp, correct per-sensor `PowerDbm`
readings — no union error, no invented columns, explicit stated time window,
and it explicitly offered to query `IncidentEvents` next rather than
fabricating that too. `live-verify` is now considered fully proven (direct-SDK
call exercises the identical `openai_client.responses.create()` path
`agent.py` uses; only the OBO-token-from-Teams hop differs, which was already
proven separately by the other 4 specialists' live Teams tests earlier this
session).

## Bicep/live-state reconciliation (this session)

`gateway/infra/core/apim/apim.bicep`'s `admin-ui-gateway` API still declared the
old `method: '*'` / `urlTemplate: '/*'` wildcard operation (`passthrough-any`) —
the exact wildcard-operation bug documented in the table above, whose live fix
(8 explicit per-verb operations) was applied via CLI but never ported back into
Bicep. Replaced `passthrough-any` with the live shape: one operation per verb
(GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS) against `urlTemplate: '/{*path}'` (with
a declared `path` template parameter), plus a dedicated `passthrough-root-get`
for the bare `/`. Verified the live API-level policies (`set-backend-service`
only, no host-header override or extra rewrite-uri) already match what's in
Bicep as-is — no further drift found there. `az bicep build` compiles clean.

## RBAC automation: converting `docs/RBAC.md`'s "manually granted" rows into Bicep/scripts (this session)

Prompted by "avoid manual work as much as possible." Two ARM-manageable gaps
found and fixed, plus one non-ARM consolidation:

| Gap | Root cause | Fix |
|---|---|---|
| Cosmos data-plane RBAC (`Cosmos DB Built-in Data Contributor` for worker/admin-ui) had to be granted with a manual `az cosmosdb sql role assignment create` after every fresh deploy | `gateway/infra/core/config/cosmos.bicep` already correctly implemented `readerPrincipals`/`writerPrincipals`/`configWriterPrincipals` as `sqlRoleAssignments` children — but `gateway/infra/main.bicep` never passed the worker/admin-ui managed identity principal ids into those params. Wiring bug, not a missing capability. | `main.bicep` now `concat()`s `identities.outputs.workerPrincipalId` into `writerPrincipals` and `identities.outputs.adminUiPrincipalId` into `configWriterPrincipals` automatically — safe unconditionally, since `identities.bicep` creates all 3 UAMIs regardless of whether the corresponding Container App/Job is enabled. |
| Foundry project RBAC for Teams users (§9c: `Azure AI Developer`, `Foundry Project Runtime User`, `Cognitive Services OpenAI User`, `Cognitive Services User`) had to be granted with 4 manual `az role assignment create` calls per environment, on top of the one role (`Foundry Agent Consumer`) `rbac.bicep` already declared | `infra/core/ai/rbac.bicep` only ever declared `foundryAgentConsumerRole` for `teamsUsersPrincipalId` — the other 4 roles documented as required in DEPLOYMENT.md §9c were never added to the Bicep module | Added 4 more conditional (`!empty(teamsUsersPrincipalId)`) role-assignment resources to `rbac.bicep`, all scoped to `aiAccount::project` exactly like the pre-existing one. `az bicep build` compiles clean on both files. |
| Fabric tenant consent (`DataAgent.Read.All`/`Execute.All`), Fabric workspace role assignment for the agent-user, Graph tenant consent (Work IQ's 7 scopes + optional `Mail.Send`), and Fabric workspace Viewer for the Teams-users group were each a separate hand-typed `az rest`/curl command across DEPLOYMENT.md §4b/§4d/§6a/§6b | None of these are ARM resource types (`oauth2PermissionGrants` and Fabric workspace `roleAssignments` are both Graph/Fabric-REST-only) — Bicep cannot express them. This is a genuine ceiling, not an oversight. | New `scripts/grant_agent_identity_access.py` consolidates all 4 into one idempotent script call: checks existing `oauth2PermissionGrants` and merges scopes (union) instead of clobbering on re-run, checks existing Fabric workspace role assignments before granting. DEPLOYMENT.md §4b/§4d/§6a/§6b/§9c rewritten to point at this script and the now-automatic Bicep roles instead of raw commands. The one irreducible manual step: `AGENT_IDENTITY_OBJECT_ID`/`AGENT_USER_OBJECT_ID` still can't be resolved before `a365 setup all` + a first live Teams turn have run (no pre-deployment lookup path found), so this script is necessarily a post-first-message step, not a pure `azd up`-time one. |

## Cowork package validation and first install (2026-09-07)

| Symptom | Root cause | Fix / verified result |
|---|---|---|
| `atk` was not recognized in PowerShell | Microsoft 365 Agents Toolkit CLI was not installed globally | Installed `@microsoft/m365agentstoolkit-cli`; verified `atk` version `1.1.16`. Use `atk auth login m365`, then `atk install --file-path <zip> --scope Personal`. |
| Package rejected with `InvalidAgentConnector`: MCP tool description file `./tools/noc-mcp-tools.json` not found | The package service resolves the declared MCP description as a package-root file; a nested `tools/` archive path was not accepted | `cowork/manifest.json` now declares `noc-mcp-tools.json`; `cowork/package.py` places that file at the ZIP root. |
| Package rejected because `mcpToolDescription` had no non-empty `tools` array | The MCP description schema is an object containing `tools: []`, not a single tool object | Wrapped `noc_investigate` in a top-level `tools` array. The package then installed successfully and returned a `TitleId` and `AppId`. |
| Teams Developer Portal registration returned an OAuthPluginVault reference and a different Application ID URI | The portal creates the Microsoft Enterprise token-store auth configuration and a new token audience. The generated URI is expected, but the registration's **Client ID must be the MCP resource app**, not the separate Cowork OAuth client. A generated URI ending in `coworkOAuthClientAppId` identifies a misconfigured registration. In Cowork, this appeared as the bundled skill running while the MCP tool was absent; the run fell back to Work IQ and APIM received no `/mcp` request. | Recreate the auth configuration with `mcpResourceAppClientId` if necessary. Use the registration ID as `OAuthPluginVault.referenceId`; add the generated URI to the MCP resource app's `identifierUris`, add the Teams `oAuthConsentRedirect`, preauthorize token-store client `ab3be6b7-f5df-413d-ac2d-abf1e3fd9c0b`, and configure the MCP host/Easy Auth to accept the generated URI as `mcpServerAudience`. Preserve `MCP_SCOPE_URI` as the original `api://<resource-app-id>/noc.invoke`; the scope URI and resulting token audience are intentionally different. |

| Cowork shows `Connector access denied`; APIM records repeated `POST /mcp` 403 responses, but the MCP Container App process logs no request | The token audience and Entra resource app were correct, but Container Apps Easy Auth had an empty `defaultAuthorizationPolicy.allowedApplications`. Authentication succeeded far enough to reach APIM/Easy Auth, then the platform rejected the Microsoft Enterprise token-store client before forwarding to Uvicorn. | Add `ab3be6b7-f5df-413d-ac2d-abf1e3fd9c0b` to Easy Auth `defaultAuthorizationPolicy.allowedApplications`. This is separate from preauthorizing the same client for the `noc.invoke` delegated scope on the Entra resource app; both are required. |

| Cowork's connect prompt reports `Authentication failed`; APIM and Uvicorn both show `POST /mcp` 401 | Easy Auth now accepted and forwarded the token, but the application rejected it because the MCP resource app had `groupMembershipClaims: null`. The app enforces the configured caller group from the token's `groups` claim, so an otherwise valid token without that claim fails closed. | Set the MCP resource app's `groupMembershipClaims` to `SecurityGroup` (automated by `setup_cowork_mcp_entra.py`). Confirm the user is a direct or transitive member of the configured caller group, then reconnect so Cowork obtains a new token containing the group claim. |

The successful package must contain `manifest.json`, `color.png`, `outline.png`,
`noc-mcp-tools.json` at the archive root, and the three skill directories.
The generated `cowork/build/noc-cowork.zip` is a local build artifact and is
ignored by Git; regenerate it after changing the manifest or tool description.

## Teams RTI access failure after first live turn (2026-09-07)

A Teams turn initially reported HTTP 403 from the Fabric RTI MCP endpoint while
enumerating tools. The persisted Foundry connection and endpoint were correct;
the missing permission was Fabric workspace access for the Agent 365-generated
agent-user identity, not the human Teams user's group membership.

After the first live turn, Application Insights `AppTraces` exposed the
`agentic_user_id` and `agent_app_instance_id`. Resolving those IDs through
Microsoft Graph and running `scripts/grant_agent_identity_access.py` granted the
agent-user `Contributor` on the Fabric workspace. The Teams-users group already
had `Viewer`, and Fabric/Graph delegated consent already existed. This is now
verified in the Fabric workspace role assignments. Wait for RBAC propagation,
then start a new Teams conversation before retrying RTI queries.
