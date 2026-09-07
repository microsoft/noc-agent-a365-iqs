# TokenOps gateway architecture — `feat/tokenops-gateway`

What this branch adds in front of Foundry, why it's split the way it is, and what does not port.
For the infra port rationale and file-by-file decisions see `gateway/PORTING_NOTES.md` (the
build log). This doc is the "why", not the "what changed" — read PORTING_NOTES.md for that.

## Topology

```
Teams / M365 Copilot → Azure Bot Service → App Service (agent/agent.py, MAF)
                                                │  x-run-token / x-agent / x-step
                                                ▼
                                    APIM (StandardV2, public ingress + VNet integration)
                              consumer ID • allowed-model • rate limit
                              validate-jwt(run_id) • llm-token-limit(run_id)
                              precall/postcall → run ledger (ignore-error)
                                                │
                        ┌───────────────────────┼───────────────────────┐
                        ▼                       ▼                       ▼
                  Azure OpenAI            Foundry / AI Services    Run Ledger (Container App)
                  (private endpoint)      (private endpoint)       + Azure Managed Redis (Entra auth)
                                                                    HINCRBY inflight, decide(), lease

  Config-sync worker (Container Apps Job) ──KQL(Log Analytics)──> spend/downgrade → APIM named values
  Cosmos (pricing, consumer bundles, run policy) ←── Admin UI (BFF + SPA, Entra-gated) ──> operator
  Retail Prices API ──refresh──> Cosmos pricing doc (unit-of-measure asserted, manual override kept)
```

## Division of labour: gateway vs. run ledger

Two systems answer different questions. Neither is redundant — the gateway is the only thing
that cannot be bypassed (every model call crosses it); the run ledger is the only thing that
knows a *run* exists and enforces its budget across every hop of that run.

| Question | Answered by |
|---|---|
| "May this app call this model at all?" | APIM (allowed-model policy) |
| "Is the tenant/consumer being flooded?" | APIM (rate limit / TPM named values) |
| "Is the app over its daily USD budget?" | Config-sync worker (KQL → downgrade named value) |
| "Can this call be made without a model key?" | APIM (managed-identity backend auth) |
| "Is this specific run over its budget?" | Run ledger (`/v1/precall`, `budget_micros`) |
| "Is this run's concurrency/step cap exceeded?" | Run ledger (`decide()` — `step_cap`, `concurrency_cap`) |
| "Should this run be halted or queued?" | Run ledger decision, surfaced by APIM (`x-run-halt-reason`, 403/429) |
| "What did this run actually cost?" | Run ledger (`/v1/postcall` settle) → Admin UI Runs page |

`llm-token-limit` keyed on the validated `run_id` claim is the durable backstop: it holds even
if the run ledger is down, because `send-request` to `/v1/precall` uses `ignore-error="true"`.
Losing the ledger degrades the gateway to tenant-level governance, not to no governance.

## Why a run ledger exists when APIM already meters tokens

APIM's own cache (`cache-store-value`) is a non-atomic read-modify-write — it cannot safely hold
an inflight-reservation counter under concurrent requests from the same run. The run ledger uses
Redis `HINCRBY`, which is atomic, specifically for that reservation. This is the one property
APIM's built-in cache cannot provide, and it's the reason the ledger is a separate service rather
than more APIM policy.

## What does NOT port (explicitly out of scope)

| Capability | Why it's out |
|---|---|
| Mid-stream cancellation of a runaway generation | No portable hook once a hosted Foundry turn is streaming; would need SSE-level intervention not exposed here |
| True-semantics `progress_guard` | Only a prompt-hash heuristic is portable through a gateway; sequenced last, accepted false-positive budget |
| Semantic `context_compaction` with pinning | Requires app-level context awareness the gateway doesn't have |
| Spend that never crosses APIM (local tools, in-process RAG) | Gateway and ledger only see what's routed through them |
| Cosmos jumpbox VM seeding (from the source Terraform) | Deliberately not ported — `az cosmosdb sql` CLI or a Container Apps Job one-off fits this repo's existing patterns better |

## Auth model notes worth remembering

- Run-ledger's own Redis connection uses **Entra ID auth**, matching the repo's no-keys posture.
- APIM's *own* cache (unrelated to the run ledger, if ever used) would still need key auth — a
  documented, accepted exception, not an oversight.
- The run token is a JWT signed with a Key Vault-held key, validated by APIM's `validate-jwt`
  against the `run_id` claim — never trust a client-supplied `run_id` directly.

## Branch A interaction

This branch was cut from `master` before Branch A's 4-agent rewrite landed, so `agent/agent.py`
here is still the single-toolbox architecture — `x-agent`/`x-step` headers are wired but carry
less meaning until the two branches merge. On merge, `x-agent` should be set per specialist
Prompt Agent call, which is a strict improvement, not a conflict to resolve carefully.

## Status

Layers 1–3 are code-complete and independently verified (`az bicep build`, XML well-formedness,
and every pytest suite re-run outside the authoring agent's own report). **Nothing has been
deployed yet** — deployment is a deliberate separate step once docs land.
