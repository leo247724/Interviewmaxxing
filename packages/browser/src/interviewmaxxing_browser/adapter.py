"""The generic ``ATSAdapter``: native, accessible HTML forms on any site.

ATS-specific adapters (for the user's first real ATS) will follow the same
protocol and may reuse the generic runtime for everything they do not override.
"""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from interviewmaxxing_core import (
    ApplicationForm,
    ApplicationPacket,
    BrowserOptions,
    FillResult,
    NavigationResult,
    PageInspection,
    SubmissionObservation,
    SubmitActionResult,
)

from .driver import PageDriver, PlaywrightDriver
from .runtime import GenericApplicationBrowser


class GenericAdapter:
    """``interviewmaxxing_core.ATSAdapter`` for any page with native controls.

    ``page`` may be a Playwright ``Page`` or any :class:`PageDriver`. One runtime is
    kept per page so submission guards and step counting persist across calls.
    """

    name = "generic"

    def __init__(self, options: BrowserOptions) -> None:
        self.options = options
        self._runtimes: dict[int, GenericApplicationBrowser] = {}

    def runtime(self, page: Any) -> GenericApplicationBrowser:
        key = id(page)
        if key not in self._runtimes:
            if isinstance(page, Page):
                driver: PageDriver = PlaywrightDriver(page)
            elif isinstance(page, PageDriver):
                driver = page
            else:
                raise TypeError("page must be a Playwright Page or a PageDriver")
            self._runtimes[key] = GenericApplicationBrowser(driver, self.options)
        return self._runtimes[key]

    async def detect(self, page: Any) -> bool:
        """The generic adapter handles any page; specific adapters take precedence."""
        return True

    async def inspect(self, page: Any) -> PageInspection:
        return await self.runtime(page).inspect()

    async def fill(self, page: Any, form: ApplicationForm, packet: ApplicationPacket) -> FillResult:
        return await self.runtime(page).fill(form, packet)

    async def next(self, page: Any) -> NavigationResult:
        return await self.runtime(page).advance()

    async def submit(self, page: Any) -> SubmitActionResult:
        return await self.runtime(page).submit()

    async def detect_submission(self, page: Any) -> SubmissionObservation:
        return await self.runtime(page).confirm()
