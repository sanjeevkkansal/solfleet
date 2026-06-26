from solfleet.safety import (
    Policy,
    PolicyRules,
    disk_free_ok,
    gate,
    version_allowed,
)


def test_version_glob_matching():
    rules = PolicyRules(allowed_versions=["4.1.*", "3.0.*"])
    assert version_allowed(rules, "4.1.0-beta.2")
    assert version_allowed(rules, "3.0.10")
    assert not version_allowed(rules, "2.3.6")
    assert not version_allowed(rules, None)


def test_wildcard_allows_anything():
    rules = PolicyRules(allowed_versions=["*"])
    assert version_allowed(rules, "99.0.0")


def test_disk_floor():
    rules = PolicyRules(min_disk_free_pct=20)
    assert disk_free_ok(rules, [43, 24])      # 57% and 76% free
    assert not disk_free_ok(rules, [85])      # only 15% free
    # no floor configured -> always ok
    assert disk_free_ok(PolicyRules(), [99])


def test_policy_per_cluster_lookup():
    p = Policy(
        defaults=PolicyRules(allowed_versions=["*"]),
        clusters={"mainnet": PolicyRules(allowed_versions=["4.1.*"])},
    )
    assert p.for_cluster("mainnet").allowed_versions == ["4.1.*"]
    assert p.for_cluster("devnet").allowed_versions == ["*"]  # falls to defaults


def test_gate_dry_run_is_not_execute():
    d = gate(operation="restart_node", cluster="devnet", node="n1",
             confirm=False, plan=["a", "b"], checks=[(True, "x")])
    assert d.mode == "dry-run"
    assert d.allowed is True
    assert any("confirm=true" in r for r in d.reasons)


def test_gate_dry_run_reports_blocking_checks():
    d = gate(operation="upgrade", cluster="mainnet", node="n1",
             confirm=False, plan=["a"], checks=[(False, "version not allowed")])
    assert d.mode == "dry-run"
    assert any("would block" in r for r in d.reasons)


def test_gate_execute_denied_on_failed_check():
    d = gate(operation="restart_node", cluster="mainnet", node="n1",
             confirm=True, plan=["a"], checks=[(False, "service not active")])
    assert d.mode == "execute"
    assert d.allowed is False
    assert "service not active" in d.reasons


def test_gate_execute_allowed_when_checks_pass():
    d = gate(operation="restart_node", cluster="devnet", node="n1",
             confirm=True, plan=["a"], checks=[(True, "x"), (True, "y")])
    assert d.allowed is True
    assert d.reasons == []
