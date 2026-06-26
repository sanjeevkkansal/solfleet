"""Build agave + matching geyser artifacts on a dedicated builder node.

Agave v3.0 dropped prebuilt validator binaries, so every operator now
needs a build host. solfleet builds once on the builder, caches the
artifact set keyed by version, and (in operations.execute_upgrade)
distributes + atomically swaps it on each node. The build runs over the
same SSH executor used everywhere else, so it is unit-testable with a
mock runner; only the live build correctness needs a real builder.

The geyser .so is ABI-locked to the agave/solana crate versions, so we
build it from the exact ref the operator pins (install.geyser_ref) and
ship it in the same artifact set as the validator binary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .audit import AuditLog
from .config import Fleet, InstallConfig
from .executor import Runner, SSHTarget, _subprocess_runner, run
from .safety import gate

AGAVE_BINS = ["agave-validator", "agave-ledger-tool", "solana"]
GEYSER_LIB = "libyellowstone_grpc_geyser.so"

# A bare host can't build agave. These are the apt packages required to
# compile agave + geyser from source on Debian/Ubuntu (libclang-dev is the
# one most commonly missed: clang-sys/bindgen need libclang.so).
BUILD_APT_PACKAGES = [
    "build-essential", "pkg-config", "libssl-dev", "libudev-dev",
    "protobuf-compiler", "libclang-dev", "clang", "cmake", "jq", "git", "curl",
]


@dataclass
class Artifact:
    name: str
    remote_path: str
    sha256: str | None = None


@dataclass
class BuildResult:
    version: str
    source: str
    out_dir: str
    artifacts: list[Artifact] = field(default_factory=list)
    cached: bool = False
    ok: bool = True
    error: str | None = None
    log_tail: str = ""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "source": self.source,
            "out_dir": self.out_dir,
            "artifacts": [
                {"name": a.name, "remote_path": a.remote_path, "sha256": a.sha256}
                for a in self.artifacts
            ],
            "cached": self.cached,
            "ok": self.ok,
            "error": self.error,
            "log_tail": self.log_tail,
        }


def _tag(version: str) -> str:
    return version if version.startswith("v") else f"v{version}"


def artifact_names(install: InstallConfig) -> list[str]:
    names = list(AGAVE_BINS)
    if install.geyser_repo:
        names.append(GEYSER_LIB)
    return names


def build_commands(install: InstallConfig, version: str) -> tuple[list[str], str]:
    """Ordered build steps and the output dir. Pure string assembly so it
    can be shown in a dry-run plan and asserted in tests."""
    cache = install.artifact_cache.rstrip("/")
    out = f"{cache}/{version}"
    agave_src = f"{cache}/src/agave"
    vt = _tag(version)

    cmds = [
        f"mkdir -p {out} {cache}/src",
        f"test -d {agave_src}/.git || git clone {install.agave_repo} {agave_src}",
        f"git -C {agave_src} fetch --tags origin",
        f"git -C {agave_src} checkout {vt}",
        f"cd {agave_src} && ./scripts/cargo-install-all.sh {out}/install",
    ]
    cmds += [f"cp {out}/install/bin/{b} {out}/{b}" for b in AGAVE_BINS]

    if install.geyser_repo:
        geyser_src = f"{cache}/src/geyser"
        cmds.append(f"test -d {geyser_src}/.git || git clone {install.geyser_repo} {geyser_src}")
        cmds.append(f"git -C {geyser_src} fetch --tags origin")
        if install.geyser_ref:
            cmds.append(f"git -C {geyser_src} checkout {install.geyser_ref}")
        cmds.append(f"cd {geyser_src} && cargo build --release")
        cmds.append(f"cp {geyser_src}/target/release/{GEYSER_LIB} {out}/{GEYSER_LIB}")

    return cmds, out


def _sha256(builder: SSHTarget, path: str, *, runner: Runner) -> str | None:
    r = run(builder, f"sha256sum {path}", runner=runner)
    if r.ok and r.stdout.strip():
        return r.stdout.split()[0]
    return None


def _collect(builder: SSHTarget, out: str, names: list[str], *, runner: Runner) -> list[Artifact]:
    artifacts = []
    for name in names:
        path = f"{out}/{name}"
        artifacts.append(Artifact(name=name, remote_path=path,
                                  sha256=_sha256(builder, path, runner=runner)))
    return artifacts


def _is_cached(builder: SSHTarget, out: str, names: list[str], *, runner: Runner) -> bool:
    test = " && ".join(f"test -f {out}/{n}" for n in names)
    return run(builder, test, runner=runner).ok


def build_artifacts(
    builder: SSHTarget,
    install: InstallConfig,
    version: str,
    *,
    runner: Runner = _subprocess_runner,
    force: bool = False,
) -> BuildResult:
    names = artifact_names(install)
    _cmds, out = build_commands(install, version)

    if not force and _is_cached(builder, out, names, runner=runner):
        return BuildResult(
            version=version, source=install.source, out_dir=out, cached=True,
            artifacts=_collect(builder, out, names, runner=runner),
        )

    log: list[str] = []
    cmds, out = build_commands(install, version)
    for cmd in cmds:
        r = run(builder, cmd, runner=runner)
        log.append(f"$ {cmd}\n{r.stdout}{r.stderr}".strip())
        if not r.ok:
            return BuildResult(
                version=version, source=install.source, out_dir=out, ok=False,
                error=f"build step failed: {cmd}: {r.stderr.strip() or r.exit_code}",
                log_tail="\n".join(log[-4:]),
            )

    return BuildResult(
        version=version, source=install.source, out_dir=out,
        artifacts=_collect(builder, out, names, runner=runner),
        log_tail="\n".join(log[-4:]),
    )


# ---- builder bootstrap ----------------------------------------------------


def bootstrap_commands() -> list[str]:
    """Shell steps that turn a bare Debian/Ubuntu host into an agave
    builder. Idempotent: apt is, rustup is checked, symlinks use -sf. Run
    as one script (each step assumes the previous ran in the same shell)."""
    pkgs = " ".join(BUILD_APT_PACKAGES)
    return [
        "DEBIAN_FRONTEND=noninteractive apt-get update -qq",
        f"DEBIAN_FRONTEND=noninteractive apt-get install -y -qq {pkgs}",
        "command -v rustup >/dev/null 2>&1 || "
        "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal",
        # cargo/rustc must be on the default PATH: the executor runs build
        # steps over non-interactive ssh, which does not source ~/.cargo/env
        'ln -sf "$HOME/.cargo/bin/cargo" /usr/local/bin/cargo',
        'ln -sf "$HOME/.cargo/bin/rustc" /usr/local/bin/rustc',
        'ln -sf "$HOME/.cargo/bin/rustup" /usr/local/bin/rustup',
        # clang-sys needs the C-API libclang.so; prefer the canonical llvm
        # one, fall back to the versioned lib, and expose it as libclang.so
        "for c in /usr/lib/llvm-*/lib/libclang.so /usr/lib/*/libclang-[0-9]*.so; do "
        "[ -e \"$c\" ] && ln -sf \"$c\" /usr/lib/x86_64-linux-gnu/libclang.so && break; done || true",
        "ldconfig",
    ]


def bootstrap_builder(
    fleet: Fleet,
    builder_name: str,
    *,
    confirm: bool = False,
    audit: AuditLog | None = None,
    runner: Runner = _subprocess_runner,
) -> dict:
    """Install the toolchain + build deps on a builder host. Dry-run by
    default; confirm=True runs it (one ssh, idempotent) and records to the
    audit log. Run once per builder before the first `upgrade`/`provision`."""
    builder = fleet.find_builder(builder_name)
    if builder is None:
        return {"error": f"unknown builder {builder_name!r}; "
                f"known: {sorted(fleet.builders)}"}

    plan = [
        f"apt install: {', '.join(BUILD_APT_PACKAGES)}",
        "install rustup (Rust toolchain) if missing",
        "symlink cargo/rustc/rustup into /usr/local/bin (non-interactive PATH)",
        "expose libclang.so for clang-sys; ldconfig",
    ]
    reachable = run(builder, "true", runner=runner).ok
    decision = gate(operation="bootstrap_builder", cluster="builders",
                    node=builder_name, confirm=confirm, plan=plan,
                    checks=[(reachable, "builder not reachable over SSH")])

    if not confirm or not decision.allowed:
        if audit:
            audit.record(operation="bootstrap_builder", cluster="builders",
                         node=builder_name, mode=decision.mode,
                         allowed=decision.allowed,
                         detail={"plan": plan, "packages": BUILD_APT_PACKAGES,
                                 "reasons": decision.reasons})
        return {"decision": decision.to_dict(), "packages": BUILD_APT_PACKAGES}

    script = ("set -e\n" + "\n".join(bootstrap_commands())
              + "\necho BOOTSTRAP_OK\ncargo --version; rustc --version; protoc --version")
    r = run(builder, script, runner=runner)
    ok = r.ok and "BOOTSTRAP_OK" in r.stdout
    result = {
        "decision": decision.to_dict(),
        "ok": ok,
        "versions": [ln for ln in r.stdout.splitlines()
                     if any(t in ln for t in ("cargo ", "rustc ", "libprotoc"))],
        "error": None if ok else (r.stderr.strip()[-500:] or f"exit {r.exit_code}"),
    }
    if audit:
        audit.record(operation="bootstrap_builder", cluster="builders",
                     node=builder_name, mode="execute", allowed=True, detail=result)
    return result
