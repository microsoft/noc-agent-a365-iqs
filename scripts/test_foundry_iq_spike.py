"""Offline self-check for the minimum Foundry IQ Gate A spike."""

import os
from pathlib import Path
from types import SimpleNamespace

from azure.core.exceptions import ResourceNotFoundError

import create_foundry_agents
import create_foundry_toolbox_spike

REPO_ROOT = Path(__file__).parents[1]


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}: {label}")
    assert condition, label


def test_definition_matching() -> None:
    expected = {
        "model": "gpt-test",
        "instructions": "Use the KB.",
        "tools": [
            {
                "type": "mcp",
                "project_connection_id": "connection-id",
                "server_url": "https://proxy.example/mcp",
            }
        ],
    }
    existing = SimpleNamespace(
        versions={"latest": {"version": "7", "definition": expected}}
    )
    check(
        "matching persisted agent definition is idempotent",
        create_foundry_agents._definition_matches(
            existing,
            "gpt-test",
            "Use the KB.",
            "connection-id",
            "https://proxy.example/mcp",
        ),
    )
    check(
        "changed proxy URL requires a new agent version",
        not create_foundry_agents._definition_matches(
            existing,
            "gpt-test",
            "Use the KB.",
            "connection-id",
            "https://other.example/mcp",
        ),
    )


def test_proxy_opt_in() -> None:
    spec = create_foundry_agents.SPECIALISTS[0]
    original_name = os.environ.pop("FOUNDRY_IQ_PROXY_CONNECTION_NAME", None)
    original_url = os.environ.pop("FOUNDRY_IQ_PROXY_MCP_URL", None)
    try:
        check(
            "proxy is disabled by default",
            create_foundry_agents._connection_override(spec) is None,
        )
        os.environ["FOUNDRY_IQ_PROXY_CONNECTION_NAME"] = "foundry-iq-apim-proxy"
        try:
            create_foundry_agents._connection_override(spec)
            raise AssertionError("partial proxy configuration did not fail")
        except RuntimeError:
            print("PASS: partial proxy opt-in fails closed")
        os.environ["FOUNDRY_IQ_PROXY_MCP_URL"] = "https://apim.example/route"
        check(
            "both proxy values enable the knowledge specialist only",
            create_foundry_agents._connection_override(spec)
            == ("foundry-iq-apim-proxy", "https://apim.example/route"),
        )
        check(
            "other specialists ignore the Foundry IQ proxy",
            create_foundry_agents._connection_override(
                create_foundry_agents.SPECIALISTS[1]
            )
            is None,
        )
    finally:
        for name, value in (
            ("FOUNDRY_IQ_PROXY_CONNECTION_NAME", original_name),
            ("FOUNDRY_IQ_PROXY_MCP_URL", original_url),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_toolbox_idempotence() -> None:
    version = {
        "version": "3",
        "tools": [
            {
                "type": "mcp",
                "server_label": "foundry-iq",
                "server_url": "https://search.example/kb/mcp",
                "project_connection_id": "kb-id",
                "require_approval": "never",
            }
        ],
    }
    check(
        "exact one-tool Toolbox definition is reusable",
        create_foundry_toolbox_spike._toolbox_definition_matches(
            version,
            connection_id="kb-id",
            server_url="https://search.example/kb/mcp",
        ),
    )
    version["tools"].append(dict(version["tools"][0]))
    check(
        "a multi-tool Toolbox is never reused for this spike",
        not create_foundry_toolbox_spike._toolbox_definition_matches(
            version,
            connection_id="kb-id",
            server_url="https://search.example/kb/mcp",
        ),
    )


def test_bicep_policy_invariants() -> None:
    policy = (
        REPO_ROOT
        / "gateway"
        / "infra"
        / "policies"
        / "foundry-iq-mcp-passthrough.xml"
    ).read_text(encoding="utf-8")
    apim = (
        REPO_ROOT / "gateway" / "infra" / "core" / "apim" / "apim.bicep"
    ).read_text(encoding="utf-8")
    check(
        "policy uses only the ai.azure.com managed-identity audience",
        'authentication-managed-identity resource="https://ai.azure.com"' in policy
        and "cognitiveservices.azure.com" not in policy,
    )
    check(
        "policy streams request and response without body access",
        'buffer-request-body="false"' in policy
        and 'buffer-response="false"' in policy
        and "context.Request.Body" not in policy
        and "context.Response.Body" not in policy,
    )
    check(
        "policy preserves query and MCP protocol/session headers",
        "rewrite-uri" not in policy
        and "set-query-parameter" not in policy
        and "Mcp-Session-Id" not in policy
        and "MCP-Protocol-Version" not in policy
        and "Last-Event-ID" not in policy,
    )
    check(
        "dedicated route is disabled by an empty backend URL",
        "var foundryIqMcpEnabled = !empty(foundryIqToolboxBackendUrl)" in apim
        and "if (foundryIqMcpEnabled)" in apim,
    )
    check(
        "Streamable HTTP operations and route are dedicated",
        "specialists/foundry-iq/mcp" in apim
        and all(f"'{method}'" in apim for method in ("GET", "POST", "DELETE", "OPTIONS")),
    )
    check(
        "dedicated MCP API has no diagnostics resource",
        "foundryIqMcpDiagnostics" not in apim,
    )
    check(
        "dedicated MCP API requires its scoped subscription key",
        "resource foundryIqMcpSubscription " in apim
        and "scope: foundryIqMcpApi.id" in apim
        and "subscriptionRequired: true" in apim,
    )


def main() -> None:
    test_definition_matching()
    test_proxy_opt_in()
    test_toolbox_idempotence()
    test_bicep_policy_invariants()
    print("PASS: Foundry IQ Gate A offline self-check passed")


if __name__ == "__main__":
    main()
