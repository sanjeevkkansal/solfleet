"""Read-only fleet validation.

`solfleet validate` answers "did I configure this right, and is the fleet
ready" without changing anything. Structural validity is already enforced
by pydantic at load time; this adds a live preflight over SSH + RPC for
every node and builder, plus a check that DNS credentials are present.

Each check is pass / warn / fail. warn = works now but worth knowing
(e.g. a builder missing the Rust toolchain, which only matters at build
time). fail = something an operation would trip over. Overall is ok only
when nothing failed; warnings do not fail the run.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from .config import Fleet
from .executor import Runner, _subprocess_runner, inspect_node, run
from .probe import probe_fleet
from .safety import Policy, disk_free_ok, load_policy

GIB = 1024 ** 3
BUILDER_MIN_CORES = 8
BUILDER_MIN_RAM_GIB = 16
BUILDER_MIN_DISK_GIB = 60


@dataclass
class Check:
    name: str
    status: str  # "pass" | "warn" | "fail"
    detail: str


def _use_pcts(inspect_result: dict) -> list[int]:
    pcts = []
    for row in inspect_result.get("disk", []):
        pct = row.get("use_pct")
        if isinstance(pct, str) and pct.endswith("%") and pct[:-1].isdigit():
            pcts.append(int(pct[:-1]))
    return pcts


def _node_checks(node, cluster_name, status, rules, *, runner: Runner) -> list[Check]:
    checks: list[Check] = []
    insp = inspect_node(node, runner=runner)
    svc = insp.get("service", {})
    ssh_ok = svc.get("ok") is True
    checks.append(Check("ssh", "pass" if ssh_ok else "fail",
                        "reachable" if ssh_ok else (svc.get("error") or "unreachable")))
    if ssh_ok:
        active = svc.get("active")
        checks.append(Check("service", "pass" if active == "active" else "warn",
                            f"active={active}"))
        ver = insp.get("version", {}).get("binary_version")
        checks.append(Check("binary", "pass" if ver else "warn", ver or "no version reported"))
        missing = [d.get("path") for d in insp.get("disk", []) if "error" in d]
        checks.append(Check("disks", "fail" if missing else "pass",
                            f"missing mounts: {missing}" if missing else "mounts present"))
        if not disk_free_ok(rules, _use_pcts(insp)):
            checks.append(Check("disk_free", "warn",
                                f"below policy floor min_disk_free_pct={rules.min_disk_free_pct}"))

    if node.rpc_url:
        if status and status.reachable and status.healthy:
            checks.append(Check("rpc", "pass", f"healthy, lag={status.slot_lag}"))
        elif status and status.healthy is False:
            checks.append(Check("rpc", "warn", "reachable but behind/unhealthy"))
        else:
            checks.append(Check("rpc", "fail", "rpc unreachable"))

    if node.role == "validator" and not node.identity:
        checks.append(Check("identity", "fail", "validator has no identity pubkey"))
    return checks


def _builder_specs(builder, *, runner: Runner) -> dict:
    cmd = ("printf 'cores=%s\\n' \"$(nproc)\"; "
           "free -b | awk '/^Mem:/{print \"ram=\"$2}'; "
           "df -PB1 / | awk 'NR==2{print \"disk=\"$4}'; "
           "(command -v cargo >/dev/null && command -v rustc >/dev/null "
           "&& echo toolchain=ok || echo toolchain=missing)")
    r = run(builder, cmd, runner=runner)
    if not r.ok:
        return {"reachable": False}
    out = {"reachable": True}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _builder_checks(builder, *, runner: Runner) -> list[Check]:
    specs = _builder_specs(builder, runner=runner)
    if not specs.get("reachable"):
        return [Check("ssh", "fail", "unreachable")]
    checks = [Check("ssh", "pass", "reachable")]

    cores = int(specs["cores"]) if specs.get("cores", "").isdigit() else None
    checks.append(Check("cores",
                        "pass" if (cores or 0) >= BUILDER_MIN_CORES else "warn",
                        f"{cores} (want >= {BUILDER_MIN_CORES} for agave builds)"))

    ram = int(specs["ram"]) if specs.get("ram", "").isdigit() else None
    ram_gib = round(ram / GIB, 1) if ram else None
    checks.append(Check("ram",
                        "pass" if (ram or 0) >= BUILDER_MIN_RAM_GIB * GIB else "warn",
                        f"{ram_gib} GiB (want >= {BUILDER_MIN_RAM_GIB})"))

    disk = int(specs["disk"]) if specs.get("disk", "").isdigit() else None
    disk_gib = round(disk / GIB, 1) if disk else None
    checks.append(Check("disk_free",
                        "pass" if (disk or 0) >= BUILDER_MIN_DISK_GIB * GIB else "warn",
                        f"{disk_gib} GiB free on / (want >= {BUILDER_MIN_DISK_GIB})"))

    toolchain_ok = specs.get("toolchain") == "ok"
    checks.append(Check("toolchain", "pass" if toolchain_ok else "warn",
                        "cargo+rustc present" if toolchain_ok
                        else "no cargo/rustc (install before building)"))
    return checks


def _dns_checks(fleet: Fleet) -> list[Check]:
    if not fleet.dns:
        return []
    if fleet.dns.provider == "cloudflare":
        present = bool(os.environ.get("CLOUDFLARE_API_TOKEN"))
        return [Check("cloudflare_token", "pass" if present else "warn",
                      "CLOUDFLARE_API_TOKEN set" if present
                      else "CLOUDFLARE_API_TOKEN not set (DNS ops will fail)")]
    if fleet.dns.provider == "route53":
        present = bool(os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"))
        return [Check("aws_creds", "pass" if present else "warn",
                      "AWS creds present" if present else "no AWS creds in env")]
    return []


def _worst(checks: list[Check]) -> str:
    if any(c.status == "fail" for c in checks):
        return "fail"
    if any(c.status == "warn" for c in checks):
        return "warn"
    return "pass"


async def validate_fleet(
    fleet: Fleet,
    *,
    policy: Policy | None = None,
    runner: Runner = _subprocess_runner,
    prober=probe_fleet,
) -> dict:
    pol = policy or load_policy()
    clusters = await prober(fleet)
    status_by_name = {n.name: n for c in clusters for n in c.nodes}

    nodes = []
    for cluster_name, cluster in fleet.clusters.items():
        rules = pol.for_cluster(cluster_name)
        for node in cluster.nodes:
            checks = _node_checks(node, cluster_name, status_by_name.get(node.name),
                                  rules, runner=runner)
            nodes.append({"name": node.name, "cluster": cluster_name, "role": node.role,
                          "status": _worst(checks), "checks": [asdict(c) for c in checks]})

    builders = []
    for name, builder in fleet.builders.items():
        checks = _builder_checks(builder, runner=runner)
        builders.append({"name": name, "status": _worst(checks),
                         "checks": [asdict(c) for c in checks]})

    dns_checks = _dns_checks(fleet)
    dns = {"status": _worst(dns_checks), "checks": [asdict(c) for c in dns_checks]} if dns_checks else None

    all_statuses = ([n["status"] for n in nodes] + [b["status"] for b in builders]
                    + ([dns["status"]] if dns else []))
    ok = "fail" not in all_statuses
    return {"ok": ok, "nodes": nodes, "builders": builders, "dns": dns}
