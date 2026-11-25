import pytest

from helpers import make_tool

from mcp_fleet_gateway import (
    CallRequest,
    Decision,
    PolicyEngine,
    Risk,
    Rule,
    matched_guards,
)
from mcp_fleet_gateway.policy import args_text


def req(tool="s1__t", tenant="default", **args):
    return CallRequest(tool=tool, arguments=args, tenant=tenant)


class TestDefaults:
    def test_read_allowed(self):
        d = PolicyEngine().evaluate(req(), make_tool("t", risk=Risk.READ))
        assert d.decision is Decision.ALLOW
        assert d.rule == "default:read"

    def test_write_allowed_by_default(self):
        d = PolicyEngine().evaluate(req(), make_tool("t", risk=Risk.WRITE))
        assert d.decision is Decision.ALLOW

    def test_write_denied_when_default_flipped(self):
        d = PolicyEngine(allow_write_default=False).evaluate(req(), make_tool("t", risk=Risk.WRITE))
        assert d.decision is Decision.DENY

    def test_destructive_needs_approval(self):
        d = PolicyEngine().evaluate(req(), make_tool("t", risk=Risk.DESTRUCTIVE))
        assert d.decision is Decision.APPROVAL
        assert d.rule == "default:destructive"

    def test_destructive_can_be_relaxed(self):
        d = PolicyEngine(destructive_requires_approval=False).evaluate(
            req(), make_tool("t", risk=Risk.DESTRUCTIVE)
        )
        assert d.decision is Decision.ALLOW

    def test_risk_attached_to_decision(self):
        d = PolicyEngine().evaluate(req(), make_tool("t", risk=Risk.WRITE))
        assert d.risk is Risk.WRITE


class TestRules:
    def test_explicit_deny(self):
        p = PolicyEngine(rules=[Rule("no-merge", Decision.DENY, tool="*merge*")])
        d = p.evaluate(req(), make_tool("merge_pr", "gh", risk=Risk.DESTRUCTIVE))
        assert d.decision is Decision.DENY
        assert d.rule == "no-merge"

    def test_deny_beats_allow(self):
        p = PolicyEngine(
            rules=[
                Rule("allow-all", Decision.ALLOW),
                Rule("but-not-this", Decision.DENY, server="db"),
            ]
        )
        d = p.evaluate(req(), make_tool("q", "db", risk=Risk.READ))
        assert d.decision is Decision.DENY

    def test_tenant_scoping(self):
        p = PolicyEngine(rules=[Rule("block-bot", Decision.DENY, tenants=("bot",))])
        assert p.evaluate(req(tenant="bot"), make_tool("t")).decision is Decision.DENY
        assert p.evaluate(req(tenant="human"), make_tool("t")).decision is Decision.ALLOW

    def test_tenant_wildcard(self):
        p = PolicyEngine(rules=[Rule("block-all", Decision.DENY, tenants=("*",))])
        assert p.evaluate(req(tenant="anyone"), make_tool("t")).decision is Decision.DENY

    def test_server_glob(self):
        p = PolicyEngine(rules=[Rule("no-k8s", Decision.DENY, server="k8s*")])
        assert p.evaluate(req(), make_tool("scale", "k8s-prod")).decision is Decision.DENY
        assert p.evaluate(req(), make_tool("q", "db")).decision is Decision.ALLOW

    def test_risk_range(self):
        p = PolicyEngine(rules=[Rule("approve-writes", Decision.APPROVAL, min_risk=Risk.WRITE)])
        assert p.evaluate(req(), make_tool("t", risk=Risk.WRITE)).decision is Decision.APPROVAL
        assert p.evaluate(req(), make_tool("t", risk=Risk.READ)).decision is Decision.ALLOW

    def test_max_risk(self):
        p = PolicyEngine(rules=[Rule("read-only", Decision.ALLOW, max_risk=Risk.READ)])
        d = p.evaluate(req(), make_tool("t", risk=Risk.DESTRUCTIVE))
        assert d.decision is Decision.APPROVAL  # 没有允许 -> 回落到默认破坏性审批

    def test_rule_with_arg_guard_selector(self):
        p = PolicyEngine(
            rules=[
                Rule("sql-guard", Decision.DENY, server="db", arg_guards=("sql_drop",)),
            ]
        )
        spec = make_tool("exec", "db", props={"sql": {"type": "string"}}, required=("sql",))
        assert p.evaluate(req(sql="DROP TABLE t"), spec).decision is Decision.DENY
        assert p.evaluate(req(sql="SELECT 1"), spec).decision is Decision.ALLOW

    def test_add_returns_self(self):
        p = PolicyEngine()
        assert p.add(Rule("r", Decision.ALLOW)) is p


class TestArgGuards:
    def test_sql_drop(self):
        spec = make_tool("exec", "db", props={"sql": {"type": "string"}})
        d = PolicyEngine().evaluate(req(sql="DROP TABLE users"), spec)
        assert d.decision is Decision.DENY
        assert d.rule == "arg_guard"
        assert "sql_drop" in d.reason

    def test_sql_delete_without_where(self):
        spec = make_tool("exec", "db", props={"sql": {"type": "string"}})
        d = PolicyEngine().evaluate(req(sql="DELETE FROM orders;"), spec)
        assert d.decision is Decision.DENY

    def test_path_traversal(self):
        spec = make_tool("read", "fs", props={"path": {"type": "string"}})
        assert PolicyEngine().evaluate(req(path="../../etc/passwd"), spec).decision is Decision.DENY

    def test_rm_rf(self):
        spec = make_tool("sh", "ops", props={"cmd": {"type": "string"}})
        assert PolicyEngine().evaluate(req(cmd="rm -rf /"), spec).decision is Decision.DENY

    def test_force_push(self):
        spec = make_tool("sh", "ops", props={"cmd": {"type": "string"}})
        assert PolicyEngine().evaluate(
            req(cmd="git push -f origin main"), spec
        ).decision is Decision.DENY

    def test_guard_can_downgrade_to_approval(self):
        spec = make_tool("exec", "db", props={"sql": {"type": "string"}})
        p = PolicyEngine(hard_block_guards=False)
        d = p.evaluate(req(sql="DROP TABLE t"), spec)
        assert d.decision is Decision.APPROVAL

    def test_explicit_allow_does_not_override_guard_by_default(self):
        """一条通配的"放行"规则不应该顺手把 DROP TABLE 也放过去。"""
        spec = make_tool("exec", "db", props={"sql": {"type": "string"}}, risk=Risk.DESTRUCTIVE)
        p = PolicyEngine(rules=[Rule("dba-ok", Decision.ALLOW, tenants=("dba",))])
        d = p.evaluate(req(tenant="dba", sql="DROP TABLE t"), spec)
        assert d.decision is Decision.DENY

    def test_rule_can_explicitly_override_guard(self):
        spec = make_tool("exec", "db", props={"sql": {"type": "string"}}, risk=Risk.DESTRUCTIVE)
        p = PolicyEngine(rules=[
            Rule("dba-ok", Decision.ALLOW, tenants=("dba",), override_guards=True),
        ])
        d = p.evaluate(req(tenant="dba", sql="DROP TABLE t"), spec)
        assert d.decision is Decision.ALLOW

    def test_benign_args_not_flagged(self):
        spec = make_tool("exec", "db", props={"sql": {"type": "string"}})
        assert PolicyEngine().evaluate(
            req(sql="SELECT * FROM orders WHERE id = 1"), spec
        ).decision is Decision.ALLOW

    def test_matched_guards_multiple(self):
        hits = matched_guards({"path": "../../etc/shadow"})
        assert "path_traversal" in hits
        assert "sensitive_path" in hits

    def test_args_text_handles_non_json(self):
        class Weird:
            def __str__(self):
                return "weird"

        assert "weird" in args_text({"x": Weird()})


class TestPresets:
    def test_readonly_tenant(self):
        p = PolicyEngine.readonly_tenant("viewer")
        assert p.evaluate(req(tenant="viewer"), make_tool("t", risk=Risk.WRITE)).decision is Decision.DENY
        assert p.evaluate(req(tenant="viewer"), make_tool("t", risk=Risk.READ)).decision is Decision.ALLOW

    def test_production_read_allowed(self):
        p = PolicyEngine.for_production()
        assert p.evaluate(req(tenant="prod"), make_tool("t", risk=Risk.READ)).decision is Decision.ALLOW

    def test_production_write_denied(self):
        p = PolicyEngine.for_production()
        assert p.evaluate(req(tenant="prod"), make_tool("t", risk=Risk.WRITE)).decision is Decision.DENY

    def test_production_write_allowed_on_staging(self):
        p = PolicyEngine.for_production()
        assert p.evaluate(req(tenant="staging"), make_tool("t", risk=Risk.WRITE)).decision is Decision.ALLOW

    def test_production_destructive_needs_approval(self):
        p = PolicyEngine.for_production()
        assert p.evaluate(
            req(tenant="prod"), make_tool("t", risk=Risk.DESTRUCTIVE)
        ).decision is Decision.APPROVAL
