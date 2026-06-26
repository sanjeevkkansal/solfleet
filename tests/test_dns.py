import httpx

from solfleet.dns import CloudflareDriver, InMemoryDnsDriver, marker_name


def test_in_memory_add_remove_list():
    d = InMemoryDnsDriver(initial={"rpc.example.com": ["1.1.1.1", "2.2.2.2"]})
    assert d.list_members("rpc.example.com") == ["1.1.1.1", "2.2.2.2"]
    d.remove_member("rpc.example.com", "1.1.1.1")
    assert d.list_members("rpc.example.com") == ["2.2.2.2"]
    d.add_member("rpc.example.com", "3.3.3.3", ttl=60)
    assert d.list_members("rpc.example.com") == ["2.2.2.2", "3.3.3.3"]
    assert ("remove", "rpc.example.com", "1.1.1.1") in d.ops
    assert ("add", "rpc.example.com", "3.3.3.3") in d.ops


def test_in_memory_marker():
    d = InMemoryDnsDriver()
    assert d.has_marker("rpc.example.com") is False
    d.ensure_marker("rpc.example.com")
    assert d.has_marker("rpc.example.com") is True


# ---- Cloudflare driver against a fake API (httpx.MockTransport) -----------


class FakeCloudflare:
    """Minimal in-memory Cloudflare zones/dns_records API."""

    def __init__(self):
        self.records = []  # {id,type,name,content,ttl}
        self._next = 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path  # includes the /client/v4 base prefix
        params = dict(request.url.params)
        if path.endswith("/zones"):
            return httpx.Response(200, json={"result": [{"id": "ZID"}]})
        if path.endswith("/dns_records") and request.method == "GET":
            matched = [r for r in self.records
                       if r["type"] == params.get("type") and r["name"] == params.get("name")]
            return httpx.Response(200, json={"result": matched})
        if path.endswith("/dns_records") and request.method == "POST":
            import json as _json
            body = _json.loads(request.content)
            body["id"] = f"r{self._next}"
            self._next += 1
            self.records.append(body)
            return httpx.Response(200, json={"result": body})
        if "/dns_records/" in path and request.method == "DELETE":
            rid = path.rsplit("/", 1)[-1]
            self.records = [r for r in self.records if r["id"] != rid]
            return httpx.Response(200, json={"result": {"id": rid}})
        return httpx.Response(404, json={"result": None})


def make_cf():
    fake = FakeCloudflare()
    client = httpx.Client(base_url="https://api.cloudflare.com/client/v4",
                          transport=httpx.MockTransport(fake.handler))
    return CloudflareDriver("example.com", "token", client=client), fake


def test_cloudflare_add_list_remove():
    cf, _ = make_cf()
    assert cf.list_members("rpc.example.com") == []
    cf.add_member("rpc.example.com", "1.1.1.1", ttl=60)
    cf.add_member("rpc.example.com", "2.2.2.2", ttl=60)
    assert cf.list_members("rpc.example.com") == ["1.1.1.1", "2.2.2.2"]
    # idempotent add
    cf.add_member("rpc.example.com", "1.1.1.1", ttl=60)
    assert cf.list_members("rpc.example.com") == ["1.1.1.1", "2.2.2.2"]
    cf.remove_member("rpc.example.com", "1.1.1.1")
    assert cf.list_members("rpc.example.com") == ["2.2.2.2"]


def test_cloudflare_marker_lifecycle():
    cf, _ = make_cf()
    assert cf.has_marker("rpc.example.com") is False
    cf.ensure_marker("rpc.example.com")
    assert cf.has_marker("rpc.example.com") is True
    # ensure is idempotent (no duplicate TXT)
    cf.ensure_marker("rpc.example.com")
    assert len(cf._records("TXT", marker_name("rpc.example.com"))) == 1
