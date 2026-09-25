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
from urllib.parse import urlsplit

from playwright.async_api import ElementHandle, Frame, Page, Request, Response
from playwright.async_api import Error as PlaywrightError
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

    async def set_files(self, selector: str, path: Path) -> bool | None:
        """Attach ``path`` and verify it. Returns whether the attached bytes themselves
        were verified (False: accepted on the uploader's own display of the file, the
        page having kept no readable copy of the bytes; None: not reported)."""
        ...

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


_INTERCEPTED = re.compile(r"intercepts pointer events", re.IGNORECASE)
"""A Playwright click that another element (an overlay) takes: its label would be too."""
_CHECKABLE = ("(el) => el instanceof HTMLInputElement && (el.type === 'checkbox' || el.type === 'radio')"
              " && !el.disabled")
"""Read-only: an enabled checkbox or radio input, the only element a click is dispatched to."""


def _context_lost(exc: Exception) -> bool:
    """A Playwright error caused by the document going away. Only the error's own
    message counts: its call log always ends "waiting for scheduled navigations to
    finish", so a click that merely did not take would read as a navigation."""
    return _CONTEXT_LOST.search(str(exc).split("Call log:")[0]) is not None

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



DEEP_QUERY = (
    "const deepFlat = (sel) => { let found; try { found = Array.from(document.querySelectorAll(sel)); } "
    "catch (e) { return []; } if (found.length) return found; const roots = []; "
    "for (const el of document.querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot); "
    "for (let i = 0; i < roots.length && i < 50; i++) { for (const el of roots[i].querySelectorAll('*')) "
    "if (el.shadowRoot) roots.push(el.shadowRoot); found.push(...roots[i].querySelectorAll(sel)); } "
    "return found; }; "
    "const deepAll = (sel) => { const parts = String(sel).split(' >> '); let found = deepFlat(parts[0]); "
    "for (const part of parts.slice(1)) { const next = []; for (const host of found) { "
    "try { next.push(...(host.shadowRoot || host).querySelectorAll(part)); } catch (e) { return []; } } "
    "found = next; } return found; }; "
    "const deepOne = (sel) => deepAll(sel)[0] || null; "
)
"""Read-only lookup shared by the fixed page scripts: a selector's matches in the
document, or, only when there are none, inside open shadow roots (where some sites
render their application dialog). ``host >> inner`` (the inspector's selector for an
element inside a shadow root, which Playwright chains the same way) resolves ``inner``
inside that host's shadow root."""

# Read-only SHA-256 of the first file attached to a control (null when the page cannot
# hash, e.g. no crypto.subtle in an insecure context).
_FILE_DIGEST = (
    "async (sel) => { " + DEEP_QUERY + "const el = deepOne(sel); if (!el || !el.files || !el.files.length) "
    "return null; if (!(globalThis.crypto && globalThis.crypto.subtle)) return null; const buf = await el.files[0].arrayBuffer(); "
    "const d = await crypto.subtle.digest('SHA-256', buf); "
    "return {origin: String(performance.timeOrigin), url: location.href, sha256: Array.from(new Uint8Array(d))"
    ".map((b) => b.toString(16).padStart(2, '0')).join('')}; }"
)
# The same for one element (the input a file was just set on): null once the page removed
# it from the document, since a detached input's files are nobody's upload.
_ELEMENT_DIGEST = (
    "async (el) => { if (!el.isConnected || !el.files || !el.files.length) return null; "
    "if (!(globalThis.crypto && globalThis.crypto.subtle)) return null; const buf = await el.files[0].arrayBuffer(); "
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
# progress (a progress bar, or "Uploading…"-style text): the input's own container (see
# FILE_ANCHOR), or the anchor when the input is gone. The input is gone when the element
# the file was set on (``attached``, when the driver holds it) left the document, or when
# the selector now names another kind of element or several: an uploader may hand the
# input's id on to a fresh input and to a hidden field of its preview (Teamtailor's
# Dropzone). Also the number of files the input itself holds (null without the input).
FILE_SHOWN = (
    "(arg) => { let found = []; try { found = [...document.querySelectorAll(arg.selector)]; } catch (e) { found = []; } "
    "let anchored = null; if (arg.anchor) { try { const a = document.querySelectorAll(arg.anchor); "
    "if (a.length === 1) anchored = a[0]; } catch (e) { anchored = null; } } "
    "const attached = arg.attached && arg.attached.nodeType === 1 ? arg.attached : null; "
    "const replaced = found.length > 1 || (found.length === 1 && found[0].type !== 'file'); "
    "let el = found[0] || null; "
    "if (attached && attached.isConnected) el = attached; else if ((attached || replaced) && anchored) el = null; "
    "const norm = (t) => String(t || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim().toLowerCase(); "
    "const fields = 'input:not([type=hidden]),select,textarea,[role=combobox],[role=textbox]'; let box = null; "
    "if (el) { for (let n = el.parentElement, d = 0; n && d < 8 && n !== document.body && n.tagName !== 'FORM'; "
    "n = n.parentElement, d++) { if ([...n.querySelectorAll(fields)].some((f) => f !== el)) break; box = n; } } "
    "if (!box) box = anchored; "
    "if (!box) return {shown: false, alert: false, busy: false, files: el && el.files ? el.files.length : null}; "
    "const seen = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); "
    "return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; }; "
    "const BUSY = /^(?:uploading|parsing|processing|analy[sz]ing|scanning|loading|autofilling|reading|please wait)\\b/; "
    "let working = [...box.querySelectorAll('[role=progressbar]')].some(seen); "
    "const texts = document.createTreeWalker(box, NodeFilter.SHOW_TEXT); "
    "for (let t = texts.nextNode(); t && !working; t = texts.nextNode()) { const s = norm(t.nodeValue); "
    "working = !!s && s.length <= 80 && BUSY.test(s) && !!t.parentElement && seen(t.parentElement); } "
    "return {origin: String(performance.timeOrigin), url: location.href, "
    "files: el && el.files ? el.files.length : null, "
    "shown: (arg.names || []).some((n) => !!n && norm(box.innerText).includes(norm(n))), busy: working, "
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


def shown_names(name: str) -> list[str]:
    """How an uploader may show a file just attached: its name, its name cut short
    ("resume-avery-…pdf": the first half of the stem, at least 8 characters), or a count
    ("1 file selected")."""
    stem = Path(name).stem
    forms = [name, "1 file"]
    if len(stem) >= 8:
        forms.append(stem[:max(8, len(stem) // 2)])
    return forms


UPLOAD_BUSY_S = 15.0
"""How long ``file_shown`` keeps waiting while the uploader shows its upload in progress."""


async def file_shown(driver: PageDriver, selector: str, name: str, *, anchor: str | None = None,
                     wait_s: float = 3.0, emptied: bool = True, attached: Any = None) -> bool:
    """Whether a file input's uploader took the file: its container, or ``anchor`` when
    the input is gone (``attached``, the element the file was set on, left the document,
    or the selector now names another element or several), shows ``name`` with no error
    alert once any progress is done, and (with ``emptied``) the input itself holds no
    file (it was emptied, or replaced by the file's name). Waits ``wait_s`` for the widget
    to render, and longer while it shows the upload in progress (a progress bar,
    "Uploading…"), up to ``UPLOAD_BUSY_S`` in all."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    end = start + wait_s
    arg: dict[str, Any] = {"selector": selector, "names": shown_names(name), "anchor": anchor}
    if attached is not None:
        arg["attached"] = attached
    while True:
        state = await driver.evaluate(FILE_SHOWN, arg)
        if isinstance(state, dict) and state.get("alert"):
            return False
        if isinstance(state, dict) and state.get("shown") and not state.get("busy"):
            return not emptied or state.get("files") in (0, None)
        now = loop.time()
        if wait_s > 0 and isinstance(state, dict) and state.get("busy"):
            end = max(end, min(start + UPLOAD_BUSY_S, now + wait_s))
        if now >= end:
            return False
        await asyncio.sleep(0.1)


# An outside press on the page body (Playwright builds PointerEvent/MouseEvent, bubbling
# and composed): what popover menus listen for to close. No click event follows.
_OUTSIDE_PRESS = ("pointerdown", "mousedown", "pointerup", "mouseup")


class PlaywrightDriver:
    """``PageDriver`` over one Playwright ``Page``."""

    attaches_files = True
    """Playwright sets a file input's files directly."""
    injects_captcha_tokens = True
    """Playwright can put a solved CAPTCHA's token into the page (``inject_captcha_token``)."""

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
        self._frame: Frame | None = None
        """The child frame entered with ``enter_frame``; None operates the page itself."""
        page.on("response", self._on_response)
        page.on("request", self._on_request)
        page.on("load", self._on_load)
        page.on("framenavigated", self._on_frame_navigated)

    @property
    def _scope(self) -> Page | Frame:
        """Where every read and action runs: the page, or the entered child frame."""
        return self._frame if self._frame is not None else self.page

    def _on_request(self, request: Request) -> None:
        try:
            if request.is_navigation_request() and (
                    request.frame == self.page.main_frame
                    or (self._frame is not None and request.frame == self._frame)):
                self._navigations += 1
        except PlaywrightError:  # pragma: no cover - page closed mid-event
            pass

    def _on_load(self, _page: Page) -> None:
        self._loads += 1

    def _on_frame_navigated(self, frame: Frame) -> None:
        if self._frame is not None and frame == self._frame:
            self._loads += 1

    async def enter_frame(self, src: str) -> None:
        """Run every later read and action inside the child frame that shows ``src`` (an
        embedded application page that refuses to load on its own), until the next
        ``goto``. Raises ``NotActionable`` when no frame shows it."""
        wanted = urlsplit(src)
        for frame in self.page.frames:
            shown = urlsplit(frame.url)
            if frame is not self.page.main_frame and (shown.scheme, shown.netloc, shown.path) == (
                    wanted.scheme, wanted.netloc, wanted.path):
                self._frame = frame
                self._mark = (self._navigations, self._loads)
                return
        raise NotActionable(f"no frame on the page shows {src}")

    def _on_response(self, response: Response) -> None:
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self._last_status = response.status
        except PlaywrightError:  # pragma: no cover - page closed mid-event
            pass

    @property
    def url(self) -> str:
        return self._scope.url

    @property
    def last_status(self) -> int | None:
        return self._last_status

    async def goto(self, url: str) -> int | None:
        self._frame = None
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
            return await self._scope.evaluate(expression, arg)
        except PlaywrightError as exc:
            if _context_lost(exc):
                raise PageContextLost(f"page context unavailable: {exc}") from exc
            raise DriverError(f"page read failed: {exc}") from exc

    async def inject_captcha_token(self, kind: str, token: str, *, callback: str = "",
                                   call_callback: bool = False) -> dict[str, int]:
        """Put a solved CAPTCHA's token into the widget's response fields and, with
        ``call_callback``, call the widget's callback with it (``captcha.CAPTCHA_INJECT``).
        The only page script that writes; it clicks nothing. A callback that navigates is
        reported as ``navigated``. Errors never carry the token."""
        from .captcha import CAPTCHA_INJECT

        before = self._doc_mark()
        try:
            result = await self._scope.evaluate(CAPTCHA_INJECT, {
                "kind": kind, "token": token, "callback": callback, "call_callback": call_callback})
        except PlaywrightError as exc:
            if before != self._doc_mark() or _context_lost(exc):
                return {"fields": 0, "called": 0, "navigated": 1}
            raise DriverError("could not put the CAPTCHA token into the page") from None
        if not isinstance(result, dict):
            return {"fields": 0, "called": 0, "navigated": int(before != self._doc_mark())}
        return {"fields": int(result.get("fields") or 0), "called": int(result.get("called") or 0),
                "navigated": int(before != self._doc_mark())}

    def _doc_mark(self) -> tuple[int, int]:
        return (self._navigations, self._loads)

    def _guard(self, before: tuple[int, int], what: str, exc: Exception | None = None) -> None:
        """Raise ``PageContextLost`` when the document changed during ``what``."""
        lost = before != self._doc_mark() or (exc is not None and _context_lost(exc))
        if lost:
            raise PageContextLost(f"the page navigated while {what}; re-inspect before continuing")

    async def fill(self, selector: str, text: str) -> None:
        before = self._doc_mark()
        try:
            await self._scope.locator(selector).fill(text, timeout=self._timeout_ms)
        except PlaywrightError as exc:
            self._guard(before, f"typing into {selector}", exc)
            raise NotActionable(f"could not type into {selector}: {exc}") from exc
        self._guard(before, f"typing into {selector}")

    async def select_values(self, selector: str, values: list[str]) -> None:
        before = self._doc_mark()
        try:
            await self._scope.locator(selector).select_option(value=values, timeout=self._timeout_ms)
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
        handle = await self._scope.query_selector(selector)
        if handle is None:
            raise NotActionable("ARIA control no longer exists")

        async def identity_check() -> None:
            self._guard(before, f"selecting in {selector}")
            same = await handle.evaluate(
                "(el, selector) => { " + DEEP_QUERY + "return el.isConnected && deepOne(selector) === el; }",
                selector,
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
        """Check or uncheck a checkbox or radio: a click on the input, else on its label
        (custom-styled inputs are often hidden behind it). When something else takes the
        pointer where they are (a fixed cookie dialog or panel over the page), the click
        is dispatched to the input itself, never to whatever covers it, and read back."""
        before = self._doc_mark()
        locator = self._scope.locator(selector)
        try:
            await locator.set_checked(checked, timeout=self._timeout_ms)
            self._guard(before, f"setting {selector}")
            return
        except PlaywrightError as exc:
            self._guard(before, f"setting {selector}", exc)
            failure: PlaywrightError = exc
        how = ""
        if label_selector is not None and not _INTERCEPTED.search(str(failure)):
            how = " via its label"
            try:
                if await locator.is_checked() != checked:
                    await self._scope.locator(label_selector).click(timeout=self._timeout_ms)
                self._guard(before, f"setting {selector}")
                return
            except PlaywrightError as exc:
                self._guard(before, f"setting {selector}", exc)
                failure = exc
        try:
            if await locator.evaluate(_CHECKABLE) and await locator.is_checked() != checked:
                await locator.dispatch_event("click")
            done = await locator.is_checked() == checked
        except PlaywrightError as exc:
            self._guard(before, f"setting {selector}", exc)
            raise NotActionable(f"could not set {selector}{how}: {failure}") from exc
        self._guard(before, f"setting {selector}")
        if not done:
            raise NotActionable(f"could not set {selector}{how}: {failure}")

    async def set_files(self, selector: str, path: Path) -> bool:
        """Attach ``path`` to the file input directly (hidden inputs behind an "Attach"
        button or a drop zone included) and verify it: the bytes the input holds; or, when
        the uploader emptied or replaced its input, the bytes it was handed with its
        input/change event (when the page can hash them) and its own display of the
        file's name without an error (``file_shown``). Everything is read from the element
        the file was set on: once the uploader removed it, the selector may name a fresh
        input or another field that took over its id. Returns whether the bytes were
        verified (False: accepted on that display alone)."""
        pinned = hashlib.sha256(path.read_bytes()).hexdigest()
        before = self._doc_mark()
        anchor = await file_anchor(self, selector)
        locator = self._scope.locator(selector)
        delivered: Any = None
        attached: ElementHandle | None = None
        try:
            try:
                attached = await locator.element_handle(timeout=self._timeout_ms)
                capture = await attached.evaluate_handle(_ARM_FILE_CAPTURE)
                try:
                    await attached.set_input_files(str(path), timeout=self._timeout_ms)
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
            try:
                held = await attached.evaluate(_ELEMENT_DIGEST)
            except PlaywrightError as exc:
                self._guard(before, f"verifying the file attached to {selector}", exc)
                raise DriverError(f"could not read the file attached to {selector}: {exc}") from exc
            self._guard(before, f"verifying the file attached to {selector}")
            if isinstance(held, dict):
                # The input still holds a file: its own bytes decide.
                if held.get("sha256") == pinned:
                    return True
                raise DriverError(f"the attached bytes in {selector} could not be verified as {path.name!r}")
            if isinstance(delivered, dict) and delivered.get("sha256") != pinned:
                raise DriverError(f"the bytes delivered to {selector} are not {path.name!r}")
            if held is None and await file_shown(self, selector, path.name, anchor=anchor, attached=attached):
                # The uploader took the file (the bytes it was handed are this exact path's,
                # verified above when the page could hash them) and emptied or replaced its
                # input; it shows the file's name and no error.
                self._guard(before, f"verifying the file attached to {selector}")
                return isinstance(delivered, dict)
            raise DriverError(f"the attached bytes in {selector} could not be verified as {path.name!r}")
        finally:
            if attached is not None:
                with contextlib.suppress(PlaywrightError):
                    await attached.dispose()

    async def click(self, selector: str, *, trial: bool = False) -> None:
        if not trial:
            self._mark = (self._navigations, self._loads)
        try:
            await self._scope.locator(selector).click(
                trial=trial, no_wait_after=True, timeout=self._timeout_ms
            )
        except PlaywrightError as exc:
            raise NotActionable(f"could not click {selector}: {exc}") from exc

    async def focus(self, selector: str) -> None:
        before = self._doc_mark()
        try:
            await self._scope.locator(selector).focus(timeout=self._timeout_ms)
        except PlaywrightError as exc:
            self._guard(before, f"focusing {selector}", exc)
            raise NotActionable(f"could not focus {selector}: {exc}") from exc
        self._guard(before, f"focusing {selector}")

    async def press(self, selector: str, key: str) -> None:
        before = self._doc_mark()
        try:
            await self._scope.locator(selector).press(key, timeout=self._timeout_ms)
        except PlaywrightError as exc:
            self._guard(before, f"pressing {key} in {selector}", exc)
            raise NotActionable(f"could not press {key} in {selector}: {exc}") from exc
        self._guard(before, f"pressing {key} in {selector}")

    async def type_text(self, selector: str, text: str, *, delay_s: float = 0.03) -> None:
        reject_control_characters(text, selector)
        before = self._doc_mark()
        try:
            await self._scope.locator(selector).press_sequentially(
                text, delay=delay_s * 1000, timeout=self._timeout_ms + len(text) * delay_s * 1000)
        except PlaywrightError as exc:
            self._guard(before, f"typing into {selector}", exc)
            raise NotActionable(f"could not type into {selector}: {exc}") from exc
        self._guard(before, f"typing into {selector}")

    async def clear_text(self, selector: str) -> None:
        before = self._doc_mark()
        locator = self._scope.locator(selector)
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
            await self._scope.locator(selector).evaluate(_SCROLL_TO_END)
        except PlaywrightError as exc:
            self._guard(before, f"scrolling {selector}", exc)
            raise NotActionable(f"could not scroll {selector}: {exc}") from exc
        self._guard(before, f"scrolling {selector}")

    async def dismiss(self) -> None:
        before = self._doc_mark()
        body = self._scope.locator("body")
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
                await self._scope.wait_for_load_state(state, timeout=remaining * 1000)
            except PlaywrightTimeout:
                return
            except PlaywrightError:  # pragma: no cover - navigation replaced the frame
                return

    async def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path), full_page=True, timeout=self._timeout_ms)

    async def html(self) -> str:
        return await self._scope.content()

    async def bring_to_front(self) -> None:
        await self.page.bring_to_front()
