"""Configuration for the Discord bot.

A typed object rather than a dict of strings, so a misspelled key is an
AttributeError where it is used instead of a KeyError inside a slash command,
and so the Discord IDs really are ints by the time the library's cache is
asked for them.
"""

from dataclasses import dataclass
from typing import Final

from common.config import (
    env_bool,
    env_int,
    optional_env,
    require_env,
    require_https_url,
    require_secret_key,
    require_snowflake,
)

# The automatic check hits the GitHub API once per page of stargazers, so a
# short interval on a popular repository would burn through the rate limit.
MIN_CHECK_DELAY_SECONDS: Final = 300
DEFAULT_CHECK_DELAY_SECONDS: Final = 3600

MAX_LINK_BUTTONS: Final = 4

# The bot's health endpoint. Bound to loopback by default because its only
# consumer is a container healthcheck running inside the same container; a
# deployment that scrapes it from outside can set BOT_HEALTH_HOST=0.0.0.0.
DEFAULT_HEALTH_HOST: Final = "127.0.0.1"
DEFAULT_HEALTH_PORT: Final = 8080


@dataclass(frozen=True)
class BotConfig:
    """Everything the bot needs, validated."""

    # A configuration object is a bag of settings by definition; splitting it
    # to satisfy the attribute count would only move the bag.
    # pylint: disable=too-many-instance-attributes

    token: str
    client_id: str
    owner: str
    repo: str
    github_token: str | None
    role_id: int
    guild_id: int
    channel_id: int
    domain: str
    secret_key: str
    mongo_host: str
    mongo_database: str
    automatic_check: bool
    check_delay: int
    command_name: str
    command_description: str
    command_extended_description: str
    link_buttons: tuple[tuple[str, str], ...]
    health_enabled: bool
    health_host: str
    health_port: int

    @property
    def repo_url(self) -> str:
        """The public URL of the repository members are asked to star."""
        return f"https://github.com/{self.owner}/{self.repo}/"

    @property
    def links_command_enabled(self) -> bool:
        """Whether the optional custom links command should be registered."""
        return bool(self.command_name and self.link_buttons)


def _link_buttons() -> tuple[tuple[str, str], ...]:
    """Return the fully configured (label, url) pairs, in order.

    A pair with only half of it set used to register a button with an empty
    URL, which Discord rejects and which took the whole command down with it.
    """
    return tuple(
        (label, url)
        for label, url in (
            (optional_env(f"BTN{i}"), optional_env(f"URL{i}"))
            for i in range(1, MAX_LINK_BUTTONS + 1)
        )
        if label and url
    )


def load_bot_config() -> BotConfig:
    """Read and validate every setting the bot needs.

    Called from ``main()`` before anything is registered, so a missing value
    produces a named error instead of a bot that registers a command called
    "None".
    """
    return BotConfig(
        token=require_env("TOKEN"),
        client_id=optional_env("CLIENT_ID", ""),
        owner=require_env("REPO_OWNER"),
        repo=require_env("GITHUB_REPO"),
        github_token=optional_env("GITHUB_TOKEN"),
        role_id=require_snowflake("ROLE_ID"),
        guild_id=require_snowflake("GUILD_ID"),
        channel_id=require_snowflake("CHANNEL_ID"),
        domain=require_https_url("DOMAIN"),
        secret_key=require_secret_key(),
        mongo_host=require_env("MONGO_HOST"),
        mongo_database=require_env("MONGO_DATABASE"),
        automatic_check=env_bool("AUTOMATIC_CHECK", True),
        check_delay=env_int(
            "AUTOMATIC_CHECK_DELAY",
            DEFAULT_CHECK_DELAY_SECONDS,
            minimum=MIN_CHECK_DELAY_SECONDS,
        ),
        # The custom links command is optional. Older versions crashed at
        # startup when it was unconfigured; now it is simply not registered.
        # Discord rejects slash command names that are not lowercase.
        command_name=(optional_env("COMMAND_NAME") or "").lower(),
        command_description=optional_env("COMMAND_DESCRIPTION", "Useful links"),
        command_extended_description=optional_env("COMMAND_EXTENDED_DESCRIPTION", ""),
        link_buttons=_link_buttons(),
        health_enabled=env_bool("BOT_HEALTH_ENABLED", True),
        health_host=optional_env("BOT_HEALTH_HOST", DEFAULT_HEALTH_HOST),
        health_port=env_int("BOT_HEALTH_PORT", DEFAULT_HEALTH_PORT, minimum=1),
    )
