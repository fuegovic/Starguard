"""Tests for the in-process rate limiter."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import threading

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


class LockWatchingClock:
    """A clock that records whether the limiter held its lock when it read."""

    def __init__(self):
        self.limiter = None
        self.held = []

    def __call__(self):
        self.held.append(self.limiter._lock.locked())  # pylint: disable=protected-access
        return 1000.0


class GateClock:
    """Hands the first caller an early time and holds it there.

    The second caller gets a later one immediately, which is the interleaving
    that puts an older timestamp behind a newer one in the deque.
    """

    def __init__(self):
        self.reached = threading.Event()
        self.release = threading.Event()
        self.first = True

    def __call__(self):
        if not self.first:
            return 1005.0
        self.first = False
        self.reached.set()
        # Bounded, because with the clock read under the lock the second
        # thread never gets this far and nothing would release it.
        self.release.wait(timeout=0.5)
        return 1000.0


def test_the_clock_is_read_under_the_lock():
    clock = LockWatchingClock()
    clock.limiter = RateLimiter(5, 60, clock=clock)
    clock.limiter.hit("a")
    assert clock.held == [True]


def test_two_threads_cannot_append_their_timestamps_out_of_order():
    # Reading the clock before taking the lock lets one thread take an
    # early timestamp, lose the processor, and append it after another
    # thread has appended a later one. Both the expiry loop and retry_after
    # read the deque as oldest first, so out of order there means hits that
    # never expire and a Retry-After computed from the wrong entry.
    clock = GateClock()
    limiter = RateLimiter(5, 60, clock=clock)

    slow = threading.Thread(target=limiter.hit, args=("a",))
    slow.start()
    assert clock.reached.wait(timeout=5)

    fast = threading.Thread(target=limiter.hit, args=("a",))
    fast.start()
    # Under the fix this cannot finish: it is waiting for the lock the slow
    # thread is holding while it reads its own clock.
    fast.join(timeout=0.25)
    clock.release.set()

    slow.join(timeout=5)
    fast.join(timeout=5)
    recorded = list(limiter._hits["a"])  # pylint: disable=protected-access
    assert recorded == sorted(recorded)


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
