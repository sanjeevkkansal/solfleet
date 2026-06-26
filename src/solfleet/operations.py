"""Orchestrated operations: gate -> (dry-run | execute) -> audit.

restart_node and execute_upgrade are dry-run by default and execute only
behind confirm + policy. RPC nodes restart via systemctl; voting
validators exit through a leader-aware safe-exit so they don't skip their
own slots. Upgrades build on the dedicated builder, distribute the
matched agave + geyser artifact set, and swap it atomically. No operation
here touches identity/vote keys.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Awaitable, Callable

import httpx

from .audit import AuditLog
from .builder import AGAVE_BINS, GEYSER_LIB, build_artifacts
from .config import Cluster, Fleet, Node, Pool
from .dns import DnsDriver
from .executor import (
    Runner,
    _subprocess_runner,
    atomic_swap,
    inspect_node,
    remote_sha256,
    run,
    scp_from,
    scp_to,
    start_service,
    stop_service,
    validator_safe_exit,
)
from .probe import RpcError, rpc_call
from .safety import (
    Policy,
    disk_free_ok,
    gate,
    load_policy,
    version_allowed,
)
from .schedule import leader_windows

# A sampler reports catch-up progress for a node vs its cluster reference.
Sampler = Callable[[Node, str], Awaitable[dict]]


async def default_sampler(node: Node, reference_rpc: str) -> dict:
    out: dict = {"healthy": None, "node_slot": None, "ref_slot": None, "lag": None}
    async with httpx.AsyncClient() as client:
        try:
            out["ref_slot"] = await rpc_call(client, reference_rpc, "getSlot")
        except (httpx.HTTPError, RpcError):
            pass
        if not node.rpc_url:
            return out
        try:
            try:
                out["healthy"] = (await rpc_call(client, node.rpc_url, "getHealth")) == "ok"
            except RpcError:
                out["healthy"] = False
            out["node_slot"] = await rpc_call(client, node.rpc_url, "getSlot")
            if out["ref_slot"] is not None and out["node_slot"] is not None:
                out["lag"] = max(0, out["ref_slot"] - out["node_slot"])
        except (httpx.HTTPError, RpcError):
            pass
    return out


async def wait_for_catchup(
    node: Node,
    reference_rpc: str,
    *,
    max_lag: int = 2,
    timeout_s: int = 600,
    interval_s: int = 5,
    sampler: Sampler = default_sampler,
    sleep: Callable[[float], Awaitable] = asyncio.sleep,
) -> dict:
    waited = 0
    last: dict = {}
    while waited <= timeout_s:
        last = await sampler(node, reference_rpc)
        if last.get("healthy") and last.get("lag") is not None and last["lag"] <= max_lag:
            return {"caught_up": True, "waited_s": waited, **last}
        await sleep(interval_s)
        waited += interval_s
    return {"caught_up": False, "waited_s": waited, **last}


def _use_pcts(inspect_result: dict) -> list[int]:
    pcts = []
    for row in inspect_result.get("disk", []):
        pct = row.get("use_pct")
        if isinstance(pct, str) and pct.endswith("%") and pct[:-1].isdigit():
            pcts.append(int(pct[:-1]))
    return pcts


async def restart_node(
    fleet: Fleet,
    node_name: str,
    *,
    confirm: bool = False,
    policy: Policy | None = None,
    audit: AuditLog | None = None,
    runner: Runner = _subprocess_runner,
    sampler: Sampler = default_sampler,
    max_lag: int = 2,
    leader_fn: Callable[[Node, str, int], Awaitable[dict | None]] | None = None,
) -> dict:
    found = fleet.find_node(node_name)
    if not found:
        return {"error": f"unknown node {node_name!r}"}
    cluster_name, cluster, node = found
    rules = (policy or load_policy()).for_cluster(cluster_name)
    is_validator = node.role == "validator"
    leader_fn = leader_fn or _leader_info

    if is_validator:
        plan = [
            f"agave-validator exit on {node.name} (wait for a leader gap of "
            f">= {rules.require_leader_window_minutes} min, then exit; systemd relaunches)",
            f"wait until {node.name} reports healthy and lag <= {max_lag} vs {cluster.reference_rpc}",
            "verify service active; record before/after slot",
        ]
    else:
        plan = [
            f"stop {node.service.unit} on {node.name}",
            f"start {node.service.unit}",
            f"wait until {node.name} reports healthy and lag <= {max_lag} vs {cluster.reference_rpc}",
            "verify service active; record before/after slot",
        ]

    # preflight: node must currently be reachable + active before we cycle it
    before = inspect_node(node, runner=runner)
    svc = before.get("service", {})
    checks = [
        (svc.get("ok") is True, "node not reachable over SSH for preflight"),
        (svc.get("active") == "active", f"service not active before restart (is {svc.get('active')!r})"),
        (disk_free_ok(rules, _use_pcts(before)),
         f"disk below policy floor min_disk_free_pct={rules.min_disk_free_pct}"),
    ]

    leader = await leader_fn(node, cluster.reference_rpc, rules.require_leader_window_minutes)

    decision = gate(
        operation="restart_node",
        cluster=cluster_name,
        node=node_name,
        confirm=confirm,
        plan=plan,
        checks=checks,
    )

    if not confirm or not decision.allowed:
        detail = {"plan": plan, "reasons": decision.reasons, "preflight": before, "leader": leader}
        if audit:
            audit.record(operation="restart_node", cluster=cluster_name, node=node_name,
                         mode=decision.mode, allowed=decision.allowed, detail=detail)
        return {"decision": decision.to_dict(), "preflight": before, "leader": leader}

    # execute
    before_slot = (await sampler(node, cluster.reference_rpc)).get("node_slot")
    if is_validator:
        cycle = validator_safe_exit(
            node, min_idle_minutes=rules.require_leader_window_minutes, runner=runner)
        cycle_ok = cycle["ok"]
    else:
        stop = stop_service(node, runner=runner)
        start = start_service(node, runner=runner)
        cycle = {"stop": stop, "start": start}
        cycle_ok = stop["ok"] and start["ok"]
    catch = await wait_for_catchup(node, cluster.reference_rpc, max_lag=max_lag, sampler=sampler)
    after = inspect_node(node, runner=runner)

    result = {
        "decision": decision.to_dict(),
        "before_slot": before_slot,
        "cycle": cycle,
        "catchup": catch,
        "after_service": after.get("service"),
        "leader": leader,
        "succeeded": bool(cycle_ok and catch.get("caught_up")),
    }
    if audit:
        audit.record(operation="restart_node", cluster=cluster_name, node=node_name,
                     mode="execute", allowed=True, detail=result)
    return result


async def _leader_info(node: Node, reference_rpc: str, min_window_minutes: int) -> dict | None:
    """Best-effort leader-window view for validators; None for RPC-only
    nodes or on RPC error (the live safe-exit still enforces the wait)."""
    if node.role != "validator" or not node.identity:
        return None
    try:
        async with httpx.AsyncClient() as client:
            return await leader_windows(
                client, reference_rpc, node.identity, min_window_minutes=min_window_minutes)
    except (httpx.HTTPError, RpcError, KeyError):
        return None


def _upgrade_plan(cluster: Cluster, node: Node, target_version: str) -> list[str]:
    install = cluster.install
    builder = install.builder if install else None
    source = install.source if install else "agave"
    geyser = " (+ matching libyellowstone_grpc_geyser.so)" if install and install.geyser_repo else ""
    cycle = (
        "agave-validator exit (leader-aware), systemd relaunches new binary"
        if node.role == "validator"
        else f"stop {node.service.unit}, swap, start"
    )
    return [
        f"on builder {builder!r}: build {source} {target_version} from source{geyser}",
        f"distribute artifact set to {node.name}; checksum-verify each (abort on mismatch)",
        f"{cycle}",
        f"swap {node.service.binary} + geyser .so + version marker atomically",
        f"wait until healthy + caught up to {cluster.reference_rpc}",
        f"verify reported version == {target_version}; record before/after",
    ]


def _upgrade_checks(cluster_name, cluster, node, rules, target_version, builder_node):
    install = cluster.install
    return [
        (version_allowed(rules, target_version),
         f"version {target_version} not in allowed_versions {rules.allowed_versions} for {cluster_name}"),
        (install is not None and install.builder is not None,
         f"cluster {cluster_name} has no install.builder configured (strategy=build requires one)"),
        (builder_node is not None,
         f"builder node {install.builder if install else None!r} not found in fleet"),
        (not (install and install.geyser_repo) or node.service.geyser_lib is not None,
         f"{node.name} builds a geyser .so but service.geyser_lib (its target path) is unset"),
    ]


def plan_upgrade(
    fleet: Fleet,
    node_name: str,
    target_version: str,
    *,
    policy: Policy | None = None,
) -> dict:
    """Dry-run only: the ordered steps to upgrade one node in place, using
    artifacts from a dedicated builder. Always safe to call."""
    found = fleet.find_node(node_name)
    if not found:
        return {"error": f"unknown node {node_name!r}"}
    cluster_name, cluster, node = found
    rules = (policy or load_policy()).for_cluster(cluster_name)
    install = cluster.install
    builder_node = fleet.find_builder(install.builder) if install else None

    plan = _upgrade_plan(cluster, node, target_version)
    checks = _upgrade_checks(cluster_name, cluster, node, rules, target_version, builder_node)
    decision = gate(operation="upgrade", cluster=cluster_name, node=node_name,
                    confirm=False, plan=plan, checks=checks)
    note = (
        "in-place upgrade: this single node has downtime during the swap; "
        "zero-downtime rolling arrives in M2 with the DNS pool."
    )
    return {"decision": decision.to_dict(), "target_version": target_version, "note": note}


def _dest_for(node: Node, artifact_name: str) -> str | None:
    """Final on-node path for an artifact, or None if the node has no
    target for it (e.g. a geyser .so on a node that runs no plugin)."""
    if artifact_name == GEYSER_LIB:
        return node.service.geyser_lib
    if artifact_name in AGAVE_BINS:
        bindir = os.path.dirname(node.service.binary) or "/usr/local/bin"
        return f"{bindir}/{artifact_name}"
    return None


def distribute(builder: Node, target: Node, artifacts, *, runner: Runner) -> dict:
    """Pull each artifact from the builder, push it beside its destination
    on the target as <dest>.solfleet-new, and verify the sha matches the
    builder's. Returns ok plus per-artifact detail; the caller swaps only
    verified files."""
    staging = tempfile.mkdtemp(prefix="solfleet-")
    results = []
    ok = True
    for art in artifacts:
        dest = _dest_for(target, art.name)
        if dest is None:
            results.append({"name": art.name, "skipped": "no destination on target"})
            continue
        local = os.path.join(staging, art.name)
        staged_remote = f"{dest}.solfleet-new"
        pull = scp_from(builder, art.remote_path, local, runner=runner)
        push = scp_to(target, local, staged_remote, runner=runner)
        got = remote_sha256(target, staged_remote, runner=runner)
        verified = got is not None and art.sha256 is not None and got == art.sha256
        results.append({
            "name": art.name, "dest": dest, "staged": staged_remote,
            "pull_ok": pull.ok, "push_ok": push.ok, "sha_match": verified,
            "expected_sha": art.sha256, "got_sha": got,
        })
        if not (pull.ok and push.ok and verified):
            ok = False
    return {"ok": ok, "staging": staging, "artifacts": results}


def _version_matches(reported: str | None, target: str) -> bool:
    if not reported:
        return False
    t = target.lstrip("v")
    r = reported.lstrip("v")
    return r == t or r.startswith(t)


def _update_marker(node: Node, version: str, *, runner: Runner) -> dict:
    if not node.service.version_marker:
        return {"skipped": True}
    marker_value = version if version.startswith("v") else f"v{version}"
    r = run(node, f"printf '%s' '{marker_value}' > {node.service.version_marker}", runner=runner)
    return {"ok": r.ok, "value": marker_value,
            "error": None if r.ok else (r.stderr.strip() or f"exit {r.exit_code}")}


async def execute_upgrade(
    fleet: Fleet,
    node_name: str,
    target_version: str,
    *,
    confirm: bool = False,
    policy: Policy | None = None,
    audit: AuditLog | None = None,
    runner: Runner = _subprocess_runner,
    sampler: Sampler = default_sampler,
    max_lag: int = 2,
    force_build: bool = False,
) -> dict:
    found = fleet.find_node(node_name)
    if not found:
        return {"error": f"unknown node {node_name!r}"}
    cluster_name, cluster, node = found
    install = cluster.install
    rules = (policy or load_policy()).for_cluster(cluster_name)
    builder_node = fleet.find_builder(install.builder) if install else None

    plan = _upgrade_plan(cluster, node, target_version)
    before = inspect_node(node, runner=runner)
    checks = _upgrade_checks(cluster_name, cluster, node, rules, target_version, builder_node)
    checks += [
        (before.get("service", {}).get("ok") is True, "node not reachable over SSH for preflight"),
        (disk_free_ok(rules, _use_pcts(before)),
         f"disk below policy floor min_disk_free_pct={rules.min_disk_free_pct}"),
    ]
    decision = gate(operation="upgrade", cluster=cluster_name, node=node_name,
                    confirm=confirm, plan=plan, checks=checks)

    if not confirm or not decision.allowed:
        detail = {"plan": plan, "reasons": decision.reasons, "preflight": before}
        if audit:
            audit.record(operation="upgrade", cluster=cluster_name, node=node_name,
                         mode=decision.mode, allowed=decision.allowed, detail=detail)
        return {"decision": decision.to_dict(), "preflight": before}

    # 1. build (or reuse cached artifact set) on the builder
    build = build_artifacts(builder_node, install, target_version, runner=runner, force=force_build)
    if not build.ok:
        result = {"decision": decision.to_dict(), "build": build.to_dict(), "succeeded": False}
        if audit:
            audit.record(operation="upgrade", cluster=cluster_name, node=node_name,
                         mode="execute", allowed=True, detail=result)
        return result

    # 2. distribute + checksum-verify
    dist = distribute(builder_node, node, build.artifacts, runner=runner)
    if not dist["ok"]:
        result = {"decision": decision.to_dict(), "build": build.to_dict(),
                  "distribute": dist, "succeeded": False}
        if audit:
            audit.record(operation="upgrade", cluster=cluster_name, node=node_name,
                         mode="execute", allowed=True, detail=result)
        return result

    swaps = [(e["staged"], e["dest"], e["name"] in AGAVE_BINS)
             for e in dist["artifacts"] if e.get("sha_match")]

    # 3. swap atomically, then cycle the service (role-aware), then verify
    if node.role == "validator":
        # replace files first (running process holds the old inode), then
        # leader-aware exit so the relaunch picks up the new binary
        swap = atomic_swap(node, swaps, runner=runner)
        marker = _update_marker(node, target_version, runner=runner)
        cycle = validator_safe_exit(
            node, min_idle_minutes=rules.require_leader_window_minutes, runner=runner)
        cycle_ok = cycle["ok"]
    else:
        stop = stop_service(node, runner=runner)
        swap = atomic_swap(node, swaps, runner=runner)
        marker = _update_marker(node, target_version, runner=runner)
        start = start_service(node, runner=runner)
        cycle = {"stop": stop, "start": start}
        cycle_ok = stop["ok"] and start["ok"]

    catch = await wait_for_catchup(node, cluster.reference_rpc, max_lag=max_lag, sampler=sampler)
    after = inspect_node(node, runner=runner)
    reported = after.get("version", {}).get("binary_version")
    version_ok = _version_matches(reported, target_version)

    result = {
        "decision": decision.to_dict(),
        "build": {"version": build.version, "cached": build.cached,
                  "artifacts": [a.name for a in build.artifacts]},
        "distribute": {"ok": dist["ok"], "artifacts": dist["artifacts"]},
        "swap": swap,
        "marker": marker,
        "cycle": cycle,
        "catchup": catch,
        "reported_version": reported,
        "version_ok": version_ok,
        "succeeded": bool(swap["ok"] and cycle_ok and catch.get("caught_up") and version_ok),
    }
    if audit:
        audit.record(operation="upgrade", cluster=cluster_name, node=node_name,
                     mode="execute", allowed=True, detail=result)
    return result


# ---- manual DNS overrides (gated) -----------------------------------------


def _pools_for_node(fleet: Fleet, node_name: str, record: str | None) -> list[Pool]:
    if not fleet.dns:
        return []
    pools = [p for p in fleet.dns.pools if node_name in p.members]
    if record:
        pools = [p for p in pools if p.record == record]
    return pools


def dns_status(fleet: Fleet, driver: DnsDriver, *, record: str | None = None) -> dict:
    """Read-only: current members of each managed pool, mapped back to node
    names where possible."""
    if not fleet.dns or not fleet.dns.pools:
        return {"pools": [], "note": "no dns pools configured"}
    ip_to_name = {n.host: n.name for c in fleet.clusters.values() for n in c.nodes}
    report = []
    for pool in fleet.dns.pools:
        if record and pool.record != record:
            continue
        members = driver.list_members(pool.record)
        report.append({
            "record": pool.record,
            "cluster": pool.cluster,
            "ttl": pool.ttl,
            "marker": driver.has_marker(pool.record),
            "members": [{"ip": ip, "node": ip_to_name.get(ip, "?")} for ip in members],
            "configured_members": pool.members,
        })
    return {"pools": report}


def _dns_override(
    fleet: Fleet, driver: DnsDriver, node_name: str, action: str,
    *, record: str | None, confirm: bool, audit: AuditLog | None,
) -> dict:
    found = fleet.find_node(node_name)
    if not found:
        return {"error": f"unknown node {node_name!r}"}
    _cluster_name, _cluster, node = found
    pools = _pools_for_node(fleet, node_name, record)
    if not pools:
        return {"error": f"node {node_name!r} is not a member of any DNS pool"
                + (f" matching record {record!r}" if record else "")}

    results = []
    for pool in pools:
        current = set(driver.list_members(pool.record))
        ip = node.host
        if action == "eject":
            plan = [f"remove {ip} ({node_name}) from {pool.record}"]
            would_empty = len(current - {ip}) == 0 and ip in current
            checks = [(not would_empty,
                       f"refusing to eject: {ip} is the last record in {pool.record} "
                       "(would cause NXDOMAIN)")]
        else:  # restore
            plan = [f"add {ip} ({node_name}) to {pool.record}"]
            checks = [(True, "")]

        decision = gate(operation=f"dns_{action}", cluster=pool.cluster, node=pool.record,
                        confirm=confirm, plan=plan, checks=checks)
        entry = {"record": pool.record, "decision": decision.to_dict()}

        if confirm and decision.allowed:
            driver.ensure_marker(pool.record)
            if action == "eject":
                driver.remove_member(pool.record, ip)
            else:
                driver.add_member(pool.record, ip, pool.ttl)
            entry["members_now"] = driver.list_members(pool.record)
        results.append(entry)
        if audit:
            audit.record(operation=f"dns_{action}", cluster=pool.cluster, node=pool.record,
                         mode=decision.mode, allowed=decision.allowed,
                         detail={"ip": node.host, "node": node_name, "plan": plan,
                                 "reasons": decision.reasons})
    return {"action": action, "node": node_name, "pools": results}


def dns_eject(fleet, driver, node_name, *, record=None, confirm=False, audit=None) -> dict:
    """Manually pull a node from its pool(s). Dry-run unless confirm=True.
    Refuses to empty a pool (last-member protection)."""
    return _dns_override(fleet, driver, node_name, "eject",
                         record=record, confirm=confirm, audit=audit)


def dns_restore(fleet, driver, node_name, *, record=None, confirm=False, audit=None) -> dict:
    """Manually add a node back to its pool(s). Dry-run unless confirm=True."""
    return _dns_override(fleet, driver, node_name, "restore",
                         record=record, confirm=confirm, audit=audit)
