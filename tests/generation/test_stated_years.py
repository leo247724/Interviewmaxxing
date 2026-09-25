"""Round 14: the factual pass reads the facts the person stated over the derived ones.

The experience timeline derives ``years_experience`` totals and areas from the resume
(``derived:experience_timeline``); the simple answers state the person's own years
(``user:simple-answers``). For the same key the stated fact replaces the derived one
(``prefer_stated``, round 11 in the routing, round 14 here), so the two never disagree.
Fictional facts only (Avery Example, Mock Co)."""

from __future__ import annotations

from typing import Any

import pytest

from interviewmaxxing_core import (
    AnswerSource,
    ApplicationField,
    ApplicationForm,
    CandidateFact,
    CandidateProfile,
    ChoiceValue,
    ControlType,
    FieldOption,
    SemanticType,
    TextValue,
)
from interviewmaxxing_generation.resolver import (
    DERIVED_SOURCE,
    USER_SOURCE,
    derived,
    prefer_stated,
    stated_by_person,
)

URL = "http://127.0.0.1:0/jobs/mock-4012/apply"
STATED = "user:simple-answers"
DERIVED = "derived:experience_timeline"
TOTAL = "How many years of experience do you have?"
PAID_MEDIA = "How many years of paid media experience do you have?"


def fact(key: str, value: Any, *, fid: str, source: str = STATED) -> CandidateFact:
    return CandidateFact(id=fid, key=key, value=value, source=source,
                         verification={"status": "VERIFIED", "method": "USER_STATED",
                                       "verified_at": "2026-09-01T12:00:00Z"})


def with_facts(candidate: CandidateProfile, *facts: CandidateFact) -> CandidateProfile:
    """Avery Example without her own years facts, plus ``facts``."""
    kept = [f for f in candidate.facts if not f.key.startswith("years_")]
    return candidate.model_copy(update={"facts": [*kept, *facts]})


def years_field(label: str, options: list[str] | None = None) -> ApplicationField:
    if options is None:
        return ApplicationField(id="years", label=label, semantic_type=SemanticType.YEARS_EXPERIENCE,
                                control_type=ControlType.TEXT, selector="#years", required=True)
    return ApplicationField(id="years", label=label, semantic_type=SemanticType.YEARS_EXPERIENCE,
                            control_type=ControlType.SELECT, selector="#years", required=True,
                            options=[FieldOption(value=f"o{i}", label=o) for i, o in enumerate(options)])


def resolve_one(make_context: Any, resolve: Any, candidate: CandidateProfile,
                field: ApplicationField) -> Any:
    context = make_context(ApplicationForm(url=URL, fields=[field]), candidate)
    packet = resolve(context)
    assert context.problems(packet) == []
    return packet


# --- the live wordings ----------------------------------------------------------------------


def test_the_stated_total_answers_beside_a_derived_one(
    fictional_candidate: CandidateProfile, make_context: Any, resolve: Any,
) -> None:
    candidate = with_facts(fictional_candidate,
                           fact("years_experience", 5, fid="derived_years_experience", source=DERIVED),
                           fact("years_experience", 8, fid="user_years_total"))
    packet = resolve_one(make_context, resolve, candidate, years_field(TOTAL))
    [answer] = packet.answers
    assert answer.value == TextValue(text="8")
    assert answer.provenance.source is AnswerSource.CANDIDATE_FACT
    assert answer.provenance.reference_ids == ["user_years_total"]
    assert packet.missing_inputs == []


def test_the_stated_paid_media_years_answer_beside_derived_ones(
    fictional_candidate: CandidateProfile, make_context: Any, resolve: Any,
) -> None:
    candidate = with_facts(
        fictional_candidate,
        fact("years_experience", 8, fid="user_years_total"),
        fact("years_experience.paid_media", 4, fid="derived_years_paid_media", source=DERIVED),
        fact("years_experience.paid_media", 7, fid="user_years_paid_media"),
        fact("years_experience.seo", 3, fid="derived_years_seo", source=DERIVED))
    [answer] = resolve_one(make_context, resolve, candidate, years_field(PAID_MEDIA)).answers
    assert answer.value == TextValue(text="7")
    assert answer.provenance.reference_ids == ["user_years_paid_media"]


def test_a_range_select_takes_the_range_of_the_stated_total(
    fictional_candidate: CandidateProfile, make_context: Any, resolve: Any,
) -> None:
    candidate = with_facts(fictional_candidate,
                           fact("years_experience", 5, fid="derived_years_experience", source=DERIVED),
                           fact("years_experience", 8, fid="user_years_total"))
    field = years_field(TOTAL, ["0-2 years", "3-5 years", "6-8 years", "9+ years"])
    [answer] = resolve_one(make_context, resolve, candidate, field).answers
    assert isinstance(answer.value, ChoiceValue) and answer.value.label == "6-8 years"
    assert answer.provenance.reference_ids == ["user_years_total"]


@pytest.mark.parametrize("stated_key,derived_key", [
    ("years_professional_experience", "years_experience"),
    ("years_experience.total", "years_experience"),
    ("years_experience", "years_professional_experience"),
])
def test_every_spelling_of_the_total_is_one_key(
    fictional_candidate: CandidateProfile, make_context: Any, resolve: Any,
    stated_key: str, derived_key: str,
) -> None:
    candidate = with_facts(fictional_candidate,
                           fact(derived_key, 5, fid="derived_total", source=DERIVED),
                           fact(stated_key, 8, fid="stated_total"))
    [answer] = resolve_one(make_context, resolve, candidate, years_field(TOTAL)).answers
    assert answer.value == TextValue(text="8") and answer.provenance.reference_ids == ["stated_total"]


# --- what the rule does not change ----------------------------------------------------------


def test_a_derived_total_answers_until_the_person_states_one(
    fictional_candidate: CandidateProfile, make_context: Any, resolve: Any,
) -> None:
    candidate = with_facts(fictional_candidate,
                           fact("years_experience", 5, fid="derived_years_experience", source=DERIVED),
                           fact("years_experience.paid_media", 7, fid="user_years_paid_media"))
    [answer] = resolve_one(make_context, resolve, candidate, years_field(TOTAL)).answers
    # A stated area never replaces the derived total.
    assert answer.value == TextValue(text="5")
    assert answer.provenance.reference_ids == ["derived_years_experience"]


@pytest.mark.parametrize("other_source", ["resume", "user:simple-answers"])
def test_two_totals_from_the_person_or_the_resume_still_disagree(
    fictional_candidate: CandidateProfile, make_context: Any, resolve: Any, other_source: str,
) -> None:
    # Only a derived fact gives way: a resume total of 6, or a second stated total, beside
    # the stated 8 is a disagreement the person resolves.
    candidate = with_facts(fictional_candidate,
                           fact("years_experience", 8, fid="user_years_total"),
                           fact("years_experience", 6, fid="other_total", source=other_source))
    packet = resolve_one(make_context, resolve, candidate, years_field(TOTAL))
    assert packet.answers == []
    assert [m.field_id for m in packet.missing_inputs] == ["years"]


def test_an_unverified_stated_total_never_replaces_a_derived_one(
    fictional_candidate: CandidateProfile, make_context: Any, resolve: Any,
) -> None:
    unverified = CandidateFact(id="user_years_total", key="years_experience", value=8, source=STATED,
                               verification={"status": "UNVERIFIED"})
    candidate = with_facts(fictional_candidate,
                           fact("years_experience", 5, fid="derived_years_experience", source=DERIVED),
                           unverified)
    [answer] = resolve_one(make_context, resolve, candidate, years_field(TOTAL)).answers
    assert answer.value == TextValue(text="5")
    assert answer.provenance.reference_ids == ["derived_years_experience"]


# --- the helper -------------------------------------------------------------------------------


def test_the_sources_are_read_by_their_prefix() -> None:
    assert (USER_SOURCE, DERIVED_SOURCE) == ("user:", "derived:")
    for source, stated, computed in [
        ("user", True, False), ("user:simple-answers", True, False), ("user:story", True, False),
        ("derived", False, True), ("derived:experience_timeline", False, True),
        ("resume", False, False), ("story:" + "d" * 64, False, False), ("username", False, False),
    ]:
        item = fact("years_experience", 1, fid="f", source=source)
        assert (stated_by_person(item), derived(item)) == (stated, computed), source


def test_prefer_stated_keeps_the_order_and_everything_it_does_not_replace() -> None:
    facts = [fact("skills", "Fictional analytics", fid="skill", source="resume"),
             fact("years_experience", 5, fid="derived_total", source=DERIVED),
             fact("years_experience.seo", 3, fid="derived_seo", source=DERIVED),
             fact("years_experience", 8, fid="stated_total"),
             fact("years_experience", 6, fid="resume_total", source="resume")]
    assert [f.id for f in prefer_stated(iter(facts))] == [
        "skill", "derived_seo", "stated_total", "resume_total"]
