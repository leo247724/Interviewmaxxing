"""The runner's approved-submission path with a scripted site: prepare -> approve ->
authorize -> submit exactly the approved packets, and every way it refuses.

The real store and the real factual resolver prepare; the submission run gets a
resolver that fails if it is ever asked, so "never re-resolves" is enforced, not
assumed. The site is a small fake that can change between runs (new, removed or
changed questions, a moved final step, per-run draft URLs). All data is fictional;
nothing leaves the process."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from interviewmaxxing_browser import AmbiguousAction
from interviewmaxxing_browser.runtime import GenericApplicationBrowser, site_refusal
from interviewmaxxing_cli.main import EXIT_OK, KEPT_DRAFT_STEP, main
from interviewmaxxing_cli.runner import (
    KEPT_DRAFT_MESSAGE,
    KEPT_DRAFT_REASON,
    MISMATCH_MESSAGE,
    NOT_AUTHORIZED_MESSAGE,
    REJECTED_MESSAGE,
    LocalApplicationRunner,
    NoninteractiveInteraction,
    RunLimits,
    StepBack,
    _ApprovedStep,
    _Run,
    _Stop,
    approval_mismatches,
    approval_omissions,
    create_submission_runner,
    field_record,
    redact_detail,
)
from interviewmaxxing_core import (
    AnswerSource,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    BrowserOptions,
    CandidateProfile,
    ControlType,
    EvidenceKind,
    EvidenceRef,
    FieldFillResult,
    FieldFillStatus,
    FieldOption,
    FillResult,
    IdentityEvidenceKind,
    JobIdentityObservation,
    MissingInput,
    MissingReason,
    NavigationResult,
    NotSubmittedNext,
    PacketAnswer,
    PacketContext,
    PageInspection,
    PageKind,
    Provenance,
    SavedAnswer,
    SemanticType,
    SubmissionObservation,
    SubmissionOutcome,
    SubmitActionResult,
    TextValue,
    UserInput,
    sha256_file,
)

S = ApplicationState
URL = "http://127.0.0.1:9/jobs/fictional/apply"
IDENTITY = JobIdentityObservation(ats_type="mock", ats_tenant="fictional", external_job_id="F-1",
                                  evidence_kind=IdentityEvidenceKind.ATS_JOB_ID_ON_PAGE,
                                  evidence="Job ID F-1 on page", title="Fictional Analyst")
ACCEPTED = SubmissionObservation(outcome=SubmissionOutcome.ACCEPTED,
                                 signals=["heading 'Application submitted'", "job id 'F-1' shown"],
                                 confirmation_reference="F-REF-1")
HEARD = [FieldOption(value="src_site", label="Company website"),
         FieldOption(value="src_other", label="Other")]


def _text(fid: str, label: str, semantic: SemanticType, *, required: bool = True) -> ApplicationField:
    return ApplicationField(id=fid, label=label, selector=f"#{fid}", semantic_type=semantic,
                            control_type=ControlType.TEXT, required=required)


def _heard(*, required: bool = False, options: list[FieldOption] = HEARD) -> ApplicationField:
    return ApplicationField(id="heard", label="How did you hear about us?", selector="#heard",
                            semantic_type=SemanticType.REFERRAL_SOURCE,
                            control_type=ControlType.SELECT, required=required, options=options)


def _contact() -> list[ApplicationField]:
    return [_text("first_name", "First name", SemanticType.FIRST_NAME),
            _text("last_name", "Last name", SemanticType.LAST_NAME),
            _text("email", "Email", SemanticType.EMAIL),
            _heard()]


# --- a fake site that can change between runs ------------------------------------------------


@dataclass
class Site:
    steps: list[list[ApplicationField]]
    final_flags: list[bool] | None = None
    runs: int = 0
    calls: list[str] = field(default_factory=list)
    fills: list[tuple[int, ApplicationPacket]] = field(default_factory=list)
    submitted: list[dict[int, ApplicationPacket]] = field(default_factory=list)
    """What was filled on every step when the submit control was clicked."""
    fill_status: dict[str, FieldFillStatus] = field(default_factory=dict)
    confirm: list[SubmissionObservation] = field(default_factory=list)
    reject_on_submit: dict[str, str] = field(default_factory=dict)
    """Field id -> validation message the site shows after a submit (it rejects it)."""
    showing_errors: dict[str, str] = field(default_factory=dict)
    after_user: list[ApplicationField] | None = None
    """The current step's questions once the user acted in the browser."""
    resume_at: int | None = None
    """The step the site opens at (a kept draft); the first one when None."""
    fill_detail: dict[str, str] = field(default_factory=dict)
    """Field id -> the detail a fill result reports for it."""
    attaches_files: bool = True
    """False stands for a browser that cannot attach files (OpenCLI's Browser Bridge)."""
    options: list[BrowserOptions] = field(default_factory=list)
    advance_to: dict[int, int] = field(default_factory=dict)
    """Step -> the step Next leads to (the next one when absent): a site that skips a page."""
    back_to: dict[int, int] = field(default_factory=dict)
    """For a browser that can go back: step -> the step Back leads to (the previous one
    when absent); a step mapped to itself stays where it is."""
    back_fails: bool = False
    """The Back control is ambiguous (``AmbiguousAction``)."""

    def form(self, step: int) -> ApplicationForm:
        count = len(self.steps)
        final = step == count - 1 if self.final_flags is None else self.final_flags[step]
        url = URL if count == 1 else f"{URL}/dft_{self.runs:03d}/step/{step + 1}"
        fields = [f.model_copy(update={"validation_error": self.showing_errors.get(f.id)})
                  for f in self.steps[step]]
        return ApplicationForm(url=url, step=step, fields=fields, is_final_step=final,
                               submit_selector="#submit" if final else None,
                               next_selector=None if final else "#next")


class FakeBrowser:
    def __init__(self, site: Site) -> None:
        self.site = site
        self.step = 0
        self.filled: dict[int, ApplicationPacket] = {}

    @property
    def attaches_files(self) -> bool:
        return self.site.attaches_files

    def _page(self) -> PageInspection:
        form = self.site.form(self.step)
        return PageInspection(kind=PageKind.APPLICATION_FORM, observed_url=form.url, form=form,
                              job_identity=IDENTITY)

    async def open(self, url: str) -> PageInspection:
        self.site.runs += 1
        self.site.showing_errors = {}
        self.site.calls.append("open")
        self.step = self.site.resume_at or 0
        return self._page()

    async def inspect(self) -> PageInspection:
        self.site.calls.append("inspect")
        return self._page()

    async def prepare_review(self) -> PageInspection:
        self.site.calls.append("prepare_review")
        return self._page()

    async def fill(self, form: ApplicationForm, packet: ApplicationPacket) -> FillResult:
        self.site.calls.append("fill")
        assert packet.problems_against(form) == []
        self.site.fills.append((form.step, packet))
        self.filled[form.step] = packet
        return FillResult(form_step=form.step, fields=[
            FieldFillResult(field_id=a.field_id,
                            status=self.site.fill_status.get(a.field_id, FieldFillStatus.FILLED),
                            detail=self.site.fill_detail.get(a.field_id))
            for a in packet.answers])

    async def advance(self) -> NavigationResult:
        self.site.calls.append("advance")
        self.step = self.site.advance_to.get(self.step, self.step + 1)
        return NavigationResult(advanced=True, inspection=self._page())

    async def submit(self) -> SubmitActionResult:
        self.site.calls.append("submit")
        self.site.submitted.append(dict(self.filled))
        self.site.showing_errors = dict(self.site.reject_on_submit)
        return SubmitActionResult(dispatched=True)

    async def confirm(self) -> SubmissionObservation:
        self.site.calls.append("confirm")
        return self.site.confirm.pop(0) if self.site.confirm else ACCEPTED

    async def wait_for_user(self, reason: str, timeout_s: float | None = None) -> PageInspection:
        self.site.calls.append("wait_for_user")
        if self.site.after_user is not None:
            self.site.steps[self.step] = self.site.after_user
        return self._page()

    async def close(self) -> None:
        self.site.calls.append("close")


class BackBrowser(FakeBrowser):
    """A browser that can go back one page in the same draft (``StepBack``)."""

    async def previous_step(self) -> PageInspection:
        self.site.calls.append("previous_step")
        if self.site.back_fails:
            raise AmbiguousAction("two controls could mean Back")
        self.step = self.site.back_to.get(self.step, self.step - 1)
        return self._page()


class FakeFactory:
    def __init__(self, site: Site, *, back: bool = False) -> None:
        self.site = site
        self.back = back
        self.starts = 0

    async def start(self, options: BrowserOptions) -> FakeBrowser:
        self.starts += 1
        self.site.options.append(options)
        return BackBrowser(self.site) if self.back else FakeBrowser(self.site)


class Candidates:
    def __init__(self, profile: CandidateProfile) -> None:
        self.profile = profile

    def load(self, candidate_id: str) -> CandidateProfile:
        return self.profile.model_copy(update={"id": candidate_id})

    def save_answer(self, candidate_id: str, answer: SavedAnswer) -> None:
        pass


class NeverResolve:
    """A submission run must never resolve or generate an answer."""

    async def resolve(self, context: PacketContext) -> ApplicationPacket:
        raise AssertionError("the approved path resolved a packet")


class Answering(NoninteractiveInteraction):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    async def request_inputs(self, missing: Sequence[MissingInput]) -> Sequence[UserInput]:
        return [UserInput.answering(m, TextValue(text=self.text)) for m in missing]


def _runner(paths, candidates: Candidates, site: Site, *, prepare_only: bool,
            interaction: NoninteractiveInteraction | None = None,
            resolver: Any = None, back: bool = False) -> LocalApplicationRunner:
    return LocalApplicationRunner(
        paths=paths, interaction=interaction or NoninteractiveInteraction(), headless=True,
        browser_factory=FakeFactory(site, back=back), candidates=candidates,
        resolver=resolver if resolver is not None or prepare_only else NeverResolve(),
        limits=RunLimits(max_steps=8, max_same_form=2), prepare_only=prepare_only)


def _prepare(paths, candidates, site, *, interaction=None) -> ApplyOutcome:
    outcome = asyncio.run(_runner(paths, candidates, site, prepare_only=True,
                                  interaction=interaction).apply(URL, candidate_id="c1"))
    assert outcome.state is S.NEEDS_INPUT and "Prepared to the final review step" in outcome.message, outcome
    return outcome


def _approve(paths, app_id: str, *, authorize: bool = True) -> str:
    with ApplicationStore.open(paths.state_db) as store:
        claim = store.claim(app_id, "cli:approve")
        packet_id = store.prepared_packet(app_id)
        assert packet_id is not None
        store.approve_submission(claim, packet_id=packet_id, approver="cli:test")
        if authorize:
            store.authorize_submission(claim)
        store.release(claim)
    return packet_id


def _submit(paths, candidates, site, app_id: str, **kwargs: Any) -> tuple[ApplyOutcome, LocalApplicationRunner]:
    runner = _runner(paths, candidates, site, prepare_only=False, **kwargs)
    return asyncio.run(runner.submit(app_id)), runner


def _events(paths, app_id: str) -> list[Any]:
    with ApplicationStore.open(paths.state_db) as store:
        return store.list_events(app_id)


@pytest.fixture
def candidates(fictional_candidate: CandidateProfile) -> Candidates:
    return Candidates(fictional_candidate)


# --- preparation records what an approval pins ----------------------------------------------


def test_preparation_records_every_step_with_its_questions(isolated_imx_home, candidates):
    site = Site(steps=[_contact()[:3], [_heard(required=False)]])
    outcome = _prepare(isolated_imx_home, candidates, site)
    [ready] = [e for e in _events(isolated_imx_home, outcome.application_id)
               if e.event == "preparation.ready"]
    steps = ready.metadata["steps"]
    saved = [e.metadata["packet_id"] for e in _events(isolated_imx_home, outcome.application_id)
             if e.event == "packet.saved"]
    assert [s["form_step"] for s in steps] == [0, 1]
    assert [s["packet_id"] for s in steps] == [saved[0], ready.metadata["packet_id"]]
    assert [s["final"] for s in steps] == [False, True]
    assert steps[0]["fields"] == [field_record(f) for f in site.steps[0]]
    assert steps[1]["fields"] == [field_record(_heard())]
    assert steps[1]["fields"][0] == {
        "id": "heard", "fingerprint": _heard().fingerprint, "required": False,
        "semantic_type": "REFERRAL_SOURCE", "control_type": "SELECT",
        "options": steps[1]["fields"][0]["options"], "label": "How did you hear about us?"}
    assert steps[1]["fields"][0]["options"] is not None
    assert steps[0]["fields"][0]["options"] is None  # a text field has no options


# --- submitting exactly the approved packet ----------------------------------------------------


def test_submits_exactly_the_approved_packet_without_resolving_again(isolated_imx_home, candidates):
    site = Site(steps=[_contact()])
    outcome = _prepare(isolated_imx_home, candidates, site)
    app_id = outcome.application_id
    packet_id = _approve(isolated_imx_home, app_id)
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        approved = store.get_packet(packet_id)
    # The profile changes after the approval; the approved values are submitted anyway.
    candidates.profile = candidates.profile.model_copy(update={
        "identity": candidates.profile.identity.model_copy(update={"first_name": "Changed"})})
    site.calls.clear()
    site.fills.clear()

    result, runner = _submit(isolated_imx_home, candidates, site, app_id)

    assert result.state is S.SUBMITTED and result.receipt is not None, result.message
    assert "Submitted; the site confirmed it." in result.message
    assert runner.browser_factory.site.options[-1].allow_submission is True  # type: ignore[attr-defined]
    assert site.calls.count("submit") == 1 and site.calls.count("fill") == 1
    [(step, filled)] = [(s, p) for s, p in site.fills if p.id == packet_id]
    assert step == 0 and filled == approved  # same id, answers, provenance; nothing re-resolved
    assert site.submitted == [{0: approved}]
    assert {a.field_id: a.value for a in filled.answers}["first_name"] == TextValue(text="Avery")
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        [attempt] = store.list_attempts(app_id)
        assert attempt.packet_id == packet_id and attempt.outcome is SubmissionOutcome.ACCEPTED
        events = store.list_events(app_id)
    names = [e.event for e in events]
    authorized = names.index("application.submission_authorized")
    assert "packet.saved" not in names[authorized:]  # the submission run saved no packet
    assert names[-2:] == ["application.submitting", "application.submitted"]
    assert events[-2].metadata["packet_id"] == packet_id


def test_a_multistep_application_fills_each_step_from_its_approved_packet(isolated_imx_home, candidates):
    site = Site(steps=[_contact()[:3], [_heard()], []])  # the last step is a review page
    outcome = _prepare(isolated_imx_home, candidates, site)
    app_id = outcome.application_id
    _approve(isolated_imx_home, app_id)
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        approval = store.submission_approval(app_id)
        assert approval is not None
        approved = {s.form_step: store.get_packet(s.packet_id) for s in approval.steps}
    assert sorted(approved) == [0, 1, 2]
    site.fills.clear()

    result, _ = _submit(isolated_imx_home, candidates, site, app_id)

    assert result.state is S.SUBMITTED, result.message
    assert [s for s, _ in site.fills] == [0, 1, 2]
    for step, packet in site.fills:
        # Each step keeps its approved id, answers and provenance; only the draft URL of
        # this run's step is new.
        assert packet.id == approved[step].id and packet.answers == approved[step].answers
        assert packet.form_url == site.form(step).url != approved[step].form_url
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert [a.packet_id for a in store.list_attempts(app_id)] == [approval.packet_id]


# --- refusals before anything is opened -------------------------------------------------------


def test_submit_without_an_authorized_approval_opens_nothing(isolated_imx_home, candidates):
    site = Site(steps=[_contact()])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    site.calls.clear()

    unapproved, runner = _submit(isolated_imx_home, candidates, site, app_id)
    assert unapproved.state is S.NEEDS_INPUT
    assert unapproved.message.startswith(NOT_AUTHORIZED_MESSAGE)
    assert runner.browser_factory.starts == 0  # type: ignore[attr-defined]

    _approve(isolated_imx_home, app_id, authorize=False)
    approved_only, runner = _submit(isolated_imx_home, candidates, site, app_id)
    assert approved_only.message.startswith(NOT_AUTHORIZED_MESSAGE)
    assert runner.browser_factory.starts == 0  # type: ignore[attr-defined]

    # A preparation-only runner never submits, even an authorized application.
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        claim = store.claim(app_id, "cli:submit")
        store.authorize_submission(claim)
        store.release(claim)
    prepare_runner = _runner(isolated_imx_home, candidates, site, prepare_only=True)
    refused = asyncio.run(prepare_runner.submit(app_id))
    assert "this runner is preparation-only" in refused.message
    assert site.calls == []
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert store.list_attempts(app_id) == []


def test_the_submission_runner_factory_is_not_preparation_only(isolated_imx_home):
    runner = create_submission_runner(isolated_imx_home, headless=True,
                                      interaction=NoninteractiveInteraction())
    assert runner.prepare_only is False


# --- the form no longer matches -----------------------------------------------------------------


def _changes() -> dict[str, tuple[Any, str]]:
    new_required = _text("travel", "Are you willing to travel?", SemanticType.CUSTOM_TEXT)
    return {
        "new required question": (
            lambda site: site.steps[0].append(new_required),
            "a new required question 'Are you willing to travel?' appeared"),
        "changed wording": (
            lambda site: site.steps[0].__setitem__(2, _text("email", "Work email", SemanticType.EMAIL)),
            "the question 'Email' changed"),
        "missing question": (
            lambda site: site.steps[0].pop(1),
            "the question 'Last name' is no longer on the form"),
        "now required": (
            lambda site: site.steps[0].__setitem__(3, _heard(required=True)),
            "the required question 'How did you hear about us?' has no approved answer"),
        "final step moved": (
            lambda site: setattr(site, "final_flags", [False]),
            "step 1 no longer submits the application"),
    }


@pytest.mark.parametrize("change", sorted(_changes()))
def test_a_changed_form_stops_before_filling_and_withdraws_the_approval(
    isolated_imx_home, candidates, change
):
    site = Site(steps=[_contact()])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    packet_id = _approve(isolated_imx_home, app_id)
    mutate, detail = _changes()[change]
    mutate(site)
    site.calls.clear()

    result, _ = _submit(isolated_imx_home, candidates, site, app_id)

    assert result.state is S.NEEDS_INPUT and result.receipt is None
    assert result.message.startswith(MISMATCH_MESSAGE), result.message
    assert detail in result.message
    assert "Nothing was submitted and the approval was withdrawn" in result.message
    assert "fill" not in site.calls and "submit" not in site.calls
    events = _events(isolated_imx_home, app_id)
    [invalidated] = [e for e in events if e.event == "application.approval_invalidated"]
    assert invalidated.metadata["packet_id"] == packet_id
    assert detail in invalidated.metadata["details"]
    assert invalidated.metadata["reason"] == MISMATCH_MESSAGE
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert store.list_attempts(app_id) == []
        assert store.approved_packet(app_id) is None
        assert store.is_preparation_only(app_id)
        assert store.prepared_packet(app_id) is None  # prepare again before approving


@pytest.mark.parametrize(("status", "detail"), [
    (FieldFillStatus.VERIFICATION_MISMATCH, "the approved answer to 'Email' does not read back"),
    (FieldFillStatus.NEEDS_CHOICE, "the site no longer accepts the approved answer to 'Email'"),
])
def test_a_value_that_does_not_stick_stops_before_submitting(isolated_imx_home, candidates,
                                                             status, detail):
    site = Site(steps=[_contact()])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    _approve(isolated_imx_home, app_id)
    site.fill_status = {"email": status}

    result, _ = _submit(isolated_imx_home, candidates, site, app_id)

    assert result.state is S.NEEDS_INPUT and result.message.startswith(MISMATCH_MESSAGE)
    assert detail in result.message
    assert "submit" not in site.calls
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert store.list_attempts(app_id) == [] and store.approved_packet(app_id) is None


def test_a_failed_fill_keeps_the_approval_for_a_retry(isolated_imx_home, candidates):
    site = Site(steps=[_contact()])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    packet_id = _approve(isolated_imx_home, app_id)
    site.fill_status = {"email": FieldFillStatus.FAILED}

    result, _ = _submit(isolated_imx_home, candidates, site, app_id)
    assert result.state is S.FAILED_RETRYABLE and "The approval stands" in result.message
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert store.approved_packet(app_id) == packet_id and store.list_attempts(app_id) == []
        [failure] = [e for e in store.list_events(app_id) if e.to_state is S.FAILED_RETRYABLE]
    assert failure.metadata["failed_fields"] == [
        {"field_id": "email", "label": "Email", "status": "FAILED", "detail": None}]

    site.fill_status = {}
    retried, _ = _submit(isolated_imx_home, candidates, site, app_id)
    assert retried.state is S.SUBMITTED, retried.message


def test_answers_the_site_rejects_after_submit_withdraw_the_approval(isolated_imx_home, candidates):
    site = Site(steps=[_contact()])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    _approve(isolated_imx_home, app_id)
    site.confirm = [SubmissionObservation(
        outcome=SubmissionOutcome.NOT_SUBMITTED, signals=["the form was shown again with errors"],
        validation_errors=["Email: enter a company address"], next_state=NotSubmittedNext.FILLING,
        detail="the site rejected the form")]
    site.reject_on_submit = {"email": "Enter a company email address."}

    result, _ = _submit(isolated_imx_home, candidates, site, app_id)

    assert result.state is S.NEEDS_INPUT and result.message.startswith(MISMATCH_MESSAGE)
    assert "the site did not accept the approved answers (Email: Enter a company email address.)" in result.message
    assert site.calls.count("submit") == 1
    events = _events(isolated_imx_home, app_id)
    # The rejection is kept, so preparing again asks for a corrected answer.
    assert any(e.event == "validation.rejected" for e in events)
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert [a.outcome for a in store.list_attempts(app_id)] == [SubmissionOutcome.NOT_SUBMITTED]
        assert store.approved_packet(app_id) is None and store.is_preparation_only(app_id)


# --- preparation-only runs after an approval ----------------------------------------------------


@pytest.mark.parametrize("authorize", [True, False])
def test_a_prepare_only_run_after_an_approval_never_submits(isolated_imx_home, candidates, authorize):
    site = Site(steps=[_contact()])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    first = _approve(isolated_imx_home, app_id, authorize=authorize)
    site.calls.clear()

    again = asyncio.run(_runner(isolated_imx_home, candidates, site, prepare_only=True).resume(app_id))

    assert again.state is S.NEEDS_INPUT and "Prepared to the final review step" in again.message
    assert "submit" not in site.calls and site.options[-1].allow_submission is False
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert store.list_attempts(app_id) == []
        assert store.is_preparation_only(app_id)
        assert store.approved_packet(app_id) is None  # a new preparation needs a new approval
        assert store.prepared_packet(app_id) not in (None, first)
    # A submission run now refuses without opening anything.
    refused, runner = _submit(isolated_imx_home, candidates, site, app_id)
    assert refused.message.startswith(NOT_AUTHORIZED_MESSAGE)
    assert runner.browser_factory.starts == 0  # type: ignore[attr-defined]


def test_an_approved_but_unauthorized_application_is_prepared_again_by_a_submitting_runner(
    isolated_imx_home, candidates
):
    """``resume`` with ``prepare_only=False`` keeps the stored restriction: without an
    authorization it prepares again (the approval then lapses) and never submits."""
    site = Site(steps=[_contact()])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    _approve(isolated_imx_home, app_id, authorize=False)
    runner = _runner(isolated_imx_home, candidates, site, prepare_only=False,
                     resolver=None or _factual())
    outcome = asyncio.run(runner.resume(app_id))
    assert outcome.state is S.NEEDS_INPUT and "Prepared to the final review step" in outcome.message
    assert "submit" not in site.calls and site.options[-1].allow_submission is False


def _factual() -> Any:
    from interviewmaxxing_generation import FactualPacketResolver

    return FactualPacketResolver()


# --- the runtime's reading of an unchanged question ---------------------------------------------


def test_an_unchanged_question_read_as_another_type_is_still_submitted(isolated_imx_home, candidates):
    motivation = _text("motivation", "Why this role?", SemanticType.CUSTOM_TEXT)
    site = Site(steps=[[*_contact(), motivation]])
    app_id = _prepare(isolated_imx_home, candidates, site,
                      interaction=Answering("Forecasting is my favourite problem.")).application_id
    packet_id = _approve(isolated_imx_home, app_id)
    site.steps[0][4] = motivation.model_copy(update={"semantic_type": SemanticType.CUSTOM_LONG_TEXT})
    site.fills.clear()

    result, _ = _submit(isolated_imx_home, candidates, site, app_id)

    assert result.state is S.SUBMITTED, result.message
    [filled] = [p for _, p in site.fills if p.id == packet_id]
    answer = filled.answer_for("motivation")
    assert answer is not None and answer.value == TextValue(text="Forecasting is my favourite problem.")
    assert answer.provenance.source is AnswerSource.USER_INPUT


def test_a_reading_that_conflicts_with_an_answer_s_source_keeps_the_approval(isolated_imx_home, candidates):
    site = Site(steps=[_contact()])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    packet_id = _approve(isolated_imx_home, app_id)
    # The same "First name" question read as a custom question: an identity answer may
    # not fill it, but the site did not change, so the approval stands.
    site.steps[0][0] = _text("first_name", "First name", SemanticType.CUSTOM_TEXT)
    site.calls.clear()

    result, _ = _submit(isolated_imx_home, candidates, site, app_id)

    assert result.state is S.FAILED_RETRYABLE and "the approval stands" in result.message
    assert "fill" not in site.calls
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert store.approved_packet(app_id) == packet_id


# --- custom controls the user operated while preparing ----------------------------------------


def _pronouns(*, required: bool) -> ApplicationField:
    return ApplicationField(id="pronouns", label="Pronouns", selector="#pronouns",
                            semantic_type=SemanticType.UNKNOWN,
                            control_type=ControlType.UNSUPPORTED, required=required)


@pytest.mark.parametrize("present", [False, True])
def test_a_custom_control_the_user_set_is_left_to_the_user_again(isolated_imx_home, candidates, present):
    # Prepared after the user set the custom control (an operated control reads as not
    # required); the next load shows it unset and required again.
    site = Site(steps=[[*_contact(), _pronouns(required=False)]])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    packet_id = _approve(isolated_imx_home, app_id)
    site.steps[0][4] = _pronouns(required=True)
    if present:
        site.after_user = [*_contact(), _pronouns(required=False)]

    result, _ = _submit(isolated_imx_home, candidates, site, app_id,
                        interaction=NoninteractiveInteraction(allow_browser_action=present))

    if present:
        assert result.state is S.SUBMITTED, result.message
        assert site.calls.count("wait_for_user") == 1
    else:
        assert result.state is S.NEEDS_INPUT and not result.message.startswith(MISMATCH_MESSAGE)
        [need] = result.missing_inputs
        assert (need.field_id, need.reason) == ("pronouns", MissingReason.UNSUPPORTED_CONTROL)
        assert "submit" not in site.calls
        with ApplicationStore.open(isolated_imx_home.state_db) as store:
            assert store.approved_packet(app_id) == packet_id  # not a change of the form


# --- preparations recorded before questions were pinned -------------------------------------------


def _legacy_preparation(paths, site: Site) -> tuple[str, str]:
    """A preparation.ready without ``steps`` (recorded by an earlier runner)."""
    form = site.form(0)
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request("c1", URL).application
        claim = store.claim(app.id, "old-runner")
        store.require_preparation_only(claim)
        store.transition(claim, S.INSPECTING)
        packet = ApplicationPacket(
            application_id=app.id, job_id=app.job_id, candidate_id=app.candidate_id,
            form_url=form.url, form_step=0, form_fingerprint=form.fingerprint,
            answers=[PacketAnswer(field_id=f.id, semantic_type=f.semantic_type,
                                  value=TextValue(text=v),
                                  provenance=Provenance(source=AnswerSource.PROFILE_IDENTITY))
                     for f, v in zip(form.fields[:3], ("Avery", "Example", "avery@example.test"),
                                     strict=True)])
        store.save_packet(claim, packet)
        store.transition(claim, S.PACKET_READY)
        store.transition(claim, S.FILLING)
        store.append_event(claim, "preparation.ready", {
            "form_url": form.url, "form_step": 0, "form_fingerprint": form.fingerprint,
            "packet_id": packet.id, "submitted": False, "browser_location": None,
            "captcha_pending": False})
        store.transition(claim, S.INSPECTING)
        store.transition(claim, S.NEEDS_INPUT, metadata={"missing_inputs": [], "reason": "prepared"})
        store.release(claim)
    return app.id, packet.id


@pytest.mark.parametrize("changed", [False, True])
def test_an_older_preparation_is_compared_by_its_packet_s_form(isolated_imx_home, candidates, changed):
    site = Site(steps=[_contact()])
    app_id, packet_id = _legacy_preparation(isolated_imx_home, site)
    _approve(isolated_imx_home, app_id)
    if changed:
        site.steps[0].append(_text("travel", "Are you willing to travel?", SemanticType.CUSTOM_TEXT,
                                   required=False))
    site.fills.clear()

    result, _ = _submit(isolated_imx_home, candidates, site, app_id)

    if changed:
        assert result.state is S.NEEDS_INPUT and result.message.startswith(MISMATCH_MESSAGE)
        assert "the questions differ from the ones the approved answers were prepared for" in result.message
        assert "submit" not in site.calls
    else:
        assert result.state is S.SUBMITTED, result.message
        assert [p.id for _, p in site.fills] == [packet_id]


# --- review fixes: the whole attempt, redacted details ----------------------------------------


@pytest.mark.parametrize("site_resumes", [False, True])
def test_pages_filled_before_a_question_stop_are_pinned_and_checked(
    isolated_imx_home, candidates, site_resumes
):
    """Page 1 is filled by a run that stops for a question on page 2; the next run carries
    on in the site's draft at page 2. The preparation pins page 1 with its questions, and
    a submission run fills every pinned page itself before it submits: a site that skips
    page 1 is not submitted."""
    motivation = _text("motivation", "Why this role?", SemanticType.CUSTOM_TEXT)
    site = Site(steps=[_contact()[:3], [motivation], []])  # contact, a question, the review page
    first = asyncio.run(_runner(isolated_imx_home, candidates, site,
                                prepare_only=True).apply(URL, candidate_id="c1"))
    assert first.state is S.NEEDS_INPUT and [m.field_id for m in first.missing_inputs] == ["motivation"]
    app_id = first.application_id
    [stop] = [e for e in _events(isolated_imx_home, app_id) if e.to_state is S.NEEDS_INPUT]
    page_one = stop.metadata["steps"][0]
    assert page_one["form_step"] == 0 and page_one["fields"] == [field_record(f) for f in site.steps[0]]

    site.resume_at = 1
    second = asyncio.run(_runner(isolated_imx_home, candidates, site, prepare_only=True,
                                 interaction=Answering("Forecasting is my field.")).resume(app_id))
    assert second.state is S.NEEDS_INPUT and "Prepared to the final review step" in second.message
    [ready] = [e for e in _events(isolated_imx_home, app_id) if e.event == "preparation.ready"]
    assert [s["form_step"] for s in ready.metadata["steps"]] == [0, 1, 2]
    assert ready.metadata["steps"][0] == {**page_one, "final": False}
    _approve(isolated_imx_home, app_id)
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        approval = store.submission_approval(app_id)
    assert approval is not None and approval.steps[0].packet_id == page_one["packet_id"]
    site.resume_at = 1 if site_resumes else None
    site.fills.clear()
    site.calls.clear()

    result, _ = _submit(isolated_imx_home, candidates, site, app_id)

    if site_resumes:
        # The site opened the kept draft at page 2 and this browser cannot go back: a
        # stop of its own that names the manual remedy, not "prepare it again".
        assert result.state is S.NEEDS_INPUT and result.message.startswith(KEPT_DRAFT_MESSAGE)
        assert ("at step 2, so the approved answers of step 1 could not be checked or filled"
                in result.message)
        assert "submit this application in the browser yourself" in result.message
        assert "Preparing it again reopens the same draft" in result.message
        assert MISMATCH_MESSAGE not in result.message and "approve it before" not in result.message
        assert "fill" not in site.calls and "submit" not in site.calls
        events = _events(isolated_imx_home, app_id)
        [withdrawn] = [e for e in events if e.event == "application.approval_invalidated"]
        assert withdrawn.metadata["reason"] == KEPT_DRAFT_MESSAGE
        assert withdrawn.metadata["details"] == [
            "the site opened its kept draft at step 2; step 1 could not be checked or filled"]
        stop = [e for e in events if e.to_state is S.NEEDS_INPUT][-1]
        assert (stop.metadata["reason"], stop.metadata["missing_inputs"]) == (KEPT_DRAFT_REASON, [])
        with ApplicationStore.open(isolated_imx_home.state_db) as store:
            assert store.list_attempts(app_id) == [] and store.approved_packet(app_id) is None
            assert store.is_preparation_only(app_id)
    else:
        assert result.state is S.SUBMITTED, result.message
        assert [(step, p.id) for step, p in site.fills] == [
            (s.form_step, s.packet_id) for s in approval.steps]


def test_values_read_back_from_the_page_are_redacted(isolated_imx_home, candidates):
    site = Site(steps=[_contact()])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    _approve(isolated_imx_home, app_id)
    site.fill_status = {"email": FieldFillStatus.VERIFICATION_MISMATCH}
    site.fill_detail = {"email": "reads back 'someone@example.test', not avery@example.test "
                                 "(+1 (555) 010-0199)"}

    result, _ = _submit(isolated_imx_home, candidates, site, app_id)

    [withdrawn] = [e for e in _events(isolated_imx_home, app_id)
                   if e.event == "application.approval_invalidated"]
    for text in (result.message, *withdrawn.metadata["details"]):
        assert "example.test" not in text and "0199" not in text and "555" not in text
    assert ("the approved answer to 'Email' does not read back (reads back '…', not …@… (…))"
            in withdrawn.metadata["details"])
    assert redact_detail("typed avery@example.test, +1 (555) 010-0199 on 2026-09-24") == (
        "typed …@…, … on …")
    assert redact_detail("option not found") == "option not found"


# --- review pass 5: kept drafts, skipped pages, the unapproved path ---------------------------


def _kept_draft_site() -> Site:
    motivation = _text("motivation", "Why this role?", SemanticType.CUSTOM_TEXT)
    return Site(steps=[_contact()[:3], [motivation], []])  # contact, a question, the review page


def _prepared_in_a_kept_draft(paths, candidates: Candidates, site: Site) -> str:
    """Page 1 filled by a run that stopped for the question on page 2; the next run carried
    on in the site's draft at page 2 and prepared the application, which is approved."""
    first = asyncio.run(_runner(paths, candidates, site, prepare_only=True)
                        .apply(URL, candidate_id="c1"))
    assert first.state is S.NEEDS_INPUT and first.missing_inputs
    site.resume_at = 1
    again = asyncio.run(_runner(paths, candidates, site, prepare_only=True,
                                interaction=Answering("Forecasting is my field."))
                        .resume(first.application_id))
    assert again.message.startswith("Prepared to the final review step"), again.message
    _approve(paths, first.application_id)
    return first.application_id


@pytest.mark.parametrize("opens_at", [1, 2])
def test_a_kept_draft_is_walked_back_so_every_approved_page_is_checked(
    isolated_imx_home, candidates, opens_at
):
    """A browser that can go back (``StepBack``) takes a kept draft back to its first
    approved page; each page is then compared and filled from its approved packet."""
    site = _kept_draft_site()
    app_id = _prepared_in_a_kept_draft(isolated_imx_home, candidates, site)
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        approval = store.submission_approval(app_id)
    assert approval is not None and [s.form_step for s in approval.steps] == [0, 1, 2]
    site.resume_at = opens_at  # the draft opens at page 2, or at the review page
    site.fills.clear()
    site.calls.clear()

    result, runner = _submit(isolated_imx_home, candidates, site, app_id, back=True)

    assert result.state is S.SUBMITTED, result.message
    assert site.calls.count("previous_step") == opens_at
    assert site.calls.index("previous_step") < site.calls.index("fill")  # nothing filled going back
    assert [(step, p.id) for step, p in site.fills] == [
        (s.form_step, s.packet_id) for s in approval.steps]
    assert site.calls.count("submit") == 1
    assert any("going back to step 1" in m for m in runner.interaction.messages)  # type: ignore[attr-defined]
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert [a.packet_id for a in store.list_attempts(app_id)] == [approval.packet_id]


@pytest.mark.parametrize("failure", ["ambiguous", "stays"])
def test_a_kept_draft_that_cannot_be_walked_back_names_the_manual_remedy(
    isolated_imx_home, candidates, failure
):
    site = _kept_draft_site()
    app_id = _prepared_in_a_kept_draft(isolated_imx_home, candidates, site)
    site.resume_at = 2
    if failure == "ambiguous":
        site.back_fails = True
    else:
        site.back_to = {2: 2}  # Back shows the same page again
    site.calls.clear()

    result, _ = _submit(isolated_imx_home, candidates, site, app_id, back=True)

    assert result.state is S.NEEDS_INPUT and result.message.startswith(KEPT_DRAFT_MESSAGE)
    assert ("at step 3, so the approved answers of steps 1 and 2 could not be checked or filled"
            in result.message)
    assert site.calls.count("previous_step") == 1
    assert "fill" not in site.calls and "submit" not in site.calls
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert store.approved_packet(app_id) is None and store.list_attempts(app_id) == []


def test_a_kept_draft_is_reported_with_the_manual_remedy_every_time(
    isolated_imx_home, candidates, capsys
):
    """Preparing a kept draft again lands in the same draft and pins the same pages, so
    every submission stops the same way; neither the message nor ``status`` ever says
    to prepare it again."""
    site = _kept_draft_site()
    app_id = _prepared_in_a_kept_draft(isolated_imx_home, candidates, site)
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        pinned = store.submission_approval(app_id)
    assert pinned is not None

    first, _ = _submit(isolated_imx_home, candidates, site, app_id)  # the draft opens at page 2
    assert first.message.startswith(KEPT_DRAFT_MESSAGE)
    assert main(["status", app_id]) == EXIT_OK
    status = capsys.readouterr().out
    assert "kept draft:   the site resumed a draft it kept at a later page" in status
    assert f"next:         {KEPT_DRAFT_STEP}" in status
    assert f"resume {app_id}" not in status and f"approve {app_id}" not in status
    assert "prepared:" not in status  # the preparation is no longer the current stop

    filled = len(site.fills)
    again = asyncio.run(_runner(isolated_imx_home, candidates, site, prepare_only=True)
                        .resume(app_id))
    assert again.message.startswith("Prepared to the final review step"), again.message
    assert [step for step, _ in site.fills[filled:]] == [1, 2]  # the draft again: no page 1
    _approve(isolated_imx_home, app_id)
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        repinned = store.submission_approval(app_id)
    assert repinned is not None and repinned.steps[0] == pinned.steps[0]  # the same page 1

    second, _ = _submit(isolated_imx_home, candidates, site, app_id)
    assert second.message == first.message
    assert "submit" not in site.calls
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert store.list_attempts(app_id) == []
        assert [e.metadata["reason"] for e in store.list_events(app_id)
                if e.event == "application.approval_invalidated"] == [KEPT_DRAFT_MESSAGE] * 2


def test_a_site_that_skips_an_approved_page_is_a_changed_form(isolated_imx_home, candidates):
    """Only the first page of a run can be a kept draft: a page the site skips after this
    run filled one withdraws the approval as a change, even for a browser that can go
    back."""
    site = Site(steps=[_contact()[:3], [_heard(required=False)], []])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    _approve(isolated_imx_home, app_id)
    site.advance_to = {0: 2}  # Next on page 1 now leads straight to the review page
    site.calls.clear()

    result, _ = _submit(isolated_imx_home, candidates, site, app_id, back=True)

    assert result.state is S.NEEDS_INPUT and result.message.startswith(MISMATCH_MESSAGE)
    assert "the site did not show step 2, so its approved answers could not be checked" in result.message
    assert "previous_step" not in site.calls and "submit" not in site.calls
    assert site.calls.count("fill") == 1


def test_a_browser_that_can_go_back_is_a_step_back():
    assert isinstance(BackBrowser(Site(steps=[[]])), StepBack)
    assert not isinstance(FakeBrowser(Site(steps=[[]])), StepBack)


def test_a_submitting_runner_only_prepares_an_application_that_was_never_restricted(
    isolated_imx_home, candidates
):
    """``prepare_only=False`` submits only an authorized approval: ``apply`` of a new
    application records the no-submit restriction and prepares it."""
    site = Site(steps=[_contact()])
    runner = _runner(isolated_imx_home, candidates, site, prepare_only=False, resolver=_factual())
    assert runner.submit_unapproved is False

    outcome = asyncio.run(runner.apply(URL, candidate_id="c1"))

    assert outcome.state is S.NEEDS_INPUT, outcome.message
    assert outcome.message.startswith("Prepared to the final review step")
    assert "submit" not in site.calls and site.options[-1].allow_submission is False
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert store.is_preparation_only(outcome.application_id)
        assert store.list_attempts(outcome.application_id) == []
    submitting = create_submission_runner(isolated_imx_home, headless=True,
                                          interaction=NoninteractiveInteraction())
    assert submitting.submit_unapproved is False


def test_the_last_guard_before_a_submit_refuses_without_an_approval(isolated_imx_home, candidates):
    """``_run`` restricts every run without an authorized approval, so the guard in
    ``_submit`` is not reached through it; called directly, it refuses."""
    site = Site(steps=[_contact()])
    runner = _runner(isolated_imx_home, candidates, site, prepare_only=False)
    form = site.form(0)
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        app = store.record_request("c1", URL).application
        claim = store.claim(app.id, "guard-test")
        for state in (S.INSPECTING, S.PACKET_READY, S.FILLING):
            store.transition(claim, state)
        packet = ApplicationPacket(application_id=app.id, job_id=app.job_id,
                                   candidate_id=app.candidate_id, form_url=form.url, form_step=0,
                                   form_fingerprint=form.fingerprint, answers=[])
        run = _Run(runner, store, claim, candidates.load("c1"), FakeBrowser(site), URL)
        with pytest.raises(_Stop) as stopped:
            asyncio.run(run._submit(packet))
        assert stopped.value.outcome.message.startswith(NOT_AUTHORIZED_MESSAGE)
        assert store.list_attempts(app.id) == []
        assert store.get_application(app.id).state is S.FAILED_RETRYABLE
    assert "submit" not in site.calls


# --- round 14: a data-processing consent page in front of an approved application ------------

CONSENT_MESSAGE = ("Jobvite asks you to accept its data-processing consent before the application "
                   "form. Accept it yourself in the browser window, then continue; a consent is "
                   "never accepted automatically.")


class ConsentBrowser(FakeBrowser):
    """A site whose apply URL shows Jobvite's consent page first; it offers the consent
    question (``data_consent``) and records whether anything asked for or accepted it."""

    async def open(self, url: str) -> PageInspection:
        await super().open(url)
        return PageInspection(kind=PageKind.SIGN_IN_REQUIRED, observed_url=URL, message=CONSENT_MESSAGE)

    async def data_consent(self, residence: str | None = None) -> ApplicationForm:
        self.site.calls.append("data_consent")
        return ApplicationForm(url=URL, fields=[ApplicationField(
            id="data-consent", selector="#jv-country-select", label="I accept the Global POLICY",
            control_type=ControlType.CHECKBOX, semantic_type=SemanticType.CONSENT, required=True)])

    async def accept_data_consent(self, question: ApplicationForm, residence: str | None = None) -> PageInspection:
        self.site.calls.append("accept_data_consent")
        return self._page()


class ConsentFactory(FakeFactory):
    async def start(self, options: BrowserOptions) -> FakeBrowser:
        self.starts += 1
        self.site.options.append(options)
        return ConsentBrowser(self.site)


def test_a_submission_run_leaves_a_consent_page_to_the_person(isolated_imx_home, candidates):
    """A submission run of an approval resolves nothing, so it never asks whether the
    person's statement covers the consent, let alone accepts it: the page is theirs."""
    site = Site(steps=[_contact()])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    _approve(isolated_imx_home, app_id)
    site.calls.clear()
    runner = LocalApplicationRunner(
        paths=isolated_imx_home, interaction=NoninteractiveInteraction(), headless=True,
        browser_factory=ConsentFactory(site), candidates=candidates, resolver=NeverResolve(),
        limits=RunLimits(max_steps=8, max_same_form=2), prepare_only=False)
    result = asyncio.run(runner.submit(app_id))
    assert result.state is S.NEEDS_INPUT, result.message
    [need] = result.missing_inputs
    assert (need.label, need.reason) == ("Accept the data-processing consent", MissingReason.USER_ACTION)
    assert "data_consent" not in site.calls and "accept_data_consent" not in site.calls
    assert "submit" not in site.calls and "fill" not in site.calls


# --- round 4: omissions, rejections, a browser that cannot attach files ------------------------


BANNER = ("We couldn\u2019t submit your application. Your application submission was flagged as "
          "possible spam. If you believe this was a mistake, please submit your application "
          "again. Try these steps: turn off your VPN, turn off browser extensions, try another "
          "browser or network.")
"""A fictional copy of Ashby's refusal banner."""


def _rules_step(*fields: ApplicationField) -> _ApprovedStep:
    form = ApplicationForm(url=URL, step=0, fields=list(fields), is_final_step=True,
                           submit_selector="#submit")
    answers = [PacketAnswer(field_id=f.id, semantic_type=f.semantic_type,
                            value=TextValue(text="fictional"),
                            provenance=(Provenance(source=AnswerSource.PROFILE_IDENTITY)
                                        if f.semantic_type is SemanticType.EMAIL else
                                        Provenance(source=AnswerSource.USER_INPUT,
                                                   reference_ids=["ui_fictional"])))
               for f in fields if f.control_type is ControlType.TEXT]
    packet = ApplicationPacket(application_id="app_x", job_id="job_x", candidate_id="c1",
                               form_url=URL, form_step=0, form_fingerprint=form.fingerprint,
                               answers=answers)
    return _ApprovedStep(packet=packet, fields={f.id: field_record(f) for f in fields})


def test_absent_or_changed_optional_questions_are_omissions_and_required_ones_withhold():
    gender = _text("gender", "Gender", SemanticType.CUSTOM_TEXT)  # required, self-identification
    email = _text("email", "Email", SemanticType.EMAIL)
    step = _rules_step(email, gender, _heard())
    changed_heard = _heard(options=[*HEARD, FieldOption(value="src_x", label="X")])

    def form(*fields: ApplicationField) -> ApplicationForm:
        return ApplicationForm(url=URL, step=0, fields=list(fields), is_final_step=True,
                               submit_selector="#submit")

    now = form(email, changed_heard, _text("notes", "Notes", SemanticType.CUSTOM_TEXT, required=False))
    assert approval_omissions(step, now) == {
        "gender": "'Gender' (optional; not on the form at submit)",
        "heard": "'How did you hear about us?' (optional; its options changed, left unanswered)",
        "notes": "'Notes' (a new optional question, left unanswered)",
    }
    assert approval_mismatches(step, now, final_step=0) == []
    # A required question absent, changed, or new still withholds the submission.
    assert approval_mismatches(step, form(gender, _heard()), final_step=0) == [
        "the question 'Email' is no longer on the form"]
    assert approval_mismatches(step, form(_text("email", "Work email", SemanticType.EMAIL), gender),
                               final_step=0) == ["the question 'Email' changed"]
    assert approval_mismatches(step, form(email, gender, _text("travel", "Travel?", SemanticType.CUSTOM_TEXT)),
                               final_step=0) == ["a new required question 'Travel?' appeared"]


def test_optional_questions_absent_at_submit_are_left_out_and_noted_on_the_receipt(
    isolated_imx_home, candidates
):
    linkedin = _text("linkedin", "LinkedIn profile", SemanticType.LINKEDIN, required=False)
    gender = _text("gender", "Gender", SemanticType.CUSTOM_TEXT)  # answered while preparing
    site = Site(steps=[[*_contact(), linkedin, gender]])
    app_id = _prepare(isolated_imx_home, candidates, site,
                      interaction=Answering("Prefer not to say")).application_id
    packet_id = _approve(isolated_imx_home, app_id)
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        approved = store.get_packet(packet_id)
    assert {a.field_id for a in approved.answers} >= {"linkedin", "gender"}
    site.steps[0] = _contact()  # the site renders its optional block conditionally
    site.fills.clear()

    result, _ = _submit(isolated_imx_home, candidates, site, app_id)

    assert result.state is S.SUBMITTED, result.message
    assert "Left unanswered: 'LinkedIn profile' (optional; not on the form at submit)" in result.message
    [(_, filled)] = site.fills
    assert filled.id == packet_id and {a.field_id for a in filled.answers} == {
        "first_name", "last_name", "email"}
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        receipt = store.get_receipt(app_id)
    assert receipt is not None
    assert "submitted without 'Gender' (optional; not on the form at submit)" in receipt.signals
    assert "submitted without 'LinkedIn profile' (optional; not on the form at submit)" in receipt.signals


def test_a_submission_the_site_refuses_is_rejected_and_a_later_run_submits_it(isolated_imx_home, candidates):
    site = Site(steps=[_contact()])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    packet_id = _approve(isolated_imx_home, app_id)
    refusal = site_refusal(BANNER)
    assert refusal == ("We couldn\u2019t submit your application. Your application submission was "
                       "flagged as possible spam.")
    site.confirm = [GenericApplicationBrowser._refused(refusal, URL, [])]

    rejected, _ = _submit(isolated_imx_home, candidates, site, app_id)

    assert rejected.state is S.FAILED_RETRYABLE and rejected.message.startswith(REJECTED_MESSAGE)
    assert "flagged as possible spam" in rejected.message and "the approval stands" in rejected.message
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        [attempt] = store.list_attempts(app_id)
        assert attempt.outcome is SubmissionOutcome.NOT_SUBMITTED
        assert store.approved_packet(app_id) == packet_id  # nothing was received; approval stands
    # The next run (for example from a real browser) submits the same approved packet.
    again, _ = _submit(isolated_imx_home, candidates, site, app_id)
    assert again.state is S.SUBMITTED, again.message
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert [a.packet_id for a in store.list_attempts(app_id)] == [packet_id, packet_id]


def test_an_uncertain_submission_whose_recorded_page_refused_it_reconciles_as_not_received(
    isolated_imx_home, candidates
):
    site = Site(steps=[_contact()])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    _approve(isolated_imx_home, app_id)
    site.confirm = [SubmissionObservation(outcome=SubmissionOutcome.UNKNOWN,
                                          signals=["observed: the form is shown again"])]
    unknown, _ = _submit(isolated_imx_home, candidates, site, app_id)
    assert unknown.state is S.SUBMISSION_UNKNOWN
    # The page text the run saved right after its submit (as a run before this rule did).
    rel = f"{app_id}/003-after-submit.txt"
    page = isolated_imx_home.artifacts_dir / rel
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("Apply for this job\n" + BANNER, encoding="utf-8")
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        claim = store.claim(app_id, "evidence")
        store.add_evidence(claim, [EvidenceRef(kind=EvidenceKind.PAGE_TEXT, path=rel,
                                               sha256=sha256_file(page),
                                               description=f"after-submit at {URL} (visible text)")])
        store.release(claim)

    settled = asyncio.run(_runner(isolated_imx_home, candidates, site, prepare_only=False).reconcile(app_id))

    assert settled.state is S.FAILED_RETRYABLE and settled.message.startswith(REJECTED_MESSAGE)
    assert "flagged as possible spam" in settled.message
    again, _ = _submit(isolated_imx_home, candidates, site, app_id)
    assert again.state is S.SUBMITTED, again.message


@pytest.mark.parametrize("present", [False, True])
def test_a_browser_that_cannot_attach_files_never_sends_without_the_resume(
    isolated_imx_home, candidates, present
):
    resume = ApplicationField(id="resume", label="Resume", selector="#resume",
                              semantic_type=SemanticType.RESUME, control_type=ControlType.FILE,
                              required=True, accept=[".pdf"])
    site = Site(steps=[[*_contact(), resume]])
    app_id = _prepare(isolated_imx_home, candidates, site).application_id
    _approve(isolated_imx_home, app_id)
    site.attaches_files = False  # e.g. --browser opencli
    site.calls.clear()

    result, _ = _submit(isolated_imx_home, candidates, site, app_id,
                        interaction=NoninteractiveInteraction(allow_browser_action=present))

    if present:  # the person attached it when asked; the fill verified it, then the submit
        assert site.calls.count("wait_for_user") == 1 and result.state is S.SUBMITTED
    else:
        assert result.state is S.NEEDS_INPUT and "submit" not in site.calls and "fill" not in site.calls
        [need] = result.missing_inputs
        assert need.field_id == "resume" and need.reason is MissingReason.USER_ACTION
        assert "resume-avery-example.pdf" in need.prompt and "never sent without it" in need.prompt
        with ApplicationStore.open(isolated_imx_home.state_db) as store:
            assert store.approved_packet(app_id) is not None and store.list_attempts(app_id) == []
