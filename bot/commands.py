"""The bot's informational and administrative slash commands.

Every handler is built inside a ``register_*`` function that is handed the
configuration it needs. The decorators used to run at import time and close
over a module-level CONFIG, which is why importing this bot connected to
MongoDB and could call sys.exit.

The verification flow lives in :mod:`bot.verification`.
"""

import asyncio
import logging

from interactions import (
    ActionRow,
    Button,
    ButtonStyle,
    Client,
    Embed,
    SlashContext,
    slash_command,
)
from pymongo.errors import PyMongoError

from bot import messages
from bot.config import BotConfig
from bot.starcheck import CheckAlreadyRunningError, StarChecker
from bot.verification import register_verification
from common.github_api import GitHubError, fetch_stargazer_logins
from common.storage import UserCollection

log = logging.getLogger("starguard.bot")


def register_commands(
    client: Client,
    config: BotConfig,
    checker: StarChecker,
    users: UserCollection | None,
) -> None:
    """Register every command and callback against ``client``."""
    register_info_commands(client, config)
    register_star_commands(client, config, checker)
    register_verification(client, config, users)
    register_links_command(client, config)


def register_info_commands(client: Client, config: BotConfig) -> None:
    """Register /ping and /help."""

    @slash_command(name="ping", description="☎️ Ping")
    async def ping(ctx: SlashContext) -> None:
        """Report the gateway latency."""
        latency_ms = round(client.latency * 1000, 2)
        await ctx.send(messages.PING.format(latency=latency_ms), ephemeral=True)

    @slash_command(name="help", description="Show a list of available commands")
    async def help_command(ctx: SlashContext) -> None:
        """List the commands this bot provides."""
        embed = Embed(
            title=messages.HELP_TITLE,
            description=messages.HELP_DESCRIPTION,
            color=messages.HELP_COLOR,
            url=messages.HELP_URL,
        )
        embed.add_field(name="> /ping", value=messages.HELP_PING)
        embed.add_field(name="> /verify", value=messages.HELP_VERIFY.format(repo=config.repo))
        embed.add_field(
            name="> /starcount",
            value=messages.HELP_STARCOUNT.format(repo_url=config.repo_url),
        )
        if config.command_name:
            embed.add_field(
                name=f"> /{config.command_name}",
                value=messages.HELP_CUSTOM.format(
                    description=config.command_description,
                    extended_description=config.command_extended_description,
                ),
            )
        embed.add_field(name="---", value=" \n")
        embed.add_field(name=messages.HELP_FOOTER_NAME, value=messages.HELP_FOOTER_VALUE)
        await ctx.send(embed=embed, ephemeral=True)

    client.add_command(ping)
    client.add_command(help_command)


def register_star_commands(client: Client, config: BotConfig, checker: StarChecker) -> None:
    """Register /starcount and /checkstars."""

    @slash_command(name="starcount", description="Get the total number of stargazers")
    async def starcount(ctx: SlashContext) -> None:
        """Report how many accounts have starred the repository."""
        await ctx.defer(ephemeral=True)
        try:
            # Deliberately uncached: the checker's ETag cache belongs to the
            # cycle that holds its lock, and sharing it with a command that
            # can run at any moment would mutate it from under a cycle.
            stargazers = await asyncio.to_thread(
                fetch_stargazer_logins,
                config.owner,
                config.repo,
                token=config.github_token,
            )
        except GitHubError as exc:
            # This used to call len() on None and raise a TypeError on every
            # rate-limited request.
            log.warning("starcount failed: %s", exc)
            await ctx.send(messages.GITHUB_UNREACHABLE.format(reason=exc), ephemeral=True)
            return
        await ctx.send(messages.STARCOUNT.format(count=len(stargazers)), ephemeral=True)

    @slash_command(
        name="checkstars",
        description="⭐ Check who has un-starred the repo and remove their role",
    )
    async def check_stars_command(ctx: SlashContext) -> None:
        """Run the star check now and report what changed."""
        await ctx.defer(ephemeral=True)
        try:
            removed = await checker.run_once(wait=False)
        except CheckAlreadyRunningError:
            await ctx.send(messages.CHECK_ALREADY_RUNNING, ephemeral=True)
            return
        except GitHubError as exc:
            await ctx.send(messages.GITHUB_UNREACHABLE.format(reason=exc), ephemeral=True)
            return
        except PyMongoError as exc:
            log.error("checkstars failed: %s", exc)
            await ctx.send(messages.DATABASE_UNREACHABLE, ephemeral=True)
            return

        if not removed:
            await ctx.send(messages.CHECK_NO_CHANGES, ephemeral=True)
            return

        names = ", ".join(f"**{name}**" for name in removed)
        await ctx.send(
            messages.CHECK_REMOVED.format(count=len(removed), names=names),
            ephemeral=True,
        )

    client.add_command(starcount)
    client.add_command(check_stars_command)


def register_links_command(client: Client, config: BotConfig) -> None:
    """Register the optional custom links command, if it is configured."""
    if not config.links_command_enabled:
        return

    @slash_command(name=config.command_name, description=config.command_description)
    async def hyperlinks(ctx: SlashContext) -> None:
        """Send the configured buttons."""
        buttons = [
            Button(style=ButtonStyle.URL, label=label, url=url)
            for label, url in config.link_buttons
        ]
        await ctx.send(
            messages.LINK_BUTTONS_TITLE,
            components=[ActionRow(*buttons)],
            ephemeral=True,
        )

    client.add_command(hyperlinks)
