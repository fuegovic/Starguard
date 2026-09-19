"""Signed, expiring tokens that bind a Discord identity to a login link.

The bot mints a token containing the invoking user's Discord ID and hands it to
them as part of the OAuth URL; the server verifies the signature before it will
associate a GitHub account with that ID. Without this, the Discord ID was an
unauthenticated query parameter and anyone could start the flow claiming to be
any Discord user.

Both sides derive the signing key from SECRET_KEY, so the bot and the server
must share the same value.
"""

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

# Namespaces the signature so a token is only ever valid for this purpose.
SALT = "starguard-discord-link-v1"

# Long enough to star a repo and log in, short enough that a leaked URL in a
# browser history or proxy log stops working quickly.
DEFAULT_MAX_AGE_SECONDS = 900


class LinkTokenError(ValueError):
    """Raised when a link token is missing, malformed, forged, or expired."""


def _serializer(secret_key):
    if not secret_key:
        raise LinkTokenError("No SECRET_KEY configured for link tokens.")
    return URLSafeTimedSerializer(secret_key, salt=SALT)


def issue_link_token(secret_key, discord_id, discord_username):
    """Return a signed token carrying the Discord identity of the requester."""
    payload = {"id": str(discord_id), "name": str(discord_username)}
    return _serializer(secret_key).dumps(payload)


def read_link_token(secret_key, token, max_age=DEFAULT_MAX_AGE_SECONDS):
    """Return ``(discord_id, discord_username)`` from a valid token.

    Raises :class:`LinkTokenError` for anything else, so callers never have to
    distinguish "absent" from "tampered with".
    """
    if not token:
        raise LinkTokenError("Missing verification token.")

    try:
        payload = _serializer(secret_key).loads(token, max_age=max_age)
    except SignatureExpired as exc:
        raise LinkTokenError(
            "This verification link has expired. Run /verify again."
        ) from exc
    except BadSignature as exc:
        raise LinkTokenError("This verification link is not valid.") from exc

    if not isinstance(payload, dict):
        raise LinkTokenError("This verification link is not valid.")

    discord_id = payload.get("id")
    discord_username = payload.get("name")
    if not discord_id or not str(discord_id).isdigit():
        raise LinkTokenError("This verification link is not valid.")

    return str(discord_id), str(discord_username or "")
