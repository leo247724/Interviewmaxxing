"""Playwright sessions: ``BrowserSessionFactory`` and the browser it returns."""

from __future__ import annotations

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from interviewmaxxing_core import BrowserOptions

from .driver import PlaywrightDriver
from .runtime import ActionPolicy, GenericApplicationBrowser


class PlaywrightApplicationBrowser(GenericApplicationBrowser):
    """``ApplicationBrowser`` driving one Chromium page through Playwright."""

    def __init__(
        self,
        playwright: Playwright,
        browser: Browser | None,
        context: BrowserContext,
        page: Page,
        options: BrowserOptions,
        *,
        action_timeout_s: float,
        settle_timeout_s: float,
        policy: ActionPolicy | None = None,
    ) -> None:
        super().__init__(
            PlaywrightDriver(page, action_timeout_s=action_timeout_s),
            options,
            settle_timeout_s=settle_timeout_s,
            policy=policy,
        )
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._page = page
        self._closed = False

    @property
    def page(self) -> Page:
        """The live Playwright page (tests and diagnostics; the runner does not need it)."""
        return self._page

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._context.close()
            if self._browser is not None:
                await self._browser.close()
        finally:
            await self._playwright.stop()


class PlaywrightSessionFactory:
    """``BrowserSessionFactory`` launching Chromium.

    With ``BrowserOptions.profile_dir`` the session uses a persistent profile, so a
    sign-in the user completes once is kept for later runs (one process at a time
    per profile). ``headless=False`` (the default) shows the window so the user can
    sign in or solve a CAPTCHA when the runtime asks."""

    def __init__(
        self,
        *,
        action_timeout_s: float = 10.0,
        settle_timeout_s: float = 15.0,
        policy: ActionPolicy | None = None,
    ) -> None:
        self.action_timeout_s = action_timeout_s
        self.settle_timeout_s = settle_timeout_s
        self.policy = policy

    async def start(self, options: BrowserOptions) -> PlaywrightApplicationBrowser:
        playwright = await async_playwright().start()
        browser: Browser | None = None
        try:
            if options.profile_dir is not None:
                options.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                context = await playwright.chromium.launch_persistent_context(
                    str(options.profile_dir),
                    headless=options.headless,
                    slow_mo=options.slow_mo_ms,
                    accept_downloads=False,
                )
                page = context.pages[0] if context.pages else await context.new_page()
            else:
                browser = await playwright.chromium.launch(
                    headless=options.headless, slow_mo=options.slow_mo_ms
                )
                context = await browser.new_context(accept_downloads=False)
                page = await context.new_page()
        except BaseException:
            if browser is not None:
                await browser.close()
            await playwright.stop()
            raise
        return PlaywrightApplicationBrowser(
            playwright,
            browser,
            context,
            page,
            options,
            action_timeout_s=self.action_timeout_s,
            settle_timeout_s=self.settle_timeout_s,
            policy=self.policy,
        )
