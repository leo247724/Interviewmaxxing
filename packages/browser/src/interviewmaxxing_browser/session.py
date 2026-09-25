"""Playwright sessions: ``BrowserSessionFactory`` and the browser it returns."""

from __future__ import annotations

import re

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from interviewmaxxing_core import BrowserOptions

from .annotations import FormAnnotator, SchemaHintLoader
from .driver import PlaywrightDriver
from .runtime import ActionPolicy, GenericApplicationBrowser

LINUX_PLATFORM = "(X11; Linux x86_64)"


def linux_user_agent(user_agent: str) -> str:
    """The same browser's user agent with a Linux desktop platform segment.

    Widget libraries such as react-select leave out ``aria-selected`` and
    ``aria-activedescendant`` when the user agent names an Apple platform (a VoiceOver
    workaround); the runtime reads those back to verify a choice. The first
    parenthesized platform segment changes and the ``HeadlessChrome`` product token
    becomes ``Chrome`` (headless Chromium is otherwise refused by common bot filters);
    the version stays the browser's own."""
    agent = re.sub(r"\([^)]*\)", LINUX_PLATFORM, user_agent, count=1)
    return agent.replace("HeadlessChrome/", "Chrome/")


BLOCKED_HOSTS: tuple[str, ...] = ("linkedin.com", "licdn.com", "applywithlinkedin.myworkdaygadgets.com")
"""Hosts an application session never contacts (the owner's rule of 2026-09-24: no
LinkedIn traffic). A Workday tenant's "Start Your Application" dialog embeds an "Apply
with LinkedIn" gadget that posts to www.linkedin.com as soon as the dialog shows."""


def blocked_request_pattern(hosts: tuple[str, ...]) -> re.Pattern[str]:
    """A URL pattern matching requests to ``hosts`` or any of their subdomains."""
    names = "|".join(re.escape(host) for host in hosts)
    return re.compile(rf"^[a-z][a-z0-9+.-]*://(?:[^/?#@]*@)?(?:[^/?#:]*\.)?(?:{names})\.?(?::\d+)?(?:[/?#]|$)",
                      re.IGNORECASE)


async def _block_hosts(context: BrowserContext, hosts: tuple[str, ...]) -> None:
    """Abort requests, and close WebSockets without connecting, to ``hosts``."""
    if hosts:
        pattern = blocked_request_pattern(hosts)
        await context.route(pattern, lambda route: route.abort("blockedbyclient"))
        await context.route_web_socket(pattern, lambda socket: socket.close())


_HEADLESS_AGENTS: dict[str, str] = {}
"""Browser executable -> its headless user agent with a Linux platform (per process)."""


async def _headless_user_agent(playwright: Playwright, browser: Browser | None) -> str:
    key = playwright.chromium.executable_path
    if key not in _HEADLESS_AGENTS:
        probe = browser or await playwright.chromium.launch(headless=True)
        try:
            page = await probe.new_page()
            try:
                agent = str(await page.evaluate("() => navigator.userAgent"))
            finally:
                await page.close()
        finally:
            if browser is None:
                await probe.close()
        _HEADLESS_AGENTS[key] = linux_user_agent(agent)
    return _HEADLESS_AGENTS[key]


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
        annotator: FormAnnotator | None = None,
        schema_hint_loader: SchemaHintLoader | None = None,
    ) -> None:
        super().__init__(
            PlaywrightDriver(page, action_timeout_s=action_timeout_s),
            options,
            settle_timeout_s=settle_timeout_s,
            policy=policy,
            annotator=annotator,
            schema_hint_loader=schema_hint_loader,
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
    sign in or solve a CAPTCHA when the runtime asks.

    Headless sessions present the browser's own user agent with a Linux desktop
    platform segment (see ``linux_user_agent``), so menu widgets expose the selection
    state the runtime reads back; visible sessions keep the platform's user agent.
    ``user_agent`` sets one explicitly for every session (tests, diagnostics).

    Requests to ``blocked_hosts`` (default ``BLOCKED_HOSTS``: LinkedIn) are aborted in
    every session, so opening a posting never contacts them."""

    def __init__(
        self,
        *,
        action_timeout_s: float = 10.0,
        settle_timeout_s: float = 15.0,
        policy: ActionPolicy | None = None,
        annotator: FormAnnotator | None = None,
        schema_hint_loader: SchemaHintLoader | None = None,
        user_agent: str | None = None,
        blocked_hosts: tuple[str, ...] = BLOCKED_HOSTS,
    ) -> None:
        self.action_timeout_s = action_timeout_s
        self.settle_timeout_s = settle_timeout_s
        self.policy = policy
        self.annotator = annotator
        self.schema_hint_loader = schema_hint_loader
        self.user_agent = user_agent
        self.blocked_hosts = blocked_hosts

    async def start(self, options: BrowserOptions) -> PlaywrightApplicationBrowser:
        playwright = await async_playwright().start()
        browser: Browser | None = None
        try:
            if options.profile_dir is not None:
                options.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                agent = self.user_agent or (
                    await _headless_user_agent(playwright, None) if options.headless else None)
                context = await playwright.chromium.launch_persistent_context(
                    str(options.profile_dir),
                    headless=options.headless,
                    slow_mo=options.slow_mo_ms,
                    accept_downloads=False,
                    user_agent=agent,
                )
                await _block_hosts(context, self.blocked_hosts)
                page = context.pages[0] if context.pages else await context.new_page()
            else:
                browser = await playwright.chromium.launch(
                    headless=options.headless, slow_mo=options.slow_mo_ms
                )
                agent = self.user_agent or (
                    await _headless_user_agent(playwright, browser) if options.headless else None)
                context = await browser.new_context(accept_downloads=False, user_agent=agent)
                await _block_hosts(context, self.blocked_hosts)
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
            annotator=self.annotator,
            schema_hint_loader=self.schema_hint_loader,
        )
