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

from interviewmaxxing_cli.runner import (
    MISMATCH_MESSAGE,
    NOT_AUTHORIZED_MESSAGE,
    LocalApplicationRunner,
    NoninteractiveInteraction,
    RunLimits,
    create_submission_runner,
    field_record,
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
    options: list[BrowserOptions] = field(default_factory=list)

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

    def _page(self) -> PageInspection:
        form = self.site.form(self.step)
        return PageInspection(kind=PageKind.APPLICATION_FORM, observed_url=form.url, form=form,
                              job_identity=IDENTITY)

    async def open(self, url: str) -> PageInspection:
        self.site.runs += 1
        self.site.showing_errors = {}
        self.site.calls.append("open")
        self.step = 0
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
                            status=self.site.fill_status.get(a.field_id, FieldFillStatus.FILLED))
            for a in packet.answers])

    async def advance(self) -> NavigationResult:
        self.site.calls.append("advance")
        self.step += 1
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


class FakeFactory:
    def __init__(self, site: Site) -> None:
        self.site = site
        self.starts = 0

    async def start(self, options: BrowserOptions) -> FakeBrowser:
        self.starts += 1
        self.site.options.append(options)
        return FakeBrowser(self.site)


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
            resolver: Any = None) -> LocalApplicationRunner:
    return LocalApplicationRunner(
        paths=paths, interaction=interaction or NoninteractiveInteraction(), headless=True,
        browser_factory=FakeFactory(site), candidates=candidates,
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
        "new optional question": (
            lambda site: site.steps[0].append(new_required.model_copy(update={"required": False})),
            "a new question 'Are you willing to travel?' appeared"),
        "changed options": (
            lambda site: site.steps[0].__setitem__(3, _heard(options=[*HEARD, FieldOption(value="src_x", label="X")])),
            "the options of 'How did you hear about us?' changed"),
        "changed wording": (
            lambda site: site.steps[0].__setitem__(2, _text("email", "Work email", SemanticType.EMAIL)),
            "the question 'Email' changed"),
        "missing question": (
            lambda site: site.steps[0].pop(1),
            "the question 'Last name' is no longer on the form"),
        "now required": (
            lambda site: site.steps[0].__setitem__(3, _heard(required=True)),
            "'How did you hear about us?' is now required"),
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
