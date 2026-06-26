# Changelog

All notable changes to this project are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Dockerfile to run the MCP server (stdio) in a container, and a `.dockerignore`.

## [0.1.1] - 2026-06-26

### Added
- MCP registry ownership marker (`mcp-name`) in the README, and `server.json`.
  Published to the official MCP registry as `io.github.sanjeevkkansal/solfleet`.

No code changes; the installable package is identical to 0.1.0.

## [0.1.0] - 2026-06-26

### Added
- First public release. MCP server and CLI for operating independent Solana
  validators and RPC nodes across devnet, testnet, and mainnet.
- Solana-aware status: slot lag, delinquency, version drift, vote credits.
- Build-and-distribute in-place upgrades: agave plus the ABI-matched Yellowstone
  geyser built from source on a builder, checksum-verified, atomic swap,
  leader-aware restart, wait for catch-up.
- Voting-validator provisioning from bare disks.
- Health-driven DNS failover (Cloudflare and Route53) with a never-empty-a-pool
  guard.
- Safety model: dry-run by default, per-cluster policy gate, SQLite audit log;
  never reads or moves keypairs.
- 15 MCP tools, 12 CLI commands, 91 tests, CI on Python 3.11-3.13, Apache-2.0.

[Unreleased]: https://github.com/sanjeevkkansal/solfleet/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/sanjeevkkansal/solfleet/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/sanjeevkkansal/solfleet/releases/tag/v0.1.0
