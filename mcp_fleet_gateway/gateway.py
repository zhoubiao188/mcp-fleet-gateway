"""编排核心：一次工具调用要过 8 道关卡。

    resolve → quarantine → validate → policy → approval → idempotency → admit → call → shape → audit

顺序是刻意排的：
  - validate 放在 policy 之前：参数都不合法就别去麻烦人审批；
  - approval 放在 admit 之前：别让一个等待审批的请求占着 Server 的并发位；
  - shape 放在 call 之后、audit 之前：审计里要同时记录瘦身前后的字节数。
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence

from .approval import ApprovalQueue, Ticket
from .audit import AuditLog
from .breaker import CircuitBreaker, ServerGuard
from .budget import AttemptLedger, Deadline
from .catalog import ConflictStrategy, ToolCatalog
from .errors import (
    ApprovalInvalid,
    ApprovalRequired,
    CircuitOpen,
    DeadlineExceeded,
    GatewayError,
    InvalidArguments,
    PolicyDenied,
    ToolNotFound,
    TransportError,
)
from .manifest import build_manifest, estimate_tokens, render_tool_line
from .models import (
    CallRequest,
    CallResult,
    Decision,
    Manifest,
    PolicyDecision,
    RegisterResult,
    Risk,
    ServerSpec,
    ToolSpec,
    digest,
)
from .policy import PolicyEngine
from .shaping import CursorStore, ShapePolicy, page_from_cursor, shape
from .transports import InProcessServer
from .validation import validate


@dataclass
class GatewayCounter:
    calls: int = 0
    ok: int = 0
    denied: int = 0
    approval_required: int = 0
    approval_used: int = 0
    replays: int = 0
    shaped: int = 0
    retries: int = 0

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "ok": self.ok,
            "denied": self.denied,
            "approval_required": self.approval_required,
            "approval_used": self.approval_used,
            "replays": self.replays,
            "shaped": self.shaped,
            "retries": self.retries,
        }


class FleetGateway:
    def __init__(
        self,
        *,
        conflict_strategy: ConflictStrategy = ConflictStrategy.REJECT,
        catalog: ToolCatalog | None = None,
        policy: PolicyEngine | None = None,
        approvals: ApprovalQueue | None = None,
        audit: AuditLog | None = None,
        shape_policy: ShapePolicy | None = None,
        budget_tokens: int = 1_200,
        max_attempts: int = 2,
        idem_ttl_ms: int = 60_000,
        now_fn: Callable[[], float] = time.time,
        mono_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        breaker_factory: Callable[[str], CircuitBreaker] | None = None,
    ) -> None:
        self.catalog = catalog or ToolCatalog(conflict_strategy=conflict_strategy)
        self.policy = policy or PolicyEngine()
        self._now = now_fn
        self._mono = mono_fn
        self._sleep = sleep_fn
        self.approvals = approvals or ApprovalQueue(now_fn=now_fn)
        self.audit = audit or AuditLog(now_fn=now_fn)
        self.shape_policy = shape_policy or ShapePolicy()
        self.budget_tokens = budget_tokens
        self.max_attempts = max(1, max_attempts)
        self.idem_ttl_ms = idem_ttl_ms
        self.counters = GatewayCounter()
        self.transports: dict[str, InProcessServer] = {}
        self.guards: dict[str, ServerGuard] = {}
        self.pages = CursorStore()
        self._breaker_factory = breaker_factory or (lambda sid: CircuitBreaker(sid, now_fn=mono_fn))
        self._idem: dict[str, tuple[float, CallResult]] = {}

    # ------------------------------------------------------------ 挂载

    def add_server(
        self,
        spec: ServerSpec,
        *,
        handlers: Mapping[str, Callable[..., Any]] | None = None,
        latency_ms: float = 0.0,
        replace_existing: bool = False,
    ) -> RegisterResult:
        transport = InProcessServer(
            spec=spec,
            handlers=dict(handlers or {}),
            latency_ms=latency_ms,
            sleep_fn=self._sleep,
        )
        return self.mount(transport, replace_existing=replace_existing)

    def mount(self, transport: InProcessServer, *, replace_existing: bool = False) -> RegisterResult:
        result = self.catalog.register_server(transport.spec, replace_existing=replace_existing)
        self.transports[transport.spec.id] = transport
        if transport.spec.id not in self.guards:
            self.guards[transport.spec.id] = ServerGuard(
                transport.spec.id,
                max_concurrency=transport.spec.max_concurrency,
                breaker=self._breaker_factory(transport.spec.id),
            )
        return result

    def acknowledge(self, qualified: str) -> ToolSpec:
        self._idem.clear()
        return self.catalog.acknowledge(qualified)

    def alias(self, bare: str, qualified: str) -> None:
        self.catalog.add_alias(bare, qualified)

    # ------------------------------------------------------------ 工具清单

    def manifest(
        self,
        *,
        budget_tokens: int | None = None,
        query: str | None = None,
        expand: Sequence[str] = (),
    ) -> Manifest:
        budget = self.budget_tokens if budget_tokens is None else budget_tokens
        if expand:
            footer = "\n## 网关能力\n- gw_page(cursor)：取下一页工具结果"
        else:
            footer = (
                "\n## 网关能力\n"
                "- gw_discover(server)：展开某个 Server 的全部工具\n"
                "- gw_page(cursor)：取下一页工具结果\n"
                "- gw_acknowledge(tool)：确认 Schema 变更后解除隔离"
            )
        # footer 也是要占上下文的，必须一起算预算，否则会出现"标称 600 实际 679"
        m = build_manifest(
            self.catalog,
            budget_tokens=max(0, budget - estimate_tokens(footer)),
            query=query,
            expand=expand,
        )
        text = m.text + footer
        return replace(m, text=text, est_tokens=estimate_tokens(text))

    def discover(self, server: str, *, budget_tokens: int = 1_600) -> dict[str, Any]:
        self.catalog.server(server)  # 不存在会抛错
        m = build_manifest(self.catalog, budget_tokens=budget_tokens, expand=(server,))
        server_tools = self.catalog.server_tools(server)
        return {
            "server": server,
            "tools": [t.qualified for t in server_tools],
            "count": len(server_tools),
            "tokens": sum(estimate_tokens(render_tool_line(t)) + 1 for t in server_tools),
            "est_tokens": m.est_tokens,
            "text": m.text,
        }

    # ------------------------------------------------------------ 调用

    def invoke(self, req: CallRequest, *, raise_on_error: bool = False) -> CallResult:
        self.counters.calls += 1
        try:
            return self._invoke(req)
        except GatewayError as exc:
            result = self._failure(req, exc)
            if raise_on_error:
                raise
            return result

    def _invoke(self, req: CallRequest) -> CallResult:
        trace_id = req.trace_id or ("tr_" + secrets.token_hex(5))
        started = self._mono()

        spec = self.catalog.resolve(req.tool, req.server_hint)
        self.catalog.ensure_usable(spec)

        errors = validate(spec.input_schema, req.arguments)
        if errors:
            raise InvalidArguments(
                f"{spec.qualified} 参数校验失败（{len(errors)} 处）",
                errors=errors[:8],
                hint=f"必填参数：{', '.join(spec.input_schema.get('required', [])) or '无'}",
            )

        pd = self.policy.evaluate(req, spec)
        ticket_id = ""
        if pd.decision is Decision.DENY:
            raise PolicyDenied(pd.reason or "策略拒绝", rule=pd.rule, tool=spec.qualified)
        if pd.decision is Decision.APPROVAL:
            if not req.approval_ticket:
                self.counters.approval_required += 1
                self.approvals.raise_for(req, spec, pd.reason)
            ticket = self.approvals.consume(
                req.approval_ticket, spec.qualified, req.args_digest
            )
            ticket_id = ticket.id
            self.counters.approval_used += 1

        replay = self._idem_lookup(req, spec)
        if replay is not None:
            self.counters.replays += 1
            audit_id = self.audit.append(
                trace_id=trace_id,
                tenant=req.tenant,
                server_id=spec.server_id,
                tool=spec.qualified,
                risk=spec.risk,
                decision=pd.decision,
                ok=True,
                rule="idempotency:" + pd.rule,
                ticket_id=ticket_id,
                args_digest=req.args_digest,
                bytes_before=replay.bytes_before,
                bytes_after=replay.bytes_after,
            )
            return replace(
                replay,
                trace_id=trace_id,
                audit_id=audit_id,
                stages=replay.stages + ("idempotent_replay",),
            )

        guard = self._guard_for(spec.server_id, spec)
        transport = self.transports.get(spec.server_id)
        if transport is None:  # pragma: no cover - 注册保证存在
            raise ToolNotFound(f"{spec.server_id} 未挂载 transport")

        guard.acquire()
        ok = False
        try:
            payload = self._call_with_retry(req, spec, transport)
            ok = True
        finally:
            guard.release(ok)

        shaped = self._shape(payload, spec)
        elapsed = (self._mono() - started) * 1000

        audit_id = self.audit.append(
            trace_id=trace_id,
            tenant=req.tenant,
            server_id=spec.server_id,
            tool=spec.qualified,
            risk=spec.risk,
            decision=pd.decision,
            ok=True,
            rule=pd.rule,
            ticket_id=ticket_id,
            args_digest=req.args_digest,
            elapsed_ms=elapsed,
            bytes_before=shaped.before,
            bytes_after=shaped.after,
        )

        result = CallResult(
            ok=True,
            tool=spec.qualified,
            server_id=spec.server_id,
            content=shaped.content,
            elapsed_ms=elapsed,
            bytes_before=shaped.before,
            bytes_after=shaped.after,
            truncated=shaped.truncated,
            cursor=shaped.cursor,
            audit_id=audit_id,
            decision=pd.decision,
            trace_id=trace_id,
            stages=(
                "resolve", "validate", "policy", "approval" if ticket_id else "admit",
                "call", "shape",
            ),
            risk=spec.risk,
        )
        if shaped.truncated:
            self.counters.shaped += 1
        self.counters.ok += 1

        if not spec.idempotent:
            self._idem[self._idem_key(req, spec)] = (self._now(), result)
        return result

    # ------------------------------------------------------------ 内部

    def _guard_for(self, server_id: str, spec: ToolSpec) -> ServerGuard:
        guard = self.guards.get(server_id)
        if guard is None:
            self.guards[server_id] = guard = ServerGuard(
                server_id, breaker=self._breaker_factory(server_id)
            )
        return guard

    def _call_with_retry(self, req: CallRequest, spec: ToolSpec, transport: InProcessServer) -> Any:
        deadline = Deadline.of(req.deadline_ms, self._mono)
        child = deadline.child(spec.timeout_ms)
        eta = float(getattr(transport, "latency_ms", 0.0) or 0.0)
        if eta and eta > child.remaining_ms():
            raise DeadlineExceeded(
                f"{spec.qualified} 预计耗时 {eta:.0f}ms，剩余预算仅 {child.remaining_ms():.0f}ms",
                suggestion="跳过该工具或缩小参数范围",
                server=spec.server_id,
            )
        child.check(spec.qualified)

        ledger = AttemptLedger(deadline, max_attempts=self.max_attempts if spec.idempotent else 1)
        last: TransportError | None = None
        while ledger.next_attempt():
            try:
                return transport.call(spec.name, req.arguments)
            except TransportError as exc:
                last = exc
                backoff_ms = 20 * ledger.attempts
                if not spec.idempotent:
                    raise
                if not ledger.note_failure(backoff_ms):
                    raise
                self.counters.retries += 1
                self._sleep(backoff_ms / 1000)
        if last is not None:
            raise last
        raise DeadlineExceeded(f"{spec.qualified} 预算耗尽", server=spec.server_id)

    def _shape(self, payload: Any, spec: ToolSpec):
        policy = self.shape_policy.merged(
            max_bytes=spec.max_result_bytes, project=spec.output_fields
        )
        return shape(payload, policy, server_id=spec.server_id, tool=spec.qualified, store=self.pages)

    def _idem_key(self, req: CallRequest, spec: ToolSpec) -> str:
        return f"{req.tenant}:{spec.qualified}:{req.args_digest}"

    def _idem_lookup(self, req: CallRequest, spec: ToolSpec) -> CallResult | None:
        if spec.idempotent:
            return None
        key = self._idem_key(req, spec)
        hit = self._idem.get(key)
        if hit is None:
            return None
        ts, result = hit
        if (self._now() - ts) * 1000 > self.idem_ttl_ms:
            self._idem.pop(key, None)
            return None
        return result

    def fetch_page(self, cursor: str) -> CallResult:
        page = page_from_cursor(self.pages, cursor, self.shape_policy)
        ok = not (isinstance(page.content, Mapping) and page.content.get("error"))
        return CallResult(
            ok=ok,
            tool="gw_page",
            content=page.content,
            bytes_before=page.before,
            bytes_after=page.after,
            truncated=True,
            cursor=page.cursor,
            error_code="" if ok else "cursor_invalid",
            error="" if ok else "翻页句柄无效或已过期",
        )

    def _failure(self, req: CallRequest, exc: GatewayError) -> CallResult:
        server_id = ""
        risk = Risk.READ
        qualified = req.tool
        try:
            spec = self.catalog.resolve(req.tool, req.server_hint)
            server_id, qualified, risk = spec.server_id, spec.qualified, spec.risk
        except GatewayError:
            pass

        decision = Decision.DENY
        if isinstance(exc, ApprovalRequired):
            decision = Decision.APPROVAL
        elif isinstance(exc, (CircuitOpen, DeadlineExceeded, TransportError)):
            decision = Decision.ALLOW

        if isinstance(exc, PolicyDenied):
            self.counters.denied += 1

        trace_id = req.trace_id or ("tr_" + secrets.token_hex(5))
        audit_id = self.audit.append(
            trace_id=trace_id,
            tenant=req.tenant,
            server_id=server_id,
            tool=qualified,
            risk=risk,
            decision=decision,
            ok=False,
            error_code=exc.code,
            rule=str(exc.detail.get("rule", "")),
            args_digest=req.args_digest,
        )
        return CallResult(
            ok=False,
            tool=qualified,
            server_id=server_id,
            error=exc.message,
            error_code=exc.code,
            error_detail=exc.to_dict(),
            audit_id=audit_id,
            decision=decision,
            trace_id=trace_id,
            risk=risk,
            stages=("rejected", exc.code),
            approval_ticket=str(exc.detail.get("ticket_id", "")),
        )

    # ------------------------------------------------------------ 观测

    def health(self) -> dict[str, Any]:
        return {
            sid: guard.health()
            for sid, guard in sorted(self.guards.items())
        }

    def stats(self) -> dict[str, Any]:
        return {
            "counters": self.counters.as_dict(),
            "catalog": self.catalog.stats(),
            "audit": self.audit.stats(),
            "pages": len(self.pages),
            "health": self.health(),
        }
