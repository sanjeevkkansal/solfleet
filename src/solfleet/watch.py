"""Solana-aware DNS failover loop.

Probes the fleet and reconciles each DNS pool so only healthy members
serve traffic. The decision is Solana-aware (slot lag, getHealth,
delinquency), which is the whole point: a generic HTTP health check would
keep a 500-slots-behind RPC node in rotation because it still returns 200.

Hard safety rule: never empty a pool. If every member of a pool fails its
eject conditions, solfleet keeps the current records and flags it loudly
rather than removing the last record and causing NXDOMAIN. Serving a
degraded node beats serving nothing.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from .audit import AuditLog
from .config import EjectWhen, Fleet
from .dns import DnsDriver
from .probe import ClusterStatus, NodeStatus, probe_fleet

Prober = Callable[[Fleet], Awaitable[list[ClusterStatus]]]


def should_eject(status: NodeStatus | None, ew: EjectWhen) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if ew.unhealthy and (status is None or not status.reachable or status.healthy is False):
        reasons.append("unhealthy or unreachable")
    if (ew.slot_lag is not None and status is not None
            and status.slot_lag is not None and status.slot_lag > ew.slot_lag):
        reasons.append(f"slot_lag {status.slot_lag} > {ew.slot_lag}")
    if ew.delinquent and status is not None and status.delinquent is True:
        reasons.append("delinquent")
    return (len(reasons) > 0, reasons)


def _node_ips(fleet: Fleet) -> dict[str, str]:
    return {n.name: n.host for c in fleet.clusters.values() for n in c.nodes}


async def watch_once(
    fleet: Fleet,
    driver: DnsDriver,
    *,
    audit: AuditLog | None = None,
    dry_run: bool = False,
    prober: Prober = probe_fleet,
) -> dict:
    if not fleet.dns or not fleet.dns.pools:
        return {"pools": [], "note": "no dns pools configured"}

    clusters = await prober(fleet)
    status_by_name = {n.name: n for c in clusters for n in c.nodes}
    ips = _node_ips(fleet)
    pools_report = []

    for pool in fleet.dns.pools:
        member_ips = {ips[m] for m in pool.members}
        current = set(driver.list_members(pool.record))

        decisions = {}
        for m in pool.members:
            ej, reasons = should_eject(status_by_name.get(m), pool.eject_when)
            decisions[m] = {"ip": ips[m], "eject": ej, "reasons": reasons}

        desired = {ips[m] for m in pool.members if not decisions[m]["eject"]}

        report = {"record": pool.record, "current": sorted(current),
                  "decisions": decisions}

        if not desired:
            # last-member protection: keep whatever is serving, never empty
            report.update(protected=True,
                          note="all members failing; keeping current records to avoid NXDOMAIN",
                          added=[], removed=[])
            pools_report.append(report)
            if audit and current:
                audit.record(operation="dns_watch", cluster=pool.cluster, node=pool.record,
                             mode="dry-run" if dry_run else "execute", allowed=True,
                             detail={"protected": True, "current": sorted(current)})
            continue

        to_remove = sorted((current & member_ips) - desired)
        to_add = sorted(desired - current)
        report.update(protected=False, added=to_add, removed=to_remove)
        pools_report.append(report)

        if not (to_add or to_remove):
            continue
        if dry_run:
            continue

        driver.ensure_marker(pool.record)
        for ip in to_remove:
            driver.remove_member(pool.record, ip)
        for ip in to_add:
            driver.add_member(pool.record, ip, pool.ttl)
        if audit:
            audit.record(operation="dns_watch", cluster=pool.cluster, node=pool.record,
                         mode="execute", allowed=True,
                         detail={"added": to_add, "removed": to_remove,
                                 "decisions": decisions})

    return {"pools": pools_report}


async def watch_loop(
    fleet: Fleet,
    driver: DnsDriver,
    *,
    interval_s: int = 30,
    iterations: int | None = None,
    audit: AuditLog | None = None,
    dry_run: bool = False,
    prober: Prober = probe_fleet,
    sleep: Callable[[float], Awaitable] = asyncio.sleep,
    on_round: Callable[[dict], None] | None = None,
) -> None:
    n = 0
    while iterations is None or n < iterations:
        report = await watch_once(fleet, driver, audit=audit, dry_run=dry_run, prober=prober)
        if on_round:
            on_round(report)
        n += 1
        if iterations is not None and n >= iterations:
            break
        await sleep(interval_s)
