"""Round 14, item 4: Jobvite's data-processing consent page is accepted when the person's
own statement covers it, and is left to the person otherwise, as before. (The "double-check"
attestation is an ARIA checkbox, a question since item 3; its statement is decided the same
way, below.)

The page in front of the form asks the person to choose a policy (a location of residence
and language) and click "I Accept". The browser reads it as one consent question ("I accept
the <policy>", ``data_consent``) without touching the page; the runner resolves it like any
consent (an exact saved answer, else the routing resolver's statement-coverage decision),
and only a checked answer from the person's own answers lets the browser choose the policy
and click "I Accept" (``accept_data_consent``). That sends the consent, never an
application.

Real headless Chromium against the local mock ATS, fictional data, temp IMX_HOME; nothing
is submitted.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory, consent_gate
from interviewmaxxing_browser.ai import AIFormRouter, BoundedDecisions, CallBudget
from interviewmaxxing_browser.ai.routing import DynamicPacketResolver
from interviewmaxxing_cli.runner import (
    CONSENT_EVENT,
    LocalApplicationRunner,
    NoninteractiveInteraction,
)
from interviewmaxxing_core import (
    AnswerScope,
    AnswerSource,
    Application,
    ApplicationForm,
    ApplicationState,
    ApplicationStore,
    BooleanValue,
    BrowserOptions,
    CandidateProfile,
    ControlType,
    JobRecord,
    LocalPaths,
    MissingReason,
    PacketContext,
    PageKind,
    SavedAnswer,
    SemanticType,
)
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

REPO = Path(__file__).resolve().parents[2]
RESUME_PATH = REPO / "tests" / "fixtures" / "browser" / "resume_avery_quill.pdf"
VERIFIED_AT = "2026-09-01T12:00:00Z"
POSTING = "/jobs/jobvite-like"
REGIONAL = "/jobs/jobvite-like/apply?policies=regional"
POLICY = "Global BRAMBLEWAY ANALYTICS APPLICANT AND CANDIDATE PRIVACY POLICY"
QUESTION = f"I accept the {POLICY}"
PAGE_STATE = """() => ({
  chosen: document.getElementById("jv-country-select")?.value ?? null,
  accept: !!Array.from(document.querySelectorAll("button")).find((b) => /accept/i.test(b.textContent)),
  url: location.pathname})"""


async def _question(options: BrowserOptions, url: str, residence: str | None) -> tuple[Any, Any, Any]:
    browser = await PlaywrightSessionFactory().start(options)
    try:
        gate = await browser.open(url)
        question = await browser.data_consent(residence)
        return gate, question, await browser.page.evaluate(PAGE_STATE)
    finally:
        await browser.close()


# --- the browser: the question, read without touching the page, and its acceptance ---------

def test_the_consent_page_is_read_as_one_consent_question_without_touching_it(
    server: Any, options: BrowserOptions
) -> None:
    gate, question, state = asyncio.run(_question(options, server.url(POSTING), "United States"))
    assert consent_gate(gate) and gate.form is None
    assert isinstance(question, ApplicationForm) and question.url.endswith("/jobs/jobvite-like/apply")
    (field,) = question.fields
    assert (field.id, field.label, field.control_type, field.semantic_type, field.required) == (
        "data-consent", QUESTION, ControlType.CHECKBOX, SemanticType.CONSENT, True)
    assert field.section_context == ["Data Consent"] and field.question_text == QUESTION
    # Nothing was chosen or clicked, and nothing reached the site.
    assert state == {"chosen": "", "accept": False, "url": "/jobs/jobvite-like/apply"}
    assert server.submissions("jobvite-like")["consents"] == []


@pytest.mark.parametrize(("residence", "label"), [
    ("United States", "I accept the United States - English"),
    ("Canada", None),  # two policies name Canada (English and French): the person's to choose
    ("Germany", None),
    (None, None),
])
def test_of_several_policies_the_one_naming_the_residence_is_the_question(
    residence: str | None, label: str | None, server: Any, options: BrowserOptions
) -> None:
    _, question, state = asyncio.run(_question(options, server.url(REGIONAL), residence))
    assert (question.fields[0].label if question is not None else None) == label
    assert state["chosen"] == "" and server.submissions("jobvite-like")["consents"] == []


@pytest.mark.parametrize("url", [POSTING, "/jobs/jobvite-like/apply?accept=link"],
                         ids=["accept-button", "accept-link"])
def test_accepting_the_consent_sends_the_policy_and_leads_to_the_form(
    url: str, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            gate = await browser.open(server.url(url))
            question = await browser.data_consent("United States")
            return gate, await browser.accept_data_consent(question, "United States")
        finally:
            await browser.close()

    gate, after = asyncio.run(scenario())
    assert after is not None and after.kind is PageKind.APPLICATION_FORM and after.form is not None, after
    if url == POSTING:
        # The form continues the posting that was opened, so it carries its identity.
        assert gate.job_identity is not None and after.job_identity == gate.job_identity
    summary = server.submissions("jobvite-like")
    assert [json.loads(c["policy"]) for c in summary["consents"]] == [{"consentPolicyId": "policy-7d1f"}]
    assert summary["accepted_count"] == 0


def test_a_question_the_page_no_longer_asks_is_not_accepted(server: Any, options: BrowserOptions) -> None:
    async def scenario() -> tuple[Any, Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.open(server.url(REGIONAL))
            question = await browser.data_consent("United States")
            assert question is not None
            # Asked for another residence, the page's question is another policy.
            stale = await browser.accept_data_consent(question, "Canada")
            reworded = question.model_copy(update={"fields": [
                question.fields[0].model_copy(update={"label": "I accept the Canada - English"})]})
            other = await browser.accept_data_consent(reworded, "United States")
            return stale, other, await browser.page.evaluate(PAGE_STATE)
        finally:
            await browser.close()

    stale, other, state = asyncio.run(scenario())
    assert stale is None and other is None
    assert state["chosen"] == "" and state["accept"] is False
    assert server.submissions("jobvite-like")["consents"] == []


# --- the statement-coverage decision over the person's saved statements ---------------------

PRIVACY = SavedAnswer(id="sa.privacy_notice", scope=AnswerScope.GLOBAL, semantic_type=SemanticType.CONSENT,
                      question="I have read and understand the employer's applicant privacy notice "
                               "and data processing terms.", value="Yes", confirmed_at=VERIFIED_AT)
CERTIFY = SavedAnswer(id="sa.certify", scope=AnswerScope.GLOBAL, semantic_type=SemanticType.ATTESTATION,
                      question="The information I provide in this application is true, complete "
                               "and accurate.", value="Yes", confirmed_at=VERIFIED_AT)


class StatementJev:
    """Routes every field as the applicant's own datum and answers the statement-coverage
    question with ``pick``; records every request."""

    def __init__(self, pick: str, probability: float = 0.98) -> None:
        self.pick, self.probability = pick, probability
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] != "choice":
                answers[name] = {"type": "noul", "noul": 1.0}
                continue
            criteria = list(question["criteria"])
            if name[0] in "rnusd" and name[1:].isdigit():
                choice = {"r": "COPY_KNOWN", "n": "literal", "u": "APPLICANT_CURRENT",
                          "s": "CONSENT", "d": "APPLICATION_ATTACHMENT"}[name[0]]
                probabilities = {key: float(key == choice) for key in criteria}
            else:
                choice = self.pick
                rest = (1 - self.probability) / (len(criteria) - 1)
                probabilities = {key: self.probability if key == choice else rest for key in criteria}
            answers[name] = {"type": "choice", "choice": choice, "confidence": self.probability,
                             "probabilities": probabilities}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
                                                 "answers": answers, "usage": {"cost": 0.0001}}).encode())

    def statement_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if "statement" in r["questions"]]


def _resolve(question: ApplicationForm, candidate: CandidateProfile, job: JobRecord,
             jev: StatementJev) -> Any:
    """The runner's resolution of the consent question: the routing resolver over a form
    the router has not seen (it classifies it first)."""
    decisions = BoundedDecisions(JevClient(ApiKey("synthetic-key", source="test"), transport=jev,
                                           max_attempts=1), CallBudget())
    resolver = DynamicPacketResolver(decisions, router=AIFormRouter(decisions))
    app = Application(id="app-consent-test", request_id="request-consent-test", job_id=job.id,
                      candidate_id=candidate.id, state=ApplicationState.INSPECTING, version=2,
                      created_at="2026-09-25T00:00:00Z", updated_at="2026-09-25T00:00:00Z")
    context = PacketContext(application=app, job=job, form=question, candidate=candidate)
    packet = asyncio.run(resolver.resolve(context))
    assert context.problems(packet) == []
    return packet


@pytest.mark.parametrize(("pick", "covered"), [("s0", True), ("NONE", False)])
def test_the_persons_privacy_statement_is_what_decides_the_consent(
    pick: str, covered: bool, server: Any, options: BrowserOptions,
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    _, question, _ = asyncio.run(_question(options, server.url(POSTING), "United States"))
    candidate = fictional_candidate.model_copy(update={"saved_answers": [PRIVACY, CERTIFY]})
    jev = StatementJev(pick)
    packet = _resolve(question, candidate, mock_job, jev)
    (request,) = jev.statement_requests()
    # Jev reads the page's own words and only the person's consent statements.
    assert request["state"]["site_statement"]["label"] == QUESTION
    assert request["state"]["site_statement"]["section_context"] == ["Data Consent"]
    assert list(request["state"]["saved_statements"].values()) == [PRIVACY.question]
    if covered:
        (answer,) = packet.answers
        assert answer.value == BooleanValue(checked=True)
        assert (answer.provenance.source, answer.provenance.reference_ids) == (
            AnswerSource.SAVED_ANSWER, [PRIVACY.id])
    else:
        assert packet.answers == []
        (missing,) = packet.missing_inputs
        assert missing.field_id == "data-consent" and missing.reason in (
            MissingReason.UNCOVERED_ATTESTATION, MissingReason.EXPLICIT_ANSWER_REQUIRED)


@pytest.mark.parametrize(("pick", "covered"), [("s0", True), ("NONE", False)])
def test_the_double_check_attestation_is_decided_by_the_persons_certification(
    pick: str, covered: bool, server: Any, options: BrowserOptions,
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    async def inspect() -> ApplicationForm:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url("/jobs/greenhouse-aria/apply"))
            assert page.form is not None
            return page.form
        finally:
            await browser.close()

    form = asyncio.run(inspect())
    double_check = form.field("question_7203")
    assert (double_check.control_type, double_check.semantic_type) == (ControlType.CHECKBOX,
                                                                       SemanticType.ATTESTATION)
    candidate = fictional_candidate.model_copy(update={"saved_answers": [PRIVACY, CERTIFY]})
    jev = StatementJev(pick)
    packet = _resolve(ApplicationForm(url=form.url, fields=[double_check]), candidate, mock_job, jev)
    (request,) = jev.statement_requests()
    assert request["state"]["site_statement"]["label"] == double_check.label
    assert list(request["state"]["saved_statements"].values()) == [CERTIFY.question]
    if covered:
        (answer,) = packet.answers
        assert answer.value == BooleanValue(checked=True)
        assert (answer.provenance.source, answer.provenance.reference_ids) == (
            AnswerSource.SAVED_ANSWER, [CERTIFY.id])
    else:
        assert packet.answers == [] and [m.field_id for m in packet.missing_inputs] == ["question_7203"]


# --- the runner: accepted when covered, the person's page otherwise --------------------------

FORM_STATE = """() => ({
  chosen: document.getElementById("jv-country-select")?.value ?? null,
  referred: document.querySelector('select[name="referred"]')?.value ?? null,
  sponsorship: document.querySelector('select[name="sponsorship"]')?.value ?? null,
  url: location.pathname})"""


def _write_profile(paths: LocalPaths, *, consent: str | None) -> None:
    """The fictional candidate with saved answers worded as the Jobvite form asks them and,
    when given, their answer to the consent page's own question."""
    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RESUME_PATH, directory / "resume.pdf")
    saved = [
        {"id": "sa.referred", "scope": "GLOBAL", "semantic_type": None,
         "question": "Were you referred to this role by a current Brambleway employee?",
         "value": "No, I was not referred", "confirmed_at": VERIFIED_AT},
        {"id": "sa.sponsorship", "scope": "GLOBAL", "semantic_type": "SPONSORSHIP",
         "question": "Do you now or in the future will you require sponsorship for work in the "
                     "United States?",
         "value": "No, I will NOT ever require any work sponsorship", "confirmed_at": VERIFIED_AT},
    ]
    if consent is not None:
        saved.append({"id": "sa.data_consent", "scope": "GLOBAL", "semantic_type": "CONSENT",
                      "question": QUESTION, "value": consent, "confirmed_at": VERIFIED_AT})
    profile = {
        "id": "default",
        "identity": {
            "first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
            "phone": "+1 (303) 555-0142",
            "address": {"street": "1234 Fictional Avenue", "city": "Denver", "region": "CO",
                        "postal_code": "80202", "country": "United States"},
            "verified_at": VERIFIED_AT,
        },
        "resume": {"id": "resume_supplied", "path": "resume.pdf"},
        "facts": [],
        "saved_answers": saved,
    }
    (directory / "profile.json").write_text(json.dumps(profile, indent=2))


class RecordingFactory:
    """The Playwright factory, recording the page as each browser closes."""

    def __init__(self) -> None:
        self.states: list[Any] = []

    async def start(self, options: BrowserOptions) -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        close = browser.close

        async def close_and_record() -> None:
            self.states.append(await browser.page.evaluate(FORM_STATE))
            await close()

        browser.close = close_and_record
        return browser


def _run(kit: SimpleNamespace, paths: LocalPaths, url: str) -> tuple[Any, list[Any], Any]:
    factory = RecordingFactory()
    runner = LocalApplicationRunner(paths=paths, interaction=NoninteractiveInteraction(),
                                    headless=True, browser_factory=factory, prepare_only=True)
    result = kit.run(runner.apply(url, candidate_id="default"))
    with ApplicationStore.open(paths.state_db) as store:
        events = store.list_events(result.application_id)
        assert store.list_attempts(result.application_id) == []
    return result, events, factory.states[-1]


def test_the_runner_accepts_a_consent_the_persons_statement_covers(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home, consent="Yes")
    result, events, state = _run(kit, isolated_imx_home, server.url(POSTING))
    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    assert "Prepared to the final review step" in result.message and result.missing_inputs == []
    (accepted,) = [e.metadata for e in events if e.event == CONSENT_EVENT]
    assert accepted["question"] == QUESTION and accepted["reference_ids"] == ["sa.data_consent"]
    assert (accepted["source"], accepted["next_page"]) == ("SAVED_ANSWER", "APPLICATION_FORM")
    assert [e.metadata["submitted"] for e in events if e.event == "preparation.ready"] == [False]
    assert state == {"chosen": None, "referred": "not_referred", "sponsorship": "sp_never",
                     "url": "/jobs/jobvite-like/apply"}
    summary = server.submissions("jobvite-like")
    assert len(summary["consents"]) == 1 and summary["accepted_count"] == 0


@pytest.mark.parametrize("consent", [None, "No"])
def test_a_consent_no_statement_covers_stays_the_persons_to_accept(
    consent: str | None, kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home, consent=consent)
    result, events, state = _run(kit, isolated_imx_home, server.url(POSTING))
    assert result.state is ApplicationState.NEEDS_INPUT
    (need,) = result.missing_inputs
    assert (need.label, need.reason, need.field_id) == (
        "Accept the data-processing consent", MissingReason.USER_ACTION, None)
    assert not any(e.event == CONSENT_EVENT for e in events)
    # The run ended on the consent page, with nothing chosen on it.
    assert state == {"chosen": "", "referred": None, "sponsorship": None, "url": "/jobs/jobvite-like/apply"}
    summary = server.submissions("jobvite-like")
    assert summary["consents"] == [] and summary["accepted_count"] == 0
