"""Candidate identity never reaches the provider: names and employers are redacted
inside free-text qualifications, contact details are dropped. Fictional profile only."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from interviewmaxxing_core import CandidateProfile
from interviewmaxxing_selection import (
    CANDIDATE_PLACEHOLDER,
    EMPLOYER_PLACEHOLDER,
    INSTITUTION_PLACEHOLDER,
    CandidateEvidence,
    IdentityScrubber,
)

Facts = Callable[..., CandidateProfile]


@pytest.fixture
def with_facts(fictional_candidate: CandidateProfile) -> Facts:
    """``with_facts(("key", value), ...)``: the fictional profile plus verified facts."""

    def make(*pairs: tuple[str, Any], **identity: Any) -> CandidateProfile:
        base = fictional_candidate.facts[0]
        extra = [
            base.model_copy(update={"id": f"fact.{key}", "key": key, "value": value})
            for key, value in pairs
        ]
        profile = fictional_candidate.model_copy(
            update={"facts": [*fictional_candidate.facts, *extra]}
        )
        if identity:
            profile = profile.model_copy(
                update={"identity": profile.identity.model_copy(update=identity)}
            )
        return profile

    return make


def facts_of(profile: CandidateProfile) -> dict[str, Any]:
    return CandidateEvidence.from_profile(profile).verified_facts


def test_fixture_identity_is_what_these_tests_assume(fictional_candidate: CandidateProfile) -> None:
    who = fictional_candidate.identity
    assert (who.first_name, who.last_name) == ("Avery", "Example")
    assert [e.company for e in fictional_candidate.experience] == ["Fictional Widgets Co"]
    assert [e.institution for e in fictional_candidate.education] == ["Example State University"]


def test_first_name_alone_is_redacted_not_leaked(with_facts: Facts) -> None:
    facts = facts_of(with_facts(("highlight", "Avery presented the Q3 results to the board")))
    assert facts["highlight"] == f"{CANDIDATE_PLACEHOLDER} presented the Q3 results to the board"


def test_last_and_full_names_and_possessives_are_redacted(with_facts: Facts) -> None:
    facts = facts_of(
        with_facts(
            ("a", "Example built the growth team"),
            ("b", "Avery Example's playbook cut CAC 30%"),
            ("c", "Contact AVERY for references"),
        )
    )
    assert facts["a"] == f"{CANDIDATE_PLACEHOLDER} built the growth team"
    assert facts["b"] == f"{CANDIDATE_PLACEHOLDER}'s playbook cut CAC 30%"
    assert facts["c"] == f"Contact {CANDIDATE_PLACEHOLDER} for references"


def test_preferred_name_is_redacted_and_longer_words_are_not(with_facts: Facts) -> None:
    profile = with_facts(
        ("d", "Rory drove CAC down 30%"),
        ("e", "Coordinated the Averyville product launch"),
        preferred_name="Rory",
    )
    facts = facts_of(profile)
    assert facts["d"] == f"{CANDIDATE_PLACEHOLDER} drove CAC down 30%"
    assert facts["e"] == "Coordinated the Averyville product launch"  # not the name


def test_employer_in_free_text_is_redacted_but_the_claim_is_kept(with_facts: Facts) -> None:
    facts = facts_of(
        with_facts(
            ("growth", "Led growth at Fictional Widgets Co, scaling paid media to $400k a month"),
            ("before", "Before Fictional Widgets, ran paid search agency-side"),
            ("school", "Completed analytics coursework at Example State University"),
        )
    )
    assert facts["growth"] == (
        f"Led growth at {EMPLOYER_PLACEHOLDER}, scaling paid media to $400k a month"
    )
    assert facts["before"] == f"Before {EMPLOYER_PLACEHOLDER}, ran paid search agency-side"
    assert facts["school"] == f"Completed analytics coursework at {INSTITUTION_PLACEHOLDER}"


@pytest.mark.parametrize(
    "value",
    [
        "avery@example.test",
        "Reach me at avery@example.test",
        "+1 555 010 0199",
        "555-010-0199",
        "(555) 010 0199",
        "5550100199",
        "https://www.linkedin.example/in/avery-example",
        "Portfolio at www.avery-example.test",
        "ZIP 97477",
    ],
)
def test_contact_values_are_dropped_entirely(with_facts: Facts, value: str) -> None:
    assert "contact" not in facts_of(with_facts(("contact", value)))


def test_values_that_are_only_identity_are_dropped(with_facts: Facts) -> None:
    facts = facts_of(with_facts(("sig", "Avery Example"), ("emp", "Fictional Widgets Co.")))
    assert "sig" not in facts and "emp" not in facts


def test_lists_are_scrubbed_item_by_item_and_numbers_are_kept(with_facts: Facts) -> None:
    facts = facts_of(
        with_facts(
            ("tools", ["GA4", "Managed Avery's dashboards", "avery@example.test"]),
            ("only_contact", ["avery@example.test"]),
            ("flag", True),
        )
    )
    assert facts["tools"] == ["GA4", f"Managed {CANDIDATE_PLACEHOLDER}'s dashboards"]
    assert "only_contact" not in facts
    assert facts["flag"] is True
    assert facts["years_experience.paid_media"] == 7 and facts["monthly_paid_media_spend"] == 400000


def test_experience_titles_are_scrubbed(fictional_candidate: CandidateProfile) -> None:
    entry = fictional_candidate.experience[0]
    profile = fictional_candidate.model_copy(
        update={
            "experience": [
                entry.model_copy(update={"title": "Paid Media Lead, Fictional Widgets Co"}),
                entry.model_copy(update={"id": "exp.other", "title": "Avery Example"}),
            ]
        }
    )
    evidence = CandidateEvidence.from_profile(profile)
    assert [e["title"] for e in evidence.experience] == [f"Paid Media Lead, {EMPLOYER_PLACEHOLDER}"]


def test_nothing_projected_contains_identity(with_facts: Facts) -> None:
    profile = with_facts(
        ("summary", "Avery Example led paid media at Fictional Widgets Co and studied at "
         "Example State University; ask Avery for the deck"),
    )
    dumped = json.dumps(CandidateEvidence.from_profile(profile).for_jev()).casefold()
    for private in ("avery", "example", "fictional widgets", "example state", "@", "555"):
        assert private not in dumped


def test_scrubber_is_reusable_on_free_text(fictional_candidate: CandidateProfile) -> None:
    scrubber = IdentityScrubber.for_profile(fictional_candidate)
    assert scrubber.scrub("Avery ran Fictional Widgets Co paid media") == (
        f"{CANDIDATE_PLACEHOLDER} ran {EMPLOYER_PLACEHOLDER} paid media"
    )
    assert scrubber.scrub("call 555 010 0199") is None
    assert scrubber.scrub("Example") is None
    assert scrubber.scrub_value(12) == 12 and scrubber.scrub_value(None) is None


def test_fact_keys_and_education_fields_are_scrubbed(with_facts: Facts) -> None:
    profile = with_facts(("Avery.metric", "Cut CAC 30%"))
    entry = profile.education[0].model_copy(update={
        "degree": "Analytics credential for Avery at Example State University",
        "field_of_study": "Marketing at Fictional Widgets Co",
        "graduation": "2020",
        "fact_ids": [profile.facts[0].id],
    })
    profile = profile.model_copy(update={"education": [entry]})
    evidence = CandidateEvidence.from_profile(profile)
    assert evidence.verified_facts["[candidate].metric"] == "Cut CAC 30%"
    assert evidence.education[0]["graduation"] == "2020"
    sent = json.dumps(evidence.for_jev()).casefold()
    for private in ("avery", "example state", "fictional widgets"):
        assert private not in sent


def test_employer_known_only_from_fact_is_redacted(with_facts: Facts) -> None:
    profile = with_facts(
        ("prior_employer", "Fictional Acquisitions LLC"),
        ("highlight", "Led paid media at Fictional Acquisitions; improved ROAS 40%"),
    )
    facts = facts_of(profile)
    assert "prior_employer" not in facts
    assert facts["highlight"] == "Led paid media at [employer]; improved ROAS 40%"


def test_provider_receives_only_the_scrubbed_projection(
    with_facts: Facts, listing: Any, prefs: Any, new_bot: Any, make_service: Any
) -> None:
    profile = with_facts(("highlight", "Avery led paid media at Fictional Widgets Co"))
    bot = new_bot()
    make_service(bot).select(listing("remote_manager"), prefs, CandidateEvidence.from_profile(profile))
    assert len(bot.calls) == 2
    for call in bot.calls:
        sent = json.dumps(call["state"]["candidate"]).casefold()
        for private in ("avery", "example", "fictional widgets", "candidate_id", "@"):
            assert private not in sent
        assert "led paid media" in sent


@pytest.mark.parametrize("employer", ["Fictional  Widgets Co", "Fictional\tWidgets Co", "Fictional_Widgets_Co"])
def test_provider_projection_scrubs_identifier_separators_and_whitespace(
    with_facts: Facts, listing: Any, prefs: Any, new_bot: Any, make_service: Any, employer: str,
) -> None:
    profile = with_facts(("Avery_metric", f"Led acquisition at {employer}; cut CAC 30%"))
    evidence = CandidateEvidence.from_profile(profile)
    assert evidence.verified_facts["[candidate]_metric"] == "Led acquisition at [employer]; cut CAC 30%"
    bot = new_bot()
    make_service(bot).select(listing("remote_manager"), prefs, evidence)
    for call in bot.calls:
        sent = json.dumps(call["state"]["candidate"]).casefold()
        assert "avery" not in sent and "fictional" not in sent
        assert "cut cac 30%" in sent
