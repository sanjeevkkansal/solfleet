"""vote_status against a mocked Solana RPC."""

import json

import httpx
import pytest

from solfleet.config import Fleet
from solfleet.voting import LAMPORTS_PER_SOL, vote_status

# synthetic example pubkeys (valid base58, not real accounts)
IDENTITY = "76ER5K389Qc4PTzH3VyNAGzJAaoJYaKTVSEWnYFahiR"
VOTE_PUBKEY = "3uZmTYUHvFmiCNkNLBKUCBnUfjmJj27SRmuiGxwWJhYU"

FLEET = Fleet.model_validate({
    "clusters": {
        "devnet": {
            "reference_rpc": "https://ref.example",
            "nodes": [
                {"name": "val", "role": "validator", "host": "10.0.0.1",
                 "identity": IDENTITY, "rpc_url": "http://10.0.0.1:8899"},
                {"name": "rpc1", "role": "rpc", "host": "10.0.0.2",
                 "rpc_url": "http://10.0.0.2:8899"},
            ],
        }
    }
})


def handler(*, voting, balance_sol, lag=0):
    def h(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        base = str(request.url)
        if method == "getVoteAccounts":
            entry = {
                "nodePubkey": IDENTITY, "votePubkey": VOTE_PUBKEY,
                "activatedStake": 5 * LAMPORTS_PER_SOL, "commission": 10,
                "lastVote": 1000 if voting else 0, "rootSlot": 968 if voting else 0,
                "epochCredits": [[5, 4200, 4000]] if voting else [],
            }
            group = "current" if voting else "delinquent"
            return _ok({group: [entry], ("delinquent" if voting else "current"): []})
        if method == "getBalance":
            return _ok({"context": {"slot": 1}, "value": int(balance_sol * LAMPORTS_PER_SOL)})
        if method == "getSlot":
            return _ok(1000 if "ref.example" in base else 1000 - lag)
        if method == "getEpochInfo":
            return _ok({"absoluteSlot": 1000, "slotIndex": 100, "slotsInEpoch": 10000, "epoch": 5})
        if method == "getLeaderSchedule":
            return _ok({})  # not a leader this epoch
        raise AssertionError(f"unexpected {method}")
    return h


def _ok(result):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


async def run(fleet, name, **kw):
    transport = httpx.MockTransport(handler(**kw))
    import solfleet.voting as v
    # patch AsyncClient to use the mock transport
    orig = httpx.AsyncClient

    def factory(*a, **k):
        return orig(transport=transport)
    v.httpx.AsyncClient = factory
    try:
        return await vote_status(fleet, name)
    finally:
        v.httpx.AsyncClient = orig


async def test_healthy_voting_validator():
    r = await run(FLEET, "val", voting=True, balance_sol=2.0)
    assert r["in_vote_set"] is True
    assert r["voting"] is True and r["delinquent"] is False
    assert r["credits"] == 4200
    assert r["last_vote"] == 1000
    assert r["commission"] == 10
    assert r["activated_stake_sol"] == 5.0
    assert r["identity_balance_sol"] == 2.0
    assert r["low_balance"] is False
    assert r["vote_account"] == VOTE_PUBKEY


async def test_delinquent_low_balance_validator():
    r = await run(FLEET, "val", voting=False, balance_sol=0.2)
    assert r["voting"] is False and r["delinquent"] is True
    assert r["credits"] == 0
    assert r["low_balance"] is True   # 0.2 < 1.0 threshold


async def test_rpc_node_rejected():
    r = await vote_status(FLEET, "rpc1")
    assert "error" in r and "not a voting validator" in r["error"]


async def test_unknown_node():
    r = await vote_status(FLEET, "nope")
    assert "error" in r
