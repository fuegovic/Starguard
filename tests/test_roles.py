"""Role changes that report failure instead of raising.

The claim button and the star check add and remove the same role, and both
have to survive the bot simply not being allowed to: a role positioned above
the bot's own, or a member who left between the lookup and the write. An
exception escaping here used to take the whole check cycle down with it.
"""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import asyncio

import pytest
from interactions.client.errors import Forbidden, HTTPException, NotFound

from bot.roles import safe_add_role, safe_remove_role

ROLE_ID = 111
MEMBER_ID = 999


class FakeHttpResponse:
    """The two attributes the library's HTTPException reads off a response."""

    def __init__(self, status=403, reason="Forbidden"):
        self.status = status
        self.reason = reason


def discord_error(kind):
    """Build the error the library would raise for a refused role change."""
    return kind(FakeHttpResponse(), text="Missing Permissions")


class FakeMember:
    """A member whose role state really changes, or refuses to."""

    def __init__(self, roles=(), error=None):
        self.id = MEMBER_ID
        self.roles = set(roles)
        self.error = error
        self.reasons = []

    async def add_role(self, role_id, reason=None):
        if self.error is not None:
            raise self.error
        self.reasons.append(reason)
        self.roles.add(role_id)

    async def remove_role(self, role_id, reason=None):
        if self.error is not None:
            raise self.error
        self.reasons.append(reason)
        self.roles.discard(role_id)


def test_adding_a_role_reports_success_and_the_reason_it_gave():
    member = FakeMember()
    assert asyncio.run(safe_add_role(member, ROLE_ID, "star")) is True
    assert member.roles == {ROLE_ID}
    # The reason reaches the Discord audit log, which is where a moderator
    # looks to find out why the bot did something.
    assert member.reasons == ["star"]


def test_removing_a_role_reports_success_and_the_reason_it_gave():
    member = FakeMember(roles=(ROLE_ID,))
    assert asyncio.run(safe_remove_role(member, ROLE_ID, "no_star")) is True
    assert member.roles == set()
    assert member.reasons == ["no_star"]


@pytest.mark.parametrize("kind", [Forbidden, NotFound, HTTPException])
def test_a_refused_add_is_reported_not_raised(kind, caplog):
    member = FakeMember(error=discord_error(kind))

    with caplog.at_level("WARNING", logger="starguard.bot"):
        assert asyncio.run(safe_add_role(member, ROLE_ID, "star")) is False

    assert member.roles == set()
    assert str(MEMBER_ID) in caplog.text
    assert "Missing Permissions" in caplog.text


@pytest.mark.parametrize("kind", [Forbidden, NotFound, HTTPException])
def test_a_refused_removal_is_reported_not_raised(kind, caplog):
    member = FakeMember(roles=(ROLE_ID,), error=discord_error(kind))

    with caplog.at_level("WARNING", logger="starguard.bot"):
        assert asyncio.run(safe_remove_role(member, ROLE_ID, "no_star")) is False

    # The role is still there, which is exactly what the caller must be told.
    assert member.roles == {ROLE_ID}
    assert str(MEMBER_ID) in caplog.text
