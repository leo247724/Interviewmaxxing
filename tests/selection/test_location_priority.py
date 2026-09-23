"""Location priority (Austin onsite/hybrid strongly preferred, remote a valid secondary
tier), ranking, cache invalidation, and candidate identity never reaching Jev."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

import pytest

from interviewmaxxing_core import (
    CandidateProfile,
    HoldCode,
    JobListing,
    LocationPriority,
    SelectionChoice,
    SelectionPreferences,
    WorkArrangement,
)
from interviewmaxxing_selection import (
    FINAL_QUESTION,
    HARD_CONSTRAINTS,
    CandidateEvidence,
    HoldReason,
    LocationStatus,
    LocationTier,
    SelectionOutcome,
    SelectionService,
    jev_preferences_view,
    location_tier,
    rank_outcomes,
    ranking_reason,
)

Listing = Callable[[str], JobListing]
MakeService = Callable[..., SelectionService]
STRONG = LocationPriority.STRONGLY_PREFER_ONSITE_HYBRID


def test_default_priority_is_strong_austin_preference(prefs: SelectionPreferences) -> None:
    assert prefs.location_priority is STRONG
    assert prefs.remote is not None  # remote stays eligible
    assert jev_preferences_view(prefs)["location_priority"] == "STRONGLY_PREFER_ONSITE_HYBRID"


@pytest.mark.parametrize(
    ("location", "priority", "tier"),
    [
        (LocationStatus.ONSITE_ACCEPTED, STRONG, LocationTier.PREFERRED),
        (LocationStatus.REMOTE_REGION_MATCH, STRONG, LocationTier.SECONDARY),
        (LocationStatus.REMOTE_NEEDS_ELIGIBILITY, STRONG, LocationTier.SECONDARY),
        (LocationStatus.UNKNOWN, STRONG, LocationTier.UNRANKED),
        (LocationStatus.ONSITE_MISMATCH, STRONG, LocationTier.UNRANKED),
        (LocationStatus.ONSITE_ACCEPTED, LocationPriority.BALANCED, LocationTier.EQUAL),
        (LocationStatus.REMOTE_REGION_MATCH, LocationPriority.BALANCED, LocationTier.EQUAL),
        (LocationStatus.UNKNOWN, LocationPriority.BALANCED, LocationTier.UNRANKED),
        (
            LocationStatus.REMOTE_REGION_MATCH,
            LocationPriority.PREFER_REMOTE,
            LocationTier.PREFERRED,
        ),
        (LocationStatus.ONSITE_ACCEPTED, LocationPriority.PREFER_REMOTE, LocationTier.SECONDARY),
    ],
)
def test_location_tier(
    location: LocationStatus, priority: LocationPriority, tier: LocationTier
) -> None:
    assert location_tier(location, priority) is tier


def test_rubric_states_relative_importance_without_exclusion() -> None:
    text = FINAL_QUESTION.instructions
    assert "strongly preferred" in text
    assert "never by itself a reason to SKIP" in text
    assert "UNRANKED never counts as the preferred tier" in text
    assert "weight" not in text.casefold()


def test_priority_and_tier_reach_jev_state_and_reasons(
    listing: Listing,
    prefs: SelectionPreferences,
    candidate: CandidateEvidence,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    bot = new_bot()
    austin = make_service(bot).select(listing("austin_onsite_manager"), prefs, candidate)
    assert austin.location_tier is LocationTier.PREFERRED
    state = bot.calls[0]["state"]
    assert state["preferences"]["location_priority"] == "STRONGLY_PREFER_ONSITE_HYBRID"
    assert state["checks"]["location_tier"] == "PREFERRED"
    assert bot.calls[1]["state"]["checks"]["location_tier"] == "PREFERRED"
    assert any("preferred tier" in r for r in austin.selection.reasons)


def test_eligible_remote_is_a_valid_secondary_tier_not_excluded(
    listing: Listing,
    prefs: SelectionPreferences,
    candidate: CandidateEvidence,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    remote = listing("remote_manager")
    out = make_service(new_bot()).select(remote, prefs, candidate)
    assert out.location_tier is LocationTier.SECONDARY
    assert out.selection.effective_choice is SelectionChoice.APPLY
    assert not {h.reason for h in out.holds} & HARD_CONSTRAINTS
    assert any("secondary tier" in r and "not excluded" in r for r in out.selection.reasons)
    # Jev skipping a strong remote fit is treated as a contradiction, not a location veto.
    skipped = make_service(new_bot(selection="SKIP")).select(
        remote, prefs, candidate, use_cache=False
    )
    assert skipped.selection.effective_choice is SelectionChoice.REVIEW
    assert HoldReason.CONTRADICTORY_EVIDENCE in {h.reason for h in skipped.holds}


def test_changing_priority_invalidates_cached_decisions(
    listing: Listing,
    prefs: SelectionPreferences,
    candidate: CandidateEvidence,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    bot = new_bot()
    service = make_service(bot)
    item = listing("remote_manager")
    first = service.select(item, prefs, candidate)
    assert service.select(item, prefs, candidate) == first and len(bot.calls) == 2
    balanced = prefs.model_copy(update={"location_priority": LocationPriority.BALANCED})
    assert balanced.fingerprint != prefs.fingerprint
    assert not service.is_current(first.selection, item, balanced, candidate)
    second = service.select(item, balanced, candidate)
    assert len(bot.calls) == 4 and second.selection.id != first.selection.id
    assert second.location_tier is LocationTier.EQUAL
    assert bot.calls[2]["state"]["preferences"]["location_priority"] == "BALANCED"


def _outcomes(
    listing: Listing,
    prefs: SelectionPreferences,
    candidate: CandidateEvidence,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> dict[str, SelectionOutcome]:
    return {
        # Austin role Jev is unsure about -> REVIEW
        "austin_review": make_service(new_bot(confidence={"selection": 0.5})).select(
            listing("austin_onsite_manager"), prefs, candidate
        ),
        "remote_apply": make_service(new_bot()).select(listing("remote_manager"), prefs, candidate),
        "unknown_location": make_service(new_bot()).select(
            listing("snippet_only"), prefs, candidate
        ),
        "skip": make_service(new_bot()).select(listing("dallas_onsite"), prefs, candidate),
    }


def test_ranking_puts_austin_well_above_remote_and_skip_last(
    listing: Listing,
    prefs: SelectionPreferences,
    candidate: CandidateEvidence,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    out = _outcomes(listing, prefs, candidate, new_bot, make_service)
    assert out["austin_review"].selection.effective_choice is SelectionChoice.REVIEW
    assert out["remote_apply"].selection.effective_choice is SelectionChoice.APPLY
    ranked = rank_outcomes(reversed(list(out.values())))
    names = {id(v): k for k, v in out.items()}
    assert [names[id(o)] for o in ranked] == [
        "austin_review",
        "remote_apply",
        "unknown_location",
        "skip",
    ]
    assert "preferred location tier" in ranking_reason(out["austin_review"])
    assert "secondary location tier" in ranking_reason(out["remote_apply"])
    assert "after every APPLY/REVIEW" in ranking_reason(out["skip"])


def test_balanced_priority_ranks_by_decision(
    listing: Listing,
    candidate: CandidateEvidence,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    balanced = SelectionPreferences(location_priority=LocationPriority.BALANCED)
    out = _outcomes(listing, balanced, candidate, new_bot, make_service)
    ranked = rank_outcomes(out.values())
    assert ranked[0] is out["remote_apply"]  # APPLY before REVIEW within the equal tier
    assert ranked[1] is out["austin_review"]


def test_candidate_identity_never_reaches_jev_or_logs(
    fictional_candidate: CandidateProfile,
    listing: Listing,
    prefs: SelectionPreferences,
    new_bot: Callable[..., Any],
    make_service: MakeService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    who = fictional_candidate.identity
    leaky_facts = [
        *fictional_candidate.facts,
        fictional_candidate.facts[0].model_copy(
            update={"id": "fact.bio", "key": "bio", "value": f"Contact {who.email} any time"}
        ),
        fictional_candidate.facts[0].model_copy(
            update={"id": "fact.sig", "key": "signature", "value": f"{who.full_name}, marketer"}
        ),
        fictional_candidate.facts[0].model_copy(
            update={"id": "fact.cell", "key": "cell", "value": "Call 512-555-0147"}
        ),
    ]
    profile = fictional_candidate.model_copy(update={"facts": leaky_facts})
    evidence = CandidateEvidence.from_profile(profile)
    assert {"bio", "cell"}.isdisjoint(evidence.verified_facts)  # contact details: dropped
    assert evidence.verified_facts["signature"] == "[candidate], marketer"  # name: redacted
    assert evidence.verified_facts["current_title"] == "Paid Media Lead"

    caplog.set_level(logging.DEBUG)
    bot = new_bot()
    make_service(bot).select(listing("austin_onsite_manager"), prefs, evidence)
    sent = json.dumps([call["state"] for call in bot.calls]).casefold()
    assert "paid media lead" in sent  # the verified qualification is sent
    private = [who.email, who.full_name, who.phone, who.address.street, "512-555-0147"]
    for value in private:
        if value:
            assert value.casefold() not in sent
            assert value.casefold() not in caplog.text.casefold()
    for name in (who.first_name, who.last_name, who.preferred_name):
        if name:
            assert re.search(rf"\b{re.escape(name.casefold())}\b", sent) is None
            assert re.search(rf"\b{re.escape(name.casefold())}\b", caplog.text.casefold()) is None


@pytest.mark.parametrize(
    "value",
    ["reach me at someone@example.com", "(512) 555-0147", "https://linkedin.com/in/someone"],
)
def test_evidence_rejects_contact_details(value: str) -> None:
    with pytest.raises(ValueError, match="contact details"):
        CandidateEvidence(candidate_id="c", verified_facts={"note": value})


def test_ordinary_qualifications_are_not_mistaken_for_contact() -> None:
    evidence = CandidateEvidence(
        candidate_id="c",
        verified_facts={"tenure": "2019-01 - 2023-05", "budget": "USD 4,800,000 per year"},
    )
    assert evidence.has_qualifications


def test_remote_outside_region_is_held_regardless_of_jev(
    listing: Listing,
    prefs: SelectionPreferences,
    candidate: CandidateEvidence,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    canada = listing("remote_manager").model_copy(
        update={"remote_eligibility": "Canada", "location": "Canada"}
    )
    bot = new_bot()  # Jev (mistakenly) says eligible and APPLY
    out = make_service(bot).select(canada, prefs, candidate)
    assert out.location is LocationStatus.REMOTE_OUTSIDE_REGION
    assert out.location_tier is LocationTier.UNRANKED
    assert out.selection.effective_choice is SelectionChoice.REVIEW
    assert HoldReason.REMOTE_OUTSIDE_REGION in {h.reason for h in out.holds}
    assert HoldCode.HARD_CONSTRAINT in {h.code for h in out.selection.holds}
    assert not {h.reason for h in out.holds} & HARD_CONSTRAINTS  # reviewed, not skipped
    assert "REMOTE_OUTSIDE_REGION" in bot.calls[0]["state"]["checks"]["policy_holds"]
    skipped = make_service(new_bot(location_eligibility="not_eligible", selection="SKIP")).select(
        canada, prefs, candidate, use_cache=False
    )
    assert skipped.selection.effective_choice is SelectionChoice.SKIP  # Jev's SKIP stands


def test_narrower_remote_eligibility_is_held_regardless_of_jev(
    listing: Listing,
    prefs: SelectionPreferences,
    candidate: CandidateEvidence,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    texas = listing("remote_manager").model_copy(
        update={"remote_eligibility": "Texas", "location": "Texas"}
    )
    bot = new_bot()  # Jev says eligible and APPLY
    out = make_service(bot).select(texas, prefs, candidate)
    assert out.location is LocationStatus.REMOTE_ELIGIBILITY_AMBIGUOUS
    assert out.location_tier is LocationTier.SECONDARY  # still a possible role
    assert out.selection.effective_choice is SelectionChoice.REVIEW
    assert HoldReason.ELIGIBILITY_AMBIGUOUS in {h.reason for h in out.holds}
    assert bot.calls[0]["state"]["checks"]["location"] == "REMOTE_ELIGIBILITY_AMBIGUOUS"
    assert "ELIGIBILITY_AMBIGUOUS" in bot.calls[0]["state"]["checks"]["policy_holds"]


def test_onsite_without_a_locality_is_reviewed_not_skipped(
    listing: Listing,
    prefs: SelectionPreferences,
    candidate: CandidateEvidence,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    for location in ("Texas, United States", "United States", "Multiple Locations"):
        item = listing("austin_onsite_manager").model_copy(
            update={"location": location, "work_arrangement": WorkArrangement.HYBRID}
        )
        bot = new_bot()
        out = make_service(bot).select(item, prefs, candidate)
        assert out.location is LocationStatus.UNKNOWN, location
        assert out.location_tier is LocationTier.UNRANKED
        assert out.selection.effective_choice is SelectionChoice.REVIEW
        assert HoldReason.LOCATION_UNKNOWN in {h.reason for h in out.holds}
        assert len(bot.calls) == 2  # Jev was consulted; nothing was skipped by code
    minnesota = listing("austin_onsite_manager").model_copy(update={"location": "Austin, MN"})
    bot = new_bot()
    out = make_service(bot).select(minnesota, prefs, candidate)
    assert out.selection.effective_choice is SelectionChoice.SKIP and bot.calls == []
