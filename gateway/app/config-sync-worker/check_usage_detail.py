"""Ad-hoc CLI: print per-request usage (user, query, agent, token breakdown, est. cost).

Reads classified `usage_event` traces emitted by agent.py for specialists,
the orchestrator, automatic synthesis, and zero-LLM direct Graph execution.
It joins actual/estimated token rows with Cosmos pricing when reachable, then
falls back to the Azure Retail Prices API. This keeps reconciliation usable
after a teardown or from outside the private Cosmos network.

Usage:
  az login
  python check_usage_detail.py --workspace-id GUID [--hours 24] [--run-id ID]

Requires azure-identity and azure-monitor-query plus Log Analytics Reader.
Cosmos access is optional when Retail Prices fallback is available.
"""
import argparse
import datetime
import os

from azure.identity import DefaultAzureCredential
from azure.monitor.query import LogsQueryClient, LogsQueryStatus

import budget
import pricing as retail_pricing
from check_usage import read_pricing, DEFAULT_COSMOS_ENDPOINT

# Orchestrator App Insights workspace (azd sets APPLICATIONINSIGHTS_WORKSPACE_ID).
DEFAULT_WORKSPACE_ID = os.environ.get("APPLICATIONINSIGHTS_WORKSPACE_ID", "")

_KQL = (
    "AppTraces | where Message == \"usage_event\" "
    "| extend p = parse_json(Properties) "
    "| project TimeGenerated, "
    "user_name = tostring(p.user_name), user_id = tostring(p.user_id), "
    "agent = tostring(p.agent), model = tostring(p.model), query = tostring(p.query), "
    "usage_kind = coalesce(tostring(p.usage_kind), 'specialist'), "
    "accounting_mode = coalesce(tostring(p.accounting_mode), 'actual'), "
    "input_tokens = toint(p.input_tokens), output_tokens = toint(p.output_tokens), "
    "cached_tokens = toint(p.cached_tokens), reasoning_tokens = toint(p.reasoning_tokens), "
    "run_id = tostring(p.run_id) "
    "| order by TimeGenerated desc"
)


def query_events(cred, workspace_id: str, hours: int) -> list[dict]:
    client = LogsQueryClient(cred)
    resp = client.query_workspace(workspace_id=workspace_id, query=_KQL,
                                   timespan=datetime.timedelta(hours=hours))
    tables = resp.tables if resp.status == LogsQueryStatus.SUCCESS else (resp.partial_data or [])
    if not tables:
        return []
    cols = [str(c) for c in tables[0].columns]
    return [dict(zip(cols, row)) for row in tables[0].rows]


def resolve_pricing(cred, cosmos_endpoint: str, region: str) -> tuple[dict, str]:
    if cosmos_endpoint:
        try:
            cosmos_prices = read_pricing(cred, cosmos_endpoint)
            if cosmos_prices:
                return cosmos_prices, "Cosmos desired-state pricing"
        except Exception as exc:  # noqa: BLE001 -- fall back to public retail pricing
            print(f"(Cosmos pricing unavailable: {type(exc).__name__}; using Retail Prices API)\n")
    try:
        return retail_pricing.fetch_model_pricing(region), f"Azure Retail Prices ({region})"
    except Exception as exc:  # noqa: BLE001 -- retain token evidence without dollar rendering
        print(f"(Retail pricing unavailable, showing $0 cost: {exc})\n")
        return {}, "unavailable"


def event_cost(event: dict, prices: dict) -> float:
    if event.get("accounting_mode") == "no_llm" or not event.get("model"):
        return 0.0
    model_usage = {
        event["model"]: {
            "prompt": event.get("input_tokens") or 0,
            "completion": event.get("output_tokens") or 0,
        }
    }
    return budget.cost_for(model_usage, prices)


def summarize_runs(events: list[dict], prices: dict) -> dict[str, dict]:
    summaries: dict[str, dict] = {}
    for event in events:
        run_id = event.get("run_id") or "unscoped"
        summary = summaries.setdefault(
            run_id,
            {
                "rows": 0,
                "actual_tokens": 0,
                "estimated_tokens": 0,
                "no_llm_rows": 0,
                "actual_cost": 0.0,
                "estimated_cost": 0.0,
            },
        )
        summary["rows"] += 1
        tokens = (event.get("input_tokens") or 0) + (event.get("output_tokens") or 0)
        mode = event.get("accounting_mode") or "actual"
        if mode == "no_llm":
            summary["no_llm_rows"] += 1
        elif mode == "estimate":
            summary["estimated_tokens"] += tokens
            summary["estimated_cost"] += event_cost(event, prices)
        else:
            summary["actual_tokens"] += tokens
            summary["actual_cost"] += event_cost(event, prices)
    return summaries


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace-id", default=DEFAULT_WORKSPACE_ID)
    ap.add_argument("--cosmos-endpoint", default=DEFAULT_COSMOS_ENDPOINT)
    ap.add_argument("--pricing-region", default="eastus2")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()

    if not args.workspace_id:
        ap.error("no workspace: set APPLICATIONINSIGHTS_WORKSPACE_ID or pass --workspace-id")

    cred = DefaultAzureCredential()
    events = query_events(cred, args.workspace_id, args.hours)
    if args.run_id:
        events = [event for event in events if event.get("run_id") == args.run_id]
    if not events:
        print("No usage_event rows found in the requested window/run.")
        return 0
    prices, pricing_source = resolve_pricing(cred, args.cosmos_endpoint, args.pricing_region)

    header = (
        f"{'time':<20}{'run':<34}{'kind':<14}{'mode':<10}{'agent':<24}{'model':<12}"
        f"{'in':>8}{'out':>8}{'cached':>9}{'cost_usd':>11}"
    )
    print(header)
    for event in events:
        cost = event_cost(event, prices)
        print(
            f"{str(event['TimeGenerated'])[:19]:<20}{(event.get('run_id') or 'unscoped'):<34}"
            f"{(event.get('usage_kind') or 'specialist'):<14}{(event.get('accounting_mode') or 'actual'):<10}"
            f"{event['agent']:<24}{event['model']:<12}{event['input_tokens']:>8}{event['output_tokens']:>8}"
            f"{event['cached_tokens']:>9}{cost:>11.5f}"
        )

    print(f"\nPricing: {pricing_source}. Costs are estimates from metered token counts, not invoice reconciliation.")
    print(
        f"{'run':<34}{'rows':>6}{'actual_tokens':>16}{'estimated_tokens':>18}{'no_llm':>9}"
        f"{'actual_cost':>14}{'estimate_only':>15}"
    )
    for run_id, summary in sorted(summarize_runs(events, prices).items()):
        print(
            f"{run_id:<34}{summary['rows']:>6}{summary['actual_tokens']:>16}"
            f"{summary['estimated_tokens']:>18}{summary['no_llm_rows']:>9}"
            f"{summary['actual_cost']:>14.5f}{summary['estimated_cost']:>15.5f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
