"""Tests for the un-star check's retry loop, its backoff, and what ends a cycle.

Split from test_starcheck.py, which carries the sweep itself and the fakes
both files run against. The seam is the one that file's own docstring named:
the concurrency guard on one side, the backoff on the other. It is a split
rather than a suppression because the alternative was to keep deleting the
comments explaining why each race test exists in order to stay under
pylint's module limit.

The fakes are imported from test_starcheck rather than duplicated, which is
how test_server_authorize already borrows from test_server_routes and
test_storage.
"""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument,protected-access

import asyncio

import pytest

from common.github_api import GitHubError
from common.storage_errors import StorageError, StorageUnavailableError
from tests.test_starcheck import build, build_with_one_overtaken, link, listing


def test_the_error_backoff_grows_and_is_capped(monkeypatch):
    checker, _, _, _ = build(monkeypatch, [], set())
    monkeypatch.setattr("bot.starcheck.random.uniform", lambda low, high: 1.0)

    delays = []
    for failures in range(1, 12):
        checker._consecutive_failures = failures
        delays.append(checker._error_delay())

    # A flat 60 seconds met a GitHub outage with a request a minute for as
    # long as it lasted.
    assert delays[0] == 60
    assert delays[1] == 120
    assert delays == sorted(delays)
    assert max(delays) == 1800


def test_the_backoff_is_jittered(monkeypatch):
    checker, _, _, _ = build(monkeypatch, [], set())
    checker._consecutive_failures = 1
    delays = {round(checker._error_delay(), 6) for _ in range(50)}
    assert len(delays) > 1
    assert all(45 <= delay <= 75 for delay in delays)


class LoopStoppedError(Exception):
    """Ends run_forever from inside the sleep it would otherwise take."""


def drive_loop(monkeypatch, checker, cycles):
    """Run ``run_forever`` for ``cycles`` iterations. Returns the delays.

    The loop only ever awaits ``asyncio.sleep`` between cycles, so replacing
    it is both how the delays are observed and how the loop is stopped,
    without any real waiting.
    """
    delays = []

    async def stop_after(delay):
        delays.append(delay)
        if len(delays) >= cycles:
            raise LoopStoppedError

    monkeypatch.setattr(asyncio, "sleep", stop_after)

    async def scenario():
        with pytest.raises(LoopStoppedError):
            await checker.run_forever()

    asyncio.run(scenario())
    return delays


def exploding_fetch(owner, repo, token=None, cache=None):
    """A GitHub that is having a bad day."""
    raise GitHubError("GitHub API returned HTTP 503.")


def test_the_completion_time_is_only_set_once_a_cycle_finishes(monkeypatch):
    # The health endpoint reports the age of this value, so a cycle that
    # never ran must not look like one that just did.
    checker, _, _, _ = build(monkeypatch, [link(1, "Kept")], {"kept"})
    assert checker.last_completed is None

    asyncio.run(checker.run_once())
    assert isinstance(checker.last_completed, float)


def test_a_skipped_cycle_does_not_count_as_a_completed_one(monkeypatch):
    checker, _, _, _ = build(monkeypatch, [], set())
    checker._users = None
    asyncio.run(checker.run_once())
    assert checker.last_completed is None


def test_the_loop_waits_the_configured_interval_between_cycles(monkeypatch):
    checker, members, _, _ = build(monkeypatch, [link(1, "Gone")], set(), check_delay=900)

    assert drive_loop(monkeypatch, checker, cycles=1) == [900]
    assert members["1"].removals == 1
    assert checker._consecutive_failures == 0


def test_a_failing_cycle_is_logged_and_retried_rather_than_killing_the_loop(monkeypatch, caplog):
    # An unhandled exception here used to kill the task silently, and
    # automatic checks never ran again until the bot was restarted.
    checker, _, _, _ = build(monkeypatch, [], set(), fetch=exploding_fetch)

    with caplog.at_level("ERROR", logger="starguard.bot"):
        delays = drive_loop(monkeypatch, checker, cycles=3)

    assert checker._consecutive_failures == 3
    # Backoff, not a flat retry: each wait is longer than the last.
    assert delays == sorted(delays)
    assert 45 <= delays[0] <= 75
    assert "Automatic star check failed" in caplog.text
    assert "HTTP 503" in caplog.text


def test_the_backoff_is_forgotten_once_a_cycle_succeeds(monkeypatch):
    responses = [exploding_fetch, None]

    def flaky(owner, repo, token=None, cache=None):
        behaviour = responses.pop(0) if responses else None
        if behaviour is not None:
            behaviour(owner, repo)
        return listing()

    checker, _, _, _ = build(monkeypatch, [], set(), fetch=flaky, check_delay=1200)

    delays = drive_loop(monkeypatch, checker, cycles=2)
    assert 45 <= delays[0] <= 75
    assert delays[1] == 1200
    assert checker._consecutive_failures == 0


def test_a_database_outage_mid_walk_abandons_the_cycle(monkeypatch, caplog):
    # The per-entry guard exists so one unusable row cannot strand the rest
    # of the walk, but an unreachable database is every remaining row. Swallowed
    # per entry, the cycle reports success, stamps _last_completed and leaves
    # the health socket saying the loop is fresh, having checked nobody.
    checker, _, _, _ = build(monkeypatch, [link(1, "Gone"), link(2, "Also")], set())

    def gone(collection, discord_id):
        raise StorageUnavailableError("no replica set members available")

    monkeypatch.setattr("bot.starcheck.find_link", gone)

    with caplog.at_level("ERROR", logger="starguard.bot"), pytest.raises(StorageUnavailableError):
        asyncio.run(checker.run_once())

    # Not stamped, so the health check sees a loop that has not completed
    # rather than one that completed having done nothing.
    assert checker.last_completed is None
    assert "abandoning this cycle" in caplog.text


def test_one_unusable_row_still_does_not_strand_the_walk(monkeypatch, caplog):
    # The other half, so the outage case cannot be satisfied by simply
    # letting everything out again: a StorageError the database answered
    # with is still this row's problem and the walk carries on.
    checker, members, _, users = build(monkeypatch, [link(1, "Gone"), link(2, "Also")], set())
    real = users.find_one
    calls = []

    def refuse_the_first(query, projection=None):
        calls.append(query)
        if len(calls) == 1:
            raise StorageError("index not found")
        return real(query, projection)

    users.find_one = refuse_the_first

    with caplog.at_level("ERROR", logger="starguard.bot"):
        removed = asyncio.run(checker.run_once())

    # The second member was still reached and still lost the role.
    assert removed == ["user2"]
    assert members["2"].removals == 1
    assert checker.last_completed is not None


def test_an_outage_during_the_bookkeeping_write_abandons_the_cycle(monkeypatch, caplog):
    # The other door into the same problem, and the one the first test
    # cannot reach: StorageUnavailableError is a StorageError, so the catch
    # around set_starred consumed it before the walk's guard could see it.
    # The cycle then went on removing roles it could not record and
    # announcing farewells for them.
    checker, members, channel, _ = build(monkeypatch, [link(1, "Gone"), link(2, "Also")], set())

    def gone(*args, **kwargs):
        raise StorageUnavailableError("connection pool paused")

    monkeypatch.setattr("bot.starcheck.set_starred", gone)

    with caplog.at_level("ERROR", logger="starguard.bot"), pytest.raises(StorageUnavailableError):
        asyncio.run(checker.run_once())

    assert checker.last_completed is None
    # No farewell for a removal the database never recorded.
    assert not channel.sent
    # And the second member was never reached, because the walk stopped.
    assert members["2"].removals == 0


def test_a_write_the_database_refused_is_still_only_that_row(monkeypatch):
    # The pair, so the fix above cannot be satisfied by letting every
    # StorageError out: a failure the database answered with still counts
    # as the removal this cycle really made.
    checker, members, _, _ = build(monkeypatch, [link(1, "Gone")], set())

    def refuse(*args, **kwargs):
        raise StorageError("not authorized on starguard")

    monkeypatch.setattr("bot.starcheck.set_starred", refuse)

    assert asyncio.run(checker.run_once()) == ["user1"]
    assert members["1"].removals == 1
    assert checker.last_completed is not None


def test_an_outage_while_queuing_the_reconciliation_abandons_the_cycle(monkeypatch, caplog):
    # The third door into the same problem. This write is the last thing a
    # refused cycle does, and its catch is the other one that named
    # StorageError and so swallowed an outage. Swallowed here, the cycle
    # goes on to the next member with the database gone.
    checker, members, _, _, _ = build_with_one_overtaken(
        monkeypatch, queue_error=StorageUnavailableError("no primary available")
    )

    with caplog.at_level("ERROR", logger="starguard.bot"), pytest.raises(StorageUnavailableError):
        asyncio.run(checker.run_once())

    assert checker.last_completed is None
    # The first member's removal still happened; it is the cycle's report
    # of itself that is refused, not the work it had already done.
    assert members["1"].removals == 1
