"""How a scenario asks for a page, and the emulation it can ask for.

Kept apart from conftest.py so the checks can name the factory's type
without importing a conftest, which pytest loads by path rather than as an
ordinary module.
"""

from typing import Literal, Protocol

from playwright.sync_api import Page, ViewportSize

ColorScheme = Literal["light", "dark", "no-preference"]
ReducedMotion = Literal["reduce", "no-preference"]


class PageFactory(Protocol):
    """Opens a page in a fresh context with the emulation a scenario needs."""

    def __call__(
        self,
        *,
        viewport: ViewportSize | None = None,
        color_scheme: ColorScheme | None = None,
        reduced_motion: ReducedMotion | None = None,
        java_script_enabled: bool | None = None,
    ) -> Page:
        """Return a new page in a context carrying these emulation settings."""
