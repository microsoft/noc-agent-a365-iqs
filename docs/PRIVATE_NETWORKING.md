# Network-isolated deployment and direct data tools

> **Status:** written and statically validated (Bicep build, unit tests), **not yet deployed**.
> Everything here is opt-in. Without the switch, `azd up` deploys the current default mode unchanged.

Features ported from two sibling repos:

| Feature | Source |
|---|---|
| Private endpoints, Foundry network injection, ACI jumpbox | [foundry-iq-v2](https://github.com/pranabpaul-tech/foundry-iq-v2) |
| Direct read-only Lakehouse T-SQL tool | foundry-iq-v2 |
| Fabric workspace private link + public-access policy | [pdm-fabric-foundry](https://github.com/pranabpaul-tech/pdm-fabric-foundry) |
| Guarded read-only KQL tool, code-computed baselines | pdm-fabric-foundry |
| Fabric Operations Agent (rule-based monitor) | pdm-fabric-foundry |
| Approval gate for write actions | pdm-fabric-foundry |
| Private Foundry open/close script | pdm-fabric-foundry |

Not ported: moving the orchestrator from App Service to a Foundry hosted agent (and its
Teams-route deploy guardrail). NOC keeps Teams → Bot → App Service → MAF.

## 1. The deployment switch

```bash
# Default mode (current behaviour)
scripts/deploy.sh                      # or: ./scripts/deploy.ps1

# Network-isolated mode
scripts/deploy.sh --network yes        # or: ./scripts/deploy.ps1 -Network yes
```

Both wrappers only set `NETWORK_ISOLATION` in the azd environment and run `azd up`
(arguments after `--` go to `azd up`). The equivalent by hand is
`azd env set NETWORK_ISOLATION true && azd up`. Use a new azd environment for network mode:
Foundry VNet injection is fixed when the Foundry account is created.

| | Default | `--network yes` |
|---|---|---|
| Foundry account | public | `publicNetworkAccess: Disabled`, private endpoint (`account`), Agent Service injected into `agent` subnet |
| Azure AI Search | public | `publicNetworkAccess: disabled`, private endpoint (`searchService`) |
| Storage | private endpoint (unchanged) | same |
| App Service | VNet-integrated, route-all (unchanged) | same; reaches Foundry/Search over private endpoints |
| VNet `vnet-agent-*` | `10.42.0.0/24` | + `agent` `10.42.1.0/24` (Microsoft.App/environments) + `jumpbox` `10.42.2.0/27` (ACI) |
| Jumpbox | none | `ci-noc-jump-*` Azure CLI container, `az container exec` |
| Private DNS zones | blob | + cognitiveservices, openai, services.ai, search |

Teams keeps working in network mode: Bot Service → App Service (public endpoint) →
VNet integration → Foundry private endpoint.

## 2. Setup steps that need the private data plane

With network mode, laptop calls to Foundry/Search return `403 Public access is disabled`.
Choose one:

**Jumpbox** (preferred):

```bash
az container exec -g <rg> -n <JUMPBOX_NAME> --container-name jumpbox --exec-command "/bin/sh"
# inside: az login; git clone this repo; pip install -r scripts/requirements.txt;
# run create_foundry_iq_kb.py, create_foundry_agents.py, refresh_iq_connections.py
```

**Open briefly, then close:**

```bash
python scripts/foundry_access.py open     # account + Search public; wait a few minutes
python scripts/create_foundry_agents.py   # etc.
python scripts/foundry_access.py close
python scripts/foundry_access.py status
```

Tenant governance may force `publicNetworkAccess` back to `Disabled`; if `open` does not stick,
use the jumpbox.

Agent Service egress: Web IQ, Work IQ and Fabric public endpoints are reached from the
injected `agent` subnet through default outbound access. If your policy removes default
outbound, add a NAT gateway to that subnet.

## 3. Fabric workspace private link (optional, separate step)

Run after the Fabric workspace exists (`FABRIC_WORKSPACE_ID`):

```bash
az provider register --namespace Microsoft.Fabric
az deployment group create -g <rg> -f infra/fabric-privatelink.bicep \
  -p workspaceId=$FABRIC_WORKSPACE_ID location=<region> \
     vnetId=<AGENT_VNET_ID> peSubnetId=<AGENT_PE_SUBNET_ID>
```

From the jumpbox, confirm `<workspaceId without dashes>.z<first two chars>.w.api.fabric.microsoft.com`
resolves to a private IP (a new capacity can take up to 24 hours to appear). Only then:

```bash
python scripts/fabric_workspace_network_policy.py --status
python scripts/fabric_workspace_network_policy.py --deny    # --allow to undo; ~30 min to apply
azd env set FABRIC_API_BASE_URL https://<workspaceId without dashes>.z<xy>.w.api.fabric.microsoft.com
azd provision
```

`FABRIC_API_BASE_URL` is used by the direct Fabric Graph path. Keep a tenant-level private link
or a public client available: a workspace-private-link-only client cannot run `--allow` if
tenant public access is also restricted.

## 4. Direct read-only data tools

Registered on the MAF orchestrator only when enabled; they run as the App Service's
**service identity**, so they bypass per-user Fabric security. Grant that identity read-only
access only (Eventhouse database *viewer*, workspace *Viewer*).

| Tool | Enable | Needs |
|---|---|---|
| `query_eventhouse_kql` | `azd env set directKqlToolEnabled true` | `FABRIC_KQL_QUERY_URI`, `FABRIC_KQL_DATABASE_NAME` |
| `check_optical_baseline` | same flag | same |
| `query_lakehouse_sql` | `azd env set directSqlToolEnabled true` | `FABRIC_SQL_ENDPOINT` (Lakehouse `sqlEndpointProperties.connectionString`), `FABRIC_SQL_DATABASE` (Lakehouse name), ODBC Driver 18 on the host |

Guards (`agent/data_tools.py`): KQL uses `execute_query` (never runs `.` management commands),
blocks `externaldata`, `evaluate`, `http_request`, `sql_request`; SQL allows one `SELECT`/`WITH`
statement and blocks write/exec keywords; both cap at 200 rows and return `as_of_utc`.
`check_optical_baseline` computes per-sensor PowerDbm/Ber baselines in code (ABNORMAL needs
|z| ≥ 3 **and** ≥ 5 % change), trend per hour, optional hours-to-threshold, and data age.
The orchestrator is told to quote those labels rather than judge them itself.
`query_lakehouse_sql` is only used when the user explicitly asks for raw SQL; the default
topology route stays `ask_topology_agent` / direct Graph.

## 5. Fabric Operations Agent

Rule-based monitor over the NOC Eventhouse (`data/ops-agent/OperationsAgent.json`): sustained
optical power drop vs own baseline, BER spike, silent sensor, unacknowledged P1. Notify only,
6-hour de-duplication per link. It complements `agent/incident_monitor.py`, which triggers the
MAF investigation.

```bash
az login                                              # real user, not a service principal
python scripts/create_operations_agent.py --apply     # needs FABRIC_WORKSPACE_ID, FABRIC_KQL_DB_ID
# then in the portal: Generate Playbook, set the Teams recipient, Start
python scripts/create_operations_agent.py --capture <id>   # pull portal edits back
python scripts/create_operations_agent.py --update <id>    # push rule edits, keeps the playbook
```

Not available in East US, sovereign clouds or CMK-encrypted workspaces.

## 6. Approval gate

`agent/approval.py` stores approve/deny tokens (3-day expiry) in the existing agent-state Blob
container under `approvals/`. Any future write tool must call
`ApprovalGate.require_approved(approval_id)` first. No existing tool is gated yet.

## 7. Validation done

- `az bicep build` for `infra/main.bicep` and `infra/fabric-privatelink.bicep`
- `pytest agent/test_data_tools.py agent/test_approval.py`
- Not yet: a live `--network yes` deployment, Fabric private link DNS, Operations Agent creation.
