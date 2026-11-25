import json

from helpers import FakeClock

from mcp_fleet_gateway import AuditLog, Decision, Risk


def evt(log, trace="t1", ok=True, code="", before=100, after=40):
    return log.append(
        trace_id=trace,
        tenant="acme",
        server_id="db",
        tool="db__query",
        risk=Risk.READ,
        decision=Decision.ALLOW,
        ok=ok,
        error_code=code,
        bytes_before=before,
        bytes_after=after,
    )


class TestAppend:
    def test_returns_id(self):
        log = AuditLog()
        assert evt(log).startswith("au_")

    def test_len(self):
        log = AuditLog()
        evt(log)
        evt(log)
        assert len(log) == 2

    def test_ring_buffer_capacity(self):
        log = AuditLog(capacity=2)
        for _ in range(5):
            evt(log)
        assert len(log) == 2

    def test_recent(self):
        log = AuditLog()
        for i in range(3):
            evt(log, trace=f"t{i}")
        assert [e.trace_id for e in log.recent(2)] == ["t1", "t2"]


class TestQuery:
    def test_by_trace(self):
        log = AuditLog()
        evt(log, trace="a")
        evt(log, trace="b")
        evt(log, trace="a")
        assert len(log.by_trace("a")) == 2

    def test_replay_numbers_steps(self):
        log = AuditLog()
        evt(log, trace="x")
        evt(log, trace="x")
        rows = log.replay("x")
        assert [r["step"] for r in rows] == [1, 2]

    def test_replay_row_fields(self):
        log = AuditLog()
        evt(log)
        row = log.replay("t1")[0]
        assert row["tool"] == "db__query"
        assert row["risk"] == "read"
        assert row["decision"] == "allow"
        assert row["bytes"] == "100->40"

    def test_short_args_digest_in_row(self):
        log = AuditLog()
        log.append(
            trace_id="t", tenant="a", server_id="s", tool="s__t", risk=Risk.WRITE,
            decision=Decision.ALLOW, ok=True, args_digest="f" * 64,
        )
        assert len(log.replay("t")[0]["args"]) == 12


class TestStats:
    def test_empty(self):
        assert AuditLog().stats() == {"events": 0}

    def test_counts(self):
        log = AuditLog()
        evt(log, ok=True)
        evt(log, ok=False, code="policy_denied")
        st = log.stats()
        assert st["events"] == 2
        assert st["ok"] == 1
        assert st["failed"] == 1
        assert st["by_error"]["policy_denied"] == 1

    def test_bytes_saved_ratio(self):
        log = AuditLog()
        evt(log, before=1_000, after=250)
        assert log.stats()["bytes_saved_ratio"] == 0.75

    def test_per_server(self):
        log = AuditLog()
        evt(log)
        evt(log)
        assert log.stats()["per_server"]["db"] == 2


class TestSink:
    def test_writes_jsonl(self, tmp_path):
        sink = tmp_path / "audit" / "events.jsonl"
        log = AuditLog(sink=sink)
        evt(log)
        evt(log)
        lines = sink.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["tool"] == "db__query"

    def test_appends_across_instances(self, tmp_path):
        sink = tmp_path / "events.jsonl"
        evt(AuditLog(sink=sink))
        evt(AuditLog(sink=sink))
        assert len(sink.read_text(encoding="utf-8").strip().splitlines()) == 2
