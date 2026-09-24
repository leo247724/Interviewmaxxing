"""Blank-form corpus adapter and prediction export; never reads evaluation labels."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from interviewmaxxing_core import (
    EXPLICIT_ANSWER_REQUIRED,
    ApplicationField,
    ApplicationForm,
    ControlType,
    FieldOption,
)
from interviewmaxxing_core.forms import CHOICE_CONTROLS

from ..semantics import classify
from .classification import AIFormRouter, FieldRoute, FieldRouteDecision
from .providers import AIHold, BoundedDecisions


def corpus_form(rows: list[dict[str, Any]]) -> tuple[ApplicationForm, dict[str, Any]]:
    """Use only observed options. Missing values never become invented option IDs.

    Fixture selectors are inert and never passed to a browser. The actual observed
    widget/option labels remain untrusted context for semantic evaluation.
    """
    fields = []
    original = {}
    for row in rows:
        raw_kind = row["control_type"]
        input_type = row.get("input_type")
        if raw_kind in ("EMAIL", "TEL", "URL", "NUMBER", "DATE", "PASSWORD"):
            input_type = raw_kind.lower()
        elif not input_type and row.get("original_control_type") in ("email", "tel", "url", "number", "date", "password"):
            input_type = row["original_control_type"]
        kind = ControlType.TEXT if input_type else ControlType(raw_kind)
        options = row.get("options") or []
        original[row["id"]] = {"original_control_type": row.get("original_control_type"),
            "observed_options": [{"label": o.get("label"), "value": o.get("value")} for o in options],
            "options_complete": row.get("options_complete", False),
            "observed_context": row.get("context") or ""}
        if kind in CHOICE_CONTROLS and (not options or not row.get("options_complete")
                                      or any(o.get("value") is None for o in options)
                                      or len({o.get("value") for o in options}) != len(options)):
            kind = ControlType.UNSUPPORTED
        kwargs: dict[str, Any] = {}
        if kind in CHOICE_CONTROLS:
            kwargs["options"] = [FieldOption(value=o["value"], label=o["label"],
                                             disabled=o.get("disabled", False)) for o in options]
        if kind is ControlType.FILE:
            accept = row.get("accept")
            kwargs["accept"] = [p.strip() for p in accept.split(",") if p.strip()] if isinstance(accept, str) else accept
        label, help_text = row.get("question") or "", row.get("description") or ""
        semantic = classify(label=label, help_text=help_text,
            name=row.get("source_field_id") or "", input_type=input_type, control_type=kind)
        fields.append(ApplicationField(id=row["id"], selector=f"[data-eval-id='{row['id']}']",
            label=label, help_text=help_text or None, placeholder=row.get("placeholder"),
            semantic_type=semantic, control_type=kind, input_type=input_type, required=bool(row.get("required")),
            max_length=row.get("max_length"), **kwargs))
    return ApplicationForm(url=rows[0].get("source_url") or "https://synthetic.test/apply",
                           fields=fields), {"observed_widget_metadata": original}


def predict_corpus(rows: list[dict[str, Any]], router: AIFormRouter,
                   *, run_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Full-form predictions; expected/gold fields are never inspected or sent."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["form_id"]].append(row)
    predictions = []
    timings = []
    for form_id, group in groups.items():
        started = time.monotonic()
        form, hints = corpus_form(group)
        report = router.classify_form(form, document_id=f"{run_id}:{form_id}", schema_hints=hints)
        classification_elapsed = time.monotonic() - started
        readiness_receipt_start = len(router.decisions.budget.receipts)
        for row in group:
            decision = report.field(row["id"])
            semantic_route = decision.semantic_route or decision.route
            current_field = form.field(row["id"]).model_copy(update={"semantic_type": decision.semantic_type})
            readiness = canonical_readiness(row, current_field, decision.route, url=form.url,
                                             decisions=router.decisions, gate=decision)
            predictions.append({"id": row["id"], "form_id": form_id,
                "semantic_route": semantic_route.value, "policy_route": decision.route.value,
                **readiness,
                "proposed_route": decision.proposed_route.value if decision.proposed_route else None,
                "semantic_type": decision.semantic_type.value, "confidence": decision.confidence,
                "probabilities": decision.probabilities,
                "narrative_probability": decision.narrative_probability,
                "narrative_confidence": decision.narrative_confidence,
                "narrative_probabilities": decision.narrative_probabilities,
                "source_scope": decision.source_scope.value,
                "source_scope_confidence": decision.source_scope_confidence,
                "source_scope_probabilities": decision.source_scope_probabilities,
                "profile_copy_allowed": decision.profile_copy_allowed,
                "document_purpose": decision.document_purpose.value if decision.document_purpose else None,
                "document_purpose_probabilities": decision.document_purpose_probabilities,
                "actuator": "UNSUPPORTED" if form.field(row["id"]).control_type is ControlType.UNSUPPORTED else "SUPPORTED",
                "context_hash": report.context_hash, "reason": decision.reason,
                "model": report.model, "resolved_model": report.resolved_model,
                "prompt_version": report.prompt_version, "batch_size": router.batch_size})
        elapsed = time.monotonic() - started
        extra = router.decisions.budget.receipts[readiness_receipt_start:]
        timings.append({"form_id": form_id, "fields": len(group), "wall_seconds": elapsed,
            "classification_seconds": classification_elapsed,
            "readiness_seconds": elapsed - classification_elapsed,
            "fields_per_second": len(group) / elapsed if elapsed else None,
            "provider_calls": report.provider_calls + len(extra),
            "provider_seconds": report.latency_seconds + sum(r.latency_seconds for r in extra),
            "known_cost_usd": report.known_cost_usd + sum(r.cost_usd for r in extra if r.cost_usd is not None),
            "unknown_cost_calls": report.unknown_cost_calls + sum(r.cost_usd is None for r in extra),
            "cost_usd": None if report.unknown_cost_calls or any(r.cost_usd is None for r in extra)
                        else report.known_cost_usd + sum(r.cost_usd for r in extra if r.cost_usd is not None),
            "batch_size": router.batch_size})
    return predictions, timings


def canonical_readiness(row: dict[str, Any], fld: ApplicationField,
                        route: FieldRoute, *, url: str, decisions: BoundedDecisions | None = None,
                        gate: FieldRouteDecision | None = None) -> dict[str, Any]:
    """Exercise the canonical factual resolver with exactly the fictional sources
    supplied for this question. Placeholder identity values are never eligible.
    No model or browser call is made; unavailable documents are never fabricated.
    """
    from interviewmaxxing_core import (
        AnswerScope,
        AnswerSource,
        Application,
        ApplicationState,
        CandidateFact,
        CandidateIdentity,
        CandidateProfile,
        JobRecord,
        PacketContext,
        ResumeArtifact,
        SavedAnswer,
    )
    from interviewmaxxing_generation.resolver import resolve_packet
    from interviewmaxxing_generation.values import Mapped, translate

    hold: dict[str, Any] = {"final_route": "HUMAN_INPUT", "answer_readiness": "HUMAN_INPUT",
            "answer_source": None, "canonical_problems": []}
    if fld.control_type is ControlType.UNSUPPORTED:
        return hold | {"final_route": "UNSUPPORTED"}
    sources = [s for s in row.get("facts_available", [])
               if s.get("fictional") is True and s.get("verified") is True
               and s.get("value") is not None]
    explicit = [s for s in sources if s.get("source_kind") == "saved_answer"]
    fact_sources = [s for s in sources if s.get("source_kind") == "verified_fact"]
    if not explicit and route is FieldRoute.WRITER:
        if gate is None or gate.source_scope.value in ("EXPLICIT_ANSWER", "UNCLEAR") or (gate.source_scope_confidence or 0) < .9 or gate.source_scope_probabilities.get(gate.source_scope.value, 0) < .95:
            return hold | {"readiness_reason": "Narrative source applicability requires a scoped explicit answer or clarification"}
        return hold | {"final_route": "WRITER", "answer_readiness": "WRITER_REQUIRED"}
    if not explicit and route is not FieldRoute.COPY_KNOWN:
        return hold
    if not explicit and gate is None:
        return hold | {"readiness_reason": "Source applicability has not been established"}
    now = "2026-09-23T00:00:00Z"
    identity = CandidateIdentity(first_name="Unprovided", last_name="Unprovided",
                                 email="unprovided@synthetic.invalid", verified_at=now)
    identity_attributes = {"FIRST_NAME": "first_name", "LAST_NAME": "last_name",
        "EMAIL": "email", "PHONE": "phone", "PREFERRED_NAME": "preferred_name",
        "LINKEDIN": "linkedin_url", "WEBSITE": "website_url", "GITHUB": "github_url"}
    matching_identity = [s for s in sources if s.get("source_kind") == "identity"
                         and s.get("semantic_type") == fld.semantic_type.value]
    if not explicit and not fact_sources:
        if gate is not None and not gate.profile_copy_allowed:
            return hold | {"readiness_reason": "The question does not permit generic applicant-profile copying"}
        if fld.semantic_type in EXPLICIT_ANSWER_REQUIRED or not matching_identity:
            return hold
        if len({str(s["value"]) for s in matching_identity}) != 1:
            return hold
        raw_value = matching_identity[0]["value"]
        name = identity_attributes.get(fld.semantic_type.value)
        if name:
            identity = CandidateIdentity.model_validate(identity.model_dump() | {name: raw_value})
        elif fld.semantic_type.value == "FULL_NAME":
            words = str(raw_value).split(" ", 1)
            if len(words) != 2:
                return hold
            identity = identity.model_copy(update={"first_name": words[0], "last_name": words[1]})
        else:
            address = {"CITY": "city", "STATE": "region", "ZIP": "postal_code",
                       "COUNTRY": "country", "ADDRESS": "street"}.get(fld.semantic_type.value)
            if not address:
                return hold
            identity = identity.model_copy(update={"address": identity.address.model_copy(
                update={address: raw_value})})
    saved = []
    for i, source in enumerate(explicit):
        # Preserve source scope and wording; never invent permission for this field.
        if (source.get("question") != fld.question_text
                or source.get("semantic_type") != fld.semantic_type.value
                or source.get("scope") not in (AnswerScope.GLOBAL.value, AnswerScope.JOB.value)):
            continue
        try:
            saved.append(SavedAnswer(id=source.get("id") or f"eval_saved_{i}",
                scope=source["scope"], job_identity_key=source.get("job_identity_key"),
                job_url=source.get("job_url"), semantic_type=source["semantic_type"],
                question=source["question"], value=source["value"],
                confirmed_at=source.get("confirmed_at") or now))
        except ValueError:
            continue
    if explicit and not saved:
        return hold | {"readiness_reason": "Saved answer lacks matching explicit source scope/question/semantic"}
    candidate = CandidateProfile(id="eval_candidate", identity=identity,
        resume=ResumeArtifact(id="eval_unavailable_resume", path="/nonexistent/synthetic-resume.pdf",
            filename="unavailable.pdf", media_type="application/pdf", sha256="0" * 64,
            size_bytes=0), saved_answers=saved,
        facts=[CandidateFact(id=s.get("fact_id") or f"eval_fact_{i}",
            key=s.get("key") or s.get("semantic_type", "candidate_fact").lower(),
            value=s["value"], source="fictional evaluation evidence",
            verification={"status": "VERIFIED", "method": "USER_STATED", "verified_at": now})
            for i, s in enumerate(fact_sources)])
    job = JobRecord(id="eval_job", application_url=url, normalized_url=url,
                    identity_key="eval:job", created_at=now, updated_at=now)
    app = Application(id="eval_application", request_id="eval_request", job_id=job.id,
        candidate_id=candidate.id, state=ApplicationState.INSPECTING, version=1,
        created_at=now, updated_at=now)
    form = ApplicationForm(url=url, fields=[fld])
    ctx = PacketContext(application=app, job=job, candidate=candidate, form=form)
    packet = resolve_packet(ctx)
    if (gate is not None and not gate.profile_copy_allowed and packet.answers
            and not explicit):
        packet = packet.model_copy(update={"answers": []})
    if fact_sources and not packet.answers and decisions is not None and gate is not None:
        from .routing import DynamicPacketResolver
        try:
            answer = DynamicPacketResolver(decisions).resolve_verified_fact(ctx, fld, gate=gate)
            packet = packet.model_copy(update={"answers": [answer], "missing_inputs": []})
        except AIHold:
            pass
    if ctx.problems(packet) or not packet.answers:
        return hold | {"canonical_problems": ctx.problems(packet)}
    answer = packet.answers[0]
    # Never allow the placeholder identity to count as a source; compare the exact
    # canonical typed value against an actually supplied matching source as well.
    eligible = (explicit if answer.provenance.source is AnswerSource.SAVED_ANSWER
                else fact_sources if answer.provenance.source is AnswerSource.CANDIDATE_FACT
                else matching_identity)
    translated = [translate(fld, s["value"]) for s in eligible]
    if not any(isinstance(v, Mapped) and v.value == answer.value for v in translated):
        return hold
    return {"final_route": "COPY_KNOWN", "answer_readiness": "READY_FROM_VERIFIED_SOURCE",
            "answer_source": answer.provenance.source.value, "canonical_problems": []}
