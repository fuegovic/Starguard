"""Role changes that report failure instead of raising.

Both the claim button and the star check add or remove the same role, and
both have to survive the bot simply not being allowed to: a role positioned
above the bot's own, or a member who left between the lookup and the write.
"""

import logging

from interactions import Member
from interactions.client.errors import Forbidden, HTTPException, NotFound

log = logging.getLogger("starguard.bot")


async def safe_add_role(member: Member, role_id: int, reason: str) -> bool:
    """Add ``role_id`` to ``member``, returning True on success."""
    try:
        await member.add_role(role_id, reason=reason)
        return True
    except (Forbidden, NotFound, HTTPException) as exc:
        log.warning("Could not add the role to %s: %s", member.id, exc)
        return False


async def safe_remove_role(member: Member, role_id: int, reason: str) -> bool:
    """Remove ``role_id`` from ``member``, returning True on success."""
    try:
        await member.remove_role(role_id, reason=reason)
        return True
    except (Forbidden, NotFound, HTTPException) as exc:
        log.warning("Could not remove the role from %s: %s", member.id, exc)
        return False
