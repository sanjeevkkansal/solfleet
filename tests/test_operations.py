"""restart_node / plan_upgrade / execute_upgrade with mocked ssh + sampler."""

import pytest

from solfleet.audit import AuditLog
from solfleet.config import Fleet
from solfleet.executor import CommandResult
from solfleet.operations import execute_upgrade, plan_upgrade, restart_node
from solfleet.safety import Policy, PolicyRules

GEYSER = "https://github.com/rpcpool/yellowstone-grpc"
GEYSER_LIB_PATH = "/usr/local/lib/libyellowstone_grpc_geyser.so"

FLEET = Fleet.model_validate({
    "clusters": {
        "devnet": {
            "reference_rpc": "https://api.devnet.solana.com",
            "install": {"strategy": "build", "builder": "dev-builder", "source": "agave",
                        "geyser_repo": GEYSER, "geyser_ref": "v1.2.0"},
            "nodes": [
                {"name": "node2", "role": "rpc", "host": "198.51.100.7",
                 "rpc_url": "https://node2.example", "ssh": {"user": "root"},
                 "service": {"geyser_lib": GEYSER_LIB_PATH,
                             "version_marker": "/usr/local/bin/.v"}},
            ],
        },
        "mainnet": {
            "reference_rpc": "https://api.mainnet-beta.solana.com",
            "install": {"strategy": "build", "builder": "mn-builder", "source": "agave",
                        "geyser_repo": GEYSER, "geyser_ref": "v1.2.0"},
            "nodes": [
                {"name": "mn-val-1", "role": "validator", "host": "10.0.0.1",
                 "identity": "ID", "rpc_url": "http://10.0.0.1:8899", "ssh": {"user": "root"},
                 "service": {"geyser_lib": GEYSER_LIB_PATH, "version_marker": "/usr/local/bin/.v"}},
            ],
        },
    },
    "builders": {
        "dev-builder": {"host": "10.0.0.9", "ssh": {"user": "ubuntu"}},
        "mn-builder": {"host": "10.0.0.9", "ssh": {"user": "ubuntu"}},
    },
})

# joined-argv responses (matches ssh remote commands and scp invocations)
RESPONSES = {
    "systemctl show": CommandResult(0, "ActiveState=active\nSubState=running\nUser=sol\n", ""),
    "agave-validator --version": CommandResult(0, "agave-validator 4.1.0-beta.2 (x)\n", ""),
    "cat ": CommandResult(0, "v4.1.0-beta.2\n", ""),
    "df -P": CommandResult(
        0, "h\n/dev/a 1G 1G 1G 24% /mnt/ledger\n/dev/b 1G 1G 1G 43% /mnt/accounts\n", ""),
    "systemctl stop": CommandResult(0, "", ""),
    "systemctl start": CommandResult(0, "", ""),
    "test -f": CommandResult(0, "", ""),               # build artifacts already cached
    "sha256sum": CommandResult(0, "HASH  /p\n", ""),    # same hash on builder + target
    "mv -f": CommandResult(0, "", ""),
    "printf": CommandResult(0, "", ""),
    "exit --min-idle-time": CommandResult(0, "", ""),
    "scp": CommandResult(0, "", ""),
}


def tracking_runner(overrides=None):
    responses = dict(RESPONSES)
    if overrides:
        responses.update(overrides)
    calls = []

    def run(argv):
        joined = " ".join(argv)
        calls.append(joined)
        for needle, result in responses.items():
            if needle in joined:
                return result
        return CommandResult(0, "", "")  # default ok (build steps when not cached)

    run.calls = calls
    return run


async def caught_up_sampler(node, reference_rpc):
    return {"healthy": True, "node_slot": 100, "ref_slot": 100, "lag": 0}


async def no_leader(node, reference_rpc, min_minutes):
    return {"safe_now": True, "next_leader_slot": None}


# ---- restart_node ---------------------------------------------------------


async def test_restart_dry_run_changes_nothing(tmp_path):
    runner = tracking_runner()
    audit = AuditLog(tmp_path / "a.sqlite", clock=lambda: "t")
    result = await restart_node(FLEET, "node2", confirm=False, policy=Policy(), audit=audit,
                                runner=runner, sampler=caught_up_sampler, leader_fn=no_leader)
    assert result["decision"]["mode"] == "dry-run"
    assert result["decision"]["allowed"] is True
    assert not any("systemctl stop" in c or "systemctl start" in c for c in runner.calls)
    assert audit.recent()[0]["mode"] == "dry-run"


async def test_restart_rpc_execute_cycles_and_records(tmp_path):
    runner = tracking_runner()
    audit = AuditLog(tmp_path / "a.sqlite", clock=lambda: "t")
    result = await restart_node(FLEET, "node2", confirm=True, policy=Policy(), audit=audit,
                                runner=runner, sampler=caught_up_sampler, leader_fn=no_leader)
    assert result["decision"]["mode"] == "execute"
    assert result["succeeded"] is True
    assert any("systemctl stop" in c for c in runner.calls)
    assert any("systemctl start" in c for c in runner.calls)
    assert audit.recent()[0]["mode"] == "execute"


async def test_restart_validator_uses_safe_exit_not_systemctl(tmp_path):
    runner = tracking_runner()
    result = await restart_node(FLEET, "mn-val-1", confirm=True,
                                policy=Policy(clusters={"mainnet": PolicyRules(require_leader_window_minutes=10)}),
                                runner=runner, sampler=caught_up_sampler, leader_fn=no_leader)
    assert result["succeeded"] is True
    # leader-aware exit, never a blunt systemctl stop
    assert any("exit --min-idle-time 10" in c for c in runner.calls)
    assert not any("systemctl stop" in c for c in runner.calls)


async def test_restart_denied_when_service_inactive():
    runner = tracking_runner({"systemctl show": CommandResult(0, "ActiveState=inactive\nSubState=dead\n", "")})
    result = await restart_node(FLEET, "node2", confirm=True, policy=Policy(),
                                runner=runner, sampler=caught_up_sampler, leader_fn=no_leader)
    assert result["decision"]["allowed"] is False
    assert any("not active" in r for r in result["decision"]["reasons"])
    assert not any("systemctl stop" in c for c in runner.calls)


async def test_restart_unknown_node():
    assert "error" in await restart_node(FLEET, "nope", confirm=True)


# ---- plan_upgrade ---------------------------------------------------------


def test_plan_upgrade_allows_matching_version():
    policy = Policy(clusters={"mainnet": PolicyRules(allowed_versions=["4.1.*"])})
    result = plan_upgrade(FLEET, "mn-val-1", "4.1.0", policy=policy)
    assert result["decision"]["allowed"] is True
    assert any("geyser" in step.lower() for step in result["decision"]["plan"])
    assert any("mn-builder" in step for step in result["decision"]["plan"])


def test_plan_upgrade_denies_disallowed_version():
    policy = Policy(clusters={"mainnet": PolicyRules(allowed_versions=["4.1.*"])})
    result = plan_upgrade(FLEET, "mn-val-1", "5.0.0", policy=policy)
    assert result["decision"]["allowed"] is False
    assert any("not in allowed_versions" in r for r in result["decision"]["reasons"])


# ---- execute_upgrade ------------------------------------------------------


async def test_execute_upgrade_dry_run_no_build(tmp_path):
    runner = tracking_runner()
    result = await execute_upgrade(FLEET, "node2", "4.1.0", confirm=False, policy=Policy(),
                                   runner=runner, sampler=caught_up_sampler)
    assert result["decision"]["mode"] == "dry-run"
    assert "build" not in result
    assert not any("cargo" in c or "mv -f" in c for c in runner.calls)


async def test_execute_upgrade_rpc_swaps_and_verifies(tmp_path):
    runner = tracking_runner()
    audit = AuditLog(tmp_path / "a.sqlite", clock=lambda: "t")
    result = await execute_upgrade(FLEET, "node2", "4.1.0", confirm=True, policy=Policy(),
                                   audit=audit, runner=runner, sampler=caught_up_sampler)
    assert result["succeeded"] is True
    assert result["version_ok"] is True
    assert result["swap"]["ok"] is True
    # geyser .so distributed alongside the binary
    names = {a["name"] for a in result["distribute"]["artifacts"]}
    assert "libyellowstone_grpc_geyser.so" in names
    assert any("systemctl stop" in c for c in runner.calls)
    assert audit.recent()[0]["operation"] == "upgrade"


async def test_execute_upgrade_validator_swaps_then_safe_exits(tmp_path):
    runner = tracking_runner()
    policy = Policy(clusters={"mainnet": PolicyRules(allowed_versions=["4.1.*"],
                                                     require_leader_window_minutes=10)})
    result = await execute_upgrade(FLEET, "mn-val-1", "4.1.0", confirm=True, policy=policy,
                                   runner=runner, sampler=caught_up_sampler)
    assert result["succeeded"] is True
    # swap happens, then leader-aware exit (not systemctl stop)
    assert result["swap"]["ok"] is True
    assert any("exit --min-idle-time 10" in c for c in runner.calls)
    assert not any("systemctl stop" in c for c in runner.calls)


async def test_execute_upgrade_denied_bad_version_no_build(tmp_path):
    runner = tracking_runner()
    policy = Policy(clusters={"mainnet": PolicyRules(allowed_versions=["4.1.*"])})
    result = await execute_upgrade(FLEET, "mn-val-1", "9.9.9", confirm=True, policy=policy,
                                   runner=runner, sampler=caught_up_sampler)
    assert result["decision"]["allowed"] is False
    assert not any("mv -f" in c for c in runner.calls)


async def test_execute_upgrade_aborts_on_sha_mismatch(tmp_path):
    # target reports a different sha than the builder -> distribute fails,
    # nothing is swapped
    runner = tracking_runner()
    calls_seen = {"n": 0}
    base = dict(RESPONSES)

    def run(argv):
        joined = " ".join(argv)
        runner.calls.append(joined)
        if "sha256sum" in joined:
            # builder hash vs target hash differ
            calls_seen["n"] += 1
            return CommandResult(0, f"HASH{calls_seen['n']}  /p\n", "")
        for needle, result in base.items():
            if needle in joined:
                return result
        return CommandResult(0, "", "")

    runner.calls = []
    result = await execute_upgrade(FLEET, "node2", "4.1.0", confirm=True, policy=Policy(),
                                   runner=run, sampler=caught_up_sampler)
    assert result["succeeded"] is False
    assert result["distribute"]["ok"] is False
    assert not any("mv -f" in c for c in runner.calls)
