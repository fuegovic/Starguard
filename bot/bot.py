"""Starguard Discord bot.

Grants a role to members who have starred the configured GitHub repository and
takes it back when they un-star it.

Blocking work (HTTP calls to GitHub, the synchronous pymongo driver) runs in a
worker thread via ``asyncio.to_thread`` so it never stalls the gateway
heartbeat.
"""

import asyncio
import logging
import os
import random
import sys
from urllib.parse import urlencode

from dotenv import load_dotenv
from interactions import (
    ActionRow,
    Button,
    ButtonStyle,
    Client,
    ComponentContext,
    Embed,
    Intents,
    SlashContext,
    component_callback,
    listen,
    slash_command,
)
from interactions.client.errors import Forbidden, HTTPException, NotFound
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from bot.messages import SORRY, THANKS
from common.config import (
    ConfigError,
    env_bool,
    env_int,
    optional_env,
    require_env,
    require_secret_key,
    require_snowflake,
)
from common.github_api import GitHubError, fetch_stargazer_logins
from common.linktoken import issue_link_token
from common.storage import all_links, connect, find_link, set_starred

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("starguard.bot")

# The automatic check hits the GitHub API once per page of stargazers, so a
# short interval on a popular repository would burn through the rate limit.
MIN_CHECK_DELAY_SECONDS = 300
DEFAULT_CHECK_DELAY_SECONDS = 3600

# How long to wait before retrying after the check loop raises.
LOOP_ERROR_BACKOFF_SECONDS = 60

# Discord rejects slash command names that are not lowercase.
MAX_LINK_BUTTONS = 4


def load_config():
    """Read and validate every setting the bot needs.

    Called before the command decorators run, so a missing value produces a
    named error instead of a bot that registers a command called "None".
    """
    config = {
        "token": require_env("TOKEN"),
        "client_id": optional_env("CLIENT_ID", ""),
        "owner": require_env("REPO_OWNER"),
        "repo": require_env("GITHUB_REPO"),
        "github_token": optional_env("GITHUB_TOKEN"),
        "role_id": require_snowflake("ROLE_ID"),
        "guild_id": require_snowflake("GUILD_ID"),
        "channel_id": require_snowflake("CHANNEL_ID"),
        "domain": require_env("DOMAIN").rstrip("/"),
        "secret_key": require_secret_key(),
        "mongo_host": require_env("MONGO_HOST"),
        "mongo_database": require_env("MONGO_DATABASE"),
        "automatic_check": env_bool("AUTOMATIC_CHECK", True),
        "check_delay": env_int(
            "AUTOMATIC_CHECK_DELAY",
            DEFAULT_CHECK_DELAY_SECONDS,
            minimum=MIN_CHECK_DELAY_SECONDS,
        ),
    }

    # The custom links command is optional. Older versions crashed at startup
    # when it was unconfigured; now it is simply not registered.
    command_name = (optional_env("COMMAND_NAME") or "").lower()
    config["command_name"] = command_name
    config["command_description"] = optional_env(
        "COMMAND_DESCRIPTION", "Useful links"
    )
    config["command_extended_description"] = optional_env(
        "COMMAND_EXTENDED_DESCRIPTION", ""
    )
    config["link_buttons"] = [
        (label, url)
        for label, url in (
            (optional_env(f"BTN{i}"), optional_env(f"URL{i}"))
            for i in range(1, MAX_LINK_BUTTONS + 1)
        )
        if label and url
    ]
    return config


try:
    CONFIG = load_config()
except ConfigError as config_error:
    log.error("Configuration error: %s", config_error)
    sys.exit(1)

REPO_URL = f"https://github.com/{CONFIG['owner']}/{CONFIG['repo']}/"

MONGO_CLIENT = None
USERS = None
try:
    MONGO_CLIENT, USERS = connect(
        CONFIG["mongo_host"], CONFIG["mongo_database"], MongoClient
    )
except PyMongoError as mongo_error:
    log.error("Error connecting to MongoDB: %s", mongo_error)

client = Client(
    intents=Intents.DEFAULT | Intents.GUILD_MEMBERS,
    token=CONFIG["token"],
    sync_interactions=True,
    asyncio_debug=False,
    logger=log,
    send_command_tracebacks=False,
)

# Holding a reference keeps the loop task from being garbage collected mid-run,
# which asyncio is free to do when nothing refers to the task.
_CHECK_TASK = None


@listen()
async def on_startup():
    """Announce the bot and start the periodic star check."""
    global _CHECK_TASK  # pylint: disable=global-statement

    log.info("%s connected to Discord", client.user)
    if CONFIG["client_id"]:
        log.info(
            "Bot invite link: https://discord.com/api/oauth2/authorize"
            "?client_id=%s&permissions=268453888&scope=bot",
            CONFIG["client_id"],
        )

    if CONFIG["automatic_check"]:
        log.info(
            "Automatic star checks every %s seconds", CONFIG["check_delay"]
        )
        _CHECK_TASK = asyncio.create_task(check_star_status_loop())
    else:
        log.info("Automatic star checks are disabled (AUTOMATIC_CHECK=false)")


# 👁️ AUTOMATIC CHECK OF THE STAR STATUS
async def check_star_status_loop():
    """Re-check every linked user on an interval, forever.

    Errors are caught and logged rather than allowed to escape: an unhandled
    exception here used to kill the task silently, and automatic checks would
    never run again until the bot was restarted.
    """
    while True:
        try:
            await check_star_status()
            delay = CONFIG["check_delay"]
        # CancelledError derives from BaseException, so cancellation still
        # propagates out of this handler and stops the loop.
        except Exception:  # pylint: disable=broad-except
            log.exception("Automatic star check failed; retrying shortly")
            delay = LOOP_ERROR_BACKOFF_SECONDS
        await asyncio.sleep(delay)


# ☎️ PING
@slash_command(name="ping", description="☎️ Ping")
async def ping(ctx: SlashContext):
    """Report the gateway latency."""
    latency_ms = round(client.latency * 1000, 2)
    await ctx.send(f"Ping: {latency_ms}ms", ephemeral=True)


# 🙋 HELP
@slash_command(name="help", description="Show a list of available commands")
async def help_command(ctx: SlashContext):
    """List the commands this bot provides."""
    embed = Embed(
        title="GitHub 🌟 Verification Bot",
        description="Here is a list of available commands:",
        color=0xFFAC33,
        url="https://github.com/fuegovic/Starguard",
    )
    embed.add_field(
        name="> /ping",
        value="**Ping the bot**\n- ☎️ Ping the bot, returns the latency in milliseconds",
    )
    embed.add_field(
        name="> /verify",
        value="**GitHub verification**\n"
        f"- ✨ Star **{CONFIG['repo']}**\n"
        "- 🔑 Link your GitHub account\n"
        "- 🎁 Get a role",
    )
    embed.add_field(
        name="> /starcount",
        value=f"**💫 Displays the number of stargazers for:\n{REPO_URL}**",
    )
    if CONFIG["command_name"]:
        embed.add_field(
            name=f"> /{CONFIG['command_name']}",
            value=f"**{CONFIG['command_description']}**\n"
            f"- {CONFIG['command_extended_description']}",
        )
    embed.add_field(name="---", value=" \n")
    embed.add_field(
        name="Visit our GitHub page for the latest updates, additional "
        "information, or to report any problems",
        value="**[GitHub](https://github.com/fuegovic/Starguard)**",
    )
    await ctx.send(embed=embed, ephemeral=True)


# 🛠️ CUSTOM COMMAND - See .env.example
def register_links_command():
    """Register the optional custom links command, if it is configured."""
    if not CONFIG["command_name"] or not CONFIG["link_buttons"]:
        return

    @slash_command(
        name=CONFIG["command_name"], description=CONFIG["command_description"]
    )
    async def hyperlinks(ctx: SlashContext):
        buttons = [
            Button(style=ButtonStyle.URL, label=label, url=url)
            for label, url in CONFIG["link_buttons"]
        ]
        await ctx.send(
            "Useful links:", components=[ActionRow(*buttons)], ephemeral=True
        )

    client.add_command(hyperlinks)


# ✨ OUTPUT THE NUMBER OF STARS A REPO HAS
@slash_command(name="starcount", description="Get the total number of stargazers")
async def starcount(ctx: SlashContext):
    """Report how many accounts have starred the repository."""
    await ctx.defer(ephemeral=True)
    try:
        stargazers = await get_stargazers()
    except GitHubError as exc:
        # This used to call len() on None and raise a TypeError on every
        # rate-limited request.
        log.warning("starcount failed: %s", exc)
        await ctx.send(f"Could not reach GitHub right now: {exc}", ephemeral=True)
        return
    await ctx.send(f"There are {len(stargazers)} stargazers! ✨", ephemeral=True)


# 🔍 VERIFY USER AND GIVE A ROLE BUTTONS
@slash_command(name="verify", description="💫 Self Verification")
async def verify(ctx: SlashContext):
    """Send the three-step verification prompt."""
    # The Discord ID travels inside a signed, expiring token rather than as a
    # plain query parameter, so it cannot be swapped for someone else's.
    link_token = issue_link_token(
        CONFIG["secret_key"], ctx.author_id, str(ctx.author)
    )
    oauth_url = f"{CONFIG['domain']}/login?{urlencode({'token': link_token})}"

    ver_btns = [
        ActionRow(
            Button(
                style=ButtonStyle.URL,
                label="1: Star this repo 🌟",
                url=REPO_URL,
            ),
            Button(
                style=ButtonStyle.URL,
                label="2: Log in with GitHub 🔑",
                url=oauth_url,
            ),
            Button(
                style=ButtonStyle.BLUE,
                label="3: Claim your role ❤️‍🔥",
                custom_id="claim",
            ),
        )
    ]
    await ctx.send(
        "💫 Self Verification:\n"
        "- 1: Make sure you've starred this repo\n"
        "- 2: Authenticate with GitHub\n"
        "- 3: Claim your role\n"
        "_The GitHub link is personal to you and expires in 15 minutes._",
        components=ver_btns,
        ephemeral=True,
    )


# 🎁 CLAIM THE ROLE
@component_callback("claim")
async def claim_callback(ctx: ComponentContext):
    """Grant the role if the clicking user has a recorded star."""
    if USERS is None:
        await ctx.send(
            content="The database is unavailable right now, please try again later.",
            ephemeral=True,
        )
        return

    member = ctx.author
    try:
        user_entry = await asyncio.to_thread(find_link, USERS, ctx.author_id)
    except PyMongoError as exc:
        log.error("Could not read the link for %s: %s", ctx.author_id, exc)
        await ctx.send(
            content="Could not check your verification, please try again later.",
            ephemeral=True,
        )
        return

    if not user_entry:
        await ctx.send(
            content="Please make sure to link your GitHub account by using the "
            "**Log in with GitHub** button.",
            ephemeral=True,
        )
        return

    if not user_entry.get("starred_repo", False):
        # Only touch the role if they actually hold it.
        if member.has_role(CONFIG["role_id"]):
            await safe_remove_role(member, "no_star")
        await ctx.send(
            content="Please star the repo to get the role 🌟", ephemeral=True
        )
        return

    if member.has_role(CONFIG["role_id"]):
        await ctx.send(
            content="You already claimed your role 😁\n💫Thanks!", ephemeral=True
        )
        return

    if not await safe_add_role(member, "star"):
        await ctx.send(
            content="I could not assign the role. Please ask a moderator to "
            "check my permissions and role position.",
            ephemeral=True,
        )
        return

    await ctx.send(content=random.choice(THANKS).format(ctx.author_id))


# ⭐ Check who has un-starred the repo and remove their role
@slash_command(
    name="checkstars",
    description="⭐ Check who has un-starred the repo and remove their role",
)
async def check_stars_command(ctx: SlashContext):
    """Run the star check now and report what changed."""
    await ctx.defer(ephemeral=True)
    try:
        removed = await check_star_status()
    except GitHubError as exc:
        await ctx.send(f"Could not reach GitHub right now: {exc}", ephemeral=True)
        return
    except PyMongoError as exc:
        log.error("checkstars failed: %s", exc)
        await ctx.send("Could not reach the database right now.", ephemeral=True)
        return

    if not removed:
        await ctx.send("Star status checked, no changes.", ephemeral=True)
        return

    names = ", ".join(f"**{name}**" for name in removed)
    await ctx.send(
        f"Removed the role from {len(removed)} member(s) for un-starring the "
        f"repo: {names}",
        ephemeral=True,
    )


async def check_star_status():
    """Strip the role from linked users who no longer star the repository.

    Returns the list of display names that lost the role.
    """
    if USERS is None:
        log.warning("Skipping star check: no database connection.")
        return []

    stargazers = await get_stargazers()
    links = await asyncio.to_thread(all_links, USERS)

    guild = client.get_guild(CONFIG["guild_id"])
    if guild is None:
        log.warning("Guild %s is not in the cache; skipping.", CONFIG["guild_id"])
        return []

    channel = client.get_channel(CONFIG["channel_id"])
    removed = []

    for entry in links:
        username = entry.get("github_username_lower") or (
            entry.get("github_username") or ""
        ).lower()
        if not username or username in stargazers:
            continue

        discord_id = entry.get("discord_id")
        if not discord_id:
            continue

        # A member who left the guild returns None here. Calling has_role on
        # that used to raise and take the whole loop down with it.
        member = guild.get_member(discord_id)
        if member is None:
            log.info(
                "Discord ID %s is no longer in the guild; marking un-starred.",
                discord_id,
            )
            await record_unstarred(discord_id)
            continue

        if not member.has_role(CONFIG["role_id"]):
            await record_unstarred(discord_id)
            continue

        if not await safe_remove_role(member, "no_star"):
            continue

        await record_unstarred(discord_id)
        removed.append(str(entry.get("discord_username") or member.display_name).lstrip("@"))

        if channel is not None:
            try:
                await channel.send(content=random.choice(SORRY).format(discord_id))
            except (Forbidden, HTTPException) as exc:
                log.warning("Could not post to the announcement channel: %s", exc)

    return removed


async def record_unstarred(discord_id):
    """Persist that ``discord_id`` no longer stars the repository."""
    try:
        await asyncio.to_thread(set_starred, USERS, discord_id, False)
    except PyMongoError as exc:
        log.error("Could not update star state for %s: %s", discord_id, exc)


async def safe_add_role(member, reason):
    """Add the configured role, returning True on success."""
    try:
        await member.add_role(CONFIG["role_id"], reason=reason)
        return True
    except (Forbidden, NotFound, HTTPException) as exc:
        log.warning("Could not add the role to %s: %s", member.id, exc)
        return False


async def safe_remove_role(member, reason):
    """Remove the configured role, returning True on success."""
    try:
        await member.remove_role(CONFIG["role_id"], reason=reason)
        return True
    except (Forbidden, NotFound, HTTPException) as exc:
        log.warning("Could not remove the role from %s: %s", member.id, exc)
        return False


# 🤩 GET THE LIST OF STARGAZERS FOR THE SPECIFIED REPO
async def get_stargazers():
    """Return the lower-cased logins that star the repository."""
    return await asyncio.to_thread(
        fetch_stargazer_logins,
        CONFIG["owner"],
        CONFIG["repo"],
        CONFIG["github_token"],
    )


def main():
    """Register optional commands and connect to Discord."""
    if not CONFIG["github_token"]:
        log.warning(
            "GITHUB_TOKEN is not set. Unauthenticated GitHub requests are "
            "limited to 60 per hour, which is not enough for a repository "
            "with more than a few thousand stargazers."
        )
    register_links_command()
    client.start()


if __name__ == "__main__":
    main()
