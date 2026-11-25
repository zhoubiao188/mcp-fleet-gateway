import pytest

from helpers import make_tool

from mcp_fleet_gateway import (
    ApprovalInvalid,
    ApprovalQueue,
    ApprovalRequired,
    CallRequest,
    Risk,
    TicketStatus,
)


def make_req(**args):
    return CallRequest(tool="db__merge", arguments=args)


SPEC = make_tool("merge", "db", risk=Risk.DESTRUCTIVE)


class TestRequest:
    def test_request_is_pending(self, clock):
        q = ApprovalQueue(now_fn=clock)
        t = q.request(make_req(id=1), SPEC)
        assert t.status is TicketStatus.PENDING
        assert t.tool == "db__merge"
        assert t.risk is Risk.DESTRUCTIVE

    def test_summary_is_human_readable(self, clock):
        q = ApprovalQueue(now_fn=clock)
        t = q.request(make_req(order_id="o_1", amount=100), SPEC)
        assert "order_id=o_1" in t.summary

    def test_raise_for_generates_ticket(self, clock):
        q = ApprovalQueue(now_fn=clock)
        with pytest.raises(ApprovalRequired) as ei:
            q.raise_for(make_req(id=1), SPEC, "destructive")
        tid = ei.value.ticket_id
        assert tid
        assert q.get(tid).status is TicketStatus.PENDING
        assert len(q.pending()) == 1

    def test_args_digest_independent_of_tool_spelling(self, clock):
        q = ApprovalQueue(now_fn=clock)
        a = CallRequest(tool="db__merge", arguments={"id": 1})
        b = CallRequest(tool="merge", arguments={"id": 1})
        assert a.args_digest == b.args_digest


class TestConsume:
    def test_happy_path(self, clock):
        q = ApprovalQueue(now_fn=clock)
        r = make_req(id=1)
        t = q.request(r, SPEC)
        q.approve(t.id, "alice")
        used = q.consume(t.id, "db__merge", r.args_digest)
        assert used.status is TicketStatus.CONSUMED
        assert used.approver == "alice"

    def test_single_use(self, clock):
        q = ApprovalQueue(now_fn=clock)
        r = make_req(id=1)
        t = q.request(r, SPEC)
        q.approve(t.id)
        q.consume(t.id, "db__merge", r.args_digest)
        with pytest.raises(ApprovalInvalid) as ei:
            q.consume(t.id, "db__merge", r.args_digest)
        assert "一次性" in str(ei.value)

    def test_tool_must_match(self, clock):
        q = ApprovalQueue(now_fn=clock)
        r = make_req(id=1)
        t = q.request(r, SPEC)
        q.approve(t.id)
        with pytest.raises(ApprovalInvalid) as ei:
            q.consume(t.id, "db__drop", r.args_digest)
        assert "绑定的工具" in str(ei.value)

    def test_args_must_match(self, clock):
        """审批通过的是"参数组合"，换一组参数必须重新审批。"""
        q = ApprovalQueue(now_fn=clock)
        r = make_req(id=1)
        t = q.request(r, SPEC)
        q.approve(t.id)
        other = make_req(id=999)
        with pytest.raises(ApprovalInvalid) as ei:
            q.consume(t.id, "db__merge", other.args_digest)
        assert "实参" in str(ei.value)

    def test_must_be_approved_first(self, clock):
        q = ApprovalQueue(now_fn=clock)
        r = make_req(id=1)
        t = q.request(r, SPEC)
        with pytest.raises(ApprovalInvalid) as ei:
            q.consume(t.id, "db__merge", r.args_digest)
        assert "尚未批准" in str(ei.value)

    def test_rejected_cannot_be_consumed(self, clock):
        q = ApprovalQueue(now_fn=clock)
        r = make_req(id=1)
        t = q.request(r, SPEC)
        q.reject(t.id, "bob", "太危险")
        with pytest.raises(ApprovalInvalid) as ei:
            q.consume(t.id, "db__merge", r.args_digest)
        assert "拒绝" in str(ei.value)

    def test_expired_ticket(self, clock):
        q = ApprovalQueue(ttl_ms=1_000, now_fn=clock)
        r = make_req(id=1)
        t = q.request(r, SPEC)
        q.approve(t.id)
        clock.advance(2)
        with pytest.raises(ApprovalInvalid) as ei:
            q.consume(t.id, "db__merge", r.args_digest)
        assert "过期" in str(ei.value)
        assert q.get(t.id).status is TicketStatus.EXPIRED

    def test_unknown_ticket(self, clock):
        with pytest.raises(ApprovalInvalid):
            ApprovalQueue(now_fn=clock).consume("tk_nope", "db__merge", "x")

    def test_cannot_approve_consumed(self, clock):
        q = ApprovalQueue(now_fn=clock)
        r = make_req(id=1)
        t = q.request(r, SPEC)
        q.approve(t.id)
        q.consume(t.id, "db__merge", r.args_digest)
        with pytest.raises(ApprovalInvalid):
            q.approve(t.id)

    def test_cannot_approve_expired(self, clock):
        q = ApprovalQueue(ttl_ms=100, now_fn=clock)
        t = q.request(make_req(id=1), SPEC)
        clock.advance(1)
        with pytest.raises(ApprovalInvalid) as ei:
            q.approve(t.id)
        assert "过期" in str(ei.value)


class TestQueue:
    def test_pending_excludes_decided(self, clock):
        q = ApprovalQueue(now_fn=clock)
        t1 = q.request(make_req(a=1), SPEC)
        q.request(make_req(a=2), SPEC)
        q.approve(t1.id)
        assert len(q.pending()) == 1

    def test_expire_due(self, clock):
        q = ApprovalQueue(ttl_ms=100, now_fn=clock)
        q.request(make_req(a=1), SPEC)
        q.request(make_req(a=2), SPEC)
        clock.advance(1)
        assert q.expire_due() == 2
        assert q.pending() == []

    def test_all_tickets(self, clock):
        q = ApprovalQueue(now_fn=clock)
        q.request(make_req(a=1), SPEC)
        assert len(q.all_tickets()) == 1

    def test_expires_at(self, clock):
        q = ApprovalQueue(ttl_ms=60_000, now_fn=clock)
        t = q.request(make_req(a=1), SPEC)
        assert t.expires_at == t.requested_at + 60
