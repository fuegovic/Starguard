"""Every slash command the bot answers.

The handlers are registered against the recording client from
test_verification and then called directly, which is what the library does
with them. Nothing here reaches GitHub, Discord or MongoDB.
"""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import asyncio

import pytest

from bot import messages
from bot.commands import (
    DISCORD_CONTENT_LIMIT,
    register_commands,
    register_info_commands,
    register_links_command,
    register_star_commands,
)
from bot.memberlock import MemberLocks
from bot.starcheck import CheckAlreadyRunningError
from common.github_api import GitHubError
from common.storage_errors import StorageError
from tests.test_starcheck import make_config
from tests.test_verification import FakeContext, RecordingClient


class FakeChecker:
    """A star checker that answers with whatever the test asked for."""

    def __init__(self, removed=(), error=None):
        self.removed = list(removed)
        self.error = error
        self.calls = []

    async def run_once(self, wait=True):
        self.calls.append(wait)
        if self.error is not None:
            raise self.error
        return self.removed


def call(client, name, ctx=None):
    """Invoke one registered slash command."""
    ctx = FakeContext() if ctx is None else ctx
    asyncio.run(client.commands[name].callback(ctx))
    return ctx


def info_client(**overrides):
    client = RecordingClient()
    register_info_commands(client, make_config(**overrides))
    return client


def star_client(checker, **overrides):
    client = RecordingClient()
    register_star_commands(client, make_config(**overrides), checker)
    return client


def test_ping_reports_the_gateway_latency_in_milliseconds():
    client = RecordingClient(latency=0.0421)
    register_info_commands(client, make_config())

    ctx = call(client, "ping")
    assert ctx.last.content == messages.PING.format(latency=42.1)
    assert ctx.last.ephemeral is True


def test_help_lists_the_standard_commands():
    ctx = call(info_client(), "help")
    embed = ctx.last.embed

    assert embed.title == messages.HELP_TITLE
    names = [field.name for field in embed.fields]
    assert "> /ping" in names
    assert "> /verify" in names
    assert "> /starcount" in names
    assert ctx.last.ephemeral is True


def test_help_names_the_repository_members_are_asked_to_star():
    ctx = call(info_client(owner="fuegovic", repo="Starguard"), "help")
    rendered = " ".join(field.value for field in ctx.last.embed.fields)
    assert "Starguard" in rendered
    assert "https://github.com/fuegovic/Starguard/" in rendered


def test_help_describes_the_custom_command_only_when_it_exists():
    without = call(info_client(), "help")
    assert not [f for f in without.last.embed.fields if f.name.startswith("> /links")]

    with_command = call(
        info_client(
            command_name="links",
            command_description="Useful links",
            command_extended_description="Everything worth reading",
        ),
        "help",
    )
    custom = [f for f in with_command.last.embed.fields if f.name == "> /links"]
    assert len(custom) == 1
    assert "Everything worth reading" in custom[0].value


def test_starcount_reports_how_many_accounts_have_starred(monkeypatch):
    monkeypatch.setattr(
        "bot.commands.fetch_stargazer_count",
        lambda owner, repo, token=None: 3,
    )
    ctx = call(star_client(FakeChecker()), "starcount")

    assert ctx.deferred is True
    assert ctx.last.content == messages.STARCOUNT.format(count=3)
    assert ctx.last.ephemeral is True


def test_starcount_passes_the_configured_repository_and_token(monkeypatch):
    seen = {}

    def record(owner, repo, token=None):
        seen.update(owner=owner, repo=repo, token=token)
        return 0

    monkeypatch.setattr("bot.commands.fetch_stargazer_count", record)
    call(
        star_client(FakeChecker(), owner="fuegovic", repo="Starguard", github_token="t"),
        "starcount",
    )
    assert seen == {"owner": "fuegovic", "repo": "Starguard", "token": "t"}


def test_starcount_survives_a_rate_limited_github(monkeypatch, caplog):
    # This used to call len() on None and raise a TypeError on every
    # rate-limited request, so the member saw nothing at all.
    def refuse(owner, repo, token=None):
        raise GitHubError("GitHub API rate limit exceeded.")

    monkeypatch.setattr("bot.commands.fetch_stargazer_count", refuse)
    with caplog.at_level("WARNING", logger="starguard.bot"):
        ctx = call(star_client(FakeChecker()), "starcount")

    assert ctx.last.content == messages.GITHUB_UNREACHABLE.format(
        reason="GitHub API rate limit exceeded."
    )
    assert ctx.last.ephemeral is True
    assert "rate limit" in caplog.text


def test_checkstars_reports_who_lost_the_role():
    checker = FakeChecker(removed=["alice", "bob"])
    ctx = call(star_client(checker), "checkstars")

    assert ctx.deferred is True
    assert ctx.last.content == messages.CHECK_REMOVED.format(count=2, names="**alice**, **bob**")
    assert ctx.last.ephemeral is True


def test_a_long_list_of_names_is_shortened_rather_than_rejected():
    # Discord refuses a message over 2,000 characters outright, and by the
    # time this one is sent the roles are already gone and the rows are
    # already written. Concatenating every name failed the send and made
    # the whole command look unsuccessful for work that did happen.
    removed = [f"member-number-{index:03d}" for index in range(200)]
    ctx = call(star_client(FakeChecker(removed=removed)), "checkstars")

    content = ctx.last.content
    assert len(content) <= DISCORD_CONTENT_LIMIT
    # The count stays exact: only the list of names is cut short.
    assert content.startswith("Removed the role from 200 member(s)")
    assert "**member-number-000**" in content
    # Whole names or nothing. A name cut in half would read as a member who
    # is not in the guild.
    shown = content.count("**") // 2
    assert messages.CHECK_REMOVED_MORE.format(count=200 - shown) in content
    assert f"**member-number-{shown - 1:03d}**" in content
    assert f"**member-number-{shown:03d}**" not in content


def test_a_list_that_fits_is_not_shortened():
    # The boundary the test above cannot see: a list one name short of the
    # limit must still be reported in full, with no "and 0 more" tail.
    removed = ["x" * 96 for _ in range(19)]
    ctx = call(star_client(FakeChecker(removed=removed)), "checkstars")

    content = ctx.last.content
    assert 1900 < len(content) <= DISCORD_CONTENT_LIMIT
    assert content.count("**") // 2 == 19
    assert "more" not in content


def test_checkstars_says_so_when_nothing_changed():
    ctx = call(star_client(FakeChecker(removed=[])), "checkstars")
    assert ctx.last.content == messages.CHECK_NO_CHANGES


def test_checkstars_does_not_queue_behind_a_running_cycle():
    # A cycle over a large repository can take minutes and an interaction
    # token is only good for fifteen, so waiting would answer nobody.
    checker = FakeChecker(error=CheckAlreadyRunningError("already running"))
    ctx = call(star_client(checker), "checkstars")

    assert checker.calls == [False]
    assert ctx.last.content == messages.CHECK_ALREADY_RUNNING


def test_checkstars_reports_an_unreachable_github():
    checker = FakeChecker(error=GitHubError("Repository not found (404)."))
    ctx = call(star_client(checker), "checkstars")
    assert ctx.last.content == messages.GITHUB_UNREACHABLE.format(
        reason="Repository not found (404)."
    )


def test_checkstars_reports_an_unreachable_database(caplog):
    checker = FakeChecker(error=StorageError("no primary available"))
    with caplog.at_level("ERROR", logger="starguard.bot"):
        ctx = call(star_client(checker), "checkstars")

    assert ctx.last.content == messages.DATABASE_UNREACHABLE
    assert "no primary available" in caplog.text


def test_the_links_command_is_not_registered_when_unconfigured():
    # Previously the bot registered a command literally named "None" and
    # crashed on buttons with empty URLs.
    client = RecordingClient()
    register_links_command(client, make_config())
    assert not client.commands


def test_the_links_command_sends_the_configured_buttons():
    client = RecordingClient()
    register_links_command(
        client,
        make_config(
            command_name="links",
            link_buttons=(
                ("GitHub", "https://github.com/"),
                ("Docs", "https://example.com/docs"),
            ),
        ),
    )

    ctx = call(client, "links")
    assert ctx.last.content == messages.LINK_BUTTONS_TITLE
    assert [(b.label, b.url) for b in ctx.last.buttons] == [
        ("GitHub", "https://github.com/"),
        ("Docs", "https://example.com/docs"),
    ]
    assert ctx.last.ephemeral is True


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, {"ping", "help", "verify", "starcount", "checkstars"}),
        (
            {
                "command_name": "links",
                "link_buttons": (("GitHub", "https://github.com/"),),
            },
            {"ping", "help", "verify", "starcount", "checkstars", "links"},
        ),
    ],
)
def test_register_commands_registers_the_whole_surface(overrides, expected):
    client = RecordingClient()
    register_commands(client, make_config(**overrides), FakeChecker(), None, MemberLocks())

    assert set(client.commands) == expected
    assert set(client.component_callbacks) == {"claim", "relink"}
