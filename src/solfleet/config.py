"""fleet.yaml loading and validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

DEFAULT_CONFIG_PATHS = ("fleet.yaml", "fleet.yml")
CONFIG_ENV_VAR = "SOLFLEET_CONFIG"


class SSHConfig(BaseModel):
    user: str = "sol"
    port: int = 22
    key_file: Path | None = None


class ServiceConfig(BaseModel):
    """How the validator runs on the box. Defaults match the common
    agave layout (systemd unit + /usr/local/bin + /mnt paths)."""

    unit: str = "solana-validator"
    binary: str = "/usr/local/bin/agave-validator"
    version_marker: str | None = None
    ledger: str = "/mnt/ledger"
    accounts: str = "/mnt/accounts"
    # geyser plugin .so, ABI-locked to the agave version; swapped in
    # lockstep on upgrade. None means the node runs no geyser plugin.
    geyser_lib: str | None = None
    # provisioning: the OS user the validator runs as, the keypair the
    # operator places (solfleet never creates it), and the geyser plugin
    # config json passed to --geyser-plugin-config.
    run_user: str = "sol"
    identity_keypair: str = "/etc/solana-validator/identity.json"
    # voting validators only: the vote-account keypair the operator places
    # (solfleet never creates it). Rendered as --vote-account when the node
    # votes. Its authorized voter must be the identity above.
    vote_account_keypair: str | None = None
    geyser_config: str | None = None


class DiskConfig(BaseModel):
    """A data disk for the node. solfleet never auto-picks devices; the
    operator names them. Formatting is refused unless the device is empty
    AND explicitly acknowledged at provision time."""

    device: str                       # /dev/nvme1n1, or "tmpfs"
    mount: str                        # /mnt/ledger
    fs: Literal["ext4", "xfs", "tmpfs"] = "ext4"
    format: bool = False              # mkfs only if true, empty, and acked
    min_size_gb: int | None = None
    size_gb: int | None = None        # tmpfs size


def _default_sysctl() -> dict[str, int]:
    return {
        "net.core.rmem_max": 134217728,
        "net.core.wmem_max": 134217728,
        "net.core.rmem_default": 134217728,
        "net.core.wmem_default": 134217728,
        "vm.max_map_count": 1000000,
        "fs.nr_open": 1000000,
    }


class SystemTuning(BaseModel):
    """Host tuning applied during provisioning (systemd limits + sysctl +
    CPU governor). Defaults match Anza's validator requirements."""

    open_files: int = 1000000
    sysctl: dict[str, int] = Field(default_factory=_default_sysctl)
    cpu_governor: str | None = "performance"


class LaunchConfig(BaseModel):
    """Node-level agave launch flags. Network-wide params (entrypoints,
    known validators, genesis hash) live on the cluster."""

    rpc_port: int = 8899
    rpc_bind_address: str = "127.0.0.1"
    private_rpc: bool = True
    full_rpc_api: bool = True
    no_voting: bool = True            # rpc-only by default
    limit_ledger_size: int | None = 100000000
    enable_rpc_tx_history: bool = True
    extra_args: list[str] = Field(default_factory=list)


class Node(BaseModel):
    name: str
    role: Literal["rpc", "validator"]
    host: str
    rpc_url: str | None = None
    identity: str | None = None
    ssh: SSHConfig = SSHConfig()
    service: ServiceConfig = ServiceConfig()
    disks: list[DiskConfig] = Field(default_factory=list)
    system: SystemTuning = SystemTuning()
    launch: LaunchConfig = LaunchConfig()

    @model_validator(mode="after")
    def check_role_fields(self) -> "Node":
        if self.role == "rpc" and not self.rpc_url:
            raise ValueError(f"node {self.name}: role=rpc requires rpc_url")
        if self.role == "validator" and not self.identity:
            raise ValueError(f"node {self.name}: role=validator requires identity")
        return self


class InstallConfig(BaseModel):
    # agave v3+ ships no prebuilt validator binary; prebuilt is v2.x legacy
    strategy: Literal["build", "prebuilt"] = "build"
    builder: str | None = None
    source: Literal["agave", "jito"] = "agave"
    agave_repo: str = "https://github.com/anza-xyz/agave"
    # yellowstone geyser source; built against the matching agave version
    geyser_repo: str | None = None
    # git ref of the geyser repo whose deps match this agave version; the
    # operator owns picking it (ABI must match) and we build exactly that
    geyser_ref: str | None = None
    # where the builder caches artifact sets, keyed by version
    artifact_cache: str = "/var/cache/solfleet/artifacts"

    @model_validator(mode="after")
    def check_builder(self) -> "InstallConfig":
        if self.strategy == "build" and not self.builder:
            raise ValueError("install.strategy=build requires install.builder")
        return self


class SolanaNetwork(BaseModel):
    """Cluster-wide network identity used to render launch flags and to
    join a node at provision time."""

    entrypoints: list[str] = Field(default_factory=list)
    known_validators: list[str] = Field(default_factory=list)
    expected_genesis_hash: str | None = None


class Cluster(BaseModel):
    reference_rpc: str
    install: InstallConfig | None = None
    network: SolanaNetwork | None = None
    nodes: list[Node]

    @model_validator(mode="after")
    def check_unique_names(self) -> "Cluster":
        names = [n.name for n in self.nodes]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate node names: {sorted(dupes)}")
        return self


class BuilderHost(BaseModel):
    """A build host. Not a serving fleet member: it compiles agave +
    geyser and hands artifacts to the executor. Needs lots of cores/RAM/
    disk; never a voting node, never the operator's laptop."""

    host: str
    ssh: SSHConfig = SSHConfig()


class EjectWhen(BaseModel):
    """Conditions that pull a node out of a DNS pool. A node is ejected
    if any enabled condition is met; restored only when all clear."""

    slot_lag: int | None = None  # eject if lag strictly greater than this
    unhealthy: bool = True       # eject if getHealth != ok or unreachable
    delinquent: bool = False     # (validators) eject if delinquent


class Pool(BaseModel):
    record: str                  # e.g. rpc.example.com
    cluster: str
    members: list[str]           # node names, all must be in `cluster`
    ttl: int = 60
    eject_when: EjectWhen = EjectWhen()


class DnsConfig(BaseModel):
    provider: Literal["cloudflare", "route53"]
    zone: str
    pools: list[Pool] = []


class Fleet(BaseModel):
    clusters: dict[str, Cluster]
    builders: dict[str, BuilderHost] = {}
    dns: DnsConfig | None = None

    @model_validator(mode="after")
    def check_builders_exist(self) -> "Fleet":
        for name, cluster in self.clusters.items():
            inst = cluster.install
            if inst and inst.strategy == "build" and inst.builder:
                if inst.builder not in self.builders:
                    raise ValueError(
                        f"cluster {name}: install.builder {inst.builder!r} is not "
                        "defined in the top-level builders: map"
                    )
        return self

    def find_builder(self, name: str | None) -> BuilderHost | None:
        return self.builders.get(name) if name else None

    @model_validator(mode="after")
    def check_pool_members(self) -> "Fleet":
        if not self.dns:
            return self
        for pool in self.dns.pools:
            cluster = self.clusters.get(pool.cluster)
            if cluster is None:
                raise ValueError(f"pool {pool.record}: unknown cluster {pool.cluster!r}")
            names = {n.name for n in cluster.nodes}
            missing = [m for m in pool.members if m not in names]
            if missing:
                raise ValueError(
                    f"pool {pool.record}: members not in cluster {pool.cluster}: {missing}"
                )
        return self

    def find_node(self, name: str) -> tuple[str, Cluster, Node] | None:
        for cluster_name, cluster in self.clusters.items():
            for node in cluster.nodes:
                if node.name == name:
                    return cluster_name, cluster, node
        return None


def resolve_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get(CONFIG_ENV_VAR)
    if env:
        return Path(env)
    for candidate in DEFAULT_CONFIG_PATHS:
        p = Path(candidate)
        if p.exists():
            return p
    raise FileNotFoundError(
        f"no fleet config found; pass --config, set {CONFIG_ENV_VAR}, "
        f"or create {DEFAULT_CONFIG_PATHS[0]} in the working directory"
    )


def load_fleet(path: str | Path | None = None) -> Fleet:
    resolved = resolve_config_path(str(path) if path else None)
    with open(resolved) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{resolved}: expected a mapping at the top level")
    return Fleet.model_validate(raw)
