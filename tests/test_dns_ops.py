from solfleet.config import Fleet
from solfleet.dns import InMemoryDnsDriver
from solfleet.operations import dns_eject, dns_restore, dns_status

FLEET = Fleet.model_validate({
    "clusters": {
        "devnet": {
            "reference_rpc": "https://api.devnet.solana.com",
            "nodes": [
                {"name": "rpc1", "role": "rpc", "host": "10.0.0.1", "rpc_url": "http://10.0.0.1:8899"},
                {"name": "rpc2", "role": "rpc", "host": "10.0.0.2", "rpc_url": "http://10.0.0.2:8899"},
            ],
        }
    },
    "dns": {
        "provider": "cloudflare", "zone": "example.com",
        "pools": [{"record": "rpc.example.com", "cluster": "devnet",
                   "members": ["rpc1", "rpc2"], "ttl": 60}],
    },
})


def driver():
    return InMemoryDnsDriver(initial={"rpc.example.com": ["10.0.0.1", "10.0.0.2"]})


def test_dns_status_maps_ips_to_names():
    report = dns_status(FLEET, driver())
    pool = report["pools"][0]
    names = {m["node"] for m in pool["members"]}
    assert names == {"rpc1", "rpc2"}


def test_dns_eject_dry_run_changes_nothing():
    d = driver()
    report = dns_eject(FLEET, d, "rpc1", confirm=False)
    assert report["pools"][0]["decision"]["mode"] == "dry-run"
    assert d.list_members("rpc.example.com") == ["10.0.0.1", "10.0.0.2"]


def test_dns_eject_confirm_removes():
    d = driver()
    report = dns_eject(FLEET, d, "rpc1", confirm=True)
    assert report["pools"][0]["decision"]["allowed"] is True
    assert d.list_members("rpc.example.com") == ["10.0.0.2"]


def test_dns_eject_refuses_to_empty_pool():
    d = InMemoryDnsDriver(initial={"rpc.example.com": ["10.0.0.1"]})
    report = dns_eject(FLEET, d, "rpc1", confirm=True)
    decision = report["pools"][0]["decision"]
    assert decision["allowed"] is False
    assert any("last record" in r for r in decision["reasons"])
    assert d.list_members("rpc.example.com") == ["10.0.0.1"]  # untouched


def test_dns_restore_adds_back():
    d = InMemoryDnsDriver(initial={"rpc.example.com": ["10.0.0.2"]})
    report = dns_restore(FLEET, d, "rpc1", confirm=True)
    assert report["pools"][0]["decision"]["allowed"] is True
    assert d.list_members("rpc.example.com") == ["10.0.0.1", "10.0.0.2"]


def test_dns_eject_unknown_membership():
    report = dns_eject(FLEET, driver(), "rpc1", record="other.example.com", confirm=True)
    assert "error" in report
