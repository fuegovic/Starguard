"""The application the browser drives, and the two stand-ins it needs.

Every page a scenario looks at is produced by the real :func:`create_app`
factory, the real view functions and the real Jinja templates, served over a
real socket. Only the two things a test cannot have are replaced, and both
are replaced at the edge rather than patched inside a view: GitHub, and
MongoDB. That boundary matters more here than it does in the unit suite,
because the result page takes its tone from the HTTP status, so a fake that
short-circuited the view would be checking the fake rather than the page.
"""

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

import mongomock
from authlib.integrations.flask_client import OAuthError
from flask import Flask, request
from werkzeug.serving import make_server
from werkzeug.utils import redirect
from werkzeug.wrappers import Response as WerkzeugResponse

from common.linktoken import issue_link_token
from common.storage import UserCollection, connect
from server.config import ServerConfig
from server.server import NOT_STARRED_STATUS, STARRED_STATUS, ServerContext, create_app

SECRET: Final = "0123456789abcdef-a-real-looking-key"
DISCORD_ID: Final = 123456789
DISCORD_USERNAME: Final = "someone"
PROFILE: Final[Mapping[str, object]] = {"login": "Octocat", "id": 583231}
ACCESS_TOKEN: Final[Mapping[str, object]] = {"access_token": "gho_never_stored"}

# Which way the sign-in goes is carried in the query string rather than set
# on the fake up front, because that is how the real callback carries state:
# the browser leaves for github.com and comes back to /authorize with
# parameters on the URL. Threading the choice through the redirect keeps the
# requests a scenario makes the same two requests a member's browser makes.
OUTCOME_STARRED: Final = "starred"
OUTCOME_NOT_STARRED: Final = "not-starred"
OUTCOME_REFUSED: Final = "refused"
OUTCOME_PARAMETER: Final = "outcome"

# GitHub writes this text, not Starguard, so neither its length nor where it
# can be broken is under this project's control. A refused redirect_uri is
# the realistic long one, and the unbroken run of characters at the end of
# it is the case `overflow-wrap: break-word` in the stylesheet exists for:
# without it this is what pushes the page sideways on a narrow screen.
REFUSAL_REASON: Final = (
    "The redirect_uri must match the callback URL registered for this "
    "OAuth application, and the one presented was "
    "https://starguard.example.test/authorize?state=" + "0123456789abcdef" * 6
)


def _outcome() -> str:
    """Return the outcome the current request asked the fake GitHub for."""
    return request.args.get(OUTCOME_PARAMETER, OUTCOME_STARRED)


@dataclass(frozen=True)
class FakeApiResponse:
    """The slice of an Authlib HTTP response the views read."""

    status_code: int
    payload: Mapping[str, object] | None = None

    def json(self) -> Any:  # noqa: ANN401
        """The parsed JSON body, as Authlib hands it back."""
        return self.payload


class FakeGitHub:
    """GitHub's OAuth client, answering from the query string.

    The three methods are the ones :class:`server.server.OAuthClient`
    declares, with the same signatures, so the views cannot tell this apart
    from the registered client except by the absence of a network.
    """

    def __init__(self) -> None:
        self.profile = dict(PROFILE)

    def authorize_redirect(self, redirect_uri: str) -> WerkzeugResponse:
        """Stand in for the trip to github.com and back.

        The real client sends a 302 to github.com, which sends a second 302
        back to ``redirect_uri``. This collapses the pair into the one hop
        that stays on this origin, and carries the requested outcome across
        it the way GitHub carries ``state``.
        """
        return redirect(f"{redirect_uri}?{OUTCOME_PARAMETER}={_outcome()}")

    def authorize_access_token(self) -> Mapping[str, object] | None:
        """Exchange the callback for a token, or refuse the way GitHub does."""
        if _outcome() == OUTCOME_REFUSED:
            raise OAuthError(description=REFUSAL_REASON)
        return ACCESS_TOKEN

    def get(self, url: str, *, token: Mapping[str, object]) -> FakeApiResponse:
        """Answer the profile fetch and the starred check."""
        del token
        if url == "user":
            return FakeApiResponse(200, dict(self.profile))
        starred = _outcome() == OUTCOME_STARRED
        return FakeApiResponse(STARRED_STATUS if starred else NOT_STARRED_STATUS)


def make_config(**overrides: object) -> ServerConfig:
    """Build a server configuration for the harness."""
    settings: dict[str, object] = {
        "owner": "owner",
        "repo": "repo",
        "secret_key": SECRET,
        "client_id": "client-id",
        "client_secret": "client-secret",
        "mongo_host": "mongodb://127.0.0.1:27017/",
        "mongo_database": "starguard_e2e",
        "port": 0,
        "link_token_max_age": 900,
        # Zero hops leaves ProxyFix off, which is what a server reached
        # directly on a published port gets; the browser here is exactly
        # that client.
        "trusted_proxy_count": 0,
        # Far above anything the suite can reach. Every scenario connects
        # from 127.0.0.1, so they all share one bucket, and a limit set at
        # the production default would start refusing whichever scenario
        # happened to run last. The limiter has its own unit tests.
        "rate_limit": 10_000,
        "rate_limit_window": 60,
    }
    settings.update(overrides)
    return ServerConfig(**settings)  # type: ignore[arg-type]


def build_app() -> Flask:
    """Build the application the browser talks to.

    ``create_app`` is called first so the application is assembled exactly
    as production assembles it, including the security headers and the
    OAuth registration; the context it stored is then swapped for one
    carrying the fake client. The registered client is never called.
    """
    config = make_config()
    users: UserCollection
    _, users = connect(config.mongo_host, config.mongo_database, mongomock.MongoClient)
    app = create_app(config, users=users)
    # Nothing in app.config is touched, including SESSION_COOKIE_SECURE.
    # The unit suite has to clear that flag because its test client is not a
    # browser; a real one treats http://127.0.0.1 as a potentially
    # trustworthy origin and keeps the Secure session cookie anyway, so the
    # two requests of a verification hold together against the production
    # setting rather than a loosened one.
    app.extensions["starguard"] = ServerContext(config=config, users=users, github=FakeGitHub())
    return app


@dataclass(frozen=True)
class LiveServer:
    """A Starguard listening on loopback, and the URLs a scenario asks of it."""

    base_url: str

    def home(self) -> str:
        """The landing page."""
        return f"{self.base_url}/"

    def verification(self, outcome: str = OUTCOME_STARRED) -> str:
        """A whole verification, from the link the bot sends to the result.

        Opening this leaves the browser on the result page having passed
        through /login, the redirect that stands in for github.com and
        /authorize, which is the sequence a member's browser performs.
        """
        token = issue_link_token(SECRET, DISCORD_ID, DISCORD_USERNAME)
        return f"{self.base_url}/login?token={token}&{OUTCOME_PARAMETER}={outcome}"

    def missing_token(self) -> str:
        """/login with no token at all: the shortest error page."""
        return f"{self.base_url}/login"


@contextmanager
def running(app: Flask) -> Iterator[LiveServer]:
    """Serve ``app`` on a free loopback port for the duration of the block."""
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield LiveServer(f"http://127.0.0.1:{server.port}")
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()
