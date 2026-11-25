"""MCP Fleet Gateway —— 把 N 个 MCP Server 收敛成一个可运维的入口。"""

from __future__ import annotations

from .approval import ApprovalQueue, Ticket, TicketStatus
from .audit import AuditLog
from .breaker import CircuitBreaker, ServerGuard
from .budget import AttemptLedger, Deadline
from .catalog import ConflictStrategy, ToolCatalog, classify_drift
from .errors import (
    AmbiguousTool,
    ApprovalInvalid,
    ApprovalRequired,
    CircuitOpen,
    DeadlineExceeded,
    GatewayError,
    InvalidArguments,
    PolicyDenied,
    ServerBusy,
    ToolNotFound,
    ToolQuarantined,
    TransportError,
)
from .gateway import FleetGateway, GatewayCounter
from .manifest import build_manifest, estimate_tokens, render_tool_line, score_tool
from .models import (
    AuditEvent,
    BreakerState,
    CallRequest,
    CallResult,
    Decision,
    Manifest,
    PolicyDecision,
    RegisterResult,
    Risk,
    SchemaDrift,
    ServerSpec,
    ShapedResult,
    ToolSpec,
)
from .policy import DANGEROUS_ARG_PATTERNS, PolicyEngine, Rule, matched_guards
from .shaping import CursorStore, ShapePolicy, page_from_cursor, shape
from .transports import InProcessServer

__version__ = "0.1.0"

__all__ = [
    "AmbiguousTool",
    "ApprovalInvalid",
    "ApprovalQueue",
    "ApprovalRequired",
    "AttemptLedger",
    "AuditEvent",
    "AuditLog",
    "BreakerState",
    "CallRequest",
    "CallResult",
    "CircuitBreaker",
    "CircuitOpen",
    "ConflictStrategy",
    "CursorStore",
    "DANGEROUS_ARG_PATTERNS",
    "Deadline",
    "DeadlineExceeded",
    "Decision",
    "FleetGateway",
    "GatewayCounter",
    "GatewayError",
    "InProcessServer",
    "InvalidArguments",
    "Manifest",
    "PolicyDecision",
    "PolicyDenied",
    "PolicyEngine",
    "RegisterResult",
    "Risk",
    "Rule",
    "SchemaDrift",
    "ServerBusy",
    "ServerGuard",
    "ServerSpec",
    "ShapePolicy",
    "ShapedResult",
    "Ticket",
    "TicketStatus",
    "ToolCatalog",
    "ToolNotFound",
    "ToolQuarantined",
    "ToolSpec",
    "TransportError",
    "build_manifest",
    "classify_drift",
    "estimate_tokens",
    "matched_guards",
    "page_from_cursor",
    "render_tool_line",
    "score_tool",
    "shape",
]
