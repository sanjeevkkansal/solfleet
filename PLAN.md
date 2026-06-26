# solfleet - plan

Agent-safe fleet management for independent Solana validators and RPC nodes.
One config file describes your fleet (devnet/testnet/mainnet). An MCP server
exposes Solana-aware status, safe rolling upgrades, and health-driven DNS
failover to Claude or any MCP client. Mutations are gated, audited, and
dry-run by default.

Working name: `solfleet`. Single repo, Python, pipx-installable
(same distribution model as tf-review-mcp).

## Who it's for

- Independent voting validators (1-5 nodes, often one person).
- Small RPC shops and protocol teams running their own RPC fleet across
  devnet/testnet/mainnet.
- NOT for Helius/QuickNode-scale operators. They have internal tooling.

## Why it wins

Nothing in the ecosystem combines these four things:

1. **Solana-aware health.** Route53/Cloudflare health checks only see
   HTTP 200. A node can be 500 slots behind and still return 200.
   solfleet checks slot lag vs cluster, catch-up status, delinquency,
   and version drift.
2. **Schedule-aware orchestration.** Restarting a voting validator during
   its leader slots costs money. solfleet finds safe windows (and uses
   `agave-validator wait-for-restart-window` where available).
3. **Build-and-distribute pipeline.** Agave v3.0 dropped prebuilt
   agave-validator binaries; every operator must now build from source.
   solfleet builds once on a builder node and pushes the artifact to
   the fleet. This pain point is brand new and unsolved.
4. **MCP-native interface.** The demo is "upgrade my testnet fleet to
   v3.x, then mainnet if clean" typed into Claude. No one else has this.

## Non-goals (v1)

- No web UI. MCP + CLI only. A status page can come later.
- No automated voting-validator identity failover. Double-signing risk,
  and slashing SIMDs are landing. Identity migration stays a documented
  manual procedure in v2 at the earliest, human-confirmed step by step.
- No agent daemon on nodes. Agentless over SSH (Ansible philosophy).
- No key management. solfleet never touches identity/vote keypairs.

## Architecture

```
+----------------------------------------------------------+
|  operator laptop / small VM                               |
|                                                           |
|  solfleet-mcp (stdio MCP server)   solfleet CLI           |
|        \              /                                   |
|         core library (python)                             |
|         - inventory: fleet.yaml (pydantic)                |
|         - probe:     JSON-RPC health collector            |
|         - schedule:  leader-window finder                 |
|         - orchestr.: upgrade / restart workflows          |
|         - executor:  SSH runner (fabric), idempotent steps|
|         - dns:       cloudflare + route53 drivers         |
|         - safety:    policy gate, dry-run, audit log      |
+----------------------------------------------------------+
        |  JSON-RPC (8899)        |  SSH (22)      |  HTTPS
        v                         v                v
   fleet nodes               fleet nodes      DNS provider APIs
   (+ public cluster RPC as reference for slot lag)
```

- **probe** hits each node's RPC (`getHealth`, `getSlot`, `getVersion`,
  `getVoteAccounts`, `getIdentity`) plus a reference endpoint per cluster
  to compute slot lag. SSH fallback collects disk usage and service state.
- **executor** runs a small library of idempotent steps over SSH:
  install version, restart service, wait-for-catchup, check disk. Each
  step reports before/after state. The install step works on a clean
  node too, so it doubles as software provisioning; full host bootstrap
  (sysctl, mounts, sol user, systemd unit) is an optional later step.
- **install strategies** (per cluster): as of Agave v3.0.0 Anza no
  longer publishes the agave-validator binary; operators must build
  from source (verified against the v3.0.x release notes). So `build`
  is the default strategy: compile once on a designated Linux builder
  node (same CPU family/OS as the fleet), cache the tarball keyed by
  version+target, push the artifact to fleet nodes. `prebuilt` remains
  only for legacy agave v2.x and auxiliary CLI tools. Never build on
  the operator laptop (wrong OS/arch) and never on a voting node (a
  release build saturates cores for 30-60 min). This is a fresh pain
  point v3.0 created for every small operator; solfleet owning the
  build-and-distribute pipeline is a headline feature, not an extra.
- **dns** managed records are declared in fleet.yaml; solfleet only ever
  touches records it owns (tagged via TXT marker record).
- **safety** every mutating operation: (a) requires `confirm=true`
  argument, (b) is checked against `policy.yaml` (per-cluster allowlists,
  e.g. mainnet upgrades require pinned version, no more than 1 node at a
  time), (c) defaults to dry-run plan output, (d) appends to a JSONL
  audit log.

## fleet.yaml sketch

```yaml
clusters:
  devnet:
    reference_rpc: https://api.devnet.solana.com
    nodes:
      - name: dev-rpc-1
        role: rpc            # rpc | validator
        host: 1.2.3.4
        ssh: { user: sol, port: 22 }
        rpc_url: http://1.2.3.4:8899
  mainnet:
    reference_rpc: https://api.mainnet-beta.solana.com
    install:
      strategy: build        # build (default; agave v3+ has no prebuilt
                             # validator binary) | prebuilt (v2.x legacy)
      builder: mn-builder    # Linux box, same CPU family/OS as fleet
      source: jito           # agave | jito
    nodes:
      - name: mn-val-1
        role: validator
        identity: <pubkey>   # for leader schedule + delinquency
        ...
dns:
  provider: cloudflare       # or route53
  zone: example.com
  pools:
    - record: rpc.example.com
      cluster: devnet
      members: [dev-rpc-1, dev-rpc-2]
      ttl: 60
      eject_when: { slot_lag: ">150", health: failing }
policy:
  mainnet:
    max_concurrent_restarts: 1
    allowed_versions: ["2.1.*"]
    require_leader_window_minutes: 5
```

## MCP tool surface

Read-only (no gating):
- `fleet_status` - all nodes: health, slot lag, version, delinquency, disk.
- `node_detail` - one node, full probe + recent audit entries.
- `version_drift` - fleet versions vs cluster-recommended version.
- `leader_windows` - next safe restart windows for a validator.
- `dns_status` - managed records, current members, last ejections.
- `plan_upgrade` - dry-run: ordered steps for a rolling upgrade, with
  drain/window timing. Always safe to call.

Gated (confirm=true + policy check + audit):
- `execute_upgrade` - run a planned rolling upgrade (drain from DNS,
  install, restart, wait catch-up, rejoin), one node at a time.
- `restart_node` - single node restart with catch-up verification.
- `dns_eject` / `dns_restore` - manual pool membership override.
- `set_maintenance` - mark a node out-of-rotation for the watch loop.

Watch mode (CLI, not MCP): `solfleet watch` runs the probe loop and
applies `eject_when` rules to DNS pools. Cron- or systemd-friendly.
The MCP server reads its state; it does not need to be running for MCP.

## Deployment model

- v1: stdio MCP, run locally by each operator. Credentials (SSH key,
  DNS API token) stay on the operator's machine under their own access.
- Shared state (audit log, watch-loop state) lives in SQLite from day
  one, so a team-shared hosted mode is an additive change, not a
  redesign.
- Later: streamable HTTP transport behind a flag for team use. If
  hosted, it runs on a Tailscale tailnet with bearer auth, never on the
  public internet. A network-reachable server that can restart mainnet
  validators is a high-value target; treat hosted mode as its own
  security review.

## Build status

Full v1 system implemented and unit-tested (91 tests). 15 MCP tools, 12
CLI commands. Most paths now proven live on a disposable devnet node.

Live-proven on node2 (devnet, node-as-builder):
- read path (`status`, `inspect`, `validate`, `vote-status`)
- `restart --confirm` (RPC: systemctl; validator: leader-aware safe-exit)
- `upgrade --confirm` end to end (agave 4.1.0-rc.1 built from source,
  distributed, sha256-verified, atomically swapped, caught up; src hash
  changed, confirming the swap) — for both RPC and validator branches
- `bootstrap-builder` (installed Rust + deps on a bare host)
- voting `provision --confirm` from bare disks: wiped + formatted both
  NVMes, installed, rendered + installed a voting unit (`--vote-account`),
  started, caught up, then **voting** (credits accrued, delinquent clears)

Live testing caught two real bugs unit tests couldn't: `agave-validator
exit` rejects `--monitor`, and the exit must run as the validator's
run_user (root hits a permission error on the admin socket). Both fixed.

| Area | Module | Live-tested |
|------|--------|-------------|
| Read probe + status + validate + vote-status | `probe`/`validate`/`voting` | yes (node2) |
| SSH inspect | `executor` | yes |
| Safety gate + policy + SQLite audit | `safety`/`audit` | yes |
| bootstrap-builder | `builder.bootstrap_builder` | yes (node2) |
| Builder pipeline (build agave from source) | `builder` | yes (node2) |
| restart (rpc systemctl + validator safe-exit) | `operations.restart_node` | yes (both) |
| upgrade (distribute+swap+verify, both roles) | `operations.execute_upgrade` | yes (both) |
| provision (disks/system/unit/keys, voting) | `provision` | yes (node2) |
| Leader-window finder | `schedule` | cross-checked (stake-gated) |
| DNS driver + status/eject/restore + last-member guard | `dns.CloudflareDriver`/`operations.dns_*` | yes (live Cloudflare zone) |
| DNS watch loop (probe -> decide -> act) | `watch` | decision path unit-tested; same driver now live-proven |

## Resume here (next session)

Open work, highest-value first:
1. **DNS subsystem — now live-proven against a real Cloudflare zone**
   (2026-06-26). The CloudflareDriver (zone resolve, A add/remove, TXT
   ownership marker) and `dns_status`/`dns_eject`/`dns_restore` ran end to
   end: marker created on first mutation, IPs mapped back to node names,
   dry-run gating, and last-member protection actually refused to empty a
   live pool. Test used a throwaway `sf-test-pool` record and cleaned up
   fully (zone left at its original records). Still NOT exercised live: the autonomous
   `watch` loop cycle (probe -> decide -> act); its decision logic is
   unit-tested and it uses the now-proven driver. Route53 driver remains
   unit-tested only (no AWS zone to point at).
2. **M4 ship work.** Done: CI (`.github/workflows/ci.yml`, suite across
   Python 3.11-3.13), Apache-2.0 `LICENSE`, packaging verified (wheel +
   sdist build/install clean and secret-clean; both console scripts run),
   README polish (badges + real repo path). Remaining: demo recording
   (Claude doing a fleet upgrade) and flip the repo public (re-scan for
   secrets first, even though it is already clean).
3. **M6:** streamable-HTTP transport (Tailscale + bearer auth) for team use.

Repo: `github.com/sanjeevkkansal/solfleet` (PRIVATE). Real `fleet.yaml`
and `solfleet.sqlite` are gitignored; node2-specific live-test state is in
`RESUME.local.md` (gitignored), useful only while node2 exists.

## Milestones

**M0 - read-only core (1 weekend).**
Repo scaffold, fleet.yaml + pydantic models, probe, `fleet_status` /
`node_detail` / `version_drift` over MCP, CLI `solfleet status`.
Acceptance: point it at public devnet RPC + one real node, see truthful
status in Claude.

**M1 - SSH executor + safety gate + in-place upgrade (2 weekends).**
Executor steps, safety gate (dry-run default + policy.yaml), SQLite
audit log, `restart_node` and `plan_upgrade`/`execute_upgrade` for
role=rpc nodes. Acceptance redefined (single test node available):
upgrade-in-place on node2 with bounded, measured downtime + clean
catch-up; zero-downtime rolling moves to M2 with the DNS pool.

Artifact model: a dedicated Linux builder node compiles agave + the
matching libyellowstone_grpc_geyser.so from source, caches the artifact
set, and the executor distributes + swaps binary and geyser .so
atomically. node2 itself has no toolchain and receives artifacts
(confirmed live: binary + .so both deployed at the same timestamp).
The geyser .so is ABI-locked to the agave version, so it is always
swapped in lockstep; an upgrade that forgets it bricks the node.

Acceptance: upgrade-in-place on node2 with bounded downtime + clean
catch-up (single test node; zero-downtime rolling is M2).

**M2 - DNS drivers + watch loop (1 weekend).**
Cloudflare + Route53 drivers, TXT ownership marker, `dns_*` tools,
`solfleet watch` with eject/restore rules.
Acceptance: kill a node's validator process, watch loop ejects it from
DNS within TTL+probe interval, restores after recovery.

**M3 - voting validator support (1-2 weekends).**
Leader-window finder (`getLeaderSchedule` + slot math), integrate
`agave-validator wait-for-restart-window`, policy rules for mainnet,
delinquency alerts in watch mode.
Acceptance: upgrade a devnet voting validator with zero skipped leader
slots attributable to the restart.

**M5 - provision (built, dry-run verified).**
`solfleet provision` / `provision` MCP tool: staged, idempotent bring-up
of a bare host (preflight -> user -> system tuning -> disks -> software
via the builder pipeline -> systemd unit -> identity check -> start +
catch-up). Config gained `disks`, `system`, `launch`, and cluster
`network`. Disk formatting needs an explicit `--format-device` ack on top
of `--confirm` and is refused on non-empty devices; solfleet never creates
keys. See [docs/provisioning-and-ux.md](docs/provisioning-and-ux.md).
Acceptance: bring a bare devnet RPC host to caught-up (needs a spare host;
dry-run plan + read-only preflight verified live against node2).

**M6 - transport + UX.** HTTP transport (auth, tailnet), richer live view.
`validate` and `status --watch` already shipped from this milestone.

**M4 - ship it (1 weekend).**
README as a design doc (threat model, what the agent can/can't do, why),
demo recording (Claude doing a fleet upgrade), pipx packaging, GitHub
release, post to X + Solana Tech / validator Discords.

Total: 5-7 part-time weekends. M0 alone is already demoable.

## Tech stack

- Python 3.11+, `mcp` SDK (FastMCP) - matches tf-review-mcp tooling.
- httpx for Solana JSON-RPC (no heavy solana-py dependency).
- system `ssh`/`scp` for the executor (inherits the operator's key/agent/
  known_hosts); steps are idempotent and the runner is injectable for
  unit tests.
- pydantic for fleet.yaml/policy.yaml validation.
- httpx (Cloudflare) + boto3 (route53, optional extra) behind a small
  driver interface.
- pipx-installable; `solfleet-mcp` console script for MCP registration.

## Testing

- Unit: probe/schedule/policy logic against recorded RPC fixtures.
- Integration: `solana-test-validator` locally for RPC behavior;
  a cheap devnet node (or an existing Rome devnet box if appropriate;
  keep work/personal separation clean) for SSH + upgrade paths.
- DNS: a throwaway subdomain on a personal zone, Cloudflare free tier.

## Risks

- **Double-signing**: out of scope by design; the README threat model
  says so loudly. Biggest reputational risk if mishandled, so v1 never
  moves identity.
- **Firedancer**: upgrade steps assume agave. Keep executor steps
  pluggable per client; note Firedancer support as roadmap.
- **Small market**: this is a portfolio/credibility play first. Success
  metric is stars, validator-Discord adoption, and interview material,
  not revenue.

## Portfolio framing

This becomes the public counterpart to the Rome AI-ops story: agent-safe
interfaces to dangerous infrastructure (tf-review-mcp: Terraform;
solfleet: live blockchain nodes). The README threat-model section is the
interview artifact. Nothing Rome-internal goes in.
