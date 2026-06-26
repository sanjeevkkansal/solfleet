"""Voting-validator status: everything an operator needs to know that a
plain RPC health check doesn't show.

A voting validator can be "up" (process running, RPC healthy) yet not
actually voting: behind on catch-up, delinquent, or out of SOL to pay
vote fees. This consolidates the vote-account state (credits, last vote,
delinquency, commission, stake), the identity balance (a validator pays
vote fees every slot; an empty identity stops voting), catch-up, and the
next leader window into one read-only view.
"""

from __future__ import annotations

import httpx

from .config import Fleet
from .probe import RpcError, rpc_call
from .schedule import leader_windows

# A voting identity that drops below this is at risk of stopping voting.
LOW_BALANCE_SOL = 1.0
LAMPORTS_PER_SOL = 1_000_000_000


def _credits(entry: dict) -> int:
    ec = entry.get("epochCredits") or []
    return ec[-1][1] if ec else 0


async def vote_status(fleet: Fleet, node_name: str) -> dict:
    found = fleet.find_node(node_name)
    if not found:
        return {"error": f"unknown node {node_name!r}"}
    cluster_name, cluster, node = found
    if node.role != "validator" or not node.identity:
        return {"error": f"{node_name} is not a voting validator "
                "(needs role=validator and an identity pubkey)"}

    ref = cluster.reference_rpc
    out: dict = {"node": node_name, "cluster": cluster_name, "identity": node.identity}

    async with httpx.AsyncClient() as client:
        # vote account (keepUnstakedDelinquents so a fresh 0-stake account shows)
        entry, delinquent = None, None
        try:
            accts = await rpc_call(client, ref, "getVoteAccounts",
                                   [{"keepUnstakedDelinquents": True}])
            for group, is_delq in ((accts.get("current", []), False),
                                   (accts.get("delinquent", []), True)):
                for a in group:
                    if a.get("nodePubkey") == node.identity:
                        entry, delinquent = a, is_delq
        except (httpx.HTTPError, RpcError) as e:
            out["vote_account_error"] = str(e)

        # identity balance (pays vote fees; empty identity -> stops voting)
        try:
            bal = await rpc_call(client, ref, "getBalance", [node.identity])
            lamports = bal.get("value") if isinstance(bal, dict) else bal
            out["identity_balance_sol"] = round((lamports or 0) / LAMPORTS_PER_SOL, 4)
            out["low_balance"] = out["identity_balance_sol"] < LOW_BALANCE_SOL
        except (httpx.HTTPError, RpcError) as e:
            out["balance_error"] = str(e)

        # catch-up: node slot vs reference
        try:
            ref_slot = await rpc_call(client, ref, "getSlot")
            if node.rpc_url:
                node_slot = await rpc_call(client, node.rpc_url, "getSlot")
                out["slot_lag"] = max(0, ref_slot - node_slot)
                out["caught_up"] = out["slot_lag"] <= 2
        except (httpx.HTTPError, RpcError):
            out["caught_up"] = None

        # next leader window (planning restarts without skipping slots)
        try:
            out["leader"] = await leader_windows(client, ref, node.identity)
        except (httpx.HTTPError, RpcError, KeyError):
            out["leader"] = None

    if entry is None:
        out.update(in_vote_set=False, voting=False,
                   note="identity not in the cluster's vote accounts yet "
                        "(no vote account, or freshly created and not seen)")
        return out

    out.update(
        in_vote_set=True,
        vote_account=entry.get("votePubkey"),
        voting=not delinquent,
        delinquent=delinquent,
        credits=_credits(entry),
        last_vote=entry.get("lastVote"),
        root_slot=entry.get("rootSlot"),
        commission=entry.get("commission"),
        activated_stake_sol=round(entry.get("activatedStake", 0) / LAMPORTS_PER_SOL, 4),
    )
    return out
