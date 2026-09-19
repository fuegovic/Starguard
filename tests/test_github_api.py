"""Tests for the GitHub stargazer listing."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import pytest
import requests

from common.github_api import GitHubError, fetch_stargazer_logins


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


def test_single_page_returns_lowercased_logins():
    session = FakeSession([FakeResponse(payload=user_page("Alice", "BOB"))])
    assert fetch_stargazer_logins("o", "r", session=session) == {"alice", "bob"}


def test_follows_the_link_header_across_pages():
    session = FakeSession([
        FakeResponse(
            payload=user_page("one"),
            links={"next": {"url": "https://api.github.com/page2"}},
        ),
        FakeResponse(
            payload=user_page("two"),
            links={"next": {"url": "https://api.github.com/page3"}},
        ),
        FakeResponse(payload=user_page("three")),
    ])

    assert fetch_stargazer_logins("o", "r", session=session) == {"one", "two", "three"}
    assert len(session.calls) == 3
    # The first request asks for a full page; later ones reuse GitHub's own URL
    # verbatim, which already carries the paging parameters.
    assert session.calls[0]["params"] == {"per_page": 100}
    assert session.calls[1]["url"] == "https://api.github.com/page2"
    assert session.calls[1]["params"] is None


def test_accepts_the_star_json_envelope():
    session = FakeSession([
        FakeResponse(payload=[{"starred_at": "2024-01-01", "user": {"login": "Zed"}}])
    ])
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
    session = FakeSession([
        FakeResponse(status_code=403, headers={"X-RateLimit-Remaining": "0"})
    ])
    with pytest.raises(GitHubError, match="rate limit"):
        fetch_stargazer_logins("o", "r", session=session)


@pytest.mark.parametrize(
    "status,expected",
    [(401, "GITHUB_TOKEN"), (404, "not found"), (500, "HTTP 500")],
)
def test_error_statuses_raise(status, expected):
    session = FakeSession([FakeResponse(status_code=status)])
    with pytest.raises(GitHubError, match=expected):
        fetch_stargazer_logins("o", "r", session=session)


def test_partial_results_are_never_returned():
    # The second page fails. Returning page one alone would look like everyone
    # on later pages had un-starred, and strip their roles.
    session = FakeSession([
        FakeResponse(
            payload=user_page("kept"),
            links={"next": {"url": "https://api.github.com/page2"}},
        ),
        FakeResponse(status_code=500),
    ])
    with pytest.raises(GitHubError):
        fetch_stargazer_logins("o", "r", session=session)


def test_network_failure_raises_githuberror():
    session = FakeSession([requests.ConnectionError("boom")])
    with pytest.raises(GitHubError, match="Could not reach"):
        fetch_stargazer_logins("o", "r", session=session)


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
