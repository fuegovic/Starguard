"""Tests for the un-star check: its concurrency guard and its backoff."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument,protected-access

import asyncio

import pytest
from interactions.client.errors import Forbidden, HTTPException
from pymongo.errors import PyMongoError

from bot.config import BotConfig
from bot.starcheck import CheckAlreadyRunningError, StarChecker
from common.github_api import GitHubError, StargazerListing
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

    def has_role(self, role_id):
        return role_id in self.roles

    async def remove_role(self, role_id, reason=None):
        self.removals += 1
        self.roles.discard(role_id)


class FakeChannel:
    """Records the announcements the check posts."""

    def __init__(self):
        self.sent = []

    async def send(self, content=None):
        self.sent.append(content)


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


class FakeUsers:
    """Just enough of a collection for iter_links and set_starred."""

    def __init__(self, documents):
        self.documents = [dict(d) for d in documents]

    def find(self, query=None, projection=None):
        dropped = {k for k, v in (projection or {}).items() if not v}
        return iter([{k: v for k, v in d.items() if k not in dropped} for d in self.documents])

    def update_one(self, query, update, upsert=False):
        for document in self.documents:
            if all(document.get(k) == v for k, v in query.items()):
                document.update(update.get("$set", {}))
                return


def link(discord_id, username, starred=True):
    return {
        "discord_id": str(discord_id),
        "discord_username": f"user{discord_id}",
        "github_username": username,
        "github_username_lower": username.lower(),
        "starred_repo": starred,
    }


def listing(*logins):
    return StargazerListing(logins=frozenset(logins), api_calls=1, pages_fetched=1)


def build(monkeypatch, documents, stargazers, fetch=None, **config_overrides):
    """Wire a checker up to fakes. Returns (checker, members, channel, users)."""
    members = {document["discord_id"]: FakeMember(document["discord_id"]) for document in documents}
    channel = FakeChannel()
    users = FakeUsers(documents)
    client = FakeClient(FakeGuild(members), channel)

    def default_fetch(owner, repo, token=None, cache=None):
        return listing(*stargazers)

    monkeypatch.setattr("bot.starcheck.fetch_stargazer_listing", fetch or default_fetch)
    checker = StarChecker(client, make_config(**config_overrides), users)
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


def test_a_member_who_still_stars_is_left_alone(monkeypatch):
    checker, members, channel, _ = build(monkeypatch, [link(1, "Kept")], {"kept"})

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
    checker, _, _, _ = build(monkeypatch, [], set())
    release = None

    async def scenario():
        nonlocal release
        release = asyncio.Event()

        async def blocked():
            async with checker._lock:
                await release.wait()

        holder = asyncio.create_task(blocked())
        await asyncio.sleep(0)

        assert checker.running is True
        with pytest.raises(CheckAlreadyRunningError):
            await checker.run_once(wait=False)

        release.set()
        await holder
        assert checker.running is False

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


class RefusingMember(FakeMember):
    """A member whose role the bot is not allowed to touch."""

    async def remove_role(self, role_id, reason=None):
        raise discord_error(Forbidden)


class SilencedChannel(FakeChannel):
    """An announcement channel the bot may not post in."""

    async def send(self, content=None):
        raise discord_error(HTTPException)


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


def test_a_link_with_no_discord_id_is_skipped(monkeypatch):
    # Rows written by the oldest version were keyed on the GitHub email and
    # some have no Discord ID at all; there is nobody to take a role from.
    document = {**link(1, "Gone"), "discord_id": ""}
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
