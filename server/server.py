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
from datetime import UTC, datetime
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
from server.webhooks import MAX_REQUEST_BODY_BYTES, install_webhook

log = logging.getLogger("starguard.server")

# The scope needed to read a public profile and check whether the authenticated
# user starred a public repository. This used to request `repo`, which grants
# read and write access to every private repository the user owns: far beyond
# what a Discord role bot needs, and a serious thing to ask of a visitor.
GITHUB_OAUTH_SCOPE: Final = "read:user"

# GitHub answers "is this repo starred by me" with 204 (yes) or 404 (no),
# and with nothing else; see _read_star_check.
STARRED_STATUS: Final = 204
NOT_STARRED_STATUS: Final = 404

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


def _read_star_check(status: int) -> bool:
    """Turn the starred endpoint's status into an answer, or refuse to guess.

    There are two answers and everything else is GitHub failing to give one.
    This used to read any status other than 204 as "not starred", so a 401,
    a 403, a 429 or a 5xx during an outage wrote ``starred_repo=False`` over
    a link that was true and sent the visitor off to star a repository they
    had already starred. ValueError so the caller reports it as the
    unreadable reply it is, rather than recording a fact nobody established.
    """
    if status == STARRED_STATUS:
        return True
    if status == NOT_STARRED_STATUS:
        return False
    raise ValueError(f"the starred check answered {status}")


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

        # Taken before the question is asked, not after it is answered.
        # The answer describes some instant inside the call, and an earlier
        # horizon is the direction that lets a star event landing during it
        # win: link_account refuses to write over anything newer than this,
        # and the webhook's out-of-band fact should beat a read that was
        # already in flight. The sweep takes its own instant the same way.
        star_checked_at = datetime.now(UTC)
        starred_response = context.github.get(
            f"user/starred/{context.config.owner}/{context.config.repo}",
            token=token,
        )
        starred = _read_star_check(starred_response.status_code)
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
        recorded = link_account(
            context.users,
            discord_id=discord_id,
            discord_username=discord_username,
            github_id=github_id,
            github_username=github_username,
            linked_repo=context.config.repo_url,
            starred_repo=starred,
            # Without this the horizon is link_account's own clock, which
            # is later than the answer it stands for by however long the
            # OAuth exchange took, and a webhook that landed inside that
            # window is written over. Only the caller knows when it asked.
            observed_at=star_checked_at,
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

    # The page follows the row rather than the answer GitHub gave, because
    # the two can differ: link_account declines to write a star state a
    # newer webhook event has already contradicted, and hands back what the
    # row holds instead. Saying "not starred" to somebody the database
    # records as starred would send them round the whole flow for nothing.
    if recorded["starred_repo"]:
        return render_result(messages.VERIFIED_AND_STARRED)
    return render_result(
        messages.VERIFIED_NOT_STARRED.format(owner=context.config.owner, repo=context.config.repo)
    )


def healthz() -> tuple[dict[str, str], int]:
    """Liveness probe that also reports database reachability."""
    users = _context().users
    if users is None:
        return {"status": "degraded", "database": "unavailable"}, 503

    try:
        # A handle is not a connection. pymongo connects lazily and connect()
        # logs a failed preparation rather than raising, so this object
        # exists whether or not MongoDB is reachable; only a command that
        # goes to the server tells the two apart. Without one, the compose
        # healthcheck reads 200 straight through an outage and keeps the
        # container in service while every link attempt fails.
        users.database.command("ping")
    except PyMongoError as exc:
        log.error("Health probe could not reach MongoDB: %s", exc)
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
    # The server usually sits behind a reverse proxy, which terminates TLS.
    # Without this the OAuth redirect_uri would be built as http:// and
    # GitHub would reject it. The hop count is configurable because ProxyFix
    # counts from the right, so a second proxy in front silently shifts the
    # client address the rate limiter sees.
    #
    # Zero hops means the port is reached directly, which docker-compose.yml
    # publishes it to be, and then the middleware is left off entirely: with
    # it installed, one hop of trust is all a client needs to hand itself
    # any X-Forwarded-For it likes and take a fresh rate-limit bucket per
    # request. A deployment with no proxy has no forwarded header worth
    # believing, so the safe reading is to believe none of it.
    hops = config.trusted_proxy_count
    if hops:
        # Replacing wsgi_app is Flask's documented way to wrap the
        # application in WSGI middleware; mypy only objects because the
        # attribute is declared as a method on the class.
        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops
        )
    app.secret_key = config.secret_key
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=True,
        # An oversized body is refused before it is read. Only the webhook
        # takes a body at all, but the bound is set here rather than beside
        # that route so it also holds for an installation that has no hook,
        # and for whatever route is added next.
        MAX_CONTENT_LENGTH=MAX_REQUEST_BODY_BYTES,
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

    # Registered only when a secret is configured, and absent otherwise. The
    # receiver is not in RATE_LIMITED_ENDPOINTS on purpose; see the module
    # docstring in server/webhooks.py for why.
    if config.webhook_secret is not None:
        install_webhook(
            app,
            secret=config.webhook_secret,
            owner=config.owner,
            repo=config.repo,
            users=users,
        )

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
    # Bandit prints "nosec encountered (B104), but no failed test" here.
    # That warning is wrong: delete the suppression and B104 fires on the
    # host line below. An unscoped suppression, one with no test id after
    # it, silences that warning but would also hide any future finding on
    # that line, so the scoped form stays. Spelling the unscoped form out
    # here is not an option either: bandit reads the token wherever it
    # appears in a comment, prose included, and parses the rest of the
    # line as test ids, so quoting it printed six warnings of its own.
    #
    # max_request_body_size is the same bound as MAX_CONTENT_LENGTH and has
    # to be stated twice, because the two enforce it in different places.
    # Flask's check runs once the request has reached the application, by
    # which time waitress has already spooled the body: its own default is
    # one gibibyte, so without this an unauthenticated client could make the
    # public webhook endpoint buffer a thousand times what that route is
    # documented to bound. The bounded body is the stated reason that route
    # is safe to leave unlimited, so it has to hold at the front door.
    serve(
        app,
        host="0.0.0.0",  # nosec B104
        port=config.port,
        ident="Starguard",
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
    )


if __name__ == "__main__":
    main()
