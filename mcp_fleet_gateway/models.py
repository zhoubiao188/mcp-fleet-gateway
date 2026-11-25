"""核心数据模型。

设计原则：所有跨模块传递的对象都是不可变 dataclass，方便做 digest、做审计回放、
以及在多线程/多进程中间层之间序列化。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum, IntEnum
from typing import Any, Mapping, Sequence

NAMESPACE_SEP = "__"


class Risk(IntEnum):
    """工具的风险等级。

    网关不是靠"工具名看起来危险不危险"来判风险，而是靠声明 + 策略覆盖。
    READ 可以自由调用；WRITE 需要租户授权；DESTRUCTIVE 必须人工审批。
    """

    READ = 0
    WRITE = 1
    DESTRUCTIVE = 2

    @classmethod
    def parse(cls, value: "str | Risk | int") -> "Risk":
        if isinstance(value, Risk):
            return value
        if isinstance(value, int):
            return cls(value)
        return cls[value.strip().upper()]

    @property
    def label(self) -> str:
        return {Risk.READ: "read", Risk.WRITE: "write", Risk.DESTRUCTIVE: "destructive"}[self]


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL = "approval"


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


def canonical_json(obj: Any) -> str:
    """稳定序列化：同样的输入永远得到同样的字符串，是 digest 的前提。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def byte_len(obj: Any) -> int:
    if isinstance(obj, (bytes, bytearray)):
        return len(obj)
    if isinstance(obj, str):
        return len(obj.encode("utf-8"))
    return len(canonical_json(obj).encode("utf-8"))


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的全部元信息。这就是注入给模型的"工具契约"。"""

    server_id: str
    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_fields: tuple[str, ...] = ()
    risk: Risk = Risk.READ
    idempotent: bool = True
    timeout_ms: int = 5_000
    max_result_bytes: int = 16_000
    tags: tuple[str, ...] = ()
    version: str = "1"
    pinned: bool = False
    quarantined: bool = False
    quarantine_reason: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.server_id}{NAMESPACE_SEP}{self.name}"

    @property
    def schema_fingerprint(self) -> str:
        """只对"会影响调用契约"的部分做指纹，description 改动不算破坏性变更。"""
        props = self.input_schema.get("properties", {}) if self.input_schema else {}
        normalized = {
            "params": {
                name: {
                    "type": (spec or {}).get("type"),
                    "enum": sorted((spec or {}).get("enum", []) or []),
                }
                for name, spec in sorted(props.items())
            },
            "required": sorted(self.input_schema.get("required", []) if self.input_schema else []),
        }
        return digest(normalized)[:16]

    def with_quarantine(self, reason: str) -> "ToolSpec":
        return replace(self, quarantined=True, quarantine_reason=reason)

    def cleared(self) -> "ToolSpec":
        return replace(self, quarantined=False, quarantine_reason="")


@dataclass(frozen=True)
class ServerSpec:
    """一个 MCP Server 的注册信息。"""

    id: str
    description: str = ""
    tools: tuple[ToolSpec, ...] = ()
    weight: int = 100
    max_concurrency: int = 8
    default_timeout_ms: int = 5_000
    owner: str = ""

    def tool(self, name: str) -> "ToolSpec | None":
        for t in self.tools:
            if t.name == name:
                return t
        return None


@dataclass
class CallRequest:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    tenant: str = "default"
    trace_id: str = ""
    deadline_ms: int = 8_000
    approval_ticket: str | None = None
    server_hint: str | None = None

    @property
    def args_digest(self) -> str:
        """只对实参做指纹。

        工具名单独绑定（审批单里存的是 qualified name），所以这里不带 tool，
        这样同一个工具用裸名或全名调用都能对上同一张审批单。
        """
        return digest({"arguments": self.arguments})

    def child(self, tool: str, arguments: dict[str, Any]) -> "CallRequest":
        return replace(self, tool=tool, arguments=arguments)


@dataclass
class CallResult:
    ok: bool
    tool: str
    server_id: str = ""
    content: Any = None
    error: str = ""
    error_code: str = ""
    error_detail: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    bytes_before: int = 0
    bytes_after: int = 0
    truncated: bool = False
    cursor: str | None = None
    audit_id: str = ""
    decision: Decision = Decision.ALLOW
    stages: tuple[str, ...] = ()
    trace_id: str = ""
    risk: Risk = Risk.READ
    approval_ticket: str = ""

    @property
    def saved_ratio(self) -> float:
        if not self.bytes_before:
            return 0.0
        return 1 - (self.bytes_after / self.bytes_before)


@dataclass
class Manifest:
    """注入给模型的工具清单。"""

    text: str
    tools: tuple[str, ...] = ()
    est_tokens: int = 0
    omitted: tuple[str, ...] = ()
    staged: bool = False
    servers: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    rule: str = "default"
    reason: str = ""
    risk: Risk = Risk.READ

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


@dataclass(frozen=True)
class SchemaDrift:
    server_id: str
    tool: str
    kind: str
    detail: str
    breaking: bool

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        flag = "BREAKING" if self.breaking else "info"
        return f"[{flag}] {self.server_id}{NAMESPACE_SEP}{self.tool}: {self.kind} ({self.detail})"


@dataclass(frozen=True)
class RegisterResult:
    server_id: str
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    conflicts: tuple[tuple[str, tuple[str, ...]], ...] = ()
    drift: tuple[SchemaDrift, ...] = ()
    quarantine: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuditEvent:
    audit_id: str
    trace_id: str
    tenant: str
    server_id: str
    tool: str
    risk: Risk
    decision: Decision
    ok: bool
    error_code: str = ""
    rule: str = ""
    ticket_id: str = ""
    args_digest: str = ""
    elapsed_ms: float = 0.0
    bytes_before: int = 0
    bytes_after: int = 0
    ts: float = 0.0

    def as_row(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "trace_id": self.trace_id,
            "tenant": self.tenant,
            "server": self.server_id,
            "tool": self.tool,
            "risk": self.risk.label,
            "decision": self.decision.value,
            "ok": self.ok,
            "error_code": self.error_code,
            "rule": self.rule,
            "ticket": self.ticket_id,
            "args": self.args_digest[:12],
            "ms": round(self.elapsed_ms, 2),
            "bytes": f"{self.bytes_before}->{self.bytes_after}",
            "ts": round(self.ts, 3),
        }


@dataclass(frozen=True)
class ShapedResult:
    content: Any
    before: int
    after: int
    truncated: bool = False
    cursor: str | None = None

    @property
    def changed(self) -> bool:
        return self.after != self.before
