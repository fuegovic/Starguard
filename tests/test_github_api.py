"""Tests for the GitHub stargazer listing."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
import requests

from common.github_api import (
    MAX_ATTEMPTS_PER_PAGE,
    GitHubError,
    StargazerCache,
    fetch_stargazer_listing,
    fetch_stargazer_logins,
)


class FakeResponse:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code=200, payload=None, links=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.links = links or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    """Returns queued responses and records the requests it received."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        if not self._responses:
            raise AssertionError(f"unexpected extra request to {url}")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def user_page(*logins):
    return [{"login": login, "id": i} for i, login in enumerate(logins, start=1)]


def no_sleep(_seconds):
    """Stand in for time.sleep so retry tests do not actually wait."""


def fetch(session, **kwargs):
    """Call the listing with retries that do not sleep."""
    kwargs.setdefault("sleep", no_sleep)
    return fetch_stargazer_logins("o", "r", session=session, **kwargs)


def test_single_page_returns_lowercased_logins():
    session = FakeSession([FakeResponse(payload=user_page("Alice", "BOB"))])
    assert fetch_stargazer_logins("o", "r", session=session) == {"alice", "bob"}


def test_follows_the_link_header_across_pages():
    session = FakeSession(
        [
            FakeResponse(
                payload=user_page("one"),
                links={"next": {"url": "https://api.github.com/page2"}},
            ),
            FakeResponse(
                payload=user_page("two"),
                links={"next": {"url": "https://api.github.com/page3"}},
            ),
            FakeResponse(payload=user_page("three")),
        ]
    )

    assert fetch_stargazer_logins("o", "r", session=session) == {"one", "two", "three"}
    assert len(session.calls) == 3
    # The first request asks for a full page; later ones reuse GitHub's own URL
    # verbatim, which already carries the paging parameters.
    assert session.calls[0]["params"] == {"per_page": 100}
    assert session.calls[1]["url"] == "https://api.github.com/page2"
    assert session.calls[1]["params"] is None


def test_accepts_the_star_json_envelope():
    session = FakeSession(
        [FakeResponse(payload=[{"starred_at": "2024-01-01", "user": {"login": "Zed"}}])]
    )
    assert fetch_stargazer_logins("o", "r", session=session) == {"zed"}


def test_token_is_sent_as_a_bearer_header():
    session = FakeSession([FakeResponse(payload=user_page("a"))])
    fetch_stargazer_logins("o", "r", token="secret", session=session)
    assert session.calls[0]["headers"]["Authorization"] == "Bearer secret"


def test_no_authorization_header_without_a_token():
    session = FakeSession([FakeResponse(payload=user_page("a"))])
    fetch_stargazer_logins("o", "r", session=session)
    assert "Authorization" not in session.calls[0]["headers"]


def test_rate_limit_raises_with_a_useful_message():
    session = FakeSession([FakeResponse(status_code=403, headers={"X-RateLimit-Remaining": "0"})])
    with pytest.raises(GitHubError, match="rate limit"):
        fetch_stargazer_logins("o", "r", session=session)


@pytest.mark.parametrize(
    "status,expected",
    [(401, "GITHUB_TOKEN"), (404, "not found")],
)
def test_permanent_error_statuses_raise_without_retrying(status, expected):
    session = FakeSession([FakeResponse(status_code=status)])
    with pytest.raises(GitHubError, match=expected):
        fetch(session)
    assert len(session.calls) == 1


def test_a_server_error_is_retried_and_then_reported():
    session = FakeSession([FakeResponse(status_code=500)] * MAX_ATTEMPTS_PER_PAGE)
    with pytest.raises(GitHubError, match="HTTP 500"):
        fetch(session)
    assert len(session.calls) == MAX_ATTEMPTS_PER_PAGE


def test_a_transient_failure_is_retried_and_succeeds():
    session = FakeSession(
        [
            FakeResponse(status_code=503),
            requests.ConnectionError("dropped"),
            FakeResponse(payload=user_page("alice")),
        ]
    )
    assert fetch(session) == {"alice"}
    assert len(session.calls) == 3


def test_retry_after_is_honoured():
    slept = []
    session = FakeSession(
        [
            FakeResponse(status_code=429, headers={"Retry-After": "7"}),
            FakeResponse(payload=user_page("alice")),
        ]
    )
    assert fetch(session, sleep=slept.append) == {"alice"}
    assert slept == [7.0]


def test_a_retry_after_longer_than_the_cap_is_reported_not_slept_on():
    # Holding a worker thread for an hour is worse than telling the operator
    # to raise the check interval.
    session = FakeSession([FakeResponse(status_code=429, headers={"Retry-After": "3600"})])
    with pytest.raises(GitHubError, match="3600s wait"):
        fetch(session)
    assert len(session.calls) == 1


def test_the_primary_rate_limit_is_not_retried():
    # A 403 with nothing left does not clear for up to an hour, so retrying
    # it only wastes the budget that is already gone.
    session = FakeSession([FakeResponse(status_code=403, headers={"X-RateLimit-Remaining": "0"})])
    with pytest.raises(GitHubError, match="rate limit"):
        fetch(session)
    assert len(session.calls) == 1


def test_partial_results_are_never_returned():
    # The second page fails. Returning page one alone would look like everyone
    # on later pages had un-starred, and strip their roles.
    session = FakeSession(
        [
            FakeResponse(
                payload=user_page("kept"),
                links={"next": {"url": "https://api.github.com/page2"}},
            ),
        ]
        + [FakeResponse(status_code=500)] * MAX_ATTEMPTS_PER_PAGE
    )
    with pytest.raises(GitHubError):
        fetch(session)


def test_network_failure_raises_githuberror():
    session = FakeSession([requests.ConnectionError("boom")] * MAX_ATTEMPTS_PER_PAGE)
    with pytest.raises(GitHubError, match="Could not reach"):
        fetch(session)


def test_pagination_loop_is_bounded(monkeypatch):
    monkeypatch.setattr("common.github_api.MAX_PAGES", 3)

    class LoopingSession:
        """Always reports another page, to prove the loop is bounded."""

        def get(self, url, headers=None, params=None, timeout=None):
            return FakeResponse(
                payload=user_page("a"),
                links={"next": {"url": "https://api.github.com/same"}},
            )

    with pytest.raises(GitHubError, match="pagination loop"):
        fetch_stargazer_logins("o", "r", session=LoopingSession())


def test_unexpected_payload_shape_raises():
    session = FakeSession([FakeResponse(payload={"message": "nope"})])
    with pytest.raises(GitHubError, match="Unexpected response shape"):
        fetch_stargazer_logins("o", "r", session=session)


def page(*logins, etag=None, next_url=None):
    """A 200 page, optionally tagged and linked to a next page."""
    return FakeResponse(
        payload=user_page(*logins),
        headers={"ETag": etag} if etag else {},
        links={"next": {"url": next_url}} if next_url else None,
    )


def test_the_second_cycle_sends_the_stored_etag():
    cache = StargazerCache()
    first = FakeSession([page("alice", etag='W/"one"')])
    assert fetch(first, cache=cache) == {"alice"}
    assert "If-None-Match" not in first.calls[0]["headers"]

    second = FakeSession([FakeResponse(status_code=304)])
    assert fetch(second, cache=cache) == {"alice"}
    assert second.calls[0]["headers"]["If-None-Match"] == 'W/"one"'


def test_an_unchanged_page_costs_nothing_and_is_still_counted():
    cache = StargazerCache()
    fetch(FakeSession([page("alice", etag='W/"one"')]), cache=cache)

    listing = fetch_stargazer_listing(
        "o",
        "r",
        session=FakeSession(
            [FakeResponse(status_code=304, headers={"X-RateLimit-Remaining": "4999"})]
        ),
        cache=cache,
        sleep=no_sleep,
    )
    assert listing.logins == {"alice"}
    assert listing.pages_unchanged == 1
    assert listing.pages_fetched == 0
    assert listing.rate_limit_remaining == 4999


def test_a_304_on_one_page_does_not_freeze_the_others():
    # The case the cache exists for and the one it could get wrong: an
    # un-star shifts later entries up, so page one is unchanged while page
    # two really has lost somebody.
    cache = StargazerCache()
    first = FakeSession(
        [
            page("a", "b", etag='W/"p1"', next_url="https://api.github.com/page2"),
            page("c", "d", etag='W/"p2"'),
        ]
    )
    assert fetch(first, cache=cache) == {"a", "b", "c", "d"}

    second = FakeSession(
        [
            FakeResponse(status_code=304),
            page("c", etag='W/"p2-new"'),
        ]
    )
    assert fetch(second, cache=cache) == {"a", "b", "c"}
    assert second.calls[1]["url"] == "https://api.github.com/page2"


def test_a_shorter_listing_drops_the_pages_that_went_away():
    cache = StargazerCache()
    fetch(
        FakeSession(
            [
                page("a", etag='W/"p1"', next_url="https://api.github.com/page2"),
                page("b", etag='W/"p2"'),
            ]
        ),
        cache=cache,
    )
    assert len(cache.pages) == 2

    # Now everyone on page two is gone, so page one no longer links onward.
    assert fetch(FakeSession([page("a", etag='W/"p1-new"')]), cache=cache) == {"a"}
    assert len(cache.pages) == 1


def test_an_untagged_page_is_simply_not_cached():
    cache = StargazerCache()
    assert fetch(FakeSession([page("alice")]), cache=cache) == {"alice"}
    assert not cache.pages


def test_the_listing_reports_what_it_cost():
    listing = fetch_stargazer_listing(
        "o",
        "r",
        session=FakeSession(
            [
                FakeResponse(status_code=503),
                FakeResponse(
                    payload=user_page("alice"),
                    headers={"X-RateLimit-Remaining": "4321"},
                ),
            ]
        ),
        sleep=no_sleep,
    )
    assert listing.api_calls == 2
    assert listing.pages_fetched == 1
    assert listing.rate_limit_remaining == 4321


def test_entries_that_are_not_user_objects_are_ignored():
    # A page with something unexpected in it should cost the caller the
    # entries it cannot read, never the whole cycle.
    session = FakeSession([FakeResponse(payload=["nonsense", None, 17, {}, {"login": "Alice"}])])
    assert fetch_stargazer_logins("o", "r", session=session) == {"alice"}


def http_date(seconds_from_now, with_timezone=True):
    """An HTTP date the way RFC 9110 allows Retry-After to be spelled."""
    when = datetime.now(UTC) + timedelta(seconds=seconds_from_now)
    formatted = format_datetime(when)
    return formatted if with_timezone else formatted.rsplit(" ", 1)[0]


@pytest.mark.parametrize("with_timezone", [True, False])
def test_a_retry_after_date_is_honoured_like_a_count_of_seconds(with_timezone):
    # GitHub sends either spelling and both are allowed, so a date must not
    # fall through to the blind backoff.
    slept = []
    session = FakeSession(
        [
            FakeResponse(
                status_code=429,
                headers={"Retry-After": http_date(30, with_timezone)},
            ),
            FakeResponse(payload=user_page("alice")),
        ]
    )

    assert fetch(session, sleep=slept.append) == {"alice"}
    assert len(slept) == 1
    assert 25 <= slept[0] <= 31


def test_a_retry_after_date_that_has_already_passed_is_not_a_negative_wait():
    slept = []
    session = FakeSession(
        [
            FakeResponse(status_code=503, headers={"Retry-After": http_date(-120)}),
            FakeResponse(payload=user_page("alice")),
        ]
    )

    assert fetch(session, sleep=slept.append) == {"alice"}
    assert slept == [0.0]


@pytest.mark.parametrize("raw", ["", "   ", "whenever", "-5"])
def test_an_unusable_retry_after_falls_back_to_the_backoff(raw):
    slept = []
    session = FakeSession(
        [
            FakeResponse(status_code=503, headers={"Retry-After": raw}),
            FakeResponse(payload=user_page("alice")),
        ]
    )

    assert fetch(session, sleep=slept.append) == {"alice"}
    # The backoff starts at two seconds and is jittered down by half at most.
    assert len(slept) == 1
    assert 1.0 <= slept[0] <= 2.0


def test_the_retry_loop_never_falls_through_without_an_answer(monkeypatch):
    # Defensive: the final attempt always returns or raises, so this is only
    # reachable if the attempt budget is ever configured away. It matters
    # because returning nothing here would look like a repository with no
    # stargazers, and strip the role from everybody who has one.
    monkeypatch.setattr("common.github_api.MAX_ATTEMPTS_PER_PAGE", 0)
    with pytest.raises(GitHubError, match="retry budget"):
        fetch(FakeSession([]))
