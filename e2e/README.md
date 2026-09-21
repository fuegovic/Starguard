# Browser checks for the OAuth pages

Eight scenarios, run against a real Chromium, over the two pages the OAuth
server renders. They exist because every accessibility claim made about
these pages was argued from the stylesheet: this is where the arguments are
replaced by measurements.

```
pytest e2e -v
```

`pytest` with no arguments never collects this directory. `testpaths` in
`pyproject.toml` names `tests`, and this suite needs a browser that the unit
suite does not.

## What runs

The real `create_app` factory, the real views and the real templates, served
on a free loopback port. Only GitHub and MongoDB are replaced, both at the
edge: `e2e/harness.py` swaps in an OAuth client that answers from the query
string and a mongomock collection. Nothing in `app.config` is changed, so
the security headers, the session cookie flags and the status codes are the
ones production sends. A whole verification really does travel `/login`, a
redirect and `/authorize`.

## The scenarios

Each check carries its id in its reported name, as `@scenario:<id>`, so a
run's output can be quoted as the record of what was observed:

| id | what it measures |
| --- | --- |
| `result-page-announces-failure-assertively` | the roles the browser computes, and that aria-live, tabindex and autofocus are still absent |
| `pages-render-without-javascript` | both pages in a context with script execution disabled |
| `long-message-reflows-at-320px` | no horizontal document scroll at 320 CSS pixels |
| `content-survives-200-percent-zoom` | the message whole, unclipped and reachable at 200 percent |
| `decorative-motion-stops-under-reduced-motion` | no live animation under `prefers-reduced-motion: reduce` |
| `dark-is-default-light-is-honoured` | the colour tokens each `prefers-color-scheme` resolves to |
| `no-third-party-requests-from-the-auth-page` | every request either page makes is same-origin |
| `axe-finds-no-serious-violations` | axe-core over both pages, nothing serious or critical |

A run prints a `measured evidence` section after the results with the
numbers behind each one: the overflow in pixels, the box the message
occupied, the colours resolved, the axe rule counts.

## The browser

Playwright is pinned in `requirements-dev.txt` to the release whose bundled
Chromium revision is already in the local browser cache, because
`playwright install` reaches a CDN that is not always available. The pin and
the cache have to agree; `packages/playwright-core/browsers.json` in the
Playwright repository, at the tag for a version, says which revision that
version wants. Nothing here skips when the browser is missing: a scenario
that did not run is not evidence of anything.
