"""Response hardening and per-address rate limiting for the OAuth server.

Everything here is wired onto an application by :func:`install_security`, so
the routes stay about OAuth and the policy stays in one readable place.
"""

import re
import uuid
from collections.abc import Callable, Iterable
from typing import Final

from flask import Flask, Response, current_app, g, request

from common.logging_setup import REQUEST_ID
from common.ratelimit import RateLimiter

# The templates load one same-origin stylesheet and one same-origin SVG icon,
# and no script at all, so the policy can name no sources beyond 'self' and
# needs neither 'unsafe-inline' nor an external origin. If a future page needs
# a script, give it a file and a hash or a nonce rather than relaxing this.
CONTENT_SECURITY_POLICY: Final = "; ".join(
    (
        "default-src 'none'",
        "style-src 'self'",
        "img-src 'self'",
        "font-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS: Final[dict[str, str]] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    # The /login URL carries a signed link token in its query string, and the
    # next thing the browser does is navigate to github.com. no-referrer keeps
    # that token out of GitHub's request logs.
    "Referrer-Policy": "no-referrer",
    # frame-ancestors already covers this for current browsers; the header is
    # kept for the ones that only understand the old spelling.
    "X-Frame-Options": "DENY",
    "Permissions-Policy": ", ".join(
        f"{feature}=()"
        for feature in (
            "accelerometer",
            "camera",
            "geolocation",
            "gyroscope",
            "interest-cohort",
            "magnetometer",
            "microphone",
            "payment",
            "usb",
        )
    ),
}

REQUEST_ID_HEADER: Final = "X-Request-ID"

# An inbound request ID is echoed so a reverse proxy's trace can be followed
# into these logs, but only if it is boring: it ends up in a log line and in a
# response header, and neither should ever carry a newline or a quote.
_SAFE_REQUEST_ID = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")

RATE_LIMIT_MESSAGE: Final = (
    "Too many verification attempts from your address. Please wait a moment and try again."
)
TOO_MANY_REQUESTS: Final = 429


def client_ip() -> str:
    """Return the address to rate limit by.

    ``request.remote_addr`` is the right source only because ProxyFix has
    already rewritten it from the trusted number of X-Forwarded-For hops.
    Reading the header directly would let any client pick its own bucket.
    """
    return request.remote_addr or "unknown"


def new_request_id() -> str:
    """Return the ID for the request being handled."""
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    if _SAFE_REQUEST_ID.match(supplied):
        return supplied
    return uuid.uuid4().hex


def install_security(
    app: Flask,
    limiter: RateLimiter,
    rate_limited_endpoints: Iterable[str],
    render_error: Callable[[str, int], Response],
) -> None:
    """Attach request IDs, the rate limit and the response headers to ``app``.

    ``render_error(message, status)`` is supplied by the application so a
    refused request gets the same page as every other user-facing outcome.
    """
    limited = frozenset(rate_limited_endpoints)

    @app.before_request
    def _begin_request() -> Response | None:
        request_id = new_request_id()
        g.request_id = request_id
        # Reset in teardown: waitress reuses its worker threads, and a
        # ContextVar set in one would otherwise still be set for the next
        # request that thread picks up.
        g.request_id_token = REQUEST_ID.set(request_id)

        if request.endpoint not in limited:
            return None

        decision = limiter.hit(client_ip())
        if decision.allowed:
            return None

        current_app.logger.warning("Rate limited %s for %s", request.endpoint, client_ip())
        response = render_error(RATE_LIMIT_MESSAGE, TOO_MANY_REQUESTS)
        response.headers["Retry-After"] = str(decision.retry_after)
        return response

    @app.after_request
    def _finish_request(response: Response) -> Response:
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        request_id = g.get("request_id")
        if request_id:
            response.headers[REQUEST_ID_HEADER] = request_id
        # Pages are per-user and one of them is reached with a token in the
        # URL, so no shared cache should keep any of them. Static files keep
        # whatever Flask decided for them.
        if request.endpoint != "static":
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.teardown_request
    def _clear_request_id(_exception: BaseException | None = None) -> None:
        token = g.pop("request_id_token", None)
        if token is not None:
            REQUEST_ID.reset(token)
