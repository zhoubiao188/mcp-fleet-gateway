"""审计与回放。

审计日志的价值不在于"事后追责"，而在于**回放**：
模型说"我没乱删东西"，你把这一个 trace 的所有事件拉出来，按时间排序，
看到 decision=allow / rule=default:write / tool=db__execute_sql / args=8f3a…，
再配合原始参数快照就能定位到底是策略写松了，还是模型越权了。

生产上这里应该接 OTLP / Kafka；本项目落 JSONL，方便本地 replay。
"""
from __future__ import annotations

import json
import secrets
import time
from collections import Counter, deque
from pathlib import Path
from typing import Callable, Iterable

from .models import AuditEvent, Decision, Risk


class AuditLog:
    def __init__(
        self,
        *,
        sink: str | Path | None = None,
        capacity: int = 4_000,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._now = now_fn
        self._events: deque[AuditEvent] = deque(maxlen=capacity)
        self._sink = Path(sink) if sink else None
        if self._sink:
            self._sink.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        trace_id: str,
        tenant: str,
        server_id: str,
        tool: str,
        risk: Risk,
        decision: Decision,
        ok: bool,
        error_code: str = "",
        rule: str = "",
        ticket_id: str = "",
        args_digest: str = "",
        elapsed_ms: float = 0.0,
        bytes_before: int = 0,
        bytes_after: int = 0,
    ) -> str:
        audit_id = "au_" + secrets.token_hex(6)
        evt = AuditEvent(
            audit_id=audit_id,
            trace_id=trace_id,
            tenant=tenant,
            server_id=server_id,
            tool=tool,
            risk=risk,
            decision=decision,
            ok=ok,
            error_code=error_code,
            rule=rule,
            ticket_id=ticket_id,
            args_digest=args_digest,
            elapsed_ms=elapsed_ms,
            bytes_before=bytes_before,
            bytes_after=bytes_after,
            ts=self._now(),
        )
        self._events.append(evt)
        if self._sink:
            with self._sink.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(evt.as_row(), ensure_ascii=False) + "\n")
        return audit_id

    # ------------------------------------------------------------ 查询

    def __len__(self) -> int:
        return len(self._events)

    def events(self) -> list[AuditEvent]:
        return list(self._events)

    def recent(self, n: int = 10) -> list[AuditEvent]:
        return list(self._events)[-n:]

    def by_trace(self, trace_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.trace_id == trace_id]

    def replay(self, trace_id: str) -> list[dict]:
        rows = []
        for i, e in enumerate(self.by_trace(trace_id), 1):
            r = e.as_row()
            r["step"] = i
            rows.append(r)
        return rows

    # ------------------------------------------------------------ 统计

    def stats(self) -> dict:
        evts = list(self._events)
        if not evts:
            return {"events": 0}
        by_decision = Counter(e.decision.value for e in evts)
        by_error = Counter(e.error_code for e in evts if e.error_code)
        before = sum(e.bytes_before for e in evts)
        after = sum(e.bytes_after for e in evts)
        per_server = Counter(e.server_id for e in evts)
        return {
            "events": len(evts),
            "ok": sum(1 for e in evts if e.ok),
            "failed": sum(1 for e in evts if not e.ok),
            "by_decision": dict(by_decision),
            "by_error": dict(by_error.most_common(8)),
            "bytes_before": before,
            "bytes_after": after,
            "bytes_saved_ratio": round(1 - after / before, 4) if before else 0.0,
            "per_server": dict(per_server),
        }
