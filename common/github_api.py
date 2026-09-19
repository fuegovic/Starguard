"""Minimal GitHub REST helpers.

Only the stargazer listing lives here. It is kept free of Discord and database
concerns so the pagination, the retries and the conditional requests can be
tested directly.
"""

import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final, Protocol, cast

import requests

log = logging.getLogger(__name__)

API_ROOT: Final = "https://api.github.com"
API_VERSION: Final = "2022-11-28"
PER_PAGE: Final = 100
REQUEST_TIMEOUT: Final = 30

OK: Final = 200
NOT_MODIFIED: Final = 304

# A repository with more than 100k stargazers would exceed this; the cap only
# exists so a malformed Link header cannot spin forever.
MAX_PAGES: Final = 1000

# Statuses worth trying again. 429 is GitHub's secondary rate limit, which is
# short lived and usually carries a Retry-After; the 5xx family is GitHub
# having a bad moment. A 403 with no rate limit left is the primary limit and
# is deliberately absent, because it does not clear for up to an hour.
RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS_PER_PAGE: Final = 4
RETRY_BASE_DELAY_SECONDS: Final = 2.0
RETRY_MAX_DELAY_SECONDS: Final = 60.0


class SupportsGet(Protocol):
    """The slice of :mod:`requests` that the listing actually calls.

    Both the module itself and a :class:`requests.Session` satisfy this, which
    is what lets a caller hand in a session for connection reuse without the
    signatures below having to name one.
    """

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, int] | None,
        timeout: float,
    ) -> requests.Response:
        """Perform one GET and return the response."""


class GitHubError(RuntimeError):
    """Raised when the stargazer listing could not be retrieved in full."""


@dataclass(frozen=True)
class CachedPage:
    """One page of the listing as it was last seen."""

    etag: str
    logins: frozenset[str]
    next_url: str | None


@dataclass(frozen=True)
class StargazerListing:
    """The complete listing plus what it cost to obtain."""

    logins: frozenset[str]
    api_calls: int = 0
    pages_fetched: int = 0
    pages_unchanged: int = 0
    rate_limit_remaining: int | None = None


@dataclass
class StargazerCache:
    """ETag cache for the paginated stargazer listing.

    Conditional requests are per resource, and each page of the listing is its
    own resource, so entries are keyed by page URL. A 304 on one page says
    only that that page's body is byte-identical; it never says the listing as
    a whole is unchanged.

    That distinction is what makes this safe. GitHub orders stargazers by the
    time they starred, so a new star lands on the last page and leaves every
    earlier page untouched, while an un-star shifts every later entry up a
    slot and changes all the pages after it. Either way the result is the
    union of the pages actually walked this cycle, so a reused page can only
    contribute logins that really are still in that page's body.

    The point of all this is that a 304 does not count against the REST rate
    limit, so a repository whose early pages rarely change costs a fraction of
    a full listing per cycle.

    Not thread safe: one instance belongs to one star checker, which holds a
    lock for the whole cycle.
    """

    pages: dict[str, CachedPage] = field(default_factory=dict)

    def get(self, url: str) -> CachedPage | None:
        """Return the cached page for ``url``, or None."""
        return self.pages.get(url)

    def replace(self, pages: dict[str, CachedPage]) -> None:
        """Adopt ``pages`` as the whole cache.

        Replacing rather than merging is what drops entries for pages that no
        longer exist, which is what happens when the listing shrinks.
        """
        self.pages = pages


def _headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _describe_failure(response: requests.Response) -> str:
    """Turn a failed response into a message that names the actual problem."""
    status = response.status_code
    remaining = response.headers.get("X-RateLimit-Remaining")

    if status in (403, 429) and remaining == "0":
        return (
            "GitHub API rate limit exceeded. Set GITHUB_TOKEN to raise the "
            "limit, or increase AUTOMATIC_CHECK_DELAY."
        )
    if status == 401:
        return "GitHub rejected GITHUB_TOKEN (401). Check that it is valid."
    if status == 404:
        return (
            "Repository not found (404). Check REPO_OWNER and GITHUB_REPO, "
            "and note that a private repository needs a GITHUB_TOKEN that can "
            "read it."
        )
    return f"GitHub API returned HTTP {status}."


def _extract_login(entry: object) -> str | None:
    """Return the login from a stargazer entry.

    The plain listing returns user objects; the ``star+json`` media type wraps
    them in ``{"starred_at": ..., "user": {...}}``. Both are accepted so the
    helper keeps working if the Accept header is ever changed back.
    """
    if not isinstance(entry, dict):
        return None
    user: object = entry.get("user")
    if isinstance(user, dict):
        login: object = user.get("login")
    else:
        login = entry.get("login")
    # GitHub always sends a string here. The value is not re-validated,
    # because narrowing it would quietly turn a malformed response into a
    # silently shorter listing, and a short listing strips roles from people
    # who never un-starred.
    return cast("str | None", login)


def _rate_limit_remaining(response: requests.Response) -> int | None:
    """Return X-RateLimit-Remaining as an int, or None when absent."""
    raw = response.headers.get("X-RateLimit-Remaining")
    try:
        # An absent header is None, and int(None) is the TypeError caught
        # below; the ignore keeps that deliberate EAFP shape.
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _retry_after_seconds(response: requests.Response) -> float | None:
    """Return the Retry-After delay in seconds, or None when not sent.

    GitHub sends either a count of seconds or an HTTP date, and both spellings
    are allowed by RFC 9110, so both are handled.
    """
    raw = (response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter for retry number ``attempt``."""
    # The exponent is capped before the shift so a large attempt count cannot
    # build an enormous integer on the way to min().
    ceiling: float = RETRY_BASE_DELAY_SECONDS * 2 ** min(attempt - 1, 10)
    # B311: this jitter spreads retries out, it is not a secret.
    return min(ceiling, RETRY_MAX_DELAY_SECONDS) * random.uniform(0.5, 1.0)  # nosec B311


def _get_page(
    http: SupportsGet,
    url: str,
    headers: Mapping[str, str],
    params: Mapping[str, int] | None,
    sleep: Callable[[float], object],
) -> tuple[requests.Response, int]:
    """Fetch one page, retrying transient failures. Returns (response, calls).

    Transport errors are retried too: a dropped connection is exactly as
    transient as a 503, and giving up on the first one used to abandon a whole
    check cycle.
    """
    for attempt in range(1, MAX_ATTEMPTS_PER_PAGE + 1):
        final = attempt == MAX_ATTEMPTS_PER_PAGE

        try:
            response = http.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            if final:
                raise GitHubError(f"Could not reach the GitHub API: {exc}") from exc
            delay = _backoff_delay(attempt)
            log.warning("GitHub request failed (%s); retrying in %.1fs", exc, delay)
            sleep(delay)
            continue

        if final or response.status_code not in RETRY_STATUSES:
            return response, attempt

        # Held under its own name rather than reassigned over ``delay``: the
        # transport-error branch above has already fixed that variable as a
        # plain float, and this one is "what GitHub asked for, if anything".
        retry_after = _retry_after_seconds(response)
        if retry_after is None:
            delay = _backoff_delay(attempt)
        elif retry_after > RETRY_MAX_DELAY_SECONDS:
            # Waiting this out would hold a worker thread for longer than the
            # check interval itself, so report it instead of sleeping on it.
            raise GitHubError(
                f"GitHub asked for a {int(retry_after)}s wait before retrying "
                f"(HTTP {response.status_code}). Increase "
                "AUTOMATIC_CHECK_DELAY or set GITHUB_TOKEN."
            )
        else:
            delay = retry_after
        log.warning(
            "GitHub returned HTTP %s; retrying in %.1fs",
            response.status_code,
            delay,
        )
        sleep(delay)

    # Unreachable: the final attempt always returns or raises.
    raise GitHubError("Exhausted the GitHub API retry budget.")


@dataclass
class _Walk:
    """What one pass over the paginated listing has accumulated so far."""

    logins: set[str]
    pages: dict[str, CachedPage]
    api_calls: int = 0
    pages_fetched: int = 0
    pages_unchanged: int = 0
    rate_limit_remaining: int | None = None

    def result(self) -> StargazerListing:
        """Freeze the walk into the listing the caller gets."""
        return StargazerListing(
            logins=frozenset(self.logins),
            api_calls=self.api_calls,
            pages_fetched=self.pages_fetched,
            pages_unchanged=self.pages_unchanged,
            rate_limit_remaining=self.rate_limit_remaining,
        )


def _absorb_page(walk: _Walk, url: str, response: requests.Response, caching: bool) -> str | None:
    """Fold a 200 response into ``walk``. Returns the next page's URL."""
    if response.status_code != OK:
        raise GitHubError(_describe_failure(response))

    page = response.json()
    if not isinstance(page, list):
        raise GitHubError("Unexpected response shape from the GitHub API.")

    walk.pages_fetched += 1
    logins = frozenset(
        login.lower() for login in (_extract_login(entry) for entry in page) if login
    )
    walk.logins |= logins

    # requests parses the RFC 5988 Link header for us, which avoids the
    # hand-rolled string splitting this used to do.
    next_url = response.links.get("next", {}).get("url")

    etag = response.headers.get("ETag")
    if caching and etag:
        walk.pages[url] = CachedPage(etag, logins, next_url)

    return next_url


def fetch_stargazer_listing(
    owner: str,
    repo: str,
    token: str | None = None,
    session: SupportsGet | None = None,
    cache: StargazerCache | None = None,
    sleep: Callable[[float], object] = time.sleep,
) -> StargazerListing:
    """Return a :class:`StargazerListing` for ``owner/repo``.

    Logins are lower-cased because GitHub treats usernames case-insensitively,
    and a set is returned so membership tests stay constant time no matter how
    many stargazers the repository has.

    Raises :class:`GitHubError` rather than returning a partial set: acting on
    an incomplete listing would strip roles from people who never un-starred.

    Passing a :class:`StargazerCache` turns each page request into a
    conditional one. See that class for why a per-page 304 is safe.
    """
    http: SupportsGet = session or requests
    url = f"{API_ROOT}/repos/{owner}/{repo}/stargazers"
    params: Mapping[str, int] | None = {"per_page": PER_PAGE}
    base_headers = _headers(token)
    walk = _Walk(logins=set(), pages={})

    for _ in range(MAX_PAGES):
        cached = cache.get(url) if cache is not None else None
        headers = dict(base_headers)
        if cached is not None:
            headers["If-None-Match"] = cached.etag

        response, calls = _get_page(http, url, headers, params, sleep)
        walk.api_calls += calls

        remaining = _rate_limit_remaining(response)
        if remaining is not None:
            walk.rate_limit_remaining = remaining

        if response.status_code == NOT_MODIFIED and cached is not None:
            walk.pages_unchanged += 1
            walk.logins |= cached.logins
            walk.pages[url] = cached
            next_url = cached.next_url
        else:
            next_url = _absorb_page(walk, url, response, cache is not None)

        if not next_url:
            if cache is not None:
                cache.replace(walk.pages)
            return walk.result()

        # The next URL already carries per_page and page.
        url, params = next_url, None

    raise GitHubError(
        f"Stopped after {MAX_PAGES} pages of stargazers; this looks like a pagination loop."
    )


def fetch_stargazer_logins(
    owner: str,
    repo: str,
    token: str | None = None,
    session: SupportsGet | None = None,
    cache: StargazerCache | None = None,
    sleep: Callable[[float], object] = time.sleep,
) -> frozenset[str]:
    """Return the set of lower-cased logins that have starred ``owner/repo``.

    A thin wrapper over :func:`fetch_stargazer_listing` for callers that want
    the logins and nothing else.
    """
    return fetch_stargazer_listing(
        owner, repo, token=token, session=session, cache=cache, sleep=sleep
    ).logins
