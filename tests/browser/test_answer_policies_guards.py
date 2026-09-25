"""Round 12 guards: the standing answer policies' contract, bullet by bullet.

``test_answer_policies.py`` covers the live answer-sheet wordings; this file covers the
guards around them: which fields are candidates for the policy pass, which classes a field
may take, what the one policy request carries, every outcome status of
``DynamicPacketResolver._answer_policy`` with both sides of each gate, the priority of
facts, saved answers and user input, and the negative cases the round-12 brief names.

Every field is typed the way a live form types it: first by the browser heuristics
(``semantics.classify``), then by Jev's full-form annotation (``router.annotate``). A case
that sets a type explicitly says why. The candidate (Avery Example), the employer (Mock Co)
and every wording are fictional; these are packet resolutions only, nothing is submitted.
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from interviewmaxxing_browser.ai import (
    AIFormRouter,
    BoundedDecisions,
    CallBudget,
    DynamicPacketResolver,
)
from interviewmaxxing_browser.ai.answer_policies import POLICY_PROMPT_VERSION
from interviewmaxxing_browser.ai.providers import NarrativeDraft
from interviewmaxxing_browser.semantics import classify as classify_semantics
from interviewmaxxing_candidate.simple_answers import SimpleAnswers
from interviewmaxxing_core import (
    AnswerScope,
    AnswerSource,
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationState,
    BooleanValue,
    CandidateFact,
    CandidateProfile,
    ChoiceValue,
    ControlType,
    FactVerification,
    FieldOption,
    JobRecord,
    MissingInput,
    MissingReason,
    PacketContext,
    SavedAnswer,
    SemanticType,
    TextValue,
    UserInput,
    VerificationMethod,
    VerificationStatus,
)
from interviewmaxxing_core.answer_policies import (
    ANSWER_POLICY_KEYS,
    ANSWER_POLICY_QUESTIONS,
    POLICY_CONTRADICTIONS,
    answer_policy_key,
    stated_answer_policies,
)
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
EARLIER = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
YES_NO = ("Yes", "No")
CLAIMS = "claims_experience_asked"
THRESHOLDS = "meets_experience_thresholds"
CERTIFIES = "certifies_truth"
NOT_EMPLOYEE = "not_current_or_former_employee"
SANCTIONS = "sanctioned_locations"
ALL_CLASSES = frozenset(ANSWER_POLICY_KEYS)
ATTESTATION_CLASSES = frozenset({CERTIFIES, NOT_EMPLOYEE, SANCTIONS})
"""What an attestation may take: the truth of the application, or the applicant's status
("I have never worked for ...", "I confirm I am not located in ... Cuba ...")."""
PERSON_POLICIES = {CLAIMS: "Yes", THRESHOLDS: "Yes", CERTIFIES: "Yes", NOT_EMPLOYEE: "No",
                   SANCTIONS: "No"}
"""The person's standing rules as the lead recorded them on 2026-09-25."""
MOCK_JOB_KEY = "ats:mock:mock-co:4012"
"""The identity key of the conftest ``mock_job`` (a job-scoped answer must name it)."""

_NO_ANSWER = ("NONE", "UNKNOWN", "hold", "NOT_EXPERIENCE")
"""The choice an unscripted decision takes: no saved answer and no fact settles anything."""


# --- the scripted Jev of test_answer_policies.py (extended: see the marked fields) --------------


@dataclass
class Script:
    """How Jev reads one question (matched by a fragment of its wording)."""

    route: str = "COPY_KNOWN"
    scope: str = "HISTORICAL_OR_CONTEXTUAL"
    narrative: str = "literal"
    semantic: str | dict[str, float] | None = None
    """The semantic reading (``s<i>``); None reads a custom field as its own control."""
    policy: str | dict[str, float] = "NONE"
    """The class Jev picks (at ``policy_probability``), or its whole distribution."""
    policy_confidence: float = 0.97
    polarity: str | dict[str, float] = "SAME"
    yes_option: str | None = None
    """A fragment of the option label Jev names as the mildest yes (None: NONE)."""
    no_option: str | None = None
    details_if_yes: float = 0.0
    details_if_no: float = 0.0
    decisions: dict[str, tuple[str, float]] = field(default_factory=dict)
    """Other decisions by question name (``experience``, ``wording`` ...): choice and probability."""
    nouls: dict[str, float] = field(default_factory=dict)
    """Other nouls by name prefix (``q`` for grounding, ``complete``)."""
    # Extensions of this file; the defaults keep the original harness's answers.
    policy_probability: float = 0.99
    polarity_confidence: float = 0.97
    polarity_probability: float = 0.99
    option_confidence: float = 0.97
    option_probability: float = 0.99
    fail_policy: bool = False
    """The policy request fails at the provider (HTTP 503)."""
    supports: dict[str, float] = field(default_factory=dict)
    """By fact id: the noul of a fact-bound question (``has_``, ``states_``, relevance)."""
    negates: dict[str, float] = field(default_factory=dict)
    """By fact id: the ``lacks_`` noul."""


def distribution(choice: str | dict[str, float], criteria: list[str],
                 probability: float = 0.99) -> dict[str, float]:
    if isinstance(choice, dict):
        values = {key: choice.get(key, 0.0) for key in criteria}
    else:
        assert choice in criteria, (choice, criteria)
        rest = (1 - probability) / (len(criteria) - 1)
        values = {key: probability if key == choice else rest for key in criteria}
    total = sum(values.values())
    return {key: value / total for key, value in values.items()}


class PolicyJev:
    """Classifies each field from its script, answers the policy decision from the script
    whose fragment its wording contains, and answers every other decision with its
    no-answer choice (NONE, UNKNOWN, hold) unless the script names it. Thread-safe: the
    resolver decides fields concurrently.

    Extended here: ``fail_routes`` fails the full-form classification, ``consistency`` is
    the noul of every evidence-consistency question (0.0, the original's answer, holds)."""

    def __init__(self, scripts: dict[str, Script] | None = None, *, fail_routes: bool = False,
                 consistency: float = 0.0) -> None:
        self.scripts = scripts or {}
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.fail_routes = fail_routes
        self.consistency = consistency

    def script(self, text: str) -> Script:
        found = [script for fragment, script in self.scripts.items() if fragment in text]
        assert len(found) <= 1, f"two scripts match {text!r}"
        return found[0] if found else Script()

    def asked(self, name: str) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests if name in r["questions"]]

    def policy_requests(self) -> list[dict[str, Any]]:
        return self.asked("policy")

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        with self.lock:
            self.requests.append(request)
        state = request["state"]
        questions = request["questions"]
        if ((self.fail_routes and "r0" in questions)
                or ("policy" in questions and self.script(self._wording(state)).fail_policy)):
            return HttpResponse(503, {}, json.dumps({"error": {"message": "unavailable"}}).encode())
        answers: dict[str, Any] = {}
        for name, question in questions.items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": self._noul(name, state)}
                continue
            criteria = list(question["criteria"])
            pick, probability, confidence = self._choice(name, state, criteria)
            values = distribution(pick, criteria, probability)
            answers[name] = {"type": "choice", "choice": max(values, key=lambda key: values[key]),
                             "confidence": confidence, "probabilities": values}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())

    def _wording(self, state: dict[str, Any]) -> str:
        if "question" in state and isinstance(state["question"], dict):  # the policy decision
            q = state["question"]
            return " ".join(part for part in (q["label"], q.get("help_text") or "") if part)
        if isinstance(state.get("question"), str):
            return str(state["question"])
        if "field" in state:
            return str(state["field"]["question"])
        if "observed_question" in state:
            return str(state["observed_question"]["label"])
        if "site_statement" in state:
            return str(state["site_statement"]["label"])
        return ""

    def _noul(self, name: str, state: dict[str, Any]) -> float:
        if "selected_facts" in state:  # the evidence-consistency check names no question
            return self.consistency
        script = self.script(self._wording(state))
        if name == "details_if_yes":
            return script.details_if_yes
        if name == "details_if_no":
            return script.details_if_no
        facts = state.get("facts")
        fact = facts.get(name.rsplit("_", 1)[-1]) if isinstance(facts, dict) else None
        if isinstance(fact, dict):
            scores = script.negates if name.startswith("lacks_") else script.supports
            if fact["id"] in scores:
                return scores[fact["id"]]
        return next((value for prefix, value in script.nouls.items() if name.startswith(prefix)), 0.0)

    def _choice(self, name: str, state: dict[str, Any],
                criteria: list[str]) -> tuple[str | dict[str, float], float, float]:
        if name[0] in "rnusd" and name[1:].isdigit():
            data = state["fields"][f"f{name[1:]}"]
            script = self.script(data["question"])
            if name[0] == "r":
                return script.route, 1.0, 1.0
            if name[0] == "n":
                return script.narrative, 1.0, 1.0
            if name[0] == "u":
                return script.scope, 1.0, 1.0
            if name[0] == "d":
                return "APPLICATION_ATTACHMENT", 1.0, 1.0
            semantic = script.semantic or {"TEXTAREA": "CUSTOM_LONG_TEXT", "TEXT": "CUSTOM_TEXT"}.get(
                data["control"], "CUSTOM_BOOLEAN")
            return semantic, 1.0, 1.0
        script = self.script(self._wording(state))
        if name == "policy":
            return script.policy, script.policy_probability, script.policy_confidence
        if name == "polarity":
            return script.polarity, script.polarity_probability, script.polarity_confidence
        if name in ("yes_option", "no_option"):
            fragment = script.yes_option if name == "yes_option" else script.no_option
            options: dict[str, str] = state["options"]
            key = next((k for k, label in options.items() if fragment and fragment in label), "NONE")
            return key, script.option_probability, script.option_confidence
        if name in script.decisions:
            pick, probability = script.decisions[name]
            return pick, probability, 0.97
        held = next(choice for choice in (*_NO_ANSWER, criteria[0]) if choice in criteria)
        return held, 1.0, 1.0


def decisions(provider: PolicyJev, budget: CallBudget | None = None) -> BoundedDecisions:
    return BoundedDecisions(JevClient(ApiKey("synthetic-key", source="test"),
        transport=provider, max_attempts=1), budget or CallBudget())


class Writer:
    """A narrative writer whose one sentence cites one verified fact (test_ai_routing)."""

    def __init__(self, fact_id: str, text: str) -> None:
        self.fact_id, self.text = fact_id, text
        self.calls: list[dict[str, Any]] = []

    def write(self, **kwargs: Any) -> NarrativeDraft:
        self.calls.append(kwargs)
        return NarrativeDraft.model_validate({"status": "READY", "missing_information": [],
            "sentences": [{"text": self.text, "fact_ids": [self.fact_id]}]})


# --- fictional profile and fields ----------------------------------------------------------------


def years_fact(key: str, value: float, *, source: str = "user:confirmed fact import",
               fid: str | None = None) -> CandidateFact:
    return CandidateFact(id=fid or f"user_{key}", key=key, value=value, source=source,
        verification=FactVerification(status=VerificationStatus.VERIFIED,
            method=VerificationMethod.USER_STATED, verified_at=NOW),
        evidence=["Stated by the applicant (fictional)"])


def derived_years(key: str, value: float, fid: str) -> CandidateFact:
    """A years fact derived from dated resume roles: not stated by the person."""
    return years_fact(key, value, source="resume:dated roles", fid=fid)


def text_fact(fid: str, key: str, value: str) -> CandidateFact:
    return CandidateFact(id=fid, key=key, value=value, source="user:confirmed fact import",
        verification=FactVerification(status=VerificationStatus.VERIFIED,
            method=VerificationMethod.USER_STATED, verified_at=NOW),
        evidence=["Stated by the applicant (fictional)"])


STATED_YEARS = {"years_experience": 8, "years_experience.seo": 6, "years_experience.meta_ads": 7,
                "years_experience.google_ads": 7, "years_experience.team_leadership": 5}
"""Fictional stated years; the fixture already states years_experience.paid_media 7."""


def with_policies(candidate: CandidateProfile, policies: dict[str, str | None] | None = None,
                  *, years: dict[str, float] | None = None) -> CandidateProfile:
    """The candidate after importing ``policies`` through the simple-answers map (the
    person's rules by default) and their stated years."""
    data = SimpleAnswers.from_identity(candidate.identity).model_dump()
    data["answer_policies"] = dict.fromkeys(ANSWER_POLICY_KEYS) | (
        PERSON_POLICIES if policies is None else policies)
    imported = SimpleAnswers.model_validate(data).saved_answer_updates(confirmed_at=NOW)
    facts = [years_fact(key, value) for key, value in (STATED_YEARS if years is None else years).items()]
    return candidate.model_copy(update={"saved_answers": [*candidate.saved_answers, *imported],
                                        "facts": [*candidate.facts, *facts]})


def policy_id(candidate: CandidateProfile, key: str) -> str:
    return next(a.id for a in candidate.saved_answers if a.question == ANSWER_POLICY_QUESTIONS[key])


MOCK_JOB = (MOCK_JOB_KEY, "Mock Co")
OTHER_JOB = ("ats:mock:other-co:9001", "Other Co")
"""A job-scoped answer's job: identity key and employer (Other Co is the fixture's other job)."""


def saved(answer_id: str, question: str, value: str, *, confirmed_at: datetime = NOW,
          job: tuple[str, str] | None = None) -> SavedAnswer:
    """An untyped saved answer of the person's: GLOBAL, or scoped to ``job``."""
    scope = ({"scope": AnswerScope.GLOBAL} if job is None else
             {"scope": AnswerScope.JOB, "job_identity_key": job[0], "employer": job[1]})
    return SavedAnswer(id=answer_id, question=question, value=value, confirmed_at=confirmed_at,
                       **scope)


def typed_field(label: str, control: ControlType, options: tuple[str, ...] = (), *,
                field_id: str = "q", help_text: str | None = None, required: bool = True,
                semantic: SemanticType | None = None, **extra: Any) -> ApplicationField:
    """A field as a live form types it: the browser heuristics' reading of its label, unless
    ``semantic`` names the type a site's own inspector gave it (each caller says why)."""
    heuristic = classify_semantics(label=label, help_text=help_text or "", control_type=control)
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label, help_text=help_text,
        semantic_type=semantic or heuristic, control_type=control, required=required,
        options=[FieldOption(value=f"v{i}", label=o) for i, o in enumerate(options)] or None,
        **extra)


def context(form: ApplicationForm, candidate: CandidateProfile, job: JobRecord,
            user_inputs: list[UserInput] | None = None) -> PacketContext:
    app = Application(id="app-policies", request_id="request-policies", job_id=job.id,
        candidate_id=candidate.id, state=ApplicationState.INSPECTING, version=2,
        created_at="2026-09-25T00:00:00Z", updated_at="2026-09-25T00:00:00Z")
    return PacketContext(application=app, job=job, form=form, candidate=candidate,
                         user_inputs=user_inputs or [])


def resolve(provider: PolicyJev, candidate: CandidateProfile, job: JobRecord,
            *fields: ApplicationField, budget: CallBudget | None = None, writer: Writer | None = None,
            user_inputs: Callable[[ApplicationForm], list[UserInput]] | None = None,
            ) -> tuple[Any, PacketContext, DynamicPacketResolver]:
    router = AIFormRouter(decisions(provider, budget))
    annotated = router.annotate(ApplicationForm(url="https://example.test/apply", fields=list(fields)),
                                document_id="answer-policies")
    ctx = context(annotated, candidate, job, user_inputs(annotated) if user_inputs else None)
    resolver = DynamicPacketResolver(router.decisions, writer, router=router)  # type: ignore[arg-type]
    packet = asyncio.run(resolver.resolve(ctx))
    assert ctx.problems(packet) == []
    return packet, ctx, resolver


def traces(resolver: DynamicPacketResolver, stage: str = "answer_policy") -> list[dict[str, Any]]:
    return [t for t in resolver.narrative_traces if t["stage"] == stage]


def rendered(answer: Any) -> str | bool:
    value = answer.value
    if isinstance(value, ChoiceValue):
        return value.label
    if isinstance(value, TextValue):
        return value.text
    assert isinstance(value, BooleanValue)
    return value.checked


# --- one field, the profile around it, and what the resolver did --------------------------------


def explicit(**kwargs: Any) -> Script:
    """How the classifier reads an eligibility-style question: an explicit answer, never a
    fact screener, so the policy pass is the only path that can answer it."""
    return Script(route="HUMAN_INPUT", scope="EXPLICIT_ANSWER", **kwargs)


@dataclass(frozen=True)
class Case:
    """One required field and everything around it: how Jev reads it, the person's
    policies (None: their recorded rules), stated years (None: ``STATED_YEARS``), extra
    facts and saved answers, an address change and, when a site types the field itself,
    its type."""

    label: str
    control: ControlType = ControlType.RADIO
    options: tuple[str, ...] = YES_NO
    script: Script = field(default_factory=Script)
    policies: dict[str, str | None] | None = None
    years: dict[str, float] | None = None
    facts: tuple[CandidateFact, ...] = ()
    saved: tuple[SavedAnswer, ...] = ()
    address: dict[str, str] | None = None
    semantic: SemanticType | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def profile(case: Case, base: CandidateProfile, *, rules: bool = True) -> CandidateProfile:
    """The fictional candidate of ``case``; without ``rules`` the same profile with no
    standing policy at all (the baseline a kept hold is compared with)."""
    candidate = with_policies(base, case.policies if rules else {}, years=case.years)
    identity = candidate.identity
    if case.address:
        identity = identity.model_copy(update={"address": identity.address.model_copy(update=case.address)})
    return candidate.model_copy(update={"identity": identity,
        "facts": [*candidate.facts, *case.facts], "saved_answers": [*candidate.saved_answers, *case.saved]})


@dataclass
class Run:
    packet: Any
    ctx: PacketContext
    resolver: DynamicPacketResolver
    provider: PolicyJev

    def answer(self, field_id: str = "q") -> Any:
        return self.packet.answer_for(field_id)

    def hold(self, field_id: str = "q") -> MissingInput | None:
        return next((m for m in self.packet.missing_inputs if m.field_id == field_id), None)

    def trace(self) -> dict[str, Any]:
        [only] = traces(self.resolver)
        return only

    def requests(self) -> list[dict[str, Any]]:
        return self.provider.policy_requests()

    def classes(self, field_id: str = "q") -> frozenset[str]:
        assert self.resolver.router is not None
        report = self.resolver.router.report_for(self.ctx.form)
        assert report is not None
        return DynamicPacketResolver._policy_classes(self.ctx.form.field(field_id), report.field(field_id))

    def policy_answers(self) -> list[Any]:
        ids = {a.id for a in self.ctx.candidate.saved_answers if answer_policy_key(a) is not None}
        return [a for a in self.packet.answers if set(a.provenance.reference_ids) & ids]


def run_case(case: Case, base: CandidateProfile, job: JobRecord, *, rules: bool = True,
             writer: Writer | None = None) -> Run:
    provider = PolicyJev({case.label[:40]: case.script})
    fld = typed_field(case.label, case.control, case.options, semantic=case.semantic, **case.extra)
    packet, ctx, resolver = resolve(provider, profile(case, base, rules=rules), job, fld,
                                    writer=writer)
    return Run(packet, ctx, resolver, provider)


def assert_kept(result: Run, baseline: Run, field_id: str = "q") -> None:
    """The policy pass left the field exactly as the other passes held it."""
    assert result.answer(field_id) is None
    kept, before = result.hold(field_id), baseline.hold(field_id)
    assert kept is not None and before is not None
    assert (kept.reason, kept.prompt) == (before.reason, before.prompt)


R, S, TA, T, CB, MS = (ControlType.RADIO, ControlType.SELECT, ControlType.TEXTAREA, ControlType.TEXT,
                       ControlType.CHECKBOX, ControlType.MULTISELECT)
CLAIM_Q = "Have you managed Google Ads campaigns?"
EMPLOYEE_Q = "Are you a current or former employee of Mock Co?"
AFFILIATES_Q = "Have you worked for Mock Co or any of its affiliates?"
MOCK_CO_TEXT_Q = "Have you worked for Mock Co before?"
AGENCY_TEXT_Q = "Have you worked in a digital marketing agency?"
SANCTIONS_Q = ("Are you located in or a national of Cuba, Iran, North Korea, Syria, Crimea, or the "
               "so-called Donetsk or Luhansk People's Republics?")
OUTSIDE_US_Q = "Are you currently residing outside the United States?"
COUNTRY_Q = "Country of residence: Cuba, Iran, North Korea or Syria?"
CERTIFY_Q = "I certify that the information I provided is true, accurate and complete."
SMS_CONSENT = "I consent to receive SMS text messages from Mock Co about my application."
SFMC_Q = "Do you have experience with Salesforce Marketing Cloud?"
SFMC_OPTIONS = ("No experience yet", "Yes, some experience", "Yes, extensive experience")
YEARS_TYPED_Q = "Years of paid media experience (5+ years)"
MARKETING_5 = "Do you have 5+ years of experience in marketing?"
EMPLOYED_HERE, INTERVIEWED_HERE = POLICY_CONTRADICTIONS[NOT_EMPLOYEE]
EVIDENCE_ID = "user_years_experience.google_ads"
IN_HOUSE = text_fact("fact.in_house", "agency_experience", "in-house brand marketing on social platforms")
RESUME_GOOGLE_ADS = years_fact("years_experience.google_ads", 5, source="resume:dated roles",
                               fid="resume_google_ads")
NEVER_GOOGLE_ADS = text_fact("fact.never", "google_ads_experience", "never managed Google Ads campaigns")
SCREENER_YES = {"experience": ("YES", 0.99)}
UNKNOWN_READING = {"CUSTOM_BOOLEAN": 0.55, "UNKNOWN": 0.40, "CONSENT": 0.05}
"""Jev's semantic reading below the gate (the field becomes UNKNOWN) with 0.05 on the
explicit-answer types: the most an UNKNOWN field may carry and still take a policy."""
UNKNOWN_TOO_EXPLICIT = {"CUSTOM_BOOLEAN": 0.54, "UNKNOWN": 0.40, "CONSENT": 0.06}
MANY_OPTIONS = ("Yes", "No", *(f"Option {i}" for i in range(3, 251)))
"""250 usable options: the most a single choice may have and still take a policy."""


# --- the candidates of the policy pass -----------------------------------------------------------


NO_REQUEST = [
    # No stated policy at all, or none for the field's classes: no call is made.
    pytest.param(Case(CLAIM_Q, script=Script(policy=CLAIMS), policies={}), False,
                 id="no-stated-policy"),
    pytest.param(Case(CLAIM_Q, script=Script(policy=CLAIMS), policies={},
                      saved=(saved("sa.job_policy", ANSWER_POLICY_QUESTIONS[CLAIMS], "Yes",
                                   job=MOCK_JOB),)), False,
                 id="only-a-job-scoped-policy"),
    # A consent takes certifies_truth only; an attestation that and two more.
    pytest.param(Case(SMS_CONSENT, CB, (), explicit(policy=CERTIFIES),
                      policies={CLAIMS: "Yes", THRESHOLDS: "Yes", NOT_EMPLOYEE: "No", SANCTIONS: "No"}),
                 False, id="consent-without-a-certification-policy"),
    pytest.param(Case(CERTIFY_Q, CB, (), explicit(policy=CERTIFIES),
                      policies={CLAIMS: "Yes", THRESHOLDS: "Yes"}),
                 False, id="attestation-without-any-of-its-three-policies"),
    # Candidates are required, not routed UNSUPPORTED.
    pytest.param(Case(CLAIM_Q, script=Script(policy=CLAIMS), extra={"required": False}), False,
                 id="optional"),
    pytest.param(Case(CLAIM_Q, script=Script(route="UNSUPPORTED", policy=CLAIMS)), False,
                 id="route-unsupported"),
    # _policy_classes: another person's question, and legal wording whatever class Jev reads.
    pytest.param(Case("Is your spouse a current or former employee of Mock Co?",
                      script=Script(scope="OTHER_PERSON_OR_ENTITY", policy=NOT_EMPLOYEE)), True,
                 id="another-persons-question"),
    pytest.param(Case("Are you lawfully able to work in the United States?",
                      script=explicit(semantic="CUSTOM_BOOLEAN", policy=CLAIMS)), True,
                 id="legal-lawfully-able-to-work"),
    pytest.param(Case("Will you need a visa to work with Mock Co?",
                      script=explicit(semantic="CUSTOM_BOOLEAN", policy=CLAIMS)), True,
                 id="legal-visa"),
    pytest.param(Case("Do you hold an active security clearance?",
                      script=explicit(semantic="CUSTOM_BOOLEAN", policy=CLAIMS)), True,
                 id="legal-security-clearance"),
    pytest.param(Case("Do you have a green card?",
                      script=explicit(semantic="CUSTOM_BOOLEAN", policy=CLAIMS)), True,
                 id="legal-green-card"),
    pytest.param(Case("Are you able to start work with Mock Co?", script=explicit(policy=CLAIMS),
                      extra={"help_text": "Candidates must be legally authorized to work in the U.S."}),
                 True, id="legal-wording-in-the-help-text"),
    # Every other type takes none (legal and EEO types: see the negative cases below).
    pytest.param(Case("Are you willing to relocate?", script=explicit(policy=CLAIMS)), True,
                 id="type-relocation"),
    pytest.param(Case("Is your salary expectation above $100,000?", script=explicit(policy=CLAIMS)),
                 True, id="type-salary"),
    pytest.param(Case("Do you use he/him pronouns?", script=explicit(policy=CLAIMS)), True,
                 id="type-pronouns"),
    pytest.param(Case("Did you hear about this job from a current employee?",
                      script=explicit(policy=NOT_EMPLOYEE)), True, id="type-referral-source"),
    pytest.param(Case("Can you start within two weeks?",  # Jev reads the meaning START_DATE
                      script=explicit(semantic="START_DATE", policy=CLAIMS)), True,
                 id="type-start-date"),
    pytest.param(Case("Which of these platforms have you used: Google Ads?",  # Jev: a select-all
                      script=explicit(semantic="CUSTOM_MULTISELECT", policy=CLAIMS)), True,
                 id="type-custom-multiselect"),
    # Controls: multi-select never; a text box only as input type text.
    pytest.param(Case(CLAIM_Q, MS, YES_NO, Script(semantic="CUSTOM_MULTISELECT", policy=CLAIMS)),
                 True, id="control-multiselect"),
    pytest.param(Case(CLAIM_Q, ControlType.CHECKBOX_GROUP, YES_NO,
                      Script(semantic="CUSTOM_MULTISELECT", policy=CLAIMS)), True,
                 id="control-checkbox-group"),
    pytest.param(Case(MOCK_CO_TEXT_Q, T, (), explicit(policy=NOT_EMPLOYEE),
                      extra={"input_type": "number"}), True, id="control-number-input"),
    # Shape: a text box needs a yes/no question and no typed class; a custom choice a yes/no
    # pair or question; a typed years or residence field a yes/no pair; 2..250 options.
    pytest.param(Case("Google Ads campaign management", T, (), Script(policy=CLAIMS)), True,
                 id="shape-text-without-a-yes-no-question"),
    pytest.param(Case("Do you certify that the information you provided is true?", T, (),
                      explicit(policy=CERTIFIES)), True, id="shape-attestation-text-box"),
    pytest.param(Case("Salesforce Marketing Cloud experience", S, SFMC_OPTIONS,
                      Script(policy=CLAIMS)), True, id="shape-graded-without-a-yes-no-question"),
    pytest.param(Case(YEARS_TYPED_Q, R, ("0-2 years", "3-5 years", "5+ years"),
                      Script(policy=THRESHOLDS)), True, id="shape-years-type-with-range-options"),
    pytest.param(Case("Country of residence", S, ("Canada", "Cuba", "Mexico"),
                      explicit(policy=SANCTIONS)), True, id="shape-country-list"),
    pytest.param(Case(CLAIM_Q, R, ("Yes",), Script(policy=CLAIMS)), True, id="shape-one-option"),
    pytest.param(Case(CLAIM_Q, R, (*MANY_OPTIONS, "Option 251"), Script(policy=CLAIMS)), True,
                 id="shape-251-options"),
    # UNKNOWN: every class only while Jev's explicit-answer mass is at most 0.05.
    pytest.param(Case(EMPLOYEE_Q, script=explicit(semantic=UNKNOWN_TOO_EXPLICIT, policy=NOT_EMPLOYEE)),
                 True, id="unknown-with-0.06-explicit-mass"),
]


@pytest.mark.parametrize(("case", "classless"), NO_REQUEST)
def test_a_field_outside_the_policy_pass_gets_no_policy_request(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, case: Case, classless: bool,
) -> None:
    """Candidates and _policy_classes: no request, no trace, no policy answer; where the
    field's classes are the reason, _policy_classes is empty for the live-typed field."""
    result = run_case(case, fictional_candidate, mock_job)
    assert result.requests() == [] and traces(result.resolver) == []
    assert result.policy_answers() == []
    # Either the field takes no class, or it does and candidacy alone keeps it out.
    assert (result.classes() == frozenset()) is classless


def test_an_unknown_field_without_a_semantic_reading_takes_no_policy(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """UNKNOWN takes every class only when Jev's semantic probabilities exist: a form whose
    classification failed holds every field UNKNOWN with no reading, and none takes one."""
    provider = PolicyJev({EMPLOYEE_Q[:40]: explicit(policy=NOT_EMPLOYEE)}, fail_routes=True)
    packet, ctx, resolver = resolve(provider, with_policies(fictional_candidate), mock_job,
                                    typed_field(EMPLOYEE_Q, R, YES_NO))
    assert ctx.form.field("q").semantic_type is SemanticType.UNKNOWN
    assert provider.policy_requests() == [] and packet.answers == []
    assert resolver.router is not None
    report = resolver.router.report_for(ctx.form)
    assert report is not None and report.field("q").semantic_probabilities == {}


@pytest.mark.parametrize(("reason", "candidate"), [
    pytest.param(MissingReason.AMBIGUOUS, False, id="ambiguous"),
    pytest.param(MissingReason.UNSUPPORTED_CONTROL, False, id="unsupported-control"),
    pytest.param(MissingReason.USER_ACTION, False, id="user-action"),
    pytest.param(MissingReason.NO_ANSWER, True, id="no-answer"),
    pytest.param(MissingReason.EXPLICIT_ANSWER_REQUIRED, True, id="explicit-answer-required"),
    pytest.param(None, True, id="no-prior-hold"),
])
def test_the_prior_hold_reason_decides_candidacy(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, reason: MissingReason | None,
    candidate: bool,
) -> None:
    """Candidates: a prior missing reason AMBIGUOUS, UNSUPPORTED_CONTROL or USER_ACTION keeps
    an otherwise open custom yes/no screener out of the policy pass."""
    result = run_case(Case(CLAIM_Q), fictional_candidate, mock_job)
    assert result.resolver.router is not None
    report = result.resolver.router.report_for(result.ctx.form)
    assert report is not None
    fld = result.ctx.form.field("q")
    missing = [] if reason is None else [
        MissingInput.for_field(result.ctx.form, fld, reason=reason, prompt="An earlier hold")]
    policies = stated_answer_policies(result.ctx.candidate.applicable_saved_answers(mock_job))
    assert result.resolver._policy_candidate(result.ctx, report.field("q"), fld, missing,
                                             policies) is candidate


def test_the_persons_own_input_wins(fictional_candidate: CandidateProfile, mock_job: JobRecord) -> None:
    """Priority: user input wins; the field is no candidate and no policy request is made."""
    provider = PolicyJev({CLAIM_Q[:40]: Script(policy=CLAIMS)})
    packet, _, _ = resolve(provider, with_policies(fictional_candidate), mock_job,
        typed_field(CLAIM_Q, R, YES_NO),
        user_inputs=lambda form: [UserInput.for_field(form, "q", ChoiceValue(value="v1", label="No"))])
    [answer] = packet.answers
    assert (answer.provenance.source, rendered(answer)) == (AnswerSource.USER_INPUT, "No")
    assert provider.policy_requests() == []


@pytest.mark.parametrize(("value", "expected"), [("No", "No"), ("Maybe", None)])
def test_the_persons_own_answer_to_this_wording_wins_even_when_it_does_not_fit(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, value: str, expected: str | None,
) -> None:
    """Priority: a saved answer for exactly this wording comes first, even one that does not
    fit the options (it holds for the person instead of taking the claims policy's Yes)."""
    case = Case(CLAIM_Q, script=Script(policy=CLAIMS), saved=(saved("sa.own", CLAIM_Q, value),))
    result = run_case(case, fictional_candidate, mock_job)
    assert result.requests() == []
    if expected is None:
        hold = result.hold()
        assert result.answer() is None and hold is not None
        assert "Your saved answer 'Maybe' cannot be used here" in hold.prompt
    else:
        answer = result.answer()
        assert rendered(answer) == expected
        assert answer.provenance.reference_ids == ["sa.own"]


def test_a_confirmed_reworded_answer_that_does_not_fit_keeps_its_hold(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """Candidates: a reworded saved answer Jev confirmed but that cannot be placed holds the
    field for the person; the policy never answers over it."""
    reworded = saved("sa.search", "Have you ever run paid search campaigns on Google?", "Sometimes")
    case = Case(CLAIM_Q, script=Script(policy=CLAIMS, decisions={"wording": ("q0", 0.99)}),
                saved=(reworded,))
    result = run_case(case, fictional_candidate, mock_job)
    [equivalence] = traces(result.resolver, "question_equivalence")
    assert equivalence["status"] == "VALUE_DOES_NOT_FIT"
    hold = result.hold()
    assert hold is not None and hold.reason is MissingReason.AMBIGUOUS
    assert "answers this question but cannot be used here" in hold.prompt
    assert result.requests() == [] and result.answer() is None


FACT_CONFLICTS = [
    pytest.param(Case(CLAIM_Q, script=Script(policy=CLAIMS, decisions=SCREENER_YES,
                                             supports={EVIDENCE_ID: 0.97}, negates={IN_HOUSE.id: 0.97}),
                      facts=(IN_HOUSE,)),
                 "Your verified facts conflict on this yes/no question", id="screener-conflict"),
    pytest.param(Case(CLAIM_Q, script=Script(policy=CLAIMS, decisions=SCREENER_YES,
                                             supports={EVIDENCE_ID: 0.97}),
                      facts=(RESUME_GOOGLE_ADS,)),
                 "Verified facts disagree", id="same-key-facts-disagree"),
    # The screener's supporting fact contradicts a verified "never" fact that Jev did not read
    # as negating at the gate: the evidence-consistency check holds the field.
    pytest.param(Case(CLAIM_Q, script=Script(policy=CLAIMS, decisions=SCREENER_YES,
                                             supports={EVIDENCE_ID: 0.97},
                                             negates={NEVER_GOOGLE_ADS.id: 0.90}),
                      facts=(NEVER_GOOGLE_ADS,)),
                 "Relevant verified facts conflict with canonical evidence outside retrieval",
                 id="consistency-check-conflict"),
    # A yes/no text area routed to the writer, whose relevant facts disagree.
    pytest.param(Case(CLAIM_Q, TA, (), Script(route="WRITER", narrative="prose", policy=CLAIMS,
                                              supports={EVIDENCE_ID: 0.99, RESUME_GOOGLE_ADS.id: 0.99}),
                      facts=(RESUME_GOOGLE_ADS,)),
                 "Relevant verified facts conflict; the writer cannot choose which is true",
                 id="writer-conflict"),
]


@pytest.mark.parametrize(("case", "prompt"), FACT_CONFLICTS)
def test_verified_facts_that_disagree_keep_their_hold(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, case: Case, prompt: str,
) -> None:
    """Candidates: a field a fact path held because verified facts point both ways (the
    screener's conflict, same-key facts, the consistency check, the writer's relevant facts)
    never takes a policy answer, though the claims policy is stated and would answer Yes."""
    writer = Writer(EVIDENCE_ID, "I have managed Google Ads campaigns.")
    result = run_case(case, fictional_candidate, mock_job, writer=writer)
    hold = result.hold()
    assert result.answer() is None and hold is not None
    assert hold.prompt.startswith(prompt)
    assert result.requests() == [] and traces(result.resolver) == []
    assert result.classes()  # a candidate by its classes: only the conflict keeps it out
    assert writer.calls == []


# --- which classes a field may take, and the one request it gets ---------------------------------


def test_the_policy_request_sends_the_question_as_data_and_every_class(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """Request: one request per candidate, purpose answer_policy, questions policy (every
    class and NONE, whatever the field may take) and polarity, the state's version,
    employer, question and options; the saved policies are never sent."""
    fld = typed_field(CERTIFY_Q, CB, help_text="Required before you submit.",
                      placeholder="Check to confirm", section_context=["Declarations"])
    provider = PolicyJev({CERTIFY_Q[:40]: explicit(policy=CERTIFIES)})
    candidate = with_policies(fictional_candidate)
    packet, _, resolver = resolve(provider, candidate, mock_job, fld)
    [request] = provider.policy_requests()
    assert request["state"] == {
        "prompt_version": POLICY_PROMPT_VERSION, "employer": "Mock Co",
        "question": {"label": CERTIFY_Q, "help_text": "Required before you submit.",
                     "placeholder": "Check to confirm", "section_context": ["Declarations"],
                     "control": "CHECKBOX"},
        "options": {}}
    questions = request["questions"]
    assert set(questions) == {"policy", "polarity"}
    assert set(questions["policy"]["criteria"]) == {*ANSWER_POLICY_KEYS, "NONE"}
    assert set(questions["polarity"]["criteria"]) == {"SAME", "REVERSED"}
    body = json.dumps(request)
    assert not any(question in body for question in ANSWER_POLICY_QUESTIONS.values())
    assert not any(a.id in body for a in candidate.saved_answers if answer_policy_key(a))
    [trace] = traces(resolver)
    assert trace["classes"] == sorted(ATTESTATION_CLASSES)  # the field's three; Jev sees all
    assert [r.purpose for r in resolver.decisions.budget.receipts].count("answer_policy") == 1
    [answer] = packet.answers
    assert rendered(answer) is True


@pytest.mark.parametrize(("options", "asked"), [
    pytest.param(YES_NO, set(), id="exact-yes-and-no"),
    pytest.param(("True", "False"), set(), id="exact-true-and-false"),
    pytest.param(("I agree", "I do not agree"), {"yes_option", "no_option"}, id="neither-exact"),
    pytest.param(("No", "Yes, some experience", "Yes, extensive experience"), {"yes_option"},
                 id="exact-no-only"),
    pytest.param(("Yes", "No, never", "No, not yet"), {"no_option"}, id="exact-yes-only"),
])
def test_option_questions_are_asked_only_without_an_exact_option(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, options: tuple[str, ...],
    asked: set[str],
) -> None:
    """Request: yes_option / no_option (option keys and NONE) only on a select or radio
    with no option labelled exactly Yes/True (resp. No/False); options travel as oN keys."""
    result = run_case(Case(CLAIM_Q, S, options), fictional_candidate, mock_job)
    [request] = result.requests()
    assert set(request["questions"]) == {"policy", "polarity"} | asked
    keys = {f"o{i}": label for i, label in enumerate(options)}
    assert request["state"]["options"] == keys
    for name in asked:
        assert set(request["questions"][name]["criteria"]) == {*keys, "NONE"}


@pytest.mark.parametrize(("control", "options", "asked"), [
    pytest.param(T, (), True, id="text"),
    pytest.param(TA, (), True, id="textarea"),
    pytest.param(R, YES_NO, False, id="radio"),
    pytest.param(CB, (), False, id="checkbox"),
])
def test_details_are_asked_only_for_a_text_box(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, control: ControlType,
    options: tuple[str, ...], asked: bool,
) -> None:
    """Request: details_if_yes / details_if_no nouls only on text controls."""
    result = run_case(Case(MOCK_CO_TEXT_Q, control, options, explicit()), fictional_candidate, mock_job)
    [request] = result.requests()
    details = {name: q["type"] for name, q in request["questions"].items() if name.startswith("details")}
    assert details == ({"details_if_yes": "noul", "details_if_no": "noul"} if asked else {})


# --- outcomes that keep the prior hold -----------------------------------------------------------


HOLD_UNCHANGED = [
    pytest.param(Case(CLAIM_Q, script=Script(policy=CLAIMS, fail_policy=True)), "HELD",
                 id="provider-failure"),
    pytest.param(Case(CLAIM_Q, script=Script(policy="NONE")), "NONE", id="none"),
    # The class gate: confidence >= 0.90 and probability >= 0.95 (passing sides: ANSWERED).
    pytest.param(Case(CLAIM_Q, script=Script(policy=CLAIMS, policy_probability=0.94)), "BELOW_GATE",
                 id="class-probability-0.94"),
    pytest.param(Case(CLAIM_Q, script=Script(policy=CLAIMS, policy_confidence=0.89)), "BELOW_GATE",
                 id="class-confidence-0.89"),
    # A class the field may not take.
    pytest.param(Case(CERTIFY_Q, CB, (), explicit(policy=CLAIMS)), "NOT_ALLOWED",
                 id="attestation-read-as-claims"),
    pytest.param(Case(YEARS_TYPED_Q, script=Script(policy=CLAIMS)), "NOT_ALLOWED",
                 id="years-type-read-as-claims"),
    pytest.param(Case(COUNTRY_Q, script=explicit(policy=NOT_EMPLOYEE)), "NOT_ALLOWED",
                 id="country-type-read-as-employee"),
    pytest.param(Case(SMS_CONSENT, CB, (), explicit(policy=NOT_EMPLOYEE)), "NOT_ALLOWED",
                 id="consent-read-as-employee"),
    # The rule's policy is null.
    pytest.param(Case("Do you have 10+ years of experience with Google Ads?",
                      script=Script(policy=CLAIMS), policies={CLAIMS: "Yes"}), "NO_POLICY",
                 id="claim-naming-years-without-a-threshold-policy"),
    pytest.param(Case(EMPLOYEE_Q, script=explicit(policy=NOT_EMPLOYEE), policies={CLAIMS: "Yes"}),
                 "NO_POLICY", id="employee-question-with-only-claims-stated"),
    pytest.param(Case(CERTIFY_Q, CB, (), explicit(policy=CERTIFIES), policies={NOT_EMPLOYEE: "No"}),
                 "NO_POLICY", id="attestation-offered-by-its-employee-policy"),
    # Certification: a consent Jev misreads (its consent wording holds it before anything
    # else) or a statement that certifies no truth (truth wording counts in the question
    # text only, never in an option such as "True") ...
    pytest.param(Case(SMS_CONSENT, CB, (), explicit(policy=CERTIFIES)), "OTHER_CONSENT",
                 id="sms-consent-misread-as-certification"),
    pytest.param(Case(SMS_CONSENT, S, ("True", "False"), explicit(policy=CERTIFIES)),
                 "OTHER_CONSENT", id="sms-consent-with-true-false-options"),
    pytest.param(Case("I would like to join the Mock Co talent community.", S, ("True", "False"),
                      explicit(policy=CERTIFIES)),
                 "NO_TRUTH_WORDING", id="true-false-options-are-not-truth-wording"),
    pytest.param(Case("I hereby declare that I have read the job description.", CB, (),
                      explicit(policy=CERTIFIES)), "NO_TRUTH_WORDING",
                 id="declaration-without-truth-wording"),
    # ... or a certification that also asks for a consent or permission, whatever its type.
    pytest.param(Case("I certify that my answers are true and I consent to receive SMS text messages "
                      "from Mock Co.", CB, (), explicit(policy=CERTIFIES)), "OTHER_CONSENT",
                 id="certification-and-sms-consent"),
    pytest.param(Case("I certify that the information I provided is accurate and I authorize Mock Co "
                      "to contact me.", CB, (), explicit(policy=CERTIFIES)), "OTHER_CONSENT",
                 id="certification-and-authorization"),
    pytest.param(Case("I certify that my application is true and complete, and I opt in to marketing "
                      "emails from Mock Co.", CB, (), explicit(policy=CERTIFIES)), "OTHER_CONSENT",
                 id="certification-and-marketing-opt-in"),
    pytest.param(Case("I certify that the information I provided is true; please add me to the Mock Co "
                      "newsletter.", CB, (), explicit(policy=CERTIFIES)), "OTHER_CONSENT",
                 id="certification-and-newsletter"),
    pytest.param(Case("I certify that the information I provided is true and I would like to receive "
                      "promotional offers.", CB, (), explicit(policy=CERTIFIES)), "OTHER_CONSENT",
                 id="certification-and-promotional-offers"),
    pytest.param(Case("I certify that the information I provided is true and give permission to "
                      "verify it.", CB, (), explicit(policy=CERTIFIES)), "OTHER_CONSENT",
                 id="certification-and-permission"),
    pytest.param(Case("I certify that the information I provided is true and agree to receive text "
                      "messages about my application.", R, YES_NO, explicit(policy=CERTIFIES),
                      semantic=SemanticType.CUSTOM_BOOLEAN),  # a site's own boolean widget
                 "OTHER_CONSENT", id="custom-certification-and-text-messages"),
    # Employee: the person's newest GLOBAL Yes to having been employed or interviewed here.
    pytest.param(Case(EMPLOYEE_Q, script=explicit(policy=NOT_EMPLOYEE),
                      saved=(saved("sa.employed", EMPLOYED_HERE, "Yes"),)), "CONTRADICTS_SAVED",
                 id="saved-yes-previously-employed"),
    pytest.param(Case(EMPLOYEE_Q, script=explicit(policy=NOT_EMPLOYEE),
                      saved=(saved("sa.interviewed", INTERVIEWED_HERE, "Yes"),)), "CONTRADICTS_SAVED",
                 id="saved-yes-previously-interviewed"),
    pytest.param(Case(EMPLOYEE_Q, script=explicit(policy=NOT_EMPLOYEE),
                      saved=(saved("sa.old", EMPLOYED_HERE, "No", confirmed_at=EARLIER),
                             saved("sa.new", EMPLOYED_HERE, "Yes"))), "CONTRADICTS_SAVED",
                 id="newest-saved-answer-is-yes"),
    # ... or a Yes the person saved for this very job, whatever the GLOBAL answers say.
    pytest.param(Case(EMPLOYEE_Q, script=explicit(policy=NOT_EMPLOYEE),
                      saved=(saved("sa.job_employed", EMPLOYED_HERE, "Yes", job=MOCK_JOB),)),
                 "CONTRADICTS_SAVED", id="job-scoped-yes-previously-employed"),
    pytest.param(Case(EMPLOYEE_Q, script=explicit(policy=NOT_EMPLOYEE),
                      saved=(saved("sa.job_interviewed", INTERVIEWED_HERE, "Yes", job=MOCK_JOB),)),
                 "CONTRADICTS_SAVED", id="job-scoped-yes-previously-interviewed"),
    pytest.param(Case(EMPLOYEE_Q, script=explicit(policy=NOT_EMPLOYEE),
                      saved=(saved("sa.job_employed", EMPLOYED_HERE, "Yes", job=MOCK_JOB,
                                   confirmed_at=EARLIER),
                             saved("sa.global_no", EMPLOYED_HERE, "No"))),
                 "CONTRADICTS_SAVED", id="job-scoped-yes-beside-a-newer-global-no"),
    # Sanctions: no sanctioned place or sanctions wording, or the address is in one (or both).
    pytest.param(Case(OUTSIDE_US_Q, script=explicit(policy=SANCTIONS)), "NO_SANCTIONED_PLACE",
                 id="no-sanctioned-place"),
    pytest.param(Case(SANCTIONS_Q, script=explicit(policy=SANCTIONS), address={"region": "Crimea"}),
                 "CONTRADICTS_ADDRESS", id="address-region-crimea"),
    pytest.param(Case(SANCTIONS_Q, script=explicit(policy=SANCTIONS), address={"city": "Sevastopol"}),
                 "CONTRADICTS_ADDRESS", id="address-city-sevastopol"),
    pytest.param(Case(OUTSIDE_US_Q, script=explicit(policy=SANCTIONS), address={"country": "Syria"}),
                 "NO_SANCTIONED_PLACE", id="no-place-and-address-in-syria"),
]


@pytest.mark.parametrize(("case", "status"), HOLD_UNCHANGED)
def test_an_outcome_without_an_answer_keeps_the_prior_hold(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, case: Case, status: str,
) -> None:
    """Outcomes: provider failure (trace has the reason), NONE, BELOW_GATE, NOT_ALLOWED,
    NO_POLICY, NO_TRUTH_WORDING, OTHER_CONSENT, CONTRADICTS_SAVED, NO_SANCTIONED_PLACE and
    CONTRADICTS_ADDRESS leave the field exactly as held without any policy."""
    result = run_case(case, fictional_candidate, mock_job)
    assert len(result.requests()) == 1
    trace = result.trace()
    assert trace["status"] == status
    if status == "HELD":
        assert trace["reason"] == "Jev UNAVAILABLE"
    if status == "CONTRADICTS_SAVED":
        newest = [a.id for a in case.saved if a.value == "Yes"]
        assert trace["reference_ids"] == newest
    assert_kept(result, run_case(case, fictional_candidate, mock_job, rules=False))


OBLIGATIONS = [
    ("I certify that the information I provided is true and I agree to submit to a drug test.",
     "drug_screening"),
    ("I certify that my answers are true and complete and I agree to binding arbitration of any "
     "employment dispute.", "arbitration"),
    ("I certify that my application is true and accurate and that I will not use AI tools during "
     "the interview.", "ai_tools"),
]


@pytest.mark.parametrize("semantic", [
    pytest.param(None, id="typed-attestation"),
    # A site's own boolean widget reaches the resolver typed CUSTOM_BOOLEAN (every class).
    pytest.param(SemanticType.CUSTOM_BOOLEAN, id="typed-custom-boolean"),
])
@pytest.mark.parametrize(("label", "obligation"), OBLIGATIONS,
                         ids=["drug-test", "arbitration", "no-ai-tools"])
def test_a_certification_that_adds_an_obligation_is_never_answered(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, semantic: SemanticType | None,
    label: str, obligation: str,
) -> None:
    """Negative case: an obligation-adding attestation, typed ATTESTATION or CUSTOM_BOOLEAN,
    with Jev reading certifies_truth at 0.99, holds (ADDED_OBLIGATION) and is never answered."""
    case = Case(label, CB, (), explicit(policy=CERTIFIES), semantic=semantic)
    result = run_case(case, fictional_candidate, mock_job)
    assert result.ctx.form.field("q").semantic_type is (semantic or SemanticType.ATTESTATION)
    trace = result.trace()
    assert (trace["status"], trace["policy"]) == ("ADDED_OBLIGATION", CERTIFIES)
    assert obligation in trace["obligations"]
    assert_kept(result, run_case(case, fictional_candidate, mock_job, rules=False))


def test_an_obligation_named_only_by_an_option_holds_the_certification(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """Certifies: the obligation check reads the question text and the option labels."""
    case = Case("I certify that the information I provided is true.", R,
                ("I certify and consent to a background check", "I do not certify"),
                explicit(policy=CERTIFIES, yes_option="I certify and consent"))
    result = run_case(case, fictional_candidate, mock_job)
    trace = result.trace()
    assert (trace["status"], trace["obligations"]) == ("ADDED_OBLIGATION", ["background"])
    assert_kept(result, run_case(case, fictional_candidate, mock_job, rules=False))


STATEMENT_OBLIGATIONS = [
    pytest.param(Case("I confirm I have never worked for Mock Co and agree to binding arbitration.",
                      CB, (), explicit(policy=NOT_EMPLOYEE, polarity="REVERSED")),
                 NOT_EMPLOYEE, "arbitration", id="attestation-employee-arbitration"),
    pytest.param(Case("I confirm I am not located in Cuba, Iran, North Korea or Syria and agree to drug "
                      "screening.", CB, (), explicit(policy=SANCTIONS, polarity="REVERSED")),
                 SANCTIONS, "drug_screening", id="attestation-sanctions-drug-screening"),
    pytest.param(Case("I confirm I am not located in Cuba, Iran, North Korea or Syria and will not use "
                      "AI tools in interviews.", CB, (), explicit(policy=SANCTIONS, polarity="REVERSED")),
                 SANCTIONS, "ai_tools", id="attestation-sanctions-no-ai-tools"),
    pytest.param(Case("I confirm that I am not a current or former employee of Mock Co.", R,
                      ("I confirm and accept at-will employment", "I do not confirm"),
                      explicit(policy=NOT_EMPLOYEE, polarity="REVERSED", yes_option="I confirm")),
                 NOT_EMPLOYEE, "at_will", id="attestation-employee-at-will-option"),
    pytest.param(Case("I certify that my information is true and consent to a background check.", CB, (),
                      explicit(policy=CERTIFIES)), CERTIFIES, "background",
                 id="consent-certification-background-check"),
]


@pytest.mark.parametrize(("case", "applied", "obligation"), STATEMENT_OBLIGATIONS)
def test_a_statement_that_adds_an_obligation_takes_no_policy_whatever_its_class(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, case: Case, applied: str,
    obligation: str,
) -> None:
    """Obligations: every consent or attestation is checked for added obligations (question
    text and option labels) whatever class Jev reads; ADDED_OBLIGATION keeps the hold."""
    result = run_case(case, fictional_candidate, mock_job)
    assert result.ctx.form.field("q").semantic_type in (SemanticType.ATTESTATION, SemanticType.CONSENT)
    trace = result.trace()
    assert (trace["status"], trace["policy_applied"]) == ("ADDED_OBLIGATION", applied)
    assert obligation in trace["obligations"]
    assert_kept(result, run_case(case, fictional_candidate, mock_job, rules=False))


@pytest.mark.parametrize("semantic", [
    pytest.param(None, id="typed-sponsorship"),
    # As a site's own boolean widget would type it: the wording guard alone still holds.
    pytest.param(SemanticType.CUSTOM_BOOLEAN, id="typed-custom-boolean"),
])
def test_a_compound_legal_question_gets_no_policy_request(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, semantic: SemanticType | None,
) -> None:
    """Negative case: a legal question that also asks about former employment, with Jev
    scripted to read not_current_or_former_employee at 0.99: no request, never answered."""
    label = ("Are you legally authorized to work in the U.S. without current or future "
             "sponsorship, and have you previously worked for Mock Co?")
    case = Case(label, script=explicit(policy=NOT_EMPLOYEE), semantic=semantic)
    result = run_case(case, fictional_candidate, mock_job)
    assert result.ctx.form.field("q").semantic_type is (semantic or SemanticType.SPONSORSHIP)
    assert result.requests() == [] and result.packet.answers == []
    assert result.classes() == frozenset()


@pytest.mark.parametrize(("label", "options", "semantic"), [
    pytest.param("Are you eligible to work in the United States?", YES_NO,
                 SemanticType.WORK_AUTHORIZATION, id="work-authorization"),
    pytest.param("Do you require sponsorship to work at Mock Co?", YES_NO, SemanticType.SPONSORSHIP,
                 id="sponsorship"),
    pytest.param("Are you a protected veteran?", ("Yes", "No", "I prefer not to say"),
                 SemanticType.EEO_VETERAN_STATUS, id="eeo-veteran"),
    pytest.param("Do you have a disability?", ("Yes", "No", "I prefer not to say"),
                 SemanticType.EEO_DISABILITY_STATUS, id="eeo-disability"),
    pytest.param("Are you Hispanic or Latino?", ("Yes", "No", "I prefer not to say"),
                 SemanticType.EEO_RACE_ETHNICITY, id="eeo-hispanic"),
])
def test_legal_and_eeo_fields_never_take_a_policy(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, options: tuple[str, ...],
    semantic: SemanticType,
) -> None:
    """Negative case: legal- and EEO-typed fields take no class, whatever Jev would read."""
    case = Case(label, R, options, explicit(policy=CLAIMS))
    result = run_case(case, fictional_candidate, mock_job)
    assert result.ctx.form.field("q").semantic_type is semantic
    assert result.classes() == frozenset()
    assert result.requests() == [] and result.packet.answers == []


# --- outcomes that hold with a prompt naming the policy ------------------------------------------


HOLD_WITH_PROMPT = [
    # Thresholds with policy Yes: an unreadable minimum or no stated years.
    pytest.param(Case("Do you have 3-5 years of experience in paid media?",
                      script=Script(policy=THRESHOLDS)), "YEARS_UNREADABLE", THRESHOLDS, id="range"),
    pytest.param(Case("Do you have less than 2 years of experience?", script=Script(policy=THRESHOLDS)),
                 "YEARS_UNREADABLE", THRESHOLDS, id="upper-bound"),
    pytest.param(Case("Do you have 5+ years of experience, including 2 years in paid social?",
                      script=Script(policy=THRESHOLDS)), "YEARS_UNREADABLE", THRESHOLDS,
                 id="two-different-numbers"),
    # A bare number beside the years mention makes the minimum unreadable too.
    pytest.param(Case("Do you have 5+ years of experience, including 2 in paid social?",
                      script=Script(policy=THRESHOLDS)), "YEARS_UNREADABLE", THRESHOLDS,
                 id="a-second-bare-number"),
    pytest.param(Case("Do you have 5+ years of experience managing teams of 10 or more?",
                      script=Script(policy=THRESHOLDS)), "YEARS_UNREADABLE", THRESHOLDS,
                 id="a-bare-team-size"),
    pytest.param(Case("Are you at least 18 years of age?", script=Script(policy=CLAIMS)),
                 "YEARS_UNREADABLE", THRESHOLDS, id="age-read-as-a-claim"),
    pytest.param(Case(MARKETING_5, script=Script(policy=THRESHOLDS), years={}), "NO_STATED_YEARS",
                 THRESHOLDS, id="no-stated-years"),
    # The polarity gate (passing sides: ANSWERED).
    pytest.param(Case(EMPLOYEE_Q, script=explicit(policy=NOT_EMPLOYEE, polarity_probability=0.94)),
                 "POLARITY_UNSETTLED", NOT_EMPLOYEE, id="polarity-probability-0.94"),
    pytest.param(Case(EMPLOYEE_Q, script=explicit(policy=NOT_EMPLOYEE, polarity_confidence=0.89)),
                 "POLARITY_UNSETTLED", NOT_EMPLOYEE, id="polarity-confidence-0.89"),
    # Placing without an exact option: the pick must pass the gate and not say the opposite.
    pytest.param(Case(SFMC_Q, S, SFMC_OPTIONS, Script(policy=CLAIMS, yes_option="some experience",
                                                     option_probability=0.94)),
                 "NO_OPTION", CLAIMS, id="yes-option-probability-0.94"),
    pytest.param(Case(SFMC_Q, S, SFMC_OPTIONS, Script(policy=CLAIMS, yes_option="some experience",
                                                     option_confidence=0.89)),
                 "NO_OPTION", CLAIMS, id="yes-option-confidence-0.89"),
    pytest.param(Case(SFMC_Q, S, SFMC_OPTIONS, Script(policy=CLAIMS, yes_option="No experience")),
                 "NO_OPTION", CLAIMS, id="yes-option-says-no"),
    pytest.param(Case(SFMC_Q, S, SFMC_OPTIONS, Script(policy=CLAIMS, yes_option=None)),
                 "NO_OPTION", CLAIMS, id="yes-option-none"),
    pytest.param(Case(AFFILIATES_Q, R, ("Yes, I have", "No, I have not"),
                      explicit(policy=NOT_EMPLOYEE, no_option="Yes, I have")),
                 "NO_OPTION", NOT_EMPLOYEE, id="no-option-says-yes"),
    # A required checkbox left unchecked, a text answer longer than the field.
    pytest.param(Case("Have you ever worked for Mock Co?", CB, (), explicit(policy=NOT_EMPLOYEE)),
                 "INVALID", NOT_EMPLOYEE, id="required-checkbox-unchecked"),
    pytest.param(Case("I have previously been employed by Mock Co.", CB, (), explicit(policy=NOT_EMPLOYEE)),
                 "INVALID", NOT_EMPLOYEE, id="attestation-checkbox-left-unchecked"),
    pytest.param(Case(MOCK_CO_TEXT_Q, T, (), explicit(policy=NOT_EMPLOYEE), extra={"max_length": 2}),
                 "INVALID", NOT_EMPLOYEE, id="text-longer-than-max-length"),
    # A text question that asks for details with the answer (passing side: ANSWERED).
    pytest.param(Case(AGENCY_TEXT_Q, T, (), Script(policy=CLAIMS, details_if_yes=0.06)),
                 "NEEDS_DETAIL", CLAIMS, id="details-if-yes-0.06"),
    pytest.param(Case(MOCK_CO_TEXT_Q, TA, (), explicit(policy=NOT_EMPLOYEE, details_if_no=0.9)),
                 "NEEDS_DETAIL", NOT_EMPLOYEE, id="details-if-no"),
]


@pytest.mark.parametrize(("case", "status", "policy"), HOLD_WITH_PROMPT)
def test_an_outcome_that_needs_the_person_holds_with_a_prompt_naming_the_policy(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, case: Case, status: str, policy: str,
) -> None:
    """Outcomes: YEARS_UNREADABLE, NO_STATED_YEARS, POLARITY_UNSETTLED, NO_OPTION, INVALID and
    NEEDS_DETAIL hold (AIHold) with a prompt that names the policy and replaces the field's
    missing prompt; the field keeps its prior reason."""
    result = run_case(case, fictional_candidate, mock_job)
    baseline = run_case(case, fictional_candidate, mock_job, rules=False)
    trace = result.trace()
    assert (trace["status"], trace["policy_applied"]) == (status, policy)
    assert result.answer() is None
    held, before = result.hold(), baseline.hold()
    assert held is not None and before is not None
    assert held.reason is before.reason and held.prompt != before.prompt
    assert held.prompt.startswith(f"Your answer policy {policy} ")
    assert repr(case.label) in held.prompt


YEARS_CONFLICTS = [
    pytest.param(Case(MARKETING_5, script=Script(policy=THRESHOLDS), years={"years_experience": 8},
                      facts=(years_fact("years_experience.total", 9),)),
                 ["user_years_experience", "user_years_experience.total"], id="two-stated-totals"),
    pytest.param(Case(MARKETING_5, script=Script(policy=THRESHOLDS), years={},
                      facts=(derived_years("years_experience", 8, "resume_years_a"),
                             derived_years("years_experience", 10, "resume_years_b"))),
                 ["resume_years_a", "resume_years_b"], id="two-derived-totals"),
]


@pytest.mark.parametrize(("case", "fact_ids"), YEARS_CONFLICTS)
def test_disagreeing_stated_years_hold_the_threshold(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, case: Case, fact_ids: list[str],
) -> None:
    """Thresholds: two values for one key (none stated by the person over the other) are
    YEARS_CONFLICT: an AIHold naming the policy and the facts replaces the prompt; nothing
    is compared."""
    result = run_case(case, fictional_candidate, mock_job)
    baseline = run_case(case, fictional_candidate, mock_job, rules=False)
    trace = result.trace()
    assert trace["status"] == "YEARS_CONFLICT"
    assert sorted(trace["years_rule"]["fact_ids"]) == fact_ids
    held, before = result.hold(), baseline.hold()
    assert result.answer() is None and held is not None and before is not None
    assert held.reason is before.reason
    assert held.prompt.startswith(f"Your answer policy {THRESHOLDS} needs your stated years of "
                                  f"experience to answer {case.label!r}, but they disagree (")
    assert all(fact_id in held.prompt for fact_id in fact_ids)


# --- answers -------------------------------------------------------------------------------------


ANSWERED = [
    # Passing sides of the class, polarity and option gates (>= 0.95 and >= 0.90).
    pytest.param(Case(CLAIM_Q, script=Script(policy=CLAIMS, policy_probability=0.95)), "Yes", CLAIMS,
                 ALL_CLASSES, id="class-probability-0.95"),
    pytest.param(Case(CLAIM_Q, script=Script(policy=CLAIMS, policy_confidence=0.90)), "Yes", CLAIMS,
                 ALL_CLASSES, id="class-confidence-0.90"),
    pytest.param(Case(EMPLOYEE_Q, script=explicit(policy=NOT_EMPLOYEE, polarity_probability=0.95)),
                 "No", NOT_EMPLOYEE, ALL_CLASSES, id="polarity-probability-0.95"),
    pytest.param(Case(EMPLOYEE_Q, script=explicit(policy=NOT_EMPLOYEE, polarity_confidence=0.90)),
                 "No", NOT_EMPLOYEE, ALL_CLASSES, id="polarity-confidence-0.90"),
    pytest.param(Case(SFMC_Q, S, SFMC_OPTIONS, Script(policy=CLAIMS, yes_option="some experience",
                                                     option_probability=0.95)),
                 "Yes, some experience", CLAIMS, ALL_CLASSES, id="yes-option-probability-0.95"),
    pytest.param(Case(SFMC_Q, S, SFMC_OPTIONS, Script(policy=CLAIMS, yes_option="some experience",
                                                     option_confidence=0.90)),
                 "Yes, some experience", CLAIMS, ALL_CLASSES, id="yes-option-confidence-0.90"),
    pytest.param(Case(AFFILIATES_Q, R, ("Yes, I have", "No, I have not"),
                      explicit(policy=NOT_EMPLOYEE, no_option="No, I have not")),
                 "No, I have not", NOT_EMPLOYEE, ALL_CLASSES, id="plain-no-option"),
    pytest.param(Case(CLAIM_Q, S, ("True", "False"), Script(policy=CLAIMS)), "True", CLAIMS,
                 ALL_CLASSES, id="exact-true-option"),
    # A claim whose wording names years only as a timeframe keeps the claims rule.
    pytest.param(Case("Have you managed paid social campaigns within the last 3 years?",
                      script=Script(policy=CLAIMS)), "Yes", CLAIMS, ALL_CLASSES,
                 id="claim-with-a-timeframe"),
    # Custom types on every control shape: checkbox, text box and text area.
    pytest.param(Case(CLAIM_Q, CB, (), Script(policy=CLAIMS)), True, CLAIMS, ALL_CLASSES,
                 id="custom-checkbox"),
    pytest.param(Case(AGENCY_TEXT_Q, T, (), Script(policy=CLAIMS, details_if_yes=0.05)), "Yes.",
                 CLAIMS, ALL_CLASSES, id="text-details-0.05"),
    pytest.param(Case(MOCK_CO_TEXT_Q, TA, (), explicit(policy=NOT_EMPLOYEE, details_if_yes=0.9)),
                 "No.", NOT_EMPLOYEE, ALL_CLASSES, id="textarea-details-only-asked-for-a-yes"),
    pytest.param(Case(MOCK_CO_TEXT_Q, T, (), explicit(policy=NOT_EMPLOYEE),
                      extra={"input_type": "text", "max_length": 3}),
                 "No.", NOT_EMPLOYEE, ALL_CLASSES, id="text-input-fits-max-length-3"),
    pytest.param(Case(EMPLOYEE_Q, script=explicit(semantic=UNKNOWN_READING, policy=NOT_EMPLOYEE)),
                 "No", NOT_EMPLOYEE, ALL_CLASSES, id="unknown-with-0.05-explicit-mass"),
    pytest.param(Case(CLAIM_Q, R, MANY_OPTIONS, Script(policy=CLAIMS)), "Yes", CLAIMS, ALL_CLASSES,
                 id="250-options"),
    # Typed fields take their classes: an attestation three, a consent certifies_truth, a
    # years count the threshold, a residence the sanctions class.
    pytest.param(Case(CERTIFY_Q, CB, (), explicit(policy=CERTIFIES)), True, CERTIFIES,
                 ATTESTATION_CLASSES, id="attestation-checkbox"),
    pytest.param(Case("I certify that the information I provided is true.", R,
                      ("I certify", "I do not certify"), explicit(policy=CERTIFIES, yes_option="I certify")),
                 "I certify", CERTIFIES, ATTESTATION_CLASSES, id="attestation-statement-options"),
    pytest.param(Case("I have never worked for Mock Co.", CB, (),
                      explicit(policy=NOT_EMPLOYEE, polarity="REVERSED")),
                 True, NOT_EMPLOYEE, ATTESTATION_CLASSES, id="attestation-never-worked-here"),
    pytest.param(Case("I confirm that I am not a current or former employee of Mock Co.", R,
                      ("I confirm", "I do not confirm"),
                      explicit(policy=NOT_EMPLOYEE, polarity="REVERSED", yes_option="I confirm")),
                 "I confirm", NOT_EMPLOYEE, ATTESTATION_CLASSES, id="attestation-statement-employee"),
    # "Mock Marketing Co" is a name, not a consent to marketing messages.
    pytest.param(Case("I certify that the information I gave Mock Marketing Co is true and complete.",
                      CB, (), explicit(policy=CERTIFIES)), True, CERTIFIES, frozenset({CERTIFIES}),
                 id="consent-typed-certification-naming-mock-marketing-co"),
    pytest.param(Case(YEARS_TYPED_Q, script=Script(policy=THRESHOLDS)), "Yes", THRESHOLDS,
                 frozenset({THRESHOLDS}), id="years-type"),
    pytest.param(Case(COUNTRY_Q, script=explicit(policy=SANCTIONS)), "No", SANCTIONS,
                 frozenset({SANCTIONS}), id="country-type"),
    pytest.param(Case("Current location: are you in Cuba, Iran, North Korea or Syria?",
                      script=explicit(policy=SANCTIONS)), "No", SANCTIONS, frozenset({SANCTIONS}),
                 id="location-type"),
    pytest.param(Case("State of residence: are you in Crimea?", script=explicit(policy=SANCTIONS)),
                 "No", SANCTIONS, frozenset({SANCTIONS}), id="state-type"),
    pytest.param(Case("City of residence: do you live in Sevastopol?", script=explicit(policy=SANCTIONS)),
                 "No", SANCTIONS, frozenset({SANCTIONS}), id="city-type"),
    # Sanctions: a place named only by an option, sanctions wording, and reversed statements.
    pytest.param(Case("Are you located in any of these countries?", R,
                      ("Yes, one of: Cuba, Iran, North Korea, Syria", "No"), explicit(policy=SANCTIONS)),
                 "No", SANCTIONS, ALL_CLASSES, id="sanctioned-place-in-an-option"),
    pytest.param(Case("Are you located in a country subject to U.S. sanctions or embargo?",
                      script=explicit(policy=SANCTIONS)), "No", SANCTIONS, frozenset({SANCTIONS}),
                 id="sanctions-wording"),
    pytest.param(Case("Not located in a sanctioned country (Cuba, Iran, North Korea, Syria, Crimea)",
                      CB, (), explicit(policy=SANCTIONS, polarity="REVERSED")),
                 True, SANCTIONS, frozenset({SANCTIONS}), id="reversed-country-checkbox"),
    # The contract's own example, typed ATTESTATION by the heuristics.
    pytest.param(Case("I confirm I am not located in Cuba, Iran, North Korea, Syria or Crimea.", CB, (),
                      explicit(policy=SANCTIONS, polarity="REVERSED")),
                 True, SANCTIONS, ATTESTATION_CLASSES, id="reversed-attestation-checkbox"),
    pytest.param(Case("Are you not located in any sanctioned country (Cuba, Iran, North Korea or Syria)?",
                      script=explicit(policy=SANCTIONS, polarity="REVERSED")),
                 "Yes", SANCTIONS, frozenset({SANCTIONS}), id="reversed-radio"),
    # Employee: only the newest GLOBAL answer, or a Yes saved for this job, contradicts.
    pytest.param(Case(EMPLOYEE_Q, script=explicit(policy=NOT_EMPLOYEE),
                      saved=(saved("sa.old", EMPLOYED_HERE, "Yes", confirmed_at=EARLIER),
                             saved("sa.new", EMPLOYED_HERE, "No"))),
                 "No", NOT_EMPLOYEE, ALL_CLASSES, id="newest-saved-answer-is-no"),
    pytest.param(Case(EMPLOYEE_Q, script=explicit(policy=NOT_EMPLOYEE),
                      saved=(saved("sa.other_job", EMPLOYED_HERE, "Yes", job=OTHER_JOB),)),
                 "No", NOT_EMPLOYEE, ALL_CLASSES, id="job-scoped-yes-for-another-job"),
    pytest.param(Case(EMPLOYEE_Q, script=explicit(policy=NOT_EMPLOYEE),
                      saved=(saved("sa.job_no", EMPLOYED_HERE, "No", job=MOCK_JOB),)),
                 "No", NOT_EMPLOYEE, ALL_CLASSES, id="job-scoped-no"),
]


@pytest.mark.parametrize(("case", "expected", "applied", "classes"), ANSWERED)
def test_an_answer_cites_the_policy_it_applies(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, case: Case, expected: str | bool,
    applied: str, classes: frozenset[str],
) -> None:
    """Outcomes and placing: ANSWERED cites the policy as SAVED_ANSWER (its id, the
    simple-answers source) at no more than the class confidence; the trace records the
    field's classes, the rule and the reference ids (evidence ids only for thresholds)."""
    result = run_case(case, fictional_candidate, mock_job)
    answer = result.answer()
    assert answer is not None and rendered(answer) == expected
    assert answer.provenance.source is AnswerSource.SAVED_ANSWER
    assert answer.provenance.reference_ids == [policy_id(result.ctx.candidate, applied)]
    assert "user:simple-answers" in (answer.provenance.note or "")
    assert answer.confidence <= case.script.policy_confidence
    assert answer.semantic_type is result.ctx.form.field("q").semantic_type
    trace = result.trace()
    assert (trace["status"], trace["policy_applied"]) == ("ANSWERED", applied)
    assert trace["classes"] == sorted(classes)
    assert trace["reference_ids"] == answer.provenance.reference_ids
    assert ("evidence_ids" in trace) is (applied == THRESHOLDS)
    assert result.hold() is None and len(result.requests()) == 1


def test_an_answer_takes_the_lowest_of_its_scores_as_confidence(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """ANSWERED: confidence is the minimum of the class, polarity and option scores, never
    above the class confidence."""
    script = Script(policy=CLAIMS, policy_confidence=0.93, policy_probability=0.96,
                    polarity_confidence=0.92, polarity_probability=0.97, yes_option="some experience",
                    option_confidence=0.91, option_probability=0.98)
    result = run_case(Case(SFMC_Q, S, SFMC_OPTIONS, script), fictional_candidate, mock_job)
    answer = result.answer()
    assert rendered(answer) == "Yes, some experience"
    assert answer.confidence == pytest.approx(0.91)
    trace = result.trace()
    assert (trace["confidence"], trace["probability"]) == (pytest.approx(0.93), pytest.approx(0.96))


# --- thresholds ----------------------------------------------------------------------------------


THRESHOLD_CASES = [
    pytest.param(Case("Do you have at least 8 years of experience in marketing?",
                      script=Script(policy=THRESHOLDS)), "Yes", ["user_years_experience"], (8.0, False),
                 id="minimum-equal-to-the-total"),
    pytest.param(Case("Do you have at least 9 years of experience in marketing?",
                      script=Script(policy=THRESHOLDS)), "No", ["user_years_experience"], (9.0, False),
                 id="minimum-above-the-total"),
    pytest.param(Case("Do you have more than 8 years of experience in marketing?",
                      script=Script(policy=THRESHOLDS)), "No", ["user_years_experience"], (8.0, True),
                 id="more-than-is-strict"),
    pytest.param(Case("Do you have over 7 years of experience in marketing?",
                      script=Script(policy=THRESHOLDS)), "Yes", ["user_years_experience"], (7.0, True),
                 id="over-is-strict"),
    pytest.param(Case("Do you have 6+ years of SEO experience?", script=Script(policy=THRESHOLDS),
                      years={"years_experience": 5, "years_experience.seo": 6}),
                 "Yes", ["user_years_experience", "user_years_experience.seo"], (6.0, False),
                 id="named-area-above-the-total"),
    pytest.param(Case("Do you have 5+ years of SEO experience?", script=Script(policy=THRESHOLDS),
                      years={"years_experience": 8, "years_experience.seo": 2}),
                 "Yes", ["user_years_experience", "user_years_experience.seo"], (5.0, False),
                 id="named-area-below-the-total-takes-the-larger"),
    pytest.param(Case("Do you have 6+ years of paid social experience?", script=Script(policy=THRESHOLDS),
                      years={"years_experience": 5, "years_experience.seo": 6}),
                 "No", ["user_years_experience"], (6.0, False), id="another-areas-fact-never-counts"),
    # A claim naming a number of years follows the threshold rule.
    pytest.param(Case("Do you have 10+ years of experience with Google Ads?", script=Script(policy=CLAIMS)),
                 "No", ["user_years_experience", EVIDENCE_ID], (10.0, False),
                 id="claim-naming-10-years"),
    pytest.param(Case("Do you have 5+ years of experience with Google Ads?", script=Script(policy=CLAIMS)),
                 "Yes", ["user_years_experience", EVIDENCE_ID], (5.0, False),
                 id="claim-naming-5-years"),
    # Numbers that set no second minimum: inside a word, an amount, a timeframe's years.
    pytest.param(Case("Do you have 5+ years of B2B marketing experience?", script=Script(policy=THRESHOLDS)),
                 "Yes", ["user_years_experience"], (5.0, False), id="number-inside-a-word-b2b"),
    pytest.param(Case("Do you have 5+ years of experience with GA4?", script=Script(policy=THRESHOLDS)),
                 "Yes", ["user_years_experience"], (5.0, False), id="number-inside-a-word-ga4"),
    pytest.param(Case("Do you have 5+ years of experience managing budgets over $1M?",
                      script=Script(policy=THRESHOLDS)),
                 "Yes", ["user_years_experience"], (5.0, False), id="an-amount"),
    pytest.param(Case("Do you have 5+ years of experience growing revenue by 50%?",
                      script=Script(policy=THRESHOLDS)),
                 "Yes", ["user_years_experience"], (5.0, False), id="a-percentage"),
    pytest.param(Case("Do you have 5+ years of SEO experience in the past 10 years?",
                      script=Script(policy=THRESHOLDS)),
                 "Yes", ["user_years_experience", "user_years_experience.seo"], (5.0, False),
                 id="a-timeframe-years-mention"),
    # A fact the person stated replaces a derived one for the same key.
    pytest.param(Case("Do you have at least 10 years of experience in marketing?",
                      script=Script(policy=THRESHOLDS), years={"years_experience": 8},
                      facts=(derived_years("years_experience", 12, "resume_years_experience"),)),
                 "No", ["user_years_experience"], (10.0, False), id="stated-replaces-derived"),
    pytest.param(Case("Do you have at least 10 years of experience in marketing?",
                      script=Script(policy=THRESHOLDS), years={},
                      facts=(derived_years("years_experience", 12, "resume_years_experience"),)),
                 "Yes", ["resume_years_experience"], (10.0, False), id="derived-alone-counts"),
    # Policy No answers No without reading years (an unreadable range, none stated).
    pytest.param(Case("Do you have 3-5 years of experience in paid media?", script=Script(policy=THRESHOLDS),
                      policies={THRESHOLDS: "No"}, years={}), "No", None, None,
                 id="policy-no-reads-no-years"),
]


@pytest.mark.parametrize(("case", "expected", "evidence", "rule"), THRESHOLD_CASES)
def test_a_threshold_is_compared_with_the_stated_years(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, case: Case, expected: str,
    evidence: list[str] | None, rule: tuple[float, bool] | None,
) -> None:
    """Thresholds: Yes iff the minimum is within the larger of the stated total and the
    area facts the question names ("more than" / "over" strict); a claim naming years
    follows this rule; the trace and note carry the facts compared."""
    result = run_case(case, fictional_candidate, mock_job)
    answer = result.answer()
    assert answer is not None and rendered(answer) == expected
    assert answer.provenance.reference_ids == [policy_id(result.ctx.candidate, THRESHOLDS)]
    trace = result.trace()
    assert (trace["status"], trace["policy"], trace["policy_applied"]) == (
        "ANSWERED", case.script.policy, THRESHOLDS)
    if evidence is None:
        assert "years_rule" not in trace and "evidence_ids" not in trace
        assert "stated years facts" not in (answer.provenance.note or "")
        return
    assert sorted(trace["evidence_ids"]) == sorted(evidence)
    assert (trace["years_rule"]["years"], trace["years_rule"]["strict"]) == rule
    note = answer.provenance.note or ""
    assert "minimum years compared with the stated years facts" in note
    assert all(fact_id in note for fact_id in evidence)


# --- the priority of facts, the writer, wording equivalence and the budget -----------------------


@pytest.mark.parametrize(("decision", "noul", "source", "expected"), [
    pytest.param("YES", 0.95, AnswerSource.GENERATED_FROM_FACTS, "Yes", id="fact-yes-at-the-gate"),
    pytest.param("NO", 0.95, AnswerSource.GENERATED_FROM_FACTS, "No", id="fact-no-at-the-gate"),
    pytest.param("YES", 0.94, AnswerSource.SAVED_ANSWER, "Yes", id="supporting-fact-below-the-gate"),
    pytest.param("NO", 0.94, AnswerSource.SAVED_ANSWER, "Yes", id="negating-fact-below-the-gate"),
])
def test_the_experience_screeners_own_answer_comes_before_any_policy(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, decision: str, noul: float,
    source: AnswerSource, expected: str,
) -> None:
    """Priority: the experience screener's YES or NO with a supporting / negating fact noul
    >= 0.95 settles the field and no policy request follows (a verified No beats the claims
    policy's Yes); below 0.95 the screener holds and the policy answers."""
    script = Script(policy=CLAIMS, decisions={"experience": (decision, 0.99)},
                    supports={EVIDENCE_ID: noul} if decision == "YES" else {},
                    negates={IN_HOUSE.id: noul} if decision == "NO" else {})
    result = run_case(Case(CLAIM_Q, script=script, facts=(IN_HOUSE,)), fictional_candidate, mock_job)
    answer = result.answer()
    assert answer is not None and (answer.provenance.source, rendered(answer)) == (source, expected)
    settled = source is AnswerSource.GENERATED_FROM_FACTS
    assert len(result.requests()) == (0 if settled else 1)
    [screener] = traces(result.resolver, "experience_screener")
    assert screener["status"] == ("ANSWERED" if settled else "UNKNOWN")


def test_a_fact_screener_answer_comes_before_any_policy(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """Priority: a graded choice the fact screener answers from a verified fact never gets a
    policy request (the policy would have picked another option)."""
    sfmc = text_fact("fact.sfmc", "marketing_cloud_experience", "some Salesforce Marketing Cloud campaigns")
    script = Script(policy=CLAIMS, yes_option="extensive", decisions={"fact_choice": ("o1", 0.99)},
                    supports={sfmc.id: 0.97})
    result = run_case(Case(SFMC_Q, S, SFMC_OPTIONS, script, facts=(sfmc,)), fictional_candidate, mock_job)
    answer = result.answer()
    assert answer is not None and rendered(answer) == "Yes, some experience"
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.GENERATED_FROM_FACTS, [sfmc.id])
    assert result.requests() == []


@pytest.mark.parametrize("with_writer", [False, True], ids=["no-writer", "writer"])
def test_an_if_yes_companion_is_never_answered_by_a_policy(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, with_writer: bool,
) -> None:
    """A free-text "If yes, please describe" companion after a policy Yes takes no policy
    (its label is not a yes/no question): it holds, unless the writer answers it from facts."""
    companion = "If yes, please describe the Google Ads campaigns you managed."
    scripts = {CLAIM_Q[:40]: Script(policy=CLAIMS),
               companion[:40]: Script(route="WRITER", narrative="prose", policy=CLAIMS,
                                      supports={EVIDENCE_ID: 0.99}, nouls={"q": 0.99, "complete": 0.99})}
    provider = PolicyJev(scripts)
    writer = Writer(EVIDENCE_ID, "I have managed Google Ads campaigns for seven years.") if with_writer else None
    packet, ctx, resolver = resolve(provider, with_policies(fictional_candidate), mock_job,
                                    typed_field(CLAIM_Q, R, YES_NO), typed_field(companion, TA, field_id="d"),
                                    writer=writer)
    assert rendered(packet.answer_for("q")) == "Yes"
    [request] = provider.policy_requests()
    assert request["state"]["question"]["label"] == CLAIM_Q
    assert [t["field_id"] for t in traces(resolver)] == ["q"]
    assert resolver.router is not None
    report = resolver.router.report_for(ctx.form)
    assert report is not None
    assert DynamicPacketResolver._policy_classes(ctx.form.field("d"), report.field("d")) == frozenset()
    described = packet.answer_for("d")
    if with_writer:
        assert described is not None
        assert (described.provenance.source, described.provenance.reference_ids) == (
            AnswerSource.GENERATED_FROM_FACTS, [EVIDENCE_ID])
    else:
        assert described is None
        hold = next(m for m in packet.missing_inputs if m.field_id == "d")
        assert hold.prompt == "Narrative writer is not configured"


def test_a_policy_is_never_offered_as_a_reworded_saved_question(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """_wording_candidates: a custom field with another untyped GLOBAL saved answer is
    offered that answer's wording only, never a policy's statement."""
    agency = saved("sa.agency", "Have you worked at an advertising agency?", "Yes")
    case = Case("Have you worked in a performance marketing agency environment?",
                script=Script(policy=CLAIMS), saved=(agency,))
    result = run_case(case, fictional_candidate, mock_job)
    [request] = result.provider.asked("wording")
    assert request["state"]["saved_questions"] == {"q0": {"question": agency.question, "variants": []}}
    assert not any(question in json.dumps(request) for question in ANSWER_POLICY_QUESTIONS.values())
    [equivalence] = traces(result.resolver, "question_equivalence")
    assert equivalence["candidate_ids"] == [["sa.agency"]]
    assert rendered(result.answer()) == "Yes"
    assert result.trace()["status"] == "ANSWERED"


BUDGET_FIELDS = [
    (EMPLOYEE_Q, explicit(policy=NOT_EMPLOYEE)),
    (SANCTIONS_Q, explicit(policy=SANCTIONS)),
    (CLAIM_Q, Script(policy=CLAIMS)),
]


def budget_run(candidate: CandidateProfile, job: JobRecord, budget: CallBudget,
               granted: list[tuple[int, float]], monkeypatch: pytest.MonkeyPatch,
               ) -> tuple[DynamicPacketResolver, PacketContext]:
    """Three policy candidates on one form; ``granted`` records every allow_calls."""
    original = budget.allow_calls

    def allow_calls(calls: int, usd: float) -> None:
        granted.append((calls, usd))
        original(calls, usd)

    monkeypatch.setattr(budget, "allow_calls", allow_calls)
    provider = PolicyJev({label[:40]: script for label, script in BUDGET_FIELDS})
    router = AIFormRouter(decisions(provider, budget))
    fields = [typed_field(label, R, YES_NO, field_id=f"f{i}") for i, (label, _) in enumerate(BUDGET_FIELDS)]
    form = router.annotate(ApplicationForm(url="https://example.test/apply", fields=fields),
                           document_id="answer-policies")
    ctx = context(form, candidate, job)
    resolver = DynamicPacketResolver(router.decisions, router=router)
    packet = asyncio.run(resolver.resolve(ctx))
    assert ctx.problems(packet) == [] and len(packet.answers) == 3
    assert len(provider.policy_requests()) == 3
    return resolver, ctx


def test_policy_candidates_widen_a_form_scaled_budget_once_per_step(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget: with n candidates the budget gets allow_calls(2n, 0.01n) once per step; a
    re-resolve of the same step grants nothing more."""
    budget = CallBudget(scales_with_form=True)
    granted: list[tuple[int, float]] = []
    resolver, ctx = budget_run(with_policies(fictional_candidate), mock_job, budget, granted, monkeypatch)
    assert granted == [(6, pytest.approx(0.03))]
    # One full-form call before the form's allowance (24), then the policy pass's room (6).
    assert budget.max_calls == 1 + 24 + 6
    assert [r.purpose for r in budget.receipts].count("answer_policy") == 3
    limits = (budget.max_calls, budget.max_usd)
    asyncio.run(resolver.resolve(ctx))
    assert granted == [(6, pytest.approx(0.03))]
    assert (budget.max_calls, budget.max_usd) == limits


def test_a_fixed_budget_keeps_its_limits(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget: allow_calls only matters for CallBudget(scales_with_form=True)."""
    budget = CallBudget()
    granted: list[tuple[int, float]] = []
    budget_run(with_policies(fictional_candidate), mock_job, budget, granted, monkeypatch)
    assert granted == [(6, pytest.approx(0.03))]
    assert (budget.max_calls, budget.max_usd) == (48, 0.50)
