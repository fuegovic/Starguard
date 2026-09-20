"""A health signal for the bot process.

The OAuth server answers /healthz, but the bot opened no socket at all, so its
container could only report "the process has not exited yet" and therefore had
no healthcheck worth writing. This gives it one.

An HTTP endpoint was chosen over a heartbeat file because a container
healthcheck can then be a plain GET with no knowledge of a path or a clock
skew, and because the same check works from outside the container when the
port is published. It binds to loopback by default, so nothing is exposed
that was not exposed before.

What it can and cannot tell you: ``ready`` flips on the Startup event, and the
library reconnects to the gateway on its own, so a brief disconnect is not
reported. The real liveness signal is the age of the last completed star
check, which only exists when AUTOMATIC_CHECK is on. With automatic checks
disabled the endpoint degrades to "the event loop reached Startup", which is
still more than the container had before.
"""

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

log = logging.getLogger("starguard.bot")

HEALTH_PATH: Final = "/healthz"

# A cycle is late rather than broken until three intervals have passed, plus
# a margin for a cycle that is simply slow on a large repository.
STALE_CYCLE_MULTIPLIER: Final = 3
STALE_CYCLE_GRACE_SECONDS: Final = 300

# What /healthz answers with. ``object`` because the payload mixes the status
# strings with the two rounded ages.
HealthPayload = dict[str, object]


class HealthState:
    """What the endpoint reports. Written from the event loop, read from the
    HTTP thread, so every access is under the lock."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._started_at = clock()
        self._ready = False
        self._stale_after: float | None = None

    def mark_ready(self, stale_after: float | None = None) -> None:
        """Record that the gateway connected.

        ``stale_after`` is the age at which a missing star check counts as a
        problem, or None when automatic checks are off and there is nothing
        to be late.
        """
        with self._lock:
            self._ready = True
            self._stale_after = stale_after

    def report(self, last_check_completed: float | None = None) -> tuple[HealthPayload, HTTPStatus]:
        """Return ``(payload, http_status)`` for the current state."""
        with self._lock:
            now = self._clock()
            payload: HealthPayload = {
                "status": "ok",
                "uptime_seconds": round(now - self._started_at, 1),
                "gateway": "connected" if self._ready else "connecting",
            }

            if not self._ready:
                payload["status"] = "starting"
                return payload, HTTPStatus.SERVICE_UNAVAILABLE

            if self._stale_after is None:
                payload["star_check"] = "disabled"
                return payload, HTTPStatus.OK

            if last_check_completed is None:
                age = now - self._started_at
                payload["star_check"] = "pending"
            else:
                age = now - last_check_completed
                payload["star_check"] = "ok"
                payload["last_check_age_seconds"] = round(age, 1)

            if age > self._stale_after:
                payload["status"] = "degraded"
                payload["star_check"] = "stale"
                return payload, HTTPStatus.SERVICE_UNAVAILABLE

            return payload, HTTPStatus.OK


def stale_after_seconds(check_delay: int) -> int:
    """Return how old a completed check may get before it is a problem."""
    return check_delay * STALE_CYCLE_MULTIPLIER + STALE_CYCLE_GRACE_SECONDS


def _handler_class(
    state: HealthState, last_completed: Callable[[], float | None]
) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to ``state``."""

    class HealthHandler(BaseHTTPRequestHandler):
        """Answers GET /healthz and nothing else."""

        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # pylint: disable=invalid-name
            """Report the bot's health. The name is the BaseHTTPRequestHandler
            dispatch convention, which is not snake_case."""
            if self.path.split("?", 1)[0] != HEALTH_PATH:
                self._respond({"status": "not found"}, HTTPStatus.NOT_FOUND)
                return
            payload, status = state.report(last_completed())
            self._respond(payload, status)

        def _respond(self, payload: Mapping[str, object], status: HTTPStatus) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(  # pylint: disable=redefined-builtin
            self, format: str, *args: object
        ) -> None:
            """Route the server's own logging through ours instead of stderr.

            The signature is fixed by BaseHTTPRequestHandler.
            """
            log.debug("health: " + format, *args)

    return HealthHandler


def serve_health(
    state: HealthState,
    host: str,
    port: int,
    last_completed: Callable[[], float | None],
) -> ThreadingHTTPServer | None:
    """Start the health endpoint on a daemon thread. Returns the server.

    ``last_completed`` is a callable so the endpoint reads the checker's
    current value rather than a copy taken at startup. Returns None when the
    port cannot be bound: a missing health endpoint must not stop the bot
    from doing its actual job.

    OverflowError is caught alongside OSError because it is not one, and
    bind raises it rather than an OSError for a port above 65535. Nothing
    validates BOT_HEALTH_PORT against that ceiling, so catching only OSError
    meant a single mistyped digit killed the whole bot before it reached the
    gateway, over an endpoint that is documented as optional.
    """
    try:
        server = ThreadingHTTPServer((host, port), _handler_class(state, last_completed))
    except (OSError, OverflowError) as exc:
        log.error("Could not start the health endpoint on %s:%s: %s", host, port, exc)
        return None

    thread = threading.Thread(target=server.serve_forever, name="starguard-health", daemon=True)
    thread.start()
    log.info("Health endpoint listening on http://%s:%s%s", host, port, HEALTH_PATH)
    return server
