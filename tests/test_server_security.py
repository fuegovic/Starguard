"""Response hardening and rate limiting on the OAuth server."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import pytest
from flask import abort

from common.logging_setup import REQUEST_ID
from server.security import CONTENT_SECURITY_POLICY, REQUEST_ID_HEADER
from tests.test_server_routes import make_client


@pytest.fixture(name="client")
def client_fixture():
    return make_client()


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("X-Frame-Options", "DENY"),
    ],
)
def test_security_headers_are_present(client, header, expected):
    assert client.get("/").headers[header] == expected


def test_permissions_policy_disables_every_feature(client):
    policy = client.get("/").headers["Permissions-Policy"]
    assert "camera=()" in policy
    assert "geolocation=()" in policy
    assert "microphone=()" in policy


def test_the_policy_allows_no_script_and_no_external_origin(client):
    policy = client.get("/").headers["Content-Security-Policy"]
    assert "unsafe-inline" not in policy
    assert "script-src" not in policy
    assert "https://" not in policy
    assert policy.startswith("default-src 'none'")


def test_error_pages_are_hardened_too(client):
    # A rejected link is still a page a browser renders.
    assert client.get("/login").headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY


def test_pages_are_not_cached(client):
    assert client.get("/").headers["Cache-Control"] == "no-store"


def test_every_response_carries_a_request_id(client):
    first = client.get("/").headers[REQUEST_ID_HEADER]
    second = client.get("/").headers[REQUEST_ID_HEADER]
    assert first and second and first != second


def test_a_sane_inbound_request_id_is_echoed(client):
    response = client.get("/", headers={REQUEST_ID_HEADER: "trace-abc_123.4"})
    assert response.headers[REQUEST_ID_HEADER] == "trace-abc_123.4"


@pytest.mark.parametrize("hostile", ["a b", "x; Set-Cookie: y", '"quoted"', "z" * 65])
def test_a_hostile_inbound_request_id_is_replaced(client, hostile):
    # The value is echoed into a header and into a log line, so anything that
    # could forge either one is discarded rather than sanitised. Werkzeug
    # refuses to build a request whose header holds a newline, so the worst
    # case is not reachable from a test client, only from a WSGI server that
    # is less careful.
    response = client.get("/", headers={REQUEST_ID_HEADER: hostile})
    assert response.headers[REQUEST_ID_HEADER] != hostile


def test_login_is_rate_limited_per_address():
    client = make_client(rate_limit=3, rate_limit_window=60)
    for _ in range(3):
        assert client.get("/login").status_code == 400

    refused = client.get("/login")
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1


def test_authorize_is_rate_limited_too():
    client = make_client(rate_limit=2, rate_limit_window=60)
    for _ in range(2):
        client.get("/authorize")
    assert client.get("/authorize").status_code == 429


def test_the_health_probe_is_never_rate_limited():
    client = make_client(rate_limit=1, rate_limit_window=60)
    for _ in range(5):
        assert client.get("/healthz").status_code == 503


def test_the_limit_is_per_address():
    client = make_client(rate_limit=1, rate_limit_window=60)
    assert client.get("/login", environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code == 400
    assert client.get("/login", environ_overrides={"REMOTE_ADDR": "10.0.0.2"}).status_code == 400
    assert client.get("/login", environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code == 429


def test_a_forwarded_header_cannot_choose_the_bucket():
    # With one trusted proxy, ProxyFix takes the rightmost X-Forwarded-For
    # entry. A client that appends its own values only shifts the ones it
    # cannot control, so it cannot escape its own bucket by inventing
    # addresses.
    client = make_client(rate_limit=1, rate_limit_window=60)
    first = client.get(
        "/login",
        headers={"X-Forwarded-For": "203.0.113.9"},
        environ_overrides={"REMOTE_ADDR": "10.0.0.1"},
    )
    second = client.get(
        "/login",
        headers={"X-Forwarded-For": "198.51.100.1, 203.0.113.9"},
        environ_overrides={"REMOTE_ADDR": "10.0.0.1"},
    )
    assert first.status_code == 400
    assert second.status_code == 429


def test_static_files_keep_the_caching_flask_chose_for_them(client):
    # Everything else is per-user and one page carries a token in its URL,
    # but the stylesheet is the same for everybody.
    response = client.get("/static/css/style.css")
    assert response.status_code == 200
    assert response.headers.get("Cache-Control") != "no-store"
    # It is still hardened.
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_a_request_that_never_began_is_still_torn_down_cleanly():
    # waitress reuses its worker threads, so the request ID context variable
    # has to be reset even when the request never reached a view.
    app = make_client().application
    with app.test_request_context("/"):
        pass
    assert REQUEST_ID.get() == ""


def test_a_response_made_before_the_request_id_exists_is_still_hardened():
    # A failure early enough to precede the request hook still produces a
    # page a browser renders, so it still gets the headers, and the ID that
    # was never assigned is left out rather than echoed as an empty one.
    app = make_client().application

    @app.url_value_preprocessor
    def fail_before_the_hooks(endpoint, values):
        abort(503)

    response = app.test_client().get("/")
    assert response.status_code == 503
    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["Cache-Control"] == "no-store"
    assert REQUEST_ID_HEADER not in response.headers
