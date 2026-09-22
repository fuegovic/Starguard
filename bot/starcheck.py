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

from bot.config import BotConfig
from bot.memberlock import MemberLocks
from bot.messages import SORRY
from bot.roles import safe_remove_role
from common.github_api import (
    GitHubError,
    StargazerCache,
    StargazerListing,
    account_stars_repo,
    fetch_stargazer_listing,
)
from common.storage import (
    MongoDocument,
    UserCollection,
    find_link,
    iter_links,
    queue_role_sync,
    set_starred,
    star_event_is_newer,
)
from common.storage_errors import StorageError, StorageUnavailableError

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

    # Four collaborators it is handed and four pieces of state a cycle
    # leaves behind, one over the default. Splitting it would put the cycle
    # lock and the ETag cache it protects into different objects, which is
    # how the two get out of step.
    # pylint: disable=too-many-instance-attributes

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
        # One cycle at a time. /checkstars called straight into the check
        # while the timer loop could already be inside it, so the same role
        # was removed twice and the same "sorry to see you go" message was
        # posted twice.
        #
        # Cycle serialisation and nothing else. This used to be handed to
        # the role-sync drain as well, on the reasoning that one mutex is
        # the only way two loops can be kept off one member, and that
        # conflated two jobs: it is held across the stargazer crawl and the
        # whole sweep, so it parked every queued webhook behind a cycle
        # that takes minutes. Keeping two components off one member is now
        # member_locks' job, which is the granularity that question
        # actually has. See bot.memberlock.
        self._lock = asyncio.Lock()
        # Shared with the drain and the claim button, which is what makes
        # it exclusion rather than three private mutexes.
        self._member_locks = member_locks
        # Owned by this checker and only ever touched under the lock above,
        # which is what makes it safe to keep across cycles.
        self._cache = StargazerCache()
        self._consecutive_failures = 0
        self._last_completed: float | None = None

    @property
    def running(self) -> bool:
        """Whether a check cycle is in progress or waiting for its turn.

        The lock answers this on its own now that only cycles take it. It
        could not while the role-sync drain held the same mutex: a drain is
        not a star check, and /checkstars telling an administrator one was
        running because a drain happened to be in progress was a sentence
        they could do nothing with.
        """
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

        A drain in progress is not that and never reaches here, because it
        no longer takes this lock at all.
        """
        # Acquiring is synchronous when the lock is free, so there is no
        # window between this test and taking it in which another caller
        # could slip past on a single event loop.
        if not wait and self.running:
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
                try:
                    name = await self._check_one(guild, channel, entry, listing, observed_at)
                # The rule the drain already follows (see rolesync._drain),
                # and it matters more here. The drain leaves a failed row's
                # flag raised and comes back to it; the sweep has no queue,
                # it walks iter_links from the beginning every cycle. So a
                # single row that raises did not cost one member a check, it
                # aborted _run_cycle, and run_forever's retry started the
                # same walk and reached the same row again. Nobody after it
                # in the listing was ever checked again, and the only signal
                # was a repeating traceback in the log.
                # An unreachable database is not one bad row, and treating
                # it as one is worse than the crash this guard replaced. Every
                # remaining entry would raise the same way, the cycle would
                # still report success, _last_completed would be stamped and
                # the health socket would keep saying the loop is fresh, all
                # while nobody had been checked. Let it out: run_forever logs
                # it, backs off and retries, which is the behaviour that
                # matches what actually happened.
                except StorageUnavailableError:
                    log.error(
                        "The database became unreachable %s link(s) into the walk; "
                        "abandoning this cycle rather than reporting it complete.",
                        examined,
                    )
                    raise
                # The rule the drain already follows, for everything else.
                except Exception:  # pylint: disable=broad-except
                    log.exception(
                        "Could not check the star for Discord ID %s",
                        entry.get("discord_id"),
                    )
                    continue
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

        # Absence from a listing GitHub cut short says nothing, so the
        # account is asked about directly before anything is taken away.
        if listing.truncated and await self._stars_beyond_listing(entry):
            return None

        # The account _still_stars just ruled on, kept from the row as this
        # cycle read it. Everything below can wait on a lock, and the row
        # can change account while it waits, so the write has to say which
        # account this answer was about rather than trusting the row to
        # still name it.
        judged_account = entry.get("github_id")

        # Everything below reads Discord's state, changes it and records
        # the change, which is the same sequence the drain and the claim
        # button run against the same member. Held for one member rather
        # than for the cycle, so a queued webhook about anybody else is not
        # waiting on the crawl. See bot.memberlock.
        async with self._member_locks.hold(discord_id):
            # Ask the freshness question again now the lock is held. It was
            # answered above against the row as the cycle read it, before
            # the wait, and a drain holding this lock in the meantime may
            # have granted the role from an event newer than this listing.
            # Removing it on the strength of the older row would take back
            # a role the webhook had just earned. The write below refuses
            # the stale state and requeues the correction, so the database
            # is never wrong, but the member loses role-backed access until
            # another drain pass returns it. Locking the change without
            # locking the decision that drives it is what left that gap.
            #
            # A row deleted while this waited has nothing left to act on.
            # Reached only from _sweep, so self._users is not None here; see
            # the note on the iter_links call there.
            current = await asyncio.to_thread(
                find_link,
                self._users,  # type: ignore[arg-type]
                discord_id,
            )
            if current is None or star_event_is_newer(current, observed_at):
                log.debug(
                    "Discord ID %s changed while this cycle waited; leaving them to the drain.",
                    discord_id,
                )
                return None

            # The same question about identity rather than freshness, and it
            # has to be asked separately because a re-link is invisible to
            # the one above: link_account clears star_event_at when the
            # account changes, so the row comes back looking untouched by
            # anything newer than this listing. The decision waiting to be
            # acted on is about the account this cycle judged, and this row
            # is no longer that account.
            #
            # set_starred refuses the write for the same reason, so the
            # database was never going to be wrong either way. What it
            # cannot undo is the role removal, which happens first: without
            # this the member is stripped on the strength of an answer about
            # an account they have left, and gets it back a drain interval
            # later through queue_role_sync. Declining here is what keeps
            # that interval from happening at all. A legacy row that gained
            # an account while this waited compares unequal too, which is
            # the same situation and wants the same answer.
            if current.get("github_id") != judged_account:
                log.debug(
                    "Discord ID %s re-linked while this cycle waited; "
                    "the listing says nothing about their new account.",
                    discord_id,
                )
                return None

            # A member who left the guild returns None here. Calling
            # has_role on that used to raise and take the whole loop down
            # with it.
            member = guild.get_member(discord_id)
            if member is None:
                log.info(
                    "Discord ID %s is no longer in the guild; marking un-starred.",
                    discord_id,
                )
                await self._record_unstarred(discord_id, observed_at, judged_account)
                return None

            if not member.has_role(self._config.role_id):
                await self._record_unstarred(discord_id, observed_at, judged_account)
                return None

            if not await safe_remove_role(member, self._config.role_id, "no_star"):
                return None

            if not await self._record_unstarred(discord_id, observed_at, judged_account):
                # A newer star event overtook this cycle, so the row has
                # been queued and the drain hands the role back within the
                # poll interval. This cycle therefore has nothing true to
                # say about this member: a farewell to somebody who stars
                # the repository would stay in the channel long after the
                # role came back, and counting them as removed would have
                # /checkstars report a loss to an admin about a member who
                # holds the role.
                return None

            await announce_unstarred(channel, discord_id)

            return _display_name(entry, member)

    async def _stars_beyond_listing(self, entry: MongoDocument) -> bool:
        """Whether ``entry``'s account stars the repository, asked directly.

        Only called for an account a truncated listing does not show. A
        lookup that fails answers True, meaning "leave the role alone":
        failing to find out is not finding out that somebody un-starred.
        """
        github_id = entry.get("github_id")
        login = entry.get("github_username")
        try:
            return await asyncio.to_thread(
                account_stars_repo,
                self._config.owner,
                self._config.repo,
                github_id=github_id if isinstance(github_id, int) else None,
                login=login if isinstance(login, str) else None,
                token=self._config.github_token,
            )
        except GitHubError as exc:
            log.warning(
                "Could not look up the star for Discord ID %s directly; leaving the role: %s",
                entry.get("discord_id"),
                exc,
            )
            return True

    async def _record_unstarred(
        self, discord_id: object, observed_at: datetime, github_id: object
    ) -> bool:
        """Persist that ``discord_id`` no longer stars the repository.

        Returns False when a star event overtook this cycle, which is the
        caller's signal that the un-star is not this cycle's to report.

        ``github_id`` is the account this cycle actually judged, taken from
        the row as it was read rather than from the row as it stands now.
        The write is refused if the member has re-linked since, because the
        answer being recorded is about the account they left.
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
                github_id,
            )
        # Ahead of the StorageError below, which it is a subclass of, or the
        # outage is consumed here and the cycle carries on reporting
        # removals it cannot record. _sweep's guard never sees it, because
        # this is where the walk actually touches the database on most rows.
        except StorageUnavailableError:
            raise
        except StorageError as exc:
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
        # Same reason as above: an unreachable database is the cycle's
        # problem, not this row's.
        except StorageUnavailableError:
            raise
        except StorageError as exc:
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
            "listing_truncated": listing.truncated,
            "duration_seconds": round(duration, 2),
        }
        log.info(
            "Star check complete: %s",
            " ".join(f"{key}={value}" for key, value in summary.items()),
            extra=summary,
        )
