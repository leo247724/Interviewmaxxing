"""Round 14: CAPTCHAs solved through 2Captcha, behind a flag and a spend cap.

Every test uses a fake 2Captcha transport (``FakeTwoCaptcha``) and the local mock ATS
(``captcha-gate``, ``captcha-form``, ``captcha-steps``); nothing leaves the process, no
real key or env file is read, and IMX_HOME is a temporary directory. The fake answers a
task with ``fixture-solved:<websiteKey>``, the only token the mock accepts.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.captcha import (
    CAPTCHA_DETECT,
    CaptchaBudget,
    CaptchaSolver,
    TwoCaptcha,
    parse_detection,
)
from interviewmaxxing_cli.runner import LocalApplicationRunner, NoninteractiveInteraction
from interviewmaxxing_core import (
    ApplicationState,
    ApplicationStore,
    BrowserOptions,
    LocalPaths,
    PageKind,
    SubmissionOutcome,
)
from interviewmaxxing_selection.credentials import ApiKey

REPO = Path(__file__).resolve().parents[2]
RESUME_PATH = REPO / "tests" / "fixtures" / "browser" / "resume_avery_quill.pdf"
VERIFIED_AT = "2026-09-01T12:00:00Z"
KEY = ApiKey("fixture-2captcha-key-0001", source="test", name="TWOCAPTCHA_API_KEY")
GATE = "/jobs/captcha-gate/apply"
FORM = "/jobs/captcha-form/apply"
STEPS = "/jobs/captcha-steps/apply"


class FakeTwoCaptcha:
    """The 2Captcha API as a test double: ``createTask`` then ``getTaskResult`` (``pending``
    "processing" answers first). ``error``: the errorCode createTask answers with;
    ``never_ready``: every poll answers "processing"."""

    def __init__(self, *, pending: int = 1, cost: str | None = "0.00145", error: str | None = None,
                 never_ready: bool = False, wrong_token: bool = False) -> None:
        self.pending = pending
        self.cost = cost
        self.error = error
        self.never_ready = never_ready
        self.wrong_token = wrong_token
        """Answer with a token the site refuses (a solve the site does not take)."""
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tasks: dict[int, dict[str, Any]] = {}
        self.polls: dict[int, int] = {}

    async def post(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        assert payload["clientKey"] == KEY.reveal()  # the key goes only into request bodies
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, {k: v for k, v in payload.items() if k != "clientKey"}))
        if method == "createTask":
            if self.error:
                return {"errorId": 1, "errorCode": self.error, "errorDescription": "fixture"}
            task_id = len(self.tasks) + 101
            self.tasks[task_id] = dict(payload["task"])
            return {"errorId": 0, "taskId": task_id}
        task_id = payload["taskId"]
        self.polls[task_id] = self.polls.get(task_id, 0) + 1
        if self.never_ready or self.polls[task_id] <= self.pending:
            return {"errorId": 0, "status": "processing"}
        token = f"fixture-solved:{'wrong' if self.wrong_token else self.tasks[task_id]['websiteKey']}"
        result: dict[str, Any] = {"errorId": 0, "status": "ready",
                                  "solution": {"gRecaptchaResponse": token, "token": token}}
        if self.cost is not None:
            result["cost"] = self.cost
        return result

    def created(self) -> list[dict[str, Any]]:
        return [body["task"] for method, body in self.calls if method == "createTask"]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _solver(fake: FakeTwoCaptcha, *, budget: float = 2.0, ledger: Path | None = None,
            timeout_s: float = 120.0) -> CaptchaSolver:
    clock = Clock()
    client = TwoCaptcha(key=KEY, transport=fake, timeout_s=timeout_s, first_poll_s=10.0, poll_s=5.0,
                        sleep=clock.sleep, clock=clock)
    return CaptchaSolver(client=client, budget=CaptchaBudget(budget, ledger))


async def _open(options: BrowserOptions, url: str) -> Any:
    browser = await PlaywrightSessionFactory().start(options)
    await browser.open(url)
    return browser


# --- detection ----------------------------------------------------------------------------

@pytest.mark.parametrize(("path", "kind", "invisible", "callback", "submit_bound"), [
    (f"{GATE}?kind=recaptcha-v2", "recaptcha_v2", False, "bwaCaptchaPassed", False),
    (f"{GATE}?kind=recaptcha-v2-invisible", "recaptcha_v2", True, "bwaCaptchaPassed", False),
    (f"{GATE}?kind=hcaptcha", "hcaptcha", False, "bwaCaptchaPassed", False),
    (f"{GATE}?kind=turnstile", "turnstile", False, "bwaCaptchaPassed", False),
    (f"{FORM}?kind=recaptcha-v3", "recaptcha_v3", True, "", False),
    (f"{FORM}?kind=recaptcha-v2-submit", "recaptcha_v2", True, "bwaSubmitWithToken", True),
], ids=["recaptcha-v2", "recaptcha-v2-invisible", "hcaptcha", "turnstile", "recaptcha-v3", "submit-bound"])
def test_each_widget_is_detected_with_its_site_key(
    path: str, kind: str, invisible: bool, callback: str, submit_bound: bool,
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> Any:
        browser = await _open(options, server.url(path))
        try:
            return parse_detection(await browser.page.evaluate(CAPTCHA_DETECT))
        finally:
            await browser.close()

    url, widgets = kit.run(scenario())
    expected = kit.mock_ats.SOLVABLE_CAPTCHAS[path.split("kind=")[1]]["key"]
    assert url.startswith(server.url(path.split("?")[0]))
    [widget] = widgets
    assert (widget.kind, widget.site_key, widget.invisible, widget.callback, widget.submit_bound) == (
        kind, expected, invisible, callback, submit_bound)
    assert not widget.answered


# --- a CAPTCHA page in front of the form ---------------------------------------------------

@pytest.mark.parametrize("kind", ["recaptcha-v2", "recaptcha-v2-invisible", "hcaptcha", "turnstile"])
def test_a_captcha_page_is_solved_and_leads_to_the_form(
    kind: str, kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    fake = FakeTwoCaptcha(pending=2)
    solver = _solver(fake)

    async def scenario() -> tuple[Any, Any, str, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url(f"{GATE}?kind={kind}"))
            attempt, url, after = await browser.solve_captcha(solver, call_callback=True)
            return page, attempt, url, after
        finally:
            await browser.close()

    page, attempt, url, after = kit.run(scenario())
    assert page.kind is PageKind.CAPTCHA
    assert url.startswith(server.url(GATE))  # the page the token was solved for
    assert attempt.outcome == "solved" and attempt.cost_usd == 0.00145 and attempt.task_created
    assert attempt.seconds >= 20.0  # the first poll after 10 s, then every 5 s
    assert after is not None and after.kind is PageKind.APPLICATION_FORM
    [task] = fake.created()
    assert task["websiteKey"] == kit.mock_ats.SOLVABLE_CAPTCHAS[kind]["key"]
    assert task["websiteURL"].startswith(server.url(GATE))
    assert task.get("isInvisible", False) is (kind == "recaptcha-v2-invisible")
    assert solver.budget.spent() == pytest.approx(0.00145)
    assert "fixture-solved" not in json.dumps(attempt.record())  # never the token


def test_a_captcha_page_that_leads_on_late_is_waited_for(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """The callback posts the token, and the page reloads into the form 9 s after the
    site's answer, later than the page settles: the solve waits for it (at most
    ``_CAPTCHA_PASS_S``)."""
    solver = _solver(FakeTwoCaptcha())

    async def scenario() -> tuple[Any, Any]:
        browser = await _open(options, server.url(f"{GATE}?kind=turnstile&delay_ms=9000"))
        try:
            attempt, _, after = await browser.solve_captcha(solver, call_callback=True)
            return attempt, after
        finally:
            await browser.close()

    attempt, after = kit.run(scenario())
    assert attempt.outcome == "solved"
    assert after is not None and after.kind is PageKind.APPLICATION_FORM


def test_a_captcha_page_that_also_holds_a_form_is_left_to_the_person(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """The widget's callback could send the page's form, so it is not called, and without
    it the token would not be handed over: 2Captcha is not asked and nothing is spent."""
    fake = FakeTwoCaptcha()
    solver = _solver(fake)

    async def scenario() -> tuple[Any, Any, bool]:
        browser = await _open(options, server.url(f"{GATE}?kind=recaptcha-v2&form=email"))
        try:
            attempt, _, after = await browser.solve_captcha(solver, call_callback=True)
            return attempt, after, await browser.page.evaluate("window.__bwaCallbackCalled === true")
        finally:
            await browser.close()

    attempt, after, called = kit.run(scenario())
    assert attempt.outcome == "unsupported" and "also holds a form" in attempt.detail
    assert after is None and not called
    assert fake.calls == [] and solver.budget.spent() == 0


# --- a CAPTCHA on the form: its field only, then the gated submit sends it -------------------

@pytest.mark.parametrize("kind", ["recaptcha-v2", "recaptcha-v2-invisible", "recaptcha-v3", "hcaptcha", "turnstile"])
def test_a_captcha_on_the_form_is_answered_in_its_field_and_the_submit_is_accepted(
    kind: str, kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    fake = FakeTwoCaptcha()
    solver = _solver(fake)

    async def scenario() -> tuple[Any, Any, Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url(f"{FORM}?kind={kind}"))
            assert page.kind is PageKind.APPLICATION_FORM and page.captcha_pending
            fill = await browser.fill(page.form, kit.build(page.form, kit.pick(page.form, kit.CORE)).packet)
            assert fill.ok, fill.fields
            refused = await browser.submit()  # the unsolved CAPTCHA blocks the submit, as before
            assert not refused.dispatched and "CAPTCHA" in (refused.detail or "")
            attempt, _, after = await browser.solve_captcha(solver, call_callback=False)
            action = await browser.submit()
            return attempt, after, action, await browser.confirm()
        finally:
            await browser.close()

    before = server.submissions("captcha-form")["accepted_count"]
    attempt, after, action, observation = kit.run(scenario())
    assert attempt.outcome == "solved"
    assert after is not None and not after.captcha_pending
    assert action.dispatched and observation.outcome is SubmissionOutcome.ACCEPTED
    assert server.submissions("captcha-form")["accepted_count"] == before + 1


def test_an_invisible_recaptcha_bound_to_the_submit_button_is_left_to_the_person(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """Its callback sends the form, so it is never solved outside a CAPTCHA page: 2Captcha
    is not asked and nothing is spent."""
    fake = FakeTwoCaptcha()
    solver = _solver(fake)

    async def scenario() -> Any:
        browser = await _open(options, server.url(f"{FORM}?kind=recaptcha-v2-submit"))
        try:
            attempt, _, after = await browser.solve_captcha(solver, call_callback=False)
            assert after is None
            return attempt
        finally:
            await browser.close()

    attempt = kit.run(scenario())
    assert attempt.outcome == "unsupported" and "callback" in attempt.detail
    assert fake.calls == [] and solver.budget.spent() == 0


def test_a_session_that_cannot_write_to_the_page_never_asks_2captcha(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """OpenCLI runs only fixed read-only scripts: the CAPTCHA stays the person's."""
    fake = FakeTwoCaptcha()
    solver = _solver(fake)

    async def scenario() -> Any:
        browser = await _open(options, server.url(f"{GATE}?kind=recaptcha-v2"))
        try:
            browser.driver.injects_captcha_tokens = False  # as OpenCliDriver
            attempt, _, after = await browser.solve_captcha(solver, call_callback=True)
            assert after is None
            return attempt
        finally:
            await browser.close()

    attempt = kit.run(scenario())
    assert attempt.outcome == "unsupported" and fake.calls == []
    assert (attempt.kind, attempt.detail) == (
        "recaptcha_v2", "this browser session cannot put a CAPTCHA token into the page")


def test_opencli_may_detect_but_never_write() -> None:
    """The detection script is read-only (OpenCLI's lint passes it and it is allowlisted);
    the injection script writes, so OpenCLI never runs it and says it cannot."""
    from interviewmaxxing_browser.captcha import CAPTCHA_INJECT
    from interviewmaxxing_browser.driver import DriverError, PlaywrightDriver
    from interviewmaxxing_browser.opencli import _ALLOWED_SCRIPTS, OpenCliDriver, _lint_read_script

    _lint_read_script(CAPTCHA_DETECT)
    assert CAPTCHA_DETECT in _ALLOWED_SCRIPTS and CAPTCHA_INJECT not in _ALLOWED_SCRIPTS
    with pytest.raises(DriverError):
        _lint_read_script(CAPTCHA_INJECT)
    assert OpenCliDriver.injects_captcha_tokens is False
    assert PlaywrightDriver.injects_captcha_tokens is True


# --- the runner (preparation only) ---------------------------------------------------------

def _write_profile(paths: LocalPaths) -> None:
    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RESUME_PATH, directory / "resume.pdf")
    profile = {
        "id": "default",
        "identity": {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
                     "phone": "+1 (303) 555-0142",
                     "linkedin_url": "https://www.linkedin.example.test/in/avery-quill",
                     "verified_at": VERIFIED_AT},
        "resume": {"id": "resume_supplied", "path": "resume.pdf"},
        "facts": [],
        "saved_answers": [
            {"id": "sa.work_authorization", "scope": "GLOBAL", "semantic_type": "WORK_AUTHORIZATION",
             "question": "Are you legally authorized to work in the United States?",
             "value": "Yes, I am authorized to work in the US", "confirmed_at": VERIFIED_AT},
            {"id": "sa.sponsorship", "scope": "GLOBAL", "semantic_type": "SPONSORSHIP",
             "question": "Will you now or in the future require visa sponsorship?",
             "value": "No, I will not require sponsorship", "confirmed_at": VERIFIED_AT},
        ],
    }
    (directory / "profile.json").write_text(json.dumps(profile, indent=2))


class ReadOnlySessions:
    """Playwright sessions that, like OpenCLI, cannot write to the page."""

    async def start(self, options: BrowserOptions) -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        browser.driver.injects_captcha_tokens = False
        return browser


def _run(kit: SimpleNamespace, paths: LocalPaths, url: str, solver: CaptchaSolver | None,
         browser_factory: Any = None) -> Any:
    runner = LocalApplicationRunner(paths=paths, interaction=NoninteractiveInteraction(), headless=True,
                                    prepare_only=True, captcha_solver=solver, browser_factory=browser_factory)
    return kit.run(runner.apply(url, candidate_id="default"))


def _events(paths: LocalPaths, app_id: str, name: str) -> list[dict[str, Any]]:
    with ApplicationStore.open(paths.state_db) as store:
        return [e.metadata for e in store.list_events(app_id) if e.event == name]


def test_the_runner_solves_a_captcha_page_and_prepares_the_form(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home)
    fake = FakeTwoCaptcha()
    outcome = _run(kit, isolated_imx_home, server.url(f"{GATE}?kind=hcaptcha"), _solver(fake))
    assert outcome.state is ApplicationState.NEEDS_INPUT, outcome.message
    assert "Prepared to the final review step" in outcome.message, outcome.message
    [solve] = _events(isolated_imx_home, outcome.application_id, "captcha.solve")
    assert solve == {"kind": "hcaptcha", "outcome": "solved", "seconds": 15.0, "cost_usd": 0.00145,
                     "detail": "", "site": "127.0.0.1"}
    budget = [e for e in _events(isolated_imx_home, outcome.application_id, "provider.budget")
              if "captcha" in e.get("by_purpose", {})]
    assert budget and budget[0]["known_cost_usd"] == 0.00145 and budget[0]["calls"] == 1
    assert "Provider cost: USD 0.0014" in outcome.message  # 0.00145 at four decimals
    assert server.submissions("captcha-gate")["accepted_count"] == 0  # nothing submitted


def test_without_the_solver_a_captcha_page_stops_the_run_as_before(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home)
    outcome = _run(kit, isolated_imx_home, server.url(f"{GATE}?kind=turnstile"), None)
    assert outcome.state is ApplicationState.NEEDS_INPUT
    assert [m.label for m in outcome.missing_inputs] == ["Solve the CAPTCHA"]
    assert _events(isolated_imx_home, outcome.application_id, "captcha.solve") == []


def test_a_session_that_cannot_write_to_the_page_stops_the_run_as_before(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    """OpenCLI: the solver is on, but the CAPTCHA page stays the person's (NEEDS_INPUT) and
    2Captcha is never asked."""
    _write_profile(isolated_imx_home)
    fake = FakeTwoCaptcha()
    outcome = _run(kit, isolated_imx_home, server.url(f"{GATE}?kind=recaptcha-v2"), _solver(fake),
                   browser_factory=ReadOnlySessions())
    assert outcome.state is ApplicationState.NEEDS_INPUT
    assert [m.label for m in outcome.missing_inputs] == ["Solve the CAPTCHA"]
    assert fake.calls == []
    [solve] = _events(isolated_imx_home, outcome.application_id, "captcha.solve")
    assert (solve["outcome"], solve["kind"], solve["cost_usd"]) == ("unsupported", "recaptcha_v2", None)
    assert not [e for e in _events(isolated_imx_home, outcome.application_id, "provider.budget")
                if "captcha" in e.get("by_purpose", {})]


@pytest.mark.parametrize(("fake", "budget", "outcome_name", "calls"), [
    (FakeTwoCaptcha(), 0.0, "over_budget", 0),
    (FakeTwoCaptcha(never_ready=True), 2.0, "timeout", None),
    (FakeTwoCaptcha(error="ERROR_ZERO_BALANCE"), 2.0, "error", 1),
    (FakeTwoCaptcha(wrong_token=True), 2.0, "not_accepted", 3),
], ids=["over-budget", "timeout", "error", "refused-by-the-site"])
def test_an_unsolved_captcha_stops_the_run_as_before(
    fake: FakeTwoCaptcha, budget: float, outcome_name: str, calls: int | None,
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home)
    outcome = _run(kit, isolated_imx_home, server.url(f"{GATE}?kind=recaptcha-v2"), _solver(fake, budget=budget))
    assert outcome.state is ApplicationState.NEEDS_INPUT
    assert [m.label for m in outcome.missing_inputs] == ["Solve the CAPTCHA"]
    [solve] = _events(isolated_imx_home, outcome.application_id, "captcha.solve")
    assert solve["outcome"] == outcome_name
    if outcome_name == "error":
        assert solve["detail"] == "ERROR_ZERO_BALANCE"
    if calls is not None:
        assert len(fake.calls) == calls
    else:  # timed out after 120 s of polling: the first poll at 10 s, then every 5 s
        assert len(fake.calls) == 1 + 23


def test_a_captcha_on_a_step_before_the_last_is_solved_before_going_on(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home)
    fake = FakeTwoCaptcha()
    outcome = _run(kit, isolated_imx_home, server.url(STEPS), _solver(fake))
    assert outcome.state is ApplicationState.NEEDS_INPUT, outcome.message
    assert "Prepared to the final review step" in outcome.message, outcome.message
    assert [t["type"] for t in fake.created()] == ["RecaptchaV2TaskProxyless"]
    assert server.submissions("captcha-steps")["accepted_count"] == 0


def test_the_batch_cap_is_shared_by_the_applications_of_the_batch(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths, tmp_path: Path
) -> None:
    """Two applications, one ledger, a cap that holds one solve: the second stops."""
    _write_profile(isolated_imx_home)
    ledger = tmp_path / "batch" / "captcha-spend.jsonl"
    first = _run(kit, isolated_imx_home, server.url(f"{GATE}?kind=recaptcha-v2"),
                 _solver(FakeTwoCaptcha(), budget=0.004, ledger=ledger))
    second_fake = FakeTwoCaptcha()
    second = _run(kit, isolated_imx_home, server.url(f"{GATE}?kind=turnstile"),
                  _solver(second_fake, budget=0.004, ledger=ledger))
    assert "Prepared to the final review step" in first.message, first.message
    assert second.state is ApplicationState.NEEDS_INPUT
    assert [m.label for m in second.missing_inputs] == ["Solve the CAPTCHA"]
    assert second_fake.calls == []
    [solve] = _events(isolated_imx_home, second.application_id, "captcha.solve")
    assert solve["outcome"] == "over_budget"
    assert CaptchaBudget(0.004, ledger).spent() == pytest.approx(0.00145)


def test_solving_is_bounded_per_run(kit: SimpleNamespace) -> None:
    fake = FakeTwoCaptcha()
    solver = _solver(fake)
    solver.attempts = solver.max_attempts
    from interviewmaxxing_browser.captcha import CaptchaWidget

    attempt, token = asyncio.run(solver.token(CaptchaWidget("turnstile", "0x4AAAAAAAFixtureTurnstile01"),
                                              "http://127.0.0.1/"))
    assert token is None and attempt.outcome == "unsupported" and fake.calls == []
