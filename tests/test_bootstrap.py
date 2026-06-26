"""Builder bootstrap: command content + gated orchestrator."""

from solfleet.builder import BUILD_APT_PACKAGES, bootstrap_builder, bootstrap_commands
from solfleet.config import Fleet
from solfleet.executor import CommandResult

FLEET = Fleet.model_validate({
    "clusters": {"devnet": {"reference_rpc": "r", "nodes": [
        {"name": "n", "role": "rpc", "host": "h", "rpc_url": "http://h:8899"}]}},
    "builders": {"big": {"host": "10.0.0.8", "ssh": {"user": "ubuntu"}}},
})


def test_dep_list_includes_the_commonly_missed_ones():
    assert "libclang-dev" in BUILD_APT_PACKAGES   # the one that bit us live
    assert "protobuf-compiler" in BUILD_APT_PACKAGES
    assert "build-essential" in BUILD_APT_PACKAGES


def test_bootstrap_commands_install_and_path_rust():
    joined = "\n".join(bootstrap_commands())
    assert "apt-get install" in joined and "libclang-dev" in joined
    assert "sh.rustup.rs" in joined
    # cargo/rustc symlinked onto default PATH for non-interactive ssh
    assert "/usr/local/bin/cargo" in joined
    assert "libclang.so" in joined


def test_bootstrap_dry_run_does_nothing():
    calls = []
    def runner(argv):
        calls.append(" ".join(argv))
        return CommandResult(0, "", "")  # 'true' reachability ok
    result = bootstrap_builder(FLEET, "big", confirm=False, runner=runner)
    assert result["decision"]["mode"] == "dry-run"
    assert result["decision"]["allowed"] is True
    assert result["packages"] == BUILD_APT_PACKAGES
    # only the reachability probe ran, no apt/rustup
    assert not any("apt-get" in c for c in calls)


def test_bootstrap_execute_runs_script_and_records(tmp_path):
    from solfleet.audit import AuditLog
    audit = AuditLog(tmp_path / "a.sqlite", clock=lambda: "t")
    calls = []
    def runner(argv):
        calls.append(argv[-1])
        if argv[-1] == "true":
            return CommandResult(0, "", "")
        # the combined bootstrap script
        return CommandResult(0, "BOOTSTRAP_OK\ncargo 1.96.0\nrustc 1.96.0\nlibprotoc 3.21.12\n", "")
    result = bootstrap_builder(FLEET, "big", confirm=True, runner=runner, audit=audit)
    assert result["ok"] is True
    assert any("cargo 1.96.0" in v for v in result["versions"])
    assert any("apt-get install" in c for c in calls)  # script ran
    assert audit.recent()[0]["operation"] == "bootstrap_builder"


def test_bootstrap_unreachable_builder_denied():
    def runner(argv):
        return CommandResult(255, "", "ssh: timeout")  # 'true' fails -> unreachable
    result = bootstrap_builder(FLEET, "big", confirm=True, runner=runner)
    assert result["decision"]["allowed"] is False
    assert any("not reachable" in r for r in result["decision"]["reasons"])


def test_bootstrap_unknown_builder():
    result = bootstrap_builder(FLEET, "nope", confirm=True)
    assert "error" in result
