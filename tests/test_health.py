"""Tests for the bot's health endpoint."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import json
import urllib.error
import urllib.request
from http import HTTPStatus

import pytest

from bot.health import HealthState, serve_health, stale_after_seconds


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_a_bot_that_has_not_connected_is_not_healthy():
    payload, status = HealthState(FakeClock()).report()
    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert payload["status"] == "starting"
    assert payload["gateway"] == "connecting"


def test_a_connected_bot_with_a_recent_check_is_healthy():
    clock = FakeClock()
    state = HealthState(clock)
    state.mark_ready(stale_after_seconds(300))

    clock.advance(60)
    payload, status = state.report(last_check_completed=clock.now - 10)
    assert status == HTTPStatus.OK
    assert payload["status"] == "ok"
    assert payload["last_check_age_seconds"] == 10


def test_a_check_that_stopped_happening_is_reported():
    # The signal worth having: the process is alive and the gateway is up,
    # but the loop that does the actual work has stopped producing.
    clock = FakeClock()
    state = HealthState(clock)
    state.mark_ready(stale_after_seconds(300))

    clock.advance(10_000)
    payload, status = state.report(last_check_completed=1000.0)
    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert payload["status"] == "degraded"
    assert payload["star_check"] == "stale"


def test_a_check_that_has_not_run_yet_is_given_the_same_grace():
    clock = FakeClock()
    state = HealthState(clock)
    state.mark_ready(stale_after_seconds(300))

    clock.advance(60)
    assert state.report()[1] == HTTPStatus.OK

    clock.advance(10_000)
    assert state.report()[1] == HTTPStatus.SERVICE_UNAVAILABLE


def test_with_automatic_checks_off_only_the_gateway_is_reported():
    clock = FakeClock()
    state = HealthState(clock)
    state.mark_ready(None)

    clock.advance(100_000)
    payload, status = state.report()
    assert status == HTTPStatus.OK
    assert payload["star_check"] == "disabled"


@pytest.fixture(name="endpoint")
def endpoint_fixture():
    state = HealthState()
    server = serve_health(state, "127.0.0.1", 0, lambda: None)
    assert server is not None
    yield state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_the_endpoint_really_answers_over_http(endpoint):
    state, base = endpoint
    status, payload = fetch(f"{base}/healthz")
    assert status == 503
    assert payload["status"] == "starting"

    state.mark_ready(None)
    status, payload = fetch(f"{base}/healthz")
    assert status == 200
    assert payload["status"] == "ok"


def test_the_endpoint_serves_nothing_else(endpoint):
    _, base = endpoint
    assert fetch(f"{base}/secrets")[0] == 404


def test_a_port_that_cannot_be_bound_does_not_stop_the_bot(monkeypatch, caplog):
    # A missing health endpoint must not stop the bot from doing its actual
    # job, so the failure is logged and startup carries on.
    def refuse(address, handler):
        raise OSError(98, "Address already in use")

    monkeypatch.setattr("bot.health.ThreadingHTTPServer", refuse)
    with caplog.at_level("ERROR", logger="starguard.bot"):
        assert serve_health(HealthState(), "127.0.0.1", 8080, lambda: None) is None

    assert "8080" in caplog.text
    assert "Address already in use" in caplog.text


def test_the_endpoint_matches_the_path_and_not_the_query_string(endpoint):
    # A container healthcheck may add a cache buster, and a request for
    # another path must not become a health check by naming one.
    _, base = endpoint
    assert fetch(f"{base}/healthz?probe=1")[0] == 503
    assert fetch(f"{base}/secrets?path=/healthz")[0] == 404
