"""The /authorize callback, branch by branch.

This is where a GitHub identity becomes a row in the database, so every way
it can go wrong is exercised here: no session, a database that is down, a
refused sign-in, an empty token, a profile that cannot be read, a GitHub
account that already belongs to someone else, and a write that fails. Each
case is checked for its status code and for the page the visitor is shown.

The OAuth client is a fake and the collection is the in-memory stand-in from
test_storage, so nothing here reaches GitHub or MongoDB.
"""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

from collections import namedtuple

import pytest
from authlib.integrations.flask_client import OAuthError
from pymongo.errors import PyMongoError

from server import messages
from server.server import ServerContext, connect_users, create_app, main
from server.webhooks import MAX_REQUEST_BODY_BYTES
from tests.test_server_routes import ENVIRONMENT, make_config
from tests.test_storage import FakeCollection

ACCESS_TOKEN = {"access_token": "gho_never_stored", "token_type": "bearer"}
DISCORD_ID = "123456789"
DISCORD_USERNAME = "someone"
PROFILE = {"login": "Octocat", "id": 583231}
STARRED_PATH = "user/starred/owner/repo"

STARRED = 204
NOT_STARRED = 404

# Distinguishes "the test did not ask for a collection" from "the database is
# down", which are different states that both look like None.
_DEFAULT = object()


class FakeApiResponse:
    """A GitHub API response as authlib hands it back."""

    def __init__(self, status_code=200, payload=None, error=None):
        self.status_code = status_code
        self._payload = payload
        self._error = error

    def json(self):
        if self._error is not None:
            raise self._error
        return self._payload


class FakeGitHub:
    """The registered OAuth client, without the network.

    ``responses`` maps an API path to the response to return, or to an
    exception to raise instead.
    """

    def __init__(self, token=_DEFAULT, responses=None, token_error=None):
        self.token = ACCESS_TOKEN if token is _DEFAULT else token
        self.token_error = token_error
        self.responses = responses or {
            "user": FakeApiResponse(payload=dict(PROFILE)),
            STARRED_PATH: FakeApiResponse(status_code=STARRED),
        }
        self.requests = []

    def authorize_access_token(self):
        if self.token_error is not None:
            raise self.token_error
        return self.token

    def get(self, path, token=None):
        self.requests.append((path, token))
        result = self.responses[path]
        if isinstance(result, Exception):
            raise result
        return result


class FakeDatabase:
    """The one call the health probe makes, and whether it answers.

    /healthz pings rather than trusting the handle, because pymongo connects
    lazily: a collection object exists whether or not MongoDB is reachable.
    """

    def __init__(self, error=None):
        self.error = error
        self.commands = []

    def command(self, name):
        self.commands.append(name)
        if self.error is not None:
            raise self.error
        return {"ok": 1.0}


class ProbeableCollection(FakeCollection):
    """A collection whose database answers the probe, or refuses to."""

    def __init__(self, documents=None, error=None):
        super().__init__(documents)
        self.database = FakeDatabase(error)


class ExplodingCollection(FakeCollection):
    """A collection whose writes fail the way an unreachable replica does."""

    def update_one(self, query, update, upsert=False):
        raise PyMongoError("no primary available")


Flow = namedtuple("Flow", "client github users")


def build(github=None, users=_DEFAULT, **overrides):
    """A test client whose OAuth client and collection are fakes."""
    config = make_config(**overrides)
    users = FakeCollection() if users is _DEFAULT else users
    github = FakeGitHub() if github is None else github

    app = create_app(config, users=users)
    app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    # The routes read everything they need from here, so swapping the context
    # is all it takes to hand them a GitHub client that never leaves memory.
    app.extensions["starguard"] = ServerContext(config=config, users=users, github=github)
    return Flow(app.test_client(), github, users)


def start_session(flow, discord_id=DISCORD_ID, username=DISCORD_USERNAME):
    """Put the state /login would have left behind into the session."""
    with flow.client.session_transaction() as session:
        session["discord_id"] = discord_id
        session["discord_username"] = username


def authorize(flow, **session):
    """Complete the callback the way GitHub would redirect a browser to it."""
    start_session(flow, **session)
    return flow.client.get("/authorize?code=abc&state=xyz")


def stored(flow):
    """The single document the callback wrote."""
    assert len(flow.users.documents) == 1
    return flow.users.documents[0]


def test_a_star_is_recorded_and_the_visitor_is_sent_back_to_discord():
    flow = build()
    response = authorize(flow)

    assert response.status_code == 200
    assert messages.VERIFIED_AND_STARRED.encode() in response.data

    document = stored(flow)
    assert document["discord_id"] == DISCORD_ID
    assert document["discord_username"] == DISCORD_USERNAME
    assert document["github_id"] == PROFILE["id"]
    assert document["github_username"] == "Octocat"
    assert document["github_username_lower"] == "octocat"
    assert document["starred_repo"] is True
    assert document["linked_repo"] == "https://github.com/owner/repo/"


def test_the_star_is_checked_against_the_configured_repository():
    flow = build(owner="fuegovic", repo="Starguard")
    flow.github.responses["user/starred/fuegovic/Starguard"] = FakeApiResponse(status_code=STARRED)
    assert authorize(flow).status_code == 200
    assert flow.github.requests[1][0] == "user/starred/fuegovic/Starguard"


def test_the_access_token_is_never_written_to_the_database():
    # Older versions stored the OAuth token in clear text next to a `repo`
    # scope, which made the collection a set of credentials.
    flow = build()
    authorize(flow)

    document = stored(flow)
    assert "github_token" not in document
    assert ACCESS_TOKEN["access_token"] not in repr(document)
    # It is still what the API calls were made with.
    assert [token for _, token in flow.github.requests] == [
        ACCESS_TOKEN,
        ACCESS_TOKEN,
    ]


def test_a_visitor_who_has_not_starred_is_told_which_repository_to_star():
    flow = build()
    flow.github.responses[STARRED_PATH] = FakeApiResponse(status_code=NOT_STARRED)

    response = authorize(flow)
    assert response.status_code == 200
    assert b"you have not starred owner/repo yet" in response.data
    # The link is still recorded, so the claim button knows who they are.
    assert stored(flow)["starred_repo"] is False


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 301])
def test_a_star_check_github_could_not_answer_is_not_an_absent_star(status):
    # Only 404 means unstarred. Reading a 401, a 429 or a 5xx as "no" wrote
    # starred_repo=False over a link that was true and sent the visitor off
    # to star a repository they had already starred.
    flow = build()
    flow.github.responses[STARRED_PATH] = FakeApiResponse(status_code=status)

    response = authorize(flow)
    assert response.status_code == 502
    assert messages.PROFILE_UNREADABLE.encode() in response.data
    # Nothing was recorded, so a link that already said starred survives.
    assert flow.users.documents == []


def test_the_page_for_an_unstarred_visitor_asks_them_to_sign_in_again():
    # The claim button answers from the star state recorded here, and
    # nothing turns a recorded false back into true on its own: the
    # periodic check only ever records an un-star, and the webhook that
    # would record the star is optional. "Star it, then claim" was advice
    # that failed for precisely the person who followed it.
    flow = build()
    flow.github.responses[STARRED_PATH] = FakeApiResponse(status_code=NOT_STARRED)

    page = authorize(flow).data
    assert b"run /verify in Discord again" in page
    assert stored(flow)["starred_repo"] is False


def test_a_callback_without_a_session_never_reaches_github():
    flow = build()
    response = flow.client.get("/authorize?code=abc&state=xyz")

    assert response.status_code == 400
    assert b"Your verification session has expired" in response.data
    assert flow.github.requests == []
    assert flow.users.documents == []


def test_the_session_is_consumed_so_the_callback_cannot_be_replayed():
    # The Discord ID is popped, not read: a second visit to the same callback
    # URL must not link a second GitHub account to the same person.
    flow = build()
    assert authorize(flow).status_code == 200

    replay = flow.client.get("/authorize?code=abc&state=xyz")
    assert replay.status_code == 400
    assert b"expired" in replay.data
    assert len(flow.github.requests) == 2


def test_a_database_that_is_down_is_reported_before_github_is_called():
    flow = build(users=None)
    response = authorize(flow)

    assert response.status_code == 503
    assert messages.DATABASE_UNAVAILABLE.encode() in response.data
    assert flow.github.requests == []


def test_a_refused_sign_in_shows_the_reason_github_gave():
    flow = build(
        github=FakeGitHub(
            token_error=OAuthError(error="access_denied", description="The user denied the request")
        )
    )
    response = authorize(flow)

    assert response.status_code == 400
    assert b"GitHub sign-in failed: The user denied the request" in response.data
    assert flow.users.documents == []


@pytest.mark.parametrize("token", [None, {}, ""])
def test_an_empty_token_is_a_failed_sign_in(token):
    flow = build(github=FakeGitHub(token=token))
    response = authorize(flow)

    assert response.status_code == 400
    assert messages.SIGN_IN_FAILED.encode() in response.data
    assert flow.github.requests == []


@pytest.mark.parametrize(
    "responses",
    [
        # A profile with no login at all: a KeyError on profile["login"].
        {"user": FakeApiResponse(payload={"id": 1})},
        {"user": FakeApiResponse(payload={"login": "Octocat"})},
        # A body that is not JSON: requests raises ValueError from .json().
        {"user": FakeApiResponse(error=ValueError("no JSON object"))},
        # The token was accepted and then rejected by the API.
        {"user": OAuthError(error="bad_token", description="revoked")},
        # The profile read succeeds and the star check is the one that fails.
        {
            "user": FakeApiResponse(payload=dict(PROFILE)),
            STARRED_PATH: OAuthError(error="server_error", description="502"),
        },
    ],
)
def test_an_unreadable_profile_is_a_bad_gateway(responses):
    flow = build(github=FakeGitHub(responses=responses))
    response = authorize(flow)

    assert response.status_code == 502
    assert messages.PROFILE_UNREADABLE.encode() in response.data
    assert flow.users.documents == []


def test_a_github_account_cannot_be_claimed_by_a_second_discord_user():
    # One star, one role. Without this check the same GitHub account could be
    # walked through the flow once per Discord account.
    flow = build()
    authorize(flow, discord_id="111", username="first")

    response = authorize(flow, discord_id="222", username="second")
    assert response.status_code == 409
    assert b"The GitHub account Octocat is already linked" in response.data

    # The first person's row is untouched.
    assert stored(flow)["discord_id"] == "111"
    assert stored(flow)["discord_username"] == "first"


def test_relinking_the_same_discord_user_is_allowed():
    flow = build()
    authorize(flow)
    assert authorize(flow).status_code == 200
    assert stored(flow)["discord_id"] == DISCORD_ID


def test_a_failed_save_is_reported_rather_than_raised():
    flow = build(users=ExplodingCollection())
    response = authorize(flow)

    assert response.status_code == 503
    assert messages.SAVE_FAILED.encode() in response.data


@pytest.mark.parametrize(
    "status,role,label",
    [(200, b'role="status"', b"Success:"), (400, b'role="alert"', b"Problem:")],
)
def test_the_page_announces_failure_more_loudly_than_success(status, role, label):
    # A screen reader should interrupt for a failure and wait its turn for a
    # success. The tone is taken from the HTTP status, not from a second
    # argument every call site would have to keep in step.
    flow = build()
    if status == 200:
        response = authorize(flow)
    else:
        response = flow.client.get("/authorize?code=abc&state=xyz")

    assert response.status_code == status
    assert role in response.data
    assert label in response.data


def test_healthz_is_ok_once_the_database_is_reachable():
    users = ProbeableCollection()
    flow = build(users=users)
    response = flow.client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    # It really asked the server, rather than reporting on the handle.
    assert users.database.commands == ["ping"]


def test_healthz_reports_a_database_that_has_stopped_answering(caplog):
    # connect() logs a failed preparation and returns the handle anyway, and
    # pymongo connects lazily, so a non-None collection says nothing about
    # whether MongoDB is up. Without a real command this endpoint answered
    # 200 straight through an outage and the compose healthcheck kept the
    # container in service while every link attempt failed.
    users = ProbeableCollection(error=PyMongoError("no primary available"))
    flow = build(users=users)

    with caplog.at_level("ERROR", logger="starguard.server"):
        response = flow.client.get("/healthz")

    assert response.status_code == 503
    assert response.get_json() == {"status": "degraded", "database": "unavailable"}
    assert "no primary available" in caplog.text


def test_connect_users_returns_the_collection(monkeypatch):
    monkeypatch.setattr(
        "server.server.connect", lambda host, database, factory: ("client", "users")
    )
    assert connect_users(make_config()) == "users"


def test_an_unreachable_database_leaves_the_server_running(monkeypatch, caplog):
    # A degraded /healthz and a clear message beat a process that will not
    # start, because the OAuth flow is the only thing that needs the database.
    def refuse(host, database, factory):
        raise PyMongoError("no route to host")

    monkeypatch.setattr("server.server.connect", refuse)
    with caplog.at_level("ERROR", logger="starguard.server"):
        assert connect_users(make_config()) is None
    assert "no route to host" in caplog.text


def test_create_app_connects_to_mongodb_when_no_collection_is_supplied(monkeypatch):
    collection = object()
    monkeypatch.setattr("server.server.connect_users", lambda config: collection)
    app = create_app(make_config())
    assert app.extensions["starguard"].users is collection


def test_main_serves_the_application_with_waitress(monkeypatch):
    # Flask's own app.run() is a development server and explicitly not meant
    # to face the internet.
    served = {}
    for key, value in ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SERVER_BIND_PORT", "5055")
    monkeypatch.setattr("server.server.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("server.server.configure_logging", lambda *a, **k: "text")
    monkeypatch.setattr("server.server.create_app", lambda config: ("app", config))
    monkeypatch.setattr(
        "server.server.serve", lambda app, **kwargs: served.update(app=app, **kwargs)
    )

    main()

    assert served["app"][0] == "app"
    assert served["port"] == 5055
    assert served["host"] == "0.0.0.0"
    # The same bound as MAX_CONTENT_LENGTH, and it has to be given twice:
    # Flask checks once the request reaches the application, by which time
    # waitress has already spooled the body under its own one gibibyte
    # default. The webhook route is public and deliberately not rate
    # limited, and a body this bounds is the stated reason that is safe.
    assert served["max_request_body_size"] == MAX_REQUEST_BODY_BYTES
