"""Minimal GitHub REST helpers.

Only the stargazer listing lives here. It is kept free of Discord and database
concerns so the pagination and error handling can be tested directly.
"""

import logging

import requests

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
PER_PAGE = 100
REQUEST_TIMEOUT = 30

# A repository with more than 100k stargazers would exceed this; the cap only
# exists so a malformed Link header cannot spin forever.
MAX_PAGES = 1000


class GitHubError(RuntimeError):
    """Raised when the stargazer listing could not be retrieved in full."""


def _headers(token=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _describe_failure(response):
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


def _extract_login(entry):
    """Return the login from a stargazer entry.

    The plain listing returns user objects; the ``star+json`` media type wraps
    them in ``{"starred_at": ..., "user": {...}}``. Both are accepted so the
    helper keeps working if the Accept header is ever changed back.
    """
    if not isinstance(entry, dict):
        return None
    user = entry.get("user")
    if isinstance(user, dict):
        return user.get("login")
    return entry.get("login")


def fetch_stargazer_logins(owner, repo, token=None, session=None):
    """Return the set of lower-cased logins that have starred ``owner/repo``.

    Logins are lower-cased because GitHub treats usernames case-insensitively,
    and a set is returned so membership tests stay constant time no matter how
    many stargazers the repository has.

    Raises :class:`GitHubError` rather than returning a partial set: acting on
    an incomplete listing would strip roles from people who never un-starred.
    """
    http = session or requests
    url = f"{API_ROOT}/repos/{owner}/{repo}/stargazers"
    params = {"per_page": PER_PAGE}
    headers = _headers(token)
    logins = set()

    for _ in range(MAX_PAGES):
        try:
            response = http.get(
                url, headers=headers, params=params, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            raise GitHubError(f"Could not reach the GitHub API: {exc}") from exc

        if response.status_code != 200:
            raise GitHubError(_describe_failure(response))

        page = response.json()
        if not isinstance(page, list):
            raise GitHubError("Unexpected response shape from the GitHub API.")

        for entry in page:
            login = _extract_login(entry)
            if login:
                logins.add(login.lower())

        # requests parses the RFC 5988 Link header for us, which avoids the
        # hand-rolled string splitting this used to do.
        next_url = response.links.get("next", {}).get("url")
        if not next_url:
            return logins

        # The next URL already carries per_page and page.
        url, params = next_url, None

    raise GitHubError(
        f"Stopped after {MAX_PAGES} pages of stargazers; this looks like a "
        "pagination loop."
    )
