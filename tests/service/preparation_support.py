"""Fictional runs that record what the I1 runner records, for the WP3 view tests.

``Scenario`` is an executor factory for ``conftest.serve(..., executor_factory=...)``.
Each dispatched run (apply or resume) takes the next ``RunBody`` and executes it under
a real store claim. ``RunContext`` offers the runner's store recipes
(``apps/cli/src/interviewmaxxing_cli/runner.py``):

* ``fill``: one form step, INSPECTING -> packet -> PACKET_READY -> FILLING.
* ``prepare``: the ``_submit`` preparation branch: a screenshot of the filled final
  review page, ``preparation.ready``, then ``_stop(NEEDS_INPUT)`` (INSPECTING ->
  NEEDS_INPUT with ``missing_inputs`` empty and the "prepared for final review" reason).
* ``ask``: a step with questions only the user can answer (``_stop(NEEDS_INPUT)``).
* ``sign_in``: a sign-in page the user did not act on (a fieldless ``USER_ACTION``).
* ``fail``: a browser error before submit, after a screenshot (FAILED_RETRYABLE).

Every run of a ``prepare_only`` scenario first records the user's no-submit boundary
(``require_preparation_only``), as the runner does for prepare-only runs. All data is
fictional; nothing opens a browser or a network connection.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from interviewmaxxing_core import (
    AnswerSource,
    AnswerValue,
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    ArtifactRef,
    Claim,
    ControlType,
    EvidenceKind,
    EvidenceRef,
    FieldOption,
    FileValue,
    IdentityEvidenceKind,
    JobIdentityObservation,
    LocalPaths,
    MissingInput,
    MissingReason,
    PacketAnswer,
    Provenance,
    SemanticType,
    UserInput,
)
from interviewmaxxing_service import ServiceConfig, ServiceInteraction

from .conftest import Harness

S = ApplicationState
RUNNER_OWNER = "fictional-prepare-runner"
PREPARED_REASON = "prepared for final review; submission disabled"
PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def iso(value: datetime) -> str:
    """The service's timestamp format (UTC, milliseconds, ``Z``)."""
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- fictional fields and answers ----------------------------------------------------------


def make_field(
    field_id: str,
    label: str,
    control: ControlType,
    semantic: SemanticType = SemanticType.UNKNOWN,
    *,
    required: bool = False,
    options: Sequence[tuple[str, str]] | None = None,
    help_text: str | None = None,
    accept: list[str] | None = None,
) -> ApplicationField:
    return ApplicationField(
        id=field_id, label=label, semantic_type=semantic, control_type=control,
        selector=f"#{field_id}", required=required, help_text=help_text, accept=accept,
        options=[FieldOption(value=v, label=lbl) for v, lbl in options] if options else None,
    )


def make_form(
    url: str, step: int, fields: Sequence[ApplicationField], *, final: bool
) -> ApplicationForm:
    return ApplicationForm(
        url=url, ats_type="greenhouse", step=step, fields=list(fields), is_final_step=final,
        submit_selector="#submit" if final else None, next_selector=None if final else "#next",
    )


def answer(
    form: ApplicationForm,
    field_id: str,
    value: AnswerValue,
    source: AnswerSource,
    refs: Sequence[str] = (),
    *,
    confidence: float = 1.0,
    note: str | None = None,
) -> PacketAnswer:
    """A packet answer for ``field_id`` of ``form`` (its semantic type is the field's)."""
    return PacketAnswer(
        field_id=field_id, semantic_type=form.field(field_id).semantic_type, value=value,
        provenance=Provenance(source=source, reference_ids=list(refs), note=note),
        confidence=confidence,
    )


def question(
    form: ApplicationForm,
    field_id: str,
    *,
    reason: MissingReason = MissingReason.NO_ANSWER,
    suggestions: Sequence[str] | None = None,
) -> MissingInput:
    """``MissingInput.for_field``; a lookup gets the site's ``suggestions`` as options."""
    item = MissingInput.for_field(
        form, form.field(field_id), reason=reason,
        prompt=f"Please answer: {form.field(field_id).label}",
    )
    if suggestions is not None:
        item = item.model_copy(
            update={"options": [FieldOption(value=s, label=s) for s in suggestions]}
        )
    return item


# --- the runs ---------------------------------------------------------------------------------


@dataclass
class RunContext:
    """One run's claim on the application, with the runner's store recipes."""

    store: ApplicationStore
    claim: Claim
    paths: LocalPaths
    interaction: ServiceInteraction
    index: int
    """1-based ordinal of this run within the scenario (names its evidence files)."""
    packets: list[ApplicationPacket] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)

    @property
    def app_id(self) -> str:
        return self.claim.application_id

    @property
    def app(self) -> Application:
        return self.store.get_application(self.app_id)

    def to(self, state: ApplicationState, **kwargs: Any) -> None:
        """``runner._to``: transition unless the application is already there."""
        if self.app.state is not state:
            self.store.transition(self.claim, state, **kwargs)

    def pinned_resume(self) -> FileValue:
        pinned = self.store.pinned_resume(self.app_id)
        assert pinned is not None, "the service pins the selected resume before any run"
        return FileValue(artifact=ArtifactRef.model_validate(pinned.model_dump(
            include={"id", "path", "filename", "media_type", "sha256", "size_bytes"}
        )))

    def user_input(self, form: ApplicationForm, field_id: str) -> UserInput:
        """The user's stored answer to ``field_id`` on ``form`` (``get_user_inputs``)."""
        found = [u for u in self.store.get_user_inputs(self.app_id, form) if u.field_id == field_id]
        assert found, f"no stored answer for {field_id}"
        return found[-1]

    def from_user(self, form: ApplicationForm, field_id: str) -> PacketAnswer:
        given = self.user_input(form, field_id)
        return answer(form, field_id, given.value, AnswerSource.USER_INPUT, [given.id])

    def bind_identity(self, *, title: str, company: str, job_id: str = "4012") -> None:
        """``runner._bind_identity``: the ATS job id observed on the page."""
        self.to(S.INSPECTING)
        self.store.bind_job_identity(self.claim, JobIdentityObservation(
            ats_type="greenhouse", ats_tenant="fictional-co", external_job_id=job_id,
            evidence_kind=IdentityEvidenceKind.ATS_JOB_ID_ON_PAGE,
            evidence=f"fictional job id {job_id} in the form action", title=title,
            company=company,
        ))

    def save(
        self,
        form: ApplicationForm,
        answers: Sequence[PacketAnswer] = (),
        missing: Sequence[MissingInput] = (),
    ) -> ApplicationPacket:
        app = self.app
        packet = ApplicationPacket(
            application_id=app.id, job_id=app.job_id, candidate_id=app.candidate_id,
            form_url=form.url, form_step=form.step, form_fingerprint=form.fingerprint,
            answers=list(answers), missing_inputs=list(missing),
        )
        problems = packet.problems_against(form)
        assert problems == [], problems
        self.store.save_packet(self.claim, packet)
        self.packets.append(packet)
        return packet

    def fill(self, form: ApplicationForm, answers: Sequence[PacketAnswer]) -> ApplicationPacket:
        """One form step: INSPECTING, its packet, PACKET_READY, FILLING."""
        self.to(S.INSPECTING)
        packet = self.save(form, answers)
        self.to(S.PACKET_READY)
        self.to(S.FILLING)
        return packet

    def screenshot(self, name: str, description: str) -> EvidenceRef:
        directory = self.paths.application_artifacts(self.app_id)
        directory.mkdir(parents=True, exist_ok=True)
        content = PNG_HEADER + f"fictional {name}".encode()
        (directory / name).write_bytes(content)
        ref = EvidenceRef(
            kind=EvidenceKind.SCREENSHOT, path=f"{self.app_id}/{name}",
            sha256=hashlib.sha256(content).hexdigest(), description=description,
        )
        self.store.add_evidence(self.claim, [ref])
        self.evidence.append(ref)
        return ref

    def stop(self, missing: Sequence[MissingInput], *, reason: str) -> None:
        """``runner._stop(NEEDS_INPUT)``: a fresh INSPECTING, then NEEDS_INPUT recording
        exactly what the application waits for."""
        self.to(S.INSPECTING)
        self.store.transition(self.claim, S.NEEDS_INPUT, metadata={
            "missing_inputs": [m.model_dump(mode="json") for m in missing],
            "reason": reason,
        })

    def prepare(
        self,
        form: ApplicationForm,
        packet: ApplicationPacket,
        *,
        captcha_pending: bool = False,
        remaining: Sequence[MissingInput] = (),
    ) -> EvidenceRef:
        """The runner's preparation branch on the filled final step."""
        shot = self.screenshot(
            f"review-{self.index}.png", f"prepared-review at {form.url} (screenshot)"
        )
        self.store.append_event(self.claim, "preparation.ready", {
            "form_url": form.url,
            "form_step": form.step,
            "form_fingerprint": form.fingerprint,
            "packet_id": packet.id,
            "submitted": False,
            "browser_location": None,
            "captcha_pending": captcha_pending,
        })
        self.stop(remaining, reason=PREPARED_REASON)
        return shot

    def ask(
        self,
        form: ApplicationForm,
        answers: Sequence[PacketAnswer],
        missing: Sequence[MissingInput],
    ) -> ApplicationPacket:
        """A step with questions only the user can answer: its packet, then the stop."""
        self.to(S.INSPECTING)
        packet = self.save(form, answers, missing)
        self.stop(missing, reason="questions need your answers")
        return packet

    def sign_in(self) -> None:
        """A sign-in page the user was not asked to act on (``user_action_needs``)."""
        self.to(S.INSPECTING)
        self.stop([MissingInput(
            field_id=None, label="Sign in", reason=MissingReason.USER_ACTION,
            prompt="Sign in in the browser window, then continue.",
        )], reason="user action required")

    def fail(self, message: str) -> EvidenceRef:
        """A browser error before submit: a screenshot of the page, FAILED_RETRYABLE."""
        self.to(S.INSPECTING)
        shot = self.screenshot(f"error-{self.index}.png", "page when the run stopped (screenshot)")
        self.store.transition(self.claim, S.FAILED_RETRYABLE, failure_reason=message)
        return shot


RunBody = Callable[[RunContext], object]


class Scenario:
    """The runs a fictional site takes, in order; each dispatched run takes the next."""

    def __init__(self, paths: LocalPaths, *runs: RunBody, prepare_only: bool = True) -> None:
        self.paths = paths
        self.pending: deque[RunBody] = deque(runs)
        self.prepare_only = prepare_only
        self.interactions: list[ServiceInteraction] = []
        self.contexts: list[RunContext] = []
        self.errors: list[BaseException] = []
        """Exceptions raised by run bodies (the service would record them as a failed run)."""

    def then(self, *runs: RunBody) -> None:
        self.pending.extend(runs)

    def settle(self, h: Harness) -> None:
        """Wait for the dispatched run and fail loudly if its body raised."""
        h.wait_idle()
        assert self.errors == [], f"a fictional run raised: {self.errors!r}"

    def factory(self, config: ServiceConfig) -> Callable[[ServiceInteraction], ScenarioRunner]:
        """``executor_factory`` for ``conftest.serve``."""

        def make(interaction: ServiceInteraction) -> ScenarioRunner:
            self.interactions.append(interaction)
            return ScenarioRunner(self, interaction)

        return make


class ScenarioRunner:
    """``ApplicationExecutor`` that runs the scenario's next body under a claim."""

    def __init__(self, scenario: Scenario, interaction: ServiceInteraction) -> None:
        self.scenario = scenario
        self.interaction = interaction

    def _outcome(self, store: ApplicationStore, app_id: str) -> ApplyOutcome:
        app = store.get_application(app_id)
        return ApplyOutcome(application_id=app_id, state=app.state,
                            receipt=store.get_receipt(app_id))

    async def apply(self, application_url: str, *, candidate_id: str) -> ApplyOutcome:
        with ApplicationStore.open(self.scenario.paths.state_db) as store:
            result = store.record_request(candidate_id, application_url)
            if not result.may_proceed:
                return self._outcome(store, result.application.id)
            return self._work(store, result.application.id)

    async def resume(self, application_id: str) -> ApplyOutcome:
        with ApplicationStore.open(self.scenario.paths.state_db) as store:
            return self._work(store, application_id)

    async def reconcile(self, application_id: str) -> ApplyOutcome:
        with ApplicationStore.open(self.scenario.paths.state_db) as store:
            return self._outcome(store, application_id)

    def _work(self, store: ApplicationStore, app_id: str) -> ApplyOutcome:
        assert self.scenario.pending, "the scenario has no run left for this dispatch"
        body = self.scenario.pending.popleft()
        claim = store.claim(app_id, RUNNER_OWNER)
        try:
            if self.scenario.prepare_only:
                store.require_preparation_only(claim)
            context = RunContext(
                store=store, claim=claim, paths=self.scenario.paths,
                interaction=self.interaction, index=len(self.scenario.contexts) + 1,
            )
            self.scenario.contexts.append(context)
            body(context)
        except BaseException as exc:
            self.scenario.errors.append(exc)
            raise
        finally:
            store.release(claim)
        return self._outcome(store, app_id)
