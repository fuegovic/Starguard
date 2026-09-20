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
from datetime import UTC, datetime
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
from common.storage import (
    MongoDocument,
    UserCollection,
    iter_links,
    queue_role_sync,
    set_starred,
    star_event_is_newer,
)

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


def next_batch(documents: Iterator[MongoDocument], size: int) -> list[MongoDocument]:
    """Pull up to ``size`` documents off ``documents``. Runs in a worker thread.

    Public because the role-sync drain walks its own cursor the same way and
    for the same reason: pymongo is synchronous, so every getMore has to
    happen off the event loop or the gateway heartbeat stalls behind it.
    """
    return list(islice(documents, size))


def announcement_channel(client: Client, config: BotConfig) -> TYPE_MESSAGEABLE_CHANNEL | None:
    """Return the channel star changes are announced in, if it is cached.

    CHANNEL_ID names the announcement channel, so what comes back is
    something that can be posted to. get_channel is typed as any channel at
    all, including the kinds that have no send(), hence the cast rather than
    an isinstance check that would quietly change what a misconfigured
    CHANNEL_ID does.
    """
    return cast("TYPE_MESSAGEABLE_CHANNEL | None", client.get_channel(config.channel_id))


async def announce_unstarred(channel: TYPE_MESSAGEABLE_CHANNEL | None, discord_id: object) -> None:
    """Post the farewell for ``discord_id``, where there is anywhere to post.

    Shared with the role-sync drain so a role taken back by a webhook reads
    exactly like one taken back by the sweep. A channel the bot may not post
    in is logged and shrugged off: the role change has already happened and
    must not be undone by the announcement failing.
    """
    if channel is None:
        return
    try:
        # B311: picks a farewell message, not a secret.
        await channel.send(content=random.choice(SORRY).format(discord_id))  # nosec B311
    except (Forbidden, HTTPException) as exc:
        log.warning("Could not post to the announcement channel: %s", exc)


def error_backoff(consecutive_failures: int, base_seconds: float, max_seconds: float) -> float:
    """Return the jittered, capped wait after ``consecutive_failures`` failures.

    Shared by both background loops, which want the same shape of retry with
    different ceilings: an hourly sweep can afford to wait half an hour out,
    a queue of role changes cannot.
    """
    # The exponent is capped before the shift so a long outage cannot build
    # an enormous integer on the way to min().
    ceiling: float = base_seconds * 2 ** min(consecutive_failures - 1, 10)
    base = min(ceiling, max_seconds)
    # B311: jitter that spreads deployments out, not a secret.
    return base * random.uniform(  # nosec B311
        1 - LOOP_ERROR_BACKOFF_JITTER, 1 + LOOP_ERROR_BACKOFF_JITTER
    )


def _display_name(entry: Mapping[str, object], member: Member) -> str:
    """Return the name to report for a member who lost the role."""
    return str(entry.get("discord_username") or member.display_name).lstrip("@")


def _still_stars(entry: MongoDocument, listing: StargazerListing) -> bool:
    """Whether ``entry`` still matches somebody in the stargazer listing.

    The match is on ``github_id``, GitHub's immutable account number. This
    used to compare the stored login against the listing's logins, and a
    login is not immutable: anybody who renamed their GitHub account matched
    nothing in the next listing, so the check concluded they had un-starred
    and took the role from them.

    Documents written by much older versions have no ``github_id``, and one
    cannot be derived from a login without another API call, so those fall
    back to the login comparison. A missing id is deliberately not read as
    "not starred", which would strip the role from every one of those rows
    at once.
    """
    github_id = entry.get("github_id")
    if github_id is not None:
        # The id is load-bearing on its own; the stored login is whatever
        # spelling was current when the link was made and is ignored here.
        return github_id in listing.ids

    username = entry.get("github_username_lower") or (entry.get("github_username") or "").lower()
    # A row with neither an id nor a login gives no evidence either way, and
    # taking a role on no evidence is the failure mode worth avoiding.
    return not username or username in listing.logins


class StarChecker:
    """Strips the role from linked users who no longer star the repository."""

    # Three collaborators it is handed and five pieces of state a cycle
    # leaves behind, one over the default. Splitting it would put the lock,
    # the count of cycles it guards and the ETag cache it protects into
    # different objects, which is how two of them get out of step.
    # pylint: disable=too-many-instance-attributes

    def __init__(self, client: Client, config: BotConfig, users: UserCollection | None) -> None:
        self._client = client
        self._config = config
        self._users = users
        # One cycle at a time. /checkstars called straight into the check
        # while the timer loop could already be inside it, so the same role
        # was removed twice and the same "sorry to see you go" message was
        # posted twice.
        self._lock = asyncio.Lock()
        # How many cycles are running or queued behind the lock. Counted
        # separately from the lock because the role-sync drain holds this
        # same lock, and testing the lock made /checkstars tell an
        # administrator a star check was already running when nothing but a
        # drain was in progress. A count rather than a flag so two callers
        # cannot clear each other's: the timer loop waits for its turn where
        # /checkstars refuses to, and both are cycles.
        self._cycles_in_flight = 0
        # Owned by this checker and only ever touched under the lock above,
        # which is what makes it safe to keep across cycles.
        self._cache = StargazerCache()
        self._consecutive_failures = 0
        self._last_completed: float | None = None

    @property
    def running(self) -> bool:
        """Whether a check cycle is in progress or waiting for its turn.

        Not the same question as whether :attr:`lock` is held, because the
        role-sync drain holds that lock too. A drain is not a star check,
        and answering /checkstars as though it were told the administrator
        something that was not true.
        """
        return self._cycles_in_flight > 0

    @property
    def lock(self) -> asyncio.Lock:
        """The mutex that serialises every role change this process makes.

        Handed to the role-sync drain so the sweep and the drain take turns
        over the same members. A lock of the drain's own would be no
        exclusion at all: two mutexes held independently let both loops into
        the same member at once, which is precisely the double role change
        and double announcement this one was added to stop.
        """
        return self._lock

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

        A drain holding the shared lock is not that, and does not raise. It
        finishes in well under an interaction's lifetime, so waiting for it
        is the honest answer where claiming a check was running was not.
        """
        # There is no await between this test and the increment, so on a
        # single event loop the pair cannot interleave with another caller.
        if not wait and self.running:
            raise CheckAlreadyRunningError("A star check is already running.")

        self._cycles_in_flight += 1
        try:
            async with self._lock:
                return await self._run_cycle()
        finally:
            self._cycles_in_flight -= 1

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
        return error_backoff(
            self._consecutive_failures,
            LOOP_ERROR_BACKOFF_SECONDS,
            LOOP_ERROR_BACKOFF_MAX_SECONDS,
        )

    async def _run_cycle(self) -> list[str]:
        """The body of one cycle, always under the lock."""
        started = time.monotonic()

        if self._users is None:
            log.warning("Skipping star check: no database connection.")
            return []

        # Taken before the first page is requested, not after the last one.
        # The listing is assembled over minutes on a popular repository, so
        # a star event during the crawl may or may not be in it; the earlier
        # instant makes that whole ambiguous window count as newer than the
        # listing, which is the side to be wrong on. Everything this cycle
        # writes is conditional on it. See common.storage.set_starred.
        observed_at = datetime.now(UTC)

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

        examined, removed = await self._sweep(guild, listing, observed_at)
        self._last_completed = time.monotonic()
        self._log_summary(listing, examined, removed, time.monotonic() - started)
        return removed

    async def _sweep(
        self, guild: Guild, listing: StargazerListing, observed_at: datetime
    ) -> tuple[int, list[str]]:
        """Walk the links, removing the role where the star is gone."""
        channel = announcement_channel(self._client, self._config)
        # _run_cycle returns before it gets here when there is no collection,
        # so self._users is never None on this path. The ignore states that
        # invariant rather than adding a runtime assert for it.
        links = iter_links(self._users)  # type: ignore[arg-type]
        examined = 0
        removed: list[str] = []

        while True:
            batch = await asyncio.to_thread(next_batch, links, LINK_BATCH_SIZE)
            if not batch:
                return examined, removed

            for entry in batch:
                examined += 1
                name = await self._check_one(guild, channel, entry, listing, observed_at)
                if name is not None:
                    removed.append(name)

    async def _check_one(
        self,
        guild: Guild,
        channel: TYPE_MESSAGEABLE_CHANNEL | None,
        entry: MongoDocument,
        listing: StargazerListing,
        observed_at: datetime,
    ) -> str | None:
        """Handle one link. Returns a display name when the role was taken."""
        # Checked before anything else, including the listing comparison: a
        # star event recorded after this cycle's listing was taken is newer
        # than the listing by construction, so this cycle has nothing to say
        # about the member. Acting anyway would take a role the webhook had
        # just earned them and then write over the record of it.
        if star_event_is_newer(entry, observed_at):
            log.debug(
                "Discord ID %s changed after the listing was taken; leaving them to the drain.",
                entry.get("discord_id"),
            )
            return None

        if _still_stars(entry, listing):
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
            await self._record_unstarred(discord_id, observed_at)
            return None

        if not member.has_role(self._config.role_id):
            await self._record_unstarred(discord_id, observed_at)
            return None

        if not await safe_remove_role(member, self._config.role_id, "no_star"):
            return None

        if not await self._record_unstarred(discord_id, observed_at):
            # A newer star event overtook this cycle, so the row has been
            # queued and the drain hands the role back within the poll
            # interval. This cycle therefore has nothing true to say about
            # this member: a farewell to somebody who stars the repository
            # would stay in the channel long after the role came back, and
            # counting them as removed would have /checkstars report a loss
            # to an admin about a member who holds the role.
            return None

        await announce_unstarred(channel, discord_id)

        return _display_name(entry, member)

    async def _record_unstarred(self, discord_id: object, observed_at: datetime) -> bool:
        """Persist that ``discord_id`` no longer stars the repository.

        Returns False when a star event overtook this cycle, which is the
        caller's signal that the un-star is not this cycle's to report.
        """
        try:
            # Reached only from _sweep, so self._users is not None here; see
            # the note on the iter_links call there.
            landed = await asyncio.to_thread(
                set_starred,
                self._users,  # type: ignore[arg-type]
                discord_id,
                False,
                observed_at,
            )
        except PyMongoError as exc:
            log.error("Could not update star state for %s: %s", discord_id, exc)
            # Only the bookkeeping failed. The role really is gone and
            # nothing newer is known about this member, so the cycle still
            # reports the removal it actually made.
            return True

        if landed:
            return True

        # A webhook wrote a newer observation in the window between the
        # freshness check above and this write, so the write matched
        # nothing and the row keeps the newer fact. Saying so makes a
        # race that is otherwise invisible show up in the log.
        log.info(
            "A star event overtook the check for %s; leaving the newer state alone.",
            discord_id,
        )
        # The role change has already committed to Discord, on information
        # this refusal proves was stale, so this cycle owes the row a
        # reconciliation. See queue_role_sync for why the same event does
        # not queue anything on its way in.
        try:
            await asyncio.to_thread(
                queue_role_sync,
                self._users,  # type: ignore[arg-type]
                discord_id,
            )
        except PyMongoError as exc:
            # Caught apart from the write above, and deliberately not read
            # as "the removal stands". This failure leaves the role removed
            # from somebody the row says stars the repository, with nothing
            # queued to put it back and no later sweep that would: a sweep
            # only ever takes roles away. Returning True here announced a
            # farewell to that member and reported the loss to the
            # administrator, both about a state the database contradicts.
            log.error(
                "Could not queue the role sync for %s after a refused write; "
                "their role stays removed until they claim it again: %s",
                discord_id,
                exc,
            )
        return False

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
