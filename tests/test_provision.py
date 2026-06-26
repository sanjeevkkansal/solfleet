"""Provisioning: unit rendering, disk format guard, orchestrator paths."""

from solfleet.config import Cluster, Fleet, Node
from solfleet.executor import CommandResult
from solfleet.provision import (
    provision_node,
    render_unit,
    setup_disk,
)

GEYSER = "https://github.com/rpcpool/yellowstone-grpc"

FLEET = Fleet.model_validate({
    "clusters": {
        "devnet": {
            "reference_rpc": "https://api.devnet.solana.com",
            "install": {"strategy": "build", "builder": "b", "source": "agave",
                        "geyser_repo": GEYSER, "geyser_ref": "v1"},
            "network": {
                "entrypoints": ["entrypoint.devnet.solana.com:8001"],
                "known_validators": ["dv1deW00aaa", "dv2eW00bbb"],
                "expected_genesis_hash": "EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
            },
            "nodes": [{
                "name": "rpc1", "role": "rpc", "host": "10.0.0.1",
                "rpc_url": "http://10.0.0.1:8899", "ssh": {"user": "root"},
                "service": {"geyser_lib": "/usr/local/lib/libyellowstone_grpc_geyser.so",
                            "geyser_config": "/etc/solana-validator/geyser.json",
                            "version_marker": "/usr/local/bin/.v"},
                "disks": [
                    {"device": "/dev/nvme1n1", "mount": "/mnt/ledger", "fs": "ext4",
                     "format": True, "min_size_gb": 500},
                    {"device": "/dev/nvme2n1", "mount": "/mnt/accounts", "fs": "ext4"},
                ],
                "launch": {"rpc_port": 8899, "no_voting": True},
            }],
        }
    },
    "builders": {"b": {"host": "10.0.0.8", "ssh": {"user": "ubuntu"}}},
})

NODE = FLEET.clusters["devnet"].nodes[0]
CLUSTER = FLEET.clusters["devnet"]


# ---- unit rendering -------------------------------------------------------


def test_render_unit_contains_network_and_flags():
    unit = render_unit(NODE, CLUSTER)
    assert "User=sol" in unit
    assert "LimitNOFILE=1000000" in unit
    assert "--identity /etc/solana-validator/identity.json" in unit
    assert "--known-validator dv1deW00aaa" in unit
    assert "--entrypoint entrypoint.devnet.solana.com:8001" in unit
    assert "--expected-genesis-hash EtWTRABZaYq6iMfeYKouRu166VU2xqa1" in unit
    assert "--rpc-port 8899" in unit
    assert "--geyser-plugin-config /etc/solana-validator/geyser.json" in unit
    assert "--no-voting" in unit
    assert "Restart=always" in unit


def test_render_unit_voting_emits_vote_account():
    node = Node.model_validate({
        "name": "v", "role": "validator", "host": "h", "identity": "ID",
        "rpc_url": "http://h:8899",
        "service": {"vote_account_keypair": "/etc/solana-validator/vote-account.json"},
        "launch": {"no_voting": False},
    })
    cluster = Cluster.model_validate({"reference_rpc": "r", "nodes": [node.model_dump()]})
    unit = render_unit(node, cluster)
    assert "--vote-account /etc/solana-validator/vote-account.json" in unit
    assert "--no-voting" not in unit


def test_render_unit_omits_geyser_when_unset():
    node = Node.model_validate({
        "name": "n", "role": "rpc", "host": "h", "rpc_url": "http://h:8899",
        "service": {"geyser_config": None},
    })
    cluster = Cluster.model_validate({"reference_rpc": "r", "nodes": [node.model_dump()]})
    unit = render_unit(node, cluster)
    assert "--geyser-plugin-config" not in unit


# ---- disk format guard ----------------------------------------------------


def test_setup_disk_refuses_unacked_format():
    disk = NODE.disks[0]  # format=True
    runner = lambda argv: CommandResult(0, "", "")
    r = setup_disk(NODE, disk, allow_format=set(), runner=runner)  # not acked
    assert r["ok"] is False
    assert "not acknowledged" in r["error"]


def test_setup_disk_refuses_nonempty_device():
    disk = NODE.disks[0]
    def runner(argv):
        joined = " ".join(argv)
        if "lsblk" in joined:
            return CommandResult(0, "ext4\n", "")  # device already has a filesystem
        return CommandResult(0, "", "")
    r = setup_disk(NODE, disk, allow_format={"/dev/nvme1n1"}, runner=runner)
    assert r["ok"] is False
    assert "not empty" in r["error"]
    # never ran mkfs
    # (verified indirectly: refusal happens before mkfs)


def test_setup_disk_formats_empty_acked_device():
    disk = NODE.disks[0]
    calls = []
    def runner(argv):
        joined = " ".join(argv)
        calls.append(joined)
        if "lsblk" in joined:
            return CommandResult(0, "\n", "")  # empty
        return CommandResult(0, "", "")
    r = setup_disk(NODE, disk, allow_format={"/dev/nvme1n1"}, runner=runner)
    assert r["ok"] is True
    assert r["formatted"] is True
    assert any("mkfs.ext4" in c for c in calls)


# ---- orchestrator ---------------------------------------------------------

BIG_PREFLIGHT = "os=ubuntu22.04\narch=x86_64\ncores=16\nram=68719476736\nroot=0\n"


def make_runner(preflight_out=BIG_PREFLIGHT, identity="present"):
    def runner(argv):
        joined = " ".join(argv)
        if "uname -m" in joined:               # preflight combined command
            return CommandResult(0, preflight_out, "")
        if "lsblk" in joined:
            return CommandResult(0, "\n", "")   # disks empty
        if "test -f" in joined and "identity" in joined:
            return CommandResult(0, identity, "")
        if "sha256sum" in joined:
            return CommandResult(0, "HASH  /p\n", "")
        return CommandResult(0, "", "")          # everything else ok
    return runner


async def caught_up_sampler(node, reference_rpc):
    return {"healthy": True, "node_slot": 100, "ref_slot": 100, "lag": 0}


async def test_provision_dry_run_plans_without_acting():
    runner = make_runner()
    result = await provision_node(FLEET, "rpc1", "4.1.0", confirm=False,
                                  runner=runner, sampler=caught_up_sampler)
    assert result["decision"]["mode"] == "dry-run"
    assert result["decision"]["allowed"] is True
    assert any("systemd unit" in s for s in result["decision"]["plan"])
    assert "steps" not in result


async def test_provision_execute_full_path():
    runner = make_runner()
    result = await provision_node(FLEET, "rpc1", "4.1.0", confirm=True,
                                  allow_format={"/dev/nvme1n1"},
                                  runner=runner, sampler=caught_up_sampler)
    assert result["succeeded"] is True
    s = result["steps"]
    assert s["user"]["ok"] and s["system"]["ok"]
    assert all(d["ok"] for d in s["disks"])
    assert s["unit"]["ok"] and s["start"]["ok"]
    assert s["catchup"]["caught_up"] is True


async def test_provision_catchup_timeout_is_forwarded(monkeypatch):
    import solfleet.provision as prov
    captured = {}

    async def fake_catchup(node, ref, *, max_lag=2, sampler=None, timeout_s=600, **kw):
        captured["timeout_s"] = timeout_s
        return {"caught_up": True, "waited_s": 0, "lag": 0}

    monkeypatch.setattr(prov, "wait_for_catchup", fake_catchup)
    runner = make_runner()
    result = await provision_node(FLEET, "rpc1", "4.1.0", confirm=True,
                                  allow_format={"/dev/nvme1n1"}, runner=runner,
                                  catchup_timeout_s=99)
    assert captured["timeout_s"] == 99  # configurable value reaches wait_for_catchup
    assert result["succeeded"] is True


async def test_provision_blocks_on_small_host():
    small = "os=ubuntu22.04\narch=x86_64\ncores=2\nram=3865470976\nroot=0\n"
    runner = make_runner(preflight_out=small)
    result = await provision_node(FLEET, "rpc1", "4.1.0", confirm=True,
                                  allow_format={"/dev/nvme1n1"},
                                  runner=runner, sampler=caught_up_sampler)
    assert result["decision"]["allowed"] is False
    assert any("preflight blockers" in r for r in result["decision"]["reasons"])
    assert "steps" not in result


VOTING_FLEET = Fleet.model_validate({
    "clusters": {
        "devnet": {
            "reference_rpc": "https://api.devnet.solana.com",
            "install": {"strategy": "build", "builder": "b", "source": "agave"},
            "nodes": [{
                "name": "vote1", "role": "validator", "host": "10.0.0.5", "identity": "VOTEID",
                "rpc_url": "http://10.0.0.5:8899", "ssh": {"user": "root"},
                "service": {"vote_account_keypair": "/etc/solana-validator/vote-account.json"},
                "disks": [{"device": "/dev/nvme1n1", "mount": "/mnt/ledger", "format": False}],
                "launch": {"no_voting": False},
            }],
        }
    },
    "builders": {"b": {"host": "10.0.0.8", "ssh": {"user": "ubuntu"}}},
})


async def test_provision_voting_refuses_without_vote_key():
    # identity present (path contains 'identity'), vote keypair absent
    runner = make_runner(identity="present")
    result = await provision_node(VOTING_FLEET, "vote1", "4.1.0", confirm=True,
                                  runner=runner, sampler=caught_up_sampler)
    assert result["succeeded"] is False
    assert "vote-account keypair missing" in result["note"]
    assert "start" not in result["steps"]   # never started without the vote key


async def test_provision_refuses_to_start_without_identity():
    runner = make_runner(identity="missing")
    result = await provision_node(FLEET, "rpc1", "4.1.0", confirm=True,
                                  allow_format={"/dev/nvme1n1"},
                                  runner=runner, sampler=caught_up_sampler)
    assert result["succeeded"] is False
    assert "identity keypair missing" in result["note"]
    assert "start" not in result["steps"]  # never started without the key
