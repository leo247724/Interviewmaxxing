"""Deterministic ordering of selection outcomes for the jobs list.

Ordinal only, with no numeric preference weights:

1. SKIP always sorts last.
2. Then the location tier from ``SelectionPreferences.location_priority``. With the
   default STRONGLY_PREFER_ONSITE_HYBRID, a matching Austin onsite/hybrid role sorts
   above every eligible remote role. EQUAL (BALANCED) ranks with PREFERRED, and an
   unknown location never counts as preferred.
3. Then APPLY before REVIEW.
4. Then Jev's semantic role-focus answer: duties that match the focus, then adjacent,
   then the rest. Titles never decide this.
5. Then Jev's own APPLY probability, highest first, and the listing id for stability.

Remote roles stay in the list: the tier orders them and never removes them.
"""

from __future__ import annotations

from collections.abc import Iterable

from interviewmaxxing_core import SelectionChoice

from .decision import SelectionOutcome
from .policy import LocationTier

_TIER_ORDER = {
    LocationTier.PREFERRED: 0,
    LocationTier.EQUAL: 0,
    LocationTier.SECONDARY: 1,
    LocationTier.UNRANKED: 2,
}
_CHOICE_ORDER = {SelectionChoice.APPLY: 0, SelectionChoice.REVIEW: 1, SelectionChoice.SKIP: 2}
_ROLE_ORDER = {"match": 0, "adjacent": 1}


def _role_fit(outcome: SelectionOutcome) -> str | None:
    answer = outcome.assessments.get("role_match")
    return answer.choice if answer else None


def rank_key(outcome: SelectionOutcome) -> tuple[bool, int, int, int, float, str]:
    selection = outcome.selection
    model = selection.model_decision
    apply_probability = model.probabilities.get(SelectionChoice.APPLY, 0.0) if model else 0.0
    return (
        selection.effective_choice is SelectionChoice.SKIP,
        _TIER_ORDER[outcome.location_tier],
        _CHOICE_ORDER[selection.effective_choice],
        _ROLE_ORDER.get(_role_fit(outcome) or "", 2),
        -apply_probability,
        selection.listing_id,
    )


def rank_outcomes(outcomes: Iterable[SelectionOutcome]) -> list[SelectionOutcome]:
    """The outcomes in display order (see the module docstring)."""
    return sorted(outcomes, key=rank_key)


def ranking_reason(outcome: SelectionOutcome) -> str:
    """Why this outcome sits where it does, for display beside the decision."""
    choice = outcome.selection.effective_choice.value
    if outcome.selection.effective_choice is SelectionChoice.SKIP:
        return f"{choice}: listed after every APPLY/REVIEW result"
    tier = outcome.location_tier
    if tier in (LocationTier.PREFERRED, LocationTier.EQUAL):
        group = "preferred location tier"
    elif tier is LocationTier.SECONDARY:
        group = "eligible secondary location tier, below preferred-tier roles"
    else:
        group = "location unknown or unranked, after ranked roles"
    role = _role_fit(outcome)
    focus = {
        "match": "; duties match the role focus",
        "adjacent": "; duties only adjacent to the role focus",
        "mismatch": "; duties outside the role focus",
        "insufficient_evidence": "; role focus not established from the duties",
    }.get(role or "", "")
    return f"{choice} in the {group}{focus}"
