import pytest

from helpers import FakeClock, make_gateway, make_server, make_tool

from mcp_fleet_gateway import (
    CallRequest,
    CircuitBreaker,
    Decision,
    PolicyDenied,
    Risk,
    ToolCatalog,
)


def simple_gateway(**kw):
    """一个只有 s1 的小网关，工具行为固定，便于精确断言。"""
    gw = make_gateway(**kw)
    calls = {"ping": 0, "del": 0}

    def ping():
        calls["ping"] += 1
        return {"pong": True, "n": calls["ping"]}

    def boom():
        raise RuntimeError("upstream exploded")

    def delete(path):
        calls["del"] += 1
        return {"deleted": path}

    def big(n=40):
        return {"items": [{"id": i, "title": f"t{i}", "blob": "b" * 200} for i in range(n)]}

    spec = make_server(
        "s1",
        (
            make_tool("ping", "s1"),
            make_tool("boom", "s1"),
            make_tool("del", "s1", props={"path": {"type": "string"}}, required=("path",),
                      risk=Risk.DESTRUCTIVE, idempotent=False),
            make_tool("write", "s1", props={"v": {"type": "string"}}, required=("v",),
                      risk=Risk.WRITE, idempotent=False),
            make_tool("big", "s1", max_result_bytes=400),
        ),
    )
    gw.add_server(spec, handlers={"ping": ping, "boom": boom, "del": delete, "big": big,
                                  "write": lambda v: {"v": v}})
    gw.calls = calls  # 便于断言"到底调了几次"
    return gw


def call(gw, tool, **args):
    return gw.invoke(CallRequest(tool=tool, arguments=args))


class TestHappyPath:
    def test_call_ok(self):
        gw = simple_gateway()
        r = call(gw, "s1__ping")
        assert r.ok and r.content["pong"] is True

    def test_result_metadata(self):
        gw = simple_gateway()
        r = call(gw, "s1__ping")
        assert r.server_id == "s1"
        assert r.trace_id.startswith("tr_")
        assert r.audit_id.startswith("au_")
        assert "call" in r.stages
        assert r.decision is Decision.ALLOW

    def test_bare_name_resolves(self):
        gw = simple_gateway()
        assert call(gw, "ping").ok

    def test_counters(self):
        gw = simple_gateway()
        call(gw, "s1__ping")
        assert gw.stats()["counters"]["ok"] == 1
        assert gw.stats()["counters"]["calls"] == 1

    def test_audit_written(self):
        gw = simple_gateway()
        r = call(gw, "s1__ping")
        rows = gw.audit.replay(r.trace_id)
        assert len(rows) == 1
        assert rows[0]["ok"] is True

    def test_trace_id_passthrough(self):
        gw = simple_gateway()
        r = gw.invoke(CallRequest(tool="s1__ping", trace_id="trace-abc"))
        assert r.trace_id == "trace-abc"
        assert len(gw.audit.by_trace("trace-abc")) == 1


class TestResolution:
    def test_unknown_tool(self):
        r = call(simple_gateway(), "s1__nope")
        assert r.ok is False
        assert r.error_code == "tool_not_found"

    def test_ambiguous_tool(self):
        gw = make_gateway()
        gw.add_server(make_server("a", (make_tool("search", "a"),)), handlers={"search": lambda: 1})
        gw.add_server(make_server("b", (make_tool("search", "b"),)), handlers={"search": lambda: 2})
        r = call(gw, "search")
        assert r.error_code == "ambiguous_tool"
        assert len(r.error_detail["candidates"]) == 2

    def test_alias_resolution(self):
        from mcp_fleet_gateway import ConflictStrategy

        gw = make_gateway(conflict_strategy=ConflictStrategy.ALIAS)
        gw.add_server(make_server("a", (make_tool("search", "a"),)), handlers={"search": lambda: "a"})
        gw.add_server(make_server("b", (make_tool("search", "b"),)), handlers={"search": lambda: "b"})
        gw.alias("search", "b__search")
        assert call(gw, "search").content == "b"


class TestValidation:
    def test_missing_required(self):
        r = call(simple_gateway(), "s1__del")
        assert r.error_code == "invalid_arguments"
        assert any("path" in e for e in r.error_detail["errors"])

    def test_wrong_type(self):
        r = call(simple_gateway(), "s1__del", path=123)
        assert r.error_code == "invalid_arguments"

    def test_validation_happens_before_policy(self):
        """参数都不合法时不该去打扰人审批：错误码必须是 invalid_arguments。"""
        r = call(simple_gateway(), "s1__del")
        assert r.error_code == "invalid_arguments"
        assert simple_gateway().approvals.pending() == []


class TestPolicy:
    def test_deny_blocks(self):
        gw = simple_gateway()
        gw.policy.add(__import__("mcp_fleet_gateway").Rule("nope", Decision.DENY, server="s1"))
        r = call(gw, "s1__ping")
        assert r.error_code == "policy_denied"
        assert gw.stats()["counters"]["denied"] == 1

    def test_deny_is_audited(self):
        gw = simple_gateway()
        gw.policy.add(__import__("mcp_fleet_gateway").Rule("nope", Decision.DENY, server="s1"))
        r = call(gw, "s1__ping")
        row = gw.audit.by_trace(r.trace_id)[0]
        assert row.decision is Decision.DENY
        assert row.ok is False

    def test_raise_on_error(self):
        gw = simple_gateway()
        gw.policy.add(__import__("mcp_fleet_gateway").Rule("nope", Decision.DENY, server="s1"))
        with pytest.raises(PolicyDenied):
            gw.invoke(CallRequest(tool="s1__ping"), raise_on_error=True)

    def test_arg_guard_blocks_destructive_sql(self):
        gw = make_gateway()
        spec = make_server("db", (
            make_tool("exec", "db", props={"sql": {"type": "string"}}, required=("sql",),
                      risk=Risk.DESTRUCTIVE),
        ))
        gw.add_server(spec, handlers={"exec": lambda sql: {"ok": True}})
        r = call(gw, "db__exec", sql="DROP TABLE users")
        assert r.error_code == "policy_denied"
        assert "sql_drop" in r.error


class TestApproval:
    def test_destructive_requires_ticket(self):
        gw = simple_gateway()
        r = call(gw, "s1__del", path="/tmp/a")
        assert r.error_code == "approval_required"
        assert r.approval_ticket.startswith("tk_")
        assert gw.stats()["counters"]["approval_required"] == 1

    def test_approval_then_invoke(self):
        gw = simple_gateway()
        r = call(gw, "s1__del", path="/tmp/a")
        gw.approvals.approve(r.approval_ticket, "alice")
        ok = gw.invoke(CallRequest(tool="s1__del", arguments={"path": "/tmp/a"},
                                   approval_ticket=r.approval_ticket))
        assert ok.ok
        assert gw.stats()["counters"]["approval_used"] == 1
        assert gw.calls["del"] == 1

    def test_ticket_cannot_be_reused(self):
        gw = simple_gateway()
        r = call(gw, "s1__del", path="/tmp/a")
        gw.approvals.approve(r.approval_ticket)
        req = CallRequest(tool="s1__del", arguments={"path": "/tmp/a"},
                          approval_ticket=r.approval_ticket)
        assert gw.invoke(req).ok
        second = gw.invoke(req)
        assert second.error_code == "approval_invalid"

    def test_ticket_bound_to_arguments(self):
        """批的是 A，执行 B —— 必须拦下。"""
        gw = simple_gateway()
        r = call(gw, "s1__del", path="/tmp/a")
        gw.approvals.approve(r.approval_ticket)
        sneaky = gw.invoke(CallRequest(tool="s1__del", arguments={"path": "/tmp/other"},
                                       approval_ticket=r.approval_ticket))
        assert sneaky.error_code == "approval_invalid"
        assert gw.calls["del"] == 0

    def test_rejected_ticket(self):
        gw = simple_gateway()
        r = call(gw, "s1__del", path="/tmp/a")
        gw.approvals.reject(r.approval_ticket, reason="不允许")
        out = gw.invoke(CallRequest(tool="s1__del", arguments={"path": "/tmp/a"},
                                    approval_ticket=r.approval_ticket))
        assert out.error_code == "approval_invalid"
        assert "不允许" in out.error

    def test_pending_list(self):
        gw = simple_gateway()
        call(gw, "s1__del", path="/tmp/a")
        call(gw, "s1__del", path="/tmp/b")
        assert len(gw.approvals.pending()) == 2


class TestIdempotency:
    def test_non_idempotent_tool_replayed(self):
        gw = simple_gateway()
        first = call(gw, "s1__write", v="x")
        second = call(gw, "s1__write", v="x")
        assert first.ok and second.ok
        assert second.content == first.content
        assert "idempotent_replay" in second.stages
        assert gw.stats()["counters"]["replays"] == 1

    def test_different_args_not_replayed(self):
        gw = simple_gateway()
        call(gw, "s1__write", v="x")
        call(gw, "s1__write", v="y")
        assert gw.stats()["counters"]["replays"] == 0

    def test_idempotent_tool_not_cached(self):
        gw = simple_gateway()
        call(gw, "s1__ping")
        call(gw, "s1__ping")
        assert gw.calls["ping"] == 2

    def test_replay_has_own_audit(self):
        gw = simple_gateway()
        call(gw, "s1__write", v="x")
        second = call(gw, "s1__write", v="x")
        row = gw.audit.by_trace(second.trace_id)[0]
        assert row.rule.startswith("idempotency:")


class TestRetry:
    def test_retries_transport_error(self):
        gw = simple_gateway()
        gw.transports["s1"].fail_next(1)
        r = call(gw, "s1__ping")
        assert r.ok
        assert gw.stats()["counters"]["retries"] == 1

    def test_no_retry_for_non_idempotent(self):
        gw = simple_gateway()
        gw.transports["s1"].fail_next(1)
        r = call(gw, "s1__write", v="x")
        assert r.ok is False
        assert r.error_code == "transport_error"
        assert gw.stats()["counters"]["retries"] == 0

    def test_gives_up_after_max_attempts(self):
        gw = simple_gateway()
        gw.transports["s1"].fail_next(5)
        r = call(gw, "s1__ping")
        assert r.error_code == "transport_error"


class TestCircuit:
    def _gw(self, clock, threshold=2):
        return simple_gateway(
            max_attempts=1,
            breaker_factory=lambda sid: CircuitBreaker(
                sid, failure_threshold=threshold, min_samples=threshold,
                open_s=30, now_fn=clock,
            ),
        )

    def test_opens_after_failures(self, clock):
        gw = self._gw(clock)
        call(gw, "s1__ping")
        gw.transports["s1"].heal()
        for _ in range(2):
            call(gw, "s1__boom")
        assert gw.health()["s1"]["state"] == "open"
        r = call(gw, "s1__ping")
        assert r.error_code == "circuit_open"

    def test_healthy_server_unaffected(self, clock):
        gw = self._gw(clock)
        gw.add_server(make_server("s2", (make_tool("ping", "s2"),)),
                      handlers={"ping": lambda: "ok"})
        for _ in range(2):
            call(gw, "s1__boom")
        assert call(gw, "s2__ping").ok is True
        assert gw.health()["s1"]["state"] == "open"
        assert gw.health()["s2"]["state"] == "closed"

    def test_recovers_after_cooldown(self, clock):
        gw = self._gw(clock)
        for _ in range(2):
            call(gw, "s1__boom")
        gw.transports["s1"].heal()
        clock.advance(31)
        assert call(gw, "s1__ping").ok is True
        assert gw.health()["s1"]["state"] == "closed"


class TestDeadline:
    def test_rejects_when_eta_exceeds_budget(self):
        gw = make_gateway()
        gw.add_server(make_server("slow", (make_tool("ping", "slow"),)),
                      handlers={"ping": lambda: 1}, latency_ms=500)
        r = gw.invoke(CallRequest(tool="slow__ping", deadline_ms=100))
        assert r.error_code == "deadline_exceeded"
        assert "预计耗时" in r.error

    def test_uses_spec_timeout(self):
        gw = make_gateway()
        gw.add_server(
            make_server("slow", (make_tool("ping", "slow", timeout_ms=50),)),
            handlers={"ping": lambda: 1},
            latency_ms=300,
        )
        assert gw.invoke(CallRequest(tool="slow__ping", deadline_ms=10_000)).error_code == (
            "deadline_exceeded"
        )


class TestShaping:
    def test_big_result_shaped(self):
        gw = simple_gateway()
        r = call(gw, "s1__big")
        assert r.ok
        assert r.bytes_after < r.bytes_before
        assert r.truncated is True
        assert r.cursor

    def test_page_fetch(self):
        gw = simple_gateway()
        r = call(gw, "s1__big")
        page = gw.fetch_page(r.cursor)
        assert page.ok
        assert page.content

    def test_invalid_cursor(self):
        gw = simple_gateway()
        page = gw.fetch_page("cur_nope")
        assert page.ok is False
        assert page.error_code == "cursor_invalid"

    def test_stats_track_shaped(self):
        gw = simple_gateway()
        call(gw, "s1__big")
        assert gw.stats()["counters"]["shaped"] == 1


class TestQuarantine:
    def test_quarantined_tool_rejected(self):
        cat = ToolCatalog()
        old = make_server("db", (make_tool("q", "db", props={"a": {"type": "string"}}),))
        new = make_server("db", (make_tool("q", "db", props={"a": {"type": "integer"}}),))
        gw = make_gateway(catalog=cat)
        gw.add_server(old, handlers={"q": lambda a: a})
        gw.add_server(new, handlers={"q": lambda a: a}, replace_existing=True)
        r = call(gw, "db__q", a=1)
        assert r.error_code == "tool_quarantined"

    def test_acknowledge_then_callable(self):
        cat = ToolCatalog()
        old = make_server("db", (make_tool("q", "db", props={"a": {"type": "string"}}),))
        new = make_server("db", (make_tool("q", "db", props={"a": {"type": "integer"}}),))
        gw = make_gateway(catalog=cat)
        gw.add_server(old, handlers={"q": lambda a: a})
        gw.add_server(new, handlers={"q": lambda a: a}, replace_existing=True)
        gw.acknowledge("db__q")
        assert call(gw, "db__q", a=1).ok


class TestObservability:
    def test_health_after_calls(self):
        gw = simple_gateway()
        call(gw, "s1__ping")
        h = gw.health()["s1"]
        assert h["calls"] == 1
        assert h["failures"] == 0

    def test_stats_shape(self):
        gw = simple_gateway()
        call(gw, "s1__ping")
        st = gw.stats()
        assert set(st) == {"counters", "catalog", "audit", "pages", "health"}
        assert st["catalog"]["servers"] == 1

    def test_audit_bytes_recorded(self):
        gw = simple_gateway()
        r = call(gw, "s1__big")
        row = gw.audit.replay(r.trace_id)[0]
        assert "->" in row["bytes"]
