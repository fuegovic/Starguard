"""Configuration for the OAuth callback server.

A typed object rather than a dict of strings, so a misspelled key is an
AttributeError at the point of use instead of a KeyError somewhere in a
request, and so the port really is an int by the time waitress sees it.
"""

from dataclasses import dataclass
from typing import Final

from common.config import env_int, require_env, require_secret_key

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
    )
