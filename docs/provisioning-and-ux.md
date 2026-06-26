# Provisioning, validation, transport, and UX plan

This covers the questions about first-time install, disks, system config,
input validation, local vs HTTP, and a live-servers view. It extends the
existing model (agentless SSH, dry-run + policy gate + audit log, no key
handling) rather than replacing it. Same rule throughout: solfleet never
generates or moves identity/vote keypairs, and every destructive step is
dry-run by default, gated, and audited.

Status: design. Nothing here is built yet; it slots in as milestones
M5 (provision) and M6 (transport + UX) after the current M1-M3 code.

## 0. Builder bootstrap (bare host -> agave build host)

Agave v3+ has no prebuilt validator binary, so you need a build host, and
a bare host can't build agave out of the box. `solfleet bootstrap-builder
<name>` (dry-run by default, `--confirm` to run, idempotent, audited)
installs everything the build needs:

- apt: `build-essential pkg-config libssl-dev libudev-dev
  protobuf-compiler libclang-dev clang cmake jq git curl`
- rustup (the agave repo's `rust-toolchain.toml` selects the exact Rust
  version at build time)
- symlinks `cargo`/`rustc`/`rustup` into `/usr/local/bin` so the build,
  which runs over non-interactive ssh, finds them without sourcing
  `~/.cargo/env`
- exposes `libclang.so` so `clang-sys`/bindgen (RocksDB) can link

`libclang-dev` is the dependency most often missed: the build fails late
with "couldn't find any valid shared libraries matching libclang.so".
This was confirmed by building agave 4.1.0-rc.1 from source on a real
48-core host: the first run failed exactly there. Run bootstrap once per
builder before the first `upgrade`/`provision`.

## 1. First-time install (bare host -> serving node)

A new `solfleet provision <node>` flow, idempotent and staged. Each stage
checks current state first and skips if already done, so re-running is
safe. All of it is dry-run unless `--confirm`.

Stages:
1. **Preflight.** OS = Ubuntu 22.04+/Debian, arch = x86_64, root/sudo
   reachable, network egress to entrypoints + snapshot source, RAM/cores
   meet the cluster minimum. Refuse and report on any failure.
2. **System tuning** (section 3): `sol` user, sysctl, file limits, CPU
   governor.
3. **Disks** (section 2): format/mount ledger + accounts, fstab, chown.
4. **Install** software: reuses the existing builder + distribute +
   atomic-swap pipeline (build agave + geyser on the builder, push, swap).
   On a clean host this is a first install rather than an upgrade; same code.
5. **Render the systemd unit** from a template, filling per-node flags
   (identity path, entrypoints, known validators, expected genesis hash,
   ports, ledger/accounts paths, geyser config, `--no-voting` for RPC).
6. **Keys.** solfleet checks that the operator has placed the identity
   keypair at the configured path and refuses to start without it. It
   never creates keys.
7. **Start + wait** for snapshot download and catch-up; verify health.

`solfleet provision` therefore composes existing primitives plus three
new ones: system tuning, disk setup, unit rendering.

## 2. Ledger / accounts disks

solfleet does **not** auto-pick block devices (too dangerous). The
operator declares them per node; solfleet validates, then formats/mounts
only with explicit consent.

Proposed config (per node):

```yaml
service:
  ledger: /mnt/ledger
  accounts: /mnt/accounts
disks:
  - device: /dev/nvme1n1
    mount: /mnt/ledger
    fs: ext4              # ext4 | xfs
    format: false         # mkfs only if true AND device is empty
    min_size_gb: 500
  - device: /dev/nvme2n1
    mount: /mnt/accounts
    fs: ext4
    format: false
    min_size_gb: 500
    # or: tmpfs for accounts on very-high-RAM hosts
    # type: tmpfs
    # size_gb: 300
```

Rules and guards:
- Verify the device exists and (for `format: true`) is **unmounted and
  empty**. Refuse to mkfs a device that has a filesystem or data; that
  refusal is non-overridable without a separate explicit
  `--format-device <dev>` acknowledgement flag (defense against wiping a
  disk by typo).
- Create the mountpoint, mount, write an idempotent `/etc/fstab` entry
  (by UUID), `chown sol:sol`.
- Enforce `min_size_gb` (mainnet ledger/accounts want ~500 GB+ each;
  devnet less). Warn if ledger and accounts share one physical device
  (Solana wants them split for IOPS).
- `tmpfs` accounts supported for big-RAM hosts.

## 3. File descriptors and other system config

Managed declaratively in a `system:` block with sane defaults, rendered
and applied during provision, then read back to verify. Idempotent.

```yaml
system:
  open_files: 1000000          # LimitNOFILE in the unit + limits.d
  sysctl:
    net.core.rmem_max: 134217728
    net.core.wmem_max: 134217728
    net.core.rmem_default: 134217728
    net.core.wmem_default: 134217728
    vm.max_map_count: 1000000
    fs.nr_open: 1000000
  cpu_governor: performance
```

- systemd unit template carries `LimitNOFILE`, `LimitNPROC`, and the
  ExecStart flags; solfleet renders it from the node + cluster config.
- A solfleet-owned `/etc/sysctl.d/21-solfleet.conf` and
  `/etc/security/limits.d/90-solfleet.conf`, applied with
  `sysctl --system`, then verified by reading the live values back.
- All of this is part of the gated, audited provision path; `solfleet
  inspect` is extended to report current limits/sysctl so drift is visible.

## 4. Input and validation

Two layers, both already partly present:
- **Schema validation** (pydantic, exists): structural correctness of
  `fleet.yaml` / `policy.yaml`. Extend with disk/port/keypath/role-flag
  checks.
- **Live preflight** (the gate's `checks`, exists): per-operation, run
  against the real host before any mutation. Provisioning adds OS/arch,
  disk-empty, RAM/cores, ports-free, genesis-reachable, key-present.

New `solfleet validate` command: loads the config, runs every structural
check plus a read-only live preflight on each node, and prints a single
pass/fail report with reasons. This is the "did I configure this right"
surface, and CI-friendly (non-zero exit on problems).

Destructive steps get a second gate beyond `confirm=true`: mkfs requires
`--format-device <dev>`, and identity placement is never automated.

## 5. Local vs HTTP transport

- **Local (built):** stdio MCP, one per operator, credentials stay on the
  operator's machine. This is the default and recommended mode.
- **HTTP (planned, small):** FastMCP supports streamable HTTP. Add
  `solfleet-mcp --http --host 127.0.0.1 --port 8080 --token-env
  SOLFLEET_TOKEN`. Bearer-auth middleware required; bind to a Tailscale
  tailnet, never 0.0.0.0 on a public box. Shared state (audit, watch) is
  already SQLite, so multi-operator HTTP needs no redesign. Because an
  HTTP server that can restart mainnet validators is a high-value target,
  hosted mode is its own security review (authz per tool, rate limits,
  and likely making mutations require a second human approver).

## 6. Live-servers view (UI / prompt)

Three tiers, cheapest first:
- **MCP is the primary UI.** `fleet_status` in Claude already answers
  "what are my live servers and how are they doing." That is the product
  thesis; no extra UI needed for v1.
- **`solfleet status --watch`** (planned, cheap): a `rich`-rendered live
  table refreshing every few seconds: per-node health, slot lag, version,
  DNS pool membership, last audit action. Pure terminal, no service.
- **Read-only web status page** (optional, later): a small FastAPI app
  serving probe results + audit + pool state as a single page, behind the
  same Tailscale. Read-only by design; all mutations stay in MCP/CLI with
  the gate. No SPA, no auth sprawl.

We deliberately do not build a mutating web UI: the gated MCP/CLI is the
only path that changes nodes.

## Milestones added

- **M5 - provision:** `provision`, `validate`, disk + system + unit-render
  primitives, config schema extensions (`disks`, `system`). Acceptance:
  stand up a fresh devnet RPC node from a bare host to caught-up, idempotently.
- **M6 - transport + UX:** HTTP transport with auth, `status --watch`,
  optional read-only web page. Acceptance: drive the fleet from Claude
  over authenticated HTTP on a tailnet; live table refreshes.

## User-input summary (what the operator provides vs. what solfleet does)

| Operator provides | solfleet does |
|-------------------|----------------|
| `fleet.yaml`: hosts, SSH, roles, disks, system, install, DNS | validate, preflight, render, apply |
| Keypairs placed on the host | verify presence, never read/move/generate |
| `policy.yaml`: allowed versions, disk floor, leader window | enforce at the gate |
| `confirm` / `--format-device` consent | gate + audit every change |
| Provider creds via env (SSH key, CF token, AWS) | use transiently, never persist to repo |
