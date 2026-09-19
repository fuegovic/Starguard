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
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Final, Literal

from interactions import TYPE_MESSAGEABLE_CHANNEL, Client, Guild
from pymongo.errors import PyMongoError

from bot.config import BotConfig
from bot.roles import safe_add_role, safe_remove_role
from bot.starcheck import announce_unstarred, announcement_channel, error_backoff, next_batch
from common.storage import (
    MongoDocument,
    UserCollection,
    clear_role_sync_pending,
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
Outcome = Literal["granted", "removed", "settled", "failed"]


@dataclass(frozen=True)
class DrainResult:
    """What one pass over the pending queue did.

    ``settled`` rows are deliberately absent: a row Discord already agreed
    with is the ordinary case (a redelivered webhook, or a star the sweep
    reached first) and counting it would only make an uneventful pass look
    busy. ``examined`` minus the other three is how many there were.
    """

    examined: int = 0
    granted: int = 0
    removed: int = 0
    failed: int = 0


class RoleSyncDrainer:
    """Applies the role changes the star webhook queued in the database."""

    def __init__(
        self,
        client: Client,
        config: BotConfig,
        users: UserCollection | None,
        lock: asyncio.Lock,
    ) -> None:
        self._client = client
        self._config = config
        self._users = users
        # Not a lock of this drain's own: it is the star check's, handed
        # over by bot.create_client. See StarChecker.lock for why a second
        # mutex here would be no exclusion at all.
        self._lock = lock
        self._consecutive_failures = 0

    async def run_forever(self) -> None:
        """Drain the queue on an interval, forever.

        Errors are caught and logged rather than allowed to escape. The
        check loop learned this the hard way: an unhandled exception killed
        the task silently, and nothing ran again until the bot restarted.
        Here that would mean roles that never move, with a webhook the
        operator can see being delivered successfully.
        """
        while True:
            delay: float
            try:
                await self.drain_once()
                self._consecutive_failures = 0
                delay = self._config.role_sync_interval
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
            await asyncio.sleep(delay)

    async def drain_once(self) -> DrainResult:
        """Apply every queued role change once. Returns what it did."""
        if self._users is None:
            log.warning("Skipping the role sync drain: no database connection.")
            return DrainResult()

        guild = self._client.get_guild(self._config.guild_id)
        if guild is None:
            log.warning("Guild %s is not in the cache; skipping.", self._config.guild_id)
            return DrainResult()

        # The lock is taken around the work and nothing else, so an idle
        # poll does not make /checkstars wait behind it.
        async with self._lock:
            result = await self._drain(guild)

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
        """Walk the pending queue, always under the shared lock."""
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
            # There is nobody to move a role for. The flag comes down all
            # the same, because leaving it up would have this same unusable
            # row read, logged and skipped on every poll from now on. Only
            # rows written by the oldest version, keyed on a GitHub email,
            # can look like this.
            log.warning("A queued role sync has no Discord ID; dropping it.")
            await self._clear(discord_id, starred)
            return "failed"

        member = guild.get_member(discord_id)
        if member is None:
            # A member who left the guild, treated the way _check_one
            # treats one: there is no role to move, so the row is finished
            # rather than left raised. Leaving it raised would keep an entry
            # in the partial index for somebody who is not here, and every
            # poll from now on would read it and look them up again. Discord
            # does not restore roles to somebody who rejoins anyway, so the
            # claim button and the next sweep are what cover that case.
            log.info("Discord ID %s is no longer in the guild; nothing to sync.", discord_id)
            await self._clear(discord_id, starred)
            return "settled"

        if starred == member.has_role(self._config.role_id):
            # Discord already agrees. Ordinary rather than exceptional: a
            # webhook GitHub redelivered raises the flag again, and a sweep
            # can reach the same change first.
            await self._clear(discord_id, starred)
            return "settled"

        if starred:
            if not await safe_add_role(member, self._config.role_id, "star"):
                # The flag stays up on purpose. Clearing it would drop the
                # grant entirely, and nothing else would put it back: the
                # sweep only ever takes roles away.
                return "failed"
            await self._clear(discord_id, starred)
            return "granted"

        if not await safe_remove_role(member, self._config.role_id, "no_star"):
            # The same reasoning in the other direction, and the more
            # expensive one to get wrong. A failed removal that cleared the
            # flag is a role nobody takes back at all. The sweep repairs
            # deliveries that never arrived, not races, and it now skips
            # any row a webhook has newer information about, so a lowered
            # flag is the end of the matter rather than a day's delay.
            return "failed"

        await self._clear(discord_id, starred)
        await announce_unstarred(channel, discord_id)
        return "removed"

    async def _clear(self, discord_id: object, starred: bool) -> None:
        """Take a row off the pending queue, now that Discord matches it.

        ``starred`` is passed through because the clear is conditional on
        the row still saying it; see :func:`clear_role_sync_pending` for the
        lost update that guards against.
        """
        try:
            # Reached only from _drain, so self._users is not None here; see
            # the note on the iter_pending_role_syncs call there.
            await asyncio.to_thread(
                clear_role_sync_pending,
                self._users,  # type: ignore[arg-type]
                discord_id,
                starred,
            )
        except PyMongoError as exc:
            # The role is already right and only the bookkeeping failed, so
            # carrying on costs nothing: the next poll reads the row again,
            # finds Discord agrees with it and clears it then.
            log.error("Could not clear the pending role sync for %s: %s", discord_id, exc)
