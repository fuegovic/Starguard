"""The periodic un-star check.

Blocking work (HTTP calls to GitHub, the synchronous pymongo driver) runs in a
worker thread via ``asyncio.to_thread`` so it never stalls the gateway
heartbeat.
"""

import asyncio
import logging
import random
import time
from collections.abc import Iterator, Mapping
from itertools import islice
from typing import Final, cast

from interactions import (
    TYPE_MESSAGEABLE_CHANNEL,
    Client,
    Guild,
    Member,
)
from interactions.client.errors import Forbidden, HTTPException
from pymongo.errors import PyMongoError

from bot.config import BotConfig
from bot.messages import SORRY
from bot.roles import safe_remove_role
from common.github_api import StargazerCache, StargazerListing, fetch_stargazer_listing
from common.storage import MongoDocument, UserCollection, iter_links, set_starred

log = logging.getLogger("starguard.bot")

# How long to wait before retrying after the check loop raises, and how far
# that wait is allowed to grow. A flat 60 seconds meant a GitHub outage was
# met with a request a minute for as long as it lasted.
LOOP_ERROR_BACKOFF_SECONDS: Final = 60
LOOP_ERROR_BACKOFF_MAX_SECONDS: Final = 1800

# Retries are jittered because several deployments watching the same
# repository would otherwise come back in lockstep and arrive together.
LOOP_ERROR_BACKOFF_JITTER: Final = 0.25

# Documents pulled from the cursor per thread hop. Small enough that memory
# does not grow with the number of verified members, large enough that the
# cursor is not left idle for long between getMore calls.
LINK_BATCH_SIZE: Final = 200


# The Error suffix pep8-naming asks for would be the better name, but this
# one is already imported by bot.commands and by the test suite, so renaming
# it is a change of its own rather than part of adding annotations.
class CheckAlreadyRunningError(RuntimeError):
    """Raised when a check is requested while one is already in progress."""


def _next_batch(links: Iterator[MongoDocument], size: int) -> list[MongoDocument]:
    """Pull up to ``size`` documents off ``links``. Runs in a worker thread."""
    return list(islice(links, size))


def _display_name(entry: Mapping[str, object], member: Member) -> str:
    """Return the name to report for a member who lost the role."""
    return str(entry.get("discord_username") or member.display_name).lstrip("@")


class StarChecker:
    """Strips the role from linked users who no longer star the repository."""

    def __init__(self, client: Client, config: BotConfig, users: UserCollection | None) -> None:
        self._client = client
        self._config = config
        self._users = users
        # One cycle at a time. /checkstars called straight into the check
        # while the timer loop could already be inside it, so the same role
        # was removed twice and the same "sorry to see you go" message was
        # posted twice.
        self._lock = asyncio.Lock()
        # Owned by this checker and only ever touched under the lock above,
        # which is what makes it safe to keep across cycles.
        self._cache = StargazerCache()
        self._consecutive_failures = 0
        self._last_completed: float | None = None

    @property
    def running(self) -> bool:
        """Whether a cycle is in progress right now."""
        return self._lock.locked()

    @property
    def last_completed(self) -> float | None:
        """``time.monotonic()`` of the last cycle that finished, or None."""
        return self._last_completed

    async def run_once(self, wait: bool = True) -> list[str]:
        """Run one cycle and return the display names that lost the role.

        With ``wait=False`` a cycle that is already running raises
        :class:`CheckAlreadyRunningError` instead of queueing behind it. That is
        what /checkstars wants: a cycle over a repository with many
        stargazers can take minutes, an interaction token is only good for
        fifteen, and the answer the user asked for is being produced right
        now anyway.
        """
        # There is no await between this test and taking the lock, so on a
        # single event loop the pair cannot interleave with another caller.
        if not wait and self._lock.locked():
            raise CheckAlreadyRunningError("A star check is already running.")

        async with self._lock:
            return await self._run_cycle()

    async def run_forever(self) -> None:
        """Re-check every linked user on an interval, forever.

        Errors are caught and logged rather than allowed to escape: an
        unhandled exception here used to kill the task silently, and automatic
        checks would never run again until the bot was restarted.
        """
        while True:
            delay: float
            try:
                await self.run_once()
                self._consecutive_failures = 0
                delay = self._config.check_delay
            # CancelledError derives from BaseException, so cancellation still
            # propagates out of this handler and stops the loop.
            except Exception:  # pylint: disable=broad-except
                self._consecutive_failures += 1
                delay = self._error_delay()
                log.exception(
                    "Automatic star check failed (%s in a row); retrying in %.0f seconds",
                    self._consecutive_failures,
                    delay,
                )
            await asyncio.sleep(delay)

    def _error_delay(self) -> float:
        """Return the jittered, capped backoff for the current failure run."""
        # The exponent is capped before the shift so a long outage cannot
        # build an enormous integer on the way to min().
        ceiling: float = LOOP_ERROR_BACKOFF_SECONDS * 2 ** min(self._consecutive_failures - 1, 10)
        base = min(ceiling, LOOP_ERROR_BACKOFF_MAX_SECONDS)
        # B311: jitter that spreads deployments out, not a secret.
        return base * random.uniform(  # nosec B311
            1 - LOOP_ERROR_BACKOFF_JITTER, 1 + LOOP_ERROR_BACKOFF_JITTER
        )

    async def _run_cycle(self) -> list[str]:
        """The body of one cycle, always under the lock."""
        started = time.monotonic()

        if self._users is None:
            log.warning("Skipping star check: no database connection.")
            return []

        listing = await asyncio.to_thread(
            fetch_stargazer_listing,
            self._config.owner,
            self._config.repo,
            token=self._config.github_token,
            cache=self._cache,
        )

        guild = self._client.get_guild(self._config.guild_id)
        if guild is None:
            log.warning("Guild %s is not in the cache; skipping.", self._config.guild_id)
            return []

        examined, removed = await self._sweep(guild, listing.logins)
        self._last_completed = time.monotonic()
        self._log_summary(listing, examined, removed, time.monotonic() - started)
        return removed

    async def _sweep(self, guild: Guild, stargazers: frozenset[str]) -> tuple[int, list[str]]:
        """Walk the links, removing the role where the star is gone."""
        # CHANNEL_ID names the announcement channel, so what comes back is
        # something that can be posted to. get_channel is typed as any
        # channel at all, including the kinds that have no send(), hence the
        # cast rather than an isinstance check that would quietly change
        # what a misconfigured CHANNEL_ID does.
        channel = cast(
            "TYPE_MESSAGEABLE_CHANNEL | None",
            self._client.get_channel(self._config.channel_id),
        )
        # _run_cycle returns before it gets here when there is no collection,
        # so self._users is never None on this path. The ignore states that
        # invariant rather than adding a runtime assert for it.
        links = iter_links(self._users)  # type: ignore[arg-type]
        examined = 0
        removed: list[str] = []

        while True:
            batch = await asyncio.to_thread(_next_batch, links, LINK_BATCH_SIZE)
            if not batch:
                return examined, removed

            for entry in batch:
                examined += 1
                name = await self._check_one(guild, channel, entry, stargazers)
                if name is not None:
                    removed.append(name)

    async def _check_one(
        self,
        guild: Guild,
        channel: TYPE_MESSAGEABLE_CHANNEL | None,
        entry: MongoDocument,
        stargazers: frozenset[str],
    ) -> str | None:
        """Handle one link. Returns a display name when the role was taken."""
        username = (
            entry.get("github_username_lower") or (entry.get("github_username") or "").lower()
        )
        if not username or username in stargazers:
            return None

        discord_id = entry.get("discord_id")
        if not discord_id:
            return None

        # A member who left the guild returns None here. Calling has_role on
        # that used to raise and take the whole loop down with it.
        member = guild.get_member(discord_id)
        if member is None:
            log.info(
                "Discord ID %s is no longer in the guild; marking un-starred.",
                discord_id,
            )
            await self._record_unstarred(discord_id)
            return None

        if not member.has_role(self._config.role_id):
            await self._record_unstarred(discord_id)
            return None

        if not await safe_remove_role(member, self._config.role_id, "no_star"):
            return None

        await self._record_unstarred(discord_id)

        if channel is not None:
            try:
                # B311: picks a farewell message, not a secret.
                await channel.send(content=random.choice(SORRY).format(discord_id))  # nosec B311
            except (Forbidden, HTTPException) as exc:
                log.warning("Could not post to the announcement channel: %s", exc)

        return _display_name(entry, member)

    async def _record_unstarred(self, discord_id: object) -> None:
        """Persist that ``discord_id`` no longer stars the repository."""
        try:
            # Reached only from _sweep, so self._users is not None here; see
            # the note on the iter_links call there.
            await asyncio.to_thread(
                set_starred,
                self._users,  # type: ignore[arg-type]
                discord_id,
                False,
            )
        except PyMongoError as exc:
            log.error("Could not update star state for %s: %s", discord_id, exc)

    def _log_summary(
        self,
        listing: StargazerListing,
        examined: int,
        removed: list[str],
        duration: float,
    ) -> None:
        """Emit one line per cycle saying what it did and what it cost."""
        # Built once and used twice: formatted into the text line so a person
        # reading a terminal sees the numbers, and passed as extra fields so
        # the JSON output carries them typed rather than embedded in a string.
        summary: dict[str, object] = {
            "examined": examined,
            "roles_removed": len(removed),
            "api_calls": listing.api_calls,
            "pages_fetched": listing.pages_fetched,
            "pages_unchanged": listing.pages_unchanged,
            "rate_limit_remaining": listing.rate_limit_remaining,
            "duration_seconds": round(duration, 2),
        }
        log.info(
            "Star check complete: %s",
            " ".join(f"{key}={value}" for key, value in summary.items()),
            extra=summary,
        )
