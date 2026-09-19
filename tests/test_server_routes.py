"""Smoke tests for the OAuth server routes.

These import the real Flask app, so they also prove the module boots with a
valid configuration. The database is never contacted: pymongo creates
collection handles lazily, and no test reaches a query.
"""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument,import-outside-toplevel

import importlib

import pytest

from common.linktoken import issue_link_token

SECRET = "0123456789abcdef-a-real-looking-key"

ENVIRONMENT = {
    "REPO_OWNER": "owner",
    "GITHUB_REPO": "repo",
    "SECRET_KEY": SECRET,
    "GITHUB_CLIENT_ID": "client-id",
    "GITHUB_CLIENT_SECRET": "client-secret",
    "MONGO_HOST": "mongodb://127.0.0.1:27017/?serverSelectionTimeoutMS=1",
    "MONGO_DATABASE": "starguard_test",
}


@pytest.fixture(name="client")
def client_fixture(monkeypatch):
    for key, value in ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    # load_dotenv must not pull a developer's real .env into the test run.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)

    server = importlib.import_module("server.server")
    server = importlib.reload(server)
    server.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return server.app.test_client()


def test_home_page_reveals_nothing(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Nothing to see here" in response.data


def test_login_without_a_token_is_rejected(client):
    response = client.get("/login")
    assert response.status_code == 400
    assert b"Missing verification token" in response.data


def test_login_with_a_forged_token_is_rejected(client):
    forged = issue_link_token("some-other-secret-key", 123, "someone")
    response = client.get(f"/login?token={forged}")
    assert response.status_code == 400
    assert b"not valid" in response.data


def test_login_no_longer_accepts_a_bare_discord_id(client):
    # The old flow trusted ?id= and ?name= straight from the query string.
    response = client.get("/login?id=123456789&name=someone")
    assert response.status_code == 400


def test_login_with_a_valid_token_redirects_to_github(client):
    token = issue_link_token(SECRET, 123456789, "someone")
    response = client.get(f"/login?token={token}")
    assert response.status_code == 302
    assert response.headers["Location"].startswith(
        "https://github.com/login/oauth/authorize"
    )


def test_requested_oauth_scope_is_minimal(client):
    token = issue_link_token(SECRET, 123456789, "someone")
    location = client.get(f"/login?token={token}").headers["Location"]
    # `repo` would grant read/write to every private repository the user owns.
    assert "scope=read%3Auser" in location
    assert "repo" not in location.split("scope=")[1].split("&")[0]


def test_authorize_without_a_session_is_rejected(client):
    response = client.get("/authorize?code=abc&state=xyz")
    assert response.status_code == 400
    assert b"expired" in response.data


def test_missing_secret_key_aborts_startup(monkeypatch):
    for key, value in ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SECRET_KEY", "SecretKey")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)

    import server.server as server_module

    with pytest.raises(SystemExit):
        importlib.reload(server_module)
