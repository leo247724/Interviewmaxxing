"""Semantic role focus: titles are search seeds, duties decide. Jev's answers are scripted
here (offline); these tests pin the plumbing, rubric wording, holds, ranking and cache."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from interviewmaxxing_core import JobListing, SelectionChoice, SelectionPreferences
from interviewmaxxing_selection import (
    FOCUSED_QUESTIONS,
    HARD_CONSTRAINTS,
    CandidateEvidence,
    HoldReason,
    SelectionService,
    jev_preferences_view,
    rank_outcomes,
    ranking_reason,
)

Listing = Callable[[str], JobListing]
MakeService = Callable[..., SelectionService]

PERFORMANCE_MARKETER = CandidateEvidence(
    candidate_id="cand_fictional_operator",
    verified_facts={
        "current_title": "Senior Paid Media Manager",
        "years_experience.paid_media": 8,
        "annual_paid_budget_owned": "USD 4M across Meta, Google and TikTok",
        # Technical terms inside marketing work must not read as an engineering profile.
        "skills": ["Google Ads API", "SQL", "Python scripts for bid automation", "GA4"],
    },
)


def test_role_focus_and_titles_as_seeds_reach_jev(
    listing: Listing,
    prefs: SelectionPreferences,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    view = jev_preferences_view(prefs)
    assert view["role_focus"] == prefs.role_focus
    assert view["representative_titles"] == prefs.target_titles
    assert "target_titles" not in view  # never framed as an exact-title list
    bot = new_bot()
    make_service(bot).select(listing("acquisition_lead"), prefs, PERFORMANCE_MARKETER)
    for call in bot.calls:
        assert call["state"]["preferences"]["role_focus"] == prefs.role_focus


def test_role_question_judges_duties_not_titles() -> None:
    role = FOCUSED_QUESTIONS["role_match"]
    text = role.instructions
    for phrase in (
        "actual duties and ownership, not its title",
        "not an allowlist",
        "Title keywords alone are not proof",
        "paid acquisition budgets",
        "experiments",
        "attribution",
        "revenue outcomes",
        "leading a team or channel",
        "data, software or platform engineering is a mismatch",
    ):
        assert phrase in text
    assert "whatever the title" in role.criteria["match"]
    assert "even if the title sounds similar" in role.criteria["mismatch"]
    qualification = FOCUSED_QUESTIONS["qualification_match"].instructions
    assert "not evidence against a marketing fit" in qualification


@pytest.mark.parametrize(
    "name", ["acquisition_lead", "data_platform_engineer", "marketing_title_unrelated"]
)
def test_no_title_gate_every_role_reaches_jev(
    listing: Listing,
    prefs: SelectionPreferences,
    new_bot: Callable[..., Any],
    make_service: MakeService,
    name: str,
) -> None:
    bot = new_bot()
    out = make_service(bot).select(listing(name), prefs, PERFORMANCE_MARKETER)
    assert len(bot.calls) == 2  # decided on duties by Jev, not rejected by a title rule
    assert not {h.reason for h in out.holds} & HARD_CONSTRAINTS


def test_semantically_equivalent_acquisition_lead_is_positive(
    listing: Listing,
    prefs: SelectionPreferences,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    out = make_service(new_bot()).select(listing("acquisition_lead"), prefs, PERFORMANCE_MARKETER)
    assert out.selection.effective_choice is SelectionChoice.APPLY
    assert out.holds == []
    assert any("duties match the role focus" in r for r in out.selection.reasons)
    assert "duties match the role focus" in ranking_reason(out)


def test_pure_data_platform_engineer_is_negative(
    listing: Listing,
    prefs: SelectionPreferences,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    item = listing("data_platform_engineer")
    bot = new_bot(role_match="mismatch", qualification_match="does_not_meet", selection="SKIP")
    out = make_service(bot).select(item, prefs, PERFORMANCE_MARKETER)
    assert out.selection.effective_choice is SelectionChoice.SKIP
    assert any("outside the role focus" in r for r in out.selection.reasons)
    # Even a mistaken Jev APPLY cannot pass with a role mismatch.
    wrong = new_bot(role_match="mismatch", selection="APPLY")
    held = make_service(wrong).select(item, prefs, PERFORMANCE_MARKETER, use_cache=False)
    assert held.selection.effective_choice is SelectionChoice.REVIEW
    assert HoldReason.CONTRADICTORY_EVIDENCE in {h.reason for h in held.holds}


def test_marketing_title_with_unrelated_duties_is_held_or_rejected(
    listing: Listing,
    prefs: SelectionPreferences,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    item = listing("marketing_title_unrelated")
    adjacent = make_service(new_bot(role_match="adjacent")).select(
        item, prefs, PERFORMANCE_MARKETER
    )
    assert adjacent.selection.effective_choice is SelectionChoice.REVIEW
    assert HoldReason.ROLE_FOCUS_UNCONFIRMED in {h.reason for h in adjacent.holds}
    rejected = make_service(new_bot(role_match="mismatch", selection="SKIP")).select(
        item, prefs, PERFORMANCE_MARKETER, use_cache=False
    )
    assert rejected.selection.effective_choice is SelectionChoice.SKIP


def test_software_terms_in_marketing_resume_are_not_disqualifying(
    listing: Listing,
    prefs: SelectionPreferences,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    bot = new_bot()
    out = make_service(bot).select(listing("acquisition_lead"), prefs, PERFORMANCE_MARKETER)
    assert out.selection.effective_choice is SelectionChoice.APPLY
    skills = bot.calls[0]["state"]["candidate"]["verified_facts"]["skills"]
    assert "Google Ads API" in skills and "Python scripts for bid automation" in skills


def test_role_focus_change_invalidates_cache(
    listing: Listing,
    prefs: SelectionPreferences,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    bot = new_bot()
    service = make_service(bot)
    item = listing("acquisition_lead")
    first = service.select(item, prefs, PERFORMANCE_MARKETER)
    assert service.select(item, prefs, PERFORMANCE_MARKETER) == first and len(bot.calls) == 2
    refocused = prefs.model_copy(update={"role_focus": "Lifecycle and CRM marketing leadership."})
    assert refocused.fingerprint != prefs.fingerprint
    assert not service.is_current(first.selection, item, refocused, PERFORMANCE_MARKETER)
    second = service.select(item, refocused, PERFORMANCE_MARKETER)
    assert len(bot.calls) == 4 and second.selection.id != first.selection.id
    assert bot.calls[2]["state"]["preferences"]["role_focus"] == refocused.role_focus
    assert second.selection.preferences_fingerprint == refocused.fingerprint


def test_ranking_prefers_duty_match_over_adjacent_within_a_tier(
    listing: Listing,
    prefs: SelectionPreferences,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    lenient = {"confidence": {"selection": 0.5}}  # both REVIEW, same Austin tier
    adjacent = make_service(new_bot(role_match="adjacent", **lenient)).select(
        listing("marketing_title_unrelated"), prefs, PERFORMANCE_MARKETER
    )
    match = make_service(new_bot(**lenient)).select(
        listing("acquisition_lead"), prefs, PERFORMANCE_MARKETER
    )
    assert adjacent.selection.effective_choice is match.selection.effective_choice
    assert rank_outcomes([adjacent, match]) == [match, adjacent]
    assert "only adjacent" in ranking_reason(adjacent)
