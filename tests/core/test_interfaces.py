"""The published service interfaces compose with the store for a full local flow.

The fakes below stand in for downstream packages (candidate, generation, browser);
they exist only to prove the interfaces and store fit together, including the
missing-input resume path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from interviewmaxxing_core import (
    ApplicationBrowser,
    ApplicationForm,
    ApplicationPacket,
    ApplicationRunner,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    ATSAdapter,
    BrowserOptions,
    BrowserSessionFactory,
    CandidateLoader,
    CandidateProfile,
    ChoiceValue,
    FieldFillResult,
    FieldFillStatus,
    FillResult,
    MissingInput,
    NavigationResult,
    PacketAnswer,
    PacketContext,
    PacketResolver,
    PageInspection,
    PageKind,
    Provenance,
    SubmissionObservation,
    SubmitActionResult,
    TextValue,
    UserInput,
    UserInteraction,
    new_id,
)

S = ApplicationState


class FakeCandidates:
    def __init__(self, profile: CandidateProfile) -> None:
        self.profile = profile

    def load(self, candidate_id: str) -> CandidateProfile:
        assert candidate_id == self.profile.id
        return self.profile


class FakeResolver:
    """Uses the fixture packet, replacing missing items with user inputs."""

    def __init__(self, base: ApplicationPacket) -> None:
        self.base = base

    async def resolve(self, context: PacketContext) -> ApplicationPacket:
        by_field = {u.field_id: u for u in context.user_inputs}
        answers = list(self.base.answers)
        missing = []
        for item in self.base.missing_inputs:
            user = by_field.get(item.field_id or "")
            if user is None:
                missing.append(item)
            else:
                answers.append(PacketAnswer(
                    field_id=user.field_id, semantic_type=item.semantic_type, value=user.value,
                    provenance=Provenance(source="USER_INPUT", reference_ids=[user.id]),
                ))
        return self.base.model_copy(update={
            "id": new_id("pkt"), "application_id": context.application.id, "job_id": context.job.id,
            "candidate_id": context.candidate.id, "answers": answers, "missing_inputs": missing,
            "form_url": context.form.url, "form_step": context.form.step,
            "form_fingerprint": context.form.fingerprint,
        })


class FakeBrowser:
    def __init__(self, form: ApplicationForm, confirmation: SubmissionObservation) -> None:
        self.form = form
        self.confirmation = confirmation
        self.submits = 0

    def _page(self) -> PageInspection:
        return PageInspection(kind=PageKind.APPLICATION_FORM, observed_url=self.form.url,
                              form=self.form)

    async def open(self, url: str) -> PageInspection:
        return self._page()

    async def inspect(self) -> PageInspection:
        return self._page()

    async def fill(self, form: ApplicationForm, packet: ApplicationPacket) -> FillResult:
        assert packet.problems_against(form) == []
        return FillResult(form_step=form.step, fields=[
            FieldFillResult(field_id=a.field_id, status=FieldFillStatus.FILLED)
            for a in packet.answers
        ])

    async def advance(self) -> NavigationResult:
        raise AssertionError("single-step form")

    async def submit(self) -> SubmitActionResult:
        self.submits += 1
        return SubmitActionResult(dispatched=True)

    async def confirm(self) -> SubmissionObservation:
        return self.confirmation

    async def wait_for_user(self, reason: str, timeout_s: float | None = None) -> PageInspection:
        return self._page()

    async def close(self) -> None:
        return None


class FakeFactory:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser

    async def start(self, options: BrowserOptions) -> ApplicationBrowser:
        return self.browser


class ScriptedUser:
    def __init__(self, answers: dict[str, UserInput]) -> None:
        self.answers = answers

    async def request_inputs(self, missing: Sequence[MissingInput]) -> Sequence[UserInput]:
        return [self.answers[m.field_id] for m in missing if m.field_id in self.answers]

    async def request_action(self, message: str) -> bool:
        return False

    async def progress(self, message: str) -> None:
        return None


class MiniRunner:
    """A minimal orchestration over the interfaces (the real one is task I1)."""

    def __init__(self, store: ApplicationStore, candidates: CandidateLoader,
                 resolver: PacketResolver, browsers: BrowserSessionFactory,
                 user: UserInteraction | None, artifacts: Path) -> None:
        self.store, self.candidates, self.resolver = store, candidates, resolver
        self.browsers, self.user, self.artifacts = browsers, user, artifacts

    async def apply(self, application_url: str, *, candidate_id: str) -> ApplyOutcome:
        result = self.store.record_request(candidate_id, application_url)
        return await self._run(result.application.id, application_url)

    async def resume(self, application_id: str) -> ApplyOutcome:
        app = self.store.get_application(application_id)
        url = self.store.list_requests(app.id)[0].application_url
        return await self._run(app.id, url)

    async def _run(self, app_id: str, url: str) -> ApplyOutcome:
        claim = self.store.claim(app_id, "mini-runner")
        app = self.store.get_application(app_id)
        candidate = self.candidates.load(app.candidate_id)
        browser = await self.browsers.start(BrowserOptions(
            artifacts_dir=self.artifacts / app_id, artifacts_root=self.artifacts))
        try:
            page = await browser.open(url)
            self.store.transition(claim, S.INSPECTING)
            assert page.form is not None
            ctx = PacketContext(application=app, job=self.store.get_job(app.job_id),
                                form=page.form, candidate=candidate,
                                user_inputs=self.store.get_user_inputs(app_id, page.form))
            packet = await self.resolver.resolve(ctx)
            assert ctx.problems(packet) == []
            self.store.save_packet(claim, packet)
            if not packet.is_complete:
                self.store.transition(claim, S.NEEDS_INPUT)
                self.store.release(claim)
                return ApplyOutcome(application_id=app_id, state=S.NEEDS_INPUT,
                                    missing_inputs=packet.missing_inputs)
            self.store.transition(claim, S.PACKET_READY)
            self.store.transition(claim, S.FILLING)
            assert (await browser.fill(page.form, packet)).ok
            attempt = self.store.begin_submission(claim, packet_id=packet.id)
            await browser.submit()
            done = self.store.record_submission_outcome(claim, attempt.id,
                                                        await browser.confirm())
            return ApplyOutcome(application_id=app_id, state=done.state,
                                receipt=self.store.get_receipt(app_id))
        finally:
            await browser.close()


def test_fakes_satisfy_the_runtime_protocols(fictional_candidate, mock_packet, mock_form,
                                             accepted_observation):
    browser = FakeBrowser(mock_form, accepted_observation)
    assert isinstance(FakeCandidates(fictional_candidate), CandidateLoader)
    assert isinstance(FakeResolver(mock_packet), PacketResolver)
    assert isinstance(browser, ApplicationBrowser)
    assert isinstance(FakeFactory(browser), BrowserSessionFactory)
    assert isinstance(ScriptedUser({}), UserInteraction)
    assert not isinstance(browser, ATSAdapter)


def test_missing_input_then_resume_then_confirmed_submission(
    store, tmp_path, fictional_candidate, mock_packet, mock_form, accepted_observation
):
    browser = FakeBrowser(mock_form, accepted_observation)
    runner = MiniRunner(store, FakeCandidates(fictional_candidate), FakeResolver(mock_packet),
                        FakeFactory(browser), None, tmp_path / "artifacts")
    assert isinstance(runner, ApplicationRunner)

    first = asyncio.run(runner.apply(mock_form.url.replace(":0", ":8000"),
                                     candidate_id=fictional_candidate.id))
    assert first.state is S.NEEDS_INPUT and not first.submitted
    assert [m.field_id for m in first.missing_inputs] == ["gender", "why_us"]
    assert browser.submits == 0

    gender, why_us = first.missing_inputs
    claim = store.claim(first.application_id, "cli-prompt")
    store.save_user_inputs(claim, [
        UserInput.answering(gender, ChoiceValue(value="decline",
                                                label="I decline to self-identify")),
        UserInput.answering(why_us, TextValue(text="Because.")),
    ])
    store.release(claim)

    second = asyncio.run(runner.resume(first.application_id))
    assert second.submitted and browser.submits == 1
    assert second.receipt is not None
    assert second.receipt.confirmation_reference == "MOCK-APP-000123"
    events = [e.event for e in store.list_events(first.application_id)]
    assert events.count("application.submitted") == 1
    assert events.index("application.needs_input") < events.index("input.received")
