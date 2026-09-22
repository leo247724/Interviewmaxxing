"""D0: job discovery, Jev selection and pipeline contracts (fictional data only)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from interviewmaxxing_core import (
    DEFAULT_PIPELINE_STAGES,
    TRANSITIONS,
    ApplicationState,
    Compensation,
    CompensationFloor,
    CompensationPeriod,
    DescriptionCompleteness,
    HoldCode,
    JobListing,
    JobSearchQuery,
    JobSearchRun,
    JobSelection,
    ListingSource,
    ListingStatus,
    ModelDecision,
    OnsiteTarget,
    PipelineEntry,
    PipelineStages,
    PolicyHold,
    ProviderError,
    ProviderUsage,
    RemoteTarget,
    SelectionChoice,
    SelectionOverride,
    SelectionPreferences,
    SourceSearchResult,
    WorkArrangement,
    listing_id_for,
    meets_floor,
    snapshot_hash,
)

NOW = datetime(2026, 9, 22, 21, 0, tzinfo=UTC)
CONTRACTS = Path(__file__).resolve().parents[2] / "CONTRACTS.md"
ATS_URL = "https://boards.greenhouse.example/fictionalco/jobs/7001"


def _listing(source: str, source_id: str | None, source_url: str, **fields) -> JobListing:
    record = ListingSource(source=source, source_listing_id=source_id, source_url=source_url,
                           application_url=fields.get("application_url"), observed_at=NOW,
                           evidence="fictional test observation")
    base = dict(
        id=listing_id_for(source, source_id, source_url), source=source,
        source_listing_id=source_id, source_url=source_url, title="Marketing Manager",
        observed_at=NOW, evidence="fictional test observation", provenance=[record],
    )
    return JobListing(**{**base, **fields})


# --- query defaults and location semantics ----------------------------------------------


def test_default_search_is_the_users_stated_targets():
    query = JobSearchQuery()
    assert query.title_phrases == ["marketing manager", "marketing director"]
    [austin] = query.onsite
    assert austin.location == "Austin, TX"
    assert austin.arrangements == [WorkArrangement.ONSITE, WorkArrangement.HYBRID]
    assert query.remote == RemoteTarget(eligible_region="United States")
    assert query.minimum_compensation == CompensationFloor(amount=100000, currency="USD",
                                                           period=CompensationPeriod.YEAR)
    assert query.sources == ["linkedin", "builtin", "indeed", "google"]
    # Remote eligibility is nationwide and separate from the onsite city.
    assert "texas" not in query.remote.eligible_region.lower()
    assert query.remote.eligible_region != austin.location


def test_query_is_editable_and_validated():
    custom = JobSearchQuery(title_phrases=["  Growth   Marketing Lead ", "growth marketing lead"],
                            keywords=["B2B"], onsite=[], sources=["LinkedIn", "wellfound"],
                            minimum_compensation=None, max_results_per_source=20)
    assert custom.title_phrases == ["Growth Marketing Lead", "growth marketing lead"]
    assert custom.sources == ["linkedin", "wellfound"]
    with pytest.raises(ValidationError, match="onsite target, a remote target"):
        JobSearchQuery(onsite=[], remote=None)
    with pytest.raises(ValidationError, match="title phrase"):
        JobSearchQuery(title_phrases=["  "])
    with pytest.raises(ValidationError):
        OnsiteTarget(location="Austin, TX", arrangements=[WorkArrangement.REMOTE])
    with pytest.raises(ValidationError, match="repeats"):
        JobSearchQuery(sources=["indeed", "Indeed"])


def test_query_from_preferences_follows_edits():
    prefs = SelectionPreferences(target_titles=["Director of Marketing"],
                                 remote=RemoteTarget(eligible_region="United States"))
    query = JobSearchQuery.from_preferences(prefs, max_results_per_source=10)
    assert query.title_phrases == ["Director of Marketing"]
    assert query.max_results_per_source == 10


# --- source results ------------------------------------------------------------------------


def test_blocked_or_login_sources_are_never_silent_empty_success():
    ok_empty = SourceSearchResult(query_id="qry_1", source="builtin", state="OK",
                                  started_at=NOW)
    assert ok_empty.result_count == 0
    with pytest.raises(ValidationError, match="message"):
        SourceSearchResult(query_id="qry_1", source="indeed", state="BLOCKED", started_at=NOW)
    with pytest.raises(ValidationError, match="user_action"):
        SourceSearchResult(query_id="qry_1", source="linkedin", state="NEEDS_USER",
                           message="Sign-in wall", started_at=NOW)
    with pytest.raises(ValidationError, match="use PARTIAL"):
        SourceSearchResult(query_id="qry_1", source="indeed", state="BLOCKED",
                           message="403", listing_ids=["lst_x"], started_at=NOW)


def test_run_results_must_belong_to_the_query():
    query = JobSearchQuery(id="qry_1")
    good = SourceSearchResult(query_id="qry_1", source="google", state="OK", started_at=NOW)
    JobSearchRun(query=query, results=[good])
    with pytest.raises(ValidationError, match="another query"):
        JobSearchRun(query=query, results=[good.model_copy(update={"query_id": "qry_2"})])
    with pytest.raises(ValidationError, match="not in the query"):
        JobSearchRun(query=query.model_copy(update={"sources": ["indeed"]}), results=[good])


# --- listings, compensation and dedupe --------------------------------------------------------


def test_listing_ids_are_stable_and_source_scoped():
    a = listing_id_for("linkedin", "4001", "https://www.linkedin.example/jobs/view/4001")
    assert a == listing_id_for("linkedin", "4001", "https://www.linkedin.example/jobs/view/4001?utm_source=x")
    assert a != listing_id_for("indeed", "4001", "https://www.indeed.example/viewjob?jk=4001")
    by_url = listing_id_for("builtin", None, "https://builtin.example/job/x/1?utm_medium=y")
    assert by_url == listing_id_for("builtin", None, "https://builtin.example/job/x/1")


def test_missing_source_data_stays_unknown():
    listing = _listing("builtin", None, "https://builtin.example/job/x/1")
    assert listing.company is None and listing.compensation is None
    assert listing.work_arrangement is WorkArrangement.UNKNOWN
    assert listing.status is ListingStatus.UNKNOWN
    assert listing.description_completeness is DescriptionCompleteness.NONE
    with pytest.raises(ValidationError, match="NONE exactly when"):
        _listing("builtin", None, "https://builtin.example/job/x/1", description="Full text")


def test_compensation_is_stated_never_invented():
    Compensation(raw_text="Competitive salary")
    with pytest.raises(ValidationError, match="explicit currency and period"):
        Compensation(raw_text="$120k", minimum=120000)
    with pytest.raises(ValidationError, match="raw_text or explicit bounds"):
        Compensation()
    with pytest.raises(ValidationError, match="exceeds"):
        Compensation(minimum=2, maximum=1, currency="USD", period="YEAR")


@pytest.mark.parametrize(
    "pay, expected",
    [
        (None, None),
        (Compensation(raw_text="Competitive"), None),
        (Compensation(minimum=90000, maximum=120000, currency="USD", period="YEAR"), True),
        (Compensation(minimum=70000, maximum=95000, currency="USD", period="YEAR"), False),
        (Compensation(minimum=9000, currency="USD", period="MONTH"), True),
        (Compensation(minimum=80000, currency="USD", period="YEAR"), None),
        (Compensation(maximum=90000, currency="USD", period="YEAR"), False),
        (Compensation(minimum=120000, maximum=130000, currency="EUR", period="YEAR"), None),
        (Compensation(minimum=60, maximum=80, currency="USD", period="HOUR"), None),
    ],
)
def test_meets_floor_is_deterministic_and_tristate(pay, expected):
    floor = CompensationFloor(amount=100000, currency="USD", period="YEAR")
    assert meets_floor(pay, floor) is expected


def test_same_title_and_company_is_not_a_duplicate():
    a = _listing("linkedin", "4001", "https://www.linkedin.example/jobs/view/4001",
                 company="Fictional Co")
    b = _listing("linkedin", "4002", "https://www.linkedin.example/jobs/view/4002",
                 company="Fictional Co")
    other_co = _listing("indeed", "abc", "https://www.indeed.example/viewjob?jk=abc",
                        company="Other Fictional Inc")
    assert not a.is_same_posting(b)
    assert not a.is_same_posting(other_co)
    with pytest.raises(ValueError, match="not provably"):
        a.merged_with(b)


def test_google_result_for_the_same_application_keeps_both_provenance_records():
    linkedin = _listing("linkedin", "4001", "https://www.linkedin.example/jobs/view/4001",
                        company="Fictional Co", application_url=ATS_URL,
                        description="Snippet", description_completeness="PARTIAL")
    google = _listing("google", None, "https://www.google.example/search?q=fictional+marketing",
                      application_url=ATS_URL + "?gh_src=google",
                      location="Austin, TX", work_arrangement="HYBRID",
                      description="Full description text", description_completeness="FULL",
                      compensation=Compensation(raw_text="$110,000 - $130,000 a year",
                                                minimum=110000, maximum=130000,
                                                currency="USD", period="YEAR"))
    assert linkedin.is_same_posting(google)
    merged = linkedin.merged_with(google)
    assert [p.source for p in merged.provenance] == ["linkedin", "google"]
    assert merged.id == linkedin.id and merged.company == "Fictional Co"
    assert merged.location == "Austin, TX" and merged.work_arrangement is WorkArrangement.HYBRID
    assert merged.description_completeness is DescriptionCompleteness.FULL
    assert merged.compensation.maximum == 130000


def test_closed_status_survives_a_merge():
    open_one = _listing("linkedin", "4001", "https://www.linkedin.example/jobs/view/4001",
                        application_url=ATS_URL, status="OPEN")
    closed = _listing("builtin", None, "https://builtin.example/job/x/1",
                      application_url=ATS_URL, status="CLOSED")
    assert open_one.merged_with(closed).status is ListingStatus.CLOSED


# --- selection ---------------------------------------------------------------------------------


def _decision(choice="APPLY", apply=0.8, skip=0.1, review=0.1, confidence=0.8):
    return ModelDecision(choice=choice, confidence=confidence, provider="TypeSafe",
                         probabilities={"APPLY": apply, "SKIP": skip, "REVIEW": review})


def _selection(**fields) -> JobSelection:
    base = dict(
        listing_id="lst_x", candidate_id="cand_avery_example",
        returned_model="typesafe/jev-1.13-20260917", rubric_version="rubric-2026-09-22",
        preferences_fingerprint=SelectionPreferences().fingerprint,
        job_evidence_hash="a" * 64, candidate_evidence_hash="b" * 64,
        model_decision=_decision(), usage=ProviderUsage(total_tokens=900, cost_usd=0.004),
        effective_choice="APPLY", decided_at=NOW,
    )
    return JobSelection(**{**base, **fields})


def test_apply_selection_records_model_provenance():
    sel = _selection()
    assert sel.requested_model == "typesafe/jev-1.13"
    assert sel.model_decision.probabilities[SelectionChoice.APPLY] == 0.8
    assert "interview_probability" not in JobSelection.model_fields
    assert "submitted_at" not in JobSelection.model_fields


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"candidate_evidence_hash": None}, "MISSING_PROFILE"),
        ({"candidate_evidence_hash": None,
          "holds": [PolicyHold(code="MISSING_PROFILE", detail="No profile loaded")]},
         "not allowed with holds"),
        ({"model_decision": None, "returned_model": None,
          "provider_error": ProviderError(code="402", message="Insufficient credits"),
          "holds": [PolicyHold(code="PROVIDER_ERROR", detail="402")]}, "not allowed"),
        ({"holds": [PolicyHold(code="INSUFFICIENT_EVIDENCE", detail="No description")]},
         "not allowed with holds"),
        ({"model_decision": _decision("REVIEW", 0.3, 0.2, 0.5)}, "Jev's APPLY or an explicit"),
        ({"model_decision": None, "returned_model": None}, "needs a model decision"),
    ],
)
def test_effective_apply_is_impossible_without_clean_evidence(fields, message):
    with pytest.raises(ValidationError, match=message):
        _selection(**fields)


def test_holds_and_errors_can_still_be_recorded_as_review():
    failed = _selection(model_decision=None, returned_model=None,
                        provider_error=ProviderError(code="429", message="Rate limited",
                                                     retryable=True),
                        holds=[PolicyHold(code="PROVIDER_ERROR", detail="429 rate limited")],
                        effective_choice="REVIEW")
    assert failed.effective_choice is SelectionChoice.REVIEW
    with pytest.raises(ValidationError, match="PROVIDER_ERROR hold"):
        _selection(model_decision=None, returned_model=None, effective_choice="REVIEW",
                   provider_error=ProviderError(code="UNAVAILABLE", message="down"))


def test_override_is_recorded_separately_and_binds_the_effective_choice():
    skipped_by_jev = _decision("SKIP", 0.1, 0.8, 0.1)
    overridden = _selection(model_decision=skipped_by_jev,
                            override=SelectionOverride(choice="APPLY", reason="Referral",
                                                       decided_at=NOW))
    assert overridden.model_decision.choice is SelectionChoice.SKIP
    with pytest.raises(ValidationError, match="equal the user's override"):
        _selection(override=SelectionOverride(choice="SKIP", reason="Not now"))


@pytest.mark.parametrize(
    "probabilities",
    [
        {"APPLY": 0.5, "SKIP": 0.5},
        {"APPLY": 0.5, "SKIP": 0.4, "REVIEW": 0.4},
        {"APPLY": float("nan"), "SKIP": 0.5, "REVIEW": 0.5},
        {"APPLY": 1.2, "SKIP": -0.1, "REVIEW": -0.1},
    ],
)
def test_malformed_model_output_is_rejected(probabilities):
    with pytest.raises(ValidationError):
        ModelDecision(choice="APPLY", confidence=0.5, probabilities=probabilities)
    with pytest.raises(ValidationError):
        ModelDecision(choice="APPLY", confidence=float("nan"),
                      probabilities={"APPLY": 1, "SKIP": 0, "REVIEW": 0})
    with pytest.raises(ValidationError):
        ProviderUsage(cost_usd=float("inf"))


def test_preference_edits_invalidate_cached_decisions():
    prefs = SelectionPreferences()
    sel = _selection()
    inputs = dict(job_evidence_hash="a" * 64, candidate_evidence_hash="b" * 64,
                  rubric_version="rubric-2026-09-22")
    assert sel.is_current_for(preferences=prefs, **inputs)
    raised = prefs.model_copy(update={"minimum_compensation": CompensationFloor(
        amount=120000, currency="USD", period="YEAR")})
    assert raised.fingerprint != prefs.fingerprint
    assert not sel.is_current_for(preferences=raised, **inputs)
    assert not sel.is_current_for(preferences=prefs, **{**inputs, "rubric_version": "r2"})


def test_snapshot_hash_is_canonical():
    assert snapshot_hash({"b": 1, "a": [1, 2]}) == snapshot_hash({"a": [1, 2], "b": 1})
    assert snapshot_hash({"a": 1}) != snapshot_hash({"a": 2})


def test_hold_codes_cover_the_required_policy_cases():
    for code in ("MISSING_PROFILE", "INSUFFICIENT_EVIDENCE", "PROVIDER_ERROR",
                 "LISTING_CLOSED", "DUPLICATE_APPLICATION", "HARD_CONSTRAINT"):
        assert HoldCode(code)


# --- pipeline ------------------------------------------------------------------------------------


def test_pipeline_stage_is_user_wording_not_application_state():
    entry = PipelineEntry(candidate_id="cand_avery_example", listing_id="lst_x",
                          stage="Waiting on recruiter 🤞", import_source="Job search.numbers",
                          imported_values={"Status": "Waiting on recruiter 🤞"})
    assert entry.stage == "Waiting on recruiter 🤞"
    assert "state" not in PipelineEntry.model_fields
    # A stage named like an application state is just text; it grants nothing.
    applied = entry.model_copy(update={"stage": "SUBMITTED"})
    assert applied.application_id is None
    assert set(DEFAULT_PIPELINE_STAGES).isdisjoint({s.value for s in TRANSITIONS})
    assert ApplicationState.SUBMITTED.value not in DEFAULT_PIPELINE_STAGES


def test_pipeline_entries_and_stage_config_validate():
    PipelineEntry(candidate_id="c", title="Imported row without listing", stage="Applied")
    with pytest.raises(ValidationError, match="listing_id or a title"):
        PipelineEntry(candidate_id="c", stage="Applied")
    with pytest.raises(ValidationError):
        PipelineEntry(candidate_id="c", title="x", stage="  ")
    PipelineStages(stages=["Wishlist", "Applied", "Phone screen"])
    with pytest.raises(ValidationError, match="distinct"):
        PipelineStages(stages=["Applied", "applied"])


# --- published examples in CONTRACTS.md ------------------------------------------------------

EXAMPLE_MODELS = {
    "JobSearchQuery": JobSearchQuery,
    "SourceSearchResult": SourceSearchResult,
    "JobListing": JobListing,
    "SelectionPreferences": SelectionPreferences,
    "JobSelection": JobSelection,
    "PipelineEntry": PipelineEntry,
}


def _published_examples() -> dict[str, str]:
    text = CONTRACTS.read_text()
    found = re.findall(r"<!-- D0-EXAMPLE: (\w+) -->\s*```json\n(.*?)```", text, flags=re.S)
    return dict(found)


def test_contracts_md_publishes_a_valid_example_for_each_d0_model():
    examples = _published_examples()
    assert set(examples) == set(EXAMPLE_MODELS)
    for name, raw in examples.items():
        model = EXAMPLE_MODELS[name].model_validate_json(raw)
        assert json.loads(model.model_dump_json()) == json.loads(raw), name
