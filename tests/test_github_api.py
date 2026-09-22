"""Tests for the GitHub stargazer listing."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import zlib
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
import requests

from common.github_api import (
    MAX_ATTEMPTS_PER_PAGE,
    PER_PAGE,
    GitHubError,
    StargazerCache,
    account_stars_repo,
    fetch_stargazer_count,
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


def account_id(login):
    """A stable fake account id for ``login``.

    Derived rather than counted out, so the same person carries the same id
    on every page and in every cycle, which is the property the listing's id
    set is there to preserve.
    """
    return 1000 + zlib.crc32(login.lower().encode())


def user_page(*logins):
    return [{"login": login, "id": account_id(login)} for login in logins]


def no_sleep(_seconds):
    """Stand in for time.sleep so retry tests do not actually wait."""


def fetch(session, **kwargs):
    """Call the listing with retries that do not sleep."""
    kwargs.setdefault("sleep", no_sleep)
    return fetch_stargazer_logins("o", "r", session=session, **kwargs)


def test_single_page_returns_lowercased_logins():
    session = FakeSession([FakeResponse(payload=user_page("Alice", "BOB"))])
    assert fetch_stargazer_logins("o", "r", session=session) == {"alice", "bob"}


def test_the_listing_carries_the_account_ids_as_well_as_the_logins():
    # The id is what the un-star check matches on, because a login can be
    # renamed and an account id cannot. It was always in the response body
    # and used to be discarded.
    session = FakeSession([FakeResponse(payload=user_page("Alice", "BOB"))])
    listing = fetch_stargazer_listing("o", "r", session=session)
    assert listing.logins == {"alice", "bob"}
    assert listing.ids == {account_id("alice"), account_id("bob")}


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
        [FakeResponse(payload=[{"starred_at": "2024-01-01", "user": {"login": "Zed", "id": 7}}])]
    )
    listing = fetch_stargazer_listing("o", "r", session=session)
    assert listing.logins == {"zed"}
    assert listing.ids == {7}


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


def test_a_429_at_the_primary_limit_is_reported_rather_than_retried():
    # GitHub spells the primary limit 429 as often as 403, and the status on
    # its own cannot tell it apart from the secondary limit the retries exist
    # for; the remaining count at zero can. Retrying this one sleeps out the
    # Retry-After three times over and reports the same exhaustion at the
    # end, so a check would sit on a worker for three minutes for nothing.
    slept = []
    session = FakeSession(
        [
            FakeResponse(
                status_code=429,
                headers={"Retry-After": "60", "X-RateLimit-Remaining": "0"},
            )
        ]
    )

    with pytest.raises(GitHubError, match="rate limit"):
        fetch(session, sleep=slept.append)
    assert len(session.calls) == 1
    assert not slept


def test_a_429_with_budget_left_is_still_the_secondary_limit_and_is_retried():
    # The other side of the test above: the secondary limit clears in
    # seconds and leaves the hourly budget alone, so it must keep its retry.
    session = FakeSession(
        [
            FakeResponse(status_code=429, headers={"X-RateLimit-Remaining": "4999"}),
            FakeResponse(payload=user_page("alice")),
        ]
    )

    assert fetch(session) == {"alice"}
    assert len(session.calls) == 2


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


class UnparseableResponse(FakeResponse):
    """A 200 whose body is not JSON, the way requests reports it."""

    def json(self):
        # requests raises this rather than the stdlib error, and it
        # subclasses RequestException as well as ValueError. That second
        # base is the whole point of the test: it makes this look like a
        # transport failure to anything catching RequestException, while it
        # is raised from a place _get_page's own handler does not cover.
        raise requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)


def test_a_200_whose_body_is_not_json_raises_githuberror():
    # A truncated response, or a proxy interstitial served with the status of
    # the page it replaced. The listing must refuse it as GitHubError, which
    # is the only exception every caller catches; escaping as requests'
    # JSONDecodeError took the check cycle down with a traceback instead.
    session = FakeSession([UnparseableResponse()])
    with pytest.raises(GitHubError, match="body that is not JSON"):
        fetch_stargazer_logins("o", "r", session=session)


def test_an_unparseable_body_is_refused_rather_than_retried():
    # Refused on the first response, not retried: a body that is not JSON is
    # not a transport failure, and _get_page must not be handed a second
    # chance to turn it into one. One queued response and a session that
    # raises on an extra request is what proves only one was made.
    session = FakeSession([UnparseableResponse()])
    with pytest.raises(GitHubError):
        fetch_stargazer_logins("o", "r", session=session, sleep=no_sleep)
    assert len(session.calls) == 1


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
    # The dangerous half of the cache. A page that replayed only its logins
    # would leave the id set empty, and the un-star check matches on ids, so
    # a single 304 would strip the role from everybody behind that page.
    assert listing.ids == {account_id("alice")}
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
    listing = fetch_stargazer_listing("o", "r", session=second, cache=cache, sleep=no_sleep)
    assert listing.logins == {"a", "b", "c"}
    # The ids have to follow the logins exactly: the cached page contributes
    # its two, the re-fetched page its one, and the account that went away
    # contributes neither.
    assert listing.ids == {account_id("a"), account_id("b"), account_id("c")}
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


FIRST_PAGE_URL = "https://api.github.com/repos/o/r/stargazers"
SECOND_PAGE_URL = "https://api.github.com/page2"
COUNT_URL = "https://api.github.com/repos/o/r/stargazers/count"


def full_page_logins():
    """Exactly enough distinct logins to fill a page to its boundary."""
    return [f"user{index}" for index in range(PER_PAGE)]


class EtagAwareSession:
    """Serves pages by URL and honours If-None-Match the way GitHub does.

    FakeSession answers from a queue whatever the request headers say, which
    cannot tell a re-validated page from a freshly fetched one. That
    difference is the whole of the bug the tests below are about.
    """

    def __init__(self, pages):
        self._pages = pages
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        response = self._pages[url]
        etag = response.headers.get("ETag")
        if etag and (headers or {}).get("If-None-Match") == etag:
            return FakeResponse(status_code=304)
        return response


def test_a_full_last_page_cannot_hide_the_page_the_next_star_opens():
    # The listing is ordered oldest first, so the star after a page boundary
    # opens a page of its own and leaves the page before it byte for byte
    # what it was. Its ETag still matches, so a cached entry would answer
    # 304 with next_url=None and end the walk before the newcomer, who would
    # then look un-starred and lose the role.
    logins = full_page_logins()
    cache = StargazerCache()
    assert fetch(
        EtagAwareSession(
            {
                FIRST_PAGE_URL: page(*logins, etag='W/"full"'),
                COUNT_URL: FakeResponse(payload={"count": PER_PAGE}),
            }
        ),
        cache=cache,
    ) == set(logins)

    session = EtagAwareSession(
        {
            FIRST_PAGE_URL: page(*logins, etag='W/"full"', next_url=SECOND_PAGE_URL),
            SECOND_PAGE_URL: page("newcomer", etag='W/"p2"'),
        }
    )
    listing = fetch_stargazer_listing("o", "r", session=session, cache=cache, sleep=no_sleep)

    assert listing.logins == {*logins, "newcomer"}
    assert account_id("newcomer") in listing.ids
    # No conditional request was sent for the page at risk, which is what
    # makes the 304 above impossible rather than merely unlikely.
    assert "If-None-Match" not in session.calls[0]["headers"]


def test_a_full_page_that_already_leads_somewhere_keeps_its_304_saving():
    # The exclusion is deliberately narrow. Only the last page can grow a
    # new page behind it, so a full page that already has a next link, which
    # is every page of a long listing but one, stays cached and stays free.
    logins = full_page_logins()
    cache = StargazerCache()
    first = EtagAwareSession(
        {
            FIRST_PAGE_URL: page(*logins, etag='W/"p1"', next_url=SECOND_PAGE_URL),
            SECOND_PAGE_URL: page("tail", etag='W/"p2"'),
        }
    )
    assert fetch(first, cache=cache) == {*logins, "tail"}
    assert set(cache.pages) == {FIRST_PAGE_URL, SECOND_PAGE_URL}

    second = EtagAwareSession(
        {
            FIRST_PAGE_URL: page(*logins, etag='W/"p1"', next_url=SECOND_PAGE_URL),
            SECOND_PAGE_URL: page("tail", etag='W/"p2"'),
        }
    )
    listing = fetch_stargazer_listing("o", "r", session=second, cache=cache, sleep=no_sleep)

    assert listing.logins == {*logins, "tail"}
    assert listing.pages_unchanged == 2
    assert listing.pages_fetched == 0


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


@pytest.mark.parametrize(
    "entry",
    [
        "nonsense",
        None,
        17,
        {},
        {"login": "Alice"},
        {"login": "Alice", "id": None},
        {"login": "Alice", "id": "1234"},
        {"login": "Alice", "id": 12.5},
        {"login": None, "id": 1234},
        {"starred_at": "2024-01-01", "user": {"login": "Alice"}},
    ],
    ids=[
        "a string",
        "null",
        "a number",
        "an empty object",
        "no id",
        "a null id",
        "an id as a string",
        "an id that is not whole",
        "a null login",
        "no id inside the envelope",
    ],
)
def test_an_entry_that_cannot_be_read_costs_the_cycle_rather_than_a_role(entry):
    # This test used to assert the opposite, that a page should cost the
    # caller the entries it cannot read and never the whole cycle, on the
    # reasoning that the listing still describes the accounts GitHub
    # described properly. That reasoning was written when the un-star check
    # compared logins, and it does not survive the check matching on ids:
    # an entry dropped here is an account missing from listing.ids, the
    # check cannot tell that apart from somebody who un-starred, and the
    # member loses a role while their login sits in the very same response.
    # Refusing the page costs one cycle, which the next one makes up.
    session = FakeSession([FakeResponse(payload=[{"login": "Bob", "id": 2}, entry])])
    with pytest.raises(GitHubError, match="no usable login and account id"):
        fetch_stargazer_listing("o", "r", session=session)


def test_an_id_that_is_a_bool_is_not_read_as_the_account_numbered_one():
    # True is an int in Python and hashes equal to 1, so an unchecked id
    # would put the member whose account id is 1 in the listing and leave
    # whoever this entry is out of it.
    session = FakeSession([FakeResponse(payload=[{"login": "Alice", "id": True}])])
    with pytest.raises(GitHubError, match="no usable login and account id"):
        fetch_stargazer_listing("o", "r", session=session)


def test_a_page_whose_entries_all_read_is_accepted_whole():
    # The other side of the rule: refusing a page must not become refusing
    # the ordinary ones. The envelope spelling counts as readable too.
    session = FakeSession(
        [
            FakeResponse(
                payload=[
                    {"login": "Alice", "id": 1},
                    {"starred_at": "2024-01-01", "user": {"login": "Bob", "id": 2}},
                ]
            )
        ]
    )
    listing = fetch_stargazer_listing("o", "r", session=session)
    assert listing.logins == {"alice", "bob"}
    assert listing.ids == {1, 2}


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


def test_a_listing_github_cut_short_is_marked_truncated():
    # GitHub stops serving stargazers after the 40,000 oldest, and the last
    # page it serves has no next link, so the pages alone look complete.
    # The count is what gives it away.
    logins = full_page_logins()
    session = EtagAwareSession(
        {
            FIRST_PAGE_URL: page(*logins),
            COUNT_URL: FakeResponse(payload={"count": PER_PAGE + 4671}),
        }
    )
    listing = fetch_stargazer_listing("o", "r", session=session, sleep=no_sleep)

    assert listing.truncated is True
    assert listing.logins == set(logins)
    assert listing.api_calls == 2


def test_a_listing_ending_exactly_on_a_page_boundary_is_complete():
    logins = full_page_logins()
    session = EtagAwareSession(
        {FIRST_PAGE_URL: page(*logins), COUNT_URL: FakeResponse(payload={"count": PER_PAGE})}
    )
    assert fetch_stargazer_listing("o", "r", session=session, sleep=no_sleep).truncated is False


def test_a_listing_ending_on_a_partial_page_costs_no_count_request():
    session = FakeSession([FakeResponse(payload=user_page("alice"))])
    listing = fetch_stargazer_listing("o", "r", session=session)
    assert listing.truncated is False
    assert len(session.calls) == 1


def test_an_unreadable_count_refuses_the_listing():
    # Refused rather than read as complete: a listing that might be short
    # must not be acted on as though it were whole.
    session = EtagAwareSession(
        {FIRST_PAGE_URL: page(*full_page_logins()), COUNT_URL: FakeResponse(payload=["?"])}
    )
    with pytest.raises(GitHubError):
        fetch_stargazer_listing("o", "r", session=session, sleep=no_sleep)


def test_the_star_count_is_one_request():
    session = FakeSession([FakeResponse(payload={"count": 44671})])
    assert fetch_stargazer_count("o", "r", token="t", session=session) == 44671
    assert session.calls[0]["url"] == COUNT_URL
    assert session.calls[0]["headers"]["Authorization"] == "Bearer t"


@pytest.mark.parametrize("payload", [{"count": "44671"}, {"count": True}, {}, [1]])
def test_a_malformed_star_count_is_an_error(payload):
    with pytest.raises(GitHubError):
        fetch_stargazer_count("o", "r", session=FakeSession([FakeResponse(payload=payload)]))


def starred(*names):
    return [{"full_name": name} for name in names]


def test_an_account_found_in_its_own_starred_list_stars_the_repo():
    session = FakeSession(
        [
            FakeResponse(
                payload=starred("x/y"), links={"next": {"url": "https://api.github.com/s2"}}
            ),
            FakeResponse(payload=starred("O/R")),
        ]
    )
    assert account_stars_repo("o", "r", github_id=42, session=session) is True
    # Addressed by id, which survives a rename.
    assert session.calls[0]["url"] == "https://api.github.com/user/42/starred"


def test_an_account_whose_starred_list_ends_without_the_repo_does_not_star_it():
    session = FakeSession([FakeResponse(payload=starred("x/y"))])
    assert account_stars_repo("o", "r", login="alice", session=session) is False
    assert session.calls[0]["url"] == "https://api.github.com/users/alice/starred"


@pytest.mark.parametrize(
    "response",
    [FakeResponse(status_code=404), FakeResponse(payload={"oops": 1}), FakeResponse(payload=[{}])],
)
def test_an_incomplete_starred_list_is_an_error_not_a_no(response):
    with pytest.raises(GitHubError):
        account_stars_repo("o", "r", github_id=42, session=FakeSession([response]), sleep=no_sleep)


def test_a_missing_account_is_named_as_the_account_not_the_repository():
    session = FakeSession([FakeResponse(status_code=404)])
    with pytest.raises(GitHubError, match="no such account"):
        account_stars_repo("o", "r", login="renamed", session=session, sleep=no_sleep)


def test_an_account_with_nothing_to_address_it_by_is_an_error():
    with pytest.raises(GitHubError):
        account_stars_repo("o", "r")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (FakeResponse(status_code=401), "401"),
        (UnparseableResponse(), "not JSON"),
    ],
)
def test_a_failed_star_count_is_an_error(response, message):
    with pytest.raises(GitHubError, match=message):
        fetch_stargazer_count("o", "r", session=FakeSession([response]), sleep=no_sleep)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (FakeResponse(status_code=401), "401"),
        (UnparseableResponse(), "not JSON"),
    ],
)
def test_a_starred_list_that_cannot_be_read_is_an_error(response, message):
    with pytest.raises(GitHubError, match=message):
        account_stars_repo("o", "r", github_id=42, session=FakeSession([response]), sleep=no_sleep)


def test_the_starred_list_walk_is_bounded(monkeypatch):
    monkeypatch.setattr("common.github_api.MAX_PAGES", 3)

    class LoopingSession:
        """Always reports another page, to prove the walk is bounded."""

        def get(self, url, headers=None, params=None, timeout=None):
            return FakeResponse(
                payload=starred("x/y"), links={"next": {"url": "https://api.github.com/same"}}
            )

    with pytest.raises(GitHubError, match="pagination loop"):
        account_stars_repo("o", "r", github_id=42, session=LoopingSession())
