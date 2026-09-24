"""Edges of the pooled residence and resume gates (WP10 item 1).

Probes Jev's probability tolerance (sums within 1 ± 0.01), the exact 0.95 / 0.03
boundaries, ties inside a pool, control types that are never pooled, pre-typed fields
(Jev is not asked their meaning), custom thresholds, holds, report serialization and the
report cache. Fields are typed by the browser heuristics where the inspector would type them.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from interviewmaxxing_browser.ai import (
    AIFormRouter,
    BoundedDecisions,
    CallBudget,
    DynamicPacketResolver,
    FormRouteReport,
    RouteThresholds,
)
from interviewmaxxing_browser.ai.classification import FieldRoute, FieldRouteDecision
from interviewmaxxing_browser.semantics import classify as classify_semantics
from interviewmaxxing_core import (
    AnswerSource,
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    CandidateProfile,
    ControlType,
    FieldOption,
    JobRecord,
    PacketContext,
    SemanticType,
)
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

Spec = str | tuple[dict[str, float], float]
DEFAULTS = {"r": "COPY_KNOWN", "n": "literal", "u": "APPLICANT_CURRENT", "s": "CUSTOM_TEXT",
            "d": "APPLICATION_ATTACHMENT"}
RESIDENCE = ("LOCATION", "COUNTRY", "STATE", "CITY")


class Jev:
    """Scripted Jev. ``scripts[field_id][kind]`` answers a field's route (r), prose (n),
    source (u), semantic (s) or file purpose (d) question; ``picks`` answers other choice
    questions by name. A spec is a choice (all mass, confidence 1.0) or (probabilities,
    confidence). Unscripted choices pick NONE/UNKNOWN, nouls answer 0.0, and a non-200
    ``status`` fails every call."""

    def __init__(self, scripts: dict[str, dict[str, Spec]] | None = None,
                 picks: dict[str, Spec] | None = None, status: int = 200) -> None:
        self.scripts, self.picks, self.status = scripts or {}, picks or {}, status
        self.requests: list[dict[str, Any]] = []

    def asked(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if name in r["questions"]]

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        if self.status != 200:
            return HttpResponse(self.status, {}, b"{}")
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.0}
                continue
            criteria = list(question["criteria"])
            if name[0] in DEFAULTS and name[1:].isdigit():
                field_id = request["state"]["fields"][f"f{name[1:]}"]["field_id"]
                spec = self.scripts.get(field_id, {}).get(name[0], DEFAULTS[name[0]])
            else:
                spec = self.picks.get(name, next(
                    (c for c in ("NONE", "hold", "UNKNOWN") if c in criteria), criteria[0]))
            if isinstance(spec, str):
                probabilities, confidence = {c: float(c == spec) for c in criteria}, 1.0
            else:
                probabilities = {c: spec[0].get(c, 0.0) for c in criteria}
                confidence = spec[1]
            answers[name] = {"type": "choice", "confidence": confidence, "probabilities": probabilities,
                             "choice": max(criteria, key=lambda c: probabilities[c])}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


def router(provider: Jev) -> AIFormRouter:
    return AIFormRouter(BoundedDecisions(JevClient(ApiKey("synthetic-test", source="fixture"),
        transport=provider, max_attempts=1), CallBudget()))


def observed(label: str, control: ControlType, *options: str, field_id: str = "answer",
             required: bool = True, semantic: SemanticType | None = None) -> ApplicationField:
    """A field as the inspector reports it: typed by the browser heuristics unless given."""
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label,
        semantic_type=semantic or classify_semantics(label=label, control_type=control),
        control_type=control, required=required,
        options=[FieldOption(value=f"v{i}", label=o) for i, o in enumerate(options)] or None)


def decide(provider: Jev, *fields: ApplicationField) -> FormRouteReport:
    return router(provider).classify_form(
        ApplicationForm(url="https://synthetic.test/apply", fields=list(fields)), document_id="edges")


def resolve(provider: Jev, candidate: CandidateProfile, job: JobRecord, *fields: ApplicationField,
            ) -> tuple[ApplicationPacket, PacketContext, DynamicPacketResolver]:
    r = router(provider)
    form = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=list(fields)),
                      document_id="edges-e2e")
    ctx = PacketContext(form=form, candidate=candidate, job=job, application=Application(
        id="app-edges", request_id="request-edges", job_id=job.id, candidate_id=candidate.id,
        state=ApplicationState.INSPECTING, version=1,
        created_at="2026-09-24T00:00:00Z", updated_at="2026-09-24T00:00:00Z"))
    resolver = DynamicPacketResolver(r.decisions, router=r)
    packet = asyncio.run(resolver.resolve(ctx))
    assert ctx.problems(packet) == []
    return packet, ctx, resolver


LIVE = "Where do you currently live?"
SPLIT = {"COUNTRY": 0.24, "LOCATION": 0.71, "CUSTOM_BOOLEAN": 0.03, "UNKNOWN": 0.02}
PURPOSE_SPLIT = {"APPLICATION_ATTACHMENT": 0.52, "AUTOFILL_PARSER": 0.46, "OTHER_OR_UNCLEAR": 0.02}
ROUTE_099 = ({"APPROVED_DOCUMENT": 0.99, "AMBIGUOUS": 0.01}, 0.97)


# --- residence pool ---------------------------------------------------------------------

def test_a_pool_share_over_one_within_jev_tolerance_is_recorded_as_one() -> None:
    # Jev's probabilities may sum to 1 +/- 0.01; the recorded share stays a probability.
    report = decide(Jev({
        "live": {"s": ({"COUNTRY": 0.50, "LOCATION": 0.509}, 0.5)},
        "cv": {"r": "APPROVED_DOCUMENT",
               "d": ({"APPLICATION_ATTACHMENT": 0.509, "AUTOFILL_PARSER": 0.50}, 0.5)},
    }), observed(LIVE, ControlType.SELECT, "United States", "Canada", field_id="live"),
        observed("Upload your CV", ControlType.FILE, field_id="cv"))
    live, cv = report.field("live"), report.field("cv")
    assert (live.semantic_type, live.semantic_pool_share) == (SemanticType.LOCATION, 1.0)
    assert (cv.route, cv.document_pool_share, cv.autofill) == (FieldRoute.APPROVED_DOCUMENT, 1.0, False)


@pytest.mark.parametrize("split,expected", [
    # Exactly at both bounds: 0.95 in the pool, 0.03 on one outside reading.
    ({"COUNTRY": 0.30, "LOCATION": 0.65, "CUSTOM_BOOLEAN": 0.03, "UNKNOWN": 0.02}, SemanticType.LOCATION),
    # A sum of binary fractions that is 0.95 only up to float rounding.
    ({"COUNTRY": 0.33, "LOCATION": 0.33, "STATE": 0.29, "CUSTOM_BOOLEAN": 0.03, "UNKNOWN": 0.02},
     SemanticType.LOCATION),
    # Many small outside readings: the bound is per reading, not their total.
    ({"COUNTRY": 0.45, "LOCATION": 0.50, "CUSTOM_BOOLEAN": 0.01, "UNKNOWN": 0.01,
      "RELOCATION": 0.01, "CUSTOM_SELECT": 0.01, "WORK_AUTHORIZATION": 0.01}, SemanticType.LOCATION),
    # A hair under the pool gate, or a hair over the outside bound: not one reading.
    ({"COUNTRY": 0.2499, "LOCATION": 0.70, "CUSTOM_BOOLEAN": 0.03, "UNKNOWN": 0.0201},
     SemanticType.UNKNOWN),
    ({"COUNTRY": 0.30, "LOCATION": 0.659, "CUSTOM_BOOLEAN": 0.031, "UNKNOWN": 0.01},
     SemanticType.UNKNOWN),
])
def test_the_residence_pool_boundaries(split: dict[str, float], expected: SemanticType) -> None:
    decision = decide(Jev({"answer": {"s": (split, 0.41)}}),
                      observed(LIVE, ControlType.RADIO, "United States", "Canada")).fields[0]
    assert decision.semantic_type is expected
    assert decision.semantic_pool_share == pytest.approx(sum(split.get(t, 0.0) for t in RESIDENCE))


@pytest.mark.parametrize("split,expected", [
    ({"COUNTRY": 0.475, "LOCATION": 0.475, "CUSTOM_BOOLEAN": 0.03, "UNKNOWN": 0.02}, SemanticType.LOCATION),
    ({"CITY": 0.475, "STATE": 0.475, "CUSTOM_BOOLEAN": 0.03, "UNKNOWN": 0.02}, SemanticType.STATE),
    ({"CITY": 0.40, "STATE": 0.35, "COUNTRY": 0.22, "UNKNOWN": 0.03}, SemanticType.CITY),
])
def test_the_leading_residence_type_wins_and_a_tie_goes_to_the_first(
    split: dict[str, float], expected: SemanticType,
) -> None:
    decision = decide(Jev({"answer": {"s": (split, 0.4)}}),
                      observed(LIVE, ControlType.SELECT, "Yes", "No")).fields[0]
    assert decision.semantic_type is expected


def test_a_three_way_state_list_split_is_answered_from_the_address(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    label = "Do you reside in any of the following states: CA, OR, WA?"
    field = observed(label, ControlType.SELECT, "Yes", "No")
    assert field.semantic_type is SemanticType.CUSTOM_SELECT  # "states" is not "state"
    split = {"STATE": 0.45, "LOCATION": 0.40, "COUNTRY": 0.12, "UNKNOWN": 0.03}
    packet, ctx, resolver = resolve(Jev({"answer": {"s": (split, 0.44)}}, picks={"residence": "o0"}),
                                    fictional_candidate, mock_job, field)
    assert ctx.form.field("answer").semantic_type is SemanticType.STATE
    [answer] = packet.answers  # the fixture applicant lives in Oregon
    assert answer.value.label == "Yes" and answer.provenance.source is AnswerSource.PROFILE_IDENTITY
    [trace] = [t for t in resolver.narrative_traces if t["stage"] == "residence_screener"]
    assert (trace["status"], trace["state_list_member"]) == ("ANSWERED", True)


def test_a_radio_residence_question_is_pooled_and_answered_like_a_select(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    field = observed("Are you currently based in any of these countries? United States, Canada",
                     ControlType.RADIO, "Yes", "No")
    split = {"COUNTRY": 0.49, "LOCATION": 0.50, "CUSTOM_BOOLEAN": 0.01}
    packet, ctx, _ = resolve(Jev({"answer": {"s": (split, 0.5)}}, picks={"residence": "o0"}),
                             fictional_candidate, mock_job, field)
    assert ctx.form.field("answer").semantic_type is SemanticType.LOCATION
    assert [a.value.label for a in packet.answers] == ["Yes"]


@pytest.mark.parametrize("control,options", [
    (ControlType.TYPEAHEAD, ()),
    (ControlType.MULTISELECT, ("United States", "Canada", "Mexico")),
    (ControlType.CHECKBOX_GROUP, ("United States", "Canada", "Mexico")),
])
def test_lookups_and_multi_choice_controls_are_never_pooled(
    control: ControlType, options: tuple[str, ...],
) -> None:
    field = observed(LIVE, control, *options)
    assert field.semantic_type in (SemanticType.UNKNOWN, SemanticType.CUSTOM_MULTISELECT)
    decision = decide(Jev({"answer": {"s": ({"COUNTRY": 0.49, "LOCATION": 0.50, "CITY": 0.01}, 0.5)}}),
                      field).fields[0]
    assert decision.semantic_type is SemanticType.UNKNOWN
    assert decision.semantic_pool_share is None
    assert decision.semantic_probabilities["LOCATION"] == 0.50  # the raw reading is kept


def test_pre_typed_fields_are_not_asked_their_meaning_so_nothing_is_pooled() -> None:
    provider = Jev({"country": {"s": ({"COUNTRY": 0.5, "LOCATION": 0.5}, 0.5)}})
    report = decide(provider,
        observed("Country", ControlType.SELECT, "United States", "Canada", field_id="country"),
        observed("Are you legally authorized to work in the United States?", ControlType.SELECT,
                 "Yes", "No", field_id="authorized"))
    [request] = provider.requests
    assert not any(key.startswith("s") for key in request["questions"])
    assert report.field("country").semantic_type is SemanticType.COUNTRY
    assert report.field("authorized").semantic_type is SemanticType.WORK_AUTHORIZATION
    assert report.field("country").semantic_pool_share is None
    assert report.field("authorized").semantic_pool_share is None


def test_a_confident_non_residence_reading_is_never_overridden_by_the_pool() -> None:
    decision = decide(Jev({"answer": {"s": ({"CUSTOM_BOOLEAN": 0.96, "LOCATION": 0.02, "COUNTRY": 0.02}, 0.95)}}),
                      observed("Are you comfortable working across time zones?", ControlType.SELECT,
                               "Yes", "No")).fields[0]
    assert decision.semantic_type is SemanticType.CUSTOM_BOOLEAN
    assert decision.semantic_pool_share == pytest.approx(0.04)


def test_custom_thresholds_move_the_pooled_gate_and_invalidate_the_cache() -> None:
    wide = {"COUNTRY": 0.50, "LOCATION": 0.46, "CUSTOM_BOOLEAN": 0.04}
    provider = Jev({"answer": {"s": (wide, 0.6)}})
    r = router(provider)
    form = ApplicationForm(url="https://synthetic.test/apply",
                           fields=[observed(LIVE, ControlType.SELECT, "Yes", "No")])
    strict = r.classify_form(form, document_id="thresholds")
    assert strict.fields[0].semantic_type is SemanticType.UNKNOWN
    r.thresholds = RouteThresholds(max_pool_outside_probability=0.05)
    relaxed = r.classify_form(form, document_id="thresholds")
    assert relaxed.fields[0].semantic_type is SemanticType.COUNTRY
    assert relaxed.context_hash != strict.context_hash and len(provider.requests) == 2
    # A stricter probability gate holds the pilot's exact 0.95 split.
    r.thresholds = RouteThresholds(probability=0.97)
    provider.scripts = {"answer": {"s": (SPLIT, 0.61)}}
    assert r.classify_form(form, document_id="thresholds").fields[0].semantic_type is SemanticType.UNKNOWN


def test_held_decisions_record_no_pool_shares() -> None:
    report = decide(Jev(status=500),
                    observed(LIVE, ControlType.SELECT, "Yes", "No", field_id="live"),
                    observed("Resume / or drag and drop here", ControlType.FILE, field_id="resume"))
    for decision in report.fields:
        assert decision.route is FieldRoute.AMBIGUOUS and decision.proposed_route is None
        assert decision.semantic_pool_share is None and decision.document_pool_share is None
    assert report.field("resume").semantic_type is SemanticType.UNKNOWN  # not an explicit type


# --- resume pool --------------------------------------------------------------------------

@pytest.mark.parametrize("field", [
    observed("Upload your CV", ControlType.FILE),
    observed("Résumé", ControlType.FILE),
    observed("RÉSUMÉ (PDF only)", ControlType.FILE),
    observed("Curriculum Vitae", ControlType.FILE),
    # Only the label says resume (a custom uploader the heuristics left untyped).
    observed("Resume", ControlType.FILE, semantic=SemanticType.UNKNOWN),
    # Only the type says resume (the inspector read it from the input's name).
    observed("Attach a file", ControlType.FILE, semantic=SemanticType.RESUME),
])
def test_resume_labels_and_types_join_the_pool(field: ApplicationField) -> None:
    decision = decide(Jev({"answer": {"r": ROUTE_099, "d": (PURPOSE_SPLIT, 0.55)}}), field).fields[0]
    assert decision.route is FieldRoute.APPROVED_DOCUMENT
    assert decision.semantic_type is SemanticType.RESUME
    assert decision.document_pool_share == pytest.approx(0.98)


@pytest.mark.parametrize("field,route", [
    # A combined resume and cover letter upload is typed cover letter: not the resume pool.
    (observed("Resume / Cover Letter", ControlType.FILE), FieldRoute.AMBIGUOUS),
    (observed("Upload your CV", ControlType.FILE, required=False), FieldRoute.AMBIGUOUS),
    (observed("Attach a file", ControlType.FILE), FieldRoute.AMBIGUOUS),
])
def test_uploads_outside_the_resume_pool_keep_the_per_choice_gate(
    field: ApplicationField, route: FieldRoute,
) -> None:
    decision = decide(Jev({"answer": {"r": ROUTE_099, "d": (PURPOSE_SPLIT, 0.55)}}), field).fields[0]
    assert decision.route is route and decision.autofill is False
    assert decision.document_pool_share == pytest.approx(0.98)  # recorded for every file control


def test_an_unsupported_resume_widget_has_no_purpose_and_no_pool() -> None:
    field = ApplicationField(id="answer", selector="#answer", label="Resume",
                             control_type=ControlType.UNSUPPORTED, required=True)
    provider = Jev({"answer": {"r": ROUTE_099}})
    decision = decide(provider, field).fields[0]
    assert "d0" not in provider.requests[0]["questions"]
    assert decision.route is FieldRoute.UNSUPPORTED
    assert decision.document_purpose is None and decision.document_pool_share is None


@pytest.mark.parametrize("route,proposed", [
    ("HUMAN_INPUT", FieldRoute.HUMAN_INPUT),
    (({"APPROVED_DOCUMENT": 0.70, "UNSUPPORTED": 0.30}, 0.70), FieldRoute.APPROVED_DOCUMENT),  # below the gate
])
def test_the_pool_approves_a_required_resume_whatever_route_jev_proposed(
    route: Spec, proposed: FieldRoute,
) -> None:
    decision = decide(Jev({"answer": {"r": route, "d": (PURPOSE_SPLIT, 0.55)}}),
                      observed("Resume / or drag and drop here", ControlType.FILE)).fields[0]
    assert decision.route is FieldRoute.APPROVED_DOCUMENT
    assert decision.proposed_route is proposed  # Jev's own route stays in the report
    assert decision.source_requirement == "approved document with canonical content/hash verification"


# --- report serialization and cache ------------------------------------------------------

def test_reports_with_pool_shares_round_trip_and_older_reports_still_load() -> None:
    report = decide(Jev({"live": {"s": (SPLIT, 0.61)},
                         "resume": {"r": ROUTE_099, "d": (PURPOSE_SPLIT, 0.55)}}),
                    observed(LIVE, ControlType.SELECT, "Yes", "No", field_id="live"),
                    observed("Resume / or drag and drop here", ControlType.FILE, field_id="resume"))
    assert FormRouteReport.model_validate(report.model_dump(mode="json")) == report
    assert FormRouteReport.model_validate_json(report.model_dump_json()) == report
    dumped = report.model_dump(mode="json")
    assert dumped["fields"][0]["semantic_pool_share"] == pytest.approx(0.95)
    assert dumped["fields"][1]["document_pool_share"] == pytest.approx(0.98)
    # A report recorded before these fields existed still loads, with empty defaults.
    for key in ("batches", "options_per_field"):
        dumped.pop(key)
    for decision in dumped["fields"]:
        for key in ("semantic_pool_share", "document_pool_share", "demoted_from"):
            decision.pop(key)
    older = FormRouteReport.model_validate(dumped)
    assert (older.batches, older.options_per_field) == (0, 40)
    assert all(isinstance(d, FieldRouteDecision) and d.semantic_pool_share is None
               and d.document_pool_share is None and d.demoted_from is None for d in older.fields)


def test_a_cached_report_keeps_its_pool_shares_and_annotation() -> None:
    provider = Jev({"answer": {"s": (SPLIT, 0.61)}})
    r = router(provider)
    form = ApplicationForm(url="https://synthetic.test/apply",
                           fields=[observed(LIVE, ControlType.SELECT, "Yes", "No")])
    annotated = r.annotate(form, document_id="cache")
    again = r.annotate(form, document_id="cache")
    assert len(provider.requests) == 1 and again == annotated
    assert annotated.fields[0].semantic_type is SemanticType.LOCATION
    cached = r.classify_form(form, document_id="cache")
    assert cached.provider_calls == 0
    assert cached.fields[0].semantic_pool_share == pytest.approx(0.95)
    assert r.report_for(form) == r.report_for(annotated)
    assert r.report_for(annotated).fields[0].semantic_type is SemanticType.LOCATION
