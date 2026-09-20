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
from bot.health import HealthState, LoopHealth, serve_health, stale_after_seconds
from bot.memberlock import MemberLocks
from bot.rolesync import RoleSyncDrainer
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

    # One registry for all three things that move this role, which is what
    # makes it exclusion: the sweep, the drain and the claim button take
    # the same member's mutex and so can never be inside one member at
    # once. See bot.memberlock for why it is per member rather than the one
    # process-wide lock this replaced.
    member_locks = MemberLocks()
    checker = StarChecker(client, config, users, member_locks)
    drainer = RoleSyncDrainer(client, config, users, member_locks)
    register_commands(client, config, checker, users, member_locks)
    client.add_listener(
        listen("startup")(_startup_listener(client, config, checker, drainer, health))
    )

    if health is not None:
        _watch_loops(health, config, checker, drainer)

    return client, checker


def _watch_loops(
    health: HealthState,
    config: BotConfig,
    checker: StarChecker,
    drainer: RoleSyncDrainer,
) -> None:
    """Tell the health endpoint which reconciling loops to report on.

    Both are named whether or not they are turned on, so the payload says
    "disabled" rather than going quiet about a loop the operator may think
    is running. A loop that is off carries no deadline and cannot be late.

    The drain belongs here as much as the sweep does. On a deployment
    running AUTOMATIC_CHECK=false with ROLE_SYNC_ENABLED=true it is the
    only loop reconciling anything, and leaving it out meant a bot whose
    drain had never once reached the database still answered /healthz with
    200 for as long as it ran.
    """
    health.watch(
        LoopHealth(
            field="star_check",
            age_field="last_check_age_seconds",
            stale_after=(
                stale_after_seconds(config.check_delay) if config.automatic_check else None
            ),
            last_completed=lambda: checker.last_completed,
        )
    )
    health.watch(
        LoopHealth(
            field="role_sync",
            age_field="last_role_sync_age_seconds",
            stale_after=(
                stale_after_seconds(config.role_sync_interval) if config.role_sync_enabled else None
            ),
            last_completed=lambda: drainer.last_completed,
        )
    )


# The Coroutine's send and throw types are Any because that is how the async
# machinery is spelled in typeshed; only the None it returns is ours to state.
def _startup_listener(
    client: Client,
    config: BotConfig,
    checker: StarChecker,
    drainer: RoleSyncDrainer,
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

        if config.role_sync_enabled:
            log.info("Draining queued role changes every %s seconds", config.role_sync_interval)
            # Held for the same reason as the check task above, and in its
            # own attribute so that turning one loop off does not disturb
            # the other's handle.
            client.starguard_rolesync_task = asyncio.create_task(  # type: ignore[attr-defined]
                drainer.run_forever()
            )
        else:
            log.info("The role sync drain is disabled (ROLE_SYNC_ENABLED=false)")

        if health is not None:
            # Only the gateway is news here. Which loops the endpoint
            # reports on was settled when the client was built; see
            # _watch_loops.
            health.mark_ready()

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
    client, _ = create_client(config, connect_users(config), health)

    if config.health_enabled:
        serve_health(health, config.health_host, config.health_port)

    client.start()


if __name__ == "__main__":
    main()
