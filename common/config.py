"""Environment configuration helpers shared by the bot and the OAuth server.

Every value the project cannot run without is read through :func:`require_env`
so that a misconfigured deployment fails immediately, with a message naming the
variable, instead of crashing later with an opaque error.
"""

import os


class ConfigError(RuntimeError):
    """Raised when the environment is missing or has an unusable value."""


# Values shipped in .env.example or commonly pasted from tutorials. A Flask
# secret key that anyone can guess lets an attacker forge session cookies, so
# these are rejected outright rather than merely warned about.
PLACEHOLDER_SECRET_KEYS = frozenset({
    "secretkey",
    "changeme",
    "change-me",
    "secret",
    "your-secret-key",
    "please-change-me",
})

MIN_SECRET_KEY_LENGTH = 16


def optional_env(name, default=None):
    """Return the stripped value of ``name``, or ``default`` when unset/blank."""
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def require_env(name, hint=None):
    """Return the value of ``name``, raising :class:`ConfigError` when unset."""
    value = optional_env(name)
    if value is None:
        message = f"Required environment variable {name} is not set."
        if hint:
            message = f"{message} {hint}"
        raise ConfigError(message)
    return value


def env_int(name, default, minimum=None):
    """Return ``name`` as an int, clamped to ``minimum`` when one is given."""
    raw = optional_env(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigError(
                f"{name} must be a whole number, got {raw!r}."
            ) from exc
    if minimum is not None and value < minimum:
        return minimum
    return value


def env_bool(name, default=False):
    """Return ``name`` as a bool, accepting the usual true/false spellings."""
    raw = optional_env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(
        f"{name} must be a boolean such as true/false, got {raw!r}."
    )


def require_snowflake(name):
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


def require_secret_key():
    """Return SECRET_KEY, rejecting unset, placeholder, and too-short values.

    The bot signs verification links with this key and the server verifies
    them, so a predictable key would let anyone mint a link for any Discord
    account.
    """
    key = require_env(
        "SECRET_KEY",
        hint="Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\"",
    )
    if key.lower() in PLACEHOLDER_SECRET_KEYS:
        raise ConfigError(
            "SECRET_KEY is still set to a placeholder value. Generate a real "
            'one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    if len(key) < MIN_SECRET_KEY_LENGTH:
        raise ConfigError(
            f"SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters, "
            f"got {len(key)}."
        )
    return key
