"""GitHub OAuth callback server for Starguard.

Handles two routes: ``/login`` starts the OAuth flow for a Discord user who
presented a signed link token from the bot, and ``/authorize`` records whether
that user has starred the configured repository.

The GitHub access token is used for the duration of the request and then
discarded. It is never written to the database or to the logs.

Importing this module does nothing. Everything is built by
:func:`create_app`, which is what lets the tests construct an application with
an injected configuration instead of reloading the module with a doctored
environment, and what stops ``import server.server`` from trying to reach
MongoDB.
"""

import enum
import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, TypedDict

from authlib.integrations.flask_client import OAuth, OAuthError
from dotenv import load_dotenv
from flask import Flask, Response, current_app, render_template, request, session, url_for
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from waitress import serve
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.wrappers import Response as WerkzeugResponse

from common.config import ConfigError
from common.linktoken import LinkTokenError, read_link_token
from common.logging_setup import configure_logging
from common.ratelimit import RateLimiter
from common.storage import (
    AccountAlreadyLinkedError,
    UserCollection,
    connect,
    link_account,
)
from server import messages
from server.config import ServerConfig, load_server_config
from server.security import install_security

log = logging.getLogger("starguard.server")

# The scope needed to read a public profile and check whether the authenticated
# user starred a public repository. This used to request `repo`, which grants
# read and write access to every private repository the user owns: far beyond
# what a Discord role bot needs, and a serious thing to ask of a visitor.
GITHUB_OAUTH_SCOPE: Final = "read:user"

# GitHub answers "is this repo starred by me" with 204 (yes) or 404 (no).
STARRED_STATUS: Final = 204

RATE_LIMITED_ENDPOINTS: Final[tuple[str, ...]] = ("login", "authorize")


# Distinguishes "no collection was passed" from "the database is down", which
# are different states that both look like None. A one-member enum rather
# than a bare object() so the sentinel has a type a checker can tell apart
# from a real collection; `is` comparisons behave exactly as they did.
class _NotSupplied(enum.Enum):
    TOKEN = enum.auto()


_NOT_SUPPLIED: Final = _NotSupplied.TOKEN


class GitHubProfile(TypedDict):
    """The two fields of GitHub's ``/user`` response that Starguard reads.

    A missing key still raises KeyError at the point of use, which is what
    the caller catches; this only says what the two present keys hold.
    """

    login: str
    id: int


class OAuthResponse(Protocol):
    """The slice of an Authlib HTTP response that the routes look at."""

    @property
    def status_code(self) -> int:
        """The HTTP status GitHub answered with."""

    # Any because this is the raw parsed JSON body. Each call site below
    # immediately gives it a shape: GitHubProfile for the profile fetch, and
    # nothing at all for the starred check, which only reads the status.
    def json(self) -> Any:  # noqa: ANN401
        """The parsed JSON body."""


class OAuthClient(Protocol):
    """The slice of the Authlib OAuth client that the routes use.

    Authlib ships no type information and has no stub package, so naming the
    three methods Starguard actually calls is more useful than an Any that
    would accept a typo.
    """

    def authorize_redirect(self, redirect_uri: str) -> WerkzeugResponse:
        """Send the visitor to GitHub's authorize page."""

    def authorize_access_token(self) -> Mapping[str, object] | None:
        """Exchange the callback's code for an access token."""

    def get(self, url: str, *, token: Mapping[str, object]) -> OAuthResponse:
        """Call a GitHub API path with ``token``."""


@dataclass(frozen=True)
class ServerContext:
    """The per-application objects the routes need."""

    config: ServerConfig
    users: UserCollection | None
    github: OAuthClient


def _context() -> ServerContext:
    """Return the current application's :class:`ServerContext`."""
    context: ServerContext = current_app.extensions["starguard"]
    return context


def render_result(message: str, status: int = 200) -> Response:
    """Render the result page with a single user-facing message.

    The HTTP status already says whether this went well, so the template
    takes its tone from it rather than from a second argument every call
    site would have to keep in step. A screen reader should interrupt for a
    failure and wait its turn for a success, which is the difference between
    an assertive alert and a polite status region.
    """
    return current_app.response_class(
        render_template("result.html", message=message, is_error=status >= 400),
        status=status,
        mimetype="text/html",
    )


def login() -> WerkzeugResponse:
    """Begin the OAuth flow for the Discord user named in the link token."""
    context = _context()
    try:
        discord_id, discord_username = read_link_token(
            context.config.secret_key,
            request.args.get("token"),
            max_age=context.config.link_token_max_age,
        )
    except LinkTokenError as exc:
        log.info("Rejected verification link: %s", exc)
        return render_result(str(exc), 400)

    session["discord_id"] = discord_id
    session["discord_username"] = discord_username

    redirect_uri = url_for("authorize", _external=True)
    return context.github.authorize_redirect(redirect_uri)


def authorize() -> Response:
    """Complete the OAuth flow and record the user's star status."""
    context = _context()
    discord_id = session.pop("discord_id", None)
    discord_username = session.pop("discord_username", None)

    if not discord_id:
        return render_result(messages.SESSION_EXPIRED, 400)

    if context.users is None:
        log.error("Cannot record a link: no database connection.")
        return render_result(messages.DATABASE_UNAVAILABLE, 503)

    try:
        token = context.github.authorize_access_token()
    except OAuthError as exc:
        log.info("OAuth error for Discord ID %s: %s", discord_id, exc.description)
        return render_result(messages.SIGN_IN_FAILED_REASON.format(reason=exc.description), 400)

    if not token:
        return render_result(messages.SIGN_IN_FAILED, 400)

    try:
        profile: GitHubProfile = context.github.get("user", token=token).json()
        github_username = profile["login"]
        github_id = profile["id"]

        # 204 means the authenticated user has starred the repository.
        starred_response = context.github.get(
            f"user/starred/{context.config.owner}/{context.config.repo}",
            token=token,
        )
        starred = starred_response.status_code == STARRED_STATUS
    except (OAuthError, KeyError, ValueError) as exc:
        log.warning("Could not read the GitHub profile: %s", exc)
        return render_result(messages.PROFILE_UNREADABLE, 502)

    log.info(
        "Linking Discord ID %s to GitHub user %s (starred=%s)",
        discord_id,
        github_username,
        starred,
    )

    try:
        link_account(
            context.users,
            discord_id=discord_id,
            discord_username=discord_username,
            github_id=github_id,
            github_username=github_username,
            linked_repo=context.config.repo_url,
            starred_repo=starred,
        )
    except AccountAlreadyLinkedError:
        log.info(
            "Refused to link GitHub user %s to Discord ID %s: already linked.",
            github_username,
            discord_id,
        )
        return render_result(messages.ALREADY_LINKED.format(github_username=github_username), 409)
    except PyMongoError as exc:
        log.error("Could not save the link: %s", exc)
        return render_result(messages.SAVE_FAILED, 503)

    if starred:
        return render_result(messages.VERIFIED_AND_STARRED)
    return render_result(
        messages.VERIFIED_NOT_STARRED.format(owner=context.config.owner, repo=context.config.repo)
    )


def healthz() -> tuple[dict[str, str], int]:
    """Liveness probe that also reports database reachability."""
    if _context().users is None:
        return {"status": "degraded", "database": "unavailable"}, 503
    return {"status": "ok"}, 200


def home() -> str:
    """Deliberately uninformative landing page."""
    return render_template("home.html")


def connect_users(config: ServerConfig) -> UserCollection | None:
    """Return the users collection, or None when MongoDB is unreachable."""
    try:
        _, users = connect(config.mongo_host, config.mongo_database, MongoClient)
        return users
    except PyMongoError as exc:
        log.error("Error connecting to MongoDB: %s", exc)
        return None


def create_app(
    config: ServerConfig | None = None,
    users: UserCollection | Literal[_NotSupplied.TOKEN] | None = _NOT_SUPPLIED,
) -> Flask:
    """Build the Flask application.

    ``config`` defaults to reading the environment, and ``users`` defaults to
    connecting to MongoDB. Passing either one skips that work, which is how
    the tests build a real application without a real database.
    """
    config = load_server_config() if config is None else config
    if users is _NOT_SUPPLIED:
        users = connect_users(config)

    app = Flask(__name__, template_folder="./html")
    # The server sits behind a reverse proxy, which terminates TLS. Without
    # this the OAuth redirect_uri would be built as http:// and GitHub would
    # reject it. The hop count is configurable because ProxyFix counts from
    # the right, so a second proxy in front silently shifts the client
    # address the rate limiter sees.
    hops = config.trusted_proxy_count
    # Replacing wsgi_app is Flask's documented way to wrap the application in
    # WSGI middleware; mypy only objects because the attribute is declared as
    # a method on the class.
    app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
        app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops
    )
    app.secret_key = config.secret_key
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=True,
    )

    oauth = OAuth(app)
    github = oauth.register(
        name="github",
        client_id=config.client_id,
        client_secret=config.client_secret,
        authorize_url="https://github.com/login/oauth/authorize",
        # B106: the name ends in "token" so bandit reads it as a
        # hardcoded credential, but this is GitHub's public endpoint URL.
        # The real secret is config.client_secret, read from the environment.
        access_token_url="https://github.com/login/oauth/access_token",  # nosec B106
        api_base_url="https://api.github.com/",
        client_kwargs={"scope": GITHUB_OAUTH_SCOPE},
    )

    app.extensions["starguard"] = ServerContext(config=config, users=users, github=github)

    install_security(
        app,
        limiter=RateLimiter(config.rate_limit, config.rate_limit_window),
        rate_limited_endpoints=RATE_LIMITED_ENDPOINTS,
        render_error=render_result,
    )

    app.add_url_rule("/", view_func=home)
    app.add_url_rule("/login", view_func=login)
    app.add_url_rule("/authorize", view_func=authorize)
    app.add_url_rule("/healthz", view_func=healthz)

    return app


def main() -> None:
    """Load the environment and serve the application."""
    load_dotenv()
    configure_logging()

    try:
        config = load_server_config()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        sys.exit(1)

    app = create_app(config)

    # waitress is a production WSGI server. Flask's built-in app.run() is a
    # development server and explicitly not meant to face the internet.
    log.info("Starguard OAuth server listening on port %s", config.port)
    # B104: binding to every interface is the point. This process runs
    # in a container and is reached from another one, so loopback would make
    # it unreachable; what is and is not published is the compose file's job.
    # Bandit prints "nosec encountered (B104), but no failed test" here. That
    # warning is wrong: delete the suppression and B104 fires on this line. A
    # bare "# nosec" silences the warning but would also hide any future
    # finding on this line, so the scoped form stays.
    serve(app, host="0.0.0.0", port=config.port, ident="Starguard")  # nosec B104


if __name__ == "__main__":
    main()
