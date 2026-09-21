"""The verification flow: /verify, the recovery link and claiming the role.

This is the part that issues signed link tokens and grants the role, so it is
tested by outcome rather than by coverage: whether a role really moved, which
message the member was shown, and whether the link they were handed carries
their own Discord ID and nobody else's.

The handlers are registered against a recording client and then called
directly, which is what the library does with them.
"""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import asyncio
from urllib.parse import parse_qs, urlsplit

from interactions import ButtonStyle
from interactions.client.errors import Forbidden
from pymongo.errors import PyMongoError

from bot import messages
from bot.verification import (
    CLAIM_BUTTON_ID,
    RELINK_BUTTON_ID,
    login_url,
    register_verification,
)
from common.linktoken import read_link_token
from tests.test_roles import FakeHttpResponse
from tests.test_starcheck import ROLE_ID, make_config

AUTHOR_ID = 123456789
SECRET = "0123456789abcdef-a-real-looking-key"


class RecordingClient:
    """Collects what the register_* functions hand it.

    The real client stores commands and component callbacks in the same two
    ways; this keeps the tests to the registration API instead of the
    library's internals.
    """

    def __init__(self, latency=0.0421):
        self.commands = {}
        self.component_callbacks = {}
        self.listeners = []
        self.latency = latency

    def add_command(self, command):
        self.commands[str(command.name)] = command

    def add_component_callback(self, callback):
        for custom_id in callback.listeners:
            self.component_callbacks[custom_id] = callback

    def add_listener(self, listener):
        self.listeners.append(listener)


class Sent:
    """One message a handler sent back to the interaction."""

    def __init__(self, content, components, ephemeral, embed):
        self.content = content
        self.components = components
        self.ephemeral = ephemeral
        self.embed = embed

    @property
    def buttons(self):
        """Every button across every action row in the message."""
        return [button for row in (self.components or []) for button in row.components]


class FakeMember:
    """The author of an interaction, whose role state really changes."""

    def __init__(self, member_id=AUTHOR_ID, roles=(), add_error=None):
        self.id = member_id
        self.display_name = "someone"
        self.roles = set(roles)
        self.add_error = add_error
        self.added = 0
        self.removals = 0

    def __str__(self):
        return "someone#0001"

    def has_role(self, role_id):
        return role_id in self.roles

    async def add_role(self, role_id, reason=None):
        if self.add_error is not None:
            raise self.add_error
        self.added += 1
        self.roles.add(role_id)

    async def remove_role(self, role_id, reason=None):
        self.removals += 1
        self.roles.discard(role_id)


class FakeContext:
    """The parts of an interaction context these handlers touch."""

    def __init__(self, author=None):
        self.author = FakeMember() if author is None else author
        self.author_id = self.author.id
        self.sent = []
        self.deferred = False

    async def defer(self, ephemeral=False):
        self.deferred = True

    async def send(self, content=None, components=None, ephemeral=False, embed=None):
        self.sent.append(Sent(content, components, ephemeral, embed))

    @property
    def last(self):
        """The most recent message, which is the answer the member sees."""
        assert self.sent, "the handler answered nothing at all"
        return self.sent[-1]


class FakeUsers:
    """A collection that returns one document, or fails the way Mongo does."""

    def __init__(self, document=None, error=None):
        self.document = document
        self.error = error
        self.queries = []

    def find_one(self, query, projection=None):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.document


def register(users=None, **config_overrides):
    """Register the verification flow and return (client, config)."""
    config = make_config(secret_key=SECRET, **config_overrides)
    client = RecordingClient()
    register_verification(client, config, users)
    return client, config


def run(client, custom_id, ctx):
    """Invoke one registered component callback."""
    asyncio.run(client.component_callbacks[custom_id].callback(ctx))
    return ctx


def linked(starred=True):
    return {
        "discord_id": str(AUTHOR_ID),
        "github_username": "Octocat",
        "starred_repo": starred,
    }


def test_the_login_url_carries_a_token_and_not_a_bare_discord_id():
    # The Discord ID used to travel as a plain query parameter, so anyone
    # could start the flow claiming to be any Discord user.
    config = make_config(secret_key=SECRET, domain="https://example.com")
    url = login_url(config, AUTHOR_ID, "someone#0001")
    parts = urlsplit(url)
    query = parse_qs(parts.query)

    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://example.com/login"
    assert set(query) == {"token"}
    assert read_link_token(SECRET, query["token"][0]) == (
        str(AUTHOR_ID),
        "someone#0001",
    )


def test_verify_offers_the_three_steps_privately():
    client, config = register()
    ctx = FakeContext()
    asyncio.run(client.commands["verify"].callback(ctx))

    message = ctx.last
    assert message.content == messages.VERIFY_STEPS
    # Nobody else in the channel sees a link that is personal to one member.
    assert message.ephemeral is True

    labels = [button.label for button in message.buttons]
    assert labels == [
        messages.VERIFY_BUTTON_STAR,
        messages.VERIFY_BUTTON_LOGIN,
        messages.VERIFY_BUTTON_CLAIM,
        messages.VERIFY_BUTTON_RELINK,
    ]
    assert message.buttons[0].url == config.repo_url
    assert message.buttons[2].custom_id == CLAIM_BUTTON_ID
    assert message.buttons[3].custom_id == RELINK_BUTTON_ID


def test_every_member_gets_their_own_link():
    client, _ = register()
    first = FakeContext(FakeMember(member_id=111111111))
    second = FakeContext(FakeMember(member_id=222222222))
    asyncio.run(client.commands["verify"].callback(first))
    asyncio.run(client.commands["verify"].callback(second))

    def discord_id(context):
        url = context.last.buttons[1].url
        return read_link_token(SECRET, parse_qs(urlsplit(url).query)["token"][0])[0]

    assert discord_id(first) == "111111111"
    assert discord_id(second) == "222222222"


def test_the_relink_button_issues_a_fresh_link_without_starting_over():
    # A link is only good for fifteen minutes, and the page that says so
    # could previously only tell people to run /verify again.
    client, _ = register()
    ctx = run(client, RELINK_BUTTON_ID, FakeContext())

    message = ctx.last
    # It names the claim button, so nobody has to remember where they were.
    assert messages.VERIFY_BUTTON_CLAIM in message.content
    assert message.ephemeral is True
    assert len(message.buttons) == 1
    assert message.buttons[0].style == ButtonStyle.URL
    assert "/login?token=" in message.buttons[0].url


def test_claiming_with_a_recorded_star_grants_the_role():
    client, _ = register(users=FakeUsers(linked(starred=True)))
    member = FakeMember()
    ctx = run(client, CLAIM_BUTTON_ID, FakeContext(member))

    assert member.roles == {ROLE_ID}
    assert member.added == 1
    # The thank you is public: it is the only visible sign the bot works.
    assert ctx.last.ephemeral is False
    assert str(AUTHOR_ID) in ctx.last.content


def test_claiming_looks_the_member_up_by_their_own_discord_id():
    users = FakeUsers(linked())
    client, _ = register(users=users)
    run(client, CLAIM_BUTTON_ID, FakeContext())
    assert users.queries == [{"discord_id": str(AUTHOR_ID)}]


def test_claiming_without_a_database_says_so_rather_than_failing_silently():
    client, _ = register(users=None)
    member = FakeMember()
    ctx = run(client, CLAIM_BUTTON_ID, FakeContext(member))

    assert ctx.last.content == messages.CLAIM_DATABASE_UNAVAILABLE
    assert ctx.last.ephemeral is True
    assert member.roles == set()


class FakeUser:
    """An author from a direct message: no guild, so no roles and no has_role."""

    def __init__(self, user_id=AUTHOR_ID):
        self.id = user_id
        self.display_name = "someone"

    def __str__(self):
        return "someone#0001"


def test_verify_is_guild_only():
    # Roles only exist in a server, so the flow must not be startable
    # anywhere it could never be completed.
    client, _ = register()
    assert client.commands["verify"].dm_permission is False


def test_claiming_from_a_direct_message_is_explained_not_a_traceback():
    # A User has no has_role, so this used to raise AttributeError at the
    # member.has_role call and answer the member with nothing useful. The
    # button outlives the change that made /verify guild-only, because it
    # lives in whatever message already carried it.
    client, _ = register(users=FakeUsers(linked(starred=True)))
    ctx = run(client, CLAIM_BUTTON_ID, FakeContext(FakeUser()))

    assert ctx.last.content == messages.CLAIM_NEEDS_A_SERVER
    assert ctx.last.ephemeral is True


def test_a_failed_lookup_is_reported_not_raised(caplog):
    client, _ = register(users=FakeUsers(error=PyMongoError("no primary")))
    member = FakeMember()

    with caplog.at_level("ERROR", logger="starguard.bot"):
        ctx = run(client, CLAIM_BUTTON_ID, FakeContext(member))

    assert ctx.last.content == messages.CLAIM_LOOKUP_FAILED
    assert member.roles == set()
    assert "no primary" in caplog.text


def test_claiming_without_a_link_offers_a_new_one():
    client, _ = register(users=FakeUsers(None))
    member = FakeMember()
    ctx = run(client, CLAIM_BUTTON_ID, FakeContext(member))

    assert messages.VERIFY_BUTTON_RELINK in ctx.last.content
    assert [b.custom_id for b in ctx.last.buttons] == [RELINK_BUTTON_ID]
    assert member.roles == set()


def test_claiming_without_a_star_is_refused():
    client, _ = register(users=FakeUsers(linked(starred=False)))
    member = FakeMember()
    ctx = run(client, CLAIM_BUTTON_ID, FakeContext(member))

    assert ctx.last.content == messages.CLAIM_NOT_STARRED
    assert member.roles == set()
    # Nothing to take away, so nothing is taken away.
    assert member.removals == 0


def test_un_starring_after_claiming_costs_the_role_immediately():
    # The star check would catch this on its next cycle; pressing the button
    # in the meantime must not leave the role in place.
    client, _ = register(users=FakeUsers(linked(starred=False)))
    member = FakeMember(roles=(ROLE_ID,))
    ctx = run(client, CLAIM_BUTTON_ID, FakeContext(member))

    assert member.removals == 1
    assert member.roles == set()
    assert ctx.last.content == messages.CLAIM_NOT_STARRED


def test_a_link_with_no_star_field_is_treated_as_un_starred():
    # The oldest rows predate the field, and a missing star is not a star.
    document = linked()
    del document["starred_repo"]
    client, _ = register(users=FakeUsers(document))
    member = FakeMember()

    ctx = run(client, CLAIM_BUTTON_ID, FakeContext(member))
    assert ctx.last.content == messages.CLAIM_NOT_STARRED
    assert member.roles == set()


def test_claiming_twice_says_so_instead_of_thanking_twice():
    client, _ = register(users=FakeUsers(linked()))
    member = FakeMember(roles=(ROLE_ID,))
    ctx = run(client, CLAIM_BUTTON_ID, FakeContext(member))

    assert ctx.last.content == messages.CLAIM_ALREADY_HELD
    assert ctx.last.ephemeral is True
    assert member.added == 0


def test_a_role_the_bot_cannot_grant_is_explained_to_the_member(caplog):
    # The usual cause is a role positioned above the bot's own, which the
    # member can do nothing about and a moderator can fix in a second. The
    # thank you message would otherwise claim a role that was never granted.
    client, _ = register(users=FakeUsers(linked()))
    member = FakeMember(add_error=Forbidden(FakeHttpResponse(), text="Missing Access"))

    with caplog.at_level("WARNING", logger="starguard.bot"):
        ctx = run(client, CLAIM_BUTTON_ID, FakeContext(member))

    assert ctx.last.content == messages.CLAIM_ROLE_FAILED
    assert ctx.last.ephemeral is True
    assert member.roles == set()
    assert "Missing Access" in caplog.text
