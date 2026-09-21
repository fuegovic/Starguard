"""Per-member exclusion: what it keeps apart, and what it deliberately does not.

The registry replaced one process-wide mutex, so both halves are the
contract. Two components inside the same member at once is the bug it
exists for, and two components inside different members at once is the
reason it is not simply the old lock: the drain used to wait out a whole
star check whichever member its queued row was about.
"""

# Test names document the behaviour under test, and the registry's
# bookkeeping is the thing under test in two of them.
# pylint: disable=missing-function-docstring,protected-access

import asyncio

import pytest

from bot.memberlock import MemberLocks


async def record(locks, member_id, order, name, yields=3):
    """Hold ``member_id`` for a few event loop turns, noting both ends."""
    async with locks.hold(member_id):
        order.append(f"{name} in")
        for _ in range(yields):
            await asyncio.sleep(0)
        order.append(f"{name} out")


def test_two_callers_on_one_member_take_turns():
    locks = MemberLocks()
    order = []

    async def scenario():
        await asyncio.gather(
            record(locks, "1", order, "first"),
            record(locks, "1", order, "second"),
        )

    asyncio.run(scenario())

    # Nothing interleaves: the second caller's body starts only once the
    # first has left, which is the whole of the exclusion.
    assert order == ["first in", "first out", "second in", "second out"]


def test_two_callers_on_different_members_do_not_wait_for_each_other():
    # The half a single mutex got wrong. A queued webhook about one member
    # has no reason to wait for a sweep that is working through another.
    locks = MemberLocks()
    order = []

    async def scenario():
        await asyncio.gather(
            record(locks, "1", order, "first"),
            record(locks, "2", order, "second"),
        )

    asyncio.run(scenario())

    assert order == ["first in", "second in", "first out", "second out"]


def test_the_same_member_spelled_two_ways_is_one_mutex():
    # A Discord ID arrives as a string off a Mongo row and as a Snowflake
    # off an interaction. Two spellings of one member would be two mutexes
    # and no exclusion at all.
    locks = MemberLocks()
    order = []

    async def scenario():
        await asyncio.gather(
            record(locks, 1, order, "row"),
            record(locks, "1", order, "interaction"),
        )

    asyncio.run(scenario())

    assert order == ["row in", "row out", "interaction in", "interaction out"]


def test_a_mutex_outlives_its_holder_while_somebody_is_waiting_for_it():
    # The reason callers are counted in before they wait rather than after
    # they are let in. Dropping the entry when the holder leaves would hand
    # the waiter a mutex nobody else can find, and the next arrival would
    # create a second one and walk straight in.
    locks = MemberLocks()
    seen = {}

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def holder():
            async with locks.hold("1"):
                entered.set()
                await release.wait()
                # The waiter has queued behind this mutex by now, and it
                # is counted against the same entry rather than a new one.
                seen["holders"] = locks._entries["1"].holders

        async def waiter():
            await entered.wait()
            seen["while waiting"] = dict(locks._entries)
            release.set()
            async with locks.hold("1"):
                pass

        await asyncio.gather(holder(), waiter())

    asyncio.run(scenario())

    assert set(seen["while waiting"]) == {"1"}
    # Both of them are counted against the one entry while they overlap.
    assert seen["holders"] == 2
    # And the last one out takes it with them, so the registry does not
    # grow with the number of members the bot has ever touched.
    assert not locks._entries


def test_a_body_that_raises_still_releases_and_cleans_up():
    locks = MemberLocks()

    async def scenario():
        with pytest.raises(RuntimeError, match="member cache is confused"):
            async with locks.hold("1"):
                raise RuntimeError("member cache is confused")
        # The next caller is not locked out by the one that blew up.
        async with locks.hold("1"):
            pass

    asyncio.run(scenario())

    assert not locks._entries
