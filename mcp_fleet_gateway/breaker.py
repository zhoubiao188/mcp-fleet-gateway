"""故障隔离：每个 MCP Server 一个熔断器 + 并发闸。

为什么必须按 Server 隔离：
Agent 一次任务会并发调 5 个 Server。只要有一个 Server 变慢（比如某个自建
Server 的向量检索卡到 30 秒），共享线程池被打满，剩下 4 个健康的 Server 也一起不可用。
这就是典型的"一个下游拖垮整条链路"。

熔断器状态机：
  CLOSED --(窗口内失败数>=阈值)--> OPEN --(冷却结束)--> HALF_OPEN
  HALF_OPEN --(探针成功)--> CLOSED
  HALF_OPEN --(探针失败)--> OPEN（冷却时间指数退避，上限 max_open_s）
"""
from __future__ import annotations

import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator

from .errors import CircuitOpen, ServerBusy
from .models import BreakerState


@dataclass
class BreakerSnapshot:
    server_id: str
    state: BreakerState
    failures: int
    samples: int
    opened_count: int
    open_for_ms: float

    def as_dict(self) -> dict:
        return {
            "server": self.server_id,
            "state": self.state.value,
            "failures": self.failures,
            "samples": self.samples,
            "opened": self.opened_count,
            "open_for_ms": round(self.open_for_ms, 1),
        }


class CircuitBreaker:
    def __init__(
        self,
        server_id: str = "",
        *,
        failure_threshold: int = 5,
        window_s: float = 30.0,
        open_s: float = 15.0,
        half_open_max: int = 1,
        backoff: float = 2.0,
        max_open_s: float = 300.0,
        min_samples: int = 3,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold 必须 >= 1")
        if half_open_max < 1:
            raise ValueError("half_open_max 必须 >= 1")
        self.server_id = server_id
        self.failure_threshold = failure_threshold
        self.window_s = window_s
        self.open_s = open_s
        self.half_open_max = half_open_max
        self.backoff = backoff
        self.max_open_s = max_open_s
        self.min_samples = min_samples
        self._now = now_fn

        self._events: deque[tuple[float, bool]] = deque()
        self._state = BreakerState.CLOSED
        self._opened_at = 0.0
        self._opened_count = 0
        self._probes = 0

    # ------------------------------------------------------------ 状态

    @property
    def state(self) -> BreakerState:
        self._maybe_half_open()
        return self._state

    def _maybe_half_open(self) -> None:
        if self._state is BreakerState.OPEN and self._now() - self._opened_at >= self.current_open_s():
            self._state = BreakerState.HALF_OPEN
            self._probes = 0

    def current_open_s(self) -> float:
        if self._opened_count <= 1:
            return self.open_s
        return min(self.open_s * (self.backoff ** (self._opened_count - 1)), self.max_open_s)

    def _prune(self) -> None:
        cutoff = self._now() - self.window_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    # ------------------------------------------------------------ 判定

    def allow(self) -> bool:
        """是否放行一次调用。HALF_OPEN 下每次放行都占用一个探针配额。"""
        self._maybe_half_open()
        if self._state is BreakerState.CLOSED:
            return True
        if self._state is BreakerState.OPEN:
            return False
        if self._probes < self.half_open_max:
            self._probes += 1
            return True
        return False

    def record(self, ok: bool) -> None:
        now = self._now()
        if self._state is BreakerState.HALF_OPEN:
            if ok:
                self._close()
            else:
                self._open(now)
            return
        if self._state is BreakerState.OPEN:
            return
        self._events.append((now, ok))
        self._prune()
        failures = sum(1 for _, o in self._events if not o)
        if failures >= self.failure_threshold and len(self._events) >= self.min_samples:
            self._open(now)

    def _open(self, now: float) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = now
        self._opened_count += 1
        self._probes = 0

    def _close(self) -> None:
        self._state = BreakerState.CLOSED
        self._events.clear()
        self._probes = 0
        # opened_count 保留：用于统计"这个 Server 反复出问题"

    def failure_rate(self) -> float:
        self._prune()
        if not self._events:
            return 0.0
        failures = sum(1 for _, o in self._events if not o)
        return failures / len(self._events)

    def snapshot(self) -> BreakerSnapshot:
        self._prune()
        open_for = 0.0
        if self._state is BreakerState.OPEN:
            open_for = (self._now() - self._opened_at) * 1000
        return BreakerSnapshot(
            server_id=self.server_id,
            state=self._state,
            failures=sum(1 for _, o in self._events if not o),
            samples=len(self._events),
            opened_count=self._opened_count,
            open_for_ms=open_for,
        )

    def reset(self) -> None:
        self._state = BreakerState.CLOSED
        self._events.clear()
        self._opened_count = 0
        self._probes = 0
        self._opened_at = 0.0


class ServerGuard:
    """熔断 + 并发上限 + 统计，一个 Server 一个实例。"""

    def __init__(
        self,
        server_id: str,
        *,
        max_concurrency: int = 8,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.server_id = server_id
        self.max_concurrency = max_concurrency
        self.breaker = breaker or CircuitBreaker(server_id)
        self._inflight = 0
        self.calls = 0
        self.failures = 0
        self.rejected_open = 0
        self.rejected_busy = 0

    @property
    def inflight(self) -> int:
        return self._inflight

    def acquire(self) -> None:
        if not self.breaker.allow():
            self.rejected_open += 1
            raise CircuitOpen(
                f"{self.server_id} 已熔断（{self.breaker.state.value}），跳过调用",
                server=self.server_id,
                retry_after_ms=round(self.breaker.current_open_s() * 1000),
            )
        if self._inflight >= self.max_concurrency:
            self.rejected_busy += 1
            raise ServerBusy(
                f"{self.server_id} 并发已满（{self._inflight}/{self.max_concurrency}）",
                server=self.server_id,
            )
        self._inflight += 1
        self.calls += 1

    def release(self, ok: bool) -> None:
        self._inflight = max(0, self._inflight - 1)
        if not ok:
            self.failures += 1
        self.breaker.record(ok)

    @contextmanager
    def slot(self) -> Iterator["ServerGuard"]:
        self.acquire()
        ok = False
        try:
            yield self
            ok = True
        finally:
            self.release(ok)

    def health(self) -> dict:
        snap = self.breaker.snapshot()
        return {
            "server": self.server_id,
            "state": snap.state.value,
            "inflight": self._inflight,
            "max_concurrency": self.max_concurrency,
            "calls": self.calls,
            "failures": self.failures,
            "failure_rate": round(self.breaker.failure_rate(), 3),
            "rejected_open": self.rejected_open,
            "rejected_busy": self.rejected_busy,
            "opened_count": snap.opened_count,
        }
