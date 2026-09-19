"""Configuration for the OAuth callback server.

A typed object rather than a dict of strings, so a misspelled key is an
AttributeError at the point of use instead of a KeyError somewhere in a
request, and so the port really is an int by the time waitress sees it.
"""

from dataclasses import dataclass
from typing import Final

from common.config import (
    MIN_SECRET_KEY_LENGTH,
    PLACEHOLDER_SECRET_KEYS,
    ConfigError,
    env_int,
    optional_env,
    require_env,
    require_secret_key,
)

# The default port inside the container. docker-compose publishes it on the
# host as ${SERVER_PORT}; the two are deliberately separate, because binding
# to SERVER_PORT while the compose file mapped it to 5000 made every value
# other than 5000 unreachable.
DEFAULT_BIND_PORT: Final = 5000

DEFAULT_LINK_TOKEN_MAX_AGE: Final = 900
MIN_LINK_TOKEN_MAX_AGE: Final = 60

# Ten starts of the OAuth flow a minute from one address is far more than a
# person needs and far less than a script wants.
DEFAULT_RATE_LIMIT: Final = 10
DEFAULT_RATE_WINDOW_SECONDS: Final = 60

# The shared secret GitHub signs each webhook delivery with. Optional: an
# installation that has not set up a hook has no receiver at all.
WEBHOOK_SECRET_ENV: Final = "GITHUB_WEBHOOK_SECRET"


def optional_secret(name: str) -> str | None:
    """Return ``name`` as a validated secret, or None when it is not set.

    Held to the same placeholder list and the same minimum length as
    SECRET_KEY, because a webhook secret copied out of .env.example lets
    anyone sign a star event for anyone. :func:`require_secret_key` cannot
    be called for this: it names SECRET_KEY itself, and unset is an error
    there and the normal state here. Its two rules are borrowed rather than
    restated, so raising the minimum raises it for both.
    """
    secret = optional_env(name)
    if secret is None:
        return None
    if secret.lower() in PLACEHOLDER_SECRET_KEYS:
        raise ConfigError(
            f"{name} is still set to a placeholder value. Generate a real one "
            'with: python -c "import secrets; print(secrets.token_urlsafe(32))" '
            "and paste the same value into the webhook's secret field on GitHub."
        )
    if len(secret) < MIN_SECRET_KEY_LENGTH:
        raise ConfigError(
            f"{name} must be at least {MIN_SECRET_KEY_LENGTH} characters, got {len(secret)}."
        )
    return secret


@dataclass(frozen=True)
class ServerConfig:
    """Everything the OAuth server needs, validated."""

    # A configuration object is a bag of settings by definition; splitting it
    # to satisfy the attribute count would only move the bag.
    # pylint: disable=too-many-instance-attributes

    owner: str
    repo: str
    secret_key: str
    client_id: str
    client_secret: str
    mongo_host: str
    mongo_database: str
    port: int
    link_token_max_age: int
    trusted_proxy_count: int
    rate_limit: int
    rate_limit_window: int
    # None means no webhook is configured, and the receiver is then never
    # registered. It carries a default so that every existing construction
    # of this object, including the ones in the test suite, still reads as
    # an installation without a hook.
    webhook_secret: str | None = None

    @property
    def repo_url(self) -> str:
        """The public URL of the repository members are asked to star."""
        return f"https://github.com/{self.owner}/{self.repo}/"


def load_server_config() -> ServerConfig:
    """Read and validate every setting the server needs."""
    return ServerConfig(
        owner=require_env("REPO_OWNER"),
        repo=require_env("GITHUB_REPO"),
        secret_key=require_secret_key(),
        client_id=require_env("GITHUB_CLIENT_ID"),
        client_secret=require_env("GITHUB_CLIENT_SECRET"),
        mongo_host=require_env("MONGO_HOST"),
        mongo_database=require_env("MONGO_DATABASE"),
        port=env_int("SERVER_BIND_PORT", DEFAULT_BIND_PORT, minimum=1),
        link_token_max_age=env_int(
            "LINK_TOKEN_MAX_AGE",
            DEFAULT_LINK_TOKEN_MAX_AGE,
            minimum=MIN_LINK_TOKEN_MAX_AGE,
        ),
        # How many proxies sit in front of this process. ProxyFix reads the
        # nth value from the right of X-Forwarded-For, so getting this wrong
        # is the difference between the real client address and one a client
        # chose for itself, which would defeat the rate limit outright.
        trusted_proxy_count=env_int("TRUSTED_PROXY_COUNT", 1, minimum=1),
        rate_limit=env_int("LOGIN_RATE_LIMIT", DEFAULT_RATE_LIMIT, minimum=1),
        rate_limit_window=env_int(
            "LOGIN_RATE_LIMIT_WINDOW", DEFAULT_RATE_WINDOW_SECONDS, minimum=1
        ),
        webhook_secret=optional_secret(WEBHOOK_SECRET_ENV),
    )
