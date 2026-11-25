"""进程内 MCP transport（用于测试与演示）。

真实场景这里会是 stdio JSON-RPC（mcp.client.stdio）或 Streamable HTTP。
把它们抽象成同一个 ``call(tool, args) -> Any`` 接口，网关的编排逻辑就完全不关心
底下是子进程、HTTP 还是内存函数——这也是做可测试性的关键：
**网关的所有可靠性逻辑都能在不启动任何 MCP Server 的情况下跑完单测**。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import ToolNotFound, TransportError
from .models import ServerSpec


@dataclass
class InProcessServer:
    spec: ServerSpec
    handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    latency_ms: float = 0.0
    sleep_fn: Callable[[float], None] = time.sleep
    calls: int = 0
    last_error: str = ""

    # 故障注入：接下来 N 次调用直接失败
    _fail_budget: int = 0
    _fail_exc: str = "injected failure"

    def register(self, name: str, fn: Callable[..., Any]) -> "InProcessServer":
        self.handlers[name] = fn
        return self

    def fail_next(self, n: int = 1, message: str = "injected failure") -> "InProcessServer":
        self._fail_budget = n
        self._fail_exc = message
        return self

    def heal(self) -> "InProcessServer":
        self._fail_budget = 0
        return self

    @property
    def server_id(self) -> str:
        return self.spec.id

    def call(self, tool: str, args: dict[str, Any]) -> Any:
        self.calls += 1
        if self.latency_ms:
            self.sleep_fn(self.latency_ms / 1000)
        if self._fail_budget > 0:
            self._fail_budget -= 1
            self.last_error = self._fail_exc
            raise TransportError(f"{self.spec.id}/{tool}: {self._fail_exc}", server=self.spec.id)
        fn = self.handlers.get(tool)
        if fn is None:
            raise ToolNotFound(f"{self.spec.id} 未实现工具 {tool!r}", server=self.spec.id)
        try:
            return fn(**args)
        except TypeError as exc:
            raise TransportError(
                f"{self.spec.id}/{tool}: 参数不匹配（{exc}）", server=self.spec.id
            ) from exc
        except Exception as exc:  # noqa: BLE001 - transport 边界必须兜住一切
            self.last_error = str(exc)
            raise TransportError(f"{self.spec.id}/{tool}: {exc}", server=self.spec.id) from exc
