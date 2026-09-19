"""Starguard Discord bot.

Grants a role to members who have starred the configured GitHub repository and
takes it back when they un-star it.

Importing this module does nothing. It used to validate the environment, call
sys.exit on a bad value and connect to MongoDB as a side effect of the import,
which is why the bot's own tests had to be run in subprocesses. Everything now
happens in :func:`main`, and :func:`create_client` builds a configured client
from values it is handed.

``python -m bot.bot`` is unchanged, which is what the container runs.
"""

import asyncio
import logging
import sys
from collections.abc import Callable, Coroutine
from typing import Any

from dotenv import load_dotenv
from interactions import Client, Intents, listen
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from bot.commands import register_commands
from bot.config import BotConfig, load_bot_config
from bot.health import HealthState, serve_health, stale_after_seconds
from bot.starcheck import StarChecker
from common.config import ConfigError
from common.logging_setup import configure_logging
from common.storage import UserCollection, connect

log = logging.getLogger("starguard.bot")


def create_client(
    config: BotConfig,
    users: UserCollection | None = None,
    health: HealthState | None = None,
) -> tuple[Client, StarChecker]:
    """Build the configured Discord client. Returns ``(client, checker)``.

    Nothing here talks to the network: the client is constructed, the
    commands are registered against ``config``, and the caller decides when
    to start it.
    """
    client = Client(
        intents=Intents.DEFAULT | Intents.GUILD_MEMBERS,
        token=config.token,
        sync_interactions=True,
        asyncio_debug=False,
        logger=log,
        send_command_tracebacks=False,
    )

    checker = StarChecker(client, config, users)
    register_commands(client, config, checker, users)
    client.add_listener(listen("startup")(_startup_listener(client, config, checker, health)))

    return client, checker


# The Coroutine's send and throw types are Any because that is how the async
# machinery is spelled in typeshed; only the None it returns is ours to state.
def _startup_listener(
    client: Client,
    config: BotConfig,
    checker: StarChecker,
    health: HealthState | None,
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Build the Startup handler for this client."""

    async def on_startup() -> None:
        """Announce the bot and start the periodic star check."""
        log.info("%s connected to Discord", client.user)
        if config.client_id:
            log.info(
                "Bot invite link: https://discord.com/api/oauth2/authorize"
                "?client_id=%s&permissions=268453888&scope=bot",
                config.client_id,
            )

        if config.automatic_check:
            log.info("Automatic star checks every %s seconds", config.check_delay)
            # Holding a reference keeps the loop task from being garbage
            # collected mid-run, which asyncio is free to do when nothing
            # refers to the task. The client outlives the loop, so it is the
            # right place to keep it.
            # Client has no such attribute of its own, which is the point:
            # this is Starguard hanging its task off an object that outlives
            # the loop.
            client.starguard_check_task = asyncio.create_task(  # type: ignore[attr-defined]
                checker.run_forever()
            )
        else:
            log.info("Automatic star checks are disabled (AUTOMATIC_CHECK=false)")

        if health is not None:
            health.mark_ready(
                stale_after_seconds(config.check_delay) if config.automatic_check else None
            )

    return on_startup


def connect_users(config: BotConfig) -> UserCollection | None:
    """Return the users collection, or None when MongoDB is unreachable."""
    try:
        _, users = connect(config.mongo_host, config.mongo_database, MongoClient)
        return users
    except PyMongoError as exc:
        log.error("Error connecting to MongoDB: %s", exc)
        return None


def main() -> None:
    """Load the environment, build the bot and connect to Discord."""
    load_dotenv()
    configure_logging()

    try:
        config = load_bot_config()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        sys.exit(1)

    if not config.github_token:
        log.warning(
            "GITHUB_TOKEN is not set. Unauthenticated GitHub requests are "
            "limited to 60 per hour, which is not enough for a repository "
            "with more than a few thousand stargazers."
        )

    health = HealthState()
    client, checker = create_client(config, connect_users(config), health)

    if config.health_enabled:
        serve_health(
            health,
            config.health_host,
            config.health_port,
            lambda: checker.last_completed,
        )

    client.start()


if __name__ == "__main__":
    main()
