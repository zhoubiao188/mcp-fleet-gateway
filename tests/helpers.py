"""测试辅助：构造可控的 ToolSpec / ServerSpec / 时钟。"""
from __future__ import annotations

from typing import Any

from mcp_fleet_gateway import (
    CallRequest,
    FleetGateway,
    PolicyEngine,
    Risk,
    ServerSpec,
    ToolSpec,
)


class FakeClock:
    """可控时钟。所有和"时间流逝"相关的逻辑都必须能被它驱动。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def ms(self, ms: float) -> None:
        self.t += ms / 1000


def make_tool(
    name: str,
    server_id: str = "s1",
    *,
    description: str = "",
    props: dict[str, Any] | None = None,
    required: tuple[str, ...] = (),
    risk: Risk = Risk.READ,
    input_schema: dict[str, Any] | None = None,
    **kw: Any,
) -> ToolSpec:
    schema = input_schema
    if schema is None:
        schema = {
            "type": "object",
            "properties": props if props is not None else {"q": {"type": "string"}},
            "required": list(required),
        }
    return ToolSpec(
        server_id=server_id,
        name=name,
        description=description or f"{name} 工具",
        input_schema=schema,
        risk=risk,
        **kw,
    )


def make_server(
    server_id: str = "s1",
    tools: tuple[ToolSpec, ...] | None = None,
    *,
    description: str = "",
    weight: int = 100,
    max_concurrency: int = 8,
) -> ServerSpec:
    return ServerSpec(
        id=server_id,
        description=description or f"{server_id} 服务",
        tools=tools if tools is not None else (make_tool("ping", server_id),),
        weight=weight,
        max_concurrency=max_concurrency,
    )


def make_gateway(**kw: Any) -> FleetGateway:
    kw.setdefault("policy", PolicyEngine())
    kw.setdefault("sleep_fn", lambda _s: None)
    return FleetGateway(**kw)


def req(tool: str, **arguments: Any) -> CallRequest:
    return CallRequest(tool=tool, arguments=arguments)
