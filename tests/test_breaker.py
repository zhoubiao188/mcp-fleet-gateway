import pytest

from helpers import FakeClock

from mcp_fleet_gateway import (
    BreakerState,
    CircuitBreaker,
    CircuitOpen,
    ServerBusy,
    ServerGuard,
)


def breaker(clock, **kw):
    kw.setdefault("failure_threshold", 3)
    kw.setdefault("min_samples", 3)
    kw.setdefault("window_s", 10.0)
    kw.setdefault("open_s", 5.0)
    return CircuitBreaker("s1", now_fn=clock, **kw)


class TestStateMachine:
    def test_starts_closed(self, clock):
        assert breaker(clock).state is BreakerState.CLOSED

    def test_allows_when_closed(self, clock):
        assert breaker(clock).allow() is True

    def test_opens_after_threshold(self, clock):
        cb = breaker(clock)
        for _ in range(3):
            cb.record(False)
        assert cb.state is BreakerState.OPEN
        assert cb.allow() is False

    def test_stays_closed_below_threshold(self, clock):
        cb = breaker(clock)
        cb.record(False)
        cb.record(False)
        assert cb.state is BreakerState.CLOSED

    def test_success_resets_nothing_but_does_not_open(self, clock):
        cb = breaker(clock)
        for _ in range(10):
            cb.record(True)
        assert cb.state is BreakerState.CLOSED

    def test_min_samples_gate(self, clock):
        cb = breaker(clock, failure_threshold=3, min_samples=6)
        for _ in range(5):
            cb.record(False)
        assert cb.state is BreakerState.CLOSED
        cb.record(False)
        assert cb.state is BreakerState.OPEN

    def test_half_open_after_cooldown(self, clock):
        cb = breaker(clock)
        for _ in range(3):
            cb.record(False)
        clock.advance(5)
        assert cb.state is BreakerState.HALF_OPEN

    def test_half_open_limits_probes(self, clock):
        cb = breaker(clock, half_open_max=1)
        for _ in range(3):
            cb.record(False)
        clock.advance(5)
        assert cb.allow() is True
        assert cb.allow() is False

    def test_half_open_success_closes(self, clock):
        cb = breaker(clock)
        for _ in range(3):
            cb.record(False)
        clock.advance(5)
        cb.allow()
        cb.record(True)
        assert cb.state is BreakerState.CLOSED
        assert cb.allow() is True

    def test_half_open_failure_reopens_with_backoff(self, clock):
        cb = breaker(clock, open_s=5.0, backoff=2.0)
        for _ in range(3):
            cb.record(False)
        clock.advance(5)
        cb.allow()
        cb.record(False)
        assert cb.state is BreakerState.OPEN
        assert cb.current_open_s() == pytest.approx(10.0)

    def test_backoff_is_capped(self, clock):
        cb = breaker(clock, open_s=5.0, backoff=2.0, max_open_s=8.0)
        for _ in range(3):
            cb.record(False)
        for _ in range(3):
            clock.advance(cb.current_open_s())
            cb.allow()
            cb.record(False)
        assert cb.current_open_s() == pytest.approx(8.0)

    def test_window_pruning(self, clock):
        cb = breaker(clock, failure_threshold=2, window_s=5.0, min_samples=2)
        cb.record(False)
        clock.advance(6)
        cb.record(False)
        assert cb.state is BreakerState.CLOSED  # 第一笔失败已滑出窗口

    def test_failure_rate(self, clock):
        cb = breaker(clock)
        cb.record(True)
        cb.record(False)
        assert cb.failure_rate() == pytest.approx(0.5)

    def test_failure_rate_empty(self, clock):
        assert breaker(clock).failure_rate() == 0.0

    def test_reset(self, clock):
        cb = breaker(clock)
        for _ in range(3):
            cb.record(False)
        cb.reset()
        assert cb.state is BreakerState.CLOSED
        assert cb.allow() is True

    def test_snapshot(self, clock):
        cb = breaker(clock)
        for _ in range(3):
            cb.record(False)
        snap = cb.snapshot()
        assert snap.state is BreakerState.OPEN
        assert snap.failures == 3
        assert snap.opened_count == 1
        assert snap.as_dict()["server"] == "s1"

    def test_invalid_config(self, clock):
        with pytest.raises(ValueError):
            CircuitBreaker("s", failure_threshold=0, now_fn=clock)
        with pytest.raises(ValueError):
            CircuitBreaker("s", half_open_max=0, now_fn=clock)


class TestServerGuard:
    def test_acquire_release(self, clock):
        g = ServerGuard("s1", breaker=breaker(clock))
        g.acquire()
        assert g.inflight == 1
        g.release(True)
        assert g.inflight == 0
        assert g.calls == 1

    def test_concurrency_cap(self, clock):
        g = ServerGuard("s1", max_concurrency=1, breaker=breaker(clock))
        g.acquire()
        with pytest.raises(ServerBusy):
            g.acquire()
        g.release(True)
        g.acquire()
        assert g.inflight == 1

    def test_rejects_when_open(self, clock):
        g = ServerGuard("s1", breaker=breaker(clock))
        for _ in range(3):
            g.breaker.record(False)
        with pytest.raises(CircuitOpen) as ei:
            g.acquire()
        assert ei.value.detail["server"] == "s1"
        assert g.rejected_open == 1

    def test_slot_releases_on_exception(self, clock):
        g = ServerGuard("s1", breaker=breaker(clock))
        with pytest.raises(RuntimeError):
            with g.slot():
                raise RuntimeError("boom")
        assert g.inflight == 0
        assert g.failures == 1

    def test_slot_records_success(self, clock):
        g = ServerGuard("s1", breaker=breaker(clock))
        with g.slot():
            pass
        assert g.failures == 0

    def test_slot_does_not_leak_when_acquire_fails(self, clock):
        g = ServerGuard("s1", max_concurrency=0, breaker=breaker(clock))
        with pytest.raises(ServerBusy):
            with g.slot():
                pass
        assert g.inflight == 0

    def test_health(self, clock):
        g = ServerGuard("s1", max_concurrency=4, breaker=breaker(clock))
        with g.slot():
            pass
        h = g.health()
        assert h["server"] == "s1"
        assert h["state"] == "closed"
        assert h["calls"] == 1
        assert h["max_concurrency"] == 4
        assert h["inflight"] == 0
