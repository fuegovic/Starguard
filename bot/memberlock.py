"""Per-member exclusion for the three things in this process that move a role.

Three components change the same role on the same member: the hourly sweep,
the webhook drain and the claim button. Each of them reads a row, decides
from it, talks to Discord and writes the row back, so any two of them
running at once on one member interleave a read against somebody else's
write. That is the bug class this project keeps rediscovering, most
recently as a claim that removed the role from under a re-star the drain
had already settled.

One mutex for all three was the previous answer, and the mutex was the star
check's own cycle lock. It did keep the sweep and the drain apart, but at
the wrong granularity and over the wrong span. That lock is held across
``fetch_stargazer_listing`` and the whole member sweep, which is minutes on
a repository with 45,000 stars, so a queued webhook waited out an entire
cycle whichever member it was about, and ROLE_SYNC_INTERVAL, which
defaults to thirty seconds and floors at five, described nothing that
happens. A claim button made to wait the same way is a person watching a
spinner for minutes.

So the one mutex is split, because it was doing two jobs with different
scopes and different costs:

* :class:`bot.starcheck.StarChecker` keeps a lock of its own for cycle
  serialisation. One cycle at a time is a fact about the cycle, its ETag
  cache and the listing it is walking, and nothing outside the checker has
  any business in it.
* This holds the other job: no two components acting on the same member at
  the same time. It is taken for one member's read, role change and write,
  and it excludes only the work that actually conflicts, so two members are
  handled concurrently and a drain during a sweep finishes in its own time.

Nothing here orders two locks against each other, because no path takes two
of these at once and no path takes the cycle lock while holding one. The
sweep takes the cycle lock first and a member lock inside it, and that is
the only nesting in the process.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class _Entry:
    """One member's mutex, and how many callers hold or await it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    holders: int = 0


class MemberLocks:
    """A mutex per member, created on demand and dropped when idle.

    One instance is shared by the sweep, the drain and the claim button;
    see the module docstring for why that sharing is the whole point. A
    registry rather than a lock per member object because the three
    components do not have a member object in common: one has a Mongo row,
    one has an interaction, and the third has whatever the guild cache
    returned.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    @asynccontextmanager
    async def hold(self, member_id: object) -> AsyncIterator[None]:
        """Exclude the other components from ``member_id`` for the body.

        Keyed on ``str(member_id)`` because the same member reaches this as
        a string off a Mongo row and as a Snowflake off an interaction, and
        two spellings of one member would be two mutexes and no exclusion
        at all.
        """
        key = str(member_id)
        entry = self._entries.get(key)
        if entry is None:
            # There is no await between the miss and the insert, so on a
            # single event loop two callers cannot both find nothing and
            # end up waiting on different mutexes for the same member.
            entry = self._entries[key] = _Entry()
        # Counted before the wait, not after it, so a caller queued behind
        # the lock keeps the entry alive while the holder releases it.
        entry.holders += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.holders -= 1
            if not entry.holders:
                # The last one out takes the entry with it. A bot that has
                # served a million members must not be holding a million
                # mutexes for members nothing is doing anything to.
                del self._entries[key]
