"""SSH executor: runs idempotent steps on fleet nodes over the system ssh.

M1 starts with read-only inspection steps. Mutating steps (restart,
install) build on the same runner and go through the safety gate +
audit log; they are added once read inspection is verified live.

We shell out to the operator's `ssh` binary rather than embedding a
client: it already has the operator's key, agent, and known_hosts set
up, which is exactly the access model solfleet should inherit. The
runner is injectable so steps stay unit-testable without a network.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable

from .config import BuilderHost, Node

CONNECT_TIMEOUT = 15

# Anything we can SSH to: a fleet node or a build host (both have host + ssh).
SSHTarget = Node | BuilderHost


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


# A runner takes a full argv and returns (exit_code, stdout, stderr).
Runner = Callable[[list[str]], CommandResult]


def _subprocess_runner(argv: list[str]) -> CommandResult:
    proc = subprocess.run(argv, capture_output=True, text=True)
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def ssh_argv(node: SSHTarget, command: str) -> list[str]:
    ssh = node.ssh
    argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
            "-o", "StrictHostKeyChecking=accept-new"]
    if ssh.key_file:
        argv += ["-i", str(ssh.key_file)]
    argv += ["-p", str(ssh.port), f"{ssh.user}@{node.host}", command]
    return argv


def _systemctl(node: Node, verb: str) -> str:
    """systemctl needs root; prefix sudo unless we already log in as root."""
    cmd = f"systemctl {verb} {node.service.unit}"
    return cmd if node.ssh.user == "root" else f"sudo -n {cmd}"


def run(node: SSHTarget, command: str, *, runner: Runner = _subprocess_runner) -> CommandResult:
    return runner(ssh_argv(node, command))


def _scp_base(node: SSHTarget) -> list[str]:
    argv = ["scp", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
            "-o", "StrictHostKeyChecking=accept-new"]
    if node.ssh.key_file:
        argv += ["-i", str(node.ssh.key_file)]
    argv += ["-P", str(node.ssh.port)]
    return argv


def scp_from(node: SSHTarget, remote_path: str, local_path: str,
             *, runner: Runner = _subprocess_runner) -> CommandResult:
    argv = _scp_base(node) + [f"{node.ssh.user}@{node.host}:{remote_path}", local_path]
    return runner(argv)


def scp_to(node: SSHTarget, local_path: str, remote_path: str,
           *, runner: Runner = _subprocess_runner) -> CommandResult:
    argv = _scp_base(node) + [local_path, f"{node.ssh.user}@{node.host}:{remote_path}"]
    return runner(argv)


def remote_sha256(node: SSHTarget, path: str, *, runner: Runner = _subprocess_runner) -> str | None:
    r = run(node, f"sha256sum {path}", runner=runner)
    return r.stdout.split()[0] if r.ok and r.stdout.strip() else None


# ---- read-only inspection steps -------------------------------------------


def service_state(node: Node, *, runner: Runner = _subprocess_runner) -> dict:
    """systemd active/sub state and last start time for the validator unit."""
    unit = node.service.unit
    fmt = "ActiveState,SubState,ExecMainStartTimestamp,User"
    r = run(node, f"systemctl show {unit} -p {fmt} --no-pager", runner=runner)
    state: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            state[k] = v
    return {
        "unit": unit,
        "active": state.get("ActiveState"),
        "sub": state.get("SubState"),
        "user": state.get("User"),
        "started_at": state.get("ExecMainStartTimestamp") or None,
        "ok": r.ok,
        "error": None if r.ok else (r.stderr.strip() or f"exit {r.exit_code}"),
    }


def installed_version(node: Node, *, runner: Runner = _subprocess_runner) -> dict:
    """Version reported by the binary, and the version-marker file if set."""
    r = run(node, f"{node.service.binary} --version", runner=runner)
    binary_version = None
    if r.ok and r.stdout.strip():
        # "agave-validator 4.1.0-beta.2 (src:...; feat:...)"
        parts = r.stdout.split()
        if len(parts) >= 2:
            binary_version = parts[1]
    marker = None
    if node.service.version_marker:
        m = run(node, f"cat {node.service.version_marker}", runner=runner)
        if m.ok:
            marker = m.stdout.strip() or None
    return {
        "binary": node.service.binary,
        "binary_version": binary_version,
        "marker_version": marker,
        "ok": r.ok,
        "error": None if r.ok else (r.stderr.strip() or f"exit {r.exit_code}"),
    }


def disk_usage(node: Node, *, runner: Runner = _subprocess_runner) -> list[dict]:
    """df for ledger and accounts mounts; surfaces fill % before upgrades."""
    paths = [node.service.ledger, node.service.accounts]
    r = run(node, f"df -P -BG {' '.join(paths)}", runner=runner)
    rows: list[dict] = []
    if not r.ok:
        return [{"path": p, "error": r.stderr.strip() or f"exit {r.exit_code}"}
                for p in paths]
    for line in r.stdout.splitlines()[1:]:  # skip header
        cols = line.split()
        if len(cols) >= 6:
            rows.append({
                "filesystem": cols[0],
                "size": cols[1],
                "used": cols[2],
                "avail": cols[3],
                "use_pct": cols[4],
                "path": cols[5],
            })
    return rows


def inspect_node(node: Node, *, runner: Runner = _subprocess_runner) -> dict:
    """All read-only steps for one node. Safe to run anytime."""
    return {
        "name": node.name,
        "host": node.host,
        "service": service_state(node, runner=runner),
        "version": installed_version(node, runner=runner),
        "disk": disk_usage(node, runner=runner),
    }


# ---- mutating steps (only reached past the safety gate) -------------------


def stop_service(node: Node, *, runner: Runner = _subprocess_runner) -> dict:
    r = run(node, _systemctl(node, "stop"), runner=runner)
    return {"action": "stop", "unit": node.service.unit, "ok": r.ok,
            "error": None if r.ok else (r.stderr.strip() or f"exit {r.exit_code}")}


def start_service(node: Node, *, runner: Runner = _subprocess_runner) -> dict:
    r = run(node, _systemctl(node, "start"), runner=runner)
    return {"action": "start", "unit": node.service.unit, "ok": r.ok,
            "error": None if r.ok else (r.stderr.strip() or f"exit {r.exit_code}")}


def validator_safe_exit(node: Node, *, min_idle_minutes: int,
                        max_delinquent_stake: int = 5,
                        runner: Runner = _subprocess_runner) -> dict:
    """Graceful, leader-aware exit for a voting validator. agave-validator
    exit blocks until a restart window with no leader slots for
    min_idle_minutes, then exits; systemd (Restart=always) relaunches it.
    This is how you restart a validator without skipping your own slots."""
    # `agave-validator exit` waits for a restart window with no leader slots
    # for min-idle-time, then exits; systemd (Restart=always) relaunches.
    # (No --monitor flag: agave's exit subcommand does not accept it.)
    base = (f"{node.service.binary} --ledger {node.service.ledger} exit "
            f"--min-idle-time {min_idle_minutes} "
            f"--max-delinquent-stake {max_delinquent_stake}")
    # the validator and its admin socket are owned by the run_user; the exit
    # must run as that user (running as root hits "Permission denied" on the
    # admin RPC), so drop to run_user when we log in as someone else.
    cmd = base if node.ssh.user == node.service.run_user \
        else f"sudo -u {node.service.run_user} {base}"
    r = run(node, cmd, runner=runner)
    return {"action": "safe_exit", "min_idle_minutes": min_idle_minutes,
            "ok": r.ok, "error": None if r.ok else (r.stderr.strip() or f"exit {r.exit_code}")}


def atomic_swap(node: Node, swaps: list[tuple[str, str, bool]],
                *, runner: Runner = _subprocess_runner) -> dict:
    """Move each staged file onto its destination with `mv -f` (atomic
    within a filesystem). swaps: (staged_path, dest_path, is_executable).
    Staged files must already sit beside their destination (same fs)."""
    parts = []
    for staged, dest, is_exec in swaps:
        parts.append(f"mv -f {staged} {dest}")
        if is_exec:
            parts.append(f"chmod 755 {dest}")
    r = run(node, " && ".join(parts), runner=runner)
    return {"action": "swap", "count": len(swaps), "ok": r.ok,
            "error": None if r.ok else (r.stderr.strip() or f"exit {r.exit_code}")}
