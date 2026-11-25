import pytest

from helpers import FakeClock

from mcp_fleet_gateway import AttemptLedger, Deadline, DeadlineExceeded


class TestDeadline:
    def test_fresh_deadline(self, clock):
        d = Deadline.of(1_000, clock)
        assert d.remaining_ms() == pytest.approx(1_000)
        assert d.expired is False

    def test_counts_down(self, clock):
        d = Deadline.of(1_000, clock)
        clock.advance(0.25)
        assert d.remaining_ms() == pytest.approx(750)

    def test_expires(self, clock):
        d = Deadline.of(100, clock)
        clock.advance(0.2)
        assert d.expired is True
        assert d.remaining_ms() == 0

    def test_check_raises_when_expired(self, clock):
        d = Deadline.of(10, clock)
        clock.advance(1)
        with pytest.raises(DeadlineExceeded) as ei:
            d.check("调用工具")
        assert "调用工具" in str(ei.value)
        assert ei.value.detail["suggestion"]

    def test_child_is_capped_by_parent(self, clock):
        parent = Deadline.of(800, clock)
        assert parent.child(5_000).remaining_ms() == pytest.approx(800)

    def test_child_uses_requested_when_smaller(self, clock):
        parent = Deadline.of(800, clock)
        assert parent.child(200).remaining_ms() == pytest.approx(200)

    def test_child_of_exhausted_parent(self, clock):
        parent = Deadline.of(10, clock)
        clock.advance(1)
        assert parent.child(500).remaining_ms() == 0

    def test_negative_budget_clamped(self, clock):
        assert Deadline.of(-5, clock).budget_ms == 0

    def test_elapsed(self, clock):
        d = Deadline.of(1_000, clock)
        clock.advance(0.3)
        assert d.elapsed_ms() == pytest.approx(300)


class TestAttemptLedger:
    def test_counts_attempts(self, clock):
        led = AttemptLedger(Deadline.of(1_000, clock), max_attempts=2)
        assert led.next_attempt() is True
        assert led.next_attempt() is True
        assert led.next_attempt() is False

    def test_stops_when_deadline_gone(self, clock):
        d = Deadline.of(10, clock)
        led = AttemptLedger(d, max_attempts=5)
        clock.advance(1)
        assert led.next_attempt() is False

    def test_note_failure_respects_budget(self, clock):
        led = AttemptLedger(Deadline.of(100, clock), max_attempts=5)
        led.next_attempt()
        assert led.note_failure(backoff_ms=500) is False

    def test_note_failure_allows_retry(self, clock):
        led = AttemptLedger(Deadline.of(5_000, clock), max_attempts=5)
        led.next_attempt()
        assert led.note_failure(backoff_ms=50) is True

    def test_note_failure_respects_max_attempts(self, clock):
        led = AttemptLedger(Deadline.of(5_000, clock), max_attempts=1)
        led.next_attempt()
        assert led.note_failure(backoff_ms=10) is False

    def test_minimum_one_attempt(self, clock):
        led = AttemptLedger(Deadline.of(1_000, clock), max_attempts=0)
        assert led.max_attempts == 1
