from solfleet.builder import GEYSER_LIB, build_artifacts, build_commands
from solfleet.config import InstallConfig, Node

BUILDER = Node(name="mn-builder", role="rpc", host="10.0.0.9", rpc_url="http://10.0.0.9:8899",
               ssh={"user": "root"})

INSTALL = InstallConfig(
    strategy="build", builder="mn-builder", source="agave",
    geyser_repo="https://github.com/rpcpool/yellowstone-grpc",
    geyser_ref="v1.2.0",
    artifact_cache="/var/cache/solfleet/artifacts",
)


def runner_for(responses, default_ok=True):
    from solfleet.executor import CommandResult
    calls = []

    def run(argv):
        cmd = argv[-1]
        calls.append(cmd)
        for needle, result in responses.items():
            if needle in cmd:
                return result
        return CommandResult(0, "", "") if default_ok else CommandResult(1, "", "boom")

    run.calls = calls
    return run


def test_build_commands_include_tag_and_geyser():
    cmds, out = build_commands(INSTALL, "4.1.0")
    assert out == "/var/cache/solfleet/artifacts/4.1.0"
    joined = "\n".join(cmds)
    assert "checkout v4.1.0" in joined           # version prefixed with v
    assert "cargo-install-all.sh" in joined
    assert "git -C /var/cache/solfleet/artifacts/src/geyser checkout v1.2.0" in joined
    assert GEYSER_LIB in joined


def test_build_commands_without_geyser():
    install = InstallConfig(strategy="build", builder="b")
    cmds, _ = build_commands(install, "4.1.0")
    assert GEYSER_LIB not in "\n".join(cmds)


def test_cached_build_skips_compile():
    from solfleet.executor import CommandResult
    # test -f checks pass (cached), sha256sum returns a hash
    runner = runner_for({
        "test -f": CommandResult(0, "", ""),
        "sha256sum": CommandResult(0, "abc123  /path\n", ""),
    })
    result = build_artifacts(BUILDER, INSTALL, "4.1.0", runner=runner)
    assert result.cached is True
    assert result.ok is True
    assert {a.name for a in result.artifacts} == {
        "agave-validator", "agave-ledger-tool", "solana", GEYSER_LIB}
    assert all(a.sha256 == "abc123" for a in result.artifacts)
    # never ran a compile
    assert not any("cargo-install-all" in c for c in runner.calls)


def test_full_build_runs_compile_and_collects_hashes():
    from solfleet.executor import CommandResult
    runner = runner_for({
        "test -f": CommandResult(1, "", ""),          # not cached
        "sha256sum": CommandResult(0, "deadbeef  /p\n", ""),
    })  # everything else returns ok
    result = build_artifacts(BUILDER, INSTALL, "4.1.0", runner=runner)
    assert result.cached is False
    assert result.ok is True
    assert any("cargo-install-all" in c for c in runner.calls)
    assert all(a.sha256 == "deadbeef" for a in result.artifacts)


def test_build_failure_stops_and_reports():
    from solfleet.executor import CommandResult
    runner = runner_for({
        "test -f": CommandResult(1, "", ""),                       # not cached
        "cargo-install-all": CommandResult(1, "", "compile error"),
    })
    result = build_artifacts(BUILDER, INSTALL, "4.1.0", runner=runner)
    assert result.ok is False
    assert "cargo-install-all" in result.error
    # stopped before copying artifacts
    assert not any(c.startswith("cp ") and "agave-validator" in c for c in runner.calls)
