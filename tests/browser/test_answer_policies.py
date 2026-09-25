"""Round 12: the person's standing answer policies, applied by one Jev class decision.

Every field here is typed the way a live form types it: first by the browser heuristics
(``semantics.classify``), then by Jev's full-form annotation (``router.annotate``). The
candidate is the fictional Avery Example; the policies reach the profile through the
simple-answers import, exactly as the person's map does. The thirty wordings are live
screener wordings recorded in the pilot's readiness report and the WP2 briefs, the kinds the
lead answered by rule at retry five (employer names replaced by the fictional Mock Co); the
lead's answer sheet itself is private data and was not read. Nothing is ever submitted:
these are packet resolutions only.
"""
from __future__ import annotations

import asyncio
import json
import threading
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
from interviewmaxxing_browser.semantics import classify as classify_semantics
from interviewmaxxing_candidate.simple_answers import SimpleAnswers
from interviewmaxxing_core import (
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
    PacketContext,
    TextValue,
    VerificationMethod,
    VerificationStatus,
)
from interviewmaxxing_core.answer_policies import ANSWER_POLICY_KEYS, ANSWER_POLICY_QUESTIONS
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
YES_NO = ("Yes", "No")
CLAIMS = "claims_experience_asked"
THRESHOLDS = "meets_experience_thresholds"
CERTIFIES = "certifies_truth"
NOT_EMPLOYEE = "not_current_or_former_employee"
SANCTIONS = "sanctioned_locations"
PERSON_POLICIES = {CLAIMS: "Yes", THRESHOLDS: "Yes", CERTIFIES: "Yes", NOT_EMPLOYEE: "No",
                   SANCTIONS: "No"}
"""The person's standing rules as the lead recorded them on 2026-09-25."""

_NO_ANSWER = ("NONE", "UNKNOWN", "hold", "NOT_EXPERIENCE")
"""The choice an unscripted decision takes: no saved answer and no fact settles anything."""


# --- a scripted Jev whose answers depend on each request's own content -------------------------


@dataclass
class Script:
    """How Jev reads one question (matched by a fragment of its wording)."""

    route: str = "COPY_KNOWN"
    scope: str = "HISTORICAL_OR_CONTEXTUAL"
    narrative: str = "literal"
    semantic: str | dict[str, float] | None = None
    """The semantic reading (``s<i>``); None reads a custom field as its own control."""
    policy: str | dict[str, float] = "NONE"
    """The class Jev picks at 0.99, or its whole distribution."""
    policy_confidence: float = 0.97
    polarity: str | dict[str, float] = "SAME"
    yes_option: str | None = None
    """A fragment of the option label Jev names as the mildest yes (None: NONE)."""
    no_option: str | None = None
    details_if_yes: float = 0.0
    details_if_no: float = 0.0
    decisions: dict[str, tuple[str, float]] = field(default_factory=dict)
    """Other decisions by question name (``experience``, ``statement`` …): choice and probability."""
    nouls: dict[str, float] = field(default_factory=dict)
    """Other nouls by name prefix (``has_``, ``lacks_``)."""


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
    resolver decides fields concurrently."""

    def __init__(self, scripts: dict[str, Script] | None = None) -> None:
        self.scripts = scripts or {}
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

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
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
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
        script = self.script(self._wording(state))
        if name == "details_if_yes":
            return script.details_if_yes
        if name == "details_if_no":
            return script.details_if_no
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
            return script.policy, 0.99, script.policy_confidence
        if name == "polarity":
            return script.polarity, 0.99, 0.97
        if name in ("yes_option", "no_option"):
            fragment = script.yes_option if name == "yes_option" else script.no_option
            options: dict[str, str] = state["options"]
            key = next((k for k, label in options.items() if fragment and fragment in label), "NONE")
            return key, 0.99, 0.97
        if name in script.decisions:
            pick, probability = script.decisions[name]
            return pick, probability, 0.97
        held = next(choice for choice in (*_NO_ANSWER, criteria[0]) if choice in criteria)
        return held, 1.0, 1.0


def decisions(provider: PolicyJev, budget: CallBudget | None = None) -> BoundedDecisions:
    return BoundedDecisions(JevClient(ApiKey("synthetic-key", source="test"),
        transport=provider, max_attempts=1), budget or CallBudget())


# --- fictional profile and fields ----------------------------------------------------------------


def years_fact(key: str, value: float, *, source: str = "user:confirmed fact import",
               fid: str | None = None) -> CandidateFact:
    return CandidateFact(id=fid or f"user_{key}", key=key, value=value, source=source,
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


def typed_field(label: str, control: ControlType, options: tuple[str, ...] = (), *,
                field_id: str = "q", help_text: str | None = None,
                required: bool = True) -> ApplicationField:
    """A field as a live form types it: the browser heuristics' reading of its label."""
    semantic = classify_semantics(label=label, help_text=help_text or "", control_type=control)
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label, help_text=help_text,
        semantic_type=semantic, control_type=control, required=required,
        options=[FieldOption(value=f"v{i}", label=o) for i, o in enumerate(options)] or None)


def context(form: ApplicationForm, candidate: CandidateProfile, job: JobRecord) -> PacketContext:
    app = Application(id="app-policies", request_id="request-policies", job_id=job.id,
        candidate_id=candidate.id, state=ApplicationState.INSPECTING, version=2,
        created_at="2026-09-25T00:00:00Z", updated_at="2026-09-25T00:00:00Z")
    return PacketContext(application=app, job=job, form=form, candidate=candidate)


def resolve(provider: PolicyJev, candidate: CandidateProfile, job: JobRecord,
            *fields: ApplicationField, budget: CallBudget | None = None,
            ) -> tuple[Any, PacketContext, DynamicPacketResolver]:
    router = AIFormRouter(decisions(provider, budget))
    annotated = router.annotate(ApplicationForm(url="https://example.test/apply", fields=list(fields)),
                                document_id="answer-policies")
    ctx = context(annotated, candidate, job)
    resolver = DynamicPacketResolver(router.decisions, router=router)
    packet = asyncio.run(resolver.resolve(ctx))
    assert ctx.problems(packet) == []
    return packet, ctx, resolver


def traces(resolver: DynamicPacketResolver, stage: str = "answer_policy") -> list[dict[str, Any]]:
    return [t for t in resolver.narrative_traces if t["stage"] == stage]


def settled_by_years_facts(resolver: DynamicPacketResolver) -> bool:
    """WP2 round 11's deterministic years screener, merged after this round, settles some
    years thresholds from the years facts before any policy (the fact paths come first): its
    trace is ``experience_screener`` with ``via: years`` (or ``support: years_fact``) and
    status ANSWERED. On this round's own base it never runs, so the policy path is asserted
    in full here."""
    return any(t["stage"] == "experience_screener" and t.get("status") == "ANSWERED"
               and (t.get("via") == "years" or t.get("support") == "years_fact")
               for t in resolver.narrative_traces)


def rendered(answer: Any) -> str | bool:
    value = answer.value
    if isinstance(value, ChoiceValue):
        return value.label
    if isinstance(value, TextValue):
        return value.text
    assert isinstance(value, BooleanValue)
    return value.checked


# --- thirty live screener wordings ---------------------------------------------------------------

R, S, TA, T, CB = (ControlType.RADIO, ControlType.SELECT, ControlType.TEXTAREA, ControlType.TEXT,
                   ControlType.CHECKBOX)
GRADED = ("No", "Yes, some experience", "Yes, extensive experience")
TEAM_SCALE = ("No", "Yes, as part of a team", "Yes, I owned it")
AGREE = ("I agree", "I do not agree")
EXPLICIT = Script(route="HUMAN_INPUT", scope="EXPLICIT_ANSWER")
"""How the classifier reads an eligibility-style screener: never a fact screener."""


def explicit_policy(policy: str) -> Script:
    """An eligibility-style screener (never a fact screener) that Jev places in ``policy``."""
    return Script(policy=policy, route="HUMAN_INPUT", scope="EXPLICIT_ANSWER")

LIVE: list[tuple[str, ControlType, tuple[str, ...], Script, str | bool, str]] = [
    # claims_experience_asked: Yes (the experience screener finds no fact and holds first)
    ("Have you owned paid social strategy and execution across multiple platforms?", R, YES_NO,
     Script(policy=CLAIMS), "Yes", CLAIMS),
    ("Have you led client-facing conversations, such as performance readouts, QBRs or strategy "
     "reviews?", R, YES_NO, Script(policy=CLAIMS), "Yes", CLAIMS),
    ("Do you have SEO AND GEO optimization experience?", R, YES_NO, Script(policy=CLAIMS), "Yes",
     CLAIMS),
    ("Do you have hands-on experience managing paid campaigns across Meta/Instagram?", R, YES_NO,
     Script(policy=CLAIMS), "Yes", CLAIMS),
    ("Have you worked in a performance marketing agency environment?", S, YES_NO,
     Script(policy=CLAIMS), "Yes", CLAIMS),
    ("Do you have direct, hands-on experience managing App Store Optimization (ASO) across iOS, "
     "Android, or Windows platforms?", R, YES_NO, Script(policy=CLAIMS), "Yes", CLAIMS),
    ("Do you have experience managing acquisition specifically for a self-serve or product-led "
     "growth (PLG) motion?", R, YES_NO, Script(policy=CLAIMS), "Yes", CLAIMS),
    ("Have you personally launched, managed, and optimized paid campaigns directly inside Meta Ads "
     "Manager?", R, YES_NO, Script(policy=CLAIMS), "Yes", CLAIMS),
    ("Have you personally managed influencer, creator, affiliate, ambassador, or brand-partnership "
     "campaigns from outreach through post-campaign reporting?", R, YES_NO, Script(policy=CLAIMS),
     "Yes", CLAIMS),
    ("Have you personally owned a pipeline/revenue number and been responsible for full-funnel "
     "reporting and forecasting?", S, YES_NO, Script(policy=CLAIMS), "Yes", CLAIMS),
    ("Do you have hands-on experience running hosted events (webinars, roundtables, and/or "
     "in-person) as a measurable pipeline-driving channel?", R, YES_NO, Script(policy=CLAIMS), "Yes",
     CLAIMS),
    ("Have you run shopping via Performance Max Campaigns before? When dealing with shopping "
     "campaigns, have you managed product feeds?", R, YES_NO, Script(policy=CLAIMS), "Yes", CLAIMS),
    ("Do you have lifecycle, CRM, retention, or growth marketing experience within B2B SaaS?", R,
     YES_NO, Script(policy=CLAIMS), "Yes", CLAIMS),
    ("Do you have experience with Salesforce Marketing Cloud?", S, GRADED,
     Script(policy=CLAIMS, yes_option="some experience", no_option="No"), "Yes, some experience",
     CLAIMS),
    ("Have you managed an annual paid media budget of $1M or more?", S, TEAM_SCALE,
     Script(policy=CLAIMS, yes_option="part of a team", no_option="No"), "Yes, as part of a team",
     CLAIMS),
    ("Have you worked in a digital marketing agency?", T, (), Script(policy=CLAIMS), "Yes.", CLAIMS),
    # meets_experience_thresholds: Yes within the stated years (total 8), No above
    ("Do you have 8+ years of experience in demand generation or growth marketing at a B2B SaaS "
     "company?", R, YES_NO, Script(policy=THRESHOLDS), "Yes", THRESHOLDS),
    ("Do you have at least 8 years of total experience in direct response marketing?", R, YES_NO,
     Script(policy=THRESHOLDS), "Yes", THRESHOLDS),
    ("Do you have 5+ years of hands-on paid media experience, including paid social, paid search "
     "and programmatic?", R, YES_NO, Script(policy=THRESHOLDS), "Yes", THRESHOLDS),
    ("Do you have 10+ years of experience leading growth marketing teams?", S, YES_NO,
     Script(policy=THRESHOLDS), "No", THRESHOLDS),
    # certifies_truth: the affirmative option
    ("I certify the information contained in my resume and application is true and accurate. I "
     "understand that if hired any misrepresentation of information may impact my employment with "
     "Mock Co.", CB, (), Script(policy=CERTIFIES, route="HUMAN_INPUT", scope="EXPLICIT_ANSWER"),
     True, CERTIFIES),
    ("I certify that the information I provided is true, accurate and complete.", S, YES_NO,
     Script(policy=CERTIFIES, route="HUMAN_INPUT", scope="EXPLICIT_ANSWER"), "Yes", CERTIFIES),
    ("By submitting this application, I certify that all information provided is true and "
     "complete to the best of my knowledge.", R, AGREE,
     Script(policy=CERTIFIES, route="HUMAN_INPUT", scope="EXPLICIT_ANSWER", yes_option="I agree"),
     "I agree", CERTIFIES),
    # not_current_or_former_employee: No ("No." in free text)
    ("Are you a current or former employee of Mock Co?", R, YES_NO,
     Script(policy=NOT_EMPLOYEE, route="HUMAN_INPUT", scope="EXPLICIT_ANSWER"), "No", NOT_EMPLOYEE),
    ("Have you previously worked at or with Mock Co or its affiliates?", S, YES_NO,
     Script(policy=NOT_EMPLOYEE), "No", NOT_EMPLOYEE),
    ("Have you interviewed with Mock Co in the past 2 years?", R, YES_NO,
     Script(policy=NOT_EMPLOYEE, route="HUMAN_INPUT", scope="EXPLICIT_ANSWER"), "No", NOT_EMPLOYEE),
    ("Have you been employed with Mock Co or any affiliates before?", TA, (),
     Script(policy=NOT_EMPLOYEE, route="HUMAN_INPUT", scope="EXPLICIT_ANSWER"), "No.", NOT_EMPLOYEE),
    ("Have you worked for Mock Co or any of its affiliates in the past?", R, YES_NO,
     Script(policy=NOT_EMPLOYEE), "No", NOT_EMPLOYEE),
    # sanctioned_locations: No
    ("Are you located in or a national of Cuba, Iran, North Korea, Syria, Crimea, or the so-called "
     "Donetsk or Luhansk People's Republics?", R, YES_NO,
     Script(policy=SANCTIONS, route="HUMAN_INPUT", scope="EXPLICIT_ANSWER"), "No", SANCTIONS),
    ("Are you ordinarily resident in, or a citizen of, a country subject to comprehensive U.S. "
     "sanctions (Cuba, Iran, North Korea, Syria or the Crimea region)?", S, YES_NO,
     Script(policy=SANCTIONS, route="HUMAN_INPUT", scope="EXPLICIT_ANSWER"), "No", SANCTIONS),
]


def test_thirty_live_wordings_cover_the_five_classes() -> None:
    assert len(LIVE) == 30
    assert {applied for *_, applied in LIVE} == set(ANSWER_POLICY_KEYS)


@pytest.mark.parametrize(("label", "control", "options", "script", "expected", "applied"), LIVE,
                         ids=[f"{i:02d}-{row[5]}" for i, row in enumerate(LIVE)])
def test_a_live_wording_takes_its_class_policy(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, control: ControlType,
    options: tuple[str, ...], script: Script, expected: str | bool, applied: str,
) -> None:
    candidate = with_policies(fictional_candidate)
    provider = PolicyJev({label[:40]: script})
    packet, ctx, resolver = resolve(provider, candidate, mock_job, typed_field(label, control, options))
    assert packet.is_complete, packet.missing_inputs
    [answer] = packet.answers
    assert rendered(answer) == expected
    if settled_by_years_facts(resolver):
        # With round 11 merged, the years facts settle this threshold first: the same answer.
        assert applied == THRESHOLDS and not traces(resolver)
        assert answer.provenance.source is AnswerSource.GENERATED_FROM_FACTS
        return
    assert answer.provenance.source is AnswerSource.SAVED_ANSWER
    assert answer.provenance.reference_ids == [policy_id(candidate, applied)]
    assert answer.provenance.reference_ids[0].startswith(f"answer_policy_{applied}_")  # names the key
    assert "user:simple-answers" in (answer.provenance.note or "")
    assert answer.semantic_type is ctx.form.field("q").semantic_type
    [trace] = traces(resolver)
    assert (trace["status"], trace["policy"], trace["policy_applied"]) == ("ANSWERED", script.policy, applied)
    assert trace["reference_ids"] == [policy_id(candidate, applied)]
    [request] = provider.policy_requests()
    assert request["state"]["prompt_version"] == POLICY_PROMPT_VERSION
    assert request["state"]["employer"] == mock_job.company
    assert set(request["questions"]["policy"]["criteria"]) == {*ANSWER_POLICY_KEYS, "NONE"}
    # Jev classifies the question only: the saved policies (questions and answers) are never sent.
    assert not any(question in json.dumps(request) for question in ANSWER_POLICY_QUESTIONS.values())
    assert answer.confidence <= 0.97


# --- a statement's whole wording is what an answer agrees to ------------------------------------


@pytest.mark.parametrize(("label", "control", "options", "script", "status"), [
    # An attestation under the employee class that also opts in to promotional offers.
    ("I confirm I have never worked for Mock Co and would like to receive promotional offers.",
     CB, (), Script(policy=NOT_EMPLOYEE, polarity="REVERSED", route="HUMAN_INPUT",
                    scope="EXPLICIT_ANSWER"), "OTHER_CONSENT"),
    # The same under the sanctions class with an SMS opt-in: the heuristics type the box a
    # consent, which only a certification of truth may answer.
    ("I confirm I am not located in Cuba, Iran, North Korea or Syria, and I agree to receive SMS "
     "updates.", CB, (), Script(policy=SANCTIONS, polarity="REVERSED", route="HUMAN_INPUT",
                                scope="EXPLICIT_ANSWER"), "NOT_ALLOWED"),
    # A custom sanctions question whose answer opts in to text messages.
    ("Are you located in or a national of Cuba, Iran, North Korea or Syria? Answering opts you in "
     "to text messages.", R, YES_NO, Script(policy=SANCTIONS, route="HUMAN_INPUT",
                                            scope="EXPLICIT_ANSWER"), "OTHER_CONSENT"),
    # A custom employee question whose answer agrees to arbitration.
    ("Have you worked for Mock Co before? By answering you agree to binding arbitration.", R, YES_NO,
     Script(policy=NOT_EMPLOYEE), "ADDED_OBLIGATION"),
    # A sanctions question that adds an ongoing screening.
    ("Are you located in or a national of Cuba, Iran, North Korea or Syria? You agree to ongoing "
     "sanctions screening.", R, YES_NO, Script(policy=SANCTIONS, route="HUMAN_INPUT",
                                              scope="EXPLICIT_ANSWER"), "ADDED_OBLIGATION"),
])
def test_a_status_statement_that_adds_a_consent_or_an_obligation_keeps_its_hold(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, control: ControlType,
    options: tuple[str, ...], script: Script, status: str,
) -> None:
    candidate = with_policies(fictional_candidate)
    provider = PolicyJev({label[:40]: script})
    packet, _, resolver = resolve(provider, candidate, mock_job, typed_field(label, control, options))
    assert packet.answers == []
    [missing] = packet.missing_inputs
    assert "answer policy" not in missing.prompt  # the hold it had, never a policy's
    [trace] = traces(resolver)
    assert trace["status"] == status


def test_an_experience_question_whose_topic_names_ai_tools_still_takes_the_claims_policy(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """The obligation words guard statements, checkboxes and status questions: an experience
    question about AI tools or a background is not an obligation."""
    label = "Do you have hands-on experience using generative AI tools such as ChatGPT for ad copy?"
    candidate = with_policies(fictional_candidate)
    provider = PolicyJev({label[:40]: Script(policy=CLAIMS)})
    packet, _, resolver = resolve(provider, candidate, mock_job, typed_field(label, R, YES_NO))
    [answer] = packet.answers
    assert rendered(answer) == "Yes"
    assert traces(resolver)[0]["status"] == "ANSWERED"


def test_a_certification_over_true_and_false_takes_true(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """True / False is a yes/no pair for a policy: the certification takes "True" without an
    option decision."""
    label = "I certify that the information I provided in this application is true and complete."
    candidate = with_policies(fictional_candidate)
    provider = PolicyJev({label[:40]: Script(policy=CERTIFIES, route="HUMAN_INPUT",
                                             scope="EXPLICIT_ANSWER")})
    packet, _, _ = resolve(provider, candidate, mock_job, typed_field(label, S, ("True", "False")))
    [answer] = packet.answers
    assert rendered(answer) == "True"
    [request] = provider.policy_requests()
    assert "yes_option" not in request["questions"] and "no_option" not in request["questions"]


# --- round 12b: with round 11 on the base ----------------------------------------------------------


EMPLOYED_Q = "Have you previously worked for Mock Co or any of its affiliates?"
WHEN_Q = "If yes, when did you work there and in what role?"
DSP_Q = "Have you managed Amazon DSP campaigns?"
DSP_DETAIL_Q = "If yes, please describe your experience with Amazon DSP."


def follow_up_traces(resolver: DynamicPacketResolver, field_id: str) -> list[dict[str, Any]]:
    return [t for t in traces(resolver, "conditional_follow_up") if t["field_id"] == field_id]


def test_a_required_follow_up_to_a_policy_no_does_not_apply(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """Round 11's rule reads the policy pass's answers too: "If yes, when …?" after a policy No
    is "N/A", citing the person's policy, as after a saved No."""
    candidate = with_policies(fictional_candidate)
    provider = PolicyJev({EMPLOYED_Q[:40]: explicit_policy(NOT_EMPLOYEE),
                          WHEN_Q[:40]: Script(route="HUMAN_INPUT", scope="EXPLICIT_ANSWER")})
    packet, _, resolver = resolve(provider, candidate, mock_job,
        typed_field(EMPLOYED_Q, R, YES_NO, field_id="employed"),
        typed_field(WHEN_Q, TA, field_id="when"))
    assert packet.is_complete, packet.missing_inputs
    assert rendered(packet.answer_for("employed")) == "No"
    when = packet.answer_for("when")
    assert rendered(when) == "N/A"
    assert when.provenance.reference_ids == [policy_id(candidate, NOT_EMPLOYEE)]
    # Read once before the policy pass (unanswered then) and once after it.
    assert [t["status"] for t in follow_up_traces(resolver, "when")] == [
        "GOVERNING_UNANSWERED", "NOT_APPLICABLE"]


def test_an_optional_follow_up_to_a_policy_no_is_left_blank(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = with_policies(fictional_candidate)
    provider = PolicyJev({EMPLOYED_Q[:40]: explicit_policy(NOT_EMPLOYEE),
                          WHEN_Q[:40]: Script(route="HUMAN_INPUT", scope="EXPLICIT_ANSWER")})
    packet, _, resolver = resolve(provider, candidate, mock_job,
        typed_field(EMPLOYED_Q, R, YES_NO, field_id="employed"),
        typed_field(WHEN_Q, TA, field_id="when", required=False))
    assert packet.is_complete and packet.answer_for("when") is None
    assert follow_up_traces(resolver, "when")[-1]["status"] == "LEFT_BLANK"


def test_a_follow_up_to_a_policy_yes_keeps_its_hold(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """After a policy Yes the details are the person's (or a writer's from facts): the
    follow-up is never answered by the policy or marked not applicable."""
    candidate = with_policies(fictional_candidate)
    provider = PolicyJev({DSP_Q[:30]: Script(policy=CLAIMS),
                          DSP_DETAIL_Q[:40]: Script(route="WRITER", narrative="prose")})
    packet, _, resolver = resolve(provider, candidate, mock_job,
        typed_field(DSP_Q, R, YES_NO, field_id="dsp"), typed_field(DSP_DETAIL_Q, TA, field_id="detail"))
    assert rendered(packet.answer_for("dsp")) == "Yes"
    assert packet.answer_for("detail") is None
    assert [m.field_id for m in packet.missing_inputs] == ["detail"]
    assert follow_up_traces(resolver, "detail")[-1]["status"] == "GOVERNING_YES"
    assert [t["field_id"] for t in traces(resolver)] == ["dsp"]  # never a policy candidate


def test_two_minimums_on_a_radio_reach_the_policy_and_wait_for_the_person(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """Round 11's years screener reads "including 2 in paid social" as a second minimum and
    leaves the question to Jev; the threshold policy cannot read one minimum either, so the
    field waits with a prompt naming the policy (never Yes from the total)."""
    label = "Do you have 5+ years of experience, including 2 in paid social?"
    candidate = with_policies(fictional_candidate)
    provider = PolicyJev({label[:40]: Script(policy=THRESHOLDS)})
    packet, _, resolver = resolve(provider, candidate, mock_job, typed_field(label, R, YES_NO))
    assert packet.answers == []
    [missing] = packet.missing_inputs
    assert missing.prompt.startswith(f"Your answer policy {THRESHOLDS} applies to")
    assert not settled_by_years_facts(resolver)
    [trace] = traces(resolver)
    assert trace["status"] == "YEARS_UNREADABLE"
