"""The verification flow: /verify, the recovery link and claiming the role.

Kept apart from the informational commands because this is the part that
issues signed link tokens and grants the role, so it is the part worth
reading closely.
"""

import asyncio
import logging
import random
from typing import Final, cast
from urllib.parse import urlencode

from interactions import (
    ActionRow,
    Button,
    ButtonStyle,
    Client,
    ComponentContext,
    Member,
    SlashContext,
    Snowflake_Type,
    component_callback,
    slash_command,
)
from pymongo.errors import PyMongoError

from bot import messages
from bot.config import BotConfig
from bot.memberlock import MemberLocks
from bot.roles import safe_add_role, safe_remove_role
from common.linktoken import issue_link_token
from common.storage import UserCollection, find_link

log = logging.getLogger("starguard.bot")

CLAIM_BUTTON_ID: Final = "claim"
RELINK_BUTTON_ID: Final = "relink"


def login_url(config: BotConfig, author_id: Snowflake_Type, author: object) -> str:
    """Return the personal, expiring OAuth URL for one Discord user.

    The Discord ID travels inside a signed, expiring token rather than as a
    plain query parameter, so it cannot be swapped for someone else's.
    """
    token = issue_link_token(config.secret_key, author_id, str(author))
    return f"{config.domain}/login?{urlencode({'token': token})}"


def relink_button() -> Button:
    """The button that replaces an expired verification link."""
    return Button(
        style=ButtonStyle.GREY,
        label=messages.VERIFY_BUTTON_RELINK,
        custom_id=RELINK_BUTTON_ID,
    )


def register_verification(
    client: Client,
    config: BotConfig,
    users: UserCollection | None,
    member_locks: MemberLocks,
) -> None:
    """Register /verify and the buttons that follow it."""

    # Guild-only. The whole point of the flow is to grant a role, and roles
    # only exist inside a server, so a DM could start a verification that
    # could never be completed: ctx.author is a User there, and a User has no
    # has_role, so pressing "Claim your role" raised AttributeError.
    @slash_command(name="verify", description="💫 Self Verification", dm_permission=False)
    async def verify(ctx: SlashContext) -> None:
        """Send the three-step verification prompt."""
        buttons = ActionRow(
            Button(
                style=ButtonStyle.URL,
                label=messages.VERIFY_BUTTON_STAR,
                url=config.repo_url,
            ),
            Button(
                style=ButtonStyle.URL,
                label=messages.VERIFY_BUTTON_LOGIN,
                url=login_url(config, ctx.author_id, ctx.author),
            ),
            Button(
                style=ButtonStyle.BLUE,
                label=messages.VERIFY_BUTTON_CLAIM,
                custom_id=CLAIM_BUTTON_ID,
            ),
            relink_button(),
        )
        await ctx.send(messages.VERIFY_STEPS, components=[buttons], ephemeral=True)

    @component_callback(RELINK_BUTTON_ID)
    async def relink_callback(ctx: ComponentContext) -> None:
        """Issue a fresh login link without making the user start over.

        A link is only good for fifteen minutes, and the page that says so
        could previously only tell people to run /verify again, which meant
        leaving the message they were already looking at.
        """
        button = Button(
            style=ButtonStyle.URL,
            label=messages.VERIFY_BUTTON_LOGIN,
            url=login_url(config, ctx.author_id, ctx.author),
        )
        await ctx.send(
            messages.RELINK_SENT.format(claim_label=messages.VERIFY_BUTTON_CLAIM),
            components=[ActionRow(button)],
            ephemeral=True,
        )

    @component_callback(CLAIM_BUTTON_ID)
    async def claim_callback(ctx: ComponentContext) -> None:
        """Grant the role if the clicking user has a recorded star."""
        if users is None:
            await ctx.send(content=messages.CLAIM_DATABASE_UNAVAILABLE, ephemeral=True)
            return

        # ctx.author is a User rather than a Member when the interaction did
        # not come from a guild, and a User has no has_role. /verify is now
        # guild-only so this should be unreachable, but a button lives in the
        # message it was sent with: a prompt sent from a DM before that change
        # is still clickable, and it used to answer with an AttributeError
        # traceback instead of an explanation. A capability check rather than
        # isinstance, because whether this object can answer has_role is
        # exactly the condition, and the handler is driven with a stand-in in
        # the tests.
        if not hasattr(ctx.author, "has_role"):
            await ctx.send(content=messages.CLAIM_NEEDS_A_SERVER, ephemeral=True)
            return

        member = cast(Member, ctx.author)
        # The button is the third thing in this process that moves this
        # role, and until now it was the one nothing excluded. It reads the
        # row, decides from it and then talks to Discord, which is exactly
        # what the sweep and the drain do, so the same interleavings apply:
        # a claim that read starred_repo false could still be removing the
        # role while a re-star queued a grant, the drain saw the role it
        # was about to lose and settled the row as already correct, and the
        # removal landed afterwards. That leaves a member the database says
        # stars the repository holding no role, with nothing queued and
        # nothing that would ever put it back.
        #
        # The read is inside the lock, not before it, because a snapshot
        # taken outside is the stale information the lock exists to stop
        # anybody acting on. Waiting costs this member the one role change
        # ahead of them rather than a whole sweep; see bot.memberlock for
        # why that distinction is the design.
        async with member_locks.hold(ctx.author_id):
            try:
                user_entry = await asyncio.to_thread(find_link, users, ctx.author_id)
            except PyMongoError as exc:
                log.error("Could not read the link for %s: %s", ctx.author_id, exc)
                await ctx.send(content=messages.CLAIM_LOOKUP_FAILED, ephemeral=True)
                return

            # The repository is part of the question, not just the row. An
            # operator who repoints REPO_OWNER or GITHUB_REPO while keeping
            # the database leaves rows that say starred_repo about the
            # repository they used to watch, and that is no evidence at all
            # about the new one. Without this, everybody who had verified
            # before the move could claim the role for a repository they
            # have never starred. Every row a Discord ID can find was
            # written by link_account, which always stores linked_repo, so
            # this cannot lock out a row that simply predates the field.
            if not user_entry or user_entry.get("linked_repo") != config.repo_url:
                await ctx.send(
                    content=messages.CLAIM_NOT_LINKED.format(
                        relink_label=messages.VERIFY_BUTTON_RELINK
                    ),
                    components=[ActionRow(relink_button())],
                    ephemeral=True,
                )
                return

            if not user_entry.get("starred_repo", False):
                # Only touch the role if they actually hold it.
                if member.has_role(config.role_id):
                    await safe_remove_role(member, config.role_id, "no_star")
                await ctx.send(content=messages.CLAIM_NOT_STARRED, ephemeral=True)
                return

            if member.has_role(config.role_id):
                await ctx.send(content=messages.CLAIM_ALREADY_HELD, ephemeral=True)
                return

            if not await safe_add_role(member, config.role_id, "star"):
                await ctx.send(content=messages.CLAIM_ROLE_FAILED, ephemeral=True)
                return

            # B311: picks a thank-you message, not a secret. The nosec has
            # to sit on the random.choice line rather than the send, because
            # bandit matches a suppression to the line it reports the issue
            # on, and splitting this call across lines moved that.
            await ctx.send(
                content=random.choice(messages.THANKS).format(ctx.author_id)  # nosec B311
            )

    client.add_command(verify)
    client.add_component_callback(relink_callback)
    client.add_component_callback(claim_callback)
