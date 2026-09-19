"""Logging configuration shared by the bot and the OAuth server.

Two output formats are supported, chosen with ``LOG_FORMAT``:

``text``
    The default. One readable line per record, the same shape both processes
    printed before this module existed.
``json``
    One JSON object per line, so a log shipper does not have to reverse the
    text format with a regular expression. Anything passed as ``extra=`` to a
    log call becomes a typed field rather than being flattened into the
    message, which is what makes the star-check summary line queryable.

``LOG_LEVEL`` keeps working exactly as it did, except that an unusable value
now falls back to INFO instead of raising out of the logging setup itself.

The request ID lives in a :mod:`contextvars` variable rather than being passed
to every log call. Only the server sets it; every helper the request touches,
including the ones in ``common``, would otherwise have to take a parameter it
does not use.
"""

import contextvars
import json
import logging
import os
import sys
from collections.abc import Mapping
from typing import Final

# Set per request by the OAuth server, read by both formatters. A ContextVar
# is per-thread as well as per-task, so waitress worker threads cannot see
# each other's value.
REQUEST_ID: Final[contextvars.ContextVar[str]] = contextvars.ContextVar(
    "starguard_request_id", default=""
)

DEFAULT_LEVEL: Final = "INFO"
DEFAULT_FORMAT: Final = "text"
SUPPORTED_FORMATS: Final[tuple[str, ...]] = ("text", "json")

TEXT_LINE_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Everything a LogRecord carries on its own. Any other attribute got there
# through an ``extra=`` argument and belongs in the JSON output.
_RESERVED_RECORD_FIELDS: Final[frozenset[str]] = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", (), None))
) | {"message", "asctime", "taskName"}

# Remembered so a second call replaces our handler instead of stacking another
# one on the root logger. Nothing else the process installs is touched, which
# matters under pytest: removing every root handler would also remove the one
# caplog relies on.
_INSTALLED_HANDLER: logging.Handler | None = None


class TextFormatter(logging.Formatter):
    """The human-readable format, with the request ID appended when set."""

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        request_id = REQUEST_ID.get()
        return f"{line} [request_id={request_id}]" if request_id else line


class JsonFormatter(logging.Formatter):
    """One JSON object per record, including any ``extra=`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        # ``object`` rather than a narrower value type: the whole point of the
        # ``extra=`` loop below is that a caller can attach anything.
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = REQUEST_ID.get()
        if request_id:
            payload["request_id"] = request_id

        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # A value that json cannot encode must not be allowed to take down the
        # logging call that was trying to report a problem.
        return json.dumps(payload, default=str)


def resolve_level(raw: str | None) -> int:
    """Return a logging level number for ``raw``, falling back to INFO."""
    if not raw:
        return logging.INFO
    return logging.getLevelNamesMapping().get(raw.strip().upper(), logging.INFO)


def resolve_format(raw: str | None) -> str:
    """Return a supported format name for ``raw``, falling back to text."""
    candidate = (raw or DEFAULT_FORMAT).strip().lower()
    return candidate if candidate in SUPPORTED_FORMATS else DEFAULT_FORMAT


def configure_logging(env: Mapping[str, str] | None = None) -> str:
    """Install the root log handler. Returns the format name that was used.

    Called from each process's ``main()`` rather than at import time, so that
    importing either entry point stays free of side effects.
    """
    global _INSTALLED_HANDLER  # pylint: disable=global-statement

    env = os.environ if env is None else env
    log_format = resolve_format(env.get("LOG_FORMAT"))
    level = resolve_level(env.get("LOG_LEVEL", DEFAULT_LEVEL))

    handler = logging.StreamHandler(sys.stderr)
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter(TEXT_LINE_FORMAT))

    root = logging.getLogger()
    if _INSTALLED_HANDLER is not None:
        root.removeHandler(_INSTALLED_HANDLER)
    root.addHandler(handler)
    root.setLevel(level)
    _INSTALLED_HANDLER = handler

    return log_format
