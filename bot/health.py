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
reported. The real liveness signal is the age of the last completed pass of
each loop that reconciles roles, and there are two of them: the periodic star
check and the role-sync drain. Both are optional, and a loop that is turned
off reports itself disabled rather than late. With both off the endpoint
degrades to "the event loop reached Startup", which is still more than the
container had before.

Reporting only the star check was not enough, because the two loops are
configured independently. A deployment running on webhooks alone, with
AUTOMATIC_CHECK=false and ROLE_SYNC_ENABLED=true, has the drain as its only
reconciling loop; if MongoDB was unreachable when the bot started, every
drain returns without doing anything and never reconnects, and the endpoint
used to answer 200 for as long as that lasted.
"""

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

log = logging.getLogger("starguard.bot")

HEALTH_PATH: Final = "/healthz"

# A pass is late rather than broken until three intervals have passed, plus
# a margin for a cycle that is simply slow on a large repository. The margin
# is generous for the drain, whose interval is seconds rather than minutes,
# and that is the right way to be wrong: the number that matters is that
# there is a ceiling at all.
STALE_CYCLE_MULTIPLIER: Final = 3
STALE_CYCLE_GRACE_SECONDS: Final = 300

# What /healthz answers with. ``object`` because the payload mixes the status
# strings with the rounded ages.
HealthPayload = dict[str, object]


@dataclass(frozen=True)
class LoopHealth:
    """One background loop, as the endpoint reports it.

    ``field`` is what the payload calls the loop and ``age_field`` what it
    calls the age of its last completed pass. ``stale_after`` is how old
    that pass may get before the process counts as degraded, or None when
    the loop is turned off and nothing can be late.

    ``last_completed`` is a callable rather than a value so the endpoint
    reads what the loop holds now instead of a copy taken at registration,
    which is also how the HTTP thread gets at state the event loop owns.
    """

    field: str
    age_field: str
    stale_after: float | None
    last_completed: Callable[[], float | None]


class HealthState:
    """What the endpoint reports. Written from the event loop, read from the
    HTTP thread, so every access is under the lock."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._started_at = clock()
        self._ready = False
        self._loops: list[LoopHealth] = []

    def watch(self, loop: LoopHealth) -> None:
        """Report ``loop``'s freshness as part of the bot's health."""
        with self._lock:
            self._loops.append(loop)

    def mark_ready(self) -> None:
        """Record that the gateway connected.

        Separate from :meth:`watch` because the two answer different
        questions and are known at different times: which loops exist is a
        fact about the configuration, settled when the client is built,
        while this is the Startup event actually arriving.
        """
        with self._lock:
            self._ready = True

    def report(self) -> tuple[HealthPayload, HTTPStatus]:
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

            # Every loop is described, and then the statuses are combined,
            # rather than returning at the first stale one: an operator
            # reading the payload wants to know which loop stopped, and
            # with two of them the first one asked is not always the one
            # that did.
            stale = [loop for loop in self._loops if self._describe(payload, loop, now)]
            if stale:
                payload["status"] = "degraded"
                return payload, HTTPStatus.SERVICE_UNAVAILABLE

            return payload, HTTPStatus.OK

    def _describe(self, payload: HealthPayload, loop: LoopHealth, now: float) -> bool:
        """Write ``loop``'s state into ``payload``. Returns whether it is stale."""
        if loop.stale_after is None:
            payload[loop.field] = "disabled"
            return False

        last_completed = loop.last_completed()
        if last_completed is None:
            # A loop that has never finished a pass is given the same
            # grace from startup that a completed one gets from its last
            # pass, so a slow first cycle is not reported as a failure.
            age = now - self._started_at
            payload[loop.field] = "pending"
        else:
            age = now - last_completed
            payload[loop.field] = "ok"
            payload[loop.age_field] = round(age, 1)

        if age > loop.stale_after:
            payload[loop.field] = "stale"
            return True

        return False


def stale_after_seconds(check_delay: int) -> int:
    """Return how old a completed check may get before it is a problem."""
    return check_delay * STALE_CYCLE_MULTIPLIER + STALE_CYCLE_GRACE_SECONDS


def _handler_class(state: HealthState) -> type[BaseHTTPRequestHandler]:
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
            payload, status = state.report()
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


def serve_health(state: HealthState, host: str, port: int) -> ThreadingHTTPServer | None:
    """Start the health endpoint on a daemon thread. Returns the server.

    What it reports on is whatever has been handed to :meth:`HealthState.watch`
    by the time a request arrives. Returns None when the port cannot be
    bound: a missing health endpoint must not stop the bot from doing its
    actual job.

    OverflowError is caught alongside OSError because it is not one, and
    bind raises it rather than an OSError for a port above 65535. Nothing
    validates BOT_HEALTH_PORT against that ceiling, so catching only OSError
    meant a single mistyped digit killed the whole bot before it reached the
    gateway, over an endpoint that is documented as optional.
    """
    try:
        server = ThreadingHTTPServer((host, port), _handler_class(state))
    except (OSError, OverflowError) as exc:
        log.error("Could not start the health endpoint on %s:%s: %s", host, port, exc)
        return None

    thread = threading.Thread(target=server.serve_forever, name="starguard-health", daemon=True)
    thread.start()
    log.info("Health endpoint listening on http://%s:%s%s", host, port, HEALTH_PATH)
    return server
