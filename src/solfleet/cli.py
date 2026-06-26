"""solfleet CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone

from .audit import AuditLog
from .builder import bootstrap_builder
from .config import load_fleet
from .dns import make_driver
from .executor import inspect_node
from .operations import (
    dns_eject,
    dns_restore,
    dns_status,
    execute_upgrade,
    plan_upgrade,
    restart_node,
)
from .probe import ClusterStatus, probe_fleet
from .provision import provision_node
from .safety import load_policy
from .validate import validate_fleet
from .voting import vote_status
from .watch import watch_loop, watch_once


def format_table(clusters: list[ClusterStatus]) -> str:
    rows = []
    for cluster in clusters:
        for n in cluster.nodes:
            if n.error and not n.reachable:
                health = "UNREACHABLE"
            elif n.healthy is True:
                health = "ok"
            elif n.healthy is False:
                health = "BEHIND"
            else:
                health = "-"
            lag = str(n.slot_lag) if n.slot_lag is not None else "-"
            if n.delinquent is True:
                delinq = "DELINQUENT"
            elif n.delinquent is False:
                delinq = "voting"
            else:
                delinq = "-"
            rows.append(
                (cluster.name, n.name, n.role, health, n.version or "-", lag, delinq)
            )

    headers = ("CLUSTER", "NODE", "ROLE", "HEALTH", "VERSION", "SLOT LAG", "VOTE")
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths))]
    lines += ["  ".join(c.ljust(w) for c, w in zip(row, widths)) for row in rows]

    for cluster in clusters:
        if cluster.reference_error:
            lines.append(f"warning: {cluster.name}: {cluster.reference_error}")
    return "\n".join(lines)


def _unhealthy(clusters: list[ClusterStatus]) -> bool:
    return any(
        n.healthy is False or (n.error and not n.reachable) or n.delinquent is True
        for c in clusters for n in c.nodes
    )


def cmd_status(args: argparse.Namespace) -> int:
    if getattr(args, "watch", False):
        return _watch_status(args)
    fleet = load_fleet(args.config)
    clusters = asyncio.run(probe_fleet(fleet))
    if args.json:
        print(json.dumps([c.to_dict() for c in clusters], indent=2))
    else:
        print(format_table(clusters))
    return 1 if _unhealthy(clusters) else 0


def _watch_status(args: argparse.Namespace) -> int:
    """Refresh the status table in place until interrupted."""
    try:
        while True:
            fleet = load_fleet(args.config)
            clusters = asyncio.run(probe_fleet(fleet))
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            sys.stdout.write("\033[2J\033[H")  # clear screen, cursor home
            print(f"solfleet status — {now}  (refresh {args.interval}s, Ctrl-C to exit)\n")
            print(format_table(clusters))
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    fleet = load_fleet(args.config)
    found = fleet.find_node(args.node)
    if not found:
        known = [n.name for c in fleet.clusters.values() for n in c.nodes]
        print(f"unknown node {args.node!r}; known: {', '.join(known)}", file=sys.stderr)
        return 2
    _cluster_name, _cluster, node = found
    result = inspect_node(node)
    print(json.dumps(result, indent=2))
    svc = result["service"]
    healthy = svc.get("ok") and svc.get("active") == "active" and svc.get("sub") == "running"
    return 0 if healthy else 1


def cmd_plan_upgrade(args: argparse.Namespace) -> int:
    fleet = load_fleet(args.config)
    result = plan_upgrade(fleet, args.node, args.version, policy=load_policy(args.policy))
    print(json.dumps(result, indent=2))
    if "error" in result:
        return 2
    return 0 if result["decision"]["allowed"] else 1


def cmd_restart(args: argparse.Namespace) -> int:
    fleet = load_fleet(args.config)
    audit = AuditLog(args.audit_db) if args.audit_db else AuditLog()
    result = asyncio.run(
        restart_node(
            fleet,
            args.node,
            confirm=args.confirm,
            policy=load_policy(args.policy),
            audit=audit,
        )
    )
    print(json.dumps(result, indent=2))
    if "error" in result:
        return 2
    if not args.confirm:
        return 0  # dry-run always "succeeds"
    return 0 if result.get("succeeded") else 1


def cmd_upgrade(args: argparse.Namespace) -> int:
    fleet = load_fleet(args.config)
    audit = AuditLog(args.audit_db) if args.audit_db else AuditLog()
    result = asyncio.run(
        execute_upgrade(fleet, args.node, args.version, confirm=args.confirm,
                        policy=load_policy(args.policy), audit=audit,
                        force_build=args.force_build)
    )
    print(json.dumps(result, indent=2))
    if "error" in result:
        return 2
    if not args.confirm:
        return 0
    return 0 if result.get("succeeded") else 1


def cmd_bootstrap_builder(args: argparse.Namespace) -> int:
    fleet = load_fleet(args.config)
    audit = AuditLog(args.audit_db) if args.audit_db else AuditLog()
    result = bootstrap_builder(fleet, args.builder, confirm=args.confirm, audit=audit)
    print(json.dumps(result, indent=2))
    if "error" in result:
        return 2
    if not args.confirm:
        return 0
    return 0 if result.get("ok") else 1


def cmd_provision(args: argparse.Namespace) -> int:
    fleet = load_fleet(args.config)
    audit = AuditLog(args.audit_db) if args.audit_db else AuditLog()
    result = asyncio.run(
        provision_node(fleet, args.node, args.version, confirm=args.confirm,
                       allow_format=set(args.format_device or []),
                       policy=load_policy(args.policy), audit=audit,
                       catchup_timeout_s=args.catchup_timeout)
    )
    print(json.dumps(result, indent=2))
    if "error" in result:
        return 2
    if not args.confirm:
        return 0
    return 0 if result.get("succeeded") else 1


def cmd_watch(args: argparse.Namespace) -> int:
    fleet = load_fleet(args.config)
    if not fleet.dns:
        print("no dns config in fleet.yaml", file=sys.stderr)
        return 2
    driver = make_driver(fleet.dns)
    audit = AuditLog(args.audit_db) if args.audit_db else AuditLog()

    def show(report):
        print(json.dumps(report, indent=2))

    if args.once:
        report = asyncio.run(watch_once(fleet, driver, audit=audit, dry_run=args.dry_run))
        show(report)
    else:
        asyncio.run(watch_loop(fleet, driver, interval_s=args.interval, audit=audit,
                               dry_run=args.dry_run, on_round=show))
    return 0


def cmd_dns(args: argparse.Namespace) -> int:
    fleet = load_fleet(args.config)
    if not fleet.dns:
        print("no dns config in fleet.yaml", file=sys.stderr)
        return 2
    driver = make_driver(fleet.dns)
    audit = AuditLog(args.audit_db) if args.audit_db else AuditLog()
    if args.dns_command == "status":
        print(json.dumps(dns_status(fleet, driver, record=args.record), indent=2))
        return 0
    fn = dns_eject if args.dns_command == "eject" else dns_restore
    result = fn(fleet, driver, args.node, record=args.record, confirm=args.confirm, audit=audit)
    print(json.dumps(result, indent=2))
    return 2 if "error" in result else 0


_MARK = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}


def _format_validate(report: dict) -> str:
    lines = []
    for n in report["nodes"]:
        summary = " ".join(f"{c['name']}:{c['status']}" for c in n["checks"])
        lines.append(f"{_MARK[n['status']]:5} {n['name']:14} [{n['cluster']}/{n['role']}]  {summary}")
        for c in n["checks"]:
            if c["status"] != "pass":
                lines.append(f"        - {c['name']}: {c['detail']}")
    for b in report["builders"]:
        summary = " ".join(f"{c['name']}:{c['status']}" for c in b["checks"])
        lines.append(f"{_MARK[b['status']]:5} {b['name']:14} [builder]      {summary}")
        for c in b["checks"]:
            if c["status"] != "pass":
                lines.append(f"        - {c['name']}: {c['detail']}")
    if report.get("dns"):
        d = report["dns"]
        summary = " ".join(f"{c['name']}:{c['status']}" for c in d["checks"])
        lines.append(f"{_MARK[d['status']]:5} {'dns':14} [provider]     {summary}")
    warns = sum(1 for grp in ("nodes", "builders")
                for item in report[grp] for c in item["checks"] if c["status"] == "warn")
    lines.append("")
    lines.append(f"OVERALL: {'PASS' if report['ok'] else 'FAIL'}  ({warns} warning(s))")
    return "\n".join(lines)


def cmd_vote_status(args: argparse.Namespace) -> int:
    fleet = load_fleet(args.config)
    result = asyncio.run(vote_status(fleet, args.node))
    print(json.dumps(result, indent=2))
    if "error" in result:
        return 2
    # non-zero if the node should be voting but isn't, or balance is low
    bad = result.get("voting") is False or result.get("low_balance") is True
    return 1 if bad else 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        fleet = load_fleet(args.config)
    except Exception as e:  # structural / parse error
        print(f"config invalid: {e}", file=sys.stderr)
        return 2
    report = asyncio.run(validate_fleet(fleet, policy=load_policy(args.policy)))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(_format_validate(report))
    return 0 if report["ok"] else 1


def cmd_audit(args: argparse.Namespace) -> int:
    audit = AuditLog(args.audit_db) if args.audit_db else AuditLog()
    print(json.dumps({"events": audit.recent(node=args.node, limit=args.limit)}, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="solfleet")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="probe all nodes and print fleet health")
    status.add_argument("--config", help="path to fleet.yaml")
    status.add_argument("--json", action="store_true", help="JSON output")
    status.add_argument("--watch", action="store_true", help="refresh in place until Ctrl-C")
    status.add_argument("--interval", type=int, default=5, help="watch refresh seconds")
    status.set_defaults(func=cmd_status)

    validate = sub.add_parser("validate", help="structural + live read-only preflight of the fleet")
    validate.add_argument("--config", help="path to fleet.yaml")
    validate.add_argument("--policy", help="path to policy.yaml")
    validate.add_argument("--json", action="store_true", help="JSON output")
    validate.set_defaults(func=cmd_validate)

    votestatus = sub.add_parser("vote-status", help="voting health of a validator (credits, balance, leader)")
    votestatus.add_argument("node", help="validator node name from fleet.yaml")
    votestatus.add_argument("--config", help="path to fleet.yaml")
    votestatus.set_defaults(func=cmd_vote_status)

    inspect = sub.add_parser("inspect", help="read-only SSH inspection of one node")
    inspect.add_argument("node", help="node name from fleet.yaml")
    inspect.add_argument("--config", help="path to fleet.yaml")
    inspect.set_defaults(func=cmd_inspect)

    planup = sub.add_parser("plan-upgrade", help="dry-run plan for an in-place upgrade")
    planup.add_argument("node", help="node name from fleet.yaml")
    planup.add_argument("version", help="target version, e.g. 4.1.0")
    planup.add_argument("--config", help="path to fleet.yaml")
    planup.add_argument("--policy", help="path to policy.yaml")
    planup.set_defaults(func=cmd_plan_upgrade)

    restart = sub.add_parser("restart", help="restart a node (dry-run unless --confirm)")
    restart.add_argument("node", help="node name from fleet.yaml")
    restart.add_argument("--confirm", action="store_true",
                         help="actually stop/start the service (default: dry-run)")
    restart.add_argument("--config", help="path to fleet.yaml")
    restart.add_argument("--policy", help="path to policy.yaml")
    restart.add_argument("--audit-db", help="path to audit sqlite (default: ./solfleet.sqlite)")
    restart.set_defaults(func=cmd_restart)

    bootstrap = sub.add_parser("bootstrap-builder",
                               help="install build toolchain + deps on a builder (dry-run unless --confirm)")
    bootstrap.add_argument("builder", help="builder name from the builders: map")
    bootstrap.add_argument("--confirm", action="store_true", help="actually install")
    bootstrap.add_argument("--config", help="path to fleet.yaml")
    bootstrap.add_argument("--audit-db", help="path to audit sqlite (default: ./solfleet.sqlite)")
    bootstrap.set_defaults(func=cmd_bootstrap_builder)

    provision = sub.add_parser("provision", help="bring up a bare host (dry-run unless --confirm)")
    provision.add_argument("node", help="node name from fleet.yaml")
    provision.add_argument("version", help="agave version to install, e.g. 4.1.0")
    provision.add_argument("--confirm", action="store_true",
                           help="actually provision (default: dry-run plan)")
    provision.add_argument("--format-device", action="append", metavar="DEV",
                           help="acknowledge formatting this device (repeatable); "
                                "required for any disk with format: true")
    provision.add_argument("--catchup-timeout", type=int, default=1800, metavar="SECONDS",
                           help="how long to wait for catch-up from a fresh ledger "
                                "(default 1800; a fresh install needs longer than a restart)")
    provision.add_argument("--config", help="path to fleet.yaml")
    provision.add_argument("--policy", help="path to policy.yaml")
    provision.add_argument("--audit-db", help="path to audit sqlite (default: ./solfleet.sqlite)")
    provision.set_defaults(func=cmd_provision)

    upgrade = sub.add_parser("upgrade", help="in-place upgrade a node (dry-run unless --confirm)")
    upgrade.add_argument("node", help="node name from fleet.yaml")
    upgrade.add_argument("version", help="target version, e.g. 4.1.0")
    upgrade.add_argument("--confirm", action="store_true",
                         help="actually build/distribute/swap (default: dry-run)")
    upgrade.add_argument("--force-build", action="store_true",
                         help="rebuild even if a cached artifact set exists")
    upgrade.add_argument("--config", help="path to fleet.yaml")
    upgrade.add_argument("--policy", help="path to policy.yaml")
    upgrade.add_argument("--audit-db", help="path to audit sqlite (default: ./solfleet.sqlite)")
    upgrade.set_defaults(func=cmd_upgrade)

    watch = sub.add_parser("watch", help="health-driven DNS failover loop")
    watch.add_argument("--once", action="store_true", help="one reconcile pass then exit")
    watch.add_argument("--interval", type=int, default=30, help="seconds between passes")
    watch.add_argument("--dry-run", action="store_true", help="decide but don't change DNS")
    watch.add_argument("--config", help="path to fleet.yaml")
    watch.add_argument("--audit-db", help="path to audit sqlite (default: ./solfleet.sqlite)")
    watch.set_defaults(func=cmd_watch)

    dns_common = argparse.ArgumentParser(add_help=False)
    dns_common.add_argument("--config", help="path to fleet.yaml")
    dns_common.add_argument("--audit-db", help="path to audit sqlite (default: ./solfleet.sqlite)")

    dns = sub.add_parser("dns", help="inspect or manually override DNS pools")
    dns.set_defaults(func=cmd_dns)
    dns_sub = dns.add_subparsers(dest="dns_command", required=True)
    dns_status_p = dns_sub.add_parser("status", parents=[dns_common],
                                      help="show current pool members")
    dns_status_p.add_argument("--record", help="limit to one pool record")
    for name in ("eject", "restore"):
        p = dns_sub.add_parser(name, parents=[dns_common],
                               help=f"manually {name} a node (dry-run unless --confirm)")
        p.add_argument("node", help="node name from fleet.yaml")
        p.add_argument("--record", help="limit to one pool record")
        p.add_argument("--confirm", action="store_true", help="apply the change")

    audit = sub.add_parser("audit", help="show recent audit-log entries")
    audit.add_argument("--node", help="filter to one node")
    audit.add_argument("--limit", type=int, default=20)
    audit.add_argument("--audit-db", help="path to audit sqlite (default: ./solfleet.sqlite)")
    audit.set_defaults(func=cmd_audit)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
