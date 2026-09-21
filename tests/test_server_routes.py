"""Smoke tests for the OAuth server routes.

These build the real Flask application through its factory. The database is
never contacted: ``create_app`` is handed the users collection explicitly, so
nothing in the module reaches for MongoDB.
"""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import pytest

from common.config import ConfigError
from common.linktoken import issue_link_token
from server.config import ServerConfig, load_server_config
from server.server import create_app, main

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


def make_config(**overrides):
    settings = {
        "owner": "owner",
        "repo": "repo",
        "secret_key": SECRET,
        "client_id": "client-id",
        "client_secret": "client-secret",
        "mongo_host": "mongodb://127.0.0.1:27017/",
        "mongo_database": "starguard_test",
        "port": 5000,
        "link_token_max_age": 900,
        "trusted_proxy_count": 1,
        "rate_limit": 50,
        "rate_limit_window": 60,
    }
    settings.update(overrides)
    return ServerConfig(**settings)


def make_client(**overrides):
    app = create_app(make_config(**overrides), users=None)
    app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return app.test_client()


@pytest.fixture(name="client")
def client_fixture():
    return make_client()


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
    assert response.headers["Location"].startswith("https://github.com/login/oauth/authorize")


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


def test_healthz_reports_the_missing_database(client):
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.get_json()["database"] == "unavailable"


def test_importing_the_module_does_not_read_the_environment(monkeypatch):
    # The headline of the factory refactor: no configuration is read, no
    # process exits and no database is contacted until create_app is called.
    for key in ENVIRONMENT:
        monkeypatch.delenv(key, raising=False)
    import server.server as module  # pylint: disable=import-outside-toplevel

    assert callable(module.create_app)


def test_missing_secret_key_aborts_startup(monkeypatch):
    for key, value in ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SECRET_KEY", "SecretKey")
    # load_dotenv must not pull a developer's real .env into the test run.
    monkeypatch.setattr("server.server.load_dotenv", lambda *a, **k: False)

    with pytest.raises(SystemExit):
        main()


def test_load_server_config_reads_the_environment(monkeypatch):
    for key, value in ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TRUSTED_PROXY_COUNT", raising=False)

    config = load_server_config()
    assert config.owner == "owner"
    assert config.repo_url == "https://github.com/owner/repo/"
    assert config.port == 5000
    assert config.trusted_proxy_count == 1


def test_load_server_config_names_the_missing_variable(monkeypatch):
    for key, value in ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GITHUB_CLIENT_ID")

    with pytest.raises(ConfigError, match="GITHUB_CLIENT_ID"):
        load_server_config()
