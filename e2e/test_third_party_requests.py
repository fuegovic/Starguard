"""Every request these pages make, and where it goes.

This guards a privacy property that was fixed rather than designed in: the
auth page used to pull a stylesheet from Google Fonts, which told Google the
IP address of every member who followed a verification link. The content
security policy now says ``default-src 'none'`` with ``style-src 'self'``,
so a reintroduced third-party subresource would be refused by the browser
as well as caught here; both halves are checked, because a policy header is
only worth what the browser does with it.

The scope is deliberate. This is about what the pages fetch on their own,
not about where the flow navigates: a real verification does send the
visitor to github.com, and that is the visitor choosing to sign in there.
"""

from urllib.parse import urlsplit

from e2e.browser import PageFactory
from e2e.conftest import Evidence
from e2e.harness import OUTCOME_REFUSED, OUTCOME_STARRED, LiveServer
from e2e.scenarios import scenario


@scenario("no-third-party-requests-from-the-auth-page")
def test_no_third_party_requests_from_the_auth_page(
    live_server: LiveServer,
    open_page: PageFactory,
    evidence: Evidence,
    scenario_id: str,
) -> None:
    """Nothing either page loads comes from another origin."""
    page = open_page()
    requested: list[str] = []
    console: list[str] = []
    page.on("request", lambda request: requested.append(request.url))
    page.on("console", lambda message: console.append(message.text))

    for url in (
        live_server.home(),
        live_server.missing_token(),
        live_server.verification(OUTCOME_STARRED),
        live_server.verification(OUTCOME_REFUSED),
    ):
        page.goto(url)

    offsite = [url for url in requested if not url.startswith(f"{live_server.base_url}/")]
    assert not offsite, f"these pages fetched {offsite} from another origin"
    # The recorder has to have seen something, or an empty list proves only
    # that the listener was never called.
    assert len(requested) >= 4
    stylesheets = [url for url in requested if url.endswith(".css")]
    assert stylesheets, "the stylesheet request was not recorded"

    # A subresource the policy refuses never becomes a request, so it would
    # not appear above; it appears here instead.
    refusals = [line for line in console if "Content Security Policy" in line]
    assert not refusals, f"the browser refused a subresource: {refusals}"

    evidence(
        scenario_id,
        origin=live_server.base_url,
        requests_recorded=len(requested),
        paths_requested=sorted({urlsplit(url).path for url in requested}),
        third_party_requests=len(offsite),
        content_security_policy_refusals=len(refusals),
    )
