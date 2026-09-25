"""Round 14: the 2Captcha solver's flags, key and batch plumbing, and the CAPTCHA of an
approved submission.

Fictional values only. The key comes from a temporary env file or a monkeypatched
variable, never from a real env file, and 2Captcha is a fake transport: nothing leaves
the process. The browser tests (``tests/browser/test_captcha_round14.py``) drive the
solver against the mock ATS.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_browser.captcha import (
    SOLVER_ENV,
    TWOCAPTCHA_KEY_NAME,
    CaptchaAttempt,
    CaptchaBudget,
    CaptchaSolver,
    CaptchaWidget,
    TwoCaptcha,
    build_solver,
    solver_choice,
)
from interviewmaxxing_cli.batch import (
    CAPTCHA_SPEND_FILE,
    BatchOptions,
    BatchRunOptions,
    SubmitBatchOptions,
    provider_cost,
)
from interviewmaxxing_cli.main import (
    _batch_flag_values,
    _captcha_solver,
    _given_retry_flags,
    build_parser,
)
from interviewmaxxing_cli.retry import retry_options
from interviewmaxxing_cli.runner import LocalApplicationRunner, NoninteractiveInteraction, RunLimits
from interviewmaxxing_core import (
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    BrowserOptions,
    CandidateProfile,
    ControlType,
    FieldFillResult,
    FieldFillStatus,
    FillResult,
    IdentityEvidenceKind,
    JobIdentityObservation,
    LocalPaths,
    NavigationResult,
    NotSubmittedNext,
    PacketContext,
    PageInspection,
    PageKind,
    SavedAnswer,
    SemanticType,
    SubmissionObservation,
    SubmissionOutcome,
    SubmitActionResult,
)
from interviewmaxxing_selection.credentials import (
    ENV_FILE_VARIABLE,
    REDACTED,
    CredentialError,
    load_api_key,
    load_optional_key,
    read_key_from_env_file,
)

S = ApplicationState
JOB = "https://example.test/jobs/fictional/apply"
FIXTURE_KEY = "fixture-2captcha-key-0001"
SITE_KEY = "6LfixtureV2CheckboxKeyAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture(autouse=True)
def no_real_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever the shell holds, these tests see only the key they configure."""
    for name in (ENV_FILE_VARIABLE, TWOCAPTCHA_KEY_NAME, SOLVER_ENV):
        monkeypatch.delenv(name, raising=False)


def _env_file(tmp_path: Path, name: str = "fixture.env", *, captcha: str | None = FIXTURE_KEY) -> Path:
    path = tmp_path / name
    lines = ["OPENROUTER_API_KEY=fixture-openrouter-key-0001"]
    if captcha is not None:
        lines.append(f"TWOCAPTCHA_API_KEY={captcha}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- the flags ---------------------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    ["apply", JOB], ["resume", "app-example"], ["prepare-batch", "--inventory", "/tmp/inventory.json"],
    ["submit", "app-example"], ["submit-approved", "--batch", "b1"],
], ids=["apply", "resume", "prepare-batch", "submit", "submit-approved"])
def test_the_flags_are_given_per_command_and_default_to_nothing(command: list[str]) -> None:
    parser = build_parser()
    args = parser.parse_args([*command, "--captcha-solver", "2captcha", "--captcha-budget-usd", "0.50"])
    assert (args.captcha_solver, args.captcha_budget_usd) == ("2captcha", 0.5)
    plain = parser.parse_args(command)
    assert (plain.captcha_solver, plain.captcha_budget_usd, plain.captcha_spend_file) == (None, None, None)


@pytest.mark.parametrize("flags", [
    ["--captcha-solver", "anticaptcha"], ["--captcha-budget-usd", "-1"],
    ["--captcha-budget-usd", "101"], ["--captcha-budget-usd", "two"],
])
def test_bad_flag_values_are_usage_errors(flags: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["apply", JOB, *flags])


def test_the_solver_is_off_unless_asked_for_and_needs_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parser = build_parser()
    beside_openrouter = _env_file(tmp_path)
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(beside_openrouter))
    assert _captcha_solver(parser.parse_args(["apply", JOB])) is None  # off by default
    assert _captcha_solver(parser.parse_args(["apply", JOB, "--captcha-solver", "off"])) is None

    solver = _captcha_solver(parser.parse_args(["apply", JOB, "--captcha-solver", "2captcha",
                                                "--captcha-budget-usd", "0.25"]))
    assert solver is not None
    assert solver.client.key.reveal() == FIXTURE_KEY
    assert solver.client.key.source == f"env file {beside_openrouter}"
    assert (solver.budget.cap_usd, solver.budget.ledger) == (0.25, None)
    assert FIXTURE_KEY not in repr(solver) and FIXTURE_KEY not in str(solver.client.key)

    # IMX_CAPTCHA_SOLVER turns it on for commands that do not say; the flag wins.
    monkeypatch.setenv(SOLVER_ENV, "2captcha")
    assert _captcha_solver(parser.parse_args(["apply", JOB])) is not None
    assert _captcha_solver(parser.parse_args(["apply", JOB, "--captcha-solver", "off"])) is None

    # No key anywhere: simply off (no error, nothing asked of the person).
    monkeypatch.delenv(ENV_FILE_VARIABLE)
    assert _captcha_solver(parser.parse_args(["apply", JOB])) is None
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(_env_file(tmp_path, "openrouter-only.env", captcha=None)))
    assert _captcha_solver(parser.parse_args(["apply", JOB])) is None

    # The process variable, as for the OpenRouter key.
    monkeypatch.setenv(TWOCAPTCHA_KEY_NAME, "fixture-2captcha-key-0002")
    solver = _captcha_solver(parser.parse_args(["apply", JOB]))
    assert solver is not None and solver.client.key.source == "process environment"


def test_the_explicit_env_file_and_the_batch_spend_file_are_used(tmp_path: Path) -> None:
    explicit = _env_file(tmp_path, "explicit.env", captcha="fixture-2captcha-key-0003")
    ledger = tmp_path / "batch" / CAPTCHA_SPEND_FILE
    args = build_parser().parse_args([
        "apply", JOB, "--ai-routing", "--env-file", str(explicit),
        "--writer-model", "anthropic/claude-opus-5.5", "--captcha-solver", "2captcha",
        "--captcha-spend-file", str(ledger)])
    solver = _captcha_solver(args)
    assert solver is not None
    assert solver.client.key.reveal() == "fixture-2captcha-key-0003"
    assert solver.budget.ledger == ledger


def test_the_key_is_read_from_its_own_line_and_is_never_an_error(tmp_path: Path) -> None:
    both = _env_file(tmp_path)
    key = load_optional_key(TWOCAPTCHA_KEY_NAME, both, environ={})
    assert key is not None and key.reveal() == FIXTURE_KEY
    assert REDACTED in str(key) and FIXTURE_KEY not in str(key) and FIXTURE_KEY not in repr(key)
    # The OpenRouter loader still reads only its own line.
    assert load_api_key(both, environ={}).reveal() == "fixture-openrouter-key-0001"

    assert load_optional_key(TWOCAPTCHA_KEY_NAME, environ={}) is None
    assert load_optional_key(TWOCAPTCHA_KEY_NAME, tmp_path / "missing.env", environ={}) is None
    without = _env_file(tmp_path, "without.env", captcha=None)
    assert load_optional_key(TWOCAPTCHA_KEY_NAME, without, environ={}) is None
    # A file without the line leaves the next place to look.
    fallback = load_optional_key(TWOCAPTCHA_KEY_NAME, without,
                                 environ={TWOCAPTCHA_KEY_NAME: "fixture-2captcha-key-0004"})
    assert fallback is not None and fallback.source == "process environment"
    named = load_optional_key(TWOCAPTCHA_KEY_NAME, environ={ENV_FILE_VARIABLE: str(both)})
    assert named is not None and named.reveal() == FIXTURE_KEY

    spaced = tmp_path / "spaced.env"
    spaced.write_text('TWOCAPTCHA_API_KEY="fixture key with spaces"\n', encoding="utf-8")
    assert load_optional_key(TWOCAPTCHA_KEY_NAME, spaced, environ={}) is None
    with pytest.raises(CredentialError) as refused:
        read_key_from_env_file(spaced, name=TWOCAPTCHA_KEY_NAME)
    assert "TWOCAPTCHA_API_KEY" in str(refused.value) and "spaces" not in str(refused.value)


def test_build_solver_and_solver_choice() -> None:
    assert solver_choice(None, {}) == "off"
    assert solver_choice(None, {SOLVER_ENV: "2Captcha"}) == "2captcha"
    assert solver_choice("off", {SOLVER_ENV: "2captcha"}) == "off"
    assert solver_choice(None, {SOLVER_ENV: "something-else"}) == "off"
    assert build_solver("off", environ={TWOCAPTCHA_KEY_NAME: FIXTURE_KEY}) is None
    assert build_solver("2captcha", environ={}) is None
    solver = build_solver("2captcha", budget_usd=1.5, environ={TWOCAPTCHA_KEY_NAME: FIXTURE_KEY})
    assert solver is not None and solver.budget.cap_usd == 1.5


# --- the batch -----------------------------------------------------------------------------------


def test_a_batch_gives_every_job_the_solver_and_one_spend_file(
    isolated_imx_home: LocalPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = BatchOptions(paths=isolated_imx_home, batch_id="b-captcha", candidate_id="default",
                           captcha_solver="2captcha", captcha_budget_usd=0.5)
    spend = str(options.batch_dir / CAPTCHA_SPEND_FILE)
    for argv in (options.argv(JOB), options.argv(JOB + "?second"), options.resume_argv("app_1")):
        assert argv[argv.index("--captcha-solver") + 1] == "2captcha"
        assert argv[argv.index("--captcha-budget-usd") + 1] == "0.5"
        assert argv[argv.index("--captcha-spend-file") + 1] == spend
    submissions = SubmitBatchOptions(paths=isolated_imx_home, batch_id="s-captcha",
                                     candidate_id="default", captcha_solver="2captcha",
                                     captcha_budget_usd=0.004)
    argv = submissions.submit_argv("app_1")
    assert argv[argv.index("--captcha-budget-usd") + 1] == "0.004"  # passed on as given, not rounded
    assert argv[argv.index("--captcha-spend-file") + 1] == str(submissions.batch_dir / CAPTCHA_SPEND_FILE)
    assert build_parser().parse_args(argv[argv.index("submit"):]).captcha_budget_usd == 0.004

    recorded = options.run_options()
    assert (recorded.captcha_solver, recorded.captcha_budget_usd) == ("2captcha", 0.5)
    again = BatchRunOptions.model_validate(recorded.model_dump(mode="json"))
    assert (again.captcha_solver, again.captcha_budget_usd) == ("2captcha", 0.5)
    older = recorded.model_dump(mode="json")
    del older["captcha_solver"], older["captcha_budget_usd"]  # a summary from before round 14
    assert BatchRunOptions.model_validate(older).captcha_solver == "off"

    plain = BatchOptions(paths=isolated_imx_home, batch_id="b-plain", candidate_id="default")
    assert "--captcha-solver" not in plain.argv(JOB) and "--captcha-spend-file" not in plain.argv(JOB)
    with pytest.raises(ValueError):
        BatchOptions(paths=isolated_imx_home, batch_id="b-bad", candidate_id="default",
                     captcha_budget_usd=-1)

    # Jobs lose every IMX_* variable; with the solver on, the env file beside the
    # OpenRouter key stays named so the jobs find the key where this process did.
    monkeypatch.setenv(ENV_FILE_VARIABLE, "/private/fixture.env")
    monkeypatch.setenv(SOLVER_ENV, "2captcha")
    assert options.environment(0)[ENV_FILE_VARIABLE] == "/private/fixture.env"
    assert SOLVER_ENV not in options.environment(0)  # the flag says it, with the spend file
    assert ENV_FILE_VARIABLE not in plain.environment(0)


def test_a_retry_takes_the_solver_from_the_batch_it_retries(isolated_imx_home: LocalPaths) -> None:
    given = ["prepare-batch", "--inventory", "/tmp/inventory.json", "--captcha-solver", "2captcha"]
    assert "captcha_solver" in _given_retry_flags(given)
    assert "captcha_budget_usd" not in _given_retry_flags(given)
    cli = _batch_flag_values(build_parser().parse_args(given[:3]), isolated_imx_home)
    assert (cli["captcha_solver"], cli["captcha_budget_usd"]) == ("off", 2.0)

    recorded = BatchOptions(paths=isolated_imx_home, batch_id="b1", candidate_id="default",
                            captcha_solver="2captcha", captcha_budget_usd=0.75).run_options()
    retry = retry_options(isolated_imx_home, retry_of="b1", batch_id="r1", recorded=recorded,
                          cli=cli, explicit=set())
    assert (retry.captcha_solver, retry.captcha_budget_usd) == ("2captcha", 0.75)
    # A retry is a batch of its own: its own spend file, so its own cap.
    assert retry.dynamic_argv()[-1] == str(isolated_imx_home.home / "batches" / "r1" / CAPTCHA_SPEND_FILE)
    off = retry_options(isolated_imx_home, retry_of="b1", batch_id="r2", recorded=recorded,
                        cli=cli, explicit={"captcha_solver"})
    assert off.captcha_solver == "off"


# --- the approved submission: solved right before the gated submit ------------------------------


IDENTITY = JobIdentityObservation(ats_type="mock", ats_tenant="fictional", external_job_id="F-14",
                                  evidence_kind=IdentityEvidenceKind.ATS_JOB_ID_ON_PAGE,
                                  evidence="Job ID F-14 on page", title="Fictional Analyst")
FIELDS = [ApplicationField(id=fid, label=label, selector=f"#{fid}", semantic_type=semantic,
                           control_type=ControlType.TEXT, required=True)
          for fid, label, semantic in (("first_name", "First name", SemanticType.FIRST_NAME),
                                       ("last_name", "Last name", SemanticType.LAST_NAME),
                                       ("email", "Email", SemanticType.EMAIL))]


class FakeTwoCaptcha:
    """``createTask`` then a ready ``getTaskResult`` with a fixture token and cost."""

    def __init__(self) -> None:
        self.methods: list[str] = []

    async def post(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        assert payload["clientKey"] == FIXTURE_KEY
        method = url.rsplit("/", 1)[-1]
        self.methods.append(method)
        if method == "createTask":
            return {"errorId": 0, "taskId": 7}
        return {"errorId": 0, "status": "ready", "cost": "0.00299",
                "solution": {"gRecaptchaResponse": f"fixture-solved:{SITE_KEY}"}}


def _solver(fake: FakeTwoCaptcha, *, cap: float = 2.0) -> CaptchaSolver:
    async def no_wait(seconds: float) -> None:
        return None

    from interviewmaxxing_selection.credentials import ApiKey

    client = TwoCaptcha(key=ApiKey(FIXTURE_KEY, source="test", name=TWOCAPTCHA_KEY_NAME),
                        transport=fake, sleep=no_wait)
    return CaptchaSolver(client=client, budget=CaptchaBudget(cap))


@dataclass
class Site:
    """One final step with a reCAPTCHA widget; the site refuses the submit until it is
    answered, exactly as the runtime does."""

    solved: bool = False
    calls: list[str] = field(default_factory=list)
    call_callbacks: list[bool] = field(default_factory=list)
    received: int = 0


class ScriptedBrowser:
    def __init__(self, site: Site) -> None:
        self.site = site

    def _page(self) -> PageInspection:
        form = ApplicationForm(url=JOB, step=0, fields=FIELDS, is_final_step=True,
                               submit_selector="#submit")
        return PageInspection(kind=PageKind.APPLICATION_FORM, observed_url=JOB, form=form,
                              job_identity=IDENTITY, captcha_pending=not self.site.solved)

    async def open(self, url: str) -> PageInspection:
        self.site.calls.append("open")
        return self._page()

    async def inspect(self) -> PageInspection:
        self.site.calls.append("inspect")
        return self._page()

    async def prepare_review(self) -> PageInspection:
        self.site.calls.append("prepare_review")
        return self._page()

    async def fill(self, form: ApplicationForm, packet: ApplicationPacket) -> FillResult:
        self.site.calls.append("fill")
        return FillResult(form_step=form.step, fields=[
            FieldFillResult(field_id=a.field_id, status=FieldFillStatus.FILLED) for a in packet.answers])

    async def advance(self) -> NavigationResult:
        raise AssertionError("a one-step form has no Next")

    async def submit(self) -> SubmitActionResult:
        self.site.calls.append("submit")
        if not self.site.solved:
            return SubmitActionResult(dispatched=False, detail="a CAPTCHA on this form must be "
                                      "solved by the user before submitting")
        self.site.received += 1
        return SubmitActionResult(dispatched=True)

    async def confirm(self) -> SubmissionObservation:
        self.site.calls.append("confirm")
        if not self.site.solved:
            return SubmissionObservation(
                outcome=SubmissionOutcome.NOT_SUBMITTED, next_state=NotSubmittedNext.NEEDS_INPUT,
                signals=["a CAPTCHA on this form must be solved by the user before submitting"])
        return SubmissionObservation(outcome=SubmissionOutcome.ACCEPTED, confirmation_reference="F-REF-14",
                                     signals=["heading 'Application submitted'", "job id 'F-14' shown"])

    async def wait_for_user(self, reason: str, timeout_s: float | None = None) -> PageInspection:
        self.site.calls.append("wait_for_user")
        return self._page()

    async def solve_captcha(self, solver: CaptchaSolver, *, call_callback: bool
                            ) -> tuple[CaptchaAttempt, str, PageInspection | None]:
        self.site.calls.append("solve_captcha")
        self.site.call_callbacks.append(call_callback)
        attempt, token = await solver.token(CaptchaWidget("recaptcha_v2", SITE_KEY), JOB)
        if token is None:
            return attempt, JOB, None
        self.site.solved = token == f"fixture-solved:{SITE_KEY}"
        return attempt, JOB, self._page()

    async def close(self) -> None:
        self.site.calls.append("close")


class Factory:
    def __init__(self, site: Site) -> None:
        self.site = site

    async def start(self, options: BrowserOptions) -> ScriptedBrowser:
        return ScriptedBrowser(self.site)


class Candidates:
    def __init__(self, profile: CandidateProfile) -> None:
        self.profile = profile

    def load(self, candidate_id: str) -> CandidateProfile:
        return self.profile.model_copy(update={"id": candidate_id})

    def save_answer(self, candidate_id: str, answer: SavedAnswer) -> None:
        pass


class NeverResolve:
    async def resolve(self, context: PacketContext) -> ApplicationPacket:
        raise AssertionError("the approved path resolved a packet")


def _runner(paths: LocalPaths, candidates: Candidates, site: Site, *, prepare_only: bool,
            solver: CaptchaSolver | None) -> LocalApplicationRunner:
    return LocalApplicationRunner(
        paths=paths, interaction=NoninteractiveInteraction(), headless=True,
        browser_factory=Factory(site), candidates=candidates,
        resolver=None if prepare_only else NeverResolve(),
        limits=RunLimits(max_steps=4, max_same_form=2), prepare_only=prepare_only,
        captcha_solver=solver)


def _prepared_and_approved(paths: LocalPaths, candidates: Candidates, site: Site,
                           solver: CaptchaSolver | None) -> str:
    outcome = asyncio.run(_runner(paths, candidates, site, prepare_only=True, solver=solver)
                          .apply(JOB, candidate_id="c1"))
    assert outcome.state is S.NEEDS_INPUT and "Prepared to the final review step" in outcome.message
    assert "A CAPTCHA on this form must be solved" in outcome.message
    with ApplicationStore.open(paths.state_db) as store:
        claim = store.claim(outcome.application_id, "cli:approve")
        packet_id = store.prepared_packet(outcome.application_id)
        assert packet_id is not None
        store.approve_submission(claim, packet_id=packet_id, approver="cli:test")
        store.authorize_submission(claim)
        store.release(claim)
    return outcome.application_id


def _events(paths: LocalPaths, app_id: str, name: str) -> list[dict[str, Any]]:
    with ApplicationStore.open(paths.state_db) as store:
        return [e.metadata for e in store.list_events(app_id) if e.event == name]


@pytest.fixture
def candidates(fictional_candidate: CandidateProfile) -> Candidates:
    return Candidates(fictional_candidate)


def test_the_approved_submit_solves_the_form_captcha_right_before_the_gated_submit(
    isolated_imx_home: LocalPaths, candidates: Candidates
) -> None:
    site = Site()
    fake = FakeTwoCaptcha()
    solver = _solver(fake)
    app_id = _prepared_and_approved(isolated_imx_home, candidates, site, solver)
    assert "solve_captcha" not in site.calls  # preparation never solves the final step's CAPTCHA
    assert fake.methods == []

    site.calls.clear()
    outcome = asyncio.run(_runner(isolated_imx_home, candidates, site, prepare_only=False,
                                  solver=solver).submit(app_id))
    assert outcome.state is S.SUBMITTED, outcome.message
    assert site.received == 1
    solve, submit = site.calls.index("solve_captcha"), site.calls.index("submit")
    assert solve < submit and site.calls.count("submit") == 1
    assert site.call_callbacks == [False]  # on a form only the response field is written
    [solved] = _events(isolated_imx_home, app_id, "captcha.solve")
    assert (solved["outcome"], solved["kind"], solved["cost_usd"], solved["site"]) == (
        "solved", "recaptcha_v2", 0.00299, "example.test")
    assert "fixture-solved" not in str(_events(isolated_imx_home, app_id, "captcha.solve"))
    [usage] = [e for e in _events(isolated_imx_home, app_id, "provider.budget")
               if "captcha" in e.get("by_purpose", {})]
    assert (usage["calls"], usage["known_cost_usd"]) == (1, 0.00299)
    assert provider_cost(isolated_imx_home, app_id) == (0.00299, 1)  # batch-report's cost column
    assert "Provider cost: USD 0.0030 for 1 call(s)" in outcome.message


@pytest.mark.parametrize("with_solver", [False, True], ids=["solver-off", "over-budget"])
def test_an_unsolved_form_captcha_stops_the_approved_submit_as_before(
    with_solver: bool, isolated_imx_home: LocalPaths, candidates: Candidates
) -> None:
    site = Site()
    fake = FakeTwoCaptcha()
    solver = _solver(fake, cap=0.0) if with_solver else None
    app_id = _prepared_and_approved(isolated_imx_home, candidates, site, solver)
    outcome = asyncio.run(_runner(isolated_imx_home, candidates, site, prepare_only=False,
                                  solver=solver).submit(app_id))
    assert outcome.state is S.NEEDS_INPUT, outcome.message
    assert [m.label for m in outcome.missing_inputs] == ["Solve the CAPTCHA"]
    assert site.received == 0 and fake.methods == []
    solves = _events(isolated_imx_home, app_id, "captcha.solve")
    assert [s["outcome"] for s in solves] == (["over_budget"] if with_solver else [])
    assert ("solve_captcha" in site.calls) is with_solver
