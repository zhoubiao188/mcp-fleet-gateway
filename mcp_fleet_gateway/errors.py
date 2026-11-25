"""网关错误体系。

每个错误都带一个稳定的 ``code``，这个 code 会被写进审计日志并回给上游 Agent。
原因是：模型对"错误码 + 可执行提示"的处理能力远强于对一段自然语言报错的处理能力。
"""
from __future__ import annotations


class GatewayError(Exception):
    code = "gateway_error"
    retryable = False

    def __init__(self, message: str, **detail: object) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.detail}


class ToolNotFound(GatewayError):
    code = "tool_not_found"


class AmbiguousTool(GatewayError):
    code = "ambiguous_tool"


class ToolQuarantined(GatewayError):
    code = "tool_quarantined"


class InvalidArguments(GatewayError):
    code = "invalid_arguments"


class PolicyDenied(GatewayError):
    code = "policy_denied"


class ApprovalRequired(GatewayError):
    code = "approval_required"

    def __init__(self, message: str, ticket_id: str = "", **detail: object) -> None:
        super().__init__(message, ticket_id=ticket_id, **detail)
        self.ticket_id = ticket_id


class ApprovalInvalid(GatewayError):
    code = "approval_invalid"


class CircuitOpen(GatewayError):
    code = "circuit_open"
    retryable = True


class ServerBusy(GatewayError):
    code = "server_busy"
    retryable = True


class DeadlineExceeded(GatewayError):
    code = "deadline_exceeded"
    retryable = True


class TransportError(GatewayError):
    code = "transport_error"
    retryable = True


class DuplicateInvocation(GatewayError):
    code = "duplicate_invocation"
