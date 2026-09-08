# Cowork component-level request sequence

This describes the implementation on `feat/cowork-mcp-channel` as of
2026-09-08. It separates the usual Foundry specialist path from the direct
Fabric Graph path used by focused link blast-radius/conduit requests.

## 1. Cowork to Foundry specialist to downstream tool

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant C as Copilot Cowork
    participant P as APIM /mcp
    participant H as ACA Easy Auth + MCP host
    participant E as Microsoft Entra
    participant O as In-process NocAgent / MAF
    participant F as Foundry Prompt Agent
    participant B as Agent MCPTool + project connection
    participant T as Downstream MCP server / tool

    U->>C: Ask NOC question / use Cowork skill
    opt First use or renewed sign-in
        C->>E: OAuth sign-in and consent for noc.invoke
        E-->>C: Access token for MCP resource
    end
    C->>P: MCP initialize and tools/list
    P->>H: Forward MCP protocol and authorization
    H-->>C: Advertise noc_investigate(task), via APIM
    C->>P: tools/call noc_investigate(task)
    P->>H: Forward unchanged
    H->>H: Validate Easy Auth principal, claims, scope and group
    H->>E: OBO exchange using UAMI federated assertion
    E-->>H: Calling-user token for Foundry
    H->>H: Establish run context and TokenOps reservation
    alt Focused prompt matched by keyword router
        H->>O: _call_specialist(key, task)
        Note over H,O: Skip outer orchestrator model turn
    else No focused route matched
        H->>O: AgentMCPTool invokes NocAgent._agent
        O->>O: Foundry-backed model selects local ask_* function tools
        Note over O,F: Each selected function invokes a specialist
    end
    O->>F: Responses API request to selected persisted agent
    F->>B: Use native MCPTool in agent definition
    Note over F,B: Binding/configuration, not a separate toolbox server
    B->>T: Discover tools and invoke selected MCP tool
    Note over B,T: Connection supplies downstream authentication
    T-->>F: Evidence / tool result through MCP binding
    F-->>O: Grounded answer and model usage
    O-->>H: Specialist answer or orchestrator synthesis
    H->>H: Record usage, close reservation, reset context
    H-->>P: MCP content blocks / error / consent link
    P-->>C: MCP response
    C-->>U: Present answer and citations
```

### Steps and responsibilities

| Stage | Component | What happens |
|---|---|---|
| 1. Select | Cowork | The skill guides the task and Cowork calls the single exposed tool, `noc_investigate`. Cowork does not directly call the five Foundry specialists. |
| 2. Enter | APIM `/mcp` | Transparent Streamable HTTP MCP proxy. Preserves authorization, protocol headers, body and authentication challenges; no model/token policy runs on this route. |
| 3. Authorize | Container Apps Easy Auth and `mcp_server.py` | Easy Auth validates the token; the app requires the injected principal and checks tenant, audience, scope, identity and allowed group. |
| 4. Delegate | Entra and MCP UAMI | The UAMI obtains a federated assertion for `api://AzureADTokenExchange`; OBO obtains a user token for `https://ai.azure.com/.default`. This currently happens before routing, including direct-Graph calls. |
| 5. Govern | MCP host and run ledger | Establish per-call context, mint the run token and request a precall decision when the ledger is configured. Halt/queue decisions can stop execution. |
| 6. Route | `NocAgent` | Recognized focused prompts skip the outer model. Otherwise, the in-process MAF orchestrator uses its Foundry model to choose local `ask_*` function tools. |
| 7. Reason | Persisted Foundry Prompt Agent | Receives the question through its Responses API and uses the MCP tool configured in its definition. |
| 8. Retrieve | Native MCP binding and remote server | `MCPTool(project_connection_id=...)` supplies the endpoint and connection authentication. The remote MCP server executes its selected tool against its data source. Tool discovery can be reused by the service, not necessarily repeated per turn. |
| 9. Return | Foundry, MCP host, APIM, Cowork | Evidence becomes a specialist answer, optionally an orchestrator synthesis, then MCP content and finally Cowork's presentation. Consent requirements or failures must remain explicit. |

**Where is the toolbox?** The old shared `noc-iq-toolbox` is not in this
runtime chain. Each persisted specialist has its own native `MCPTool` and
project connection. If using "toolbox" informally, it means this configured
tool binding, not an additional deployed service or network hop.

**Routing limitation:** the current focused router selects the first matching
keyword route. It does not guarantee that every combined question reaches
multi-specialist orchestration. The diagram shows actual routing behavior,
not an idealized intent classifier.

## 2. Focused Fabric topology fast path

After the common ingress, authorization, OBO and run-context steps above:

```mermaid
sequenceDiagram
    autonumber
    participant H as Cowork MCP host
    participant N as NocAgent
    participant I as Host managed identity
    participant G as Fabric Graph REST API
    participant F as Foundry topology agent
    participant D as Fabric Data Agent MCP

    H->>N: _call_specialist(fabric_iq, question)
    N->>N: Match LINK identifier and supported topology terms
    alt Direct Graph template applies and IDs are configured
        N->>I: Get token for api.fabric.microsoft.com
        I-->>N: Service-identity token
        N->>G: GQL 1 - link endpoints and conduit
        G-->>N: Link and conduit rows
        N->>G: GQL 2 - links sharing the conduit
        G-->>N: Shared-link rows
        N->>G: GQL 3 - service, MPLS path and SLA dependencies
        G-->>N: Direct exposure rows
        N-->>H: Deterministically formatted topology answer
        Note over N,G: No Foundry agent, toolbox or downstream MCP on success
    else No template match or direct query raises an exception
        N->>F: Invoke persisted noc-topology-agent
        F->>D: Native MCPTool / fabric-iq-connection
        D->>G: Data Agent queries GraphModel
        G-->>D: Result or downstream error
        D-->>F: Tool result or token failure
        F-->>N: Specialist answer
        N-->>H: Return specialist output
    end
```

The direct route uses the **MCP host UAMI**, not the caller's delegated
Graph identity. It therefore uses the host's configured Fabric workspace
access, behind the MCP caller authorization boundary. It only returns the
three template result sets; it does not prove that every alternate path or
indirect dependency has been explored.

The template HTTP read timeout is 20 seconds. The enclosing Cowork invocation
has a maximum 28-second timeout covering OBO and execution. If a direct Graph
query raises, the current implementation attempts the persisted specialist
with whatever outer time budget remains. This can turn a direct-query timeout
into a final nested Data Agent token error. That nested connector failure
remains unresolved; direct Graph success bypasses it.

## 3. Specialist-to-tool mapping

| Cowork intent | Persisted Foundry agent | Native MCP connection | Tool/data surface |
|---|---|---|---|
| `foundry_iq`: runbooks and past tickets | `noc-knowledge-agent` | `kb-mcp-connection` | Foundry IQ knowledge-base MCP / Azure AI Search-backed knowledge |
| `fabric_iq`: general topology | `noc-topology-agent` | `fabric-iq-connection` | Fabric Data Agent MCP / GraphModel; nested-token issue remains |
| `web_iq`: public advisories | `noc-threatintel-agent` | `web-iq-connection` | Public web-search MCP |
| `work_iq`: on-call and bridge context | `noc-comms-agent` | `WorkIQ` | Microsoft 365 MCP / Teams and Outlook context |
| `rti_iq`: incident evidence | `noc-incident-agent` | `FABRIC_RTI_CONNECTION_ID` | Fabric Eventhouse KQL MCP / telemetry and alerts |
| Focused link blast radius / conduit | **Bypassed** on direct success | **Bypassed** | Three deterministic Fabric Graph REST GQL queries |

For calls to Foundry, Fabric/Work/RTI specialists use the calling-user token;
Foundry IQ and Web IQ use the service credential. Downstream connection
authentication is separate from authentication to the Foundry agent itself.
First-use consent is possible on user-delegated connections.

## 4. Governance and evidence boundaries

- Run-ledger calls are a side path from the host, not a hop between the
  Foundry agent and its MCP tool. Model specialists report SDK usage; the
  outer MCP reservation currently uses text-token estimates.
- A successful direct Graph call has no specialist LLM usage. The current
  outer MCP accounting still estimates text tokens, so do not interpret
  that estimate as proof of a specialist model invocation.
- Cowork can rephrase the MCP result. Its final wording is not a raw Graph
  result: absence of a diverse path, guaranteed outage, and incurred SLA
  penalties require evidence beyond the three topology templates.

Implementation references: [`mcp_server.py`](../agent/mcp_server.py)
(`call_tool`, `_invoke_noc_tool`, `_direct_specialist_for_task`),
[`agent.py`](../agent/agent.py) (`_call_specialist`,
`_call_topology_graph`, `_build_orchestrator_tools`), and
[`create_foundry_agents.py`](../scripts/create_foundry_agents.py)
(`MCPTool` provisioning).
