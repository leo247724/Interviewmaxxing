"""CAPTCHA solving through the person's 2Captcha account (WP1 round 14).

Off unless asked for (``--captcha-solver 2captcha`` or ``IMX_CAPTCHA_SOLVER=2captcha``) and
unless ``TWOCAPTCHA_API_KEY`` is configured beside the OpenRouter key (the env file named by
``--env-file`` or ``IMX_OPENROUTER_ENV_FILE``, or the process environment). Without a key
the solver is simply off: every CAPTCHA stops the run as it always did ("Solve the
CAPTCHA").

- :data:`CAPTCHA_DETECT` is a fixed read-only page script: the reCAPTCHA v2 (checkbox and
  invisible), reCAPTCHA v3, hCaptcha and Cloudflare Turnstile widgets on the page, each
  with its site key, whether it is already answered, and its callback name.
- :class:`TwoCaptcha` asks the 2Captcha REST API (``createTask``, then ``getTaskResult``
  every few seconds, at most two minutes) for a token. The key goes only into request
  bodies; it is never logged, and error messages carry 2Captcha's error code only.
- :class:`CaptchaBudget` caps the spend: per run, or per batch through a ledger file in
  the batch directory that every worker shares (locked appends). A solve is reserved at
  a fixed estimate before its task is created and settled at the cost 2Captcha reports.
- :data:`CAPTCHA_INJECT` (Playwright only: it writes to the page) puts the token into the
  widget's response fields and, when asked, calls the widget's callback. Nothing here
  clicks a submit control; a callback is only called where no application form could be
  sent by it (a CAPTCHA page in front of the form).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from interviewmaxxing_selection.credentials import ApiKey, load_optional_key

TWOCAPTCHA_KEY_NAME = "TWOCAPTCHA_API_KEY"
SOLVER_ENV = "IMX_CAPTCHA_SOLVER"
"""``2captcha`` turns the solver on for commands that do not say ``--captcha-solver``."""
SOLVERS = ("off", "2captcha")
DEFAULT_BUDGET_USD = 2.00
API_BASE = "https://api.2captcha.com"
ESTIMATE_USD = 0.003
"""What a solve is reserved at before its cost is known (2Captcha lists USD 1-3 per 1000
reCAPTCHA, hCaptcha or Turnstile tokens)."""
SUPPORTED_KINDS = ("recaptcha_v2", "recaptcha_v3", "hcaptcha", "turnstile")

CaptchaKind = Literal["recaptcha_v2", "recaptcha_v3", "hcaptcha", "turnstile"]
Outcome = Literal["solved", "not_accepted", "unsupported", "over_budget", "timeout", "error"]


# --- the page -------------------------------------------------------------------------

CAPTCHA_DETECT = r"""() => {
  const out = [];
  const seen = new Set();
  const answered = (names) => Array.from(document.querySelectorAll(names.map((n) => '[name="' + n + '"]').join(',')))
    .some((t) => !!(t.value || '').trim());
  const param = (src, name) => {
    try {
      const u = new URL(src, location.href);
      return u.searchParams.get(name) || new URLSearchParams(u.hash.replace(/^#/, '')).get(name) || '';
    } catch (e) { return ''; }
  };
  const add = (w) => {
    const key = w.kind + ':' + w.site_key;
    if (!w.site_key || seen.has(key)) return;
    seen.add(key);
    out.push(w);
  };
  // reCAPTCHA v3 (and Enterprise): api.js?render=<site key>, no widget of its own.
  const v3 = new Set();
  for (const s of document.querySelectorAll('script[src]')) {
    if (!/recaptcha\/(?:api|enterprise)\.js/.test(s.src)) continue;
    const render = param(s.src, 'render');
    if (render && render !== 'explicit' && render !== 'onload') v3.add(render + (s.src.includes('enterprise') ? '|e' : ''));
  }
  const actionOf = (el) => (el && (el.getAttribute('data-action') || el.getAttribute('data-recaptcha-action'))) ||
    ((document.querySelector('[data-recaptcha-action]') || {getAttribute: () => ''}).getAttribute('data-recaptcha-action') || '');
  for (const el of document.querySelectorAll('[data-sitekey]')) {
    const key = el.getAttribute('data-sitekey') || '';
    const cls = (el.getAttribute('class') || '') + ' ' + ((el.closest('.h-captcha,.cf-turnstile,.g-recaptcha') || {}).className || '');
    const submit = el.matches('button, input[type="submit"]');
    const callback = el.getAttribute('data-callback') || '';
    if (/\bh-captcha\b/.test(cls)) {
      add({kind: 'hcaptcha', site_key: key, invisible: el.getAttribute('data-size') === 'invisible', action: '',
           enterprise: false, callback, submit_bound: submit, answered: answered(['h-captcha-response'])});
    } else if (/\bcf-turnstile\b/.test(cls)) {
      add({kind: 'turnstile', site_key: key, invisible: false, action: el.getAttribute('data-action') || '',
           enterprise: false, callback, submit_bound: submit, answered: answered(['cf-turnstile-response'])});
    } else {
      const isV3 = v3.has(key) || v3.has(key + '|e');
      add({kind: isV3 ? 'recaptcha_v3' : 'recaptcha_v2', site_key: key,
           invisible: el.getAttribute('data-size') === 'invisible' || el.classList.contains('g-recaptcha') && submit,
           action: isV3 ? actionOf(el) : '', enterprise: v3.has(key + '|e') || !!document.querySelector('script[src*="recaptcha/enterprise.js"]'),
           callback, submit_bound: submit, answered: answered(['g-recaptcha-response'])});
    }
  }
  for (const f of document.querySelectorAll('iframe[src]')) {
    const src = f.src;
    if (/recaptcha\/(?:api2|enterprise)\/anchor/.test(src)) {
      const key = param(src, 'k');
      const isV3 = v3.has(key) || v3.has(key + '|e');
      add({kind: isV3 ? 'recaptcha_v3' : 'recaptcha_v2', site_key: key, invisible: param(src, 'size') === 'invisible',
           action: isV3 ? actionOf(null) : '', enterprise: /\/enterprise\//.test(src), callback: '', submit_bound: false,
           answered: answered(['g-recaptcha-response'])});
    } else if (/hcaptcha\.com/.test(src)) {
      add({kind: 'hcaptcha', site_key: param(src, 'sitekey'), invisible: false, action: '', enterprise: false,
           callback: '', submit_bound: false, answered: answered(['h-captcha-response'])});
    } else if (/challenges\.cloudflare\.com/.test(src)) {
      const m = src.match(/\/(0x[0-9A-Za-z_-]{10,})(?:\/|$)/);
      add({kind: 'turnstile', site_key: m ? m[1] : '', invisible: false, action: '', enterprise: false,
           callback: '', submit_bound: false, answered: answered(['cf-turnstile-response'])});
    }
  }
  for (const entry of v3) {
    const [key, e] = entry.split('|');
    add({kind: 'recaptcha_v3', site_key: key, invisible: true, action: actionOf(null), enterprise: e === 'e',
         callback: '', submit_bound: false, answered: answered(['g-recaptcha-response'])});
  }
  return {url: location.href, widgets: out};
}"""
"""Read-only: the CAPTCHA widgets on the page (``{url, widgets: [...]}``)."""

CAPTCHA_INJECT = r"""({kind, token, callback, call_callback}) => {
  const names = kind === 'hcaptcha' ? ['h-captcha-response', 'g-recaptcha-response']
    : kind === 'turnstile' ? ['cf-turnstile-response'] : ['g-recaptcha-response'];
  let fields = 0;
  for (const name of names) {
    for (const el of document.querySelectorAll('[name="' + name + '"], #' + name)) {
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      setter.call(el, token);
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      fields++;
    }
  }
  // reCAPTCHA v3 and invisible v2 hand the page its token through execute(): it now
  // resolves to this one (the site's own script still decides when to call it).
  if (kind.startsWith('recaptcha') && window.grecaptcha) {
    const resolved = () => Promise.resolve(token);
    try { window.grecaptcha.execute = resolved; } catch (e) {}
    try { if (window.grecaptcha.enterprise) window.grecaptcha.enterprise.execute = resolved; } catch (e) {}
    try { window.grecaptcha.getResponse = () => token; } catch (e) {}
  }
  let called = 0;
  if (call_callback) {
    const byName = (name) => name.split('.').reduce((o, k) => (o == null ? undefined : o[k]), window);
    const call = (fn) => { if (typeof fn === 'function') { fn(token); called++; } };
    if (callback) {
      call(byName(callback));
    } else if (kind.startsWith('recaptcha') && window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
      const walk = (node, depth) => {
        if (!node || typeof node !== 'object' || depth > 5) return;
        for (const k of Object.keys(node)) {
          const v = node[k];
          if (k === 'callback' && typeof v === 'function') call(v);
          else if (k === 'callback' && typeof v === 'string') call(byName(v));
          else if (v && typeof v === 'object') walk(v, depth + 1);
        }
      };
      for (const c of Object.values(window.___grecaptcha_cfg.clients)) walk(c, 0);
    }
  }
  return {fields, called};
}"""
"""Writes to the page (Playwright only): the token into the widget's response fields and,
with ``call_callback``, the widget's callback called with it."""


@dataclass(frozen=True, slots=True)
class CaptchaWidget:
    kind: CaptchaKind
    site_key: str
    invisible: bool = False
    action: str = ""
    enterprise: bool = False
    callback: str = ""
    submit_bound: bool = False
    """The widget is the form's submit button (an invisible reCAPTCHA bound to it)."""
    answered: bool = False

    @classmethod
    def parse(cls, raw: Any) -> CaptchaWidget | None:
        if not isinstance(raw, Mapping) or raw.get("kind") not in SUPPORTED_KINDS:
            return None
        key = str(raw.get("site_key") or "").strip()
        if not key:
            return None
        return cls(kind=raw["kind"], site_key=key, invisible=bool(raw.get("invisible")),
                   action=str(raw.get("action") or ""), enterprise=bool(raw.get("enterprise")),
                   callback=str(raw.get("callback") or ""), submit_bound=bool(raw.get("submit_bound")),
                   answered=bool(raw.get("answered")))


def parse_detection(raw: Any) -> tuple[str, list[CaptchaWidget]]:
    """(page URL, widgets) from :data:`CAPTCHA_DETECT`."""
    if not isinstance(raw, Mapping):
        return "", []
    widgets = [w for w in (CaptchaWidget.parse(item) for item in raw.get("widgets") or []) if w is not None]
    return str(raw.get("url") or ""), widgets


# --- the 2Captcha API ---------------------------------------------------------------


class CaptchaTransport(Protocol):
    async def post(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """POST ``payload`` as JSON and return the JSON response."""
        ...


class UrllibTransport:
    """The real transport: JSON over HTTPS with the standard library, off the event loop."""

    def __init__(self, timeout_s: float = 30.0) -> None:
        self.timeout_s = timeout_s

    async def post(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._post, url, dict(payload))

    def _post(self, url: str, payload: dict[str, Any]) -> Mapping[str, Any]:
        request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise CaptchaError("MALFORMED_RESPONSE")
        return data


class CaptchaError(Exception):
    """2Captcha refused or failed a task. The message is its error code (never the key)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CaptchaSolution:
    token: str
    cost_usd: float | None
    seconds: float

    def __repr__(self) -> str:  # never the token
        return f"CaptchaSolution(token=<{len(self.token)} chars>, cost_usd={self.cost_usd}, seconds={self.seconds:.1f})"


def task_for(widget: CaptchaWidget, page_url: str) -> dict[str, Any]:
    """The 2Captcha task for one widget (proxyless: solved from 2Captcha's own network)."""
    if widget.kind == "recaptcha_v2":
        task: dict[str, Any] = {"type": ("RecaptchaV2EnterpriseTaskProxyless" if widget.enterprise
                                         else "RecaptchaV2TaskProxyless"),
                                "websiteURL": page_url, "websiteKey": widget.site_key}
        if widget.invisible:
            task["isInvisible"] = True
        return task
    if widget.kind == "recaptcha_v3":
        task = {"type": "RecaptchaV3TaskProxyless", "websiteURL": page_url,
                "websiteKey": widget.site_key, "minScore": 0.7, "pageAction": widget.action or "submit"}
        if widget.enterprise:
            task["isEnterprise"] = True
        return task
    if widget.kind == "hcaptcha":
        return {"type": "HCaptchaTaskProxyless", "websiteURL": page_url, "websiteKey": widget.site_key}
    task = {"type": "TurnstileTaskProxyless", "websiteURL": page_url, "websiteKey": widget.site_key}
    if widget.action:
        task["action"] = widget.action
    return task


@dataclass
class TwoCaptcha:
    """The 2Captcha REST client (API v2: ``createTask`` / ``getTaskResult``)."""

    key: ApiKey
    transport: CaptchaTransport = field(default_factory=UrllibTransport)
    timeout_s: float = 120.0
    first_poll_s: float = 10.0
    poll_s: float = 5.0
    base_url: str = API_BASE
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic

    async def _call(self, method: str, body: dict[str, Any]) -> Mapping[str, Any]:
        try:
            data = await self.transport.post(f"{self.base_url}/{method}",
                                             {"clientKey": self.key.reveal(), **body})
        except CaptchaError:
            raise
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as exc:
            raise CaptchaError(f"NETWORK_{type(exc).__name__.upper()}") from None
        if not isinstance(data, Mapping):
            raise CaptchaError("MALFORMED_RESPONSE")
        if (data.get("errorId") or 0) not in (0, "0"):
            # Only the code's own shape is kept: it goes into the run's events.
            code = re.sub(r"[^A-Z0-9_]", "", str(data.get("errorCode") or "").upper())[:64]
            raise CaptchaError(code or "ERROR_UNKNOWN")
        return data

    async def solve(self, widget: CaptchaWidget, page_url: str) -> CaptchaSolution:
        """A token for ``widget`` on ``page_url``; ``CaptchaError`` (``TIMEOUT`` after
        ``timeout_s``) otherwise."""
        started = self.clock()
        created = await self._call("createTask", {"task": task_for(widget, page_url)})
        task_id = created.get("taskId")
        if task_id is None:
            raise CaptchaError("NO_TASK_ID")
        wait = self.first_poll_s
        while True:
            left = self.timeout_s - (self.clock() - started)
            if left <= 0:
                raise CaptchaError("TIMEOUT")
            await self.sleep(min(wait, left))
            wait = self.poll_s
            result = await self._call("getTaskResult", {"taskId": task_id})
            if result.get("status") != "ready":
                continue
            solution = result.get("solution") or {}
            token = str(solution.get("gRecaptchaResponse") or solution.get("token") or "").strip()
            if not token:
                raise CaptchaError("EMPTY_SOLUTION")
            try:
                cost = float(result["cost"]) if result.get("cost") is not None else None
            except (TypeError, ValueError):
                cost = None
            return CaptchaSolution(token=token, cost_usd=cost, seconds=self.clock() - started)


# --- the spend cap ------------------------------------------------------------------


class CaptchaBudget:
    """A spend cap in USD, per run or (``ledger``: a JSON-lines file every worker of a batch
    appends to, under a lock) per batch. :meth:`reserve` holds ``ESTIMATE_USD`` before a
    task is created; :meth:`settle` replaces it by the reported cost."""

    def __init__(self, cap_usd: float, ledger: Path | None = None) -> None:
        self.cap_usd = max(0.0, float(cap_usd))
        self.ledger = ledger
        self._entries: dict[str, float] = {}

    @contextlib.contextmanager
    def _locked(self) -> Any:
        assert self.ledger is not None
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                yield handle
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _sum(lines: list[str]) -> float:
        held: dict[str, float] = {}
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                held[entry["id"]] = float(entry.get("usd") or 0.0)
        return sum(held.values())

    def spent(self) -> float:
        if self.ledger is None:
            return sum(self._entries.values())
        with self._locked() as handle:
            return self._sum(handle.read().splitlines())

    def reserve(self, estimate: float = ESTIMATE_USD) -> str | None:
        """A reservation id, or None when ``estimate`` more would pass the cap."""
        reservation = uuid.uuid4().hex
        if self.ledger is None:
            if sum(self._entries.values()) + estimate > self.cap_usd + 1e-9:
                return None
            self._entries[reservation] = estimate
            return reservation
        with self._locked() as handle:
            if self._sum(handle.read().splitlines()) + estimate > self.cap_usd + 1e-9:
                return None
            handle.write(json.dumps({"id": reservation, "usd": estimate, "state": "reserved"}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return reservation

    def settle(self, reservation: str, cost_usd: float | None) -> None:
        """The reservation's actual cost (None: 2Captcha did not report one; the estimate
        stays counted)."""
        usd = cost_usd if cost_usd is not None else ESTIMATE_USD
        if self.ledger is None:
            self._entries[reservation] = usd
            return
        with self._locked() as handle:
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps({"id": reservation, "usd": usd, "state": "settled"}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


# --- the solver ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptchaAttempt:
    """One attempt at a CAPTCHA, as the run records it: never the token or the key."""

    outcome: Outcome
    kind: str = ""
    seconds: float = 0.0
    cost_usd: float | None = None
    detail: str = ""
    task_created: bool = False
    """2Captcha was asked (a cost may be recorded)."""

    def record(self, page_url: str = "") -> dict[str, Any]:
        host = (urlsplit(page_url).hostname or "") if page_url else ""
        return {"kind": self.kind, "outcome": self.outcome, "seconds": round(self.seconds, 1),
                "cost_usd": self.cost_usd, "detail": self.detail, "site": host}


@dataclass
class CaptchaSolver:
    """What a run solves CAPTCHAs with: the client, the spend cap and a bound on attempts."""

    client: TwoCaptcha
    budget: CaptchaBudget
    max_attempts: int = 3
    attempts: int = 0

    async def token(self, widget: CaptchaWidget, page_url: str) -> tuple[CaptchaAttempt, str | None]:
        """Ask 2Captcha for ``widget``'s token within the cap: (the attempt, the token)."""
        if self.attempts >= self.max_attempts:
            return CaptchaAttempt("unsupported", widget.kind, detail="too many CAPTCHA attempts in this run"), None
        reservation = self.budget.reserve()
        if reservation is None:
            return CaptchaAttempt("over_budget", widget.kind,
                                  detail=f"the CAPTCHA budget of USD {self.budget.cap_usd:.2f} is spent"), None
        self.attempts += 1
        started = self.client.clock()
        try:
            solution = await self.client.solve(widget, page_url)
        except CaptchaError as exc:
            self.budget.settle(reservation, 0.0)  # 2Captcha charges solved tasks only
            outcome: Outcome = "timeout" if exc.code == "TIMEOUT" else "error"
            return CaptchaAttempt(outcome, widget.kind, seconds=self.client.clock() - started,
                                  cost_usd=0.0, detail=exc.code, task_created=True), None
        self.budget.settle(reservation, solution.cost_usd)
        return CaptchaAttempt("solved", widget.kind, seconds=solution.seconds, cost_usd=solution.cost_usd,
                              task_created=True), solution.token


def solver_choice(flag: str | None, environ: Mapping[str, str] | None = None) -> str:
    """``--captcha-solver`` as given, else ``IMX_CAPTCHA_SOLVER``, else ``off``."""
    env = os.environ if environ is None else environ
    value = (flag or env.get(SOLVER_ENV) or "off").strip().lower()
    return value if value in SOLVERS else "off"


def build_solver(choice: str, *, budget_usd: float = DEFAULT_BUDGET_USD, env_file: Path | None = None,
                 ledger: Path | None = None, transport: CaptchaTransport | None = None,
                 environ: Mapping[str, str] | None = None) -> CaptchaSolver | None:
    """The run's solver, or None: the solver is off, or ``TWOCAPTCHA_API_KEY`` is configured
    nowhere (then every CAPTCHA stops the run exactly as without the solver)."""
    if choice != "2captcha":
        return None
    key = load_optional_key(TWOCAPTCHA_KEY_NAME, env_file, environ=environ)
    if key is None:
        return None
    client = TwoCaptcha(key=key, transport=transport or UrllibTransport())
    return CaptchaSolver(client=client, budget=CaptchaBudget(budget_usd, ledger))
