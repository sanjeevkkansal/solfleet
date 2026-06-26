"""Watch-loop tests with an in-memory DNS driver and injected probe results."""

from solfleet.config import Fleet
from solfleet.dns import InMemoryDnsDriver
from solfleet.probe import ClusterStatus, NodeStatus
from solfleet.watch import should_eject, watch_once
from solfleet.config import EjectWhen

FLEET = Fleet.model_validate({
    "clusters": {
        "devnet": {
            "reference_rpc": "https://api.devnet.solana.com",
            "nodes": [
                {"name": "rpc1", "role": "rpc", "host": "10.0.0.1", "rpc_url": "http://10.0.0.1:8899"},
                {"name": "rpc2", "role": "rpc", "host": "10.0.0.2", "rpc_url": "http://10.0.0.2:8899"},
                {"name": "rpc3", "role": "rpc", "host": "10.0.0.3", "rpc_url": "http://10.0.0.3:8899"},
            ],
        }
    },
    "dns": {
        "provider": "cloudflare",
        "zone": "example.com",
        "pools": [{
            "record": "rpc.example.com",
            "cluster": "devnet",
            "members": ["rpc1", "rpc2", "rpc3"],
            "ttl": 60,
            "eject_when": {"slot_lag": 150, "unhealthy": True},
        }],
    },
})


def status(name, *, reachable=True, healthy=True, lag=0):
    return NodeStatus(name=name, cluster="devnet", role="rpc",
                      reachable=reachable, healthy=healthy, slot_lag=lag)


def prober_returning(statuses):
    async def prober(fleet):
        return [ClusterStatus(name="devnet", reference_rpc="ref", reference_slot=1000,
                              nodes=statuses)]
    return prober


def test_should_eject_rules():
    ew = EjectWhen(slot_lag=150, unhealthy=True)
    assert should_eject(status("n", healthy=True, lag=0), ew)[0] is False
    assert should_eject(status("n", lag=300), ew)[0] is True           # lag
    assert should_eject(status("n", healthy=False), ew)[0] is True     # unhealthy
    assert should_eject(status("n", reachable=False, healthy=None), ew)[0] is True
    assert should_eject(None, ew)[0] is True                            # missing probe


async def test_lagging_node_is_ejected():
    driver = InMemoryDnsDriver(initial={"rpc.example.com": ["10.0.0.1", "10.0.0.2", "10.0.0.3"]})
    prober = prober_returning([status("rpc1"), status("rpc2", lag=500), status("rpc3")])
    report = await watch_once(FLEET, driver, prober=prober)
    pool = report["pools"][0]
    assert pool["removed"] == ["10.0.0.2"]
    assert driver.list_members("rpc.example.com") == ["10.0.0.1", "10.0.0.3"]
    assert driver.has_marker("rpc.example.com")  # marker ensured before mutating


async def test_recovered_node_is_restored():
    # rpc2 was previously ejected; now healthy -> added back
    driver = InMemoryDnsDriver(initial={"rpc.example.com": ["10.0.0.1", "10.0.0.3"]})
    prober = prober_returning([status("rpc1"), status("rpc2"), status("rpc3")])
    report = await watch_once(FLEET, driver, prober=prober)
    assert report["pools"][0]["added"] == ["10.0.0.2"]
    assert driver.list_members("rpc.example.com") == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]


async def test_last_member_protection_never_empties_pool():
    driver = InMemoryDnsDriver(initial={"rpc.example.com": ["10.0.0.1"]})
    # every member unhealthy
    prober = prober_returning([
        status("rpc1", healthy=False), status("rpc2", healthy=False),
        status("rpc3", reachable=False, healthy=None),
    ])
    report = await watch_once(FLEET, driver, prober=prober)
    pool = report["pools"][0]
    assert pool["protected"] is True
    # pool kept its record rather than going empty
    assert driver.list_members("rpc.example.com") == ["10.0.0.1"]
    assert pool["removed"] == []


async def test_dry_run_makes_no_changes():
    driver = InMemoryDnsDriver(initial={"rpc.example.com": ["10.0.0.1", "10.0.0.2", "10.0.0.3"]})
    prober = prober_returning([status("rpc1"), status("rpc2", lag=500), status("rpc3")])
    report = await watch_once(FLEET, driver, dry_run=True, prober=prober)
    assert report["pools"][0]["removed"] == ["10.0.0.2"]  # would remove
    # but nothing actually changed
    assert driver.list_members("rpc.example.com") == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    assert driver.ops == []
