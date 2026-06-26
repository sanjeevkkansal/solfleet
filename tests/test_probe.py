"""Probe tests against a mocked Solana JSON-RPC transport."""

import json

import httpx
import pytest

from solfleet.config import Cluster, Node
from solfleet.probe import probe_cluster

REFERENCE = "https://reference.example"
NODE_OK = "http://node-ok:8899"
NODE_BEHIND = "http://node-behind:8899"
NODE_DOWN = "http://node-down:8899"

IDENTITY_OK = "GoodValidator111111111111111111111111111111"
IDENTITY_DELINQUENT = "BadValidator1111111111111111111111111111111"


def rpc_result(result):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def make_handler(reference_slot=1000):
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        base = str(request.url)

        if base.startswith(NODE_DOWN):
            raise httpx.ConnectError("connection refused")

        if base.startswith(REFERENCE):
            if method == "getSlot":
                return rpc_result(reference_slot)
            if method == "getVersion":
                return rpc_result({"solana-core": "3.0.10"})
            if method == "getVoteAccounts":
                return rpc_result(
                    {
                        "current": [
                            {"nodePubkey": IDENTITY_OK, "activatedStake": 5_000_000_000_000}
                        ],
                        "delinquent": [
                            {"nodePubkey": IDENTITY_DELINQUENT, "activatedStake": 0}
                        ],
                    }
                )

        if base.startswith(NODE_OK):
            if method == "getHealth":
                return rpc_result("ok")
            if method == "getVersion":
                return rpc_result({"solana-core": "3.0.10"})
            if method == "getSlot":
                return rpc_result(reference_slot - 2)

        if base.startswith(NODE_BEHIND):
            if method == "getHealth":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "error": {"code": -32005, "message": "Node is behind by 250 slots"},
                    },
                )
            if method == "getVersion":
                return rpc_result({"solana-core": "2.3.6"})
            if method == "getSlot":
                return rpc_result(reference_slot - 250)

        raise AssertionError(f"unexpected call {method} to {base}")

    return handler


def make_cluster():
    return Cluster(
        reference_rpc=REFERENCE,
        nodes=[
            Node(name="ok", role="rpc", host="node-ok", rpc_url=NODE_OK),
            Node(name="behind", role="rpc", host="node-behind", rpc_url=NODE_BEHIND),
            Node(name="down", role="rpc", host="node-down", rpc_url=NODE_DOWN),
            Node(
                name="val",
                role="validator",
                host="node-val",
                identity=IDENTITY_DELINQUENT,
            ),
        ],
    )


@pytest.fixture
async def result():
    transport = httpx.MockTransport(make_handler())
    async with httpx.AsyncClient(transport=transport) as client:
        yield await probe_cluster(client, "testnet", make_cluster())


async def test_reference_data(result):
    assert result.reference_slot == 1000
    assert result.reference_version == "3.0.10"
    assert result.reference_error is None


async def test_healthy_node(result):
    ok = next(n for n in result.nodes if n.name == "ok")
    assert ok.reachable and ok.healthy
    assert ok.version == "3.0.10"
    assert ok.slot_lag == 2
    assert ok.error is None


async def test_behind_node_health_error_handled(result):
    behind = next(n for n in result.nodes if n.name == "behind")
    assert behind.reachable
    assert behind.healthy is False
    assert behind.slot_lag == 250
    assert behind.version == "2.3.6"


async def test_unreachable_node(result):
    down = next(n for n in result.nodes if n.name == "down")
    assert not down.reachable
    assert "unreachable" in down.error


async def test_validator_delinquency_from_reference(result):
    val = next(n for n in result.nodes if n.name == "val")
    assert val.delinquent is True
    # no rpc_url: probe is cluster-side only, and says so
    assert "no rpc_url" in val.error
