"""Executor tests with a mocked ssh runner (no network)."""

from solfleet.config import Node
from solfleet.executor import (
    CommandResult,
    disk_usage,
    inspect_node,
    installed_version,
    service_state,
    ssh_argv,
)

NODE = Node(
    name="node2",
    role="rpc",
    host="198.51.100.7",
    rpc_url="https://node2.example",
    ssh={"user": "root", "port": 22, "key_file": "/keys/test.pem"},
    service={"version_marker": "/usr/local/bin/.solana-validator.version"},
)


def fake_runner(responses: dict[str, CommandResult]):
    """Match by substring of the remote command (last argv element)."""
    def run(argv):
        command = argv[-1]
        for needle, result in responses.items():
            if needle in command:
                return result
        raise AssertionError(f"no fake response for: {command}")
    return run


def test_ssh_argv_includes_key_and_port():
    argv = ssh_argv(NODE, "uptime")
    assert argv[0] == "ssh"
    assert "-i" in argv and "/keys/test.pem" in argv
    assert "-p" in argv and "22" in argv
    assert argv[-2] == "root@198.51.100.7"
    assert argv[-1] == "uptime"
    assert "BatchMode=yes" in argv


def test_service_state_parses_systemctl():
    runner = fake_runner({
        "systemctl show": CommandResult(
            0,
            "ActiveState=active\nSubState=running\n"
            "ExecMainStartTimestamp=Wed 2026-06-10 06:36:38 UTC\nUser=sol\n",
            "",
        )
    })
    s = service_state(NODE, runner=runner)
    assert s["active"] == "active"
    assert s["sub"] == "running"
    assert s["user"] == "sol"
    assert "2026-06-10" in s["started_at"]
    assert s["ok"]


def test_installed_version_parses_binary_and_marker():
    runner = fake_runner({
        "--version": CommandResult(
            0, "agave-validator 4.1.0-beta.2 (src:1f1e15d2; feat:2a87b3ba)\n", ""
        ),
        "cat /usr/local/bin/.solana-validator.version": CommandResult(
            0, "v4.1.0-beta.2\n", ""
        ),
    })
    v = installed_version(NODE, runner=runner)
    assert v["binary_version"] == "4.1.0-beta.2"
    assert v["marker_version"] == "v4.1.0-beta.2"
    assert v["ok"]


def test_disk_usage_parses_df():
    runner = fake_runner({
        "df -P": CommandResult(
            0,
            "Filesystem 1G-blocks Used Available Capacity Mounted on\n"
            "/dev/nvme3n1 1800G 386G 1300G 24% /mnt/ledger\n"
            "/dev/nvme2n1 880G 355G 480G 43% /mnt/accounts\n",
            "",
        )
    })
    rows = disk_usage(NODE, runner=runner)
    assert {r["path"] for r in rows} == {"/mnt/ledger", "/mnt/accounts"}
    ledger = next(r for r in rows if r["path"] == "/mnt/ledger")
    assert ledger["use_pct"] == "24%"


def test_disk_usage_reports_error_on_failure():
    runner = fake_runner({"df -P": CommandResult(1, "", "df: no such mount")})
    rows = disk_usage(NODE, runner=runner)
    assert all("error" in r for r in rows)


def test_inspect_node_aggregates():
    runner = fake_runner({
        "systemctl show": CommandResult(0, "ActiveState=active\nSubState=running\n", ""),
        "--version": CommandResult(0, "agave-validator 4.1.0-beta.2 (x)\n", ""),
        "cat ": CommandResult(0, "v4.1.0-beta.2\n", ""),
        "df -P": CommandResult(
            0, "h\n/dev/a 1G 1G 1G 24% /mnt/ledger\n/dev/b 1G 1G 1G 43% /mnt/accounts\n", ""
        ),
    })
    result = inspect_node(NODE, runner=runner)
    assert result["name"] == "node2"
    assert result["service"]["active"] == "active"
    assert result["version"]["binary_version"] == "4.1.0-beta.2"
    assert len(result["disk"]) == 2
