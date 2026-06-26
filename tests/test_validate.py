"""validate_fleet tests with mocked ssh runner + injected probe results."""

from solfleet.config import Fleet
from solfleet.executor import CommandResult
from solfleet.probe import ClusterStatus, NodeStatus
from solfleet.validate import validate_fleet

FLEET = Fleet.model_validate({
    "clusters": {
        "devnet": {
            "reference_rpc": "https://api.devnet.solana.com",
            "install": {"strategy": "build", "builder": "big-builder", "source": "agave"},
            "nodes": [
                {"name": "node2", "role": "rpc", "host": "10.0.0.1",
                 "rpc_url": "http://10.0.0.1:8899", "ssh": {"user": "root"}},
            ],
        }
    },
    "builders": {
        "big-builder": {"host": "10.0.0.8", "ssh": {"user": "ubuntu"}},
        "tiny-builder": {"host": "10.0.0.9", "ssh": {"user": "ubuntu"}},
    },
})

HEALTHY_NODE = NodeStatus(name="node2", cluster="devnet", role="rpc",
                          reachable=True, healthy=True, slot_lag=0, version="4.1.0-beta.2")

# big-builder is adequate; tiny-builder mimics the undersized EC2
BIG_SPECS = "cores=16\nram=68719476736\ndisk=214748364800\ntoolchain=ok\n"   # 16c/64G/200G
TINY_SPECS = "cores=2\nram=3865470976\ndisk=7516192768\ntoolchain=missing\n"  # 2c/3.6G/7G

NODE_INSPECT = {
    "systemctl show": CommandResult(0, "ActiveState=active\nSubState=running\nUser=sol\n", ""),
    "--version": CommandResult(0, "agave-validator 4.1.0-beta.2 (x)\n", ""),
    "cat ": CommandResult(0, "v4.1.0-beta.2\n", ""),
    "df -P": CommandResult(0, "h\n/dev/a 1G 1G 1G 24% /mnt/ledger\n/dev/b 1G 1G 1G 43% /mnt/accounts\n", ""),
}


def runner_for(specs_by_host):
    def run(argv):
        joined = " ".join(argv)
        # builder spec probe (single combined command contains 'nproc')
        if "nproc" in joined:
            host = next((h for h in specs_by_host if h in joined), None)
            return CommandResult(0, specs_by_host[host], "") if host else CommandResult(1, "", "no host")
        for needle, result in NODE_INSPECT.items():
            if needle in joined:
                return result
        return CommandResult(0, "", "")
    return run


async def prober(fleet):
    return [ClusterStatus(name="devnet", reference_rpc="ref", reference_slot=1000,
                          nodes=[HEALTHY_NODE])]


async def test_healthy_fleet_passes():
    runner = runner_for({"10.0.0.8": BIG_SPECS, "10.0.0.9": BIG_SPECS})
    report = await validate_fleet(FLEET, runner=runner, prober=prober)
    assert report["ok"] is True
    node = report["nodes"][0]
    assert node["status"] == "pass"
    assert {c["name"] for c in node["checks"]} >= {"ssh", "service", "binary", "disks", "rpc"}


async def test_undersized_builder_warns_not_fails():
    runner = runner_for({"10.0.0.8": BIG_SPECS, "10.0.0.9": TINY_SPECS})
    report = await validate_fleet(FLEET, runner=runner, prober=prober)
    tiny = next(b for b in report["builders"] if b["name"] == "tiny-builder")
    assert tiny["status"] == "warn"
    by = {c["name"]: c for c in tiny["checks"]}
    assert by["cores"]["status"] == "warn"
    assert by["ram"]["status"] == "warn"
    assert by["toolchain"]["status"] == "warn"
    # warnings alone keep the fleet ok
    assert report["ok"] is True


async def test_unreachable_node_fails():
    def runner(argv):
        joined = " ".join(argv)
        if "nproc" in joined:
            return CommandResult(0, BIG_SPECS, "")
        return CommandResult(255, "", "ssh: connect timed out")
    report = await validate_fleet(FLEET, runner=runner, prober=prober)
    node = report["nodes"][0]
    assert node["status"] == "fail"
    assert any(c["name"] == "ssh" and c["status"] == "fail" for c in node["checks"])
    assert report["ok"] is False


async def test_unreachable_builder_fails():
    def runner(argv):
        joined = " ".join(argv)
        if "nproc" in joined and "10.0.0.9" in joined:
            return CommandResult(255, "", "timeout")  # tiny-builder down
        if "nproc" in joined:
            return CommandResult(0, BIG_SPECS, "")
        for needle, result in NODE_INSPECT.items():
            if needle in joined:
                return result
        return CommandResult(0, "", "")
    report = await validate_fleet(FLEET, runner=runner, prober=prober)
    tiny = next(b for b in report["builders"] if b["name"] == "tiny-builder")
    assert tiny["status"] == "fail"
    assert report["ok"] is False


async def test_dns_token_warning(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    fleet = Fleet.model_validate({
        "clusters": {"devnet": {"reference_rpc": "r", "nodes": [
            {"name": "n", "role": "rpc", "host": "h", "rpc_url": "http://h:8899"}]}},
        "dns": {"provider": "cloudflare", "zone": "example.com", "pools": []},
    })
    runner = runner_for({})
    report = await validate_fleet(fleet, runner=runner, prober=prober)
    assert report["dns"]["status"] == "warn"
    assert report["dns"]["checks"][0]["name"] == "cloudflare_token"
