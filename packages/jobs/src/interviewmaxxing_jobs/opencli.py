"""Transport to the user's live Chrome through the installed OpenCLI Browser Bridge.

Every call is a structured argument array (never a shell string). Sessions are
OpenCLI *owned* sessions named ``imx-jobs-<source>``: this module never binds,
navigates or closes a tab it does not own, and refuses any other session name, so
the user's own tabs (for example ``imx-assessment-opencli``) cannot be touched.

``evaluate`` runs read-only extraction scripts shipped in ``interviewmaxxing_jobs/js``.
Clicks go through OpenCLI's structured ``click`` command and are used only to
select a search result in a results list; nothing here types into, submits or
clicks an application form.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Any, Literal, Protocol

SESSION_PATTERN = re.compile(r"^imx-jobs-[a-z0-9][a-z0-9-]*$")
DEFAULT_PROFILE = "jgd7jms9"
WindowMode = Literal["background", "foreground"]


class TransportError(RuntimeError):
    """A browser command failed (non-zero exit, timeout or unreadable output)."""


class BrowserTransport(Protocol):
    """What the source adapters need from a browser. Tests use a recorded fake."""

    def open(self, session: str, url: str) -> None: ...

    def wait_for(self, session: str, selector: str, timeout_ms: int) -> bool:
        """True once ``selector`` matches, False on timeout."""
        ...

    def pause(self, session: str, seconds: float) -> None: ...

    def evaluate(self, session: str, script: str) -> Any:
        """Run a read-only extraction script and return its JSON result."""
        ...

    def click(self, session: str, selector: str, nth: int | None = None) -> None:
        """Structured click on a search-result element (never a form submit)."""
        ...

    def close(self, session: str) -> None: ...


def check_session_name(session: str) -> str:
    if not SESSION_PATTERN.fullmatch(session):
        raise ValueError(
            f"refusing browser session {session!r}: job search only uses its own "
            "'imx-jobs-<source>' sessions"
        )
    return session


@cache
def _script_text(name: str) -> str:
    return (resources.files("interviewmaxxing_jobs") / "js" / f"{name}.js").read_text("utf-8")


def extraction_script(name: str) -> str:
    """The IIFE for ``js/<name>.js`` with the shared helpers prepended."""
    return f"(() => {{\n{_script_text('_common')}\n{_script_text(name)}\n}})()"


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass
class OpenCliTransport:
    """Drives ``opencli --profile <profile> browser <session> ...``."""

    profile: str = DEFAULT_PROFILE
    window: WindowMode = "background"
    executable: str = "opencli"
    timeout_s: float = 90.0
    runner: Runner = field(default=subprocess.run, repr=False)
    _opened: set[str] = field(default_factory=set, init=False, repr=False)

    def _run(self, session: str, args: Sequence[str], *, timeout_s: float | None = None) -> str:
        check_session_name(session)
        argv = [self.executable, "--profile", self.profile, "browser", session, *args]
        try:
            proc = self.runner(
                argv, capture_output=True, text=True, timeout=timeout_s or self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"opencli {args[0]} timed out after {exc.timeout}s") from exc
        except OSError as exc:
            raise TransportError(f"cannot run {self.executable!r}: {exc}") from exc
        if proc.returncode != 0:
            raise TransportError(_error_text(proc.stdout, proc.stderr, args[0]))
        return proc.stdout

    def open(self, session: str, url: str) -> None:
        args = ["open", url]
        if session not in self._opened:
            args += ["--window", self.window]
        self._run(session, args)
        self._opened.add(session)

    def wait_for(self, session: str, selector: str, timeout_ms: int) -> bool:
        try:
            self._run(session, ["wait", "selector", selector, "--timeout", str(timeout_ms)],
                      timeout_s=timeout_ms / 1000 + 30)
        except TransportError as exc:
            if "not found" in str(exc).lower() or "timeout" in str(exc).lower():
                return False
            raise
        return True

    def pause(self, session: str, seconds: float) -> None:
        self._run(session, ["wait", "time", f"{seconds:g}"], timeout_s=seconds + 30)

    def evaluate(self, session: str, script: str) -> Any:
        out = self._run(session, ["eval", script]).strip()
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise TransportError(f"extraction returned non-JSON output ({len(out)} chars)") from exc

    def click(self, session: str, selector: str, nth: int | None = None) -> None:
        args = ["click", selector]
        if nth is not None:
            args += ["--nth", str(nth)]
        out = self._run(session, args)
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError:
            return
        if isinstance(envelope, dict) and "error" in envelope:
            raise TransportError(f"click failed: {envelope['error'].get('code', 'error')}")

    def close(self, session: str) -> None:
        try:
            self._run(session, ["close"], timeout_s=30)
        finally:
            self._opened.discard(session)


def _error_text(stdout: str, stderr: str, command: str) -> str:
    for text in (stdout, stderr):
        text = text.strip()
        if not text:
            continue
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(envelope, dict) and isinstance(envelope.get("error"), dict):
                err = envelope["error"]
                return f"opencli {command}: {err.get('code', 'error')}: {err.get('message', '')}"
        lines = [ln.strip() for ln in text.splitlines()
                 if ln.strip() and "update available" not in ln.lower()
                 and "npm install" not in ln.lower()]
        if lines:
            return f"opencli {command}: {lines[0].lstrip('✖ ').strip()}"
    return f"opencli {command} failed"
