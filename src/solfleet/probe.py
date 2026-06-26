"""Solana-aware health probe.

Plain JSON-RPC over httpx; no node-side agent. Each node is compared
against its cluster's reference RPC to compute slot lag, and validators
are checked for delinquency via the reference's getVoteAccounts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, asdict
from typing import Any

import httpx

from .config import Cluster, Fleet, Node

RPC_TIMEOUT = 10.0
# getHealth returns ok only when the node is within this many slots of
# the cluster; we surface our own lag number as well for finer policy.


@dataclass
class NodeStatus:
    name: str
    cluster: str
    role: str
    reachable: bool = False
    healthy: bool | None = None
    version: str | None = None
    slot: int | None = None
    slot_lag: int | None = None
    delinquent: bool | None = None
    activated_stake_sol: float | None = None
    vote_credits: int | None = None
    last_vote: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ClusterStatus:
    name: str
    reference_rpc: str
    reference_slot: int | None = None
    reference_version: str | None = None
    reference_error: str | None = None
    nodes: list[NodeStatus] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "reference_rpc": self.reference_rpc,
            "reference_slot": self.reference_slot,
            "reference_version": self.reference_version,
            "reference_error": self.reference_error,
            "nodes": [n.to_dict() for n in self.nodes],
        }


async def rpc_call(
    client: httpx.AsyncClient, url: str, method: str, params: list | None = None
) -> Any:
    """Single JSON-RPC call. Raises on transport errors and RPC errors,
    except getHealth's node-unhealthy error which the caller handles."""
    resp = await client.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []},
        timeout=RPC_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RpcError(method, body["error"])
    return body.get("result")


class RpcError(Exception):
    def __init__(self, method: str, error: dict):
        self.method = method
        self.error = error
        super().__init__(f"{method}: {error.get('message', error)}")


async def probe_node(
    client: httpx.AsyncClient,
    cluster_name: str,
    node: Node,
    reference_slot: int | None,
    vote_status: dict[str, dict] | None,
) -> NodeStatus:
    status = NodeStatus(name=node.name, cluster=cluster_name, role=node.role)

    if node.identity and vote_status is not None:
        entry = vote_status.get(node.identity)
        if entry:
            status.delinquent = entry["delinquent"]
            status.activated_stake_sol = entry["stake_sol"]
            status.vote_credits = entry.get("credits")
            status.last_vote = entry.get("last_vote")

    if not node.rpc_url:
        # validator without a local RPC port; reference-side data only
        status.error = "no rpc_url configured; probe limited to cluster-side data"
        return status

    try:
        try:
            health = await rpc_call(client, node.rpc_url, "getHealth")
            status.healthy = health == "ok"
        except RpcError:
            # getHealth reports "node is behind" as a JSON-RPC error
            status.healthy = False
        status.reachable = True

        version = await rpc_call(client, node.rpc_url, "getVersion")
        status.version = version.get("solana-core")

        status.slot = await rpc_call(client, node.rpc_url, "getSlot")
        if reference_slot is not None and status.slot is not None:
            status.slot_lag = max(0, reference_slot - status.slot)
    except (httpx.HTTPError, RpcError) as e:
        if not status.reachable:
            status.error = f"unreachable: {e}"
        else:
            status.error = str(e)

    return status


async def probe_cluster(
    client: httpx.AsyncClient, name: str, cluster: Cluster
) -> ClusterStatus:
    result = ClusterStatus(name=name, reference_rpc=cluster.reference_rpc)

    vote_status: dict[str, dict] | None = None
    try:
        result.reference_slot = await rpc_call(
            client, cluster.reference_rpc, "getSlot"
        )
        version = await rpc_call(client, cluster.reference_rpc, "getVersion")
        result.reference_version = version.get("solana-core")

        if any(n.identity for n in cluster.nodes):
            # keepUnstakedDelinquents so a freshly provisioned 0-stake
            # validator still shows up in the vote set
            accounts = await rpc_call(
                client, cluster.reference_rpc, "getVoteAccounts",
                [{"keepUnstakedDelinquents": True}],
            )
            vote_status = {}
            for delinquent, group in (
                (False, accounts.get("current", [])),
                (True, accounts.get("delinquent", [])),
            ):
                for acct in group:
                    ec = acct.get("epochCredits") or []
                    vote_status[acct["nodePubkey"]] = {
                        "delinquent": delinquent,
                        "stake_sol": acct.get("activatedStake", 0) / 1_000_000_000,
                        "credits": ec[-1][1] if ec else 0,
                        "last_vote": acct.get("lastVote"),
                    }
    except (httpx.HTTPError, RpcError) as e:
        result.reference_error = f"reference rpc failed: {e}"

    result.nodes = list(
        await asyncio.gather(
            *(
                probe_node(client, name, node, result.reference_slot, vote_status)
                for node in cluster.nodes
            )
        )
    )
    return result


async def probe_fleet(fleet: Fleet) -> list[ClusterStatus]:
    async with httpx.AsyncClient() as client:
        return list(
            await asyncio.gather(
                *(
                    probe_cluster(client, name, cluster)
                    for name, cluster in fleet.clusters.items()
                )
            )
        )
