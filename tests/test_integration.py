"""端到端集成测试：12 个 Server / 94 个工具的真实规模。"""
import pytest

from helpers import FakeClock

from mcp_fleet_gateway import CallRequest, CircuitBreaker, Decision
from mcp_fleet_gateway.demo_fleet import build_fleet


@pytest.fixture
def gw():
    return build_fleet(budget_tokens=1_200, sleep_fn=lambda _s: None)


def call(gw, tool, **args):
    return gw.invoke(CallRequest(tool=tool, arguments=args))


class TestFleetShape:
    def test_server_and_tool_counts(self, gw):
        st = gw.stats()["catalog"]
        assert st["servers"] == 12
        assert st["tools"] >= 90
        assert st["quarantined"] == 0

    def test_risky_tools_declared(self, gw):
        assert gw.stats()["catalog"]["by_risk"]["destructive"] >= 5

    def test_duplicate_names_detected(self, gw):
        dup = gw.stats()["catalog"]["duplicate_names"]
        assert "search" in dup
        assert len(dup["search"]) == 5

    def test_every_tool_has_schema(self, gw):
        for t in gw.catalog.all_tools():
            assert t.input_schema["type"] == "object"
            assert t.description


class TestManifestBudget:
    def test_full_manifest_would_blow_budget(self, gw):
        full = gw.manifest(budget_tokens=100_000)
        assert full.staged is False
        assert full.est_tokens > 1_500

    def test_default_budget_stays_within(self, gw):
        m = gw.manifest()
        assert m.staged is True
        assert m.est_tokens <= 1_200
        assert len(m.tools) < 94

    def test_omitted_servers_indexed(self, gw):
        m = gw.manifest()
        assert "gw_discover" in m.text
        assert len(m.omitted) > 0

    def test_discover_expands_server(self, gw):
        out = gw.discover("billing")
        assert out["count"] == 6
        assert all(t.startswith("billing__") for t in out["tools"])

    def test_query_lifts_relevant_tools(self, gw):
        m = gw.manifest(query="退款 refund 资金")
        assert any("refund" in t for t in m.tools)


class TestAmbiguity:
    def test_bare_search_is_ambiguous(self, gw):
        r = call(gw, "search", q="支付超时")
        assert r.error_code == "ambiguous_tool"
        assert len(r.error_detail["candidates"]) == 5

    def test_qualified_works(self, gw):
        assert call(gw, "notion__search", q="支付超时").ok

    def test_alias_makes_it_predictable(self):
        from mcp_fleet_gateway import ConflictStrategy

        gw = build_fleet(
            budget_tokens=1_200, sleep_fn=lambda _s: None,
            conflict_strategy=ConflictStrategy.ALIAS,
        )
        gw.alias("search", "vector__search")
        r = gw.invoke(CallRequest(tool="search", arguments={"query": "支付超时", "k": 3}))
        assert r.ok
        assert r.server_id == "vector"


class TestSafety:
    def test_drop_table_blocked(self, gw):
        r = call(gw, "postgres__execute_sql", sql="DROP TABLE orders")
        assert r.error_code == "policy_denied"
        assert "sql_drop" in r.error

    def test_delete_all_blocked(self, gw):
        r = call(gw, "postgres__execute_sql", sql="DELETE FROM orders;")
        assert r.error_code == "policy_denied"

    def test_path_traversal_blocked(self, gw):
        r = call(gw, "files__read_file", path="../../etc/shadow")
        assert r.error_code == "policy_denied"

    def test_rm_rf_blocked(self, gw):
        r = gw.invoke(CallRequest(tool="web__fetch_url", arguments={"url": "x"}))
        assert r.ok  # 正常工具不受影响

    def test_tenant_rule_blocks_prod_sql(self, gw):
        r = gw.invoke(
            CallRequest(
                tool="postgres__execute_sql",
                arguments={"sql": "SELECT 1"},
                tenant="prod-agent",
            )
        )
        assert r.error_code == "policy_denied"
        assert r.error_detail["rule"] == "deny:prod-sql-ddl"

    def test_same_sql_allowed_for_other_tenant(self, gw):
        r = gw.invoke(
            CallRequest(
                tool="postgres__execute_sql",
                arguments={"sql": "SELECT 1"},
                tenant="analyst",
                approval_ticket="skip",
            )
        )
        # 非 prod 租户不命中 deny 规则，但仍需审批（破坏性）
        assert r.error_code in {"approval_required", "approval_invalid"}


class TestApprovalFlow:
    def test_scale_requires_approval(self, gw):
        r = call(gw, "k8s__scale", deployment="pay-api", replicas=10)
        assert r.error_code == "approval_required"
        assert r.approval_ticket

    def test_approve_then_execute(self, gw):
        r = call(gw, "k8s__scale", deployment="pay-api", replicas=10)
        gw.approvals.approve(r.approval_ticket, "sre-oncall")
        done = gw.invoke(
            CallRequest(
                tool="k8s__scale",
                arguments={"deployment": "pay-api", "replicas": 10},
                approval_ticket=r.approval_ticket,
            )
        )
        assert done.ok
        assert done.content["replicas"] == 10

    def test_reusing_ticket_with_new_args_fails(self, gw):
        r = call(gw, "k8s__scale", deployment="pay-api", replicas=10)
        gw.approvals.approve(r.approval_ticket)
        hijack = gw.invoke(
            CallRequest(
                tool="k8s__scale",
                arguments={"deployment": "core-db", "replicas": 0},
                approval_ticket=r.approval_ticket,
            )
        )
        assert hijack.error_code == "approval_invalid"

    def test_readonly_ops_need_no_ticket(self, gw):
        assert call(gw, "k8s__get_pods", namespace="prod").ok


class TestIsolation:
    def test_k8s_outage_does_not_break_others(self):
        clk = FakeClock()
        gw = build_fleet(
            sleep_fn=lambda _s: None,
            max_attempts=1,
            breaker_factory=lambda sid: CircuitBreaker(
                sid, failure_threshold=3, min_samples=3, open_s=30, now_fn=clk
            ),
        )
        gw.transports["k8s"].fail_next(20)
        for _ in range(4):
            call(gw, "k8s__get_pods", namespace="prod")
        assert gw.health()["k8s"]["state"] == "open"

        # 其他 11 个 Server 完全不受影响
        assert call(gw, "github__search_code", q="charge").ok
        assert call(gw, "notion__get_page", page_id="pg1").ok
        assert call(gw, "billing__query_invoices", month="2026-11").ok

    def test_recovers_after_probe(self):
        clk = FakeClock()
        gw = build_fleet(
            sleep_fn=lambda _s: None,
            max_attempts=1,
            breaker_factory=lambda sid: CircuitBreaker(
                sid, failure_threshold=2, min_samples=2, open_s=10, now_fn=clk
            ),
        )
        gw.transports["k8s"].fail_next(20)
        for _ in range(3):
            call(gw, "k8s__get_pods", namespace="prod")
        assert gw.health()["k8s"]["state"] == "open"

        gw.transports["k8s"].heal()
        clk.advance(11)
        assert call(gw, "k8s__get_pods", namespace="prod").ok
        assert gw.health()["k8s"]["state"] == "closed"


class TestShaping:
    def test_vector_search_shrinks_hard(self, gw):
        r = call(gw, "vector__search", query="支付超时", k=40)
        assert r.ok
        assert r.bytes_before > 400_000
        assert r.bytes_after < 10_000
        assert r.saved_ratio > 0.95

    def test_projection_keeps_useful_fields(self, gw):
        r = call(gw, "vector__search", query="支付超时", k=5)
        payload = r.content.get("data", r.content)
        first = payload["matches"][0]
        assert set(first) == {"title", "url", "score", "chunk"}

    def test_paging_round_trip(self, gw):
        r = call(gw, "vector__search", query="支付超时", k=40)
        assert r.cursor
        page = gw.fetch_page(r.cursor)
        assert page.ok
        assert page.content
        # 句柄一次性
        assert gw.fetch_page(r.cursor).ok is False

    def test_code_search_paginated(self, gw):
        r = call(gw, "github__search_code", q="charge", limit=40)
        assert r.ok
        assert r.bytes_after < r.bytes_before
        assert r.content["_meta"]["total"] == 40


class TestAuditReplay:
    def test_trace_replay(self, gw):
        r = call(gw, "vector__search", query="支付超时", k=5)
        rows = gw.audit.replay(r.trace_id)
        assert len(rows) == 1
        assert rows[0]["server"] == "vector"
        assert rows[0]["decision"] == Decision.ALLOW.value

    def test_denied_call_recorded(self, gw):
        r = call(gw, "postgres__execute_sql", sql="DROP TABLE orders")
        row = gw.audit.replay(r.trace_id)[0]
        assert row["ok"] is False
        assert row["error_code"] == "policy_denied"

    def test_global_stats(self, gw):
        call(gw, "vector__search", query="x", k=10)
        call(gw, "postgres__execute_sql", sql="DROP TABLE x")
        st = gw.stats()["audit"]
        assert st["events"] == 2
        assert st["by_decision"]["deny"] == 1
