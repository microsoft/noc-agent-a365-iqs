"""Focused self-check for the deterministic Fabric Graph topology path."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://example.invalid/api/projects/dummy")
os.environ.setdefault("AZURE_AI_MODEL_DEPLOYMENT_NAME", "dummy-model")

import agent as agent_module  # noqa: E402
from agent import NocAgent  # noqa: E402


class _Credential:
    def get_token(self, scope):
        assert scope == "https://api.fabric.microsoft.com/.default"
        return SimpleNamespace(token="fabric-token")


class _Response:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return {"status": {"code": "0000"}, "result": {"data": self._rows}}


class _Client:
    responses = []
    calls = []

    def __init__(self, timeout):
        assert timeout == 20

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, endpoint, headers, json):
        self.calls.append((endpoint, headers, json["query"]))
        return _Response(self.responses.pop(0))


def _build_agent():
    instance = NocAgent.__new__(NocAgent)
    instance._service_credential = _Credential()
    return instance


async def _run():
    instance = _build_agent()
    with patch.dict(os.environ, {"FABRIC_WORKSPACE_ID": "workspace-1", "FABRIC_GRAPH_MODEL_ID": "graph-1"}):
        _Client.responses = [
            [{"LinkId": "LINK-SYD-MEL-FIBRE-01", "OriginRouter": "CORE-SYD-01", "TerminatingRouter": "CORE-MEL-01", "ConduitId": "CONDUIT-SYD-MEL-INLAND"}],
            [{"LinkId": "LINK-SYD-MEL-FIBRE-01", "ConduitId": "CONDUIT-SYD-MEL-INLAND"}, {"LinkId": "LINK-SYD-MEL-FIBRE-02", "ConduitId": "CONDUIT-SYD-MEL-INLAND"}],
            [{"ServiceId": "VPN-ACME-CORP", "CustomerName": "ACME Corporation", "ActiveUsers": 450, "SLAPolicyId": "SLA-ACME-GOLD", "Tier": "Gold", "PenaltyPerHourUSD": 50000, "PathId": "MPLS-PATH-SYD-MEL-PRIMARY"}],
        ]
        _Client.calls = []
        with patch.object(agent_module.httpx, "AsyncClient", _Client):
            answer = await instance._call_topology_graph(
                "If LINK-SYD-MEL-FIBRE-01 goes down, what's the blast radius and shared conduit?"
            )

    assert len(_Client.calls) == 3
    assert all("/workspaces/workspace-1/GraphModels/graph-1/executeQuery" in call[0] for call in _Client.calls)
    assert all("LINK-SYD-MEL-FIBRE-01" in call[2] for call in _Client.calls)
    assert "CORE-SYD-01" in answer and "CORE-MEL-01" in answer
    assert "LINK-SYD-MEL-FIBRE-02" in answer
    assert "VPN-ACME-CORP" in answer and "$50,000/hour" in answer

    with patch.dict(os.environ, {"FABRIC_WORKSPACE_ID": "", "FABRIC_GRAPH_MODEL_ID": ""}):
        assert await instance._call_topology_graph("blast radius for LINK-SYD-MEL-FIBRE-01") is None


def main():
    asyncio.run(_run())
    print("PASS: test_topology_graph.py self-check passed")


if __name__ == "__main__":
    main()
