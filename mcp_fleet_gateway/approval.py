"""人工审批闸门。

这里有一个很容易被忽略的漏洞：**审批通过的是"某个工具"，但执行的可能不是"那件事"**。

真实事故：运营在审批面板上看到 "db__execute_sql 更新订单状态"，批了；
模型拿着这张工单，重新拼了一条 ``DELETE FROM orders WHERE 1=1`` 执行。
因为早期实现只校验 ticket 有效，不校验参数。

所以本模块的审批单必须绑定三样东西：工具全名 + **实参指纹** + 有效期，且一次性消费。
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Sequence

from .errors import ApprovalInvalid, ApprovalRequired
from .models import CallRequest, Risk, ToolSpec


class TicketStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Ticket:
    id: str
    tenant: str
    tool: str
    args_digest: str
    risk: Risk
    requested_at: float
    ttl_ms: int
    status: TicketStatus = TicketStatus.PENDING
    approver: str = ""
    reason: str = ""
    summary: str = ""
    decided_at: float = 0.0

    @property
    def expires_at(self) -> float:
        return self.requested_at + self.ttl_ms / 1000

    def expired(self, now: float) -> bool:
        return now >= self.expires_at


class ApprovalQueue:
    def __init__(
        self,
        *,
        ttl_ms: int = 10 * 60 * 1000,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.ttl_ms = ttl_ms
        self._now = now_fn
        self._tickets: dict[str, Ticket] = {}

    # ------------------------------------------------------------ 申请

    def request(self, req: CallRequest, spec: ToolSpec, reason: str = "") -> Ticket:
        tid = "tk_" + secrets.token_hex(8)
        ticket = Ticket(
            id=tid,
            tenant=req.tenant,
            tool=spec.qualified,
            args_digest=req.args_digest,
            risk=spec.risk,
            requested_at=self._now(),
            ttl_ms=self.ttl_ms,
            reason=reason or f"{spec.risk.label} 风险操作",
            summary=_summarize(req.arguments),
        )
        self._tickets[tid] = ticket
        return ticket

    def raise_for(self, req: CallRequest, spec: ToolSpec, reason: str = "") -> None:
        ticket = self.request(req, spec, reason)
        raise ApprovalRequired(
            f"{spec.qualified} 需要人工审批，已生成工单 {ticket.id}；"
            f"审批后请携带该 ticket 重新调用",
            ticket_id=ticket.id,
            summary=ticket.summary,
        )

    # ------------------------------------------------------------ 决策

    def get(self, ticket_id: str) -> Ticket:
        t = self._tickets.get(ticket_id)
        if t is None:
            raise ApprovalInvalid(f"审批单不存在：{ticket_id}")
        return t

    def approve(self, ticket_id: str, approver: str = "human") -> Ticket:
        t = self.get(ticket_id)
        if t.status is TicketStatus.CONSUMED:
            raise ApprovalInvalid("该审批单已被消费，不能重复使用")
        if t.status is TicketStatus.REJECTED:
            raise ApprovalInvalid("该审批单已被拒绝")
        if t.expired(self._now()):
            raise ApprovalInvalid("审批单已过期")
        updated = replace(t, status=TicketStatus.APPROVED, approver=approver, decided_at=self._now())
        self._tickets[ticket_id] = updated
        return updated

    def reject(self, ticket_id: str, approver: str = "human", reason: str = "") -> Ticket:
        t = self.get(ticket_id)
        updated = replace(
            t, status=TicketStatus.REJECTED, approver=approver,
            reason=reason or t.reason, decided_at=self._now(),
        )
        self._tickets[ticket_id] = updated
        return updated

    def consume(self, ticket_id: str, tool: str, args_digest: str) -> Ticket:
        """消费审批单。三件事必须同时成立：已批准、未过期、参数一致。"""
        t = self.get(ticket_id)
        if t.status is TicketStatus.CONSUMED:
            raise ApprovalInvalid("审批单已使用（一次性消费），请重新申请")
        if t.status is TicketStatus.REJECTED:
            raise ApprovalInvalid(f"审批单被拒绝：{t.reason}")
        if t.status is not TicketStatus.APPROVED:
            raise ApprovalInvalid(f"审批单尚未批准（当前 {t.status.value}）")
        now = self._now()
        if t.expired(now):
            self._tickets[ticket_id] = replace(t, status=TicketStatus.EXPIRED)
            raise ApprovalInvalid("审批单已过期")
        if t.tool != tool:
            raise ApprovalInvalid(f"审批单绑定的工具是 {t.tool}，不能用于 {tool}")
        if t.args_digest != args_digest:
            raise ApprovalInvalid(
                "实参与审批单不一致（参数指纹不匹配）——审批通过的是一个参数组合，"
                "换一组参数必须重新审批"
            )
        consumed = replace(t, status=TicketStatus.CONSUMED, decided_at=now)
        self._tickets[ticket_id] = consumed
        return consumed

    # ------------------------------------------------------------ 查询

    def pending(self) -> list[Ticket]:
        now = self._now()
        return [
            t for t in self._tickets.values()
            if t.status is TicketStatus.PENDING and not t.expired(now)
        ]

    def all_tickets(self) -> list[Ticket]:
        return list(self._tickets.values())

    def expire_due(self) -> int:
        now = self._now()
        n = 0
        for tid, t in list(self._tickets.items()):
            if t.status in (TicketStatus.PENDING, TicketStatus.APPROVED) and t.expired(now):
                self._tickets[tid] = replace(t, status=TicketStatus.EXPIRED)
                n += 1
        return n


def _summarize(arguments: dict) -> str:
    parts = []
    for k, v in list(arguments.items())[:4]:
        s = str(v).replace("\n", " ")
        if len(s) > 40:
            s = s[:37] + "…"
        parts.append(f"{k}={s}")
    return ", ".join(parts) or "（无参数）"
