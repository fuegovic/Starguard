"""Tests for the in-process rate limiter."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import pytest

from common.ratelimit import RateLimiter


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_requests_under_the_limit_are_allowed():
    limiter = RateLimiter(3, 60, clock=FakeClock())
    assert [limiter.hit("a").allowed for _ in range(3)] == [True, True, True]


def test_the_limit_is_enforced():
    limiter = RateLimiter(2, 60, clock=FakeClock())
    limiter.hit("a")
    limiter.hit("a")
    decision = limiter.hit("a")
    assert decision.allowed is False
    assert decision.remaining == 0


def test_keys_do_not_share_a_budget():
    limiter = RateLimiter(1, 60, clock=FakeClock())
    assert limiter.hit("a").allowed is True
    assert limiter.hit("b").allowed is True
    assert limiter.hit("a").allowed is False


def test_the_window_slides_rather_than_resetting():
    # A fixed window lets twice the limit through across a boundary: three
    # requests at t=59 and three more at t=61 would both be inside their own
    # window. Here the early ones only expire one at a time.
    clock = FakeClock()
    limiter = RateLimiter(3, 60, clock=clock)
    for _ in range(3):
        limiter.hit("a")
        clock.advance(10)

    # t=59: the oldest hit is 59 seconds old, so nothing has expired yet.
    clock.advance(29)
    assert limiter.hit("a").allowed is False

    # t=61: only the first of the three has aged out, so exactly one more
    # request gets through rather than a whole fresh window's worth.
    clock.advance(2)
    assert limiter.hit("a").allowed is True
    assert limiter.hit("a").allowed is False


def test_retry_after_says_when_the_oldest_hit_expires():
    clock = FakeClock()
    limiter = RateLimiter(1, 60, clock=clock)
    limiter.hit("a")
    clock.advance(20)
    assert limiter.hit("a").retry_after == 40


def test_retry_after_is_never_zero():
    clock = FakeClock()
    limiter = RateLimiter(1, 60, clock=clock)
    limiter.hit("a")
    clock.advance(59.9)
    assert limiter.hit("a").retry_after >= 1


def test_the_table_does_not_grow_without_limit():
    # An unbounded dict keyed by client address is a way to exhaust memory.
    clock = FakeClock()
    limiter = RateLimiter(5, 60, max_keys=10, clock=clock)
    for index in range(200):
        limiter.hit(f"client-{index}")
        clock.advance(1)
    assert len(limiter._hits) <= 11  # pylint: disable=protected-access


def test_expired_keys_are_reclaimed_before_anything_is_evicted():
    clock = FakeClock()
    limiter = RateLimiter(5, 10, max_keys=3, clock=clock)
    for index in range(4):
        limiter.hit(f"old-{index}")

    clock.advance(30)
    limiter.hit("fresh")
    assert set(limiter._hits) == {"fresh"}  # pylint: disable=protected-access


@pytest.mark.parametrize("limit,window", [(0, 60), (-1, 60), (1, 0), (1, -5)])
def test_a_nonsensical_configuration_is_refused(limit, window):
    with pytest.raises(ValueError):
        RateLimiter(limit, window)


def test_the_configured_budget_is_readable_and_counted_down():
    limiter = RateLimiter(3, 60, clock=FakeClock())
    assert limiter.limit == 3
    assert [limiter.hit("a").remaining for _ in range(3)] == [2, 1, 0]
