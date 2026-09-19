"""GitHub ``star`` webhook receiver.

Before this existed the bot learned about un-stars by walking every page of
the stargazer list on a timer. At about forty-five thousand stars that is
four hundred and fifty pages an hour, and it grows every year, so the hourly
sweep was both the slowest thing the bot did and the reason a role could take
an hour to move. A webhook turns the common case into one small request per
event; the sweep stays as the backstop for whatever a webhook missed.

The delivery arrives at *this* process, and only the bot process can change a
Discord role. The two never talk to each other, so the ``users`` collection is
the whole channel between them: this route records what changed and raises
``role_sync_pending``, and the bot polls for the rows that are still raised.
Nothing here calls Discord or GitHub. GitHub wants a 2XX within ten seconds
and its own documentation asks receivers to defer the work, so a delivery
costs one indexed insert and one indexed read-and-update, and no network call.

Authentication is the HMAC and nothing else. The ``User-Agent`` and the source
address can both be forged and neither is checked; the signature cannot be,
and it is verified over the raw bytes before anything else looks at them.

The route is deliberately **not** in ``RATE_LIMITED_ENDPOINTS``. Every real
delivery comes from a handful of GitHub addresses, so a per-address limit
would put the entire event stream in one bucket, and a burst of stars is
exactly when the bucket overflows. GitHub does not retry a failed delivery,
so a 429 is not a delay, it is an event that never arrives and a role that
never moves. The flood case that limiting would answer is already cheap here:
an unsigned request costs one SHA-256 over a body that ``MAX_CONTENT_LENGTH``
bounds, and touches no database, no socket and no log line. Volume control
belongs at the proxy in front, which can drop a connection for less than this
process spends accepting one.
"""

import hashlib
import hmac
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from flask import Flask, Response, current_app, request
from pymongo.errors import PyMongoError

from common.storage import (
    STAR_SOURCE_WEBHOOK,
    UserCollection,
    claim_delivery,
    deliveries_for,
    record_star_event,
)

log = logging.getLogger("starguard.webhooks")

WEBHOOK_PATH: Final = "/webhooks/github"
EXTENSION_KEY: Final = "starguard_webhook"

EVENT_HEADER: Final = "X-GitHub-Event"
DELIVERY_HEADER: Final = "X-GitHub-Delivery"
HOOK_ID_HEADER: Final = "X-GitHub-Hook-ID"
SIGNATURE_HEADER: Final = "X-Hub-Signature-256"
SIGNATURE_PREFIX: Final = "sha256="

PING_EVENT: Final = "ping"
STAR_EVENT: Final = "star"

# The star event's two actions and the star state each one leaves behind.
# ``starred_at`` is not used as the event time: it is null for ``deleted``,
# so the two actions would be timed by different clocks, and deliveries are
# not ordered anyway. The moment of receipt is the one clock that exists for
# both.
STAR_ACTIONS: Final[Mapping[str, bool]] = {"created": True, "deleted": False}

# A star payload is a few kilobytes of repository and sender. One mebibyte is
# far above anything GitHub sends and far below what an unauthenticated
# sender could make this process hash: the body has to be read into memory
# before the signature over it can be computed, so the bound is what keeps
# that cost fixed. Applied as Flask's MAX_CONTENT_LENGTH in create_app, which
# refuses an oversized body rather than reading it.
MAX_REQUEST_BODY_BYTES: Final = 1024 * 1024

HTTP_OK: Final = 200
HTTP_ACCEPTED: Final = 202
HTTP_NO_CONTENT: Final = 204
HTTP_BAD_REQUEST: Final = 400
HTTP_UNAUTHORIZED: Final = 401
HTTP_NOT_FOUND: Final = 404
HTTP_SERVICE_UNAVAILABLE: Final = 503

# A hex SHA-256 digest and nothing else. Checking the shape before comparing
# means hmac.compare_digest only ever sees two ASCII strings of the same
# length: it raises TypeError on a non-ASCII one, and this value is chosen by
# whoever sent the request.
_HEX_DIGEST: Final = re.compile(r"\A[0-9a-fA-F]{64}\Z")


@dataclass(frozen=True)
class WebhookContext:
    """The three things the receiver needs, fixed when the route is installed.

    Separate from ``ServerContext`` so this module stays independent of the
    OAuth server it is registered on, rather than importing it back.
    """

    secret: str
    # Lower-cased, and compared against a lower-cased payload; see _repository.
    full_name: str
    users: UserCollection | None


def _context() -> WebhookContext:
    """Return the current application's :class:`WebhookContext`."""
    context: WebhookContext = current_app.extensions[EXTENSION_KEY]
    return context


def _reply(status: int, note: str = "") -> Response:
    """Answer with ``status`` and, where it helps, one line of plain text.

    The body is read by a person looking at GitHub's delivery log, not by a
    program, and it says nothing back to the sender that the sender did not
    already know. A 204 carries none at all.
    """
    return current_app.response_class(note, status=status, mimetype="text/plain")


def _signature_matches(secret: str, body: bytes, header: str | None) -> bool:
    """Say whether ``header`` is GitHub's HMAC of ``body`` under ``secret``."""
    if header is None or not header.startswith(SIGNATURE_PREFIX):
        return False
    supplied = header.removeprefix(SIGNATURE_PREFIX)
    if not _HEX_DIGEST.match(supplied):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # compare_digest rather than ==, so the number of leading bytes that
    # happen to be right is not readable from how long the answer took.
    return hmac.compare_digest(expected, supplied.lower())


def _parse_object(body: bytes) -> Mapping[str, object] | None:
    """Return ``body`` as a JSON object, or None when it is not one.

    The HMAC proves who sent these bytes, not that they hold what this code
    expects, so everything read out of the result below is still untrusted.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        # JSONDecodeError for bad syntax, UnicodeDecodeError for bad UTF-8.
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _repository(payload: Mapping[str, object]) -> str | None:
    """Return ``repository.full_name``, lower-cased, or None when absent.

    Lower-cased because GitHub stores the canonical capitalisation of a name
    and treats it as case-insensitive everywhere else, including the starred
    check this server already makes. An operator who writes ``Owner/Repo`` in
    the environment has a working configuration, not a hook pointed at
    somebody else's repository, and should not have every delivery refused.
    """
    repository = payload.get("repository")
    if not isinstance(repository, Mapping):
        return None
    full_name = repository.get("full_name")
    if not isinstance(full_name, str):
        return None
    return full_name.lower()


def _sender_id(payload: Mapping[str, object]) -> int | None:
    """Return ``sender.id``, the immutable numeric GitHub account id.

    Never ``sender.login``. A login can be changed by its owner at any time,
    and a lookup by a stale login finds nothing, which reads exactly like
    "this person never verified": the member quietly keeps or loses a role
    for a reason nobody can see in a log. That bug has already been paid for
    once in this project.
    """
    sender = payload.get("sender")
    if not isinstance(sender, Mapping):
        return None
    github_id = sender.get("id")
    # bool is a subclass of int, so a JSON `true` would otherwise be read as
    # account number 1, which is a real GitHub account.
    if isinstance(github_id, bool) or not isinstance(github_id, int):
        return None
    return github_id


def _handle_star(users: UserCollection, payload: Mapping[str, object]) -> Response:
    """Record one verified star event. Raises PyMongoError to the caller."""
    delivery_id = request.headers.get(DELIVERY_HEADER)
    if not delivery_id:
        # GitHub always sends one. Without it there is no dedupe key, and
        # claiming every such request under one shared key would make the
        # second one look like a replay of the first.
        return _reply(HTTP_BAD_REQUEST, f"Missing {DELIVERY_HEADER}.")

    # The claim comes before anything is acted on, and an insert against a
    # unique index is what makes the test and the record one step. GitHub
    # reuses the delivery id when an operator redelivers by hand, and the
    # remembered ids expire after ten minutes so that recovery still works.
    if not claim_delivery(deliveries_for(users), delivery_id, datetime.now(UTC)):
        log.info("Ignoring replayed webhook delivery %s.", delivery_id)
        return _reply(HTTP_OK, "Already handled.")

    action = payload.get("action")
    if not isinstance(action, str):
        return _reply(HTTP_BAD_REQUEST, "Missing action.")
    if action not in STAR_ACTIONS:
        # A star action this version does not know, which would mean GitHub
        # added one. Nothing to do is a success, not a failed delivery.
        log.info("Ignoring unknown star action %r.", action)
        return _reply(HTTP_NO_CONTENT)

    github_id = _sender_id(payload)
    if github_id is None:
        return _reply(HTTP_BAD_REQUEST, "Missing sender id.")

    updated = record_star_event(
        users,
        github_id=github_id,
        starred=STAR_ACTIONS[action],
        source=STAR_SOURCE_WEBHOOK,
        occurred_at=datetime.now(UTC),
    )
    if updated is None:
        # Nobody has linked that GitHub account. This is the common case by
        # a wide margin, because the event fires for everyone who stars the
        # repository and almost none of them use the bot, so it costs one
        # indexed lookup and a debug line rather than a log flood.
        log.debug("Star %s by GitHub id %s belongs to no verified member.", action, github_id)
        return _reply(HTTP_NO_CONTENT)

    log.info(
        "Recorded star %s for GitHub id %s (pending role sync: %s).",
        action,
        github_id,
        bool(updated.get("role_sync_pending")),
    )
    return _reply(HTTP_ACCEPTED, "Recorded.")


def github_webhook() -> Response:
    """Verify and record one GitHub webhook delivery."""
    context = _context()

    # The raw bytes, before anything parses them. The signature covers this
    # exact sequence, so verifying a re-serialised body would be verifying
    # something GitHub never sent.
    body = request.get_data()

    if not _signature_matches(context.secret, body, request.headers.get(SIGNATURE_HEADER)):
        # Debug, not warning, and deliberately so. This route is not rate
        # limited, so a line per rejected request is a way to fill a disk
        # from outside. The two readers who need this both have it already:
        # the reverse proxy's access log records every 401 with its source,
        # and an operator whose secret does not match sees the status and
        # this body in GitHub's own delivery log, next to the field they
        # would have to correct.
        log.debug("Rejected a webhook delivery with a bad or missing signature.")
        return _reply(HTTP_UNAUTHORIZED, "Invalid signature.")

    event = request.headers.get(EVENT_HEADER, "")
    if event == PING_EVENT:
        log.info("Ping received for hook %s.", request.headers.get(HOOK_ID_HEADER, "unknown"))
        return _reply(HTTP_OK, "pong")
    if event != STAR_EVENT:
        # Subscribed to more than star, or GitHub sent something new. Taking
        # it and doing nothing keeps the delivery log green for an event that
        # is not a problem.
        return _reply(HTTP_NO_CONTENT)

    payload = _parse_object(body)
    if payload is None:
        return _reply(HTTP_BAD_REQUEST, "Body is not a JSON object.")

    full_name = _repository(payload)
    if full_name != context.full_name:
        # A hook on the wrong repository is a misconfiguration that would
        # otherwise show up as roles that never move, so it is refused
        # loudly: 404 is what an operator sees in the delivery log.
        log.error("Refused a star event for %r, which is not this repository.", full_name)
        return _reply(HTTP_NOT_FOUND, "Hook is configured for another repository.")

    if context.users is None:
        log.error("Cannot record a star event: no database connection.")
        return _reply(HTTP_SERVICE_UNAVAILABLE, "Database unavailable.")

    try:
        return _handle_star(context.users, payload)
    except PyMongoError as exc:
        # The database was reachable at startup and is not now. A 5xx here
        # marks the delivery failed in GitHub's log, which is the one place
        # an operator can redeliver it from once the database is back.
        log.error("Could not record a star event: %s", exc)
        return _reply(HTTP_SERVICE_UNAVAILABLE, "Database unavailable.")


def install_webhook(
    app: Flask,
    secret: str,
    owner: str,
    repo: str,
    users: UserCollection | None,
) -> None:
    """Register the receiver on ``app``.

    Called only when a secret is configured. An installation without one has
    no such route at all, rather than one that answers 404: a route that
    exists can be probed, measured and eventually mistaken for working, and
    there is nothing it could do without a secret to verify against.
    """
    app.extensions[EXTENSION_KEY] = WebhookContext(
        secret=secret,
        full_name=f"{owner}/{repo}".lower(),
        users=users,
    )
    app.add_url_rule(WEBHOOK_PATH, view_func=github_webhook, methods=["POST"])
