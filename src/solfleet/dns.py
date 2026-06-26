"""DNS drivers for Solana-aware failover.

A pool is a DNS name (e.g. rpc.example.com) backed by one A record per
member node IP; clients round-robin across them. Ejecting a node means
removing its A record; restoring adds it back. The Solana-aware decision
(slot lag, health, delinquency) lives in watch.py; this module is just
the record plumbing.

Safety: solfleet only manages records it owns, proven by a TXT marker
record `_solfleet.<name>`. ensure_marker() is called before the first
mutation; a refusal to touch unmarked records lives in the watch/ops
layer. Drivers: Cloudflare (httpx), Route53 (boto3, lazy import), and an
in-memory driver for dry-run and tests.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from .config import DnsConfig

MARKER_CONTENT = "solfleet-managed"
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"


class DnsError(Exception):
    pass


class DnsDriver(Protocol):
    def list_members(self, record: str) -> list[str]: ...
    def add_member(self, record: str, ip: str, ttl: int) -> None: ...
    def remove_member(self, record: str, ip: str) -> None: ...
    def has_marker(self, record: str) -> bool: ...
    def ensure_marker(self, record: str) -> None: ...


def marker_name(record: str) -> str:
    return f"_solfleet.{record}"


class InMemoryDnsDriver:
    """Used for dry-run and tests. Records every mutation in `ops`."""

    def __init__(self, initial: dict[str, list[str]] | None = None,
                 markers: set[str] | None = None):
        self._a: dict[str, set[str]] = {k: set(v) for k, v in (initial or {}).items()}
        self._markers: set[str] = set(markers or [])
        self.ops: list[tuple[str, str, str]] = []  # (action, record, ip)

    def list_members(self, record: str) -> list[str]:
        return sorted(self._a.get(record, set()))

    def add_member(self, record: str, ip: str, ttl: int = 60) -> None:
        self._a.setdefault(record, set()).add(ip)
        self.ops.append(("add", record, ip))

    def remove_member(self, record: str, ip: str) -> None:
        self._a.get(record, set()).discard(ip)
        self.ops.append(("remove", record, ip))

    def has_marker(self, record: str) -> bool:
        return record in self._markers

    def ensure_marker(self, record: str) -> None:
        self._markers.add(record)


class CloudflareDriver:
    def __init__(self, zone: str, token: str, *, client: httpx.Client | None = None):
        self.zone = zone
        self._client = client or httpx.Client(
            base_url=CLOUDFLARE_API,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )
        self._zone_id: str | None = None

    def _zid(self) -> str:
        if self._zone_id is None:
            r = self._client.get("/zones", params={"name": self.zone})
            r.raise_for_status()
            result = r.json().get("result") or []
            if not result:
                raise DnsError(f"cloudflare zone {self.zone!r} not found")
            self._zone_id = result[0]["id"]
        return self._zone_id

    def _records(self, rtype: str, name: str) -> list[dict]:
        r = self._client.get(f"/zones/{self._zid()}/dns_records",
                             params={"type": rtype, "name": name})
        r.raise_for_status()
        return r.json().get("result") or []

    def list_members(self, record: str) -> list[str]:
        return sorted(rec["content"] for rec in self._records("A", record))

    def add_member(self, record: str, ip: str, ttl: int = 60) -> None:
        if ip in self.list_members(record):
            return
        r = self._client.post(f"/zones/{self._zid()}/dns_records",
                              json={"type": "A", "name": record, "content": ip, "ttl": ttl})
        r.raise_for_status()

    def remove_member(self, record: str, ip: str) -> None:
        for rec in self._records("A", record):
            if rec["content"] == ip:
                self._client.delete(
                    f"/zones/{self._zid()}/dns_records/{rec['id']}").raise_for_status()

    def has_marker(self, record: str) -> bool:
        return bool(self._records("TXT", marker_name(record)))

    def ensure_marker(self, record: str) -> None:
        if self.has_marker(record):
            return
        self._client.post(
            f"/zones/{self._zid()}/dns_records",
            json={"type": "TXT", "name": marker_name(record),
                  "content": MARKER_CONTENT, "ttl": 300}).raise_for_status()


class Route53Driver:
    """Single A record set per name with multiple ResourceRecords; add/
    remove rewrite the value list via UPSERT. boto3 is imported lazily so
    it is only required when provider=route53."""

    def __init__(self, zone: str, *, client=None):
        if client is None:
            import boto3  # lazy: optional dependency
            client = boto3.client("route53")
        self._c = client
        self.zone = zone.rstrip(".") + "."
        self._zone_id: str | None = None

    def _zid(self) -> str:
        if self._zone_id is None:
            resp = self._c.list_hosted_zones_by_name(DNSName=self.zone)
            zones = [z for z in resp.get("HostedZones", []) if z["Name"] == self.zone]
            if not zones:
                raise DnsError(f"route53 zone {self.zone!r} not found")
            self._zone_id = zones[0]["Id"]
        return self._zone_id

    def _record_set(self, rtype: str, name: str) -> dict | None:
        fqdn = name.rstrip(".") + "."
        resp = self._c.list_resource_record_sets(
            HostedZoneId=self._zid(), StartRecordName=fqdn, StartRecordType=rtype, MaxItems="1")
        for rs in resp.get("ResourceRecordSets", []):
            if rs["Name"] == fqdn and rs["Type"] == rtype:
                return rs
        return None

    def list_members(self, record: str) -> list[str]:
        rs = self._record_set("A", record)
        return sorted(r["Value"] for r in rs["ResourceRecords"]) if rs else []

    def _upsert(self, record: str, values: list[str], ttl: int) -> None:
        fqdn = record.rstrip(".") + "."
        if not values:
            rs = self._record_set("A", record)
            if rs:
                self._c.change_resource_record_sets(
                    HostedZoneId=self._zid(),
                    ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": rs}]})
            return
        self._c.change_resource_record_sets(
            HostedZoneId=self._zid(),
            ChangeBatch={"Changes": [{"Action": "UPSERT", "ResourceRecordSet": {
                "Name": fqdn, "Type": "A", "TTL": ttl,
                "ResourceRecords": [{"Value": v} for v in sorted(values)]}}]})

    def add_member(self, record: str, ip: str, ttl: int = 60) -> None:
        members = set(self.list_members(record))
        if ip not in members:
            self._upsert(record, list(members | {ip}), ttl)

    def remove_member(self, record: str, ip: str) -> None:
        members = set(self.list_members(record))
        if ip in members:
            self._upsert(record, list(members - {ip}), 60)

    def has_marker(self, record: str) -> bool:
        return self._record_set("TXT", marker_name(record)) is not None

    def ensure_marker(self, record: str) -> None:
        if self.has_marker(record):
            return
        fqdn = marker_name(record).rstrip(".") + "."
        self._c.change_resource_record_sets(
            HostedZoneId=self._zid(),
            ChangeBatch={"Changes": [{"Action": "UPSERT", "ResourceRecordSet": {
                "Name": fqdn, "Type": "TXT", "TTL": 300,
                "ResourceRecords": [{"Value": f'"{MARKER_CONTENT}"'}]}}]})


def make_driver(dns: DnsConfig, *, token: str | None = None) -> DnsDriver:
    if dns.provider == "cloudflare":
        import os
        token = token or os.environ.get("CLOUDFLARE_API_TOKEN")
        if not token:
            raise DnsError("cloudflare driver needs CLOUDFLARE_API_TOKEN")
        return CloudflareDriver(dns.zone, token)
    if dns.provider == "route53":
        return Route53Driver(dns.zone)
    raise DnsError(f"unknown dns provider {dns.provider!r}")
