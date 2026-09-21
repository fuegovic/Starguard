"""What the per-member lock guarantees when two components meet on one member.

The sweep, the drain and the **Claim your role** button all read a
member's roles, change them and record the change, and they run at the
same time as each other. These tests are about the seam between them
rather than about any one of their jobs, which is why they live apart
from the three modules that cover those: what is under test here is that
one member is only ever inside one of them, that the exclusion costs
nothing to anybody else, and that whoever waits decides from what it
finds afterwards rather than from what it read before.
"""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument,protected-access

import asyncio
import threading
from datetime import UTC, datetime, timedelta

from bot.memberlock import MemberLocks
from bot.rolesync import DrainResult, RoleSyncDrainer
from bot.starcheck import StarChecker
from tests.test_rolesync import RecordingUsers, pending
from tests.test_starcheck import (
    ROLE_ID,
    FakeChannel,
    FakeClient,
    FakeGuild,
    FakeMember,
    account_id,
    link,
    listing,
    make_config,
)
from tests.test_verification import FakeContext, register

# How long a tracked role change holds the member open for company. Loop
# turns rather than seconds, so the suite pays microseconds for it: each
# one is a bare asyncio.sleep(0). The bound is generous because what has
# to arrive within it is another component's thread hop, and it is a bound
# rather than a rendezvous because the fixed code never sends anybody,
# which is the whole point. See TrackedMember.
COMPANY_TURNS = 500

# How long the two hang guards in test_a_drain_does_not_wait_out_a_sweep_of
# _other_members wait before giving up. Not a timing assumption: with the
# locks split both events are set within microseconds of being waited on,
# and the bound exists only so a regression that puts the drain back behind
# the cycle fails rather than hangs forever. Generous on purpose, because
# what has to finish inside it is a whole drain pass, and ten seconds was
# short enough that a loaded machine turned a passing run into a spurious
# failure reported against the wrong assertion: the guard fires inside the
# worker thread, asyncio.to_thread hands the error to the sweep task, and
# the test then reports that the sweep had finished.
HANG_GUARD_SECONDS = 60


def test_a_claim_a_drain_and_a_sweep_never_act_on_one_member_at_once(monkeypatch):
    # The path nobody had put in the picture. The claim button is the
    # third thing in this process that moves this role, and until now it
    # was excluded from neither of the other two. What that costs:
    #
    #   1. The row says un-starred, the member holds the role, and they
    #      press Claim. It reads the row and starts removing the role.
    #   2. They re-star. The webhook writes starred and raises the flag.
    #   3. The drain reads the row, sees the role still on the member,
    #      concludes Discord already agrees and lowers the flag.
    #   4. The claim's removal lands.
    #
    # The row then says the member stars the repository, nothing is
    # queued, and they hold no role, and the sweep only ever takes roles
    # away, so nothing would ever put it back. That interleaving cannot be
    # forced once the fix is in, which is what the fix means, so what is
    # asserted here is the property that rules it out: no two of the three
    # are ever inside one member at the same time.
    #
    # All three are given the same work on the same member, so each of
    # them looks at member.has_role and each of them wants to remove the
    # role. Without exclusion they all see it held and all three call
    # Discord.
    depth = {"now": 0, "peak": 0}
    document = {
        **pending(1, False),
        "linked_repo": make_config().repo_url,
    }
    users = RecordingUsers([document])
    member = TrackedMember("1", depth)
    channel = FakeChannel()
    client = FakeClient(FakeGuild({"1": member}), channel)
    config = make_config()

    monkeypatch.setattr("bot.starcheck.fetch_stargazer_listing", lambda *a, **k: listing())
    member_locks = MemberLocks()
    checker = StarChecker(client, config, users, member_locks)
    drainer = RoleSyncDrainer(client, config, users, member_locks)
    buttons, _ = register(users=users, member_locks=member_locks)

    async def all_three():
        ctx = FakeContext(author=member)
        return await asyncio.gather(
            checker.run_once(),
            drainer.drain_once(),
            buttons.component_callbacks["claim"].callback(ctx),
        )

    asyncio.run(all_three())

    assert depth["peak"] == 1
    # Whichever of the three got there first did the work, and the other
    # two found the role already gone. One role change, and at most one
    # farewell: the claim posts none, and neither of the loops announces
    # a removal it did not make.
    assert member.removals == 1
    assert member.roles == set()
    assert len(channel.sent) <= 1
    # Nothing is left queued over a role change that has already happened.
    assert users.documents[0]["role_sync_pending"] is False


class TrackedMember(FakeMember):
    """A member counting how many role changes are in flight on them at once.

    The role change is held open until somebody else turns up inside it or
    the turns run out. Without that the test measures thread scheduling
    rather than exclusion: every component reaches a member through an
    asyncio.to_thread hop, and one that resumes early can finish its whole
    role change before the next one is handed back the event loop, so an
    unlocked run looked exclusive about half the time.
    """

    def __init__(self, member_id, depth, roles=(ROLE_ID,)):
        super().__init__(member_id, roles=roles)
        self._depth = depth

    async def _tracked(self, call):
        self._depth["now"] += 1
        self._depth["peak"] = max(self._depth["peak"], self._depth["now"])
        try:
            for _ in range(COMPANY_TURNS):
                if self._depth["now"] > 1:
                    # Somebody else is inside this member, which is the
                    # answer; there is nothing to wait for any longer.
                    break
                await asyncio.sleep(0)
            await call
        finally:
            self._depth["now"] -= 1

    async def add_role(self, role_id, reason=None):
        await self._tracked(super().add_role(role_id, reason))

    async def remove_role(self, role_id, reason=None):
        await self._tracked(super().remove_role(role_id, reason))


def test_a_sweep_and_a_drain_never_act_on_the_same_member_at_once(monkeypatch):
    # The two loops reach the same members and the same role. Registries of
    # their own would be no exclusion at all, which is why the sweep, the
    # drain and the claim button are handed one: two of them inside a
    # member at once is the double role change and the double farewell the
    # exclusion exists to stop.
    #
    # Both loops have real work on this one member. The listing says the
    # star is gone, so the sweep strips the role, and a queued un-star says
    # the drain should strip the same role. Each of them looks at
    # member.has_role before it acts, so without exclusion both see it held
    # and both call Discord.
    depth = {"now": 0, "peak": 0}
    documents = [pending(1, False)]
    members = {"1": TrackedMember("1", depth)}
    channel = FakeChannel()
    users = RecordingUsers(documents)
    client = FakeClient(FakeGuild(members), channel)
    config = make_config()

    monkeypatch.setattr("bot.starcheck.fetch_stargazer_listing", lambda *a, **k: listing())
    member_locks = MemberLocks()
    checker = StarChecker(client, config, users, member_locks)
    drainer = RoleSyncDrainer(client, config, users, member_locks)

    async def both():
        return await asyncio.gather(checker.run_once(), drainer.drain_once())

    removed, result = asyncio.run(both())

    assert depth["peak"] == 1
    # Whichever of them got there first did the work; the other found it
    # already done. One role change, and one goodbye rather than two.
    assert members["1"].removals == 1
    assert members["1"].roles == set()
    assert len(channel.sent) == 1
    assert removed in ([], ["user1"])
    assert result.examined == 1
    assert users.documents[0]["role_sync_pending"] is False


def test_a_drain_does_not_wait_out_a_sweep_of_other_members(monkeypatch):
    # The other half of the split, and why one mutex for both jobs was
    # wrong however well it excluded them. The cycle lock is held across
    # fetch_stargazer_listing, documented as minutes on a repository with
    # 45,000 stargazers, and across the whole member sweep after it.
    # Handing that lock to the drain meant a webhook queued for anybody at
    # all waited out an entire cycle, so ROLE_SYNC_INTERVAL, thirty seconds
    # by default, described nothing that happens.
    crawling = threading.Event()
    finish_crawl = threading.Event()

    def slow_fetch(owner, repo, token=None, cache=None):
        crawling.set()
        # Stands in for the minutes a crawl costs. Waited on rather than
        # slept through, so the test runs as fast as the code does, and
        # released by the test once the drain has been all the way through.
        # See HANG_GUARD_SECONDS for why the bound is what it is.
        assert finish_crawl.wait(timeout=HANG_GUARD_SECONDS)
        return listing("kept", ids={account_id("Kept")})

    documents = [link(1, "Gone"), pending(2, True, username="Kept")]
    members = {"1": FakeMember("1", roles=(ROLE_ID,)), "2": FakeMember("2", roles=())}
    channel = FakeChannel()
    users = RecordingUsers(documents)
    client = FakeClient(FakeGuild(members), channel)
    config = make_config()

    monkeypatch.setattr("bot.starcheck.fetch_stargazer_listing", slow_fetch)
    member_locks = MemberLocks()
    checker = StarChecker(client, config, users, member_locks)
    drainer = RoleSyncDrainer(client, config, users, member_locks)

    async def scenario():
        sweep = asyncio.create_task(checker.run_once())
        assert await asyncio.to_thread(crawling.wait, HANG_GUARD_SECONDS)

        result = await drainer.drain_once()
        # The crawl has not returned, so the drain really did deliver a
        # queued role change from inside a cycle rather than after it.
        assert not sweep.done()

        finish_crawl.set()
        return result, await sweep

    result, removed = asyncio.run(scenario())

    assert result == DrainResult(examined=1, granted=1)
    assert members["2"].roles == {ROLE_ID}
    # And the cycle still finishes its own work on the other member.
    assert removed == ["user1"]
    assert members["1"].roles == set()


class OvertakenUsers(RecordingUsers):
    """A collection where somebody else's write lands inside the lock window.

    ``find_one`` is the re-read the drain and the sweep each make once they
    hold the member lock, so changing the row on the first of those is
    exactly the write another component gets in while they wait for it.
    Nothing here choreographs tasks against each other: the window is
    defined by which read it is rather than by timing, which is what makes
    these two deterministic where a real second task would not be.
    """

    def __init__(self, documents, **changes):
        super().__init__(documents)
        self.changes = changes
        self.rereads = 0

    def find_one(self, query, projection=None):
        # Applied on every read rather than only the first, so that a
        # version of the code which never re-reads fails these on what the
        # member ends up with rather than on the bookkeeping below. Each
        # of these drives one member, so there is only ever one such read.
        self.rereads += 1
        for document in self.documents:
            document.update(self.changes)
        return super().find_one(query, projection)


def test_a_drain_does_not_grant_from_a_snapshot_a_sweep_overtook():
    # The queue snapshot said starred. While this drain waited for the
    # member lock, a sweep took the role away and wrote starred_repo
    # False. Granting from the older snapshot hands straight back the role
    # the sweep just removed. The conditional clear refuses it, so the row
    # stays queued and the stored state is never wrong, but the member
    # holds a role the row does not ask for until a later pass takes it
    # off them, which is the whole of the damage and the reason the lock
    # has to cover the decision and not only the change.
    users = OvertakenUsers([pending(1, True)], starred_repo=False)
    members = {"1": FakeMember("1", roles=())}
    client = FakeClient(FakeGuild(members), FakeChannel())
    drainer = RoleSyncDrainer(client, make_config(), users, MemberLocks())

    asyncio.run(drainer.drain_once())

    assert members["1"].additions == 0
    assert members["1"].roles == set()
    assert users.rereads == 1


def test_a_sweep_does_not_remove_a_role_a_drain_granted_while_it_waited(monkeypatch):
    # The mirror of it. The listing says this member has stopped starring,
    # and that was decided against the row as the cycle read it. While the
    # cycle waited for the member lock, a drain granted the role from an
    # event newer than the listing. Removing it now takes back a role the
    # webhook had just earned; the guarded write refuses the stale state
    # and requeues a correction, so again the database is right and it is
    # the member who pays, by losing access until another drain returns it.
    later = datetime.now(UTC) + timedelta(hours=1)
    users = OvertakenUsers([link(1, "Gone")], star_event_at=later, starred_repo=True)
    members = {"1": FakeMember("1", roles=(ROLE_ID,))}
    client = FakeClient(FakeGuild(members), FakeChannel())
    monkeypatch.setattr("bot.starcheck.fetch_stargazer_listing", lambda *a, **k: listing())
    checker = StarChecker(client, make_config(), users, MemberLocks())

    removed = asyncio.run(checker.run_once())

    assert removed == []
    assert members["1"].removals == 0
    assert members["1"].roles == {ROLE_ID}
    assert users.rereads == 1
