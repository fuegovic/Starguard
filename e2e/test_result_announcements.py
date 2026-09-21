"""How a screen reader is told what happened, as the browser computes it.

The result page carries no aria-live, no tabindex and no autofocus. All
three were removed on purpose: role="alert" and role="status" already imply
an assertive and a polite live region, so an explicit aria-live restates the
role, and moving focus opens a second announcement path that competes with
the region. Their absence is the thing worth guarding, because each one is
the sort of attribute someone adds back while trying to make the message
more accessible.
"""

from playwright.sync_api import Page, expect

from e2e.browser import PageFactory
from e2e.conftest import Evidence
from e2e.harness import OUTCOME_REFUSED, OUTCOME_STARRED, LiveServer
from e2e.scenarios import scenario

# Attributes that must not appear anywhere on a result page. tabindex covers
# both the focusable spelling and tabindex="-1", which is the one a script
# would use to move focus onto the message.
REMOVED_ATTRIBUTES = ("aria-live", "tabindex", "autofocus")

_STRAY_SELECTOR = ",".join(f"[{name}]" for name in REMOVED_ATTRIBUTES)


def _stray_attributes(page: Page) -> list[str]:
    """Return the outer HTML of every element carrying a removed attribute."""
    found: list[str] = page.eval_on_selector_all(
        _STRAY_SELECTOR, "elements => elements.map(element => element.outerHTML)"
    )
    return found


@scenario("result-page-announces-failure-assertively")
def test_result_page_announces_failure_assertively(
    live_server: LiveServer,
    open_page: PageFactory,
    evidence: Evidence,
    scenario_id: str,
) -> None:
    """A failure is an alert, a success is a status, and both say which in text."""
    page = open_page()

    failure = page.goto(live_server.verification(OUTCOME_REFUSED))
    assert failure is not None
    assert failure.status == 400
    message = page.locator("p.message")
    expect(message).to_have_attribute("role", "alert")
    expect(message).to_have_class("message message-error")
    expect(message.locator(".message-label")).to_have_text("Problem:")
    # Through Playwright's role engine rather than the attribute, so this is
    # the role the browser computes and not the one the template wrote.
    expect(page.get_by_role("alert")).to_contain_text("GitHub sign-in failed")
    # A failure announced politely as well would be announced twice.
    assert page.get_by_role("status").count() == 0
    failure_strays = _stray_attributes(page)

    success = page.goto(live_server.verification(OUTCOME_STARRED))
    assert success is not None
    assert success.status == 200
    message = page.locator("p.message")
    expect(message).to_have_attribute("role", "status")
    expect(message).to_have_class("message message-success")
    expect(message.locator(".message-label")).to_have_text("Success:")
    expect(page.get_by_role("status")).to_contain_text("Authentication successful")
    # A success that interrupts is the regression the polite role prevents.
    assert page.get_by_role("alert").count() == 0
    success_strays = _stray_attributes(page)

    assert not failure_strays, f"removed attributes are back on the error page: {failure_strays}"
    assert not success_strays, f"removed attributes are back on the success page: {success_strays}"

    evidence(
        scenario_id,
        error_status=failure.status,
        error_role="alert",
        error_label="Problem:",
        success_status=success.status,
        success_role="status",
        success_label="Success:",
        guarded_attributes=", ".join(REMOVED_ATTRIBUTES),
        guarded_attributes_found=len(failure_strays) + len(success_strays),
    )
