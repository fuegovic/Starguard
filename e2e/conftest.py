"""Fixtures for the browser suite: a live server, a browser, and evidence.

Nothing here skips. A missing browser or an unservable application is a
failure, because a scenario that did not run is not evidence of anything,
and the point of this suite is that the accessibility claims about these
pages stop resting on a reading of the stylesheet.
"""

from collections.abc import Callable, Iterator

import pytest

# axe-playwright-python ships no type information and has no stub package,
# the same situation pyproject.toml records for authlib. It is imported here
# rather than in the check that uses it so the ignore lives in one place.
from axe_playwright_python.sync_playwright import Axe  # type: ignore[import-untyped]
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize, sync_playwright

from e2e.browser import ColorScheme, PageFactory, ReducedMotion
from e2e.harness import LiveServer, build_app, running

# What each scenario measured, printed after the results. A pass says a
# threshold held; the numbers say by how much, which is what makes a reflow
# or a zoom result quotable rather than merely green.
_EVIDENCE: list[tuple[str, dict[str, object]]] = []

Evidence = Callable[..., None]


@pytest.fixture(name="live_server", scope="session")
def live_server_fixture() -> Iterator[LiveServer]:
    """Serve the real application on loopback for the whole session."""
    with running(build_app()) as server:
        yield server


@pytest.fixture(name="browser", scope="session")
def browser_fixture() -> Iterator[Browser]:
    """Launch the Chromium build Playwright is pinned against."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture(name="open_page")
def open_page_fixture(browser: Browser) -> Iterator[PageFactory]:
    """Hand out pages in fresh contexts, and close them all afterwards.

    A context per page rather than one page for the session: colour scheme,
    reduced motion and script execution are context-level emulation, and a
    fresh context also starts with no cookies, which keeps one scenario's
    verification session out of the next one's.
    """
    contexts: list[BrowserContext] = []

    def open_page(
        *,
        viewport: ViewportSize | None = None,
        color_scheme: ColorScheme | None = None,
        reduced_motion: ReducedMotion | None = None,
        java_script_enabled: bool | None = None,
    ) -> Page:
        context = browser.new_context(
            viewport=viewport,
            color_scheme=color_scheme,
            reduced_motion=reduced_motion,
            java_script_enabled=java_script_enabled,
        )
        contexts.append(context)
        return context.new_page()

    yield open_page
    for context in contexts:
        context.close()


@pytest.fixture(name="axe", scope="session")
def axe_fixture() -> Axe:
    """The axe-core rule set, loaded from the installed package."""
    return Axe()


@pytest.fixture(name="evidence")
def evidence_fixture() -> Evidence:
    """Record what a scenario measured, for the summary at the end."""

    def record(scenario_id: str, **measurements: object) -> None:
        _EVIDENCE.append((scenario_id, dict(measurements)))

    return record


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Print the measurements, grouped by scenario, after the results."""
    if not _EVIDENCE:
        return
    terminalreporter.write_sep("=", "measured evidence")
    for scenario_id, measurements in _EVIDENCE:
        terminalreporter.write_line(f"@scenario:{scenario_id}")
        for key, value in measurements.items():
            terminalreporter.write_line(f"    {key}: {value}")
