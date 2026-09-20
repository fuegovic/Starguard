"""The role-sync drain: the bot half of GitHub star webhook support.

A star webhook arrives at the OAuth server process, and only this process is
connected to the Discord gateway, so only this process can change a role. The
two never talk to each other, which is why the ``users`` collection is the
whole channel between them: the server records the new star state and raises
``role_sync_pending``, and this loop lowers it again once Discord agrees.

That replaces polling for the answer. The hourly sweep next door costs one
GitHub request per page of stargazers, which is 450 of them for a repository
with 45,000 stars; a webhook costs none, and the queue it leaves behind is a
partial index holding only the rows that are actually waiting. A poll that
finds nothing therefore reads an empty index and writes nothing at all, which
is what makes a thirty second interval affordable.

Blocking work (the synchronous pymongo driver) runs in a worker thread via
``asyncio.to_thread``, for the same reason as the sweep: the gateway
heartbeat lives on this event loop.
"""

import asyncio
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Final, Literal

from interactions import TYPE_MESSAGEABLE_CHANNEL, Client, Guild
from pymongo.errors import PyMongoError

from bot.config import BotConfig
from bot.memberlock import MemberLocks
from bot.roles import safe_add_role, safe_remove_role
from bot.starcheck import announce_unstarred, announcement_channel, error_backoff, next_batch
from common.storage import (
    MongoDocument,
    UserCollection,
    clear_role_sync_pending,
    clear_role_sync_pending_by_id,
    iter_pending_role_syncs,
)

log = logging.getLogger("starguard.bot")

# Pending rows pulled off the cursor per thread hop. Much smaller than the
# sweep's batch, because this queue holds only what is waiting rather than
# every verified member: a burst of stars should start reaching Discord as
# the cursor is read, not once the whole burst has been collected.
PENDING_BATCH_SIZE: Final = 50

# How far the retry wait may grow while the drain keeps failing. Far lower
# than the sweep's half hour, because a queued row is a member holding, or
# missing, a role right now. A transient MongoDB blip must not park somebody
# else's role for thirty minutes. The first retry is one ordinary interval
# and it doubles from there.
DRAIN_ERROR_BACKOFF_MAX_SECONDS: Final = 300

# What one queued row turned into. Counted rather than logged per row: a
# popular repository can queue a lot of stars at once and a line each would
# bury everything else in the log.
Outcome = Literal["granted", "removed", "settled", "failed", "unusable"]


@dataclass(frozen=True)
class DrainResult:
    """What one pass over the pending queue did.

    ``settled`` rows are deliberately absent: a row Discord already agreed
    with is the ordinary case (a redelivered webhook, or a star the sweep
    reached first) and counting it would only make an uneventful pass look
    busy. ``examined`` minus the other fields is how many there were.

    ``failed`` and ``unusable`` are counted apart because the loop treats
    them differently. A failure is worth retrying and backing off for; an
    unusable row is a row nothing here can act on at all, and letting it
    drive the backoff would slow every other row down on its account.
    """

    examined: int = 0
    granted: int = 0
    removed: int = 0
    failed: int = 0
    unusable: int = 0


class RoleSyncDrainer:
    """Applies the role changes the star webhook queued in the database."""

    def __init__(
        self,
        client: Client,
        config: BotConfig,
        users: UserCollection | None,
        member_locks: MemberLocks,
    ) -> None:
        self._client = client
        self._config = config
        self._users = users
        # The same registry the sweep and the claim button use, handed over
        # by bot.create_client. A registry of this drain's own would be no
        # exclusion at all: three sets of mutexes held independently let
        # all three components into one member at once.
        #
        # This used to be the star check's cycle lock instead, which
        # excluded the sweep but at the sweep's granularity: a queued
        # webhook about any member waited out a crawl that takes minutes on
        # a large repository, so the drain interval below described nothing
        # that happens. See bot.memberlock.
        self._member_locks = member_locks
        self._consecutive_failures = 0
        self._last_completed: float | None = None

    @property
    def last_completed(self) -> float | None:
        """``time.monotonic()`` of the last pass that ran, or None.

        Read by the health endpoint, and set only by a pass that actually
        walked the queue. A pass that returned early because there is no
        database connection or no guild in the cache did no reconciling,
        and saying otherwise is what let a drain-only deployment report
        itself healthy while doing nothing at all.
        """
        return self._last_completed

    async def run_forever(self) -> None:
        """Drain the queue on an interval, forever.

        Errors are caught and logged rather than allowed to escape. The
        check loop learned this the hard way: an unhandled exception killed
        the task silently, and nothing ran again until the bot restarted.
        Here that would mean roles that never move, with a webhook the
        operator can see being delivered successfully.

        A pass that completes while leaving rows queued backs off the same
        way a pass that raised does. Only the raising kind used to, and a
        Discord refusal is not one: with the role above the bot's in the
        hierarchy, or the permission missing, every row in a burst fails,
        stays queued and is resent a few seconds later, forever, taking
        every one of those members' locks against the sweep each time.
        Nothing about that recovers faster for being retried at the
        polling interval.
        """
        while True:
            delay: float
            try:
                result = await self.drain_once()
            # CancelledError derives from BaseException, so cancellation
            # still propagates out of this handler and stops the loop.
            except Exception:  # pylint: disable=broad-except
                self._consecutive_failures += 1
                delay = self._error_delay()
                log.exception(
                    "Role sync drain failed (%s in a row); retrying in %.0f seconds",
                    self._consecutive_failures,
                    delay,
                )
            else:
                delay = self._delay_after(result)
            await asyncio.sleep(delay)

    def _delay_after(self, result: DrainResult) -> float:
        """Return the wait after a pass that finished, and record its outcome.

        No traceback and no ``log.exception`` here: nothing was raised, and
        each individual failure has already been logged by the row that
        produced it. What this line adds is that they are not clearing.
        """
        if not result.failed:
            self._consecutive_failures = 0
            return self._config.role_sync_interval

        self._consecutive_failures += 1
        delay = self._error_delay()
        log.warning(
            "Role sync drain left %s row(s) queued (%s pass(es) in a row); "
            "retrying in %.0f seconds",
            result.failed,
            self._consecutive_failures,
            delay,
        )
        return delay

    async def drain_once(self) -> DrainResult:
        """Apply every queued role change once. Returns what it did."""
        if self._users is None:
            log.warning("Skipping the role sync drain: no database connection.")
            return DrainResult()

        guild = self._client.get_guild(self._config.guild_id)
        if guild is None:
            log.warning("Guild %s is not in the cache; skipping.", self._config.guild_id)
            return DrainResult()

        result = await self._drain(guild)
        self._last_completed = time.monotonic()

        # Silent when the queue was empty, which is almost every pass. A
        # line every thirty seconds saying nothing happened is how a log
        # stops being read.
        if result.examined:
            summary = asdict(result)
            log.info(
                "Role sync drain complete: %s",
                " ".join(f"{key}={value}" for key, value in summary.items()),
                extra=summary,
            )
        return result

    def _error_delay(self) -> float:
        """Return the jittered, capped backoff for the current failure run."""
        return error_backoff(
            self._consecutive_failures,
            self._config.role_sync_interval,
            DRAIN_ERROR_BACKOFF_MAX_SECONDS,
        )

    async def _drain(self, guild: Guild) -> DrainResult:
        """Walk the pending queue, one member's lock at a time."""
        channel = announcement_channel(self._client, self._config)
        # drain_once returns before it gets here when there is no
        # collection, so self._users is never None on this path. The ignore
        # states that invariant rather than adding a runtime assert for it.
        pending = iter_pending_role_syncs(self._users)  # type: ignore[arg-type]
        outcomes: Counter[Outcome] = Counter()
        examined = 0

        while True:
            batch = await asyncio.to_thread(next_batch, pending, PENDING_BATCH_SIZE)
            if not batch:
                return DrainResult(
                    examined=examined,
                    granted=outcomes["granted"],
                    removed=outcomes["removed"],
                    failed=outcomes["failed"],
                    unusable=outcomes["unusable"],
                )

            for entry in batch:
                examined += 1
                try:
                    outcomes[await self._sync_one(guild, channel, entry)] += 1
                # One unusable row must not strand the rest of the queue
                # behind it. Everything below already handles the failures
                # it expects, so anything reaching here is a surprise worth
                # a traceback, and the row keeps its flag and is retried.
                except Exception:  # pylint: disable=broad-except
                    outcomes["failed"] += 1
                    log.exception(
                        "Could not sync the role for Discord ID %s",
                        entry.get("discord_id"),
                    )

    async def _sync_one(
        self,
        guild: Guild,
        channel: TYPE_MESSAGEABLE_CHANNEL | None,
        entry: MongoDocument,
    ) -> Outcome:
        """Make Discord agree with one queued row. Returns what it did."""
        discord_id = entry.get("discord_id")
        # The row carries the star state the server recorded, and that is
        # the whole instruction: this loop never asks GitHub anything.
        starred = bool(entry.get("starred_repo"))

        if not discord_id:
            # There is nobody to move a role for, so the flag comes down
            # and the row leaves the queue. It has to come down by the
            # Mongo _id: clear_role_sync_pending is keyed on the Discord ID
            # this row does not have, and handing it the missing value
            # searched for the literal string "None". That matched nothing,
            # so the flag stayed raised, the row came back on the next poll
            # and the same line claiming it had been dropped was written
            # every thirty seconds for as long as the bot ran.
            #
            # Counted apart from a failure because it is not one. Nothing
            # was refused and there is nothing to retry, so an unusable row
            # must not push the loop into the backoff a Discord refusal
            # earns.
            #
            # These rows are commoner than they look, and it is worth being
            # accurate about where they come from rather than filing them
            # under "ancient data". From October 2023 until the rebuild,
            # /login read the Discord ID from an unvalidated query
            # parameter and stored whatever came back, so an
            # unauthenticated GET with no id, followed by OAuth, wrote a
            # null Discord ID. That is nearly three years of releases, and
            # the signed link token that closed it did not backfill what it
            # left behind. Null is therefore the shape to expect; an absent
            # key and an empty string reach here too.
            await self._clear_unusable(entry)
            return "unusable"

        # Everything below reads Discord's state, changes it and records
        # the change, which is the same sequence the sweep and the claim
        # button run against the same member. Taken here rather than around
        # the whole pass so a burst of stars is still delivered member by
        # member while a sweep works through somebody else. See
        # bot.memberlock.
        async with self._member_locks.hold(discord_id):
            member = guild.get_member(discord_id)
            if member is None:
                # A member who left the guild, treated the way _check_one
                # treats one: there is no role to move, so the row is
                # finished rather than left raised. Leaving it raised would
                # keep an entry in the partial index for somebody who is
                # not here, and every poll from now on would read it and
                # look them up again. Discord does not restore roles to
                # somebody who rejoins anyway, so the claim button and the
                # next sweep are what cover that case.
                log.info("Discord ID %s is no longer in the guild; nothing to sync.", discord_id)
                await self._clear(discord_id, starred)
                return "settled"

            if starred == member.has_role(self._config.role_id):
                # Discord already agrees. Ordinary rather than exceptional:
                # a webhook GitHub redelivered raises the flag again, and a
                # sweep can reach the same change first.
                await self._clear(discord_id, starred)
                return "settled"

            if starred:
                if not await safe_add_role(member, self._config.role_id, "star"):
                    # The flag stays up on purpose. Clearing it would drop
                    # the grant entirely, and nothing else would put it
                    # back: the sweep only ever takes roles away.
                    return "failed"
                await self._clear(discord_id, starred)
                return "granted"

            if not await safe_remove_role(member, self._config.role_id, "no_star"):
                # The same reasoning in the other direction, and the more
                # expensive one to get wrong. A failed removal that cleared
                # the flag is a role nobody takes back at all. The sweep
                # repairs deliveries that never arrived, not races, and it
                # now skips any row a webhook has newer information about,
                # so a lowered flag is the end of the matter rather than a
                # day's delay.
                return "failed"

            if not await self._clear(discord_id, starred):
                # A star event landed while the removal was in flight, so
                # the conditional clear matched nothing and the newer grant
                # is still queued for the next pass. The role change stands
                # and is reported, but the farewell does not go out: the
                # next pass hands the role straight back, and a public
                # goodbye to somebody who stars the repository would stay
                # in the channel long after they had it again. The sweep
                # learned this first; see StarChecker._record_unstarred.
                log.info(
                    "A star event overtook the drain for %s; leaving the newer state queued.",
                    discord_id,
                )
                return "removed"

            await announce_unstarred(channel, discord_id)
            return "removed"

    async def _clear_unusable(self, entry: MongoDocument) -> None:
        """Take a row nothing can act on off the queue, and say which.

        Two things set this apart from :meth:`_clear`, and neither is
        interchangeable with it. It names the row by its Mongo ``_id``,
        because the Discord ID that method needs is the very thing this row
        is missing. And it is unconditional, where that method guards on
        the star state the bot acted on: the guard is there to keep a
        webhook that landed mid-call from being unqueued, and nothing here
        talked to Discord about a member that does not exist.

        Warned rather than dropped quietly. This deletes queued work, and
        the row is the only evidence that a link nobody can use is sitting
        in the collection, so the line has to carry enough to find it
        again by hand.
        """
        log.warning(
            "A queued role sync has no Discord ID; clearing it. Mongo _id %r, "
            "GitHub account %r. There is no member to move a role for. Rows like "
            "this were left by the releases where /login took the Discord ID from "
            "an unvalidated query parameter.",
            entry.get("_id"),
            entry.get("github_username") or entry.get("github_id"),
        )
        try:
            # Reached only from _drain, so self._users is not None here; see
            # the note on the iter_pending_role_syncs call there.
            await asyncio.to_thread(
                clear_role_sync_pending_by_id,
                self._users,  # type: ignore[arg-type]
                entry.get("_id"),
            )
        except PyMongoError as exc:
            # Nothing was going to happen to this row anyway, so a failed
            # clear costs only the same line again on the next poll.
            log.error("Could not clear the unusable pending role sync: %s", exc)

    async def _clear(self, discord_id: object, starred: bool) -> bool:
        """Take a row off the pending queue, now that Discord matches it.

        ``starred`` is passed through because the clear is conditional on
        the row still saying it; see :func:`clear_role_sync_pending` for the
        lost update that guards against.

        Returns whether the clear landed, which is the caller's only way to
        learn that a webhook overtook it and the row is still queued. The
        paths that have nothing to say either way ignore the answer.
        """
        try:
            # Reached only from _drain, so self._users is not None here; see
            # the note on the iter_pending_role_syncs call there.
            return await asyncio.to_thread(
                clear_role_sync_pending,
                self._users,  # type: ignore[arg-type]
                discord_id,
                starred,
            )
        except PyMongoError as exc:
            # The role is already right and only the bookkeeping failed, so
            # carrying on costs nothing: the next poll reads the row again,
            # finds Discord agrees with it and clears it then.
            #
            # Reported as landed on purpose. Nothing was refused, so this
            # is no evidence that a newer star event exists, and reading a
            # failed write as one would suppress a farewell that is owed.
            log.error("Could not clear the pending role sync for %s: %s", discord_id, exc)
            return True
