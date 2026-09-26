"""Round 15: the holds of the first mass slice that the person's own answers settle.

Batch ``mass-20260925-a`` held 12 of 25 applications. Many of the holds were questions the
profile or the simple answers already answer: sponsorship radios, a salary with its
currency, the current company and title, English fluency, pronouns, time zone, middle name,
platform multi-selects and years buckets. Each test uses the live wording the brief quotes
on a fictional profile: Avery Example, moved to Austin, TX, with answers imported through
the simple-answers map, and Mock Co jobs. Fields are typed by the browser heuristics and
Jev's annotation. Nothing is submitted.
"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from interviewmaxxing_browser.ai import (
    AIFormRouter,
    BoundedDecisions,
    CallBudget,
    DynamicPacketResolver,
)
from interviewmaxxing_browser.semantics import classify as classify_semantics
from interviewmaxxing_candidate.simple_answers import SimpleAnswers
from interviewmaxxing_core import (
    AnswerScope,
    AnswerSource,
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationState,
    CandidateFact,
    CandidateProfile,
    ChoiceValue,
    ControlType,
    Experience,
    FieldOption,
    JobRecord,
    MultiChoiceValue,
    PacketContext,
    PostalAddress,
    SavedAnswer,
    SemanticType,
    TextValue,
)
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
R, S, T, CB = ControlType.RADIO, ControlType.SELECT, ControlType.TEXT, ControlType.CHECKBOX_GROUP
YES_NO = ("Yes", "No")


# --- a scripted Jev --------------------------------------------------------------------------------


@dataclass
class Script:
    route: str = "HUMAN_INPUT"
    scope: str = "EXPLICIT_ANSWER"
    semantic: str | None = None
    picks: dict[str, str] = field(default_factory=dict)
    """Decision name -> a fragment of the option label Jev picks (at 0.99)."""


class Jev:
    """Classifies each field from its script and answers every other decision with its
    no-answer choice unless the script names a pick; every noul is 0."""

    def __init__(self, scripts: dict[str, Script] | None = None) -> None:
        self.scripts = scripts or {}
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def script(self, text: str) -> Script:
        found = [s for fragment, s in self.scripts.items() if fragment in text]
        return found[0] if found else Script()

    def asked(self, name: str) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests if name in r["questions"]]

    def decisions_other_than_classification(self) -> list[str]:
        with self.lock:
            return [name for r in self.requests for name in r["questions"]
                    if not (name[0] in "rnusd" and name[1:].isdigit())]

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        with self.lock:
            self.requests.append(request)
        state = request["state"]
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.0}
                continue
            criteria = list(question["criteria"])
            choice, probability = self._choice(name, state, criteria)
            rest = (1 - probability) / (len(criteria) - 1)
            answers[name] = {"type": "choice", "choice": choice, "confidence": 0.97 if probability < 1 else 1.0,
                             "probabilities": {k: probability if k == choice else rest for k in criteria}}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())

    def _choice(self, name: str, state: dict[str, Any], criteria: list[str]) -> tuple[str, float]:
        if name[0] in "rnusd" and name[1:].isdigit():
            data = state["fields"][f"f{name[1:]}"]
            script = self.script(data["question"])
            if name[0] == "s":
                semantic = script.semantic or {"TEXTAREA": "CUSTOM_LONG_TEXT", "TEXT": "CUSTOM_TEXT"}.get(
                    data["control"], "CUSTOM_BOOLEAN")
                return semantic, 1.0
            return {"r": script.route, "n": "literal", "u": script.scope,
                    "d": "APPLICATION_ATTACHMENT"}[name[0]], 1.0
        text = state.get("question") if isinstance(state.get("question"), str) else ""
        script = self.script(str(text))
        if name in script.picks:
            options: dict[str, str] = state.get("options", {})
            key = next(k for k, label in options.items() if script.picks[name] in label)
            return key, 0.99
        return next(c for c in ("NONE", "UNKNOWN", "hold", criteria[0]) if c in criteria), 1.0


def decisions(provider: Jev) -> BoundedDecisions:
    return BoundedDecisions(JevClient(ApiKey("synthetic-key", source="test"),
        transport=provider, max_attempts=1), CallBudget())


# --- the fictional profile and jobs ----------------------------------------------------------------


def person(base: CandidateProfile, **answers: Any) -> CandidateProfile:
    """Avery Example in Austin, TX, with ``answers`` imported through the simple-answers map
    (null keys add nothing)."""
    identity = base.identity.model_copy(update={"address": PostalAddress(
        city="Austin", region="TX", postal_code="78701", country="United States")})
    data = SimpleAnswers.from_identity(identity).model_dump()
    data.update(answers)
    imported = SimpleAnswers.model_validate(data).saved_answer_updates(confirmed_at=NOW)
    return base.model_copy(update={"identity": identity, "saved_answers": [*base.saved_answers, *imported]})


def job_at(base: JobRecord, location: str | None = "Remote - US") -> JobRecord:
    return base.model_copy(update={"location": location})


def typed_field(label: str, control: ControlType, options: tuple[str, ...] = (), *,
                field_id: str = "q", required: bool = True, help_text: str | None = None) -> ApplicationField:
    semantic = classify_semantics(label=label, control_type=control)
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label, semantic_type=semantic,
        control_type=control, required=required, help_text=help_text,
        options=[FieldOption(value=f"v{i}", label=o) for i, o in enumerate(options)] if options else None)


def resolve(provider: Jev, candidate: CandidateProfile, job: JobRecord,
            *fields: ApplicationField) -> tuple[Any, PacketContext, DynamicPacketResolver]:
    router = AIFormRouter(decisions(provider))
    annotated = router.annotate(ApplicationForm(url="https://example.test/apply", fields=list(fields)),
                                document_id="mass-slice")
    app = Application(id="app-mass", request_id="request-mass", job_id=job.id,
        candidate_id=candidate.id, state=ApplicationState.INSPECTING, version=2,
        created_at="2026-09-25T00:00:00Z", updated_at="2026-09-25T00:00:00Z")
    ctx = PacketContext(application=app, job=job, form=annotated, candidate=candidate)
    resolver = DynamicPacketResolver(router.decisions, router=router)
    packet = asyncio.run(resolver.resolve(ctx))
    assert ctx.problems(packet) == []
    return packet, ctx, resolver


def traces(resolver: DynamicPacketResolver, stage: str) -> list[dict[str, Any]]:
    return [t for t in resolver.narrative_traces if t["stage"] == stage]


def label_of(answer: Any) -> str | list[str]:
    if isinstance(answer.value, MultiChoiceValue):
        return [c.label for c in answer.value.choices]
    if isinstance(answer.value, TextValue):
        return answer.value.text
    assert isinstance(answer.value, ChoiceValue)
    return answer.value.label


def answer_for(packet: Any, field_id: str = "q") -> Any:
    return next((a for a in packet.answers if a.field_id == field_id), None)


def saved_id(candidate: CandidateProfile, question: str) -> str:
    return next(a.id for a in candidate.saved_answers if a.question == question)


# --- sponsorship ------------------------------------------------------------------------------------

GREENHOUSE_SPONSOR = ("Will you now or in the future require sponsorship for employment visa status "
                      "(e.g., H-1B visa status)?")
PLAIN_SPONSOR = "Will you require visa sponsorship to work for Mock Co?"
STATEMENT_SPONSOR = "Do you require visa sponsorship?"
STATEMENT_OPTIONS = ("N/A - I am based in the United States", "I will require sponsorship",
                     "I do not require sponsorship")
REQUIRES_SPONSORSHIP = "Will you now or in the future require visa sponsorship for employment?"


@pytest.mark.parametrize(("label", "options", "expected"), [
    (GREENHOUSE_SPONSOR, YES_NO, "No"),
    (PLAIN_SPONSOR, YES_NO, "No"),
    (STATEMENT_SPONSOR, STATEMENT_OPTIONS, "I do not require sponsorship"),
])
def test_a_sponsorship_radio_takes_the_persons_own_answer_without_a_call(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, options: tuple[str, ...],
    expected: str,
) -> None:
    candidate = person(fictional_candidate, requires_visa_sponsorship="No")
    provider = Jev()
    packet, ctx, resolver = resolve(provider, candidate, job_at(mock_job, None), typed_field(label, R, options))
    assert ctx.form.field("q").semantic_type is SemanticType.SPONSORSHIP
    answer = answer_for(packet)
    assert answer is not None and label_of(answer) == expected
    assert answer.provenance.source is AnswerSource.SAVED_ANSWER
    assert answer.provenance.reference_ids == [saved_id(candidate, REQUIRES_SPONSORSHIP)]
    [trace] = traces(resolver, "sponsorship_answer")
    assert (trace["via"], trace["status"], trace["choice"]) == ("stated", "ANSWERED", "no")
    assert not provider.decisions_other_than_classification()


def test_the_persons_yes_takes_the_statement_that_they_will_need_sponsorship(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = person(fictional_candidate, requires_visa_sponsorship="Yes")
    packet, _, _ = resolve(Jev(), candidate, job_at(mock_job), typed_field(STATEMENT_SPONSOR, R, STATEMENT_OPTIONS))
    assert label_of(answer_for(packet)) == "I will require sponsorship"


@pytest.mark.parametrize(("label", "location"), [
    ("Will you require visa sponsorship to work in Canada?", "Remote - US"),
    ("Will you require visa sponsorship to work in London?", "Remote - US"),
    ("Are you able to work without visa sponsorship?", "Remote - US"),
    ("Are you authorized to work and will you require visa sponsorship?", "Remote - US"),
    (STATEMENT_SPONSOR, "London, United Kingdom"),
    (STATEMENT_SPONSOR, "Sydney, Australia"),
])
def test_a_sponsorship_question_about_another_country_or_subject_is_not_the_stated_answers(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, location: str,
) -> None:
    candidate = person(fictional_candidate, requires_visa_sponsorship="No")
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, location),
                                  typed_field(label, R, STATEMENT_OPTIONS if label == STATEMENT_SPONSOR else YES_NO))
    assert answer_for(packet) is None
    assert not [t for t in traces(resolver, "sponsorship_answer") if t["status"] == "ANSWERED"]


def test_the_exact_saved_answer_is_placed_on_statement_options(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # The person's own answer to exactly this wording, "No", fits no option by its text; its
    # polarity picks "I do not require sponsorship" (before round 15 it held NOT_PLACED).
    candidate = person(fictional_candidate, requires_visa_sponsorship="No")
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, "London, United Kingdom"),
                                  typed_field(REQUIRES_SPONSORSHIP, R, STATEMENT_OPTIONS))
    assert label_of(answer_for(packet)) == "I do not require sponsorship"
    [trace] = traces(resolver, "sponsorship_answer")
    assert trace["via"] == "exact"


@pytest.mark.parametrize(("label", "options", "expected"), [
    (GREENHOUSE_SPONSOR, YES_NO, "No"),
    (STATEMENT_SPONSOR, STATEMENT_OPTIONS, "I do not require sponsorship"),
])
def test_a_citizen_status_settles_a_generic_sponsorship_question_by_the_table(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, options: tuple[str, ...],
    expected: str,
) -> None:
    candidate = person(fictional_candidate, work_authorization_status="us_citizen")
    provider = Jev()
    packet, _, resolver = resolve(provider, candidate, job_at(mock_job, None), typed_field(label, R, options))
    assert label_of(answer_for(packet)) == expected
    [trace] = traces(resolver, "status_derivation")
    assert (trace["via"], trace["status"]) == ("table", "ANSWERED")
    assert not provider.asked("status")


def test_a_status_that_contradicts_the_sponsorship_answer_is_never_placed(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # The importer rejects the pair, so it is built here as an older import would have left it.
    candidate = person(fictional_candidate, work_authorization_status="us_citizen")
    older = SavedAnswer(id="sa.old_sponsorship", scope=AnswerScope.GLOBAL, semantic_type=SemanticType.SPONSORSHIP,
                        question=REQUIRES_SPONSORSHIP, value="Yes", confirmed_at=NOW)
    candidate = candidate.model_copy(update={"saved_answers": [*candidate.saved_answers, older]})
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job), typed_field(STATEMENT_SPONSOR, R, STATEMENT_OPTIONS))
    assert answer_for(packet) is None
    # With a stated status the derivation decides a reworded question, and it holds on the
    # contradiction; the canonical answer is never placed over it.
    assert not traces(resolver, "sponsorship_answer")
    assert [t["status"] for t in traces(resolver, "status_derivation")] == ["CONTRADICTS_STATED"]


def test_an_exact_answer_that_contradicts_the_status_is_never_placed(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = person(fictional_candidate, work_authorization_status="us_citizen")
    older = SavedAnswer(id="sa.old_sponsorship", scope=AnswerScope.GLOBAL, semantic_type=SemanticType.SPONSORSHIP,
                        question=REQUIRES_SPONSORSHIP, value="Yes", confirmed_at=NOW)
    candidate = candidate.model_copy(update={"saved_answers": [*candidate.saved_answers, older]})
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job),
                                  typed_field(REQUIRES_SPONSORSHIP, R, STATEMENT_OPTIONS))
    assert answer_for(packet) is None
    assert [t["status"] for t in traces(resolver, "sponsorship_answer")] == ["CONTRADICTS_STATUS"]


# --- the current role --------------------------------------------------------------------------------

COPY = Script(route="COPY_KNOWN", scope="APPLICANT_CURRENT")
COMPANY_LABEL = "Current/Most Recent Company Name"
TITLE_LABEL = "Current/Most Recent Job Title"


def verified(fid: str, key: str, value: Any, source: str = "resume") -> CandidateFact:
    return CandidateFact(id=fid, key=key, value=value, source=source,
                         verification={"status": "VERIFIED", "method": "USER_CONFIRMED",
                                       "verified_at": "2026-09-01T12:00:00Z"})


def with_current_role(base: CandidateProfile, *, current: bool = True, extra_current: bool = False,
                      link: bool = True) -> CandidateProfile:
    """The profile stating its current role only in ``experience`` (no current_company or
    current_title fact), as the live profile does."""
    employment = verified("fact.employment_now", "employment",
                          "Performance Marketing Consultant at Fictional Consulting (2023-present)")
    earlier = verified("fact.employment_before", "employment", "Paid Media Lead at Mock Agency (2019-2023)")
    roles = [Experience(id="exp.now", company="Fictional Consulting", title="Performance Marketing Consultant",
                        start="2023-01", current=current, fact_ids=[employment.id] if link else []),
             Experience(id="exp.before", company="Mock Agency", title="Paid Media Lead", start="2019-01",
                        end="2023-01", current=extra_current, fact_ids=[earlier.id])]
    facts = [f for f in base.facts if f.key not in ("current_company", "current_title")]
    return base.model_copy(update={"facts": [*facts, employment, earlier], "experience": roles})


@pytest.mark.parametrize(("label", "expected"), [(COMPANY_LABEL, "Fictional Consulting"),
                                                 (TITLE_LABEL, "Performance Marketing Consultant")])
def test_the_current_role_answers_the_current_or_most_recent_company_and_title(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, expected: str,
) -> None:
    candidate = with_current_role(person(fictional_candidate))
    packet, _, _ = resolve(Jev({label: COPY}), candidate, job_at(mock_job), typed_field(label, T))
    answer = answer_for(packet)
    assert answer is not None and label_of(answer) == expected
    assert answer.provenance.source is AnswerSource.CANDIDATE_FACT
    assert answer.provenance.reference_ids == ["fact.employment_now"]
    assert "current role (exp.now)" in (answer.provenance.note or "")


@pytest.mark.parametrize("shape", ["none_current", "two_current", "no_verified_link"])
def test_the_current_role_needs_exactly_one_current_role_with_a_verified_fact(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, shape: str,
) -> None:
    candidate = with_current_role(person(fictional_candidate), current=shape != "none_current",
                                  extra_current=shape == "two_current", link=shape != "no_verified_link")
    packet, _, _ = resolve(Jev({COMPANY_LABEL: COPY}), candidate, job_at(mock_job), typed_field(COMPANY_LABEL, T))
    assert answer_for(packet) is None


def test_the_current_company_fact_still_comes_first(fictional_candidate: CandidateProfile, mock_job: JobRecord) -> None:
    candidate = with_current_role(person(fictional_candidate))
    fact = verified("fact.current_company", "current_company", "Fictional Consulting LLC", source="user")
    candidate = candidate.model_copy(update={"facts": [*candidate.facts, fact]})
    packet, _, _ = resolve(Jev({COMPANY_LABEL: COPY}), candidate, job_at(mock_job), typed_field(COMPANY_LABEL, T))
    answer = answer_for(packet)
    assert label_of(answer) == "Fictional Consulting LLC" and answer.provenance.reference_ids == ["fact.current_company"]


# --- pronouns ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("options", "expected"), [
    (("He/him/his", "She/her/hers", "They/them/theirs", "Other"), ["He/him/his"]),
    (("He / Him", "She / Her", "They / Them", "Prefer not to say"), ["He / Him"]),
    (("He/Him", "She/Her", "They/Them"), ["He/Him"]),
])
def test_the_saved_pronouns_take_the_option_naming_them(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, options: tuple[str, ...], expected: list[str],
) -> None:
    candidate = person(fictional_candidate, pronouns="he/him")
    field = typed_field("Pronouns", CB, options)
    packet, ctx, _ = resolve(Jev(), candidate, job_at(mock_job), field)
    assert ctx.form.field("q").semantic_type is SemanticType.PRONOUNS
    answer = answer_for(packet)
    assert answer is not None and label_of(answer) == expected
    assert answer.provenance.reference_ids == [saved_id(candidate, "What pronouns do you use?")]


@pytest.mark.parametrize(("saved", "options"), [
    ("he/they", ("He/Him", "They/Them", "She/Her")),
    ("he/him", ("He/They", "She/Her", "They/Them")),
    ("he/him", ("He/him/his", "He/Him/His", "She/Her")),
])
def test_pronouns_no_single_option_names_are_not_guessed(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, saved: str, options: tuple[str, ...],
) -> None:
    candidate = person(fictional_candidate, pronouns=saved)
    packet, _, _ = resolve(Jev(), candidate, job_at(mock_job), typed_field("Pronouns", CB, options))
    assert answer_for(packet) is None  # Jev's option mapping (NONE here) decides, as before


# --- English -----------------------------------------------------------------------------------------

ENGLISH = "Are you able to speak, read, and write English fluently?"


@pytest.mark.parametrize(("level", "expected"), [
    ("Native", "Yes"), ("Fluent", "Yes"), ("Native or bilingual proficiency", "Yes"), ("C2", "Yes"),
    ("Basic", "No"), ("Limited working proficiency", "No"),
])
def test_english_fluency_follows_the_stated_level(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, level: str, expected: str,
) -> None:
    candidate = person(fictional_candidate, english_proficiency=level)
    packet, _, _ = resolve(Jev(), candidate, job_at(mock_job), typed_field(ENGLISH, R, YES_NO))
    answer = answer_for(packet)
    assert answer is not None and label_of(answer) == expected
    assert answer.provenance.reference_ids == [saved_id(candidate, "What is your level of proficiency in English?")]


@pytest.mark.parametrize(("label", "level"), [
    (ENGLISH, "Professional working proficiency"),
    (ENGLISH, "Intermediate"),
    ("Do you speak any languages other than English?", "Native"),
    ("Have you read our English-language privacy policy?", "Native"),
])
def test_english_fluency_is_not_guessed(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, level: str,
) -> None:
    candidate = person(fictional_candidate, english_proficiency=level)
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job), typed_field(label, R, YES_NO))
    assert answer_for(packet) is None
    assert not [t for t in traces(resolver, "english_fluency") if t["status"] == "ANSWERED"]


# --- salary with its currency ------------------------------------------------------------------------

TARGET_SALARY = "What is your target annual salary? Please include the currency."


@pytest.mark.parametrize(("saved", "expected"), [
    ("135,000 per year", "$135,000 USD per year"),
    ("USD 135,000 per year", "$135,000 USD per year"),
    ("$135,000 per year", "$135,000 USD per year"),
    ("11,250 per month", "$135,000 USD per year"),
])
def test_a_salary_asked_with_its_currency_states_it(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, saved: str, expected: str,
) -> None:
    candidate = person(fictional_candidate, desired_salary=saved)
    field = typed_field(TARGET_SALARY, T)
    packet, ctx, resolver = resolve(Jev(), candidate, job_at(mock_job), field)
    assert ctx.form.field("q").semantic_type is SemanticType.SALARY_EXPECTATION
    answer = answer_for(packet)
    assert answer is not None and label_of(answer) == expected
    assert answer.provenance.source is AnswerSource.SAVED_ANSWER
    [trace] = traces(resolver, "salary_derivation")
    assert (trace["status"], trace["currency"], trace["wording"]) == ("ANSWERED", "USD", "PLAIN")


def test_a_salary_without_a_currency_for_a_person_abroad_holds(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = person(fictional_candidate, desired_salary="135,000 per year")
    identity = candidate.identity.model_copy(update={"address": PostalAddress(city="Tbilisi", country="Georgia")})
    candidate = candidate.model_copy(update={"identity": identity})
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job), typed_field(TARGET_SALARY, T))
    assert answer_for(packet) is None
    assert [t["status"] for t in traces(resolver, "salary_derivation")] == ["CURRENCY_UNSTATED"]


@pytest.mark.parametrize("label", ["Desired salary currency", "Which currency is your salary in?"])
def test_a_question_for_the_currency_itself_is_still_not_derived(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
) -> None:
    candidate = person(fictional_candidate, desired_salary="USD 135,000 per year")
    _, _, resolver = resolve(Jev(), candidate, job_at(mock_job), typed_field(label, T))
    assert [t["status"] for t in traces(resolver, "salary_derivation")] in ([], ["NOT_DERIVED"])


# --- middle name -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("label", "help_text"), [
    ("Middle Name", "write N/A if none"),
    ("Middle Name — write N/A if none", None),
])
@pytest.mark.parametrize(("saved", "expected"), [("none", "N/A"), ("N/A", "N/A"), ("Jordan", "Jordan")])
def test_a_middle_name_question_takes_the_saved_middle_name(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, help_text: str | None,
    saved: str, expected: str,
) -> None:
    candidate = person(fictional_candidate, middle_name=saved)
    packet, _, _ = resolve(Jev(), candidate, job_at(mock_job), typed_field(label, T, help_text=help_text))
    answer = answer_for(packet)
    assert answer is not None and label_of(answer) == expected
    assert answer.provenance.reference_ids == [saved_id(candidate, "What is your middle name?")]


def test_without_a_saved_middle_name_the_question_waits(fictional_candidate: CandidateProfile, mock_job: JobRecord) -> None:
    packet, _, _ = resolve(Jev(), person(fictional_candidate), job_at(mock_job),
                           typed_field("Middle Name", T, help_text="write N/A if none"))
    assert answer_for(packet) is None


def test_the_importer_stores_none_as_na(fictional_candidate: CandidateProfile) -> None:
    for word in ("none", "None", "no", "n/a", "-"):
        assert SimpleAnswers.model_validate(
            SimpleAnswers.from_identity(fictional_candidate.identity).model_dump() | {"middle_name": word}
        ).middle_name == "N/A"


# --- time zone ---------------------------------------------------------------------------------------

TIME_ZONE = "What is your Time Zone?"
ZONES = ("Eastern", "Central", "Mountain", "Pacific")
ZONES_LONG = ("Eastern Time (ET)", "Central Time (CT)", "Mountain Time (MT)", "Pacific Time (PT)")


@pytest.mark.parametrize(("saved", "options", "expected"), [
    ("Central", ZONES, "Central"),
    ("Central", ZONES_LONG, "Central Time (CT)"),
    ("US Central (CST)", ZONES_LONG, "Central Time (CT)"),
    ("America/Chicago", ZONES, "Central"),
])
def test_the_saved_time_zone_takes_the_option_naming_it(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, saved: str, options: tuple[str, ...], expected: str,
) -> None:
    candidate = person(fictional_candidate, time_zone=saved)
    packet, _, _ = resolve(Jev(), candidate, job_at(mock_job), typed_field(TIME_ZONE, R, options))
    answer = answer_for(packet)
    assert answer is not None and label_of(answer) == expected
    assert answer.provenance.source is AnswerSource.SAVED_ANSWER
    assert answer.provenance.reference_ids == [saved_id(candidate, "What is your time zone?")]


@pytest.mark.parametrize(("city", "region", "expected"), [
    ("Austin", "TX", "Central Time (CT)"), ("El Paso", "TX", "Mountain Time (MT)"),
    ("Denver", "CO", "Mountain Time (MT)"), ("Seattle", "WA", "Pacific Time (PT)"),
    ("Knoxville", "TN", "Eastern Time (ET)"), ("Nashville", "TN", "Central Time (CT)"),
])
def test_an_identity_typed_time_zone_question_reads_the_verified_address(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, city: str, region: str, expected: str,
) -> None:
    candidate = person(fictional_candidate)
    identity = candidate.identity.model_copy(update={"address": PostalAddress(city=city, region=region,
                                                                              country="United States")})
    candidate = candidate.model_copy(update={"identity": identity})
    # An address-derived answer passes the full-form route gate like any identity copy: the
    # classifier reads the question as the applicant's own current datum.
    located = Script(route="COPY_KNOWN", scope="APPLICANT_CURRENT", semantic="LOCATION")
    packet, ctx, _ = resolve(Jev({TIME_ZONE: located}), candidate, job_at(mock_job),
                             typed_field(TIME_ZONE, R, ZONES_LONG))
    assert ctx.form.field("q").semantic_type is SemanticType.LOCATION
    answer = answer_for(packet)
    assert answer is not None and label_of(answer) == expected
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY


def test_a_custom_time_zone_select_without_a_saved_zone_waits(fictional_candidate: CandidateProfile,
                                                              mock_job: JobRecord) -> None:
    # A custom select cannot carry an address-derived answer (core's provenance rule): the
    # person states time_zone, or the classifier types the question LOCATION (WP10).
    packet, ctx, resolver = resolve(Jev(), person(fictional_candidate), job_at(mock_job),
                                    typed_field(TIME_ZONE, R, ZONES))
    assert ctx.form.field("q").semantic_type not in (SemanticType.LOCATION,)
    assert answer_for(packet) is None
    assert [t["status"] for t in traces(resolver, "time_zone")] == ["NO_TIME_ZONE"]


@pytest.mark.parametrize("label", ["Which time zones are you available to work in?",
                                   "Are you comfortable working Pacific Time hours?"])
def test_a_working_hours_time_zone_question_is_not_the_persons_zone(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
) -> None:
    candidate = person(fictional_candidate, time_zone="Central")
    _, _, resolver = resolve(Jev(), candidate, job_at(mock_job), typed_field(label, R, ZONES))
    assert not traces(resolver, "time_zone")


# --- multi-selects and scales from the person's facts ----------------------------------------------

STATED = "user:simple-answers"


def with_years(base: CandidateProfile, *extra: CandidateFact) -> CandidateProfile:
    """Avery Example with stated years: paid media, Meta, Google and LinkedIn 7 each, SEO 6."""
    facts = [verified(f"user_years_{area}", f"years_experience.{area}", years, source=STATED)
             for area, years in (("paid_media", 7), ("meta_ads", 7), ("google_ads", 7), ("linkedin_ads", 7),
                                 ("seo", 6))]
    kept = [f for f in base.facts if not f.key.startswith("years_")]
    return base.model_copy(update={"facts": [*kept, *facts, *extra]})


PLATFORMS_Q = "Have you managed your own ads in the following platforms?"
PLATFORM_OPTIONS = ("Facebook/Instagram", "Google", "LinkedIn", "TikTok", "Twitter", "Amazon", "Bing")


def test_a_platform_select_all_takes_the_stated_platforms_and_leaves_the_rest(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = with_years(person(fictional_candidate))
    provider = Jev({PLATFORMS_Q: Script(route="COPY_KNOWN", scope="HISTORICAL_OR_CONTEXTUAL")})
    packet, _, resolver = resolve(provider, candidate, job_at(mock_job), typed_field(PLATFORMS_Q, CB, PLATFORM_OPTIONS))
    answer = answer_for(packet)
    assert answer is not None and label_of(answer) == ["Facebook/Instagram", "Google", "LinkedIn"]
    assert answer.provenance.source is AnswerSource.GENERATED_FROM_FACTS
    assert set(answer.provenance.reference_ids) == {"user_years_meta_ads", "user_years_google_ads",
                                                    "user_years_linkedin_ads"}
    [trace] = [t for t in traces(resolver, "fact_screener") if t.get("kind") == "multi"]
    assert trace["status"] == "ANSWERED" and trace["selected"] == ["o0", "o1", "o2"]
    # Round 11: only the options no fact names go to Jev (here NONE for each: left unselected).
    [request] = provider.asked("fact_select")
    assert {name for name in request["questions"] if name.startswith("source_")} == {
        "source_o3", "source_o4", "source_o5", "source_o6"}


class FailingSelect(Jev):
    """Fails the select-all decision, as a provider refusal or timeout would."""

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        if "fact_select" in json.loads(body)["questions"]:
            return HttpResponse(500, {}, b"{}")
        return super().__call__(url, headers, body, timeout)


def test_a_failed_decision_about_the_other_platforms_keeps_the_stated_ones(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = with_years(person(fictional_candidate))
    provider = FailingSelect({PLATFORMS_Q: Script(route="COPY_KNOWN", scope="HISTORICAL_OR_CONTEXTUAL")})
    packet, _, resolver = resolve(provider, candidate, job_at(mock_job), typed_field(PLATFORMS_Q, CB, PLATFORM_OPTIONS))
    answer = answer_for(packet)
    assert answer is not None and label_of(answer) == ["Facebook/Instagram", "Google", "LinkedIn"]
    [trace] = [t for t in traces(resolver, "fact_screener") if t.get("kind") == "multi"]
    assert trace["status"] == "ANSWERED" and "decision_error" in trace
    assert set(trace["left_unselected"]) == {"o3", "o4", "o5", "o6"}


def test_a_platform_select_all_with_no_stated_platform_holds(fictional_candidate: CandidateProfile,
                                                              mock_job: JobRecord) -> None:
    candidate = with_years(person(fictional_candidate))
    options = ("TikTok", "Twitter", "Amazon", "Bing")
    packet, _, _ = resolve(Jev({PLATFORMS_Q: Script(route="COPY_KNOWN", scope="HISTORICAL_OR_CONTEXTUAL")}),
                           candidate, job_at(mock_job), typed_field(PLATFORMS_Q, CB, options))
    assert answer_for(packet) is None


AI_USE = "Which of the following best describes how you use AI in your current role? (Select all that apply)"
AI_OPTIONS = ("I use AI chat assistants for drafting and research",
              "I have built and documented AI workflows or automations",
              "I have written code with the help of AI",
              "I don't use AI in my work")


class FactPicker(Jev):
    """Answers the select-all decision SUPPORTED and each option's source with the fact whose
    text contains the option's keyword (or NONE)."""

    KEYWORDS: ClassVar[dict[str, str]] = {"workflows": "workflow", "code": "code"}

    def _choice(self, name: str, state: dict[str, Any], criteria: list[str]) -> tuple[str, float]:
        if name == "fact_select":
            return "SUPPORTED", 1.0
        if name.startswith("source_"):
            option = state["options"][name.removeprefix("source_")]
            keyword = next((word for key, word in self.KEYWORDS.items() if key in option), None)
            fact = next((key for key, item in state["facts"].items()
                         if keyword and keyword in str(item["value"]).casefold()), None)
            return (fact, 1.0) if fact in criteria else ("NONE", 1.0)
        return super()._choice(name, state, criteria)


def test_the_ai_use_select_all_reaches_the_fact_screener(fictional_candidate: CandidateProfile,
                                                         mock_job: JobRecord) -> None:
    facts = (verified("fact.ai_workflows", "experience",
                      "Built and documented AI workflows that automated weekly paid media reporting."),
             verified("fact.ai_code", "experience", "Wrote Python code with AI assistants to pull ad platform data."))
    candidate = with_years(person(fictional_candidate), *facts)
    provider = FactPicker({AI_USE: Script(route="HUMAN_INPUT", scope="UNCLEAR")})
    packet, _, _ = resolve(provider, candidate, job_at(mock_job), typed_field(AI_USE, CB, AI_OPTIONS))
    assert provider.asked("fact_select")  # before round 15 no experience wording admitted it
    answer = answer_for(packet)
    assert answer is not None and label_of(answer) == [AI_OPTIONS[1], AI_OPTIONS[2]]
    assert set(answer.provenance.reference_ids) == {"fact.ai_workflows", "fact.ai_code"}


NATIONAL_BRAND = "How many years of experience do you have in planning / buying media for a national brand"


@pytest.mark.parametrize(("years", "expected"), [(7, "5-7"), (4, "0-4"), (9, "8+")])
def test_a_years_bucket_takes_the_range_of_the_stated_paid_media_years(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, years: int, expected: str,
) -> None:
    candidate = with_years(person(fictional_candidate))
    candidate = candidate.model_copy(update={"facts": [
        f.model_copy(update={"value": years}) if f.id == "user_years_paid_media" else f for f in candidate.facts]})
    provider = Jev({NATIONAL_BRAND: Script(route="COPY_KNOWN", scope="HISTORICAL_OR_CONTEXTUAL")})
    packet, ctx, resolver = resolve(provider, candidate, job_at(mock_job),
                                    typed_field(NATIONAL_BRAND, R, ("0-4", "5-7", "8+")))
    assert ctx.form.field("q").semantic_type is SemanticType.YEARS_EXPERIENCE
    answer = answer_for(packet)
    assert answer is not None and label_of(answer) == expected
    assert answer.provenance.reference_ids == ["user_years_paid_media"]
    assert not provider.asked("fact_choice")
    [trace] = [t for t in traces(resolver, "fact_screener") if t.get("kind") == "years_bucket"]
    assert trace["status"] == "ANSWERED"


CTV = "How would you rate your proficiency in CTV buying?"
CTV_SCALE = ("No experience", "Beginner", "Intermediate", "Advanced", "Expert")


def test_a_ctv_scale_takes_no_experience_when_no_fact_names_ctv(fictional_candidate: CandidateProfile,
                                                                 mock_job: JobRecord) -> None:
    candidate = with_years(person(fictional_candidate))
    provider = Jev({CTV: Script(route="COPY_KNOWN", scope="HISTORICAL_OR_CONTEXTUAL")})
    packet, _, resolver = resolve(provider, candidate, job_at(mock_job), typed_field(CTV, R, CTV_SCALE))
    answer = answer_for(packet)
    assert answer is not None and label_of(answer) == "No experience"
    assert answer.provenance.source is AnswerSource.GENERATED_FROM_FACTS
    assert "user_years_meta_ads" in answer.provenance.reference_ids
    assert not provider.asked("fact_choice")
    [trace] = [t for t in traces(resolver, "fact_screener") if t.get("kind") == "channel_scale"]
    assert (trace["status"], trace["channel"]) == ("ANSWERED", "ctv")


@pytest.mark.parametrize("shape", ["ctv_stated", "no_none_option", "no_platform_years", "not_a_channel"])
def test_a_proficiency_scale_is_never_claimed_down_without_cause(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, shape: str,
) -> None:
    extra = ((verified("fact.ctv", "experience", "Bought connected TV (CTV) inventory through a DSP."),)
             if shape == "ctv_stated" else ())
    candidate = with_years(person(fictional_candidate), *extra)
    if shape == "no_platform_years":
        candidate = candidate.model_copy(update={"facts": [f for f in candidate.facts if not f.key.startswith("years_")]})
    label = "How would you rate your proficiency in Excel?" if shape == "not_a_channel" else CTV
    options = CTV_SCALE[1:] if shape == "no_none_option" else CTV_SCALE
    packet, _, resolver = resolve(Jev({label: Script(route="COPY_KNOWN", scope="HISTORICAL_OR_CONTEXTUAL")}),
                                  candidate, job_at(mock_job), typed_field(label, R, options))
    answer = answer_for(packet)
    assert answer is None or label_of(answer) != "No experience"
    assert not [t for t in traces(resolver, "fact_screener")
                if t.get("kind") == "channel_scale" and t["status"] == "ANSWERED"]
