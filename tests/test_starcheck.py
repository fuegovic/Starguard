"""Tests for the un-star check: its concurrency guard and its backoff."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument,protected-access

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from interactions.client.errors import Forbidden, HTTPException
from pymongo.errors import PyMongoError

from bot.config import BotConfig
from bot.memberlock import MemberLocks
from bot.starcheck import CheckAlreadyRunningError, StarChecker
from common.github_api import StargazerListing
from common.storage import STAR_SOURCE_SWEEP, STAR_SOURCE_WEBHOOK, record_star_event
from tests.test_roles import discord_error

ROLE_ID = 111
GUILD_ID = 222
CHANNEL_ID = 333


def make_config(**overrides):
    settings = {
        "token": "fake.token.value",
        "client_id": "",
        "owner": "owner",
        "repo": "repo",
        "github_token": None,
        "role_id": ROLE_ID,
        "guild_id": GUILD_ID,
        "channel_id": CHANNEL_ID,
        "domain": "https://example.com",
        "secret_key": "0123456789abcdef-a-real-looking-key",
        "mongo_host": "mongodb://127.0.0.1:27017/",
        "mongo_database": "starguard_test",
        "automatic_check": True,
        "check_delay": 300,
        "role_sync_enabled": True,
        "role_sync_interval": 30,
        "command_name": "",
        "command_description": "Useful links",
        "command_extended_description": "",
        "link_buttons": (),
        "health_enabled": False,
        "health_host": "127.0.0.1",
        "health_port": 8080,
    }
    settings.update(overrides)
    return BotConfig(**settings)


class FakeMember:
    """A guild member whose role state really changes."""

    def __init__(self, member_id, roles=(ROLE_ID,)):
        self.id = member_id
        self.display_name = f"member{member_id}"
        self.roles = set(roles)
        self.removals = 0
        self.additions = 0

    def has_role(self, role_id):
        return role_id in self.roles

    async def add_role(self, role_id, reason=None):
        self.additions += 1
        self.roles.add(role_id)

    async def remove_role(self, role_id, reason=None):
        self.removals += 1
        self.roles.discard(role_id)


class FakeChannel:
    """Records the announcements the check posts."""

    def __init__(self):
        self.sent = []

    async def send(self, content=None):
        self.sent.append(content)


class UpdateResult:
    """What pymongo hands back, reduced to the field storage reads off it."""

    def __init__(self, matched_count):
        self.matched_count = matched_count


class FakeGuild:
    """A guild whose member cache the test controls."""

    def __init__(self, members):
        self._members = members

    def get_member(self, discord_id):
        return self._members.get(str(discord_id))


class FakeClient:
    """Only the two cache lookups the check makes."""

    def __init__(self, guild, channel):
        self._guild = guild
        self._channel = channel

    def get_guild(self, guild_id):
        return self._guild if guild_id == GUILD_ID else None

    def get_channel(self, channel_id):
        return self._channel if channel_id == CHANNEL_ID else None


MISSING = object()


class FakeUsers:
    """Just enough of a collection for the two cursors and the two writers.

    Both the reads and the writes really apply their filters. The pending
    queue is a filtered read, and every write the sweep makes is now
    conditional, so a fake that matched everything would let both of those
    guards be deleted with the suite still green. An unsupported operator
    raises rather than matching, for the same reason.
    """

    def __init__(self, documents):
        self.documents = [dict(d) for d in documents]

    @classmethod
    def _matches(cls, document, query):
        for key, condition in (query or {}).items():
            if key == "$or":
                if not any(cls._matches(document, sub) for sub in condition):
                    return False
                continue
            value = document.get(key, MISSING)
            if not isinstance(condition, dict):
                if value != condition:
                    return False
            elif "$exists" in condition:
                if (value is not MISSING) != condition["$exists"]:
                    return False
            elif "$lte" in condition:
                if value is MISSING or not value <= condition["$lte"]:
                    return False
            else:
                raise AssertionError(f"unsupported query: {condition!r}")
        return True

    @staticmethod
    def _project(document, projection):
        dropped = {k for k, v in (projection or {}).items() if not v}
        return {k: v for k, v in document.items() if k not in dropped}

    def find(self, query=None, projection=None):
        return iter(
            [self._project(d, projection) for d in self.documents if self._matches(d, query)]
        )

    def find_one(self, query, projection=None):
        for document in self.documents:
            if self._matches(document, query):
                return self._project(document, projection)
        return None

    def update_one(self, query, update, upsert=False):
        for document in self.documents:
            if self._matches(document, query):
                document.update(update.get("$set", {}))
                return UpdateResult(1)
        return UpdateResult(0)

    # return_document is accepted and ignored: common.storage only ever
    # asks for the document as it stands after the update.
    def find_one_and_update(self, query, update, projection=None, return_document=None):
        for document in self.documents:
            if self._matches(document, query):
                document.update(update.get("$set", {}))
                return self._project(document, projection)
        return None


def account_id(login):
    """A stable fake GitHub account id for ``login``.

    Derived from the login only so a test can name one thing and get both;
    the point of the id is that it survives the login changing, which is
    what the rename tests below set up by passing ``ids`` by hand.
    """
    return 1000 + sum(ord(character) for character in login.lower())


def link(discord_id, username, starred=True):
    return {
        "discord_id": str(discord_id),
        "discord_username": f"user{discord_id}",
        "github_id": account_id(username),
        "github_username": username,
        "github_username_lower": username.lower(),
        "starred_repo": starred,
    }


def legacy_link(discord_id, username, starred=True):
    """A row written before ``github_id`` was recorded."""
    document = link(discord_id, username, starred)
    del document["github_id"]
    return document


def listing(*logins, ids=None):
    """A stargazer listing for ``logins``, carrying the matching ids.

    The ids default to the ones :func:`link` stores, so a test that names
    logins still describes the same accounts on both sides. Passing ``ids``
    explicitly is how a rename is set up: the account stays, the login moves.
    """
    return StargazerListing(
        logins=frozenset(logins),
        ids=frozenset(account_id(login) for login in logins) if ids is None else frozenset(ids),
        api_calls=1,
        pages_fetched=1,
    )


def build(
    monkeypatch, documents, stargazers, fetch=None, ids=None, member_locks=None, **config_overrides
):
    """Wire a checker up to fakes. Returns (checker, members, channel, users).

    ``member_locks`` is the registry the sweep takes a member's mutex from.
    A private one is right for a checker on its own; the tests that run a
    sweep against a drain or a claim pass the registry they share, because
    three private registries would be three sets of mutexes and no
    exclusion at all.
    """
    # Rows with no usable Discord ID name no member, the same way the
    # drain's build does. Indexing them unconditionally raised KeyError on
    # the shape the sweep most needs to be handed: a document whose
    # `discord_id` key is absent entirely.
    members = {
        document["discord_id"]: FakeMember(document["discord_id"])
        for document in documents
        if document.get("discord_id")
    }
    channel = FakeChannel()
    users = FakeUsers(documents)
    client = FakeClient(FakeGuild(members), channel)

    def default_fetch(owner, repo, token=None, cache=None):
        return listing(*stargazers, ids=ids)

    monkeypatch.setattr("bot.starcheck.fetch_stargazer_listing", fetch or default_fetch)
    checker = StarChecker(
        client,
        make_config(**config_overrides),
        users,
        MemberLocks() if member_locks is None else member_locks,
    )
    return checker, members, channel, users


def test_a_member_who_unstarred_loses_the_role(monkeypatch):
    checker, members, channel, users = build(
        monkeypatch, [link(1, "Gone"), link(2, "Kept")], {"kept"}
    )

    removed = asyncio.run(checker.run_once())

    assert removed == ["user1"]
    assert members["1"].removals == 1
    assert members["1"].roles == set()
    assert members["2"].roles == {ROLE_ID}
    assert len(channel.sent) == 1
    assert users.documents[0]["starred_repo"] is False


class ExplodingMember(FakeMember):
    """A member whose role removal fails the way nothing below it expects.

    ``safe_remove_role`` catches Forbidden, NotFound and HTTPException, so a
    RuntimeError is exactly the class of surprise the sweep used to have no
    answer for.
    """

    async def remove_role(self, role_id, reason=None):
        raise RuntimeError("member cache is confused")


def test_one_unusable_row_does_not_strand_the_rest_of_the_walk(monkeypatch, caplog):
    # The sweep's counterpart to the drain's
    # test_one_unusable_row_does_not_strand_the_rest_of_the_queue, and the
    # stakes are higher here. The drain leaves a failed row's flag raised and
    # comes back to it; the sweep has no queue and re-walks iter_links from
    # the beginning every cycle, so a row that raised aborted the cycle and
    # the retry reached the same row again. Nobody positioned after it was
    # ever checked again, for as long as the bot ran.
    checker, members, channel, users = build(
        monkeypatch, [link(1, "Gone"), link(2, "AlsoGone")], set()
    )
    # FakeGuild holds this very dict, so swapping the entry is what the
    # sweep's own member lookup sees.
    members["1"] = ExplodingMember("1")

    with caplog.at_level("ERROR", logger="starguard.bot"):
        removed = asyncio.run(checker.run_once())

    # The walk continued past the failure and did the rest of its job.
    assert removed == ["user2"]
    assert members["2"].roles == set()
    assert users.documents[1]["starred_repo"] is False
    assert len(channel.sent) == 1

    # The row that blew up is reported rather than swallowed, and its own
    # state is left alone for the next cycle to try again.
    assert "Could not check the star for Discord ID 1" in caplog.text
    assert "member cache is confused" in caplog.text
    assert users.documents[0]["starred_repo"] is True


def test_a_member_who_still_stars_is_left_alone(monkeypatch):
    checker, members, channel, _ = build(monkeypatch, [link(1, "Kept")], {"kept"})

    assert asyncio.run(checker.run_once()) == []
    assert members["1"].removals == 0
    assert not channel.sent


def test_a_member_who_renamed_their_github_account_keeps_the_role(monkeypatch):
    # The bug the id matching exists for. GitHub usernames are changeable,
    # so after a rename the stored spelling appears nowhere in the listing;
    # comparing logins read that as an un-star and took the role from
    # somebody who had never touched their star.
    stored = link(1, "OldName")
    checker, members, channel, users = build(
        monkeypatch, [stored], {"newname"}, ids={stored["github_id"]}
    )

    assert asyncio.run(checker.run_once()) == []
    assert members["1"].removals == 0
    assert members["1"].roles == {ROLE_ID}
    assert not channel.sent
    assert users.documents[0]["starred_repo"] is True


def test_somebody_else_holding_the_old_login_does_not_save_the_role(monkeypatch):
    # The other half of the same fact: a rename frees the login, so the
    # spelling in an old row can belong to a stranger who does star the
    # repository. Once an id is stored the login is ignored entirely.
    stored = link(1, "Handle")
    checker, members, channel, users = build(
        monkeypatch, [stored], {"handle"}, ids={stored["github_id"] + 1}
    )

    assert asyncio.run(checker.run_once()) == ["user1"]
    assert members["1"].roles == set()
    assert len(channel.sent) == 1
    assert users.documents[0]["starred_repo"] is False


def test_a_row_written_before_ids_were_stored_falls_back_to_its_login(monkeypatch):
    # An id cannot be derived from a login without another API call, so the
    # oldest rows keep the old comparison. Reading the missing id as "not
    # starred" would strip the role from all of them at once.
    checker, members, channel, users = build(monkeypatch, [legacy_link(1, "Kept")], {"kept"})

    assert asyncio.run(checker.run_once()) == []
    assert members["1"].removals == 0
    assert not channel.sent
    assert users.documents[0]["starred_repo"] is True


def test_a_row_written_before_ids_were_stored_still_loses_the_role_on_an_un_star(monkeypatch):
    checker, members, channel, users = build(monkeypatch, [legacy_link(1, "Gone")], set())

    assert asyncio.run(checker.run_once()) == ["user1"]
    assert members["1"].roles == set()
    assert len(channel.sent) == 1
    assert users.documents[0]["starred_repo"] is False


def test_a_row_with_neither_an_id_nor_a_login_is_left_alone(monkeypatch):
    # Nothing to compare against, so there is no evidence of an un-star, and
    # taking a role on no evidence is the failure worth avoiding.
    document = {**legacy_link(1, "Gone"), "github_username": "", "github_username_lower": ""}
    checker, members, channel, _ = build(monkeypatch, [document], set())

    assert asyncio.run(checker.run_once()) == []
    assert members["1"].removals == 0
    assert not channel.sent


def test_two_cycles_never_overlap(monkeypatch):
    # /checkstars called straight into the check while the timer loop could
    # already be inside it, so the same role was removed twice and the same
    # farewell was posted twice.
    depth = {"now": 0, "peak": 0}

    def slow_fetch(owner, repo, token=None, cache=None):
        depth["now"] += 1
        depth["peak"] = max(depth["peak"], depth["now"])
        try:
            return listing()
        finally:
            depth["now"] -= 1

    checker, members, channel, _ = build(monkeypatch, [link(1, "Gone")], set(), fetch=slow_fetch)

    async def both():
        return await asyncio.gather(checker.run_once(), checker.run_once())

    results = asyncio.run(both())

    assert depth["peak"] == 1
    # The second pass finds the role already gone, so nothing is duplicated.
    assert sorted(len(result) for result in results) == [0, 1]
    assert members["1"].removals == 1
    assert len(channel.sent) == 1


def test_a_waiting_caller_runs_after_the_first_finishes(monkeypatch):
    checker, _, _, _ = build(monkeypatch, [], set())
    order = []

    async def scenario():
        async def cycle(name):
            order.append(f"{name} start")
            await checker.run_once()
            order.append(f"{name} end")

        await asyncio.gather(cycle("first"), cycle("second"))

    asyncio.run(scenario())
    assert order.index("first end") < order.index("second end")


def test_checkstars_is_told_a_cycle_is_already_running(monkeypatch):
    # A real cycle, held where a long one spends its time, rather than the
    # lock taken by hand: the lock is shared with the role-sync drain, so
    # holding it proves nothing about a check being in progress.
    checker, members, _, _ = build(monkeypatch, [link(1, "Gone")], set())

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        original = members["1"].remove_role

        async def hold(role_id, reason=None):
            entered.set()
            await release.wait()
            await original(role_id, reason)

        members["1"].remove_role = hold
        cycle = asyncio.create_task(checker.run_once())
        await entered.wait()

        assert checker.running is True
        with pytest.raises(CheckAlreadyRunningError):
            await checker.run_once(wait=False)

        release.set()
        assert await cycle == ["user1"]
        assert checker.running is False

    asyncio.run(scenario())


def test_a_drain_in_progress_is_neither_a_running_check_nor_something_to_wait_for(monkeypatch):
    # The drain used to be handed this very lock, so /checkstars answered
    # "a star check is already running" whenever a non-empty drain happened
    # to be in progress. That is a sentence an administrator can do nothing
    # with: no check was running, no results were coming, and pressing it
    # again a second later said the same thing. The cycle lock holds cycles
    # now, and the drain holds one member's mutex at a time.
    member_locks = MemberLocks()
    checker, _, _, _ = build(monkeypatch, [], set(), member_locks=member_locks)

    async def scenario():
        release = asyncio.Event()

        async def drain():
            async with member_locks.hold("1"):
                await release.wait()

        holder = asyncio.create_task(drain())
        await asyncio.sleep(0)

        assert checker.running is False
        # Nor does the check queue behind it. The sweep takes each
        # member's mutex as it reaches them, and it reaches nobody here.
        assert await checker.run_once(wait=False) == []
        assert checker.running is False

        release.set()
        await holder

    asyncio.run(scenario())


def test_a_missing_database_is_skipped_rather_than_crashing(monkeypatch):
    checker, _, _, _ = build(monkeypatch, [], set())
    checker._users = None
    assert asyncio.run(checker.run_once()) == []


def test_a_guild_missing_from_the_cache_is_skipped(monkeypatch):
    checker, members, _, _ = build(monkeypatch, [link(1, "Gone")], set(), guild_id=GUILD_ID + 1)
    assert asyncio.run(checker.run_once()) == []
    assert members["1"].removals == 0


def test_a_member_who_left_the_guild_is_recorded_not_crashed_on(monkeypatch):
    # Calling has_role on the None a departed member returns used to raise
    # and take the whole loop down with it.
    checker, members, channel, users = build(monkeypatch, [link(1, "Gone")], set())
    members.clear()

    assert asyncio.run(checker.run_once()) == []
    assert users.documents[0]["starred_repo"] is False
    assert not channel.sent


def test_a_member_who_stars_during_the_walk_keeps_the_role(monkeypatch):
    # The worst of the lost updates. One listing is fetched and then minutes
    # are spent walking 45,000 stargazers against it. A member who stars
    # during that walk is not in the listing, so the check would take the
    # role the webhook had just earned them and write starred_repo false
    # over the webhook's record of it. The row would then agree with the
    # check forever and nothing would self-correct.
    document = link(1, "Gone", starred=False)
    checker, members, channel, users = build(monkeypatch, [document], set())

    def star_during_the_walk(owner, repo, token=None, cache=None):
        # The listing is taken first, then the webhook lands. The offset is
        # explicit rather than relying on datetime.now advancing between two
        # statements, which is not something to make a test depend on.
        record_star_event(
            users,
            document["github_id"],
            True,
            STAR_SOURCE_WEBHOOK,
            datetime.now(UTC) + timedelta(seconds=1),
        )
        return listing()

    monkeypatch.setattr("bot.starcheck.fetch_stargazer_listing", star_during_the_walk)

    assert asyncio.run(checker.run_once()) == []
    assert members["1"].removals == 0
    assert members["1"].roles == {ROLE_ID}
    assert not channel.sent
    # The webhook's record survives intact, attribution included, so the
    # drain still has something true to act on.
    assert users.documents[0]["starred_repo"] is True
    assert users.documents[0]["star_source"] == STAR_SOURCE_WEBHOOK
    assert users.documents[0]["role_sync_pending"] is True


def test_a_member_who_relinks_while_the_cycle_waits_keeps_the_role(monkeypatch):
    # The freshness re-check under the lock cannot see a re-link, and that
    # is not an oversight in the check: link_account clears star_event_at
    # when the account changes, so the row comes back looking untouched by
    # anything newer than the listing.
    #
    # set_starred refuses the write, because it names the account this cycle
    # judged, so the database is right either way. What that refusal cannot
    # undo is the role removal, which happens first. Without the identity
    # check the member is stripped on the strength of an answer about an
    # account they have left, and gets the role back only when the drain
    # next runs on the entry queue_role_sync raised. Declining here is what
    # stops that interval from happening at all.
    document = link(1, "Gone", starred=False)
    checker, members, channel, users = build(monkeypatch, [document], set())
    reread = users.find_one

    def relink_first(query, projection=None):
        # The window between the row this cycle judged and the re-read it
        # does holding the lock. Applied to the row rather than through
        # link_account, whose $ne and $unset this module's fake does not
        # take; that link_account leaves this shape is asserted in
        # tests/test_storage.py and against mongomock.
        users.find_one = reread
        users.documents[0].update(github_id=account_id("Elsewhere"), starred_repo=True)
        users.documents[0].pop("star_event_at", None)
        return reread(query, projection)

    users.find_one = relink_first

    assert asyncio.run(checker.run_once()) == []
    assert members["1"].removals == 0
    assert members["1"].roles == {ROLE_ID}
    assert not channel.sent
    assert users.documents[0]["starred_repo"] is True


def test_a_star_event_older_than_the_listing_does_not_stop_the_sweep(monkeypatch):
    # The regression guard for gating on role_sync_pending instead of a
    # timestamp. An operator running with the drain turned off accumulates
    # raised flags that nobody lowers, and a check that skipped flagged
    # rows would skip those members for good. A timestamp ages out.
    document = link(1, "Gone", starred=False)
    checker, members, channel, users = build(monkeypatch, [document], set())
    record_star_event(
        users,
        document["github_id"],
        True,
        STAR_SOURCE_WEBHOOK,
        datetime.now(UTC) - timedelta(days=1),
    )
    assert users.documents[0]["role_sync_pending"] is True

    assert asyncio.run(checker.run_once()) == ["user1"]
    assert members["1"].roles == set()
    assert users.documents[0]["starred_repo"] is False
    assert users.documents[0]["star_source"] == STAR_SOURCE_SWEEP
    assert len(channel.sent) == 1


def test_a_star_event_that_lands_mid_row_does_not_get_written_over(monkeypatch):
    # The residual window the read-side skip cannot close: the row passed
    # the freshness check, and the webhook lands in the microseconds before
    # the write. The conditional write is what catches it, so the role is
    # taken but the record of the newer star is not destroyed, and the row
    # is still queued for the drain to put the role back.
    document = link(1, "Gone", starred=False)
    checker, members, channel, users = build(monkeypatch, [document], set())
    github_id = document["github_id"]
    original = members["1"].remove_role

    async def star_between_the_check_and_the_write(role_id, reason=None):
        await original(role_id, reason)
        record_star_event(
            users,
            github_id,
            True,
            STAR_SOURCE_WEBHOOK,
            datetime.now(UTC) + timedelta(seconds=1),
        )

    members["1"].remove_role = star_between_the_check_and_the_write

    # Nothing is reported, because the drain is about to undo it.
    assert asyncio.run(checker.run_once()) == []

    # The role really was taken, and that part is not recoverable here: the
    # window is microseconds wide and the check had already committed. What
    # the conditional write saves is the record, which is what lets the
    # drain put the role back on its next poll.
    assert members["1"].roles == set()
    assert users.documents[0]["starred_repo"] is True
    assert users.documents[0]["star_source"] == STAR_SOURCE_WEBHOOK
    assert users.documents[0]["role_sync_pending"] is True
    assert not channel.sent


def build_with_one_overtaken(monkeypatch, queue_error=None):
    """One cycle over two un-stars, the second of which a star overtakes.

    Both members are in the same cycle on purpose. Every assertion about
    the overtaken one is that the check says nothing, and an assertion
    that nothing happened proves little without a member beside it for
    whom everything did.

    ``queue_error`` is raised by the write that raises the pending flag,
    which is the one failure the cycle cannot shrug off.

    Returns ``(checker, members, channel, users, queued)``, where
    ``queued`` collects the Discord IDs any write puts on the drain's
    queue.
    """
    checker, members, channel, users = build(
        monkeypatch, [link(1, "Gone"), link(2, "AlsoGone")], set()
    )

    original_update = users.update_one
    queued = []

    def note_the_flag(query, update, upsert=False):
        if update.get("$set", {}).get("role_sync_pending") is True:
            queued.append(query["discord_id"])
            if queue_error is not None:
                raise queue_error
        return original_update(query, update, upsert)

    users.update_one = note_the_flag
    original_remove = members["2"].remove_role

    async def star_between_the_check_and_the_write(role_id, reason=None):
        await original_remove(role_id, reason)
        # The row already says starred, so record_star_event writes the
        # timestamp and deliberately queues nothing: no star state moved.
        record_star_event(
            users,
            users.documents[1]["github_id"],
            True,
            STAR_SOURCE_WEBHOOK,
            datetime.now(UTC) + timedelta(seconds=1),
        )

    members["2"].remove_role = star_between_the_check_and_the_write
    return checker, members, channel, users, queued


def test_only_the_overtaken_write_queues_a_role_sync(monkeypatch):
    # Raising the flag is how the check admits it acted on stale data, so
    # it must happen on exactly the rows where the write was refused. An
    # ordinary un-star has nothing for the drain to reconcile, and queueing
    # those would put every swept member on a queue built to be empty.
    checker, _, _, users, queued = build_with_one_overtaken(monkeypatch)

    asyncio.run(checker.run_once())

    # The ordinary un-star wrote its state and queued nothing.
    assert users.documents[0]["starred_repo"] is False
    assert "role_sync_pending" not in users.documents[0]
    # The overtaken one queued itself, once, and only the check did it.
    assert queued == ["2"]
    assert users.documents[1]["role_sync_pending"] is True
    assert users.documents[1]["starred_repo"] is True
    assert users.documents[1]["star_source"] == STAR_SOURCE_WEBHOOK


def test_an_overtaken_removal_is_neither_announced_nor_reported(monkeypatch, caplog):
    checker, members, channel, _, _ = build_with_one_overtaken(monkeypatch)

    with caplog.at_level("INFO", logger="starguard.bot"):
        removed = asyncio.run(checker.run_once())

    # The ordinary un-star does all three: the role goes, the farewell is
    # posted, and the member is reported.
    assert members["1"].roles == set()
    assert removed == ["user1"]
    assert len(channel.sent) == 1
    assert "<@1>" in channel.sent[0]

    # The overtaken one lost the role too, but the drain gives it straight
    # back, so this cycle says nothing about them anywhere. A farewell to
    # somebody who stars the repository outlives the restored role in the
    # channel's scrollback, and reporting a loss to an admin about a member
    # who holds the role is an hour of debugging.
    assert members["2"].roles == set()
    assert "user2" not in removed
    assert not any("<@2>" in sent for sent in channel.sent)

    # run_once's return value is exactly what /checkstars renders, and the
    # summary counts the same list, so both follow from `removed` above.
    summaries = [r for r in caplog.records if r.message.startswith("Star check complete")]
    assert summaries[0].examined == 2
    assert summaries[0].roles_removed == 1


def test_a_refusal_that_cannot_be_queued_is_still_not_reported(monkeypatch, caplog):
    # The two failures stacked. The refusal already proved the role was
    # taken on stale information, and now the reconciliation cannot be
    # queued either, so nothing will hand the role back: a sweep only ever
    # takes roles away. Both writes went through one try block, so the
    # second failure was read as "only the bookkeeping failed" and the
    # cycle announced a farewell and reported a loss, about a member the
    # database says stars the repository.
    checker, members, channel, users, queued = build_with_one_overtaken(
        monkeypatch, queue_error=PyMongoError("no primary available")
    )

    with caplog.at_level("ERROR", logger="starguard.bot"):
        removed = asyncio.run(checker.run_once())

    # The queue write was attempted and really did fail.
    assert queued == ["2"]
    assert "role_sync_pending" not in users.documents[1]
    # The member beside them is unaffected, so this is the overtaken row
    # and not a cycle that gave up.
    assert removed == ["user1"]
    assert len(channel.sent) == 1
    assert "<@1>" in channel.sent[0]
    # Nothing is claimed about the member whose state nobody can now fix,
    # and the operator gets the one line that says the role is stuck.
    assert members["2"].roles == set()
    assert "user2" not in removed
    assert not any("<@2>" in sent for sent in channel.sent)
    assert "Could not queue the role sync for 2" in caplog.text
    assert "no primary available" in caplog.text


def test_every_link_is_examined_across_batches(monkeypatch):
    monkeypatch.setattr("bot.starcheck.LINK_BATCH_SIZE", 3)
    documents = [link(index, f"user{index}") for index in range(1, 11)]
    checker, members, _, _ = build(monkeypatch, documents, set())

    removed = asyncio.run(checker.run_once())
    assert len(removed) == 10
    assert all(member.removals == 1 for member in members.values())


def test_the_cycle_logs_one_summary_line(monkeypatch, caplog):
    checker, _, _, _ = build(monkeypatch, [link(1, "Kept")], {"kept"})

    with caplog.at_level("INFO", logger="starguard.bot"):
        asyncio.run(checker.run_once())

    summaries = [r for r in caplog.records if r.message.startswith("Star check complete")]
    assert len(summaries) == 1
    assert summaries[0].examined == 1
    assert summaries[0].roles_removed == 0
    assert summaries[0].api_calls == 1
    assert "duration_seconds" in summaries[0].__dict__


class RefusingMember(FakeMember):
    """A member whose role the bot is not allowed to touch."""

    async def remove_role(self, role_id, reason=None):
        raise discord_error(Forbidden)


class SilencedChannel(FakeChannel):
    """An announcement channel the bot may not post in."""

    async def send(self, content=None):
        raise discord_error(HTTPException)


@pytest.mark.parametrize("spelling", ["null", "missing", "empty"])
def test_a_link_with_no_discord_id_is_skipped(monkeypatch, spelling):
    # The sweep meets the same rows the drain does, so it is parametrised
    # over the same three shapes. Null is the one that exists in numbers:
    # from b715c73 (October 2023) until the rebuild, /login read the
    # Discord ID from an unvalidated query parameter and stored whatever
    # came back, so an unauthenticated GET with no id, followed by OAuth,
    # wrote `discord_id: None`. There is nobody to take a role from.
    document = {**link(1, "Gone")}
    if spelling == "missing":
        del document["discord_id"]
    else:
        document["discord_id"] = None if spelling == "null" else ""
    checker, _, channel, _ = build(monkeypatch, [document], set())

    assert asyncio.run(checker.run_once()) == []
    assert not channel.sent


def test_a_role_the_bot_cannot_remove_is_left_alone_for_the_next_cycle(monkeypatch, caplog):
    checker, members, channel, users = build(monkeypatch, [link(1, "Gone")], set())
    members["1"] = RefusingMember("1")

    with caplog.at_level("WARNING", logger="starguard.bot"):
        assert asyncio.run(checker.run_once()) == []

    # Nothing is announced and nothing is recorded, so the next cycle tries
    # again rather than believing the role was taken.
    assert not channel.sent
    assert users.documents[0]["starred_repo"] is True
    assert "Could not remove the role" in caplog.text


def test_the_role_is_still_removed_without_an_announcement_channel(monkeypatch):
    checker, members, _, users = build(
        monkeypatch, [link(1, "Gone")], set(), channel_id=CHANNEL_ID + 1
    )

    assert asyncio.run(checker.run_once()) == ["user1"]
    assert members["1"].roles == set()
    assert users.documents[0]["starred_repo"] is False


def test_a_channel_that_refuses_the_message_does_not_undo_the_removal(monkeypatch, caplog):
    checker, members, _, _ = build(monkeypatch, [link(1, "Gone")], set())
    checker._client._channel = SilencedChannel()

    with caplog.at_level("WARNING", logger="starguard.bot"):
        assert asyncio.run(checker.run_once()) == ["user1"]

    assert members["1"].roles == set()
    assert "announcement channel" in caplog.text


def test_a_database_write_that_fails_does_not_stop_the_sweep(monkeypatch, caplog):
    documents = [link(1, "Gone"), link(2, "AlsoGone")]
    checker, members, _, users = build(monkeypatch, documents, set())

    def refuse(query, update, upsert=False):
        raise PyMongoError("no primary available")

    users.update_one = refuse

    with caplog.at_level("ERROR", logger="starguard.bot"):
        removed = asyncio.run(checker.run_once())

    # Both members still lose the role; only the bookkeeping failed.
    assert removed == ["user1", "user2"]
    assert all(member.roles == set() for member in members.values())
    assert caplog.text.count("Could not update star state") == 2
