---
page_type: sample
languages:
- python
- bicep
- typescript
products:
- azure
- azure-openai
- azure-ai-foundry
- microsoft-fabric
urlFragment: noc-agent-a365
name: noc-agent-a365
description: "A Microsoft Agent Framework (MAF) agent, hosted on Agent 365 (A365) for Teams"
---

# NOC/NOA IQ Agent — Sydney Fibre-Cut Demo

> ⚠️ **Proof-of-Concept — Not for Production Use**
> This repository is demo/PoC code built to illustrate an end-to-end
> Microsoft IQ (Foundry IQ, Fabric IQ, Web IQ, Work IQ) integration pattern.
> It has not been hardened for production: security review, error handling,
> scalability, monitoring, data-residency, and compliance controls suitable
> for production workloads are out of scope. Do not deploy this code as-is
> into a production environment — treat it as a reference implementation to
> adapt, not a deployable product.

A Microsoft Agent Framework (MAF) agent, hosted on Agent 365 (A365) for Teams
/ M365 Copilot, that triages network operations incidents using all four
Microsoft IQ surfaces: **Foundry IQ**, **Fabric IQ**, **Web IQ**, and
**Work IQ**. One MAF agent, four tools, model-driven routing — no Copilot
Studio, no hand-rolled dispatcher.

- **Scenario**: a Sydney↔Melbourne fibre cut (see `docs/SEQUENCE.md` §3 for
  the 5 narrative beats a correct end-to-end run must surface).
- **A365 hosting layer**: modeled on the Agent 365 Hosted-Agent pattern
  (`AgentApplication` + `AIAgentA365HostAgent` + `TokenExchange` middleware).
- **MAF + IQ tool-binding pattern**: a single MAF `Agent`/`FoundryChatClient`
  with one Foundry Toolbox bundling all 4 IQ connections behind one MCP tool
  (see `docs/ARCHITECTURE.md`).
- **Fabric ontology mechanics**: a Fabric `Ontology` + companion `GraphModel`
  item queried via a Fabric Data Agent, exposed to Foundry as an MCP
  connection (see `docs/DEPLOYMENT.md` §4).

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for component-level detail,
[`docs/SEQUENCE.md`](docs/SEQUENCE.md) for sequence diagrams (deployment +
runtime turn, including the 5 narrative beats in §3),
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for exact reproducible deployment
steps, and [`docs/OUTBOUND_NOTIFICATIONS.md`](docs/OUTBOUND_NOTIFICATIONS.md)
for the (optional, additive) agent-initiated multi-persona email broadcast
capability — separate from the 4 read-only IQ tools above.

## Architecture (deployed Azure estate)

![Azure architecture — VNet-injected APIM gateway, Foundry Prompt Agents, run-ledger governance](docs/images/azure-architecture.svg)

VNet-injected APIM sits in front of the AI Foundry project's 4 persisted Prompt
Agents (each with its own single-connection Toolbox: `foundry-iq`, `fabric-iq`,
`web-iq`, `work-iq`). A Container Apps environment hosts the `run-ledger`
(backed by Azure Managed Redis), the `ca-adminui` governance UI, and the
`config-sync-worker` job that reconciles Log Analytics spend + Cosmos pricing
into APIM named values. Private endpoints isolate Foundry, Key Vault, Cosmos,
and ACR inside `snet-private-endpoints`. Full component/edge legend is in the
diagram itself.

## TokenOps run-scoped governance — sequence diagram

![TokenOps mixed-routing sequence diagram — run ledger precall/postcall governance](docs/images/tokenops-sequence.svg)

Shows the mixed-routing path: direct `responses.create` calls from the
orchestrator to each Prompt Agent, with token usage reported to the run ledger
via `precall`/`postcall` regardless of credential kind (service credential vs.
OBO/`UserEntraToken`). Full narrative and all steps are in
[`docs/SEQUENCE.md`](docs/SEQUENCE.md) §3; PNG fallback at
[`docs/images/tokenops-sequence.png`](docs/images/tokenops-sequence.png).

For per-run reconciliation, first discover the `teams-...` or `monitor-...`
run ID from App Insights, then run
`gateway\app\config-sync-worker\check_usage_detail.py --workspace-id <guid> --run-id <id>`.
It reports per-step input/output/cached/reasoning tokens, actual model cost,
estimate-only usage, and zero-LLM direct Graph execution separately, using
Cosmos pricing when reachable or Azure Retail Prices as a fallback. See the
[numbered procedure and sample report](docs/DEPLOYMENT.md#complete-token-and-cost-breakdown-for-one-teams-turn-or-monitor-incident).

### Sample usage and cost glimpse

Example output for a seven-day report (`--hours 168`, Azure Retail Prices,
`eastus2`). Times are UTC; costs are estimated, not invoice reconciliation.

| Time | Agent | Input Tokens (in) | Output Tokens (out) | Cached Tokens | Cost (USD) | Note |
|---|---|---:|---:|---:|---:|---|
| 01:12:04 | `noc-comms-agent` | 12,698 | 1,964 | 1,920 | $0.06121 | Proactive specialist |
| 01:11:47 | `noc-incident-agent` | 3,808 | 1,031 | 1,536 | $0.02499 | RTI evidence |
| 01:11:25 | `noc-knowledge-agent` | 14,667 | 970 | 0 | $0.05122 | Foundry IQ |
| 01:11:08 | `noc-threatintel-agent` | 38,953 | 847 | 2,816 | $0.11009 | Web IQ |

**Sample run total:** 13 rows · 197,406 tokens · **$0.64933 estimated**.
For the complete per-row report, use the deployment procedure linked above.

## IQ auth-type matrix (read this before wiring connections)

| IQ surface | Auth type | Why |
|---|---|---|
| Foundry IQ | Service credential | Shared corpus, not user-scoped |
| Fabric IQ | `UserEntraToken` (OBO) | Row/item-level security is per-user |
| Web IQ | `CustomKeys` (`x-apikey`) | Public web data, identity-independent |
| Work IQ | `UserEntraToken` (OBO) | Reads the user's M365 data; app-only auth is blocked |

## Quick start

```bash
# From Git Bash on Windows
cd noc-agent-a365
azd env new noc-iq-demo
azd env set AZURE_LOCATION eastus2
azd env set webIqApiKey "<your Web IQ key>"
azd up
```

Full steps (Foundry IQ KB build, Fabric ontology + Data Agent, Web/Work IQ
connections, A365 publish, teardown) are in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Live Teams acceptance and four-IQ prompts

A greeting such as `hi` verifies Teams -> Agent 365/Bot -> App Service only.
Before enabling the proactive monitor, exercise the delegated Fabric/Work/RTI
connections in the same Teams chat and complete every **Sign in to ...** card.
Then send `/monitor subscribe` and verify `/api/health` reports
`monitor_subscribed: true`. See
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md#optional-detected-incident-monitor-disabled-by-default)
for the safe enablement and anomaly-replay commands.

Use these four partner-demo prompts:

1. **Foundry IQ — runbooks and history**
   > Using Foundry IQ only, identify the fibre-cut runbook and historical tickets relevant to LINK-SYD-MEL-FIBRE-01. Separate documented procedure from historical narrative and cite each source.
2. **Fabric IQ — topology plus RTI evidence**
   > Using Fabric IQ and RTI IQ, determine the endpoints, conduit, shared-conduit links, directly exposed services and SLAs for LINK-SYD-MEL-FIBRE-01, then give the exact IncidentEvents timeline and optical evidence for INC-2025-08-14-0042. Distinguish graph exposure from measured incident evidence.
3. **Web IQ — public intelligence**
   > Using Web IQ only, find one recent public vendor advisory relevant to telecom network operations. Give the title, vendor, publication date, URL, and operational relevance.
4. **Work IQ — Microsoft 365 context**
   > Using Work IQ only, find the current on-call or incident-bridge context available in my Teams and Outlook, and draft a concise NOC status update. Do not invent information that is not present.

Foundry IQ and Web IQ use service/key authentication. Fabric IQ, RTI IQ, and
Work IQ are user-scoped and may present separate short-lived consent cards;
open each card immediately and retry that prompt after consent.

## Repository layout

```
agent/        MAF agent (agent.py) + generic A365 host (host_agent_server.py, etc.)
infra/        Bicep — new RG, Foundry account/project, Search, Storage, App Insights,
              Fabric F2 capacity, App Service, project-scope RBAC
scripts/      create_foundry_iq_kb.py, create_fabric_ontology.py, create_fabric_data_agent.py
data/         NOC corpus: runbooks, tickets, equipment/infra specs,
              ontology_entities (CSVs), telemetry, topology
docs/         ARCHITECTURE.md, SEQUENCE.md, DEPLOYMENT.md, TROUBLESHOOTING.md
manifest/     Teams app manifest (generated by `a365 setup all`; gitignored)
```

## Data collection and telemetry

When `APPLICATIONINSIGHTS_CONNECTION_STRING` is configured, the sample sends
standard request, dependency, exception, and application-log telemetry to the
operator's Azure Application Insights resource. TokenOps `usage_event` records
also include the correlated run ID, user/object identifier and display name,
specialist/model name, token counts, and up to 500 characters of the submitted
query. This data is used for troubleshooting and estimated cost reporting; it
is not sent to the sample maintainers.

To opt out, omit or unset `APPLICATIONINSIGHTS_CONNECTION_STRING` before
starting or deploying the App Service. This disables Azure Monitor export and
therefore also disables the documented per-session usage/cost report. Operators
are responsible for configuring access control, retention, and redaction for
their own Application Insights workspace and for avoiding sensitive content in
test prompts.

## Cost & teardown

The Fabric F2 capacity is billable (~US$0.36/hr). Pause it when not
demoing. The resource group (and everything in it, including the F2
capacity) is deleted at teardown — see
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md#10-teardown-after-e2e-passes). The
Fabric workspace is a separate tenant object and must be deleted independently
of the resource group.

## Local dev hygiene

No `.venv`/`node_modules` are checked in — `.gitignore` covers `.venv/`,
`__pycache__/`, `.env`, `a365.config.json`, `a365.generated.config.json`, and
`.azure/`. Run `pip install -r agent/requirements.txt` (and
`scripts/requirements.txt` for the provisioning scripts) into a local venv
you create yourself; remove it when done (`rm -rf .venv`).

## Trademarks

This project may contain trademarks or logos for projects, products, or services.
Authorized use of Microsoft trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not
cause confusion or imply Microsoft sponsorship. Any use of third-party trademarks or
logos are subject to those third-party's policies.
