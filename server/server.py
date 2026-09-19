"""GitHub OAuth callback server for Starguard.

Handles two routes: ``/login`` starts the OAuth flow for a Discord user who
presented a signed link token from the bot, and ``/authorize`` records whether
that user has starred the configured repository.

The GitHub access token is used for the duration of the request and then
discarded. It is never written to the database or to the logs.
"""

import logging
import os
import sys

from authlib.integrations.flask_client import OAuth, OAuthError
from dotenv import load_dotenv
from flask import Flask, render_template, request, session, url_for
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from waitress import serve
from werkzeug.middleware.proxy_fix import ProxyFix

from common.config import ConfigError, env_int, require_env, require_secret_key
from common.linktoken import LinkTokenError, read_link_token
from common.storage import AccountAlreadyLinkedError, connect, link_account

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("starguard.server")

# The scope needed to read a public profile and check whether the authenticated
# user starred a public repository. This used to request `repo`, which grants
# read and write access to every private repository the user owns: far beyond
# what a Discord role bot needs, and a serious thing to ask of a visitor.
GITHUB_OAUTH_SCOPE = "read:user"

# GitHub answers "is this repo starred by me" with 204 (yes) or 404 (no).
STARRED_STATUS = 204


def load_config():
    """Read and validate every setting the server needs."""
    return {
        "owner": require_env("REPO_OWNER"),
        "repo": require_env("GITHUB_REPO"),
        "secret_key": require_secret_key(),
        "client_id": require_env("GITHUB_CLIENT_ID"),
        "client_secret": require_env("GITHUB_CLIENT_SECRET"),
        "mongo_host": require_env("MONGO_HOST"),
        "mongo_database": require_env("MONGO_DATABASE"),
        # The port inside the container. docker-compose publishes it on the
        # host as ${SERVER_PORT}; the two are deliberately separate, because
        # binding to SERVER_PORT while the compose file mapped it to 5000 made
        # every value other than 5000 unreachable.
        "port": env_int("SERVER_BIND_PORT", 5000, minimum=1),
        "link_token_max_age": env_int("LINK_TOKEN_MAX_AGE", 900, minimum=60),
    }


try:
    CONFIG = load_config()
except ConfigError as config_error:
    log.error("Configuration error: %s", config_error)
    sys.exit(1)

REPO_URL = f"https://github.com/{CONFIG['owner']}/{CONFIG['repo']}/"

app = Flask(__name__, template_folder="./html")
# The server sits behind a reverse proxy, which terminates TLS. Without this
# the OAuth redirect_uri would be built as http:// and GitHub would reject it.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = CONFIG["secret_key"]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
)

oauth = OAuth(app)
github = oauth.register(
    name="github",
    client_id=CONFIG["client_id"],
    client_secret=CONFIG["client_secret"],
    authorize_url="https://github.com/login/oauth/authorize",
    access_token_url="https://github.com/login/oauth/access_token",
    api_base_url="https://api.github.com/",
    client_kwargs={"scope": GITHUB_OAUTH_SCOPE},
)

MONGO_CLIENT = None
USERS = None
try:
    MONGO_CLIENT, USERS = connect(
        CONFIG["mongo_host"], CONFIG["mongo_database"], MongoClient
    )
except PyMongoError as mongo_error:
    log.error("Error connecting to MongoDB: %s", mongo_error)


def render_result(message):
    """Render the result page with a single user-facing message."""
    return render_template("result.html", message=message)


@app.route("/login")
def login():
    """Begin the OAuth flow for the Discord user named in the link token."""
    try:
        discord_id, discord_username = read_link_token(
            app.secret_key,
            request.args.get("token"),
            max_age=CONFIG["link_token_max_age"],
        )
    except LinkTokenError as exc:
        log.info("Rejected verification link: %s", exc)
        return render_result(str(exc)), 400

    session["discord_id"] = discord_id
    session["discord_username"] = discord_username

    redirect_uri = url_for("authorize", _external=True)
    return github.authorize_redirect(redirect_uri)


@app.route("/authorize")
def authorize():
    """Complete the OAuth flow and record the user's star status."""
    discord_id = session.pop("discord_id", None)
    discord_username = session.pop("discord_username", None)

    if not discord_id:
        return render_result(
            "Your verification session has expired. Run /verify in Discord again."
        ), 400

    if USERS is None:
        log.error("Cannot record a link: no database connection.")
        return render_result(
            "The database is unavailable right now. Please try again later."
        ), 503

    try:
        token = github.authorize_access_token()
    except OAuthError as exc:
        log.info("OAuth error for Discord ID %s: %s", discord_id, exc.description)
        return render_result(f"GitHub sign-in failed: {exc.description}"), 400

    if not token:
        return render_result("GitHub sign-in failed. Please try again."), 400

    try:
        profile = github.get("user", token=token).json()
        github_username = profile["login"]
        github_id = profile["id"]

        # 204 means the authenticated user has starred the repository.
        starred_response = github.get(
            f"user/starred/{CONFIG['owner']}/{CONFIG['repo']}", token=token
        )
        starred = starred_response.status_code == STARRED_STATUS
    except (OAuthError, KeyError, ValueError) as exc:
        log.warning("Could not read the GitHub profile: %s", exc)
        return render_result(
            "Could not read your GitHub profile. Please try again."
        ), 502

    log.info(
        "Linking Discord ID %s to GitHub user %s (starred=%s)",
        discord_id,
        github_username,
        starred,
    )

    try:
        link_account(
            USERS,
            discord_id=discord_id,
            discord_username=discord_username,
            github_id=github_id,
            github_username=github_username,
            linked_repo=REPO_URL,
            starred_repo=starred,
        )
    except AccountAlreadyLinkedError:
        log.info(
            "Refused to link GitHub user %s to Discord ID %s: already linked.",
            github_username,
            discord_id,
        )
        return render_result(
            f"The GitHub account {github_username} is already linked to another "
            "Discord user. Each GitHub account can only be used once."
        ), 409
    except PyMongoError as exc:
        log.error("Could not save the link: %s", exc)
        return render_result(
            "Could not save your verification right now. Please try again later."
        ), 503

    if starred:
        return render_result(
            "Authentication successful! Head back to Discord and claim your role."
        )
    return render_result(
        f"Authentication successful, but you have not starred {CONFIG['owner']}/"
        f"{CONFIG['repo']} yet. Star it, then claim your role in Discord."
    )


@app.route("/healthz")
def healthz():
    """Liveness probe that also reports database reachability."""
    if USERS is None:
        return {"status": "degraded", "database": "unavailable"}, 503
    return {"status": "ok"}, 200


@app.route("/")
def home():
    """Deliberately uninformative landing page."""
    return render_template("home.html")


if __name__ == "__main__":
    # waitress is a production WSGI server. Flask's built-in app.run() is a
    # development server and explicitly not meant to face the internet.
    log.info("Starguard OAuth server listening on port %s", CONFIG["port"])
    serve(app, host="0.0.0.0", port=CONFIG["port"], ident="Starguard")
