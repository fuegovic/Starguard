"""The role-sync drain: what it does with a queued row, and what it refuses to.

The webhook lands in the server process and the role lives in this one, so
the database is the whole channel between them. These tests drive that
channel from the bot's end: a row with a star state and a raised flag goes
in, and Discord plus the lowered flag come out. The cases that matter most
are the ones where nothing should be written, because a flag lowered over
work that did not happen is a role nobody takes back at all.
"""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument,protected-access

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from interactions.client.errors import Forbidden
from pymongo.errors import PyMongoError

from bot.bot import create_client
from bot.memberlock import MemberLocks
from bot.rolesync import DRAIN_ERROR_BACKOFF_MAX_SECONDS, DrainResult, RoleSyncDrainer
from bot.starcheck import StarChecker
from common.storage import STAR_SOURCE_WEBHOOK, record_star_event
from tests.test_roles import discord_error
from tests.test_starcheck import (
    GUILD_ID,
    ROLE_ID,
    FakeChannel,
    FakeClient,
    FakeGuild,
    FakeMember,
    FakeUsers,
    drive_loop,
    link,
    listing,
    make_config,
)


class RecordingUsers(FakeUsers):
    """A collection that remembers what it was asked for.

    The queue is backed by a partial index over ``role_sync_pending: true``,
    so what the drain asks for is as much part of the contract as what it
    writes: a poll that scanned every verified member would give the index
    away, and a poll that wrote on an idle tick would give away the reason
    the index is partial.
    """

    def __init__(self, documents):
        super().__init__(documents)
        self.queries = []
        self.writes = []

    def find(self, query=None, projection=None):
        self.queries.append(query)
        return super().find(query, projection)

    def update_one(self, query, update, upsert=False):
        self.writes.append((query, update))
        return super().update_one(query, update, upsert)


class RefusingMember(FakeMember):
    """A member whose role the bot is not allowed to touch, either way."""

    async def add_role(self, role_id, reason=None):
        raise discord_error(Forbidden)

    async def remove_role(self, role_id, reason=None):
        raise discord_error(Forbidden)


class ExplodingGuild(FakeGuild):
    """A guild whose member cache fails for one Discord ID."""

    def __init__(self, members, broken):
        super().__init__(members)
        self._broken = str(broken)

    def get_member(self, discord_id):
        if str(discord_id) == self._broken:
            raise RuntimeError("member cache is confused")
        return super().get_member(discord_id)


def pending(discord_id, starred, username="Someone"):
    """A link the server has queued for the bot to act on.

    It carries an ``_id`` because the pending cursor is the one that keeps
    it. For a row with no usable ``discord_id`` that is the only identity
    the drain has to clear the flag by, so a fake without one would let
    the clear be deleted with the suite still green.
    """
    return {
        "_id": f"oid-{discord_id}",
        **link(discord_id, username, starred=starred),
        "role_sync_pending": True,
        "star_source": "webhook",
    }


# Every shape a queued row with no usable Discord ID comes in.
#
# ``null`` is the one production actually holds. From b715c73 (October
# 2023) until the rebuild, /login read the Discord ID from an unvalidated
# query parameter and stored whatever came back, so an unauthenticated GET
# with no id, followed by OAuth, wrote ``discord_id: None``. That is nearly
# three years of releases and nothing ever backfilled them.
#
# ``missing`` was only producible for about a day in the same month, and
# ``empty`` is the one the original bug hid behind: it round-trips through
# str() and so matched the Discord-keyed clear that the other two do not.
NO_DISCORD_ID = ("null", "missing", "empty")


def unusable(spelling, discord_id=1, starred=False):
    """A queued row carrying no Discord ID, in one of the three shapes."""
    document = pending(discord_id, starred)
    if spelling == "missing":
        del document["discord_id"]
    else:
        document["discord_id"] = None if spelling == "null" else ""
    return document


def build(documents, holds=(), members=None, guild=None, member_locks=None, **config_overrides):
    """Wire a drainer up to fakes. Returns (drainer, members, channel, users).

    ``holds`` names the Discord IDs that already have the role, which is
    the other half of every case here: the drain's job is the difference
    between what the row says and what Discord says.

    ``member_locks`` is the registry the drain takes a member's mutex from.
    A private one is right for a drain on its own; the tests that run a
    drain against a sweep or a claim pass the registry they share, because
    three private registries would be three sets of mutexes and no
    exclusion at all.
    """
    if members is None:
        held = {str(one) for one in holds}
        members = {
            document["discord_id"]: FakeMember(
                document["discord_id"],
                roles=(ROLE_ID,) if document["discord_id"] in held else (),
            )
            for document in documents
            if document.get("discord_id")
        }

    channel = FakeChannel()
    users = RecordingUsers(documents)
    client = FakeClient(FakeGuild(members) if guild is None else guild, channel)
    drainer = RoleSyncDrainer(
        client,
        make_config(**config_overrides),
        users,
        MemberLocks() if member_locks is None else member_locks,
    )
    return drainer, members, channel, users


def test_an_idle_poll_reads_only_the_queue_and_writes_nothing(caplog):
    # The whole point of the partial index. Ordinary links are not waiting
    # for anything, so a poll must not read them, must not write to them,
    # and must not say anything about having found nothing.
    documents = [link(1, "Kept"), link(2, "Gone", starred=False)]
    drainer, members, channel, users = build(documents, holds=[1, 2])

    with caplog.at_level("INFO", logger="starguard.bot"):
        assert asyncio.run(drainer.drain_once()) == DrainResult()

    assert users.queries == [{"role_sync_pending": True}]
    assert not users.writes
    assert not channel.sent
    assert all(member.additions == 0 for member in members.values())
    assert all(member.removals == 0 for member in members.values())
    assert "Role sync drain complete" not in caplog.text


def test_a_queued_star_grants_the_role(caplog):
    drainer, members, channel, users = build([pending(1, True)])

    with caplog.at_level("INFO", logger="starguard.bot"):
        result = asyncio.run(drainer.drain_once())

    assert result == DrainResult(examined=1, granted=1)
    assert members["1"].additions == 1
    assert members["1"].roles == {ROLE_ID}
    assert users.documents[0]["role_sync_pending"] is False
    # A grant is not announced. The thank-you belongs to the claim button,
    # where somebody is actually waiting for an answer.
    assert not channel.sent
    assert "granted=1" in caplog.text


def test_a_queued_un_star_removes_the_role_and_announces_it():
    drainer, members, channel, users = build([pending(1, False)], holds=[1])

    result = asyncio.run(drainer.drain_once())

    assert result == DrainResult(examined=1, removed=1)
    assert members["1"].removals == 1
    assert members["1"].roles == set()
    assert users.documents[0]["role_sync_pending"] is False
    # The same farewell the sweep posts, because it is the same event.
    assert len(channel.sent) == 1
    assert "<@1>" in channel.sent[0]


@pytest.mark.parametrize("starred,holds", [(True, [1]), (False, [])])
def test_a_row_discord_already_agrees_with_is_only_cleared(starred, holds):
    # A redelivered webhook, or a sweep that reached the change first. The
    # flag still has to come down or the row is read again forever.
    drainer, members, channel, users = build([pending(1, starred)], holds=holds)

    assert asyncio.run(drainer.drain_once()) == DrainResult(examined=1)
    assert members["1"].additions == 0
    assert members["1"].removals == 0
    assert users.documents[0]["role_sync_pending"] is False
    assert not channel.sent


def test_a_member_who_left_the_guild_is_taken_off_the_queue(caplog):
    # Matches what the sweep does with a departed member: there is no role
    # to move, so the row is finished rather than left in the index to be
    # looked up again every thirty seconds for as long as the bot runs.
    drainer, members, channel, users = build([pending(1, False)], holds=[1])
    members.clear()

    with caplog.at_level("INFO", logger="starguard.bot"):
        assert asyncio.run(drainer.drain_once()) == DrainResult(examined=1)

    assert users.documents[0]["role_sync_pending"] is False
    assert not channel.sent
    assert "no longer in the guild" in caplog.text


@pytest.mark.parametrize("starred,holds", [(True, []), (False, [1])])
def test_a_refused_role_change_leaves_the_flag_raised(caplog, starred, holds):
    # The bug this test exists for: a flag cleared over a Discord call that
    # failed is a role nobody puts right. The sweep repairs deliveries
    # that never arrived, not races, so nothing else comes back for this.
    held = (ROLE_ID,) if holds else ()
    members = {"1": RefusingMember("1", roles=held)}
    drainer, _, channel, users = build([pending(1, starred)], members=members)

    with caplog.at_level("WARNING", logger="starguard.bot"):
        result = asyncio.run(drainer.drain_once())

    assert result == DrainResult(examined=1, failed=1)
    assert users.documents[0]["role_sync_pending"] is True
    assert not users.writes
    assert not channel.sent
    assert "Could not" in caplog.text


def test_a_webhook_landing_mid_call_is_not_unqueued_by_the_clear():
    # The lost update behind the third argument to clear_role_sync_pending,
    # and the sibling of the test above: there the Discord call failed, here
    # it succeeded but the world moved while it was in flight. The drain
    # grants the role for a row that says starred, and before the call
    # returns a webhook records an un-star and raises the flag again. An
    # unconditional clear would lower the flag that webhook had just put up,
    # and the member would keep a role they should have lost, with no
    # later pass coming back for it.
    documents = [pending(1, True)]
    users = RecordingUsers(documents)

    class InterruptedMember(FakeMember):
        """A member whose row a webhook rewrites while the call is in flight."""

        async def add_role(self, role_id, reason=None):
            await super().add_role(role_id, reason)
            # What record_star_event writes in that window. The flag was
            # already up, so the row looks unchanged from the outside.
            users.documents[0].update(starred_repo=False, role_sync_pending=True)

    members = {"1": InterruptedMember("1", roles=())}
    channel = FakeChannel()
    client = FakeClient(FakeGuild(members), channel)
    drainer = RoleSyncDrainer(client, make_config(), users, MemberLocks())

    async def two_polls():
        return await drainer.drain_once(), await drainer.drain_once()

    first, second = asyncio.run(two_polls())

    # The clear is filtered on the state the drain acted on, so it matches
    # nothing and the newer un-star is still queued.
    assert first == DrainResult(examined=1, granted=1)
    # The next poll acts on that newer value, which is the whole point of
    # leaving the flag up rather than lowering it and hoping.
    assert second == DrainResult(examined=1, removed=1)
    assert members["1"].roles == set()
    assert users.documents[0]["role_sync_pending"] is False
    assert len(channel.sent) == 1


def test_a_re_star_while_the_role_is_being_taken_gets_no_public_farewell():
    # The sibling of the case above on the other side of the row. The
    # removal succeeds, and before the clear runs a webhook records the
    # re-star and raises the flag again, so the conditional clear matches
    # nothing and the newer grant is still queued. That much already
    # worked. What did not is that the farewell went out anyway: the next
    # poll hands the role straight back, and "sorry to see you go" stays
    # in the channel for somebody who stars the repository.
    documents = [pending(1, False)]
    users = RecordingUsers(documents)

    class InterruptedMember(FakeMember):
        """A member whose row a webhook rewrites while the call is in flight."""

        async def remove_role(self, role_id, reason=None):
            await super().remove_role(role_id, reason)
            # What record_star_event writes in that window: the star is
            # back, and the flag that was already up stays up.
            users.documents[0].update(starred_repo=True, role_sync_pending=True)

    members = {"1": InterruptedMember("1", roles=(ROLE_ID,))}
    channel = FakeChannel()
    client = FakeClient(FakeGuild(members), channel)
    drainer = RoleSyncDrainer(client, make_config(), users, MemberLocks())

    async def two_polls():
        return await drainer.drain_once(), await drainer.drain_once()

    first, second = asyncio.run(two_polls())

    # The role really was taken, so the pass reports it. What it does not
    # do is say so in public, because it is about to be given back.
    assert first == DrainResult(examined=1, removed=1)
    assert not channel.sent
    # The flag stayed up, so the next poll acts on the newer value.
    assert second == DrainResult(examined=1, granted=1)
    assert members["1"].roles == {ROLE_ID}
    assert users.documents[0]["role_sync_pending"] is False
    # And still nothing was announced, about either half of it.
    assert not channel.sent


def test_one_unusable_row_does_not_strand_the_rest_of_the_queue(caplog):
    documents = [pending(1, True), pending(2, True, username="Other")]
    members = {"1": FakeMember("1", roles=()), "2": FakeMember("2", roles=())}
    drainer, _, _, users = build(documents, members=members, guild=ExplodingGuild(members, 1))

    with caplog.at_level("ERROR", logger="starguard.bot"):
        result = asyncio.run(drainer.drain_once())

    assert result == DrainResult(examined=2, granted=1, failed=1)
    assert members["2"].roles == {ROLE_ID}
    # The row that blew up keeps its flag and is tried again next time.
    assert users.documents[0]["role_sync_pending"] is True
    assert users.documents[1]["role_sync_pending"] is False
    assert "member cache is confused" in caplog.text


@pytest.mark.parametrize("spelling", NO_DISCORD_ID)
def test_a_row_with_no_discord_id_is_cleared_by_its_mongo_id(caplog, spelling):
    # There is nobody to move a role for, and the clear this used to issue
    # was keyed on the very field the row does not have: a null or absent
    # `discord_id` was searched for as the literal string "None", which
    # matched nothing, so the flag stayed raised and the row came back on
    # the next poll, and the next, with the same line claiming it had been
    # dropped every thirty seconds for as long as the bot ran. The
    # empty-string spelling happened to match, which is why a passing test
    # did not catch it. All three take the by-id path now.
    document = unusable(spelling)
    drainer, _, channel, users = build([document])

    async def two_polls():
        return [await drainer.drain_once() for _ in range(2)]

    with caplog.at_level("WARNING", logger="starguard.bot"):
        results = asyncio.run(two_polls())

    # The flag really comes down, so the second poll reads an empty queue
    # rather than the same row again.
    assert users.documents[0]["role_sync_pending"] is False
    assert results == [DrainResult(examined=1, unusable=1), DrainResult()]
    # Cleared by the Mongo _id, unconditionally. Naming the star state
    # here would be a second way to match nothing, not a safeguard.
    assert users.writes == [({"_id": "oid-1"}, {"$set": {"role_sync_pending": False}})]
    # Counted apart from a failure, so a row nothing can act on does not
    # put the drain into the backoff a Discord refusal earns.
    assert drainer._consecutive_failures == 0
    assert not channel.sent
    # Said out loud, once, with enough to find the row again by hand: this
    # deletes queued work, and a silent delete is the worse failure.
    assert caplog.text.count("no Discord ID") == 1
    assert "clearing it" in caplog.text
    assert "oid-1" in caplog.text


@pytest.mark.parametrize("spelling", NO_DISCORD_ID)
def test_an_unusable_row_does_not_stop_the_rest_of_the_queue(caplog, spelling):
    # The counterpart to the test above: clearing the row is only the right
    # answer if everything behind it still moves.
    row = unusable(spelling, starred=True)
    drainer, members, _, users = build([row, pending(2, True, username="Other")])

    with caplog.at_level("WARNING", logger="starguard.bot"):
        result = asyncio.run(drainer.drain_once())

    assert result == DrainResult(examined=2, granted=1, unusable=1)
    assert members["2"].roles == {ROLE_ID}
    assert users.documents[0]["role_sync_pending"] is False
    assert users.documents[1]["role_sync_pending"] is False
    # Pinned to the by-id filter, not just to the flag coming down. The
    # empty-string spelling is the one the Discord-keyed clear reaches on
    # its own, so a test that only checks the queue emptied would pass for
    # it with the fix removed, and that parameter would be decoration.
    assert ({"_id": "oid-1"}, {"$set": {"role_sync_pending": False}}) in users.writes


@pytest.mark.parametrize("spelling", NO_DISCORD_ID)
def test_a_failed_clear_of_an_unusable_row_is_logged_not_raised(caplog, spelling):
    # The same shrug as an ordinary clear that fails. Nothing was going to
    # happen to this row in any case, so the cost of the failure is the
    # same line again on the next poll, not a traceback out of the drain.
    drainer, _, _, users = build([unusable(spelling, starred=True)])

    def refuse(query, update, upsert=False):
        raise PyMongoError("no primary available")

    users.update_one = refuse

    with caplog.at_level("ERROR", logger="starguard.bot"):
        assert asyncio.run(drainer.drain_once()) == DrainResult(examined=1, unusable=1)

    assert "Could not clear the unusable pending role sync" in caplog.text
    assert "no primary available" in caplog.text


def test_a_clear_that_fails_does_not_undo_the_role_change(caplog):
    drainer, members, _, users = build([pending(1, True)])

    def refuse(query, update, upsert=False):
        raise PyMongoError("no primary available")

    users.update_one = refuse

    with caplog.at_level("ERROR", logger="starguard.bot"):
        result = asyncio.run(drainer.drain_once())

    # The role is right and only the bookkeeping failed, so the next poll
    # finds Discord already agrees and clears the flag then.
    assert result == DrainResult(examined=1, granted=1)
    assert members["1"].roles == {ROLE_ID}
    assert "Could not clear the pending role sync" in caplog.text


def test_the_whole_queue_is_drained_across_batches(monkeypatch):
    monkeypatch.setattr("bot.rolesync.PENDING_BATCH_SIZE", 2)
    documents = [pending(index, True, username=f"user{index}") for index in range(1, 8)]
    drainer, members, _, users = build(documents)

    assert asyncio.run(drainer.drain_once()) == DrainResult(examined=7, granted=7)
    assert all(member.roles == {ROLE_ID} for member in members.values())
    assert all(document["role_sync_pending"] is False for document in users.documents)


def test_no_database_connection_is_skipped_rather_than_crashing(caplog):
    drainer, _, _, _ = build([])
    drainer._users = None

    with caplog.at_level("WARNING", logger="starguard.bot"):
        assert asyncio.run(drainer.drain_once()) == DrainResult()

    assert "no database connection" in caplog.text
    # And it is not progress. On a deployment with AUTOMATIC_CHECK=false
    # this loop is the only one reconciling anything, so counting a pass
    # that never reached the database would be the health endpoint
    # answering 200 about a bot that does nothing. A connection made at
    # startup is never remade, so this state lasts until a restart.
    assert drainer.last_completed is None


def test_a_guild_missing_from_the_cache_is_skipped():
    drainer, members, _, users = build([pending(1, True)], guild_id=GUILD_ID + 1)

    assert asyncio.run(drainer.drain_once()) == DrainResult()
    assert members["1"].additions == 0
    assert not users.writes
    # Nor is this, for the same reason: nothing was reconciled.
    assert drainer.last_completed is None


def test_a_pass_that_walked_the_queue_is_recorded_as_progress():
    # The other side of the two above, and what the health endpoint reads
    # off this loop. An empty queue counts: reading the partial index and
    # finding nothing waiting is the drain working, not the drain stuck.
    drainer, _, _, _ = build([])
    assert drainer.last_completed is None

    asyncio.run(drainer.drain_once())
    idle = drainer.last_completed
    assert idle is not None

    drainer, members, _, _ = build([pending(1, True)])
    asyncio.run(drainer.drain_once())
    assert members["1"].roles == {ROLE_ID}
    assert drainer.last_completed is not None


def test_the_completion_time_is_only_set_once_a_pass_has_walked_the_queue():
    # The health endpoint reports the age of this value, so a pass that
    # never reconciled anything must not look like one that just did.
    drainer, _, _, _ = build([pending(1, True)])
    assert drainer.last_completed is None

    asyncio.run(drainer.drain_once())
    assert isinstance(drainer.last_completed, float)


@pytest.mark.parametrize("skip", ["no database", "no guild"])
def test_a_skipped_pass_does_not_count_as_a_completed_one(skip):
    # Both early returns do no reconciling at all. Recording them as a
    # completed pass is what let a drain-only deployment report itself
    # healthy while moving no roles: the endpoint would see a fresh
    # timestamp every thirty seconds for a loop that was doing nothing.
    guild_id = GUILD_ID + 1 if skip == "no guild" else GUILD_ID
    drainer, _, _, _ = build([pending(1, True)], guild_id=guild_id)
    if skip == "no database":
        drainer._users = None

    assert asyncio.run(drainer.drain_once()) == DrainResult()
    assert drainer.last_completed is None


def test_the_loop_waits_the_configured_interval_between_drains(monkeypatch):
    drainer, members, _, _ = build([pending(1, True)], role_sync_interval=45)

    assert drive_loop(monkeypatch, drainer, cycles=1) == [45]
    assert members["1"].roles == {ROLE_ID}
    assert drainer._consecutive_failures == 0


def test_a_failing_drain_is_logged_and_retried_rather_than_killing_the_loop(monkeypatch, caplog):
    # An unhandled exception here would kill the task silently, and roles
    # would stop moving while the operator watched the webhook being
    # delivered successfully.
    drainer, _, _, users = build([pending(1, True)], role_sync_interval=30)

    def refuse(query=None, projection=None):
        raise PyMongoError("no primary available")

    users.find = refuse

    with caplog.at_level("ERROR", logger="starguard.bot"):
        delays = drive_loop(monkeypatch, drainer, cycles=3)

    assert drainer._consecutive_failures == 3
    # Backoff, not a flat retry: each wait is longer than the last.
    assert delays == sorted(delays)
    assert 22.5 <= delays[0] <= 37.5
    assert "Role sync drain failed" in caplog.text
    assert "no primary available" in caplog.text


def test_a_pass_that_leaves_rows_queued_backs_off_like_a_failure(monkeypatch, caplog):
    # A role above the bot's own in the hierarchy, or a missing permission,
    # fails every row in the queue and fails it again on every pass. The
    # loop used to call that a completed drain: the counter was reset, the
    # ordinary interval was used, and the same refused requests went back
    # out a few seconds later, forever, taking the shared lock off the
    # sweep each time. Nothing about it recovers faster for being retried.
    members = {"1": RefusingMember("1", roles=())}
    drainer, _, _, users = build([pending(1, True)], members=members, role_sync_interval=30)

    with caplog.at_level("WARNING", logger="starguard.bot"):
        delays = drive_loop(monkeypatch, drainer, cycles=3)

    assert drainer._consecutive_failures == 3
    assert delays == sorted(delays)
    assert 22.5 <= delays[0] <= 37.5
    assert delays[-1] > delays[0]
    # Still queued, which is the point: the row is retried, just not at
    # the polling interval.
    assert users.documents[0]["role_sync_pending"] is True
    assert "left 1 row(s) queued" in caplog.text
    # Nothing was raised, so nothing prints a traceback. A stack trace for
    # a Discord refusal the row already logged is noise.
    assert "Traceback" not in caplog.text


def test_the_drain_backoff_tops_out_well_below_the_sweeps(monkeypatch):
    drainer, _, _, _ = build([], role_sync_interval=30)
    monkeypatch.setattr("bot.starcheck.random.uniform", lambda low, high: 1.0)

    delays = []
    for failures in range(1, 12):
        drainer._consecutive_failures = failures
        delays.append(drainer._error_delay())

    assert delays[0] == 30
    assert delays[1] == 60
    assert delays == sorted(delays)
    # A queued row is a role somebody is missing right now, so this loop
    # tops out at five minutes where the sweep tops out at half an hour.
    assert max(delays) == DRAIN_ERROR_BACKOFF_MAX_SECONDS


def test_a_star_during_the_walk_survives_the_sweep_and_reaches_the_drain(monkeypatch):
    # The four-step failure, end to end, with both guards in place:
    #
    #   1. The sweep takes a listing. This member is not in it.
    #   2. During the walk they star, and the webhook writes starred_repo
    #      true and raises the flag.
    #   3. The sweep reaches the row. It used to remove the role and write
    #      starred_repo false over the webhook's value.
    #   4. The drain then read a row saying un-starred with the role
    #      already gone, called it settled, and lowered the flag over
    #      evidence step 3 had destroyed.
    #
    # Steps 3 and 4 are what the timestamp rule removes.
    document = link(1, "Gone", starred=False)
    users = RecordingUsers([document])
    members = {"1": FakeMember("1", roles=(ROLE_ID,))}
    channel = FakeChannel()
    client = FakeClient(FakeGuild(members), channel)
    config = make_config()

    def star_during_the_walk(owner, repo, token=None, cache=None):
        record_star_event(
            users,
            document["github_id"],
            True,
            STAR_SOURCE_WEBHOOK,
            datetime.now(UTC) + timedelta(seconds=1),
        )
        return listing()

    monkeypatch.setattr("bot.starcheck.fetch_stargazer_listing", star_during_the_walk)
    member_locks = MemberLocks()
    checker = StarChecker(client, config, users, member_locks)
    drainer = RoleSyncDrainer(client, config, users, member_locks)

    async def sweep_then_drain():
        return await checker.run_once(), await drainer.drain_once()

    removed, result = asyncio.run(sweep_then_drain())

    # Step 3 does not happen. The member keeps the role they just earned
    # and is not told goodbye for a star they still hold.
    assert removed == []
    assert members["1"].roles == {ROLE_ID}
    assert members["1"].removals == 0
    assert not channel.sent

    # So step 4 reads the truth instead of the sweep's overwrite, finds
    # Discord already agrees with it, and settles the row honestly.
    assert users.documents[0]["starred_repo"] is True
    assert users.documents[0]["star_source"] == STAR_SOURCE_WEBHOOK
    assert result == DrainResult(examined=1)
    assert users.documents[0]["role_sync_pending"] is False


def test_a_role_taken_on_stale_information_is_queued_and_put_back(monkeypatch):
    # The window the freshness check cannot close, end to end. The row
    # already says starred, so the webhook's own writer declines to queue
    # anything: no star state moved. What moved is Discord, and only the
    # sweep knows that, which is why the refused write is what queues.
    #
    #   1. The listing is taken. This member is not in it.
    #   2. The sweep passes the freshness check and removes the role.
    #   3. The member re-stars. star_event_at is written, no flag.
    #   4. The conditional write is refused, so the sweep raises the flag.
    #   5. The drain grants the role back.
    document = link(1, "Gone", starred=True)
    users = RecordingUsers([document])
    members = {"1": FakeMember("1", roles=(ROLE_ID,))}
    channel = FakeChannel()
    client = FakeClient(FakeGuild(members), channel)
    config = make_config()
    original_remove = members["1"].remove_role

    async def star_between_the_check_and_the_write(role_id, reason=None):
        await original_remove(role_id, reason)
        record_star_event(
            users,
            document["github_id"],
            True,
            STAR_SOURCE_WEBHOOK,
            datetime.now(UTC) + timedelta(seconds=1),
        )
        # The point of this case: the webhook queued nothing, because as
        # far as the row is concerned nothing changed.
        assert "role_sync_pending" not in users.documents[0]

    members["1"].remove_role = star_between_the_check_and_the_write

    monkeypatch.setattr("bot.starcheck.fetch_stargazer_listing", lambda *a, **k: listing())
    member_locks = MemberLocks()
    checker = StarChecker(client, config, users, member_locks)
    drainer = RoleSyncDrainer(client, config, users, member_locks)

    async def sweep_then_drain():
        return await checker.run_once(), await drainer.drain_once()

    removed, result = asyncio.run(sweep_then_drain())

    # The sweep really did take the role. That part is not recoverable: it
    # had committed to Discord before it could learn it was working from a
    # stale listing. What it does not do is talk about it, because the
    # member is about to have the role back.
    assert removed == []
    assert not channel.sent
    # It queued the row rather than leaving the member stripped, so the
    # drain hands the role straight back.
    assert result == DrainResult(examined=1, granted=1)
    assert members["1"].roles == {ROLE_ID}
    assert users.documents[0]["starred_repo"] is True
    assert users.documents[0]["star_source"] == STAR_SOURCE_WEBHOOK
    assert users.documents[0]["role_sync_pending"] is False


def run_startup(client):
    """Fire the Startup event the way the gateway would, and clean up.

    Returns ``(check_task, drain_task)``, either of which is None when that
    loop is configured off.
    """

    async def scenario():
        await client.listeners["startup"][0].callback()
        tasks = tuple(
            getattr(client, name, None)
            for name in ("starguard_check_task", "starguard_rolesync_task")
        )
        for task in tasks:
            if task is not None:
                # Nothing has yielded yet, so a task that is already done
                # would mean it never started rather than that it finished.
                assert not task.done()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        return tasks

    return asyncio.run(scenario())


def test_startup_starts_the_drain_and_holds_its_own_reference_to_it():
    client, _ = create_client(make_config(role_sync_interval=45), users=None)

    check_task, drain_task = run_startup(client)

    # Holding the reference is what keeps asyncio from collecting the task
    # mid-run, and the drain keeps its own attribute so that turning either
    # loop off leaves the other's handle alone.
    assert drain_task is not None
    assert drain_task is not check_task


def test_startup_with_the_drain_turned_off_starts_nothing(caplog):
    # An operator who has not configured the GitHub webhook has nothing to
    # drain, and should not be made to poll for it.
    client, _ = create_client(make_config(role_sync_enabled=False), users=None)

    with caplog.at_level("INFO", logger="starguard.bot"):
        check_task, drain_task = run_startup(client)

    assert drain_task is None
    assert not hasattr(client, "starguard_rolesync_task")
    # The sweep is independent and keeps running.
    assert check_task is not None
    assert "ROLE_SYNC_ENABLED=false" in caplog.text
