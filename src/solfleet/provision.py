"""First-time host provisioning: bare box -> serving node.

Staged and idempotent (each stage checks current state and skips if
already done), dry-run by default, gated, audited. solfleet never creates
or moves keypairs: it verifies the operator has placed the identity file
and refuses to start without it. Disk formatting is refused unless the
device is empty AND the operator explicitly acknowledges that device.

Software install reuses the builder pipeline (build -> distribute ->
atomic swap), so a fresh host is just "install" instead of "upgrade".
Like the builder code, this is unit-tested with a mock runner; live
correctness needs a real bare host.
"""

from __future__ import annotations

from .audit import AuditLog
from .builder import AGAVE_BINS, build_artifacts
from .config import Cluster, Fleet, Node
from .executor import (
    Runner,
    _subprocess_runner,
    atomic_swap,
    run,
    start_service,
)
from .operations import default_sampler, distribute, wait_for_catchup
from .safety import Policy, gate, load_policy

GIB = 1024 ** 3
PROVISION_MIN_RAM_GIB = 8
PROVISION_MIN_CORES = 8


# ---- unit rendering (pure) ------------------------------------------------


def render_unit(node: Node, cluster: Cluster) -> str:
    """Render the systemd unit text for a node from its launch config and
    the cluster network. Pure: no I/O, fully testable."""
    svc = node.service
    lc = node.launch
    net = cluster.network

    args = [svc.binary, f"--identity {svc.identity_keypair}"]
    for kv in (net.known_validators if net else []):
        args.append(f"--known-validator {kv}")
    args.append(f"--rpc-port {lc.rpc_port}")
    args.append(f"--rpc-bind-address {lc.rpc_bind_address}")
    for ep in (net.entrypoints if net else []):
        args.append(f"--entrypoint {ep}")
    if net and net.expected_genesis_hash:
        args.append(f"--expected-genesis-hash {net.expected_genesis_hash}")
    args.append("--wal-recovery-mode skip_any_corrupted_record")
    if lc.limit_ledger_size is not None:
        args.append(f"--limit-ledger-size {lc.limit_ledger_size}")
    args.append(f"--ledger {svc.ledger}")
    args.append(f"--accounts {svc.accounts}")
    if lc.enable_rpc_tx_history:
        args.append("--enable-rpc-transaction-history")
        args.append("--enable-extended-tx-metadata-storage")
    if lc.private_rpc:
        args.append("--private-rpc")
    if lc.full_rpc_api:
        args.append("--full-rpc-api")
    if svc.geyser_config:
        args.append(f"--geyser-plugin-config {svc.geyser_config}")
    if lc.no_voting:
        args.append("--no-voting")
    elif svc.vote_account_keypair:
        args.append(f"--vote-account {svc.vote_account_keypair}")
    args.extend(lc.extra_args)
    args.append("--log -")

    exec_start = " \\\n  ".join(args)
    return (
        f"[Unit]\n"
        f"Description=Solana validator ({node.name})\n"
        f"After=network-online.target\n"
        f"Wants=network-online.target\n\n"
        f"[Service]\n"
        f"Type=simple\n"
        f"User={svc.run_user}\n"
        f"Group={svc.run_user}\n"
        f"LimitNOFILE={node.system.open_files}\n"
        f"Restart=always\n"
        f"RestartSec=1\n"
        f"ExecStart={exec_start}\n\n"
        f"[Install]\n"
        f"WantedBy=multi-user.target\n"
    )


# ---- idempotent stages (each returns a result dict) -----------------------


def preflight(node: Node, *, runner: Runner = _subprocess_runner) -> dict:
    cmd = ("printf 'os=%s\\n' \"$(. /etc/os-release 2>/dev/null; echo $ID$VERSION_ID)\"; "
           "printf 'arch=%s\\n' \"$(uname -m)\"; "
           "printf 'cores=%s\\n' \"$(nproc)\"; "
           "free -b | awk '/^Mem:/{print \"ram=\"$2}'; "
           "printf 'root=%s\\n' \"$(id -u)\"")
    r = run(node, cmd, runner=runner)
    info: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            info[k] = v
    cores = int(info["cores"]) if info.get("cores", "").isdigit() else None
    ram = int(info["ram"]) if info.get("ram", "").isdigit() else None
    checks = [
        (r.ok, "host not reachable over SSH"),
        (info.get("arch") == "x86_64", f"unsupported arch {info.get('arch')!r} (need x86_64)"),
        ((cores or 0) >= PROVISION_MIN_CORES, f"only {cores} cores (want >= {PROVISION_MIN_CORES})"),
        ((ram or 0) >= PROVISION_MIN_RAM_GIB * GIB,
         f"only {round((ram or 0)/GIB,1)} GiB RAM (want >= {PROVISION_MIN_RAM_GIB})"),
    ]
    return {"info": info, "checks": [{"ok": ok, "detail": msg} for ok, msg in checks],
            "blockers": [msg for ok, msg in checks if not ok]}


def ensure_user(node: Node, *, runner: Runner = _subprocess_runner) -> dict:
    user = node.service.run_user
    cmd = f"id -u {user} >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin {user}"
    r = run(node, cmd, runner=runner)
    return {"action": "ensure_user", "user": user, "ok": r.ok,
            "error": None if r.ok else (r.stderr.strip() or f"exit {r.exit_code}")}


def apply_system(node: Node, *, runner: Runner = _subprocess_runner) -> dict:
    sysctl_lines = "\n".join(f"{k} = {v}" for k, v in node.system.sysctl.items())
    cmds = [
        f"install -d -m 755 /etc/sysctl.d /etc/security/limits.d",
        f"printf '%s\\n' '{sysctl_lines}' > /etc/sysctl.d/21-solfleet.conf",
        "sysctl --system >/dev/null",
        f"printf '%s soft nofile {node.system.open_files}\\n"
        f"%s hard nofile {node.system.open_files}\\n' "
        f"'{node.service.run_user}' '{node.service.run_user}' "
        f"> /etc/security/limits.d/90-solfleet.conf",
    ]
    if node.system.cpu_governor:
        cmds.append(
            "for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do "
            f"[ -w \"$g\" ] && echo {node.system.cpu_governor} > \"$g\" || true; done")
    r = run(node, " && ".join(cmds), runner=runner)
    return {"action": "apply_system", "open_files": node.system.open_files,
            "sysctl_keys": list(node.system.sysctl), "governor": node.system.cpu_governor,
            "ok": r.ok, "error": None if r.ok else (r.stderr.strip() or f"exit {r.exit_code}")}


def setup_disk(node: Node, disk, *, allow_format: set[str], runner: Runner = _subprocess_runner) -> dict:
    user = node.service.run_user
    if disk.fs == "tmpfs":
        size = f"size={disk.size_gb}G" if disk.size_gb else "size=50%"
        cmds = [f"mkdir -p {disk.mount}",
                f"grep -q ' {disk.mount} ' /etc/fstab || "
                f"echo 'tmpfs {disk.mount} tmpfs {size} 0 0' >> /etc/fstab",
                f"mountpoint -q {disk.mount} || mount {disk.mount}",
                f"chown {user}:{user} {disk.mount}"]
        r = run(node, " && ".join(cmds), runner=runner)
        return {"action": "setup_disk", "mount": disk.mount, "fs": "tmpfs",
                "formatted": False, "ok": r.ok,
                "error": None if r.ok else (r.stderr.strip() or f"exit {r.exit_code}")}

    # block device: inspect emptiness/mount before any destructive step
    probe = run(node, f"lsblk -no FSTYPE {disk.device} 2>/dev/null; "
                      f"findmnt -nro TARGET {disk.device} 2>/dev/null", runner=runner)
    existing = probe.stdout.strip()
    empty = existing == ""

    formatted = False
    if disk.format:
        if disk.device not in allow_format:
            return {"action": "setup_disk", "mount": disk.mount, "ok": False,
                    "error": f"format requested but {disk.device} not acknowledged "
                             "(pass --format-device to allow)"}
        if not empty:
            return {"action": "setup_disk", "mount": disk.mount, "ok": False,
                    "error": f"refusing to format {disk.device}: not empty/unmounted "
                             f"({existing!r})"}
        mkfs = run(node, f"mkfs.{disk.fs} -q {disk.device}", runner=runner)
        if not mkfs.ok:
            return {"action": "setup_disk", "mount": disk.mount, "ok": False,
                    "error": f"mkfs failed: {mkfs.stderr.strip() or mkfs.exit_code}"}
        formatted = True

    cmds = [
        f"mkdir -p {disk.mount}",
        f"uuid=$(blkid -s UUID -o value {disk.device}) && "
        f"(grep -q \"$uuid\" /etc/fstab || "
        f"echo \"UUID=$uuid {disk.mount} {disk.fs} defaults,noatime 0 2\" >> /etc/fstab)",
        f"mountpoint -q {disk.mount} || mount {disk.mount}",
        f"chown {user}:{user} {disk.mount}",
    ]
    r = run(node, " && ".join(cmds), runner=runner)
    return {"action": "setup_disk", "mount": disk.mount, "device": disk.device,
            "fs": disk.fs, "formatted": formatted, "was_empty": empty, "ok": r.ok,
            "error": None if r.ok else (r.stderr.strip() or f"exit {r.exit_code}")}


def install_unit(node: Node, unit_text: str, *, runner: Runner = _subprocess_runner) -> dict:
    path = f"/etc/systemd/system/{node.service.unit}.service"
    # write atomically then reload
    cmd = (f"install -d -m 755 /etc/solana-validator; "
           f"cat > {path}.solfleet-new <<'SOLFLEET_UNIT'\n{unit_text}\nSOLFLEET_UNIT\n"
           f"mv -f {path}.solfleet-new {path} && systemctl daemon-reload && "
           f"systemctl enable {node.service.unit} >/dev/null 2>&1 || true")
    r = run(node, cmd, runner=runner)
    return {"action": "install_unit", "path": path, "ok": r.ok,
            "error": None if r.ok else (r.stderr.strip() or f"exit {r.exit_code}")}


def check_identity_key(node: Node, *, runner: Runner = _subprocess_runner) -> dict:
    path = node.service.identity_keypair
    r = run(node, f"test -f {path} && echo present || echo missing", runner=runner)
    present = r.ok and r.stdout.strip() == "present"
    return {"action": "check_identity_key", "path": path, "present": present,
            "note": "solfleet never creates keys; place the identity keypair here"}


def _is_voting(node: Node) -> bool:
    return node.role == "validator" and not node.launch.no_voting


def check_vote_key(node: Node, *, runner: Runner = _subprocess_runner) -> dict:
    """For voting validators only: confirm the vote-account keypair is in
    place. solfleet never creates it; the operator creates + funds the vote
    account out of band."""
    path = node.service.vote_account_keypair
    if not path:
        return {"action": "check_vote_key", "path": None, "present": False,
                "note": "voting node has no service.vote_account_keypair set"}
    r = run(node, f"test -f {path} && echo present || echo missing", runner=runner)
    present = r.ok and r.stdout.strip() == "present"
    return {"action": "check_vote_key", "path": path, "present": present}


# ---- orchestrator ---------------------------------------------------------


def _provision_plan(node: Node, cluster: Cluster, target_version: str) -> list[str]:
    install = cluster.install
    geyser = " + geyser .so" if install and install.geyser_repo else ""
    disk_steps = [f"setup disk {d.device} -> {d.mount} (format={d.format})" for d in node.disks]
    return [
        f"preflight {node.name} (OS / arch / cores / RAM / root)",
        f"create user {node.service.run_user}",
        f"apply system tuning (open_files={node.system.open_files}, sysctl, governor)",
        *disk_steps,
        f"build {install.source if install else 'agave'} {target_version} on "
        f"builder {install.builder if install else None!r}",
        f"distribute + place agave binary{geyser}",
        f"install systemd unit {node.service.unit}.service",
        f"verify identity keypair at {node.service.identity_keypair}",
        f"start {node.service.unit} and wait for catch-up",
    ]


async def provision_node(
    fleet: Fleet,
    node_name: str,
    target_version: str,
    *,
    confirm: bool = False,
    allow_format: set[str] | None = None,
    policy: Policy | None = None,
    audit: AuditLog | None = None,
    runner: Runner = _subprocess_runner,
    sampler=default_sampler,
    max_lag: int = 2,
    catchup_timeout_s: int = 1800,
) -> dict:
    allow_format = set(allow_format or [])
    found = fleet.find_node(node_name)
    if not found:
        return {"error": f"unknown node {node_name!r}"}
    cluster_name, cluster, node = found
    install = cluster.install
    builder_node = fleet.find_builder(install.builder) if install else None

    pre = preflight(node, runner=runner)
    plan = _provision_plan(node, cluster, target_version)
    checks = [
        (not pre["blockers"], "preflight blockers: " + "; ".join(pre["blockers"])),
        (install is not None and install.builder is not None,
         "cluster has no install.builder (provision installs software)"),
        (builder_node is not None,
         f"builder {install.builder if install else None!r} not found in fleet"),
    ]
    decision = gate(operation="provision", cluster=cluster_name, node=node_name,
                    confirm=confirm, plan=plan, checks=checks)

    if not confirm or not decision.allowed:
        if audit:
            audit.record(operation="provision", cluster=cluster_name, node=node_name,
                         mode=decision.mode, allowed=decision.allowed,
                         detail={"plan": plan, "reasons": decision.reasons, "preflight": pre})
        return {"decision": decision.to_dict(), "preflight": pre}

    def finish(steps, succeeded, note=None):
        result = {"decision": decision.to_dict(), "steps": steps, "succeeded": succeeded}
        if note:
            result["note"] = note
        if audit:
            audit.record(operation="provision", cluster=cluster_name, node=node_name,
                         mode="execute", allowed=True, detail=result)
        return result

    steps: dict = {}
    steps["user"] = ensure_user(node, runner=runner)
    steps["system"] = apply_system(node, runner=runner)
    steps["disks"] = [setup_disk(node, d, allow_format=allow_format, runner=runner)
                      for d in node.disks]
    if any(not d["ok"] for d in steps["disks"]):
        return finish(steps, False, "disk setup failed; not installing software")

    build = build_artifacts(builder_node, install, target_version, runner=runner)
    steps["build"] = {"ok": build.ok, "cached": build.cached, "error": build.error}
    if not build.ok:
        return finish(steps, False)

    dist = distribute(builder_node, node, build.artifacts, runner=runner)
    steps["distribute"] = {"ok": dist["ok"], "artifacts": dist["artifacts"]}
    if not dist["ok"]:
        return finish(steps, False, "artifact distribution/verification failed")

    swaps = [(e["staged"], e["dest"], e["name"] in AGAVE_BINS)
             for e in dist["artifacts"] if e.get("sha_match")]
    steps["swap"] = atomic_swap(node, swaps, runner=runner)
    steps["unit"] = install_unit(node, render_unit(node, cluster), runner=runner)

    steps["identity"] = check_identity_key(node, runner=runner)
    if not steps["identity"]["present"]:
        return finish(steps, False,
                      "identity keypair missing; place it on the host and re-run")

    if _is_voting(node):
        steps["vote_key"] = check_vote_key(node, runner=runner)
        if not steps["vote_key"]["present"]:
            return finish(steps, False,
                          "voting node: vote-account keypair missing; create+fund "
                          "the vote account, place the keypair, and re-run")

    steps["start"] = start_service(node, runner=runner)
    steps["catchup"] = await wait_for_catchup(node, cluster.reference_rpc,
                                              max_lag=max_lag, sampler=sampler,
                                              timeout_s=catchup_timeout_s)

    succeeded = bool(
        steps["user"]["ok"] and steps["system"]["ok"]
        and all(d["ok"] for d in steps["disks"]) and steps["swap"]["ok"]
        and steps["unit"]["ok"] and steps["start"]["ok"]
        and steps["catchup"].get("caught_up"))
    return finish(steps, succeeded)
