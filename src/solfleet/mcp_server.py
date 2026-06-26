"""solfleet MCP server (stdio).

Read-only tools are ungated. Every mutating tool (restart, upgrade,
dns_*) defaults to a dry-run and only changes anything with confirm=true,
after passing policy.yaml, and writes to the audit log. No tool ever
touches identity/vote keypairs.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .audit import AuditLog
from .builder import bootstrap_builder
from .config import load_fleet
from .dns import make_driver
from .operations import (
    dns_eject,
    dns_restore,
    dns_status,
    execute_upgrade,
    plan_upgrade,
    restart_node,
)
import httpx

from .probe import RpcError, probe_fleet
from .provision import provision_node
from .safety import load_policy
from .schedule import leader_windows
from .validate import validate_fleet
from .voting import vote_status as _vote_status

mcp = FastMCP("solfleet")


def _audit() -> AuditLog:
    return AuditLog()


@mcp.tool()
async def fleet_status() -> dict[str, Any]:
    """Health of every node in the fleet, grouped by cluster: reachability,
    getHealth, version, slot lag vs the cluster reference RPC, and
    delinquency/stake for voting validators."""
    fleet = load_fleet()
    clusters = await probe_fleet(fleet)
    return {"clusters": [c.to_dict() for c in clusters]}


@mcp.tool()
async def node_detail(name: str) -> dict[str, Any]:
    """Full probe result for a single node by name, including its
    cluster's reference data."""
    fleet = load_fleet()
    found = fleet.find_node(name)
    if not found:
        known = [n.name for c in fleet.clusters.values() for n in c.nodes]
        return {"error": f"unknown node {name!r}", "known_nodes": known}

    cluster_name, _cluster, _node = found
    clusters = await probe_fleet(fleet)
    for cluster_status in clusters:
        if cluster_status.name != cluster_name:
            continue
        for node_status in cluster_status.nodes:
            if node_status.name == name:
                return {
                    "node": node_status.to_dict(),
                    "cluster": {
                        "name": cluster_status.name,
                        "reference_slot": cluster_status.reference_slot,
                        "reference_version": cluster_status.reference_version,
                        "reference_error": cluster_status.reference_error,
                    },
                }
    return {"error": f"probe returned no result for {name!r}"}


@mcp.tool()
async def version_drift() -> dict[str, Any]:
    """Compare each node's solana-core version against its cluster's
    reference RPC version. Drift does not always mean outdated (the
    reference may itself lag a release), but it flags what to look at."""
    fleet = load_fleet()
    clusters = await probe_fleet(fleet)
    report = []
    for cluster_status in clusters:
        ref = cluster_status.reference_version
        nodes = [
            {
                "name": n.name,
                "version": n.version,
                "matches_reference": (
                    None if (ref is None or n.version is None) else n.version == ref
                ),
            }
            for n in cluster_status.nodes
        ]
        report.append(
            {
                "cluster": cluster_status.name,
                "reference_version": ref,
                "nodes": nodes,
                "drift": any(node["matches_reference"] is False for node in nodes),
            }
        )
    return {"clusters": report}


@mcp.tool()
async def vote_status(name: str) -> dict[str, Any]:
    """Voting health of a validator that a plain RPC check can't show:
    in the vote set, voting vs delinquent, epoch credits, last vote, root
    slot, commission, activated stake, identity SOL balance (+ low-balance
    warning; a validator pays vote fees every slot), catch-up, and the next
    leader window. Read-only."""
    fleet = load_fleet()
    return await _vote_status(fleet, name)


@mcp.tool()
async def leader_schedule(name: str, min_window_minutes: int = 5) -> dict[str, Any]:
    """Upcoming leader slots for a validator and whether it's safe to
    restart now (no leader slot within min_window_minutes). Read-only;
    helps plan restarts/upgrades without skipping your own slots."""
    fleet = load_fleet()
    found = fleet.find_node(name)
    if not found:
        return {"error": f"unknown node {name!r}"}
    _cn, cluster, node = found
    if node.role != "validator" or not node.identity:
        return {"error": f"{name} is not a voting validator"}
    try:
        async with httpx.AsyncClient() as client:
            return await leader_windows(client, cluster.reference_rpc, node.identity,
                                        min_window_minutes=min_window_minutes)
    except (httpx.HTTPError, RpcError, KeyError) as e:
        return {"error": f"leader schedule unavailable: {e}"}


@mcp.tool()
async def validate() -> dict[str, Any]:
    """Read-only readiness check of the whole fleet: per-node SSH/service/
    binary/disk/RPC, per-builder cores/RAM/disk/toolchain, and DNS
    credential presence. Returns pass/warn/fail per check; ok is true only
    when nothing failed. Changes nothing."""
    fleet = load_fleet()
    return await validate_fleet(fleet, policy=load_policy())


@mcp.tool()
async def plan_node_upgrade(name: str, target_version: str) -> dict[str, Any]:
    """Dry-run plan for upgrading one node in place to target_version,
    using artifacts from the cluster's dedicated builder. Read-only:
    produces the ordered steps and policy preflight, changes nothing."""
    fleet = load_fleet()
    return plan_upgrade(fleet, name, target_version, policy=load_policy())


@mcp.tool()
async def restart(name: str, confirm: bool = False) -> dict[str, Any]:
    """Restart a node and wait for catch-up. RPC nodes cycle via systemctl;
    voting validators use a leader-aware safe-exit so they don't skip slots.

    Defaults to a dry-run that returns the plan and policy preflight. Pass
    confirm=true to actually cycle the service; gated by policy and
    recorded in the audit log. Never touches keys."""
    fleet = load_fleet()
    return await restart_node(
        fleet, name, confirm=confirm, policy=load_policy(), audit=_audit()
    )


@mcp.tool()
async def bootstrap_builder_host(builder: str, confirm: bool = False) -> dict[str, Any]:
    """Install the build toolchain + deps (Rust, protoc, libclang-dev, ...)
    on a builder host so it can compile agave from source. Dry-run by
    default; confirm=true runs it (idempotent) and records to the audit
    log. Run once per builder before the first upgrade/provision."""
    fleet = load_fleet()
    return bootstrap_builder(fleet, builder, confirm=confirm, audit=_audit())


@mcp.tool()
async def provision(name: str, version: str, confirm: bool = False,
                    format_devices: list[str] | None = None,
                    catchup_timeout_s: int = 1800) -> dict[str, Any]:
    """Bring up a bare host into a serving node: preflight, user, system
    tuning, disks, software install (via the builder), systemd unit, key
    check, start + catch-up.

    Defaults to a dry-run plan. confirm=true executes, gated by preflight
    and policy and recorded in the audit log. Disks marked format must be
    listed in format_devices to be wiped; solfleet never creates keys.
    catchup_timeout_s bounds the wait for the fresh node to catch up
    (default 1800; raise it for slow snapshot downloads)."""
    fleet = load_fleet()
    return await provision_node(
        fleet, name, version, confirm=confirm,
        allow_format=set(format_devices or []),
        policy=load_policy(), audit=_audit(),
        catchup_timeout_s=catchup_timeout_s,
    )


@mcp.tool()
async def upgrade(name: str, target_version: str, confirm: bool = False) -> dict[str, Any]:
    """Upgrade one node in place to target_version: build on the cluster's
    builder, distribute + checksum-verify the agave + geyser artifact set,
    swap atomically, cycle (leader-aware for validators), and verify.

    Defaults to a dry-run plan. confirm=true executes, gated by policy
    (allowed versions, disk floor) and recorded in the audit log."""
    fleet = load_fleet()
    return await execute_upgrade(
        fleet, name, target_version, confirm=confirm, policy=load_policy(), audit=_audit()
    )


@mcp.tool()
async def dns_pool_status(record: str | None = None) -> dict[str, Any]:
    """Current members of each managed DNS pool, mapped back to node names.
    Read-only."""
    fleet = load_fleet()
    if not fleet.dns:
        return {"error": "no dns config in fleet.yaml"}
    return dns_status(fleet, make_driver(fleet.dns), record=record)


@mcp.tool()
async def dns_pool_eject(name: str, record: str | None = None,
                         confirm: bool = False) -> dict[str, Any]:
    """Manually pull a node from its DNS pool(s). Dry-run unless confirm=true.
    Refuses to empty a pool (last-member protection). Audited."""
    fleet = load_fleet()
    if not fleet.dns:
        return {"error": "no dns config in fleet.yaml"}
    return dns_eject(fleet, make_driver(fleet.dns), name, record=record,
                     confirm=confirm, audit=_audit())


@mcp.tool()
async def dns_pool_restore(name: str, record: str | None = None,
                           confirm: bool = False) -> dict[str, Any]:
    """Manually add a node back to its DNS pool(s). Dry-run unless confirm=true.
    Audited."""
    fleet = load_fleet()
    if not fleet.dns:
        return {"error": "no dns config in fleet.yaml"}
    return dns_restore(fleet, make_driver(fleet.dns), name, record=record,
                       confirm=confirm, audit=_audit())


@mcp.tool()
async def audit_log(name: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Recent audit entries (every dry-run and execute), newest first.
    Optionally filtered to one node."""
    return {"events": _audit().recent(node=name, limit=limit)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
