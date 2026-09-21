"""The decorative layer's animation, and which set of colour tokens wins.

Both are read back from ``getComputedStyle`` and from the browser's own
animation timeline, never from the stylesheet text: a rule that is present
but overridden, or a media query that never matches, looks identical to a
working one when you only read the file.
"""

from playwright.sync_api import Page

from e2e.browser import PageFactory
from e2e.conftest import Evidence
from e2e.harness import LiveServer
from e2e.scenarios import scenario

# The tokens declared on :root, and the ones the light media query swaps in.
DARK_BACKGROUND = "rgb(43, 43, 43)"
DARK_TEXT = "rgb(245, 245, 245)"
LIGHT_BACKGROUND = "rgb(244, 244, 245)"
LIGHT_TEXT = "rgb(28, 28, 30)"

_ANIMATION_STATE = """() => {
  const layer = document.querySelector('.background');
  const style = getComputedStyle(layer);
  return {
    animation_name: style.animationName,
    animation_play_state: style.animationPlayState,
    animation_duration: style.animationDuration,
    running_animations: document.getAnimations().length
  };
}"""

_THEME_STATE = """() => {
  const body = getComputedStyle(document.body);
  const layer = getComputedStyle(document.querySelector('.background'));
  return {
    body_background: body.backgroundColor,
    body_text: body.color,
    layer_background: layer.backgroundColor,
    declared_color_scheme: getComputedStyle(document.documentElement).colorScheme,
    prefers_light: matchMedia('(prefers-color-scheme: light)').matches,
    prefers_dark: matchMedia('(prefers-color-scheme: dark)').matches
  };
}"""


def _animation_state(page: Page) -> dict[str, object]:
    """Read back what the decorative layer is actually doing."""
    state: dict[str, object] = page.evaluate(_ANIMATION_STATE)
    return state


def _theme_state(page: Page) -> dict[str, object]:
    """Read back the colours the page resolved to."""
    state: dict[str, object] = page.evaluate(_THEME_STATE)
    return state


@scenario("decorative-motion-stops-under-reduced-motion")
def test_decorative_motion_stops_under_reduced_motion(
    live_server: LiveServer,
    open_page: PageFactory,
    evidence: Evidence,
    scenario_id: str,
) -> None:
    """The drifting gradient has no animation at all under reduce."""
    moving = open_page(reduced_motion="no-preference")
    moving.goto(live_server.home())
    while_allowed = _animation_state(moving)

    still = open_page(reduced_motion="reduce")
    still.goto(live_server.home())
    while_reduced = _animation_state(still)

    # The control first. Without it, a stylesheet that had lost the
    # animation entirely would pass the assertion below and prove nothing.
    assert while_allowed["animation_name"] == "drift"
    assert while_allowed["running_animations"] == 1

    assert while_reduced["animation_name"] == "none"
    assert while_reduced["animation_duration"] == "0s"
    # animation-play-state stays "running" even when animation-name is
    # none, so it is not on its own a usable signal; the count of live
    # animations on the timeline is, and it is what a repaint follows.
    assert while_reduced["running_animations"] == 0

    evidence(
        scenario_id,
        with_motion_allowed=while_allowed,
        with_motion_reduced=while_reduced,
    )


@scenario("dark-is-default-light-is-honoured")
def test_dark_is_default_light_is_honoured(
    live_server: LiveServer,
    open_page: PageFactory,
    evidence: Evidence,
    scenario_id: str,
) -> None:
    """Dark tokens for a dark client, light tokens for a light one."""
    dark = open_page(color_scheme="dark")
    dark.goto(live_server.home())
    in_dark = _theme_state(dark)

    light = open_page(color_scheme="light")
    light.goto(live_server.home())
    in_light = _theme_state(light)

    unstated = open_page(color_scheme="no-preference")
    unstated.goto(live_server.home())
    in_unstated = _theme_state(unstated)

    assert in_dark["body_background"] == DARK_BACKGROUND
    assert in_dark["body_text"] == DARK_TEXT
    assert in_dark["layer_background"] == DARK_BACKGROUND

    assert in_light["body_background"] == LIGHT_BACKGROUND
    assert in_light["body_text"] == LIGHT_TEXT
    assert in_light["layer_background"] == LIGHT_BACKGROUND

    # Dark is the default in the cascade, and that is as far as the claim
    # goes. prefers-color-scheme has no third value: a client that states no
    # preference matches the light query, so it is served the light tokens,
    # not the ones on :root. What the page does declare for a client with no
    # preference is `color-scheme: dark light`, which puts dark first and so
    # decides the colours the browser itself paints (scrollbars, form
    # controls, the canvas before the stylesheet arrives). Asserted rather
    # than described, because reading the file suggests the opposite.
    assert in_unstated["prefers_light"] is True
    assert in_unstated["prefers_dark"] is False
    assert in_unstated["body_background"] == LIGHT_BACKGROUND
    assert in_dark["declared_color_scheme"] == "dark light"

    evidence(
        scenario_id,
        prefers_dark=in_dark,
        prefers_light=in_light,
        no_preference=in_unstated,
    )
