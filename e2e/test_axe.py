"""axe-core over both pages, in both of the result page's two states.

No violation at serious or critical impact is allowed. Anything axe reports
below that is printed as part of the run's evidence rather than filtered
out of the results: a suppressed finding is one nobody looks at again.
"""

from typing import Any

from axe_playwright_python.sync_playwright import Axe  # type: ignore[import-untyped]

from e2e.browser import PageFactory
from e2e.conftest import Evidence
from e2e.harness import OUTCOME_REFUSED, OUTCOME_STARRED, LiveServer
from e2e.scenarios import scenario

BLOCKING_IMPACTS = ("serious", "critical")


def _summarise(violations: list[dict[str, Any]]) -> list[str]:
    """Render one line per violation: rule, impact and how many nodes."""
    return [
        f"{violation['id']} ({violation['impact']}) x{len(violation['nodes'])}"
        for violation in violations
    ]


@scenario("axe-finds-no-serious-violations")
def test_axe_finds_no_serious_violations(
    live_server: LiveServer,
    open_page: PageFactory,
    axe: Axe,
    evidence: Evidence,
    scenario_id: str,
) -> None:
    """Neither page has a serious or critical axe violation."""
    page = open_page()
    pages = {
        "home": live_server.home(),
        "result-error": live_server.verification(OUTCOME_REFUSED),
        "result-success": live_server.verification(OUTCOME_STARRED),
    }

    # Every page is checked before anything fails, so one run reports the
    # whole surface rather than the first page that happens to break.
    reports: list[str] = []
    for name, url in pages.items():
        page.goto(url)
        results = axe.run(page)
        violations: list[dict[str, Any]] = results.response["violations"]
        serious = [v for v in violations if v["impact"] in BLOCKING_IMPACTS]
        if serious:
            reports.append(f"{name}:\n{results.generate_report()}")
        evidence(
            scenario_id,
            page=name,
            axe_version=results.response["testEngine"]["version"],
            rules_checked=len(results.response["passes"]) + len(violations),
            violations_total=len(violations),
            violations_at_serious_or_critical=len(serious),
            all_violations=_summarise(violations) or "none",
        )

    assert not reports, "\n\n".join(reports)
