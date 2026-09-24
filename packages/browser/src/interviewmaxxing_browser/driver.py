"""Page drivers: the primitive browser operations the runtime needs.

:class:`GenericApplicationBrowser` is written against :class:`PageDriver` only, so the
same inspection, fill, navigation and confirmation logic can drive a page through
Playwright (:class:`PlaywrightDriver`) or, later, through a user-present OpenCLI
session. A driver performs exactly the operation asked; it never decides what to
fill or whether something was submitted.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Request, Response
from playwright.async_api import TimeoutError as PlaywrightTimeout


class DriverError(RuntimeError):
    """A primitive operation could not be performed."""


class NotActionable(DriverError):
    """The element could not be operated, so nothing was done to the page."""


class PageContextLost(DriverError):
    """The document was replaced (navigation, origin change, context destroyed) while
    operating a control. Nothing further may be written until the page is inspected
    again; the runtime aborts the rest of the fill."""


class CapabilityUnsupported(NotActionable):
    """This driver cannot perform the operation here; nothing was changed. The message
    says what the user can do in the visible browser instead."""


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

    async def select_accessible(
        self, selector: str, values: list[str], binding: dict[str, Any],
        *, before_action: Callable[[], Awaitable[None]] | None = None,
    ) -> list[str]: ...

    async def set_checked(self, selector: str, checked: bool, *, label_selector: str | None = None) -> None: ...

    async def set_files(self, selector: str, path: Path) -> None: ...

    async def click(self, selector: str, *, trial: bool = False) -> None:
        """Click once without waiting for navigation. ``trial`` checks actionability
        only. Raises ``NotActionable`` when the element could not be clicked."""
        ...

    async def focus(self, selector: str) -> None:
        """Focus the element without clicking or typing."""
        ...

    async def press(self, selector: str, key: str) -> None:
        """Focus the element and press one key (``ArrowDown``, ``Escape``). Raises
        ``CapabilityUnsupported`` when this session cannot press keys."""
        ...

    async def type_text(self, selector: str, text: str, *, delay_s: float = 0.03) -> None:
        """Type ``text`` key by key into the element (appending; it is not cleared)."""
        ...

    async def clear_text(self, selector: str) -> None:
        """Empty a text input the way a person does (select all, delete) and verify it
        is empty; raises ``NotActionable`` otherwise."""
        ...

    async def scroll_to_end(self, selector: str) -> None:
        """Scroll a list (or its nearest scrollable ancestor) to its end."""
        ...

    async def dismiss(self) -> None:
        """Press outside every control, as a person clicks an empty part of the page to
        close a popover menu: pointer and mouse press events on the page body, never on
        a control. Raises ``CapabilityUnsupported`` when this session cannot."""
        ...

    async def settle(self, timeout_s: float) -> None:
        """Wait (bounded) for navigation and network activity to finish."""
        ...

    async def screenshot(self, path: Path) -> None: ...

    async def html(self) -> str: ...

    async def bring_to_front(self) -> None: ...


_CONTEXT_LOST = re.compile(
    r"execution context was destroyed|frame was detached|navigation|target closed|"
    r"page closed|context was destroyed",
    re.IGNORECASE,
)

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def reject_control_characters(text: str, selector: str) -> None:
    """Refuse to type control characters key by key.

    ``type_text`` presses every character as a key. A newline is the Enter key: pressed
    inside an application form it submits or advances the form (implicit submission),
    outside every submission guard. Tabs move the focus, other control keys are never a
    value. Values reach the drivers through the packet contract, which rejects them too;
    this is the last line of defence. Raises ``NotActionable``."""
    match = _CONTROL_CHARACTERS.search(text)
    if match is not None:
        raise NotActionable(
            f"refusing to type the control character U+{ord(match.group(0)):04X} into "
            f"{selector}: a newline or control key inside a form is an action, not a value")



# Read-only SHA-256 of the first file attached to a control (null when the page cannot
# hash, e.g. no crypto.subtle in an insecure context).
_FILE_DIGEST = (
    "async (sel) => { const el = document.querySelector(sel); if (!el || !el.files || !el.files.length) "
    "return null; if (!(globalThis.crypto && globalThis.crypto.subtle)) return null; const buf = await el.files[0].arrayBuffer(); "
    "const d = await crypto.subtle.digest('SHA-256', buf); "
    "return {origin: String(performance.timeOrigin), url: location.href, sha256: Array.from(new Uint8Array(d))"
    ".map((b) => b.toString(16).padStart(2, '0')).join('')}; }"
)

# Read-only: a selector for a file input's own uploader, the outermost ancestor holding no
# other field (preferring a labelled group or an id, which survive a re-render). Taken
# before attaching: some uploaders replace the input with the file's name (Greenhouse).
FILE_ANCHOR = (
    "(sel) => { const el = document.querySelector(sel); if (!el) return null; "
    "const fields = 'input:not([type=hidden]),select,textarea,[role=combobox],[role=textbox]'; "
    "const unique = (s) => { try { return document.querySelectorAll(s).length === 1; } catch (e) { return false; } }; "
    "let box = null, named = null; "
    "for (let n = el.parentElement, d = 0; n && d < 8 && n !== document.body && n.tagName !== 'FORM'; "
    "n = n.parentElement, d++) { if ([...n.querySelectorAll(fields)].some((f) => f !== el)) break; box = n; "
    "const by = n.getAttribute('aria-labelledby'); "
    "if (!named && n.id && unique('#' + CSS.escape(n.id))) named = '#' + CSS.escape(n.id); "
    "else if (!named && by && unique('[aria-labelledby=\"' + by.replace(/\"/g, '') + '\"]')) "
    "named = '[aria-labelledby=\"' + by.replace(/\"/g, '') + '\"]'; } "
    "if (named) return named; if (!box) return null; const parts = []; "
    "for (let n = box; n && n !== document.documentElement; n = n.parentElement) { "
    "if (n !== box && n.id && unique('#' + CSS.escape(n.id))) { parts.unshift('#' + CSS.escape(n.id)); break; } "
    "const p = n.parentElement; if (!p) { parts.unshift(n.tagName.toLowerCase()); break; } "
    "const same = [...p.children].filter((c) => c.tagName === n.tagName); "
    "parts.unshift(n.tagName.toLowerCase() + (same.length > 1 ? ':nth-of-type(' + (same.indexOf(n) + 1) + ')' : '')); } "
    "const s = parts.join(' > '); return unique(s) ? s : null; }"
)

# Read-only: whether a file input's uploader shows the file's name and no error alert or
# progress: the input's own container (see FILE_ANCHOR), or the anchor when the input is
# gone. Also the number of files the input itself holds (null without the input).
FILE_SHOWN = (
    "(arg) => { const el = document.querySelector(arg.selector); "
    "const norm = (t) => String(t || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim().toLowerCase(); "
    "const fields = 'input:not([type=hidden]),select,textarea,[role=combobox],[role=textbox]'; let box = null; "
    "if (el) { for (let n = el.parentElement, d = 0; n && d < 8 && n !== document.body && n.tagName !== 'FORM'; "
    "n = n.parentElement, d++) { if ([...n.querySelectorAll(fields)].some((f) => f !== el)) break; box = n; } } "
    "if (!box && arg.anchor) { const found = document.querySelectorAll(arg.anchor); if (found.length === 1) box = found[0]; } "
    "if (!box) return {shown: false, alert: false, busy: false, files: el && el.files ? el.files.length : null}; "
    "const seen = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); "
    "return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; }; "
    "return {origin: String(performance.timeOrigin), url: location.href, "
    "files: el && el.files ? el.files.length : null, "
    "shown: !!arg.name && norm(box.innerText).includes(norm(arg.name)), "
    "busy: [...box.querySelectorAll('[role=progressbar]')].some(seen), "
    "alert: [...box.querySelectorAll('[role=alert],[aria-invalid=true]')].some((a) => seen(a) && "
    "(a.getAttribute('aria-invalid') === 'true' || norm(a.textContent)))}; }"
)

# A one-shot capture of the File list a file input delivers with its own input/change
# event, taken in the document's capture phase (before the page's handlers run). Pages
# that move the chosen file into their own state and empty the input can still have the
# delivered bytes verified. It only reads; ``dispose`` removes both listeners.
_ARM_FILE_CAPTURE = (
    "(el) => { const box = {files: null}; const grab = (e) => { if (e.target === el && box.files === null) "
    "box.files = Array.from(el.files || []); }; document.addEventListener('input', grab, true); "
    "document.addEventListener('change', grab, true); box.dispose = () => { "
    "document.removeEventListener('input', grab, true); document.removeEventListener('change', grab, true); }; "
    "return box; }"
)
# SHA-256 of the first File captured above (null when none was delivered or the page
# cannot hash); disposes of the capture.
_CAPTURED_DIGEST = (
    "async (box) => { box.dispose(); const file = (box.files && box.files[0]) || null; if (!file) return null; "
    "if (!(globalThis.crypto && globalThis.crypto.subtle)) return null; "
    "const d = await crypto.subtle.digest('SHA-256', await file.arrayBuffer()); "
    "return {name: file.name, size: file.size, sha256: Array.from(new Uint8Array(d))"
    ".map((b) => b.toString(16).padStart(2, '0')).join('')}; }"
)

# Scrolls a (possibly virtualized) menu list to its end: the element itself or its
# nearest scrollable ancestor. A driver action, never an OpenCLI read script.
_SCROLL_TO_END = (
    "(el) => { for (let n = el, i = 0; n && i < 3; n = n.parentElement, i++) { "
    "if (n.scrollHeight > n.clientHeight + 1) { n.scrollTop = n.scrollHeight; return true; } } return false; }"
)

async def file_anchor(driver: PageDriver, selector: str) -> str | None:
    """The selector of a file input's uploader container (see ``FILE_ANCHOR``), or None
    when it cannot be read: it only helps verify an input the uploader replaces. A lost
    page context still raises."""
    try:
        anchor = await driver.evaluate(FILE_ANCHOR, selector)
    except PageContextLost:
        raise
    except DriverError:
        return None
    return anchor if isinstance(anchor, str) else None


async def file_shown(driver: PageDriver, selector: str, name: str, *, anchor: str | None = None,
                     wait_s: float = 3.0, emptied: bool = True) -> bool:
    """Whether a file input's uploader took the file: its container, or ``anchor`` when
    the input is gone, shows ``name`` with no error alert once any progress bar is done,
    and (with ``emptied``) the input itself holds no file (it was emptied, or replaced by
    the file's name). Waits (bounded) for the widget to render."""
    loop = asyncio.get_running_loop()
    end = loop.time() + wait_s
    while True:
        state = await driver.evaluate(FILE_SHOWN, {"selector": selector, "name": name, "anchor": anchor})
        if isinstance(state, dict) and state.get("alert"):
            return False
        if isinstance(state, dict) and state.get("shown") and not state.get("busy"):
            return not emptied or state.get("files") in (0, None)
        if loop.time() >= end:
            return False
        await asyncio.sleep(0.1)


# An outside press on the page body (Playwright builds PointerEvent/MouseEvent, bubbling
# and composed): what popover menus listen for to close. No click event follows.
_OUTSIDE_PRESS = ("pointerdown", "mousedown", "pointerup", "mouseup")


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
        try:
            return await self.page.evaluate(expression, arg)
        except PlaywrightError as exc:
            if _CONTEXT_LOST.search(str(exc)):
                raise PageContextLost(f"page context unavailable: {exc}") from exc
            raise DriverError(f"page read failed: {exc}") from exc

    def _doc_mark(self) -> tuple[int, int]:
        return (self._navigations, self._loads)

    def _guard(self, before: tuple[int, int], what: str, exc: Exception | None = None) -> None:
        """Raise ``PageContextLost`` when the document changed during ``what``."""
        lost = before != self._doc_mark() or (
            exc is not None and _CONTEXT_LOST.search(str(exc)) is not None
        )
        if lost:
            raise PageContextLost(f"the page navigated while {what}; re-inspect before continuing")

    async def fill(self, selector: str, text: str) -> None:
        before = self._doc_mark()
        try:
            await self.page.locator(selector).fill(text, timeout=self._timeout_ms)
        except PlaywrightError as exc:
            self._guard(before, f"typing into {selector}", exc)
            raise NotActionable(f"could not type into {selector}: {exc}") from exc
        self._guard(before, f"typing into {selector}")

    async def select_values(self, selector: str, values: list[str]) -> None:
        before = self._doc_mark()
        try:
            await self.page.locator(selector).select_option(value=values, timeout=self._timeout_ms)
        except PlaywrightError as exc:
            self._guard(before, f"selecting in {selector}", exc)
            raise NotActionable(f"could not select {values} in {selector}: {exc}") from exc
        self._guard(before, f"selecting in {selector}")

    async def select_accessible(
        self, selector: str, values: list[str], binding: dict[str, Any],
        *, before_action: Callable[[], Awaitable[None]] | None = None,
    ) -> list[str]:
        from .aria import select_accessible

        before = self._doc_mark()
        handle = await self.page.query_selector(selector)
        if handle is None:
            raise NotActionable("ARIA control no longer exists")

        async def identity_check() -> None:
            self._guard(before, f"selecting in {selector}")
            same = await handle.evaluate(
                "(el, selector) => el.isConnected && document.querySelector(selector) === el", selector,
            )
            if not same:
                raise NotActionable("ARIA control node was replaced; re-inspect")

        try:
            result = await select_accessible(
                self, selector, values, binding, identity_check=identity_check, before_action=before_action,
            )
            self._guard(before, f"selecting in {selector}")
            return result
        except PlaywrightError as exc:
            self._guard(before, f"selecting in {selector}", exc)
            raise NotActionable(f"ARIA control became unavailable: {exc}") from exc
        finally:
            await handle.dispose()

    async def set_checked(self, selector: str, checked: bool, *, label_selector: str | None = None) -> None:
        before = self._doc_mark()
        locator = self.page.locator(selector)
        try:
            await locator.set_checked(checked, timeout=self._timeout_ms)
            self._guard(before, f"setting {selector}")
            return
        except PlaywrightError as exc:
            self._guard(before, f"setting {selector}", exc)
            if label_selector is None:
                raise NotActionable(f"could not set {selector}: {exc}") from exc
        # Custom-styled inputs are often hidden behind their label: click the label.
        try:
            if await locator.is_checked() != checked:
                await self.page.locator(label_selector).click(timeout=self._timeout_ms)
        except PlaywrightError as exc:
            self._guard(before, f"setting {selector}", exc)
            raise NotActionable(f"could not set {selector} via its label: {exc}") from exc
        self._guard(before, f"setting {selector}")

    async def set_files(self, selector: str, path: Path) -> None:
        """Attach ``path`` to the file input directly (hidden inputs behind an "Attach"
        button or a drop zone included) and verify it: the bytes the input holds; or, when
        the uploader emptied or replaced its input, the bytes it was handed with its
        input/change event (when the page can hash them) and its own display of the
        file's name without an error (``file_shown``)."""
        pinned = hashlib.sha256(path.read_bytes()).hexdigest()
        before = self._doc_mark()
        anchor = await file_anchor(self, selector)
        locator = self.page.locator(selector)
        delivered: Any = None
        try:
            capture = await locator.evaluate_handle(_ARM_FILE_CAPTURE, timeout=self._timeout_ms)
            try:
                await locator.set_input_files(str(path), timeout=self._timeout_ms)
                delivered = await capture.evaluate(_CAPTURED_DIGEST)
            finally:
                with contextlib.suppress(PlaywrightError):
                    await capture.evaluate("(box) => box.dispose()")
                with contextlib.suppress(PlaywrightError):
                    await capture.dispose()
        except PlaywrightError as exc:
            self._guard(before, f"attaching a file to {selector}", exc)
            raise NotActionable(f"could not attach a file to {selector}: {exc}") from exc
        self._guard(before, f"attaching a file to {selector}")
        held = await self.evaluate(_FILE_DIGEST, selector)
        self._guard(before, f"verifying the file attached to {selector}")
        if isinstance(held, dict):
            # The input still holds a file: its own bytes decide.
            if held.get("sha256") == pinned:
                return
            raise DriverError(f"the attached bytes in {selector} could not be verified as {path.name}")
        if isinstance(delivered, dict) and delivered.get("sha256") != pinned:
            raise DriverError(f"the bytes delivered to {selector} are not {path.name}")
        if held is None and await file_shown(self, selector, path.name, anchor=anchor):
            # The uploader took the file (the bytes it was handed are this exact path's,
            # verified above when the page could hash them) and emptied or replaced its
            # input; it shows the file's name and no error.
            self._guard(before, f"verifying the file attached to {selector}")
            return
        raise DriverError(f"the attached bytes in {selector} could not be verified as {path.name}")

    async def click(self, selector: str, *, trial: bool = False) -> None:
        if not trial:
            self._mark = (self._navigations, self._loads)
        try:
            await self.page.locator(selector).click(
                trial=trial, no_wait_after=True, timeout=self._timeout_ms
            )
        except PlaywrightError as exc:
            raise NotActionable(f"could not click {selector}: {exc}") from exc

    async def focus(self, selector: str) -> None:
        before = self._doc_mark()
        try:
            await self.page.locator(selector).focus(timeout=self._timeout_ms)
        except PlaywrightError as exc:
            self._guard(before, f"focusing {selector}", exc)
            raise NotActionable(f"could not focus {selector}: {exc}") from exc
        self._guard(before, f"focusing {selector}")

    async def press(self, selector: str, key: str) -> None:
        before = self._doc_mark()
        try:
            await self.page.locator(selector).press(key, timeout=self._timeout_ms)
        except PlaywrightError as exc:
            self._guard(before, f"pressing {key} in {selector}", exc)
            raise NotActionable(f"could not press {key} in {selector}: {exc}") from exc
        self._guard(before, f"pressing {key} in {selector}")

    async def type_text(self, selector: str, text: str, *, delay_s: float = 0.03) -> None:
        reject_control_characters(text, selector)
        before = self._doc_mark()
        try:
            await self.page.locator(selector).press_sequentially(
                text, delay=delay_s * 1000, timeout=self._timeout_ms + len(text) * delay_s * 1000)
        except PlaywrightError as exc:
            self._guard(before, f"typing into {selector}", exc)
            raise NotActionable(f"could not type into {selector}: {exc}") from exc
        self._guard(before, f"typing into {selector}")

    async def clear_text(self, selector: str) -> None:
        before = self._doc_mark()
        locator = self.page.locator(selector)
        try:
            if await locator.input_value(timeout=self._timeout_ms):
                await locator.press("ControlOrMeta+a", timeout=self._timeout_ms)
                await locator.press("Backspace", timeout=self._timeout_ms)
            if await locator.input_value(timeout=self._timeout_ms):
                await locator.fill("", timeout=self._timeout_ms)
            left = await locator.input_value(timeout=self._timeout_ms)
        except PlaywrightError as exc:
            self._guard(before, f"clearing {selector}", exc)
            raise NotActionable(f"could not clear {selector}: {exc}") from exc
        self._guard(before, f"clearing {selector}")
        if left:
            raise NotActionable(f"{selector} still holds text after clearing it")

    async def scroll_to_end(self, selector: str) -> None:
        before = self._doc_mark()
        try:
            await self.page.locator(selector).evaluate(_SCROLL_TO_END)
        except PlaywrightError as exc:
            self._guard(before, f"scrolling {selector}", exc)
            raise NotActionable(f"could not scroll {selector}: {exc}") from exc
        self._guard(before, f"scrolling {selector}")

    async def dismiss(self) -> None:
        before = self._doc_mark()
        body = self.page.locator("body")
        try:
            for event in _OUTSIDE_PRESS:
                await body.dispatch_event(event, {"button": 0, "buttons": 1 if event.endswith("down") else 0},
                                          timeout=self._timeout_ms)
        except PlaywrightError as exc:
            self._guard(before, "pressing outside the menu", exc)
            raise NotActionable(f"could not press outside the menu: {exc}") from exc
        self._guard(before, "pressing outside the menu")

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
