"""Tests for the bot's health endpoint."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import json
import urllib.error
import urllib.request
from http import HTTPStatus

import pytest

from bot.health import HealthState, LoopHealth, serve_health, stale_after_seconds


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Loop:
    """A watched loop whose last completed pass the test moves by hand."""

    def __init__(self, field, age_field, stale_after):
        self.field = field
        self.age_field = age_field
        self.stale_after = stale_after
        self.last_completed = None

    def watched_by(self, state):
        """Register with ``state`` and return self, for one-line setup."""
        state.watch(
            LoopHealth(
                field=self.field,
                age_field=self.age_field,
                stale_after=self.stale_after,
                last_completed=lambda: self.last_completed,
            )
        )
        return self


def star_check(state, stale_after=None):
    """Watch the periodic sweep, the way bot.create_client does."""
    return Loop("star_check", "last_check_age_seconds", stale_after).watched_by(state)


def role_sync(state, stale_after=None):
    """Watch the webhook drain, the way bot.create_client does."""
    return Loop("role_sync", "last_role_sync_age_seconds", stale_after).watched_by(state)


def test_a_bot_that_has_not_connected_is_not_healthy():
    payload, status = HealthState(FakeClock()).report()
    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert payload["status"] == "starting"
    assert payload["gateway"] == "connecting"


def test_a_connected_bot_with_a_recent_check_is_healthy():
    clock = FakeClock()
    state = HealthState(clock)
    loop = star_check(state, stale_after_seconds(300))
    state.mark_ready()

    clock.advance(60)
    loop.last_completed = clock.now - 10
    payload, status = state.report()
    assert status == HTTPStatus.OK
    assert payload["status"] == "ok"
    assert payload["star_check"] == "ok"
    assert payload["last_check_age_seconds"] == 10


def test_the_endpoint_reads_the_loop_rather_than_a_copy_taken_at_startup():
    # The loop is registered when the client is built and finishes passes
    # for as long as the bot runs, so a value read once at registration
    # would report the first pass forever.
    clock = FakeClock()
    state = HealthState(clock)
    loop = star_check(state, stale_after_seconds(300))
    state.mark_ready()

    clock.advance(10_000)
    assert state.report()[1] == HTTPStatus.SERVICE_UNAVAILABLE

    loop.last_completed = clock.now
    assert state.report()[1] == HTTPStatus.OK


def test_a_check_that_stopped_happening_is_reported():
    # The signal worth having: the process is alive and the gateway is up,
    # but the loop that does the actual work has stopped producing.
    clock = FakeClock()
    state = HealthState(clock)
    loop = star_check(state, stale_after_seconds(300))
    state.mark_ready()

    loop.last_completed = 1000.0
    clock.advance(10_000)
    payload, status = state.report()
    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert payload["status"] == "degraded"
    assert payload["star_check"] == "stale"


def test_a_drain_that_stopped_happening_is_reported_too():
    # The finding this second axis exists for. With AUTOMATIC_CHECK=false
    # and ROLE_SYNC_ENABLED=true the drain is the only loop reconciling
    # anything, and a bot whose drain has never once reached the database
    # answered 200 for as long as it ran.
    clock = FakeClock()
    state = HealthState(clock)
    star_check(state)
    role_sync(state, stale_after_seconds(30))
    state.mark_ready()

    clock.advance(60)
    payload, status = state.report()
    assert status == HTTPStatus.OK
    assert payload["role_sync"] == "pending"

    clock.advance(10_000)
    payload, status = state.report()
    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert payload["status"] == "degraded"
    assert payload["role_sync"] == "stale"
    # The loop that is off says so rather than going quiet, so the payload
    # names which of the two stopped and which was never running.
    assert payload["star_check"] == "disabled"


def test_a_healthy_drain_is_reported_with_its_own_age():
    clock = FakeClock()
    state = HealthState(clock)
    loop = role_sync(state, stale_after_seconds(30))
    state.mark_ready()

    clock.advance(60)
    loop.last_completed = clock.now - 5
    payload, status = state.report()
    assert status == HTTPStatus.OK
    assert payload["role_sync"] == "ok"
    assert payload["last_role_sync_age_seconds"] == 5


def test_one_healthy_loop_does_not_cover_for_a_stale_one():
    clock = FakeClock()
    state = HealthState(clock)
    sweep = star_check(state, stale_after_seconds(300))
    role_sync(state, stale_after_seconds(30))
    state.mark_ready()

    clock.advance(1_000)
    sweep.last_completed = clock.now
    payload, status = state.report()
    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert payload["star_check"] == "ok"
    assert payload["role_sync"] == "stale"


def test_a_check_that_has_not_run_yet_is_given_the_same_grace():
    clock = FakeClock()
    state = HealthState(clock)
    star_check(state, stale_after_seconds(300))
    state.mark_ready()

    clock.advance(60)
    assert state.report()[1] == HTTPStatus.OK

    clock.advance(10_000)
    assert state.report()[1] == HTTPStatus.SERVICE_UNAVAILABLE


def test_with_both_loops_off_only_the_gateway_is_reported():
    clock = FakeClock()
    state = HealthState(clock)
    star_check(state)
    role_sync(state)
    state.mark_ready()

    clock.advance(100_000)
    payload, status = state.report()
    assert status == HTTPStatus.OK
    assert payload["star_check"] == "disabled"
    assert payload["role_sync"] == "disabled"


@pytest.fixture(name="endpoint")
def endpoint_fixture():
    state = HealthState()
    server = serve_health(state, "127.0.0.1", 0)
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

    state.mark_ready()
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
        assert serve_health(HealthState(), "127.0.0.1", 8080) is None

    assert "8080" in caplog.text
    assert "Address already in use" in caplog.text


def test_a_port_above_the_ceiling_does_not_kill_the_bot(caplog):
    # Nothing validates BOT_HEALTH_PORT against 65535, and bind answers a
    # port above it with OverflowError, which is not an OSError. Catching
    # only OSError meant one mistyped digit took the whole bot down before
    # it ever reached the gateway, over an endpoint that is optional.
    #
    # The real server is used rather than a stand-in, because the point of
    # this test is which exception the standard library actually raises.
    with caplog.at_level("ERROR", logger="starguard.bot"):
        assert serve_health(HealthState(), "127.0.0.1", 70000) is None

    assert "70000" in caplog.text
    assert "0-65535" in caplog.text


def test_the_endpoint_matches_the_path_and_not_the_query_string(endpoint):
    # A container healthcheck may add a cache buster, and a request for
    # another path must not become a health check by naming one.
    _, base = endpoint
    assert fetch(f"{base}/healthz?probe=1")[0] == 503
    assert fetch(f"{base}/secrets?path=/healthz")[0] == 404
