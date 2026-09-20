"""Environment configuration helpers shared by the bot and the OAuth server.

Every value the project cannot run without is read through :func:`require_env`
so that a misconfigured deployment fails immediately, with a message naming the
variable, instead of crashing later with an opaque error.
"""

import os
from typing import Final, overload
from urllib.parse import urlsplit


class ConfigError(RuntimeError):
    """Raised when the environment is missing or has an unusable value."""


# Values shipped in .env.example or commonly pasted from tutorials. A Flask
# secret key that anyone can guess lets an attacker forge session cookies, so
# these are rejected outright rather than merely warned about.
PLACEHOLDER_SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {
        "secretkey",
        "changeme",
        "change-me",
        "secret",
        "your-secret-key",
        "please-change-me",
    }
)

MIN_SECRET_KEY_LENGTH: Final = 16

# The range a TCP port can actually be. Zero is excluded on purpose: the
# kernel reads it as "any free port", which is useful in a test and useless
# in a deployment, where nothing would know where the service ended up.
MIN_PORT: Final = 1
MAX_PORT: Final = 65535


# The overloads exist so that ``optional_env(name, "")`` is a str at the call
# site rather than ``str | None``. Half the callers pass a default precisely so
# they never have to handle None, and without these they would all have to.
@overload
def optional_env(name: str) -> str | None: ...


@overload
def optional_env(name: str, default: str) -> str: ...


@overload
def optional_env(name: str, default: str | None) -> str | None: ...


def optional_env(name: str, default: str | None = None) -> str | None:
    """Return the stripped value of ``name``, or ``default`` when unset/blank."""
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def require_env(name: str, hint: str | None = None) -> str:
    """Return the value of ``name``, raising :class:`ConfigError` when unset."""
    value = optional_env(name)
    if value is None:
        message = f"Required environment variable {name} is not set."
        if hint:
            message = f"{message} {hint}"
        raise ConfigError(message)
    return value


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    """Return ``name`` as an int, clamped to ``minimum`` when one is given.

    ``minimum`` clamps rather than raising, which is right for the values
    that use it: an ``AUTOMATIC_CHECK_DELAY`` under the floor becomes the
    floor and the deployment carries on with a value the operator can live
    with. There is deliberately no ``maximum`` to match, because a ceiling
    that clamped would be wrong wherever one is wanted. See
    :func:`env_port`, which is the case that wanted one.
    """
    raw = optional_env(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be a whole number, got {raw!r}.") from exc
    if minimum is not None and value < minimum:
        return minimum
    return value


def env_port(name: str, default: int) -> int:
    """Return ``name`` as a TCP port, rejecting anything outside 1-65535.

    A reader of its own rather than a bound passed to :func:`env_int`,
    because this one raises where that one clamps, and the difference is
    the whole point. Clamping a port is not a smaller version of what was
    asked for, it is a different address: ``BOT_HEALTH_PORT=70000`` would
    quietly bind 65535, and an operator hunting their typo would find a
    service listening and answering on a port they never named.

    Raising is also what the alternative costs. ``bind()`` answers a port
    above the ceiling with ``OverflowError``, and a caller that survives
    that is a caller that came up without the socket: for the bot's
    optional health endpoint, a container whose healthcheck then fails
    every probe for its whole life, explained only by one startup log line
    that has long scrolled past. Naming the variable at startup is what
    every other unusable value in this module does.
    """
    value = env_int(name, default)
    if not MIN_PORT <= value <= MAX_PORT:
        raise ConfigError(
            f"{name} must be a TCP port between {MIN_PORT} and {MAX_PORT}, got {value}."
        )
    return value


def env_bool(name: str, default: bool = False) -> bool:
    """Return ``name`` as a bool, accepting the usual true/false spellings."""
    raw = optional_env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean such as true/false, got {raw!r}.")


def require_snowflake(name: str) -> int:
    """Return a Discord ID as an int.

    Discord IDs arrive from the environment as strings. The library's cache is
    keyed by integers, so passing the raw string silently misses every lookup.
    """
    raw = require_env(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name} must be a Discord ID (a number), got {raw!r}. "
            "Enable Developer Mode in Discord and use 'Copy ID'."
        ) from exc


def require_https_url(name: str) -> str:
    """Return ``name`` as an absolute https URL, with no trailing slash.

    Members are sent to this address by a Discord button, so a malformed value
    breaks the whole verification flow with nothing useful in the log: Discord
    refuses to render a button whose URL has no scheme, and GitHub refuses an
    OAuth redirect_uri it was not configured with. Failing here names the
    variable instead.
    """
    raw = require_env(name)
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.netloc:
        raise ConfigError(
            f"{name} must be an absolute https URL such as "
            f"https://starguard.example.com, got {raw!r}."
        )
    if parts.query or parts.fragment:
        raise ConfigError(
            f"{name} must be a plain URL with no query string or fragment, got {raw!r}."
        )
    return raw.rstrip("/")


def require_secret_key() -> str:
    """Return SECRET_KEY, rejecting unset, placeholder, and too-short values.

    The bot signs verification links with this key and the server verifies
    them, so a predictable key would let anyone mint a link for any Discord
    account.
    """
    key = require_env(
        "SECRET_KEY",
        hint='Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"',
    )
    if key.lower() in PLACEHOLDER_SECRET_KEYS:
        raise ConfigError(
            "SECRET_KEY is still set to a placeholder value. Generate a real "
            'one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    if len(key) < MIN_SECRET_KEY_LENGTH:
        raise ConfigError(
            f"SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters, got {len(key)}."
        )
    return key
