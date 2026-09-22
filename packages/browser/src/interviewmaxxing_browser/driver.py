"""Page drivers: the primitive browser operations the runtime needs.

:class:`GenericApplicationBrowser` is written against :class:`PageDriver` only, so the
same inspection, fill, navigation and confirmation logic can drive a page through
Playwright (:class:`PlaywrightDriver`) or, later, through a user-present OpenCLI
session. A driver performs exactly the operation asked; it never decides what to
fill or whether something was submitted.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Request, Response
from playwright.async_api import TimeoutError as PlaywrightTimeout


class DriverError(RuntimeError):
    """A primitive operation could not be performed."""


class NotActionable(DriverError):
    """The element could not be operated, so nothing was done to the page."""


@runtime_checkable
class PageDriver(Protocol):
    @property
    def url(self) -> str: ...

    @property
    def last_status(self) -> int | None:
        """HTTP status of the most recent main-document response, if known."""
        ...

    async def goto(self, url: str) -> int | None: ...

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        """Evaluate a JavaScript function expression in the page and return JSON."""
        ...

    async def fill(self, selector: str, text: str) -> None: ...

    async def select_values(self, selector: str, values: list[str]) -> None: ...

    async def set_checked(self, selector: str, checked: bool, *, label_selector: str | None = None) -> None: ...

    async def set_files(self, selector: str, path: Path) -> None: ...

    async def click(self, selector: str, *, trial: bool = False) -> None:
        """Click once without waiting for navigation. ``trial`` checks actionability
        only. Raises ``NotActionable`` when the element could not be clicked."""
        ...

    async def settle(self, timeout_s: float) -> None:
        """Wait (bounded) for navigation and network activity to finish."""
        ...

    async def screenshot(self, path: Path) -> None: ...

    async def html(self) -> str: ...

    async def bring_to_front(self) -> None: ...


class PlaywrightDriver:
    """``PageDriver`` over one Playwright ``Page``."""

    def __init__(
        self,
        page: Page,
        *,
        action_timeout_s: float = 10.0,
        navigation_grace_s: float = 1.0,
    ) -> None:
        self.page = page
        self._timeout_ms = action_timeout_s * 1000
        self._grace_s = navigation_grace_s
        self._last_status: int | None = None
        # Main-frame navigation requests started and documents loaded, so that
        # ``settle`` can tell "a navigation is under way" from "nothing happened".
        self._navigations = 0
        self._loads = 0
        self._mark = (0, 0)
        page.on("response", self._on_response)
        page.on("request", self._on_request)
        page.on("load", self._on_load)

    def _on_request(self, request: Request) -> None:
        try:
            if request.is_navigation_request() and request.frame == self.page.main_frame:
                self._navigations += 1
        except PlaywrightError:  # pragma: no cover - page closed mid-event
            pass

    def _on_load(self, _page: Page) -> None:
        self._loads += 1

    def _on_response(self, response: Response) -> None:
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self._last_status = response.status
        except PlaywrightError:  # pragma: no cover - page closed mid-event
            pass

    @property
    def url(self) -> str:
        return self.page.url

    @property
    def last_status(self) -> int | None:
        return self._last_status

    async def goto(self, url: str) -> int | None:
        try:
            response = await self.page.goto(url, wait_until="load", timeout=self._timeout_ms * 3)
        except PlaywrightError as exc:
            raise DriverError(f"could not open {url}: {exc}") from exc
        if response is not None:
            self._last_status = response.status
        self._mark = (self._navigations, self._loads)
        return self._last_status

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        return await self.page.evaluate(expression, arg)

    async def fill(self, selector: str, text: str) -> None:
        try:
            await self.page.locator(selector).fill(text, timeout=self._timeout_ms)
        except PlaywrightError as exc:
            raise NotActionable(f"could not type into {selector}: {exc}") from exc

    async def select_values(self, selector: str, values: list[str]) -> None:
        try:
            await self.page.locator(selector).select_option(value=values, timeout=self._timeout_ms)
        except PlaywrightError as exc:
            raise NotActionable(f"could not select {values} in {selector}: {exc}") from exc

    async def set_checked(self, selector: str, checked: bool, *, label_selector: str | None = None) -> None:
        locator = self.page.locator(selector)
        try:
            await locator.set_checked(checked, timeout=self._timeout_ms)
            return
        except PlaywrightError as exc:
            if label_selector is None:
                raise NotActionable(f"could not set {selector}: {exc}") from exc
        # Custom-styled inputs are often hidden behind their label: click the label.
        try:
            if await locator.is_checked() != checked:
                await self.page.locator(label_selector).click(timeout=self._timeout_ms)
        except PlaywrightError as exc:
            raise NotActionable(f"could not set {selector} via its label: {exc}") from exc

    async def set_files(self, selector: str, path: Path) -> None:
        try:
            await self.page.locator(selector).set_input_files(str(path), timeout=self._timeout_ms)
        except PlaywrightError as exc:
            raise NotActionable(f"could not attach a file to {selector}: {exc}") from exc

    async def click(self, selector: str, *, trial: bool = False) -> None:
        if not trial:
            self._mark = (self._navigations, self._loads)
        try:
            await self.page.locator(selector).click(
                trial=trial, no_wait_after=True, timeout=self._timeout_ms
            )
        except PlaywrightError as exc:
            raise NotActionable(f"could not click {selector}: {exc}") from exc

    async def settle(self, timeout_s: float) -> None:
        """After a click: give a navigation a short grace period to start; if one did,
        wait for the new document to load; then wait for network idle. All bounded."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        navigations, loads = self._mark
        grace = loop.time() + min(self._grace_s, timeout_s)
        while self._navigations == navigations and loop.time() < grace:
            await asyncio.sleep(0.02)
        if self._navigations > navigations:
            while self._loads == loads and loop.time() < deadline:
                await asyncio.sleep(0.02)
        self._mark = (self._navigations, self._loads)
        states: tuple[Literal["load"], Literal["networkidle"]] = ("load", "networkidle")
        for state in states:
            remaining = max(deadline - loop.time(), 0.1)
            try:
                await self.page.wait_for_load_state(state, timeout=remaining * 1000)
            except PlaywrightTimeout:
                return
            except PlaywrightError:  # pragma: no cover - navigation replaced the frame
                return

    async def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path), full_page=True, timeout=self._timeout_ms)

    async def html(self) -> str:
        return await self.page.content()

    async def bring_to_front(self) -> None:
        await self.page.bring_to_front()
