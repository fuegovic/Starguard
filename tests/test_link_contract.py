"""One link document, written by the server and read by the bot.

The two processes share a collection and nothing else, so that document is
the whole contract between them. Everywhere else in this suite each side is
tested against its own stand-in collection, which is exactly the condition
under which two independently correct halves drift apart: the original bug
was rows keyed by the GitHub email rather than the Discord ID, so a second
link overwrote the first person's row and left them holding the role with no
record for the un-star check to find.

So these drive the real /authorize route into a real collection, then hand
that same collection to the real star check. Only the GitHub OAuth calls and
the Discord client are faked, and mongomock stands in for the database so
this stays as fast and as hermetic as the rest of the suite.

The tests never build a link document. If one did, it would have stopped
testing the seam and gone back to testing each side's idea of the schema.
"""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import asyncio
from datetime import datetime

import pytest

from bot.memberlock import MemberLocks
from bot.starcheck import StarChecker
from common.storage import SCHEMA_VERSION, all_links, connect, find_link
from server.server import ServerContext, create_app
from tests.test_server_authorize import (
    NOT_STARRED,
    PROFILE,
    STARRED,
    STARRED_PATH,
    FakeApiResponse,
    FakeGitHub,
)
from tests.test_server_routes import make_config as make_server_config
from tests.test_starcheck import (
    ROLE_ID,
    FakeChannel,
    FakeClient,
    FakeGuild,
    FakeMember,
    listing,
)
from tests.test_starcheck import make_config as make_bot_config

mongomock = pytest.importorskip("mongomock")

# The lower-cased login GitHub reports for the account the OAuth flow
# returns, and the immutable account id that goes with it. The star check
# matches on the id; the login is only the fallback for rows written before
# the id was recorded.
GITHUB_LOGIN = PROFILE["login"].lower()
GITHUB_ID = PROFILE["id"]

# Every field bot/starcheck.py reads off a link document, from _still_stars,
# _check_one and _display_name. The server has to write all of them.
FIELDS_THE_STAR_CHECK_READS = frozenset(
    {"discord_id", "discord_username", "github_id", "github_username", "github_username_lower"}
)


@pytest.fixture(name="users")
def users_fixture():
    """A collection prepared exactly the way both processes prepare theirs."""
    _, collection = connect("mongodb://localhost:27017/", "starguard", mongomock.MongoClient)
    return collection


def verify_through_the_server(users, discord_id, username, starred=True):
    """Walk one person through the real OAuth callback. Returns the response."""
    config = make_server_config()
    app = create_app(config, users=users)
    app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    app.extensions["starguard"] = ServerContext(
        config=config,
        users=users,
        github=FakeGitHub(
            responses={
                "user": FakeApiResponse(payload=dict(PROFILE)),
                STARRED_PATH: FakeApiResponse(status_code=STARRED if starred else NOT_STARRED),
            }
        ),
    )

    client = app.test_client()
    with client.session_transaction() as session:
        session["discord_id"] = discord_id
        session["discord_username"] = username
    return client.get("/authorize?code=abc&state=xyz")


def run_the_star_check(monkeypatch, users, stargazers, member, stargazer_ids=()):
    """Run the real check over ``users``. Returns (removed, channel).

    The ids are named separately from the logins because that is the seam:
    the server writes GitHub's own account id and the check looks for that
    id, so a test that only lined up the logins would pass while the two
    halves disagreed about which field identifies a person.
    """

    def fetch(owner, repo, token=None, cache=None):
        return listing(*stargazers, ids=stargazer_ids)

    monkeypatch.setattr("bot.starcheck.fetch_stargazer_listing", fetch)
    channel = FakeChannel()
    client = FakeClient(FakeGuild({member.id: member}), channel)
    # A registry of its own, because nothing else in this test is holding a
    # member lock; the seam under test is the document, not the exclusion.
    checker = StarChecker(client, make_bot_config(), users, MemberLocks())
    return asyncio.run(checker.run_once()), channel


def member_for(document, roles=(ROLE_ID,)):
    """A guild member for the person the stored document describes.

    Keyed off the ID the server wrote, so a change to how either side spells
    a Discord ID shows up here as a member the check cannot find.
    """
    return FakeMember(document["discord_id"], roles=roles)


def test_the_server_writes_the_document_the_bot_expects_to_read(users):
    assert verify_through_the_server(users, "123456789", "someone").status_code == 200

    # What the star check iterates: the bot's own view of the row.
    documents = all_links(users)
    assert len(documents) == 1
    document = documents[0]

    assert set(document) == {
        "schema_version",
        "discord_id",
        "discord_username",
        "github_id",
        "github_username",
        "github_username_lower",
        "linked_repo",
        "starred_repo",
        "updated_at",
    }
    assert set(document) >= FIELDS_THE_STAR_CHECK_READS

    assert document["schema_version"] == SCHEMA_VERSION
    # Discord IDs are strings and GitHub IDs are ints on both sides; the
    # lookup misses silently if either one changes its mind.
    assert document["discord_id"] == "123456789"
    assert isinstance(document["github_id"], int)
    # The id the check matches on, spelled the way GitHub spelled it. This
    # is the load-bearing one now, and an int on one side against a string
    # on the other would silently look like everybody had un-starred.
    assert document["github_id"] == GITHUB_ID
    assert document["github_username_lower"] == document["github_username"].lower()
    assert document["github_username_lower"] == GITHUB_LOGIN
    assert document["starred_repo"] is True
    # A BSON date, not the ISO string version 1 wrote, so the database can
    # compare and index it.
    assert isinstance(document["updated_at"], datetime)
    # Both processes build this URL from REPO_OWNER and GITHUB_REPO, and
    # they have to agree on it.
    assert document["linked_repo"] == make_bot_config().repo_url


def test_un_starring_after_verifying_costs_the_role_the_server_recorded(users, monkeypatch):
    verify_through_the_server(users, "123456789", "someone")
    document = find_link(users, "123456789")

    member = member_for(document)
    removed, channel = run_the_star_check(monkeypatch, users, stargazers=set(), member=member)

    # The name in the farewell comes from the row the server wrote.
    assert removed == ["someone"]
    assert member.roles == set()
    assert len(channel.sent) == 1
    assert find_link(users, "123456789")["starred_repo"] is False


def test_a_member_who_is_still_starring_keeps_the_role(users, monkeypatch):
    verify_through_the_server(users, "123456789", "someone")
    document = find_link(users, "123456789")

    member = member_for(document)
    removed, channel = run_the_star_check(
        monkeypatch,
        users,
        stargazers={GITHUB_LOGIN},
        member=member,
        stargazer_ids={document["github_id"]},
    )

    assert removed == []
    assert member.roles == {ROLE_ID}
    assert member.removals == 0
    assert not channel.sent
    assert find_link(users, "123456789")["starred_repo"] is True


def test_a_member_who_renames_on_github_keeps_the_role_the_server_recorded(users, monkeypatch):
    # GitHub lets people rename, and the listing then reports a login the
    # stored document has never seen. The row is still the same account, so
    # the two halves have to agree on matching by id rather than by name.
    verify_through_the_server(users, "123456789", "someone")
    document = find_link(users, "123456789")

    member = member_for(document)
    removed, channel = run_the_star_check(
        monkeypatch,
        users,
        stargazers={"renamed-since-linking"},
        member=member,
        stargazer_ids={document["github_id"]},
    )

    assert removed == []
    assert member.roles == {ROLE_ID}
    assert member.removals == 0
    assert not channel.sent
    assert find_link(users, "123456789")["starred_repo"] is True


def test_someone_who_never_starred_is_not_announced_as_leaving(users, monkeypatch):
    # The server records the link with starred_repo False and the member
    # never claimed the role, so there is nothing to take and nothing to say.
    verify_through_the_server(users, "123456789", "someone", starred=False)
    document = find_link(users, "123456789")
    assert document["starred_repo"] is False

    member = member_for(document, roles=())
    removed, channel = run_the_star_check(monkeypatch, users, stargazers=set(), member=member)

    assert removed == []
    assert member.removals == 0
    assert not channel.sent


def test_a_second_discord_user_cannot_take_over_the_first_ones_row(users, monkeypatch):
    # The bug this whole seam exists to prevent. Keyed on the GitHub email,
    # the second verification overwrote the first person's row, and the star
    # check then had no record of somebody who was still holding the role.
    assert verify_through_the_server(users, "111", "first").status_code == 200
    assert verify_through_the_server(users, "222", "second").status_code == 409

    assert len(all_links(users)) == 1
    document = find_link(users, "111")
    assert document["discord_username"] == "first"
    assert find_link(users, "222") is None

    # The first person is still accounted for, which is the part that broke.
    member = member_for(document)
    removed, _ = run_the_star_check(monkeypatch, users, stargazers=set(), member=member)
    assert removed == ["first"]
    assert member.roles == set()


def test_re_verifying_updates_the_row_the_check_already_knows_about(users, monkeypatch):
    verify_through_the_server(users, "123456789", "someone", starred=False)
    first_seen = find_link(users, "123456789")["updated_at"]

    verify_through_the_server(users, "123456789", "someone-renamed", starred=True)
    document = find_link(users, "123456789")

    assert len(all_links(users)) == 1
    assert document["starred_repo"] is True
    assert document["discord_username"] == "someone-renamed"
    assert document["updated_at"] >= first_seen

    member = member_for(document)
    removed, _ = run_the_star_check(
        monkeypatch,
        users,
        stargazers={GITHUB_LOGIN},
        member=member,
        stargazer_ids={document["github_id"]},
    )
    assert removed == []
