"""Both pages with script execution switched off in the browser.

The templates load no script, and the content security policy is
``default-src 'none'`` with no script-src, so nothing about these pages is
supposed to need JavaScript. That is easy to assert about the markup and
harder to be sure of about the rendered page, which is what this checks:
the context has script execution disabled at the browser, not merely a
document that happens to link no script file.
"""

from playwright.sync_api import expect

from e2e.browser import PageFactory
from e2e.conftest import Evidence
from e2e.harness import OUTCOME_NOT_STARRED, LiveServer
from e2e.scenarios import scenario

# Loaded in the same context to prove the flag took effect. Without it a
# passing run would be indistinguishable from one where the emulation was
# quietly ignored and the pages simply have no script of their own.
SCRIPT_PROOF_URL = (
    "data:text/html,<p id='probe'>script-did-not-run</p>"
    "<script>document.getElementById('probe').textContent = 'script-ran'</script>"
)


@scenario("pages-render-without-javascript")
def test_pages_render_without_javascript(
    live_server: LiveServer,
    open_page: PageFactory,
    evidence: Evidence,
    scenario_id: str,
) -> None:
    """Heading and message still render with JavaScript disabled."""
    page = open_page(java_script_enabled=False)

    page.goto(SCRIPT_PROOF_URL)
    proof = page.locator("#probe").inner_text()
    assert proof == "script-did-not-run"

    home = page.goto(live_server.home())
    assert home is not None
    assert home.status == 200
    expect(page.locator("h1")).to_have_text("StarGuard")
    expect(page.locator("main p")).to_contain_text("Nothing to see here")

    result = page.goto(live_server.verification(OUTCOME_NOT_STARRED))
    assert result is not None
    assert result.status == 200
    expect(page.locator("h1")).to_have_text("GitHub Verification")
    expect(page.locator("p.message")).to_contain_text("you have not starred owner/repo yet")
    # The whole verification, including the redirect the fake GitHub sends,
    # completed without a script: a flow that had come to depend on one
    # would have stopped at /login.
    assert page.url.endswith("/authorize?outcome=" + OUTCOME_NOT_STARRED)

    script_count = page.locator("script").count()
    assert script_count == 0

    evidence(
        scenario_id,
        script_execution=proof,
        home_status=home.status,
        result_status=result.status,
        script_elements_on_result_page=script_count,
    )
