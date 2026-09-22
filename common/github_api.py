"""Minimal GitHub REST helpers.

The stargazer listing, the stargazer count and a per-account star lookup live
here. It is kept free of Discord and database
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
from typing import Final, Protocol

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
# having a bad moment. 403 is deliberately absent: GitHub uses it for the
# primary limit, which does not clear for up to an hour.
RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

# The two statuses GitHub spells the primary rate limit with. The status alone
# does not say which limit was hit, because the secondary limit shares both;
# the remaining count at zero is what separates them.
PRIMARY_LIMIT_STATUSES: Final[frozenset[int]] = frozenset({403, 429})
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
    """One page of the listing as it was last seen.

    Both halves of the identity are memoised, not just the logins. A page
    that replayed only its logins would contribute nothing to the id set on
    a 304, and the un-star check matches on ids, so every member behind a
    cached page would look as though they had un-starred.

    ``next_url`` is the link as it stood when the page was read. A page whose
    link could have appeared since is never cached, so replaying this one
    cannot truncate the walk; :func:`_hides_a_future_page` is that rule.
    """

    etag: str
    logins: frozenset[str]
    ids: frozenset[int]
    next_url: str | None


@dataclass(frozen=True)
class StargazerListing:
    """The complete listing plus what it cost to obtain.

    ``ids`` and ``logins`` describe the same set of accounts: the id is what
    the un-star check matches on, because it cannot be renamed, and the login
    is what the logs are readable by.

    ``truncated`` means GitHub stopped serving the listing before its end.
    It will not page past the 40,000 oldest stargazers, and the last page it
    does serve carries no next link, so a walk over a larger repository
    ends there looking complete. When this is set, an account missing from
    the listing is not evidence that it un-starred; see
    :func:`account_stars_repo` for the question to ask instead.
    """

    logins: frozenset[str]
    ids: frozenset[int]
    api_calls: int = 0
    pages_fetched: int = 0
    pages_unchanged: int = 0
    rate_limit_remaining: int | None = None
    truncated: bool = False


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
    contribute accounts that really are still in that page's body.

    The one page that does not follow from that argument is a last page with
    no room left, which is kept out of the cache entirely; see
    :func:`_hides_a_future_page`.

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

    if _is_primary_rate_limit(response):
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


def _extract_identity(entry: object) -> tuple[str, int] | None:
    """Return the ``(login, account id)`` pair from a stargazer entry.

    The plain listing returns user objects; the ``star+json`` media type wraps
    them in ``{"starred_at": ..., "user": {...}}``. Both are accepted so the
    helper keeps working if the Accept header is ever changed back.

    The id was already in every response body and used to be thrown away.
    Collecting it is what lets the un-star check match on something GitHub
    does not let people change.

    None means the entry does not carry both halves in a usable shape, and
    :func:`_absorb_page` then refuses the page rather than dropping the
    entry out of the listing. This used to hand both values back uncast and
    unchecked, and the reason written here was that narrowing them would
    quietly turn a malformed response into a silently shorter listing,
    which strips roles from people who never un-starred. That danger is
    real and is still the one being avoided; what has changed is that not
    narrowing them no longer avoids it. The un-star check matches on the id
    alone for every row written since ids were recorded, so an id that is
    absent, null or not an integer already produces exactly that shorter
    listing: the account is missing from ``ids``, or sits there in a shape
    that can never equal the integer in the row, and its owner looks as
    though they had un-starred. Reading the value and refusing the page is
    the only version of this that does not act on it.

    A bool is turned away with the rest because it is an ``int`` subclass,
    so ``True`` would otherwise be indistinguishable from the account whose
    id is 1.
    """
    if not isinstance(entry, dict):
        return None
    user: object = entry.get("user")
    if not isinstance(user, dict):
        user = entry
    login = user.get("login")
    account_id = user.get("id")
    if isinstance(login, str) and isinstance(account_id, int) and not isinstance(account_id, bool):
        return login, account_id
    return None


def _rate_limit_remaining(response: requests.Response) -> int | None:
    """Return X-RateLimit-Remaining as an int, or None when absent."""
    raw = response.headers.get("X-RateLimit-Remaining")
    try:
        # An absent header is None, and int(None) is the TypeError caught
        # below; the ignore keeps that deliberate EAFP shape.
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _is_primary_rate_limit(response: requests.Response) -> bool:
    """True when the response is the primary rate limit, not a passing blip.

    One predicate serves both the retry decision and the message, because the
    two disagreeing was the bug: the message already called a 429 with
    nothing left the primary limit, while the retry loop went on treating it
    as the short-lived secondary one and slept out its Retry-After three
    times before reporting the same exhaustion anyway.
    """
    remaining = _rate_limit_remaining(response)
    return response.status_code in PRIMARY_LIMIT_STATUSES and remaining == 0


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

        # The primary limit wears a retryable status but is not retryable: it
        # takes up to an hour to clear, so the whole budget would go on
        # sleeping and the caller would hear about it three minutes later
        # than it could have. Handing the response back lets
        # _describe_failure name it. 403 needs no such test because it is not
        # in RETRY_STATUSES at all; 429 is, because the secondary limit that
        # the retries exist for uses the same status.
        if _is_primary_rate_limit(response):
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

    # Tallies of one walk, each read once by result(). Grouping them into
    # sub-objects would add indirection without separating any concern.
    # pylint: disable=too-many-instance-attributes

    logins: set[str]
    ids: set[int]
    pages: dict[str, CachedPage]
    api_calls: int = 0
    pages_fetched: int = 0
    pages_unchanged: int = 0
    rate_limit_remaining: int | None = None
    last_page_full: bool = False
    truncated: bool = False

    def result(self) -> StargazerListing:
        """Freeze the walk into the listing the caller gets."""
        return StargazerListing(
            logins=frozenset(self.logins),
            ids=frozenset(self.ids),
            api_calls=self.api_calls,
            pages_fetched=self.pages_fetched,
            pages_unchanged=self.pages_unchanged,
            rate_limit_remaining=self.rate_limit_remaining,
            truncated=self.truncated,
        )


def _hides_a_future_page(entry_count: int, next_url: str | None) -> bool:
    """True when caching this page could hide a page that does not exist yet.

    Stargazers come back oldest first, so a new star always lands at the end
    of the listing. When the last page is already full, that star opens a
    brand new page without touching the body of the page before it: the ETag
    still matches, GitHub answers 304, and the cached ``next_url`` of None
    ends the walk one page early. The listing then looks as though everyone
    on the new page had un-starred, and the check strips their roles.

    Only the final page can be caught this way, which is what keeps the 304
    saving intact for the pages that make up almost all of a listing. A page
    that already has a next link keeps it whatever happens behind it, and a
    page with room left absorbs the new star into its own body, which changes
    the ETag and produces a 200. So the only entry left out of the cache is
    the last page, and only in the one case where the listing ends exactly on
    a page boundary; that costs a single unconditional request per cycle.
    """
    return next_url is None and entry_count >= PER_PAGE


def _absorb_page(walk: _Walk, url: str, response: requests.Response, caching: bool) -> str | None:
    """Fold a 200 response into ``walk``. Returns the next page's URL."""
    if response.status_code != OK:
        raise GitHubError(_describe_failure(response))

    try:
        page = response.json()
    except ValueError as exc:
        # A 200 whose body is not JSON at all: a truncated response, or a
        # proxy or CDN interstitial served with the status of the thing it
        # replaced. requests raises its own JSONDecodeError, which subclasses
        # RequestException as well as ValueError, and that inheritance is the
        # trap: _get_page catches RequestException around the transport call
        # only, so this parse sits outside it and the error escaped
        # fetch_stargazer_listing unconverted. Every caller catches
        # GitHubError and nothing else, so an un-translated one took down the
        # check cycle with a traceback instead of the handled path.
        #
        # Refused rather than retried, alongside the shape check below, for
        # the reason in this function's own docstring: a listing this cannot
        # read in full is incomplete, and acting on an incomplete listing
        # strips roles from people who never un-starred.
        raise GitHubError("A page of stargazers came back with a body that is not JSON.") from exc

    if not isinstance(page, list):
        raise GitHubError("Unexpected response shape from the GitHub API.")

    walk.pages_fetched += 1
    walk.last_page_full = len(page) >= PER_PAGE
    identities = [_extract_identity(entry) for entry in page]
    readable = [identity for identity in identities if identity is not None]
    if len(readable) != len(identities):
        # The same rule as the one on the listing as a whole: an incomplete
        # answer is refused rather than reconciled against. Dropping the
        # entry would leave a stargazer out of the sets the un-star check
        # matches on, and the check cannot tell that apart from somebody
        # who really did un-star.
        raise GitHubError(
            "A page of stargazers carried an entry with no usable login and account id, "
            "so the listing was refused rather than read as un-starred."
        )
    logins = frozenset(login.lower() for login, _ in readable)
    ids = frozenset(account_id for _, account_id in readable)
    walk.logins |= logins
    walk.ids |= ids

    # requests parses the RFC 5988 Link header for us, which avoids the
    # hand-rolled string splitting this used to do.
    next_url = response.links.get("next", {}).get("url")

    etag = response.headers.get("ETag")
    if caching and etag and not _hides_a_future_page(len(page), next_url):
        walk.pages[url] = CachedPage(etag, logins, ids, next_url)

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

    Both the immutable numeric account ids and the logins come back. Logins
    are lower-cased because GitHub treats usernames case-insensitively, and
    sets are returned so membership tests stay constant time no matter how
    many stargazers the repository has.

    Raises :class:`GitHubError` rather than returning a partial set: acting on
    an incomplete listing would strip roles from people who never un-starred.
    A page carrying an entry this cannot read is incomplete in exactly that
    way, so it is refused here too; see :func:`_extract_identity`.

    Passing a :class:`StargazerCache` turns each page request into a
    conditional one. See that class for why a per-page 304 is safe.
    """
    http: SupportsGet = session or requests
    url = f"{API_ROOT}/repos/{owner}/{repo}/stargazers"
    params: Mapping[str, int] | None = {"per_page": PER_PAGE}
    base_headers = _headers(token)
    walk = _Walk(logins=set(), ids=set(), pages={})

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
            walk.ids |= cached.ids
            walk.pages[url] = cached
            # A cached page is never a full last page, see
            # _hides_a_future_page, so a walk that ends on one reached the
            # real end of the listing.
            walk.last_page_full = False
            # Trusting the cached link is only safe because of the rule in
            # _hides_a_future_page: a page that was both full and last was
            # never cached, so a cached None really does mean the listing
            # ends here, not merely that it ended here last time.
            next_url = cached.next_url
        else:
            next_url = _absorb_page(walk, url, response, cache is not None)

        if not next_url:
            if cache is not None:
                cache.replace(walk.pages)
            if walk.last_page_full:
                _check_for_truncation(http, owner, repo, base_headers, walk, sleep)
            return walk.result()

        # The next URL already carries per_page and page.
        url, params = next_url, None

    raise GitHubError(
        f"Stopped after {MAX_PAGES} pages of stargazers; this looks like a pagination loop."
    )


def _check_for_truncation(
    http: SupportsGet,
    owner: str,
    repo: str,
    headers: Mapping[str, str],
    walk: _Walk,
    sleep: Callable[[float], object],
) -> None:
    """Mark ``walk`` truncated when GitHub holds back part of the listing.

    GitHub serves the oldest 40,000 stargazers and no more: the page after
    that answers 422, and the page before it carries no next link at all,
    so the walk has no way to tell from the pages alone that it stopped
    early. On danny-avila/LibreChat that hid 4,671 stargazers, and the
    check read every linked member among them as having un-starred.

    Only a walk that ended on a full page can have been cut off, because a
    page with room left is the true end of the listing. That is also the
    one case a repository whose star count is an exact multiple of the page
    size lands in, so the count settles it for one extra request. A count
    above what the walk saw marks the listing truncated; a star arriving
    during the walk can do the same, and that is harmless, because a
    truncated listing only makes the check ask about each missing account
    directly instead of reading its absence as an un-star.
    """
    count, calls = _fetch_count(http, owner, repo, headers, sleep)
    walk.api_calls += calls
    walk.truncated = count > len(walk.ids)
    if walk.truncated:
        log.info(
            "GitHub served %s of %s stargazers; accounts missing from the listing "
            "will be checked one by one.",
            len(walk.ids),
            count,
        )


def _fetch_count(
    http: SupportsGet,
    owner: str,
    repo: str,
    headers: Mapping[str, str],
    sleep: Callable[[float], object],
) -> tuple[int, int]:
    """Return ``(stargazer count, api calls spent)`` for ``owner/repo``."""
    url = f"{API_ROOT}/repos/{owner}/{repo}/stargazers/count"
    response, calls = _get_page(http, url, headers, None, sleep)
    if response.status_code != OK:
        raise GitHubError(_describe_failure(response))
    try:
        body = response.json()
    except ValueError as exc:
        raise GitHubError("The stargazer count came back with a body that is not JSON.") from exc
    count = body.get("count") if isinstance(body, dict) else None
    if not isinstance(count, int) or isinstance(count, bool):
        raise GitHubError("Unexpected response shape from the GitHub API.")
    return count, calls


def fetch_stargazer_count(
    owner: str,
    repo: str,
    token: str | None = None,
    session: SupportsGet | None = None,
    sleep: Callable[[float], object] = time.sleep,
) -> int:
    """Return how many accounts have starred ``owner/repo``, in one request.

    The listing cannot answer this for a large repository: it takes a
    request per hundred stargazers and stops at 40,000 of them.
    """
    count, _ = _fetch_count(session or requests, owner, repo, _headers(token), sleep)
    return count


def account_stars_repo(
    owner: str,
    repo: str,
    github_id: int | None = None,
    login: str | None = None,
    token: str | None = None,
    session: SupportsGet | None = None,
    sleep: Callable[[float], object] = time.sleep,
) -> bool:
    """Whether one account stars ``owner/repo``, from its own starred list.

    This is the question to ask about an account that a truncated listing
    does not show. It walks the account's starred repositories rather than
    the repository's stargazers, so its cost is set by how much that one
    person has starred, not by how popular the repository is.

    The account is addressed by id when there is one, because a login can
    be renamed and the id cannot, and by login only for rows old enough to
    have no id. Anything short of a complete, readable answer raises
    :class:`GitHubError`: the caller must read that as "no evidence", never
    as "not starred".
    """
    if github_id is not None:
        url = f"{API_ROOT}/user/{github_id}/starred"
    elif login:
        url = f"{API_ROOT}/users/{login}/starred"
    else:
        raise GitHubError("No account id or login to look the star up by.")

    http: SupportsGet = session or requests
    headers = _headers(token)
    params: Mapping[str, int] | None = {"per_page": PER_PAGE}
    target = f"{owner}/{repo}".lower()

    for _ in range(MAX_PAGES):
        response, _calls = _get_page(http, url, headers, params, sleep)
        if response.status_code == 404:
            # The account, not the repository: a login that was renamed
            # away or an account that was deleted.
            raise GitHubError("GitHub has no such account (404); it was renamed or deleted.")
        if response.status_code != OK:
            raise GitHubError(_describe_failure(response))
        try:
            page = response.json()
        except ValueError as exc:
            raise GitHubError("A page of starred repositories was not JSON.") from exc
        if not isinstance(page, list):
            raise GitHubError("Unexpected response shape from the GitHub API.")
        for entry in page:
            full_name = entry.get("full_name") if isinstance(entry, dict) else None
            if not isinstance(full_name, str):
                raise GitHubError("A starred repository carried no usable name.")
            if full_name.lower() == target:
                return True
        next_url = response.links.get("next", {}).get("url")
        if not next_url:
            return False
        url, params = next_url, None

    raise GitHubError(
        f"Stopped after {MAX_PAGES} pages of starred repositories; "
        "this looks like a pagination loop."
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
