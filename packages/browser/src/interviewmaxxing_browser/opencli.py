"""OpenCLI page driver: the same runtime over a user-present Chrome via Browser Bridge.

``OpenCliDriver`` implements :class:`~interviewmaxxing_browser.driver.PageDriver` by
running ``opencli browser <session> ...`` as structured subprocess argument lists (no
shell). It is used through :class:`GenericApplicationBrowser`, so field identity,
fingerprints, consent handling, confirmation and uncertainty rules are exactly those
of the Playwright path.

Rules this driver enforces:

* **Owned session and tab only.** Each driver creates its own tab (``tab new``) in a
  uniquely named owned session (``imx-application-<16 hex>`` by default, background
  window) and pins every command to that tab with ``--tab``. Ownership is established
  and checked against the protected-tab list *before* any navigation; a session's
  restored default tab is never navigated. It never binds, selects or closes another
  tab, and refuses protected sessions (the user's assessment session, job-search
  sessions).
* **Mutations only through structured commands** (``fill``, ``select``, ``check``,
  ``uncheck``, ``upload``, ``click``, ``open``, and for menu widgets ``focus``,
  ``keys`` and ``type``). Each is verified afterwards: the command's own envelope,
  then a read-only check of the control and of the document (an unexpected
  navigation is an error). ``keys`` is sent only after a read-only check that the
  target element has focus.
* **Evaluation is read-only and allowlisted.** ``evaluate`` runs only the fixed
  read scripts of this package (inspector, control/document state, digests). It is
  not a general JavaScript sandbox; the internal regex lint is only defence in depth.
* **Unsupported capabilities are errors, never success.** Selecting several options
  of a multi-select, uploading when Browser Bridge may not set files, pressing keys
  when the CLI refuses them, scrolling a menu list and focusing a window raise
  :class:`CapabilityUnsupported` with what the user can do instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from interviewmaxxing_core import BrowserOptions

from .annotations import FormAnnotator, SchemaHintLoader
from .aria import ARIA_EXPANSION, ARIA_OBSERVE, ARIA_STATE, COMBO_STATE, ENTER_SAFE, PHONE_STATE
from .driver import (
    _FILE_DIGEST,
    DEEP_QUERY,
    FILE_ANCHOR,
    FILE_SHOWN,
    DriverError,
    NotActionable,
    PageContextLost,
    file_anchor,
    file_shown,
    reject_control_characters,
)
from .driver import CapabilityUnsupported as CapabilityUnsupported  # public name kept here
from .runtime import (
    _BUTTONS_WITHIN,
    _DOCUMENT_IDENTITY,
    _EFFECTIVE_SUBMISSION,
    _IN_OWN_POPUP,
    _NATIVE_VALIDITY,
    _READ_CHECKED,
    _READ_CONTROL,
    ActionPolicy,
    GenericApplicationBrowser,
)
from .snapshot import inspector_script
from .uploads import UPLOAD_STATE

DEFAULT_SESSION = "imx-application"
PROTECTED_SESSION_PREFIXES = ("imx-assessment", "imx-jobs")
"""Sessions owned by the user or by other workers; this driver never uses them."""


# --- errors ---------------------------------------------------------------------------


class OpenCliError(DriverError):
    """``opencli`` reported a failure. ``code`` is its structured error code, if any."""

    def __init__(self, message: str, *, code: str | None = None, hint: str | None = None,
                 command: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.command = command


class OpenCliUnavailable(OpenCliError):
    """OpenCLI, its daemon, the Browser Bridge extension or the profile is not usable."""


class OpenCliTimeout(OpenCliError):
    """The command did not finish in time; whether it took effect is unknown."""


class OpenCliTargetError(NotActionable):
    """The target element was missing or ambiguous, so nothing was done."""


class UnverifiedAction(DriverError):
    """The command ran but its effect could not be confirmed (re-identified element,
    value read back differently, or the page navigated unexpectedly)."""


class OpenCliContextLost(UnverifiedAction, PageContextLost):
    """The document was replaced while operating a control; the fill is aborted."""


_TARGET_CODES = frozenset({
    "not_found", "stale_ref", "invalid_selector", "selector_not_found", "selector_ambiguous",
    "selector_nth_out_of_range", "option_not_found", "not_a_select",
})
_UNAVAILABLE_PATTERNS = re.compile(
    r"daemon|not connected|extension|no browser|profile .*not|ECONNREFUSED|bridge",
    re.IGNORECASE,
)


# --- transport ------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], float], Awaitable[CommandResult]]
"""Runs one argv list (never through a shell) with a timeout in seconds."""


async def subprocess_runner(argv: Sequence[str], timeout_s: float) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise OpenCliTimeout(f"opencli did not finish within {timeout_s:g}s",
                             command=" ".join(argv[:5])) from None
    return CommandResult(process.returncode or 0, out.decode("utf-8", "replace"),
                         err.decode("utf-8", "replace"))


def _clean_stderr(text: str) -> str:
    lines = [ln.strip().lstrip("✖").strip() for ln in text.splitlines()]
    keep = [ln for ln in lines if ln and not ln.startswith(("Update available", "Run: npm"))]
    return " ".join(keep)


# --- read-only evaluation ---------------------------------------------------------------

_WRITES = re.compile(
    r"\.(?:click|submit|requestSubmit|dispatchEvent|setAttribute|removeAttribute|toggleAttribute|"
    r"appendChild|removeChild|replaceChild|insertBefore|insertAdjacent\w*|remove|reset|focus|blur|"
    r"setRangeText|setSelectionRange|showPicker|pushState|replaceState|assign|reload|setItem)\s*\("
    r"|\.(?:innerHTML|outerHTML|innerText|textContent|value|checked|selected|selectedIndex|files|"
    r"href|src|action|method|hidden|disabled|__\w+)\s*(?:[+\-*/]?=)(?!=)"
    r"|\b(?:location|document\.cookie|window\.name)\s*=(?!=)"
    r"|\bfetch\s*\(|XMLHttpRequest|sendBeacon|\bwindow\.open\s*\(|\bnew\s+(?:Event|MouseEvent|"
    r"KeyboardEvent|InputEvent)\b"
)


def _lint_read_script(expression: str) -> None:
    """Lint: refuse obvious page-changing calls. This is defence in depth for the fixed
    scripts below, not a general guarantee that arbitrary JavaScript is harmless;
    ``OpenCliDriver.evaluate`` therefore also allowlists the exact scripts it runs."""
    match = _WRITES.search(expression)
    if match:
        raise DriverError(f"evaluation must be read-only; refused {match.group(0).strip()!r}")


def _wrap(expression: str, arg: Any) -> str:
    # Async so that a script may await (file digests); OpenCLI awaits the promise.
    return (
        "(async () => { try { return JSON.stringify({ok: true, value: await (" + expression + ")("
        + json.dumps(arg) + ")}); } catch (e) { return JSON.stringify({ok: false, error: "
        "String((e && e.message) || e)}); } })()"
    )


_DOC_STATE = (
    "() => { const n = performance.getEntriesByType('navigation')[0]; "
    "return {origin: String(performance.timeOrigin), url: location.href, ready: document.readyState, "
    "status: n && n.responseStatus ? n.responseStatus : null}; }"
)
_CONTROL_STATE = (
    "(sel) => { " + DEEP_QUERY + "const el = deepOne(sel); if (!el) return null; "
    "return {origin: String(performance.timeOrigin), url: location.href, value: el.value, "
    "checked: !!el.checked, values: el.tagName === 'SELECT' ? Array.from(el.selectedOptions).map((o) => o.value) : null, "
    "files: el.files ? Array.from(el.files).map((f) => ({name: f.name, size: f.size})) : null}; }"
)
_ACTIONABLE = (
    "(sel) => { " + DEEP_QUERY + "let els; try { document.querySelector(sel); els = deepAll(sel); } "
    "catch (e) { return {n: -1}; } "
    "if (els.length !== 1) return {n: els.length}; const el = els[0]; const r = el.getBoundingClientRect(); "
    "const cs = getComputedStyle(el); return {n: 1, disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true', "
    "visible: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'}; }"
)
_HTML = "() => document.documentElement.outerHTML"
_FOCUSED = (
    "(sel) => { " + DEEP_QUERY + "let els; try { document.querySelector(sel); els = deepAll(sel); } "
    "catch (e) { return false; } let active = document.activeElement; "
    "while (active && active.shadowRoot && active.shadowRoot.activeElement) active = active.shadowRoot.activeElement; "
    "return els.length === 1 && active === els[0]; }"
)
_IN_SHADOW = (
    "(sel) => { " + DEEP_QUERY + "if (String(sel).includes(' >> ')) return deepAll(sel).length > 0; "
    "let light; try { light = document.querySelectorAll(sel).length; } "
    "catch (e) { return false; } return light === 0 && deepAll(sel).length > 0; }"
)
"""Whether a selector reaches its element only inside an open shadow root, which
OpenCLI's own targeting (CSS and semantic locators) does not enter."""

_ALLOWED_SCRIPTS: frozenset[str] = frozenset({
    inspector_script(), ARIA_EXPANSION, ARIA_OBSERVE, ARIA_STATE, _DOC_STATE, _CONTROL_STATE, _ACTIONABLE, _HTML, _FILE_DIGEST,
    _READ_CONTROL, _READ_CHECKED, _NATIVE_VALIDITY, _EFFECTIVE_SUBMISSION, _DOCUMENT_IDENTITY,
    COMBO_STATE, PHONE_STATE, ENTER_SAFE, _FOCUSED, FILE_ANCHOR, FILE_SHOWN, _BUTTONS_WITHIN,
    UPLOAD_STATE,
    _IN_OWN_POPUP,
    _IN_SHADOW,
})
"""The only page scripts ``OpenCliDriver.evaluate`` will run: fixed read-only ones."""


# --- driver ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenCliConfig:
    """Where and how the driver runs. ``profile`` is the Browser Bridge profile alias
    (``opencli profile list``); ``None`` uses OpenCLI's default profile."""

    session: str = DEFAULT_SESSION
    """Prefix for a uniquely owned session; the driver always appends a random suffix."""
    profile: str | None = None
    window: Literal["background", "foreground"] = "background"
    executable: str = "opencli"
    command_timeout_s: float = 30.0
    navigation_grace_s: float = 1.5
    poll_interval_s: float = 0.1
    protected_sessions: frozenset[str] = frozenset({"imx-assessment-opencli"})
    protected_tabs: frozenset[str] = frozenset()
    """Tab ids the driver must never act on (e.g. the user's own working tab)."""
    attach_files: bool = False
    """Browser Bridge refuses ``upload``. Without it a dialog wizard's resume step uses a
    resume the site already has (the pinned file's, or the only one), or is left to the
    person ("Attach your resume in the browser window"); a page form's file field is
    still tried once and reports what the person can do."""

    def __post_init__(self) -> None:
        session = self.session.strip()
        if not session or session.startswith("-"):
            raise ValueError("an OpenCLI session name is required")
        if session in self.protected_sessions or session.startswith(PROTECTED_SESSION_PREFIXES):
            raise ValueError(f"session {session!r} belongs to the user or another worker")


@dataclass
class _Doc:
    origin: str = ""
    url: str = ""
    status: int | None = None


class OpenCliDriver:
    """``PageDriver`` over one owned OpenCLI tab."""

    def __init__(self, config: OpenCliConfig | None = None, *, runner: Runner | None = None) -> None:
        self.config = config or OpenCliConfig()
        self.session = f"{self.config.session}-{secrets.token_hex(8)}"
        self._run = runner or subprocess_runner
        self._lock = asyncio.Lock()
        self._tab: str | None = None
        self._doc = _Doc()
        self._mark = ""
        self.log: list[list[str]] = []
        """Every argv sent (for diagnostics and tests)."""

    # --- transport ---------------------------------------------------------------

    @property
    def attaches_files(self) -> bool:
        return self.config.attach_files

    @property
    def tab(self) -> str | None:
        """The page id of the tab this driver opened (``None`` before the first ``goto``)."""
        return self._tab

    def _argv(self, command: Sequence[str], options: Sequence[str] = (),
              positionals: Sequence[str] = (), *, pin: bool = True) -> list[str]:
        argv = [self.config.executable]
        if self.config.profile:
            argv += ["--profile", self.config.profile]
        argv += ["browser", self.session, *command, *options]
        if pin:
            if self._tab is None:
                raise DriverError("no page is open in this OpenCLI session yet; call goto() first")
            argv += ["--tab", self._tab]
        if positionals:
            argv += ["--", *positionals]  # values can never be read as options
        return argv

    async def _call(self, argv: list[str], *, timeout_s: float | None = None) -> Any:
        async with self._lock:
            self.log.append(argv)
            result = await self._run(argv, timeout_s or self.config.command_timeout_s)
        command = " ".join(argv[argv.index("browser") + 2: argv.index("browser") + 3]) if "browser" in argv else ""
        payload: Any = None
        text = result.stdout.strip()
        if text:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = text
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            err = payload["error"]
            code = str(err.get("code") or "") or None
            message = str(err.get("message") or "opencli error")
            hint = err.get("hint")
            if code in _TARGET_CODES:
                raise OpenCliTargetError(f"{command}: {message}")
            raise OpenCliError(f"{command}: {message}", code=code, hint=hint, command=command)
        if result.returncode != 0:
            detail = _clean_stderr(result.stderr) or text or f"exit status {result.returncode}"
            if _UNAVAILABLE_PATTERNS.search(detail):
                raise OpenCliUnavailable(f"{command}: {detail}", command=command)
            raise OpenCliError(f"{command}: {detail}", command=command)
        return payload

    # --- PageDriver ----------------------------------------------------------------

    @property
    def url(self) -> str:
        return self._doc.url

    @property
    def last_status(self) -> int | None:
        return self._doc.status

    async def _own_tab(self) -> None:
        """Create this driver's own tab and prove ownership before navigating anything.
        ``tab new`` never reuses a session's restored default tab, unlike ``open``."""
        created = await self._call(
            self._argv(["tab", "new"], ["--window", self.config.window], pin=False)
        )
        page = created.get("page") if isinstance(created, dict) else None
        if not page:
            raise OpenCliError(f"tab new did not report a tab id: {created!r}", command="tab")
        page = str(page)
        if page in self.config.protected_tabs:
            raise DriverError(f"OpenCLI returned protected tab {page}; refusing to use it")
        listed = await self._call(self._argv(["tab", "list"], pin=False))
        pages = {str(t.get("page")) for t in listed if isinstance(t, dict)} if isinstance(listed, list) else set()
        if page not in pages:
            raise OpenCliError(f"new tab {page} is not listed in session {self.session}", command="tab")
        self._tab = page

    async def goto(self, url: str) -> int | None:
        if self._tab is None:
            await self._own_tab()
        await self._call(self._argv(["open"], positionals=[url]))
        await self._refresh_doc()
        self._mark = self._doc.origin
        return self._doc.status

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        if expression not in _ALLOWED_SCRIPTS:
            raise DriverError("OpenCLI evaluation runs only this package's fixed read-only scripts")
        _lint_read_script(expression)
        if expression == inspector_script():
            await self._refresh_doc()
        raw = await self._call(self._argv(["eval"], positionals=[_wrap(expression, arg)]))
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raise OpenCliError(f"eval returned non-JSON output: {raw[:120]!r}", command="eval") from None
        if not isinstance(raw, dict) or "ok" not in raw:
            raise OpenCliError(f"eval returned an unexpected shape: {str(raw)[:120]!r}", command="eval")
        if not raw["ok"]:
            raise DriverError(f"page script failed: {raw.get('error')}")
        return raw.get("value")

    async def _refresh_doc(self) -> _Doc:
        state = await self.evaluate(_DOC_STATE)
        if isinstance(state, dict):
            self._doc = _Doc(origin=str(state.get("origin", "")), url=str(state.get("url", "")),
                             status=state.get("status") if isinstance(state.get("status"), int) else None)
        return self._doc

    async def _control(self, selector: str) -> dict[str, Any]:
        state = await self.evaluate(_CONTROL_STATE, selector)
        if not isinstance(state, dict):
            raise UnverifiedAction(f"{selector} is no longer on the page after the action")
        if state.get("origin") != self._doc.origin or state.get("url") != self._doc.url:
            raise OpenCliContextLost(
                f"the page navigated while operating {selector}; re-inspect before continuing"
            )
        return state

    async def _targeted(self, argv: list[str], selector: str) -> Any:
        """Run one action command on ``selector``. An element OpenCLI cannot find because
        it lives inside an open shadow root (which its CSS and semantic locators do not
        enter) is reported as a capability the person has to cover, not as a missing
        element."""
        try:
            return await self._call(argv)
        except OpenCliTargetError as exc:
            try:
                shadowed = await self.evaluate(_IN_SHADOW, selector) is True
            except DriverError:
                shadowed = False
            if shadowed:
                raise CapabilityUnsupported(
                    f"OpenCLI cannot reach {selector} inside the page's shadow root; do this step "
                    "yourself in the browser window, then continue") from exc
            raise

    def _check_match(self, envelope: Any, what: str) -> dict[str, Any]:
        if not isinstance(envelope, dict):
            raise UnverifiedAction(f"{what}: unexpected response {envelope!r}")
        if envelope.get("matches_n") not in (None, 1):
            raise UnverifiedAction(f"{what}: matched {envelope.get('matches_n')} elements")
        if envelope.get("match_level") == "reidentified":
            raise UnverifiedAction(f"{what}: acted on a re-identified element; re-inspect")
        return envelope

    async def fill(self, selector: str, text: str) -> None:
        envelope = self._check_match(
            await self._targeted(self._argv(["fill"], positionals=[selector, text]), selector), f"fill {selector}"
        )
        if not envelope.get("verified") or envelope.get("actual") != text:
            raise UnverifiedAction(f"fill {selector}: the field reads back {envelope.get('actual')!r}")
        state = await self._control(selector)
        if state.get("value") != text:
            raise UnverifiedAction(f"fill {selector}: the field reads back {state.get('value')!r}")

    async def select_values(self, selector: str, values: list[str]) -> None:
        if len(values) != 1:
            raise CapabilityUnsupported(
                f"OpenCLI can set only one option of the multi-select {selector}; "
                "choose the options yourself in the browser window"
            )
        self._check_match(
            await self._targeted(self._argv(["select"], positionals=[selector, values[0]]), selector),
            f"select {selector}",
        )
        state = await self._control(selector)
        if state.get("values") != values:
            # OpenCLI matches labels before values; never accept a different option.
            raise UnverifiedAction(f"select {selector}: selected {state.get('values')!r}, not {values!r}")

    async def select_accessible(
        self, selector: str, values: list[str], binding: dict[str, Any],
        *, before_action: Callable[[], Awaitable[None]] | None = None,
    ) -> list[str]:
        from .aria import select_accessible

        return await select_accessible(self, selector, values, binding, before_action=before_action)

    async def set_checked(self, selector: str, checked: bool, *, label_selector: str | None = None) -> None:
        envelope = self._check_match(
            await self._targeted(self._argv(["check" if checked else "uncheck"], positionals=[selector]), selector),
            f"{'check' if checked else 'uncheck'} {selector}",
        )
        state = await self._control(selector)
        if envelope.get("checked") is not checked or state.get("checked") is not checked:
            raise UnverifiedAction(f"{selector} reads back checked={state.get('checked')!r}")

    async def _attached_digest(self, selector: str) -> str | None:
        """SHA-256 of the file the control holds, computed read-only in the page."""
        result = await self.evaluate(_FILE_DIGEST, selector)
        if not isinstance(result, dict):
            return None
        if result.get("origin") != self._doc.origin or result.get("url") != self._doc.url:
            raise OpenCliContextLost(f"the page navigated while reading {selector}")
        digest = result.get("sha256")
        return digest if isinstance(digest, str) and len(digest) == 64 else None

    async def set_files(self, selector: str, path: Path) -> bool:
        wanted = [{"name": path.name, "size": path.stat().st_size}]
        pinned = hashlib.sha256(path.read_bytes()).hexdigest()
        before = await self._control(selector)
        if before.get("files") == wanted:
            # Something with the same name and size is attached (for example by the
            # user). Only the actual bytes decide; an unverifiable file is never accepted.
            digest = await self._attached_digest(selector)
            if digest == pinned:
                return True
            raise CapabilityUnsupported(
                f"the file attached to {selector} is not the pinned {path.name} "
                f"({'different contents' if digest else 'contents could not be verified'}); "
                "replace it with the exact pinned file in the browser window, then continue"
            )
        anchor = await file_anchor(self, selector)
        try:
            envelope = await self._call(self._argv(["upload"], positionals=[selector, str(path)]))
        except OpenCliTargetError:
            raise
        except OpenCliError as exc:
            if await self._files(selector) == []:
                raise CapabilityUnsupported(
                    f"Browser Bridge could not attach files here ({exc}). Attach {path.name} to "
                    "the upload field yourself in the browser window, then continue so its "
                    "actual bytes can be verified against the pinned file"
                ) from exc
            raise UnverifiedAction(f"upload {selector} failed after changing the field: {exc}") from exc
        self._check_match(envelope, f"upload {selector}")
        after = await self._files(selector)
        if after in ([], None) and await file_shown(self, selector, path.name, anchor=anchor):
            return False  # the uploader took the file and shows it; its bytes are not readable
        after = await self._control(selector)
        if after.get("files") != wanted:
            raise UnverifiedAction(f"upload {selector}: the field holds {after.get('files')!r}")
        if await self._attached_digest(selector) != pinned:
            raise UnverifiedAction(f"upload {selector}: the attached bytes are not the pinned file")
        return True

    async def _files(self, selector: str) -> Any:
        state = await self.evaluate(_CONTROL_STATE, selector)
        return state.get("files") if isinstance(state, dict) else None

    async def click(self, selector: str, *, trial: bool = False) -> None:
        if trial:
            check = await self.evaluate(_ACTIONABLE, selector)
            if not isinstance(check, dict) or check.get("n") != 1:
                raise OpenCliTargetError(f"{selector} does not match exactly one element")
            if check.get("disabled") or not check.get("visible"):
                raise OpenCliTargetError(f"{selector} is disabled or not visible")
            return
        self._mark = self._doc.origin
        self._check_match(await self._targeted(self._argv(["click"], positionals=[selector]), selector),
                          f"click {selector}")

    async def _keyboard(self, command: str, positionals: list[str], what: str,
                        selector: str | None = None) -> Any:
        """Run a focus/keys/type command; a refusal of the command itself means this
        session cannot use the keyboard here, and nothing was done."""
        try:
            argv = self._argv([command], positionals=positionals)
            return await (self._targeted(argv, selector) if selector else self._call(argv))
        except (OpenCliTargetError, OpenCliUnavailable, OpenCliTimeout):
            raise
        except OpenCliError as exc:
            raise CapabilityUnsupported(
                f"OpenCLI could not {what} ({exc}); operate this control yourself in the "
                "browser window, then continue"
            ) from exc

    async def focus(self, selector: str) -> None:
        self._check_match(await self._keyboard("focus", [selector], f"focus {selector}", selector),
                          f"focus {selector}")
        await self._control(selector)

    async def press(self, selector: str, key: str) -> None:
        await self.focus(selector)
        # ``keys`` acts on whatever has focus: send it only while the target does.
        if await self.evaluate(_FOCUSED, selector) is not True:
            raise UnverifiedAction(f"{selector} did not keep the focus; {key} was not pressed")
        await self._keyboard("keys", [key], f"press {key}")
        await self._control(selector)

    async def type_text(self, selector: str, text: str, *, delay_s: float = 0.03) -> None:
        reject_control_characters(text, selector)
        self._check_match(await self._keyboard("type", [selector, text], f"type into {selector}", selector),
                          f"type {selector}")
        await self._control(selector)

    async def clear_text(self, selector: str) -> None:
        await self.fill(selector, "")

    async def scroll_to_end(self, selector: str) -> None:
        raise CapabilityUnsupported(
            f"OpenCLI cannot scroll the list {selector}; choose from it yourself in the browser window"
        )

    async def dismiss(self) -> None:
        raise CapabilityUnsupported(
            "OpenCLI cannot press outside an open menu; close it yourself in the browser window"
        )

    async def settle(self, timeout_s: float) -> None:
        """Wait (bounded) for a navigation started by the last click to load. Page
        scripts are read-only, so a new document is recognised by its timeOrigin."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        grace = loop.time() + min(self.config.navigation_grace_s, timeout_s)
        navigated = False
        while loop.time() < deadline:
            try:
                state = await self.evaluate(_DOC_STATE)
            except DriverError:
                state = None  # the old document is going away
            if isinstance(state, dict):
                origin = str(state.get("origin", ""))
                if origin != self._mark:
                    navigated = True
                if navigated and state.get("ready") == "complete":
                    break
                if not navigated and loop.time() >= grace:
                    break
            await asyncio.sleep(self.config.poll_interval_s)
        await self._refresh_doc()
        self._mark = self._doc.origin

    async def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await self._call(self._argv(["screenshot"], ["--full-page"], [str(path)]))
        if not path.is_file():
            raise OpenCliError(f"screenshot was not written to {path}", command="screenshot")

    async def html(self) -> str:
        return str(await self.evaluate(_HTML))

    async def bring_to_front(self) -> None:
        raise CapabilityUnsupported(
            f"OpenCLI does not focus windows; switch to the tab of session "
            f"{self.session!r} ({self._doc.url or 'not opened yet'}) yourself"
        )

    async def release(self, *, keep_tab: bool = False) -> None:
        """Release this driver's owned lease, normally closing its tab first.

        ``keep_tab`` retains both page and lease for the user's review. Failed operations
        retain ownership for retry. Unowned tabs and sessions are never touched.
        """
        if self._tab is None:
            return
        if keep_tab:
            # OpenCLI close/unbind removes managed pages from their session. Keep
            # the lease with the recorded tab so review remains possible.
            return
        await self._call(self._argv(["tab", "close"], positionals=[self._tab], pin=False))
        self._tab = None
        await self._call(self._argv(["close"], pin=False))


class OpenCliApplicationBrowser(GenericApplicationBrowser):
    """``ApplicationBrowser`` driving one owned OpenCLI tab."""

    driver: OpenCliDriver

    def __init__(self, driver: OpenCliDriver, options: BrowserOptions, *,
                 settle_timeout_s: float, policy: ActionPolicy | None = None,
                 annotator: FormAnnotator | None = None,
                 schema_hint_loader: SchemaHintLoader | None = None) -> None:
        super().__init__(driver, options, settle_timeout_s=settle_timeout_s, policy=policy,
                         annotator=annotator, schema_hint_loader=schema_hint_loader)
        self.driver = driver
        self._keep_for_review = False

    @property
    def location(self) -> str:
        """Where the user finds this application's tab, for ``UserInteraction`` prompts."""
        cfg = self.driver.config
        return (f"Chrome profile {cfg.profile or '(default)'}, OpenCLI session {self.driver.session!r}, "
                f"tab {self.driver.tab or '(not opened)'} at {self.driver.url or 'no page yet'}")

    def keep_for_review(self) -> str:
        self._keep_for_review = True
        return self.location

    @property
    def waiting_for_person(self) -> bool:
        """The last page is waiting for the person to attach a file (the resume of a
        dialog wizard): the tab stays open for them."""
        last = self._last
        return last is not None and any(
            last.bindings[field_id].attach_by_person for field_id in last.unsupported_pending)

    async def close(self) -> None:
        await self.driver.release(keep_tab=self._keep_for_review or self.waiting_for_person)


class OpenCliSessionFactory:
    """``BrowserSessionFactory`` for the user-present OpenCLI path.

    ``start`` checks that OpenCLI and the configured profile respond (without
    opening anything); the tab is created by the first ``open``. ``BrowserOptions``
    ``profile_dir`` does not apply (the Chrome profile is the user's connected one);
    ``headless`` means "do not try to focus the window"."""

    def __init__(self, config: OpenCliConfig | None = None, *, runner: Runner | None = None,
                 settle_timeout_s: float = 15.0, policy: ActionPolicy | None = None,
                 annotator: FormAnnotator | None = None,
                 schema_hint_loader: SchemaHintLoader | None = None) -> None:
        self.config = config or OpenCliConfig()
        self.runner = runner
        self.settle_timeout_s = settle_timeout_s
        self.policy = policy
        self.annotator = annotator
        self.schema_hint_loader = schema_hint_loader

    async def start(self, options: BrowserOptions) -> OpenCliApplicationBrowser:
        driver = OpenCliDriver(self.config, runner=self.runner)
        argv = [self.config.executable]
        if self.config.profile:
            argv += ["--profile", self.config.profile]
        argv += ["browser", driver.session, "tab", "list"]
        try:
            await driver._call(argv, timeout_s=min(self.config.command_timeout_s, 20.0))
        except OpenCliError as exc:
            raise OpenCliUnavailable(
                f"OpenCLI is not ready for profile {self.config.profile or '(default)'}: {exc}. "
                "Run `opencli doctor`."
            ) from exc
        return OpenCliApplicationBrowser(driver, options, settle_timeout_s=self.settle_timeout_s,
                                         policy=self.policy, annotator=self.annotator,
                                         schema_hint_loader=self.schema_hint_loader)
