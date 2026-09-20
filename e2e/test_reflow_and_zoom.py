"""Reflow at 320 CSS pixels, and the same content at 200 percent zoom.

Both are measured rather than looked at. A screenshot can be read either
way, and the failure these guard against is a few pixels of horizontal
overflow, which is exactly what a screenshot hides and what forces a reader
to scroll sideways to finish every line.

The messages used here are the longest the pages can show. The refused one
matters most: GitHub writes that text, so neither its length nor where it
can be broken is under this project's control, and it ends in an unbroken
run of characters that only ``overflow-wrap: break-word`` keeps inside the
card.
"""

from dataclasses import dataclass

from playwright.sync_api import Page, ViewportSize, expect

from e2e.browser import PageFactory
from e2e.conftest import Evidence
from e2e.harness import OUTCOME_NOT_STARRED, OUTCOME_REFUSED, REFUSAL_REASON, LiveServer
from e2e.scenarios import scenario
from server import messages

# The narrowest viewport WCAG 1.4.10 names for reflow.
NARROW_VIEWPORT: ViewportSize = {"width": 320, "height": 640}

# 200 percent zoom on a 1280x1024 window. Layout is decided in CSS pixels,
# and browser zoom works by halving how many of them the window holds, so
# halving the viewport reproduces exactly the reflow the zoom causes. The
# other candidate, device_scale_factor=2, changes only how many device
# pixels each CSS pixel is painted with: the layout comes out identical to
# the unzoomed one, so it would measure nothing.
ZOOMED_VIEWPORT: ViewportSize = {"width": 640, "height": 512}

_LAYOUT_METRICS = """() => {
  const root = document.documentElement;
  const message = document.querySelector('p.message');
  const box = message.getBoundingClientRect();
  return {
    viewport_width: window.innerWidth,
    viewport_height: window.innerHeight,
    document_scroll_width: root.scrollWidth,
    document_client_width: root.clientWidth,
    document_scroll_height: root.scrollHeight,
    body_scroll_width: document.body.scrollWidth,
    message_left: Math.round(box.left),
    message_right: Math.round(box.right),
    message_width: Math.round(box.width),
    message_scroll_width: message.scrollWidth,
    message_client_width: message.clientWidth,
    message_scroll_height: message.scrollHeight,
    message_client_height: message.clientHeight,
    message_bottom_in_document: Math.round(box.bottom + window.scrollY)
  };
}"""


@dataclass(frozen=True)
class Sample:
    """One result page, and the text it is expected to show in full."""

    outcome: str
    status: int
    text: str


SAMPLES = (
    Sample(
        OUTCOME_REFUSED,
        400,
        messages.SIGN_IN_FAILED_REASON.format(reason=REFUSAL_REASON),
    ),
    Sample(
        OUTCOME_NOT_STARRED,
        200,
        messages.VERIFIED_NOT_STARRED.format(owner="owner", repo="repo"),
    ),
)


def _metrics(page: Page) -> dict[str, int]:
    """Measure the document and the message box as the browser laid them out."""
    measured: dict[str, int] = page.evaluate(_LAYOUT_METRICS)
    return measured


def _overflow_report(sample: Sample, measured: dict[str, int], overflow: int) -> str:
    """Say what overflowed and by how much, so the failure needs no rerun."""
    return (
        f"the {sample.outcome} result page overflows a "
        f"{measured['document_client_width']}px viewport by {overflow}px: the message box is "
        f"{measured['message_width']}px wide and spans {measured['message_left']}"
        f"..{measured['message_right']}. Its longest unbroken run of characters is "
        f"{max(len(word) for word in sample.text.split())} characters, and "
        "`overflow-wrap: break-word` does not shrink an element's min-content size, so a "
        "shrink-to-fit flex item never narrows below that run"
    )


def _load(page: Page, live_server: LiveServer, sample: Sample) -> dict[str, int]:
    """Open one result page, confirm its text arrived whole, and measure it."""
    response = page.goto(live_server.verification(sample.outcome))
    assert response is not None
    assert response.status == sample.status
    message = page.locator("p.message")
    expect(message).to_be_visible()
    # The full string, not a prefix: a message the layout truncated would
    # still pass a "contains the first few words" check.
    assert sample.text in " ".join(message.inner_text().split())
    return _metrics(page)


@scenario("long-message-reflows-at-320px")
def test_long_message_reflows_at_320px(
    live_server: LiveServer,
    open_page: PageFactory,
    evidence: Evidence,
    scenario_id: str,
) -> None:
    """At 320 CSS pixels wide, no result page scrolls sideways."""
    page = open_page(viewport=NARROW_VIEWPORT)
    # Every message is measured before anything fails, so one run says which
    # of them reflow and which do not rather than stopping at the first.
    failures: list[str] = []

    for sample in SAMPLES:
        measured = _load(page, live_server, sample)
        overflow = measured["document_scroll_width"] - measured["document_client_width"]
        evidence(
            scenario_id,
            outcome=sample.outcome,
            viewport=f"{measured['viewport_width']}x{measured['viewport_height']}",
            document_scroll_width=measured["document_scroll_width"],
            document_client_width=measured["document_client_width"],
            horizontal_overflow_px=overflow,
            message_width=measured["message_width"],
            message_box=f"{measured['message_left']}..{measured['message_right']}",
            longest_unbroken_token=max(len(word) for word in sample.text.split()),
        )
        if overflow > 0:
            failures.append(_overflow_report(sample, measured, overflow))
        # The card itself has to fit too. A document that does not scroll
        # sideways only because its overflowing child is clipped is not a
        # page that reflowed.
        if measured["message_left"] < 0 or measured["message_right"] > measured["viewport_width"]:
            failures.append(
                f"the {sample.outcome} message card spans {measured['message_left']}"
                f"..{measured['message_right']} in a {measured['viewport_width']}px viewport"
            )
        if measured["message_scroll_width"] > measured["message_client_width"]:
            failures.append(f"the {sample.outcome} message text is clipped inside its own card")

    assert not failures, "\n".join(failures)


@scenario("content-survives-200-percent-zoom")
def test_content_survives_200_percent_zoom(
    live_server: LiveServer,
    open_page: PageFactory,
    evidence: Evidence,
    scenario_id: str,
) -> None:
    """At 200 percent zoom the message is whole, unclipped and reachable."""
    page = open_page(viewport=ZOOMED_VIEWPORT)
    failures: list[str] = []

    for sample in SAMPLES:
        measured = _load(page, live_server, sample)
        overflow = measured["document_scroll_width"] - measured["document_client_width"]
        clipped = max(
            measured["message_scroll_height"] - measured["message_client_height"],
            measured["message_scroll_width"] - measured["message_client_width"],
        )
        evidence(
            scenario_id,
            outcome=sample.outcome,
            emulated_as="1280x1024 at 200%, measured as a 640x512 CSS-pixel viewport",
            viewport=f"{measured['viewport_width']}x{measured['viewport_height']}",
            document_scroll_width=measured["document_scroll_width"],
            document_client_width=measured["document_client_width"],
            horizontal_overflow_px=overflow,
            message_clipped_px=clipped,
            message_bottom_in_document=measured["message_bottom_in_document"],
            document_scroll_height=measured["document_scroll_height"],
        )
        if overflow > 0:
            failures.append(_overflow_report(sample, measured, overflow))
        # Not clipped by its own box in either direction.
        if clipped > 0:
            failures.append(f"the {sample.outcome} message is clipped by {clipped}px at 200%")
        # Vertical scrolling is allowed at this size; content the document
        # cannot scroll far enough to reach is not.
        if measured["message_bottom_in_document"] > measured["document_scroll_height"]:
            failures.append(
                f"the {sample.outcome} message ends at "
                f"{measured['message_bottom_in_document']}px, past the "
                f"{measured['document_scroll_height']}px the document can scroll to"
            )

    assert not failures, "\n".join(failures)
