"""Factual job selection: deterministic policy + Jev focused/final questions.

Flow for one listing:

1. Code checks pay against the floor, work arrangement/location, and the explicit
   hard constraints (closed, already applied, pay below floor, onsite outside accepted
   locations, excluded title keywords or companies). Any hard constraint means SKIP
   without calling Jev.
2. A complete earlier Jev decision for identical inputs (same model, rubric, job,
   candidate and preference hashes) is reused instead of spending credits again.
3. Jev answers the focused questions, then the final APPLY/SKIP/REVIEW question with
   those assessments. Provider failure or an invalid answer means REVIEW.
4. Holds (missing/contradictory evidence, ambiguous eligibility, low confidence,
   suspected injection, missing profile) cap the effective decision.

The result is a core ``JobSelection`` (inside :class:`SelectionOutcome`). This package
never opens browsers or submits applications; an effective APPLY only makes the
listing eligible for the existing requested-application flow.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from interviewmaxxing_core import (
    DEFAULT_JEV_MODEL,
    JobListing,
    JobSelection,
    LocalPaths,
    ModelDecision,
    ProviderError,
    ProviderUsage,
    SelectionChoice,
    SelectionPreferences,
    snapshot_hash,
    utc_now,
)

from .decision import SelectionOutcome
from .evidence import CandidateEvidence, jev_listing_view, jev_preferences_view, job_evidence
from .jev import ChoiceAnswer, DecisionResult, JevClient, JevProviderError, ProviderFailure
from .jev import ProviderFailureKind as _Kind
from .policy import (
    CompensationStatus,
    Hold,
    HoldReason,
    LocationStatus,
    LocationTier,
    check_compensation,
    check_location,
    effective_decision,
    evidence_holds,
    hard_constraint_holds,
    location_priority_reason,
    location_tier,
)
from .rubric import (
    Assessment,
    SelectionPolicy,
    assessment_holds,
    assessments_from,
    final_request,
    focused_request,
    reasons_from,
)
from .storage import SelectionStore

log = logging.getLogger("interviewmaxxing_selection")

ApplicationLookup = Callable[[JobListing], str | None]
"""Returns the canonical application ID if this listing was already applied to."""


def evidence_hashes(
    listing: JobListing, candidate: CandidateEvidence | None
) -> tuple[str, str | None]:
    """``(job_evidence_hash, candidate_evidence_hash)``; the candidate hash is ``None``
    when there are no verified qualifications (recorded with a MISSING_PROFILE hold)."""
    job_hash = snapshot_hash(job_evidence(listing))
    if candidate is None or not candidate.has_qualifications:
        return job_hash, None
    return job_hash, snapshot_hash(candidate.for_jev())


def to_contract_error(failure: ProviderFailure) -> ProviderError:
    code = str(failure.status) if failure.status in (401, 402, 403, 429) else failure.kind.value
    message = f"{failure.message} ({failure.action})" if failure.message else failure.action
    return ProviderError(code=code, message=message, retryable=failure.retryable)


@dataclass(frozen=True, slots=True)
class _Context:
    listing: JobListing
    preferences: SelectionPreferences
    candidate: CandidateEvidence | None
    job_hash: str
    candidate_hash: str | None
    cache_key: str
    compensation: CompensationStatus
    location: LocationStatus
    tier: LocationTier
    existing: str | None

    @property
    def tier_reason(self) -> str:
        return location_priority_reason(
            self.tier, self.preferences.location_priority, self.location
        )


class SelectionService:
    def __init__(
        self,
        *,
        client: JevClient | None,
        store: SelectionStore | None = None,
        model: str = DEFAULT_JEV_MODEL,
        policy: SelectionPolicy | None = None,
        application_lookup: ApplicationLookup | None = None,
        candidate_id: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.client = client
        self.store = store
        self.model = model
        self.policy = policy or SelectionPolicy()
        self.application_lookup = application_lookup
        self.candidate_id = candidate_id
        self.clock = clock

    @property
    def rubric_version(self) -> str:
        return self.policy.rubric_version

    def cache_key(
        self,
        listing: JobListing,
        preferences: SelectionPreferences,
        candidate: CandidateEvidence | None,
    ) -> str:
        job_hash, candidate_hash = evidence_hashes(listing, candidate)
        return snapshot_hash(
            {
                "model": self.model,
                "rubric": self.rubric_version,
                "job": job_hash,
                "candidate": candidate_hash,
                "preferences": preferences.fingerprint,
            }
        )

    def is_current(
        self,
        selection: JobSelection,
        listing: JobListing,
        preferences: SelectionPreferences,
        candidate: CandidateEvidence | None,
    ) -> bool:
        """False once the listing, candidate evidence, preferences, rubric/thresholds or
        model changed since ``selection`` was made (it must then be recomputed)."""
        job_hash, candidate_hash = evidence_hashes(listing, candidate)
        return selection.requested_model == self.model and selection.is_current_for(
            preferences=preferences,
            job_evidence_hash=job_hash,
            candidate_evidence_hash=candidate_hash,
            rubric_version=self.rubric_version,
        )

    def select(
        self,
        listing: JobListing,
        preferences: SelectionPreferences,
        candidate: CandidateEvidence | None,
        *,
        use_cache: bool = True,
    ) -> SelectionOutcome:
        job_hash, candidate_hash = evidence_hashes(listing, candidate)
        location = check_location(listing, preferences)
        ctx = _Context(
            listing=listing,
            preferences=preferences,
            candidate=candidate,
            job_hash=job_hash,
            candidate_hash=candidate_hash,
            cache_key=self.cache_key(listing, preferences, candidate),
            compensation=check_compensation(listing, preferences),
            location=location,
            tier=location_tier(location, preferences.location_priority),
            existing=self.application_lookup(listing) if self.application_lookup else None,
        )
        holds = evidence_holds(
            listing,
            preferences,
            has_candidate_evidence=candidate_hash is not None,
            compensation=ctx.compensation,
            location=ctx.location,
        )
        hard = hard_constraint_holds(
            listing,
            preferences,
            compensation=ctx.compensation,
            location=ctx.location,
            existing_application_id=ctx.existing,
        )
        if hard:
            return self._finish(
                ctx, holds=hard + holds, reasons=[h.detail for h in hard] + [ctx.tier_reason]
            )

        if use_cache and self.store is not None:
            cached = self.store.find_cached(listing.id, ctx.cache_key)
            if cached is not None:
                log.info(
                    "selection cache hit listing=%s selection=%s", listing.id, cached.selection.id
                )
                return cached

        state: dict[str, Any] = {
            "listing": jev_listing_view(listing),
            "candidate": candidate.for_jev() if candidate and candidate_hash else None,
            "preferences": jev_preferences_view(preferences),
            "checks": {
                "pay_vs_floor": ctx.compensation.value,
                "location": ctx.location.value,
                "location_tier": ctx.tier.value,
                "location_priority_reason": ctx.tier_reason,
                "policy_holds": sorted({h.reason.value for h in holds}),
            },
        }
        if self.client is None:
            failure = ProviderFailure(kind=_Kind.NOT_CONFIGURED, message="no Jev client configured")
            return self._failed(ctx, holds, failure, [], [])

        results: list[DecisionResult] = []
        requests: list[dict[str, Any]] = []
        try:
            focused = focused_request(self.model, state)
            requests.append(focused.model_dump(mode="json"))
            results.append(self.client.decide(focused))
            assessments = assessments_from(results[0].response)
            final = final_request(self.model, state, assessments)
            requests.append(final.model_dump(mode="json"))
            results.append(self.client.decide(final))
        except JevProviderError as exc:
            return self._failed(ctx, holds, exc.error, results, requests)

        answer = results[1].response.choice("selection")
        choice = SelectionChoice(answer.choice)
        holds = holds + assessment_holds(assessments, choice)
        threshold = {
            SelectionChoice.APPLY: self.policy.apply_confidence,
            SelectionChoice.SKIP: self.policy.skip_confidence,
        }.get(choice)
        if threshold is not None and answer.confidence < threshold:
            holds.append(
                Hold(
                    reason=HoldReason.LOW_CONFIDENCE,
                    detail=f"{choice.value} confidence {answer.confidence:.2f} < {threshold:.2f}",
                )
            )
        decision, note = _model_decision(answer, results[1])
        return self._finish(
            ctx,
            holds=holds,
            reasons=[ctx.tier_reason, *reasons_from(assessments)]
            + [h.detail for h in holds]
            + note,
            model_decision=decision,
            assessments=assessments,
            results=results,
            requests=requests,
        )

    # -- helpers ---------------------------------------------------------------

    def _failed(
        self,
        ctx: _Context,
        holds: list[Hold],
        failure: ProviderFailure,
        results: list[DecisionResult],
        requests: list[dict[str, Any]],
    ) -> SelectionOutcome:
        hold = Hold(
            reason=HoldReason.PROVIDER_ERROR, detail=f"{failure.kind.value}: {failure.action}"
        )
        return self._finish(
            ctx,
            holds=[*holds, hold],
            reasons=[hold.detail, ctx.tier_reason],
            assessments=assessments_from(results[0].response) if results else {},
            results=results,
            requests=requests,
            failure=failure,
        )

    def _finish(
        self,
        ctx: _Context,
        *,
        holds: list[Hold],
        reasons: list[str],
        model_decision: ModelDecision | None = None,
        assessments: dict[str, Assessment] | None = None,
        results: list[DecisionResult] | None = None,
        requests: list[dict[str, Any]] | None = None,
        failure: ProviderFailure | None = None,
    ) -> SelectionOutcome:
        results = results or []
        model_choice = model_decision.choice if model_decision else None
        selection = JobSelection(
            listing_id=ctx.listing.id,
            candidate_id=self._candidate_id(ctx.candidate),
            requested_model=self.model,
            returned_model=results[-1].response.model if results else None,
            rubric_version=self.rubric_version,
            preferences_fingerprint=ctx.preferences.fingerprint,
            job_evidence_hash=ctx.job_hash,
            candidate_evidence_hash=ctx.candidate_hash,
            model_decision=model_decision,
            provider_error=to_contract_error(failure) if failure else None,
            usage=_usage(results),
            holds=[h.to_contract() for h in holds],
            effective_choice=effective_decision(model_choice, holds),
            reasons=reasons,
            decided_at=self.clock(),
        )
        outcome = SelectionOutcome(
            selection=selection,
            holds=holds,
            assessments=assessments or {},
            compensation=ctx.compensation,
            location=ctx.location,
            location_tier=ctx.tier,
            existing_application_id=ctx.existing,
            returned_models=[r.response.model for r in results],
            provider_generation_ids=[r.response.id for r in results if r.response.id],
            provider_calls=len(results),
            latency_seconds=math.fsum(r.latency_seconds for r in results),
            provider_failure=failure,
            cache_key=ctx.cache_key,
        )
        if self.store is not None:
            self.store.save(
                outcome,
                job_snapshot=job_evidence(ctx.listing),
                candidate_snapshot=ctx.candidate.model_dump(mode="json") if ctx.candidate else None,
                preferences=ctx.preferences.model_dump(mode="json"),
                requests=requests or [],
                responses=[r.raw for r in results],
            )
        log.info(
            "selection %s listing=%s effective=%s model_choice=%s holds=%s calls=%d cost=%s",
            selection.id,
            selection.listing_id,
            selection.effective_choice.value,
            model_choice.value if model_choice else None,
            ",".join(h.reason.value for h in holds) or "-",
            outcome.provider_calls,
            selection.usage.cost_usd if selection.usage else None,
        )
        return outcome

    def _candidate_id(self, candidate: CandidateEvidence | None) -> str:
        if candidate is not None:
            return candidate.candidate_id
        return self.candidate_id or LocalPaths.from_env().candidate_id


def _model_decision(
    answer: ChoiceAnswer, result: DecisionResult
) -> tuple[ModelDecision, list[str]]:
    """Jev's final answer as a core ``ModelDecision``. The client accepts sums within
    0.01 of 1; core requires 0.001, so a slightly rounded distribution is renormalized
    for the record (the raw answer stays in the audit) and noted in the reasons."""
    choice = SelectionChoice(answer.choice)
    provider, decision_id = result.response.provider, result.response.id
    probabilities = {SelectionChoice(k): v for k, v in answer.probabilities.items()}
    try:
        decision = ModelDecision(
            choice=choice,
            probabilities=probabilities,
            confidence=answer.confidence,
            provider=provider,
            decision_id=decision_id,
        )
        return decision, []
    except ValidationError:
        total = math.fsum(probabilities.values())
        decision = ModelDecision(
            choice=choice,
            probabilities={k: v / total for k, v in probabilities.items()},
            confidence=answer.confidence,
            provider=provider,
            decision_id=decision_id,
        )
        return decision, [f"provider probabilities summed to {total:.4f}; renormalized"]


def _usage(results: list[DecisionResult]) -> ProviderUsage | None:
    if not results:
        return None
    usages = [r.response.usage for r in results if r.response.usage is not None]
    costs = [u.cost for u in usages if u.cost is not None]
    prompt = sum(u.input_tokens for u in usages)
    completion = sum(u.output_tokens for u in usages)
    return ProviderUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost_usd=math.fsum(costs) if costs else None,
    )
