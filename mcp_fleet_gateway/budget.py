"""超时预算：把"一个总超时"拆成"每个环节的可用时间"。

Agent 的典型链路是 规划 → 检索 → 调用工具 → 再规划 → 再调用，总时长有 SLO（比如 30s）。
如果每个工具调用都自带 10s 超时，第 4 次调用一定超 SLO —— 用户早就关掉页面了。

Deadline 的做法是把"剩余时间"显式传递下去：
  - 每一跳只拿到 min(工具自带超时, 剩余预算)；
  - 剩余预算不足时立刻失败并给出可执行的降级提示，而不是傻等。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .errors import DeadlineExceeded


@dataclass(frozen=True)
class Deadline:
    start: float
    budget_ms: int
    _now: Callable[[], float] = time.monotonic

    @classmethod
    def of(cls, ms: int, now_fn: Callable[[], float] = time.monotonic) -> "Deadline":
        return cls(start=now_fn(), budget_ms=max(0, int(ms)), _now=now_fn)

    @property
    def expires_at(self) -> float:
        return self.start + self.budget_ms / 1000

    def remaining_ms(self) -> float:
        return max(0.0, (self.expires_at - self._now()) * 1000)

    @property
    def expired(self) -> bool:
        return self.remaining_ms() <= 0

    def child(self, requested_ms: int) -> "Deadline":
        """子预算：不超过父预算的剩余部分。"""
        allowed = min(max(0, int(requested_ms)), int(round(self.remaining_ms())))
        return Deadline(start=self._now(), budget_ms=allowed, _now=self._now)

    def check(self, stage: str = "") -> None:
        if self.expired:
            raise DeadlineExceeded(
                f"整体预算已耗尽{'（' + stage + '）' if stage else ''}",
                suggestion="减少一次工具调用，或缩小检索范围后重试",
            )

    def elapsed_ms(self) -> float:
        return (self._now() - self.start) * 1000


class AttemptLedger:
    """同一 trace 内的尝试次数账本，防止"重试风暴"把预算和时间一起吃掉。"""

    def __init__(self, deadline: Deadline, *, max_attempts: int = 3) -> None:
        self.deadline = deadline
        self.max_attempts = max(1, max_attempts)
        self.attempts = 0

    def next_attempt(self) -> bool:
        if self.attempts >= self.max_attempts:
            return False
        if self.deadline.expired:
            return False
        self.attempts += 1
        return True

    def note_failure(self, backoff_ms: int) -> bool:
        """记录一次失败，返回"是否还值得重试"。"""
        if self.attempts >= self.max_attempts:
            return False
        if self.deadline.remaining_ms() <= backoff_ms + 50:
            return False
        return True
