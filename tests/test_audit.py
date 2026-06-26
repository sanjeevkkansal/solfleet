from solfleet.audit import AuditLog


def make_log(tmp_path):
    ticks = iter([f"2026-06-13T00:00:0{i}+00:00" for i in range(9)])
    return AuditLog(tmp_path / "audit.sqlite", clock=lambda: next(ticks))


def test_record_and_recent(tmp_path):
    log = make_log(tmp_path)
    log.record(operation="restart_node", cluster="devnet", node="node2",
               mode="dry-run", allowed=True, detail={"plan": ["stop", "start"]})
    log.record(operation="restart_node", cluster="devnet", node="node2",
               mode="execute", allowed=True, detail={"succeeded": True})

    events = log.recent()
    assert len(events) == 2
    # newest first
    assert events[0]["mode"] == "execute"
    assert events[0]["detail"]["succeeded"] is True
    assert events[1]["mode"] == "dry-run"


def test_recent_filters_by_node(tmp_path):
    log = make_log(tmp_path)
    log.record(operation="restart_node", cluster="devnet", node="node2",
               mode="execute", allowed=True, detail={})
    log.record(operation="restart_node", cluster="devnet", node="other",
               mode="execute", allowed=True, detail={})
    assert len(log.recent(node="node2")) == 1
    assert log.recent(node="node2")[0]["node"] == "node2"


def test_persists_across_instances(tmp_path):
    db = tmp_path / "audit.sqlite"
    AuditLog(db, clock=lambda: "t").record(
        operation="upgrade", cluster="devnet", node="n", mode="dry-run",
        allowed=False, detail=None)
    reopened = AuditLog(db)
    rows = reopened.recent()
    assert len(rows) == 1
    assert rows[0]["allowed"] is False
    assert rows[0]["detail"] is None
