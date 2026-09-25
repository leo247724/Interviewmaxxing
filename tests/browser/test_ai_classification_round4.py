"""WP10 round 4, review pass 4.

M4: a required upload whose label merely mentions the resume ("Transcript (do not attach your
resume)", "Portfolio or resume") was approved as the resume on a sure route; the browser
heuristics type any label with "resume" or "CV" RESUME. A label that names another document or
says the upload is not the resume is not the resume: it is never approved as one, and its
RESUME type becomes UNKNOWN, so the resume is never attached to it.

L7: full-form requests used a fresh 60 s ``BoundedDecisions`` per classification, so the
annotator and the resolver's fallback shared no single flight and no cache. One instance is
kept per runtime.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest

from interviewmaxxing_browser.ai import (
    AIFormRouter,
    BoundedDecisions,
    CallBudget,
    DynamicPacketResolver,
)
from interviewmaxxing_browser.ai.classification import FieldRoute
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
    JobRecord,
    PacketContext,
    SemanticType,
)
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

Spec = str | tuple[dict[str, float], float]
DEFAULTS = {"r": "COPY_KNOWN", "n": "literal", "u": "APPLICANT_CURRENT", "s": "CUSTOM_TEXT",
            "d": "APPLICATION_ATTACHMENT"}


def asked(request: dict[str, Any]) -> list[str]:
    """Field ids a full-form request asks about, in form order."""
    indexes = sorted(int(key[1:]) for key in request["questions"] if key[0] == "r" and key[1:].isdigit())
    return [request["state"]["fields"][f"f{i}"]["field_id"] for i in indexes]


class Jev:
    """Scripted Jev. ``scripts[field_id][kind]`` answers a field's route (r), prose (n),
    source (u), semantic (s) or file purpose (d) question: a choice (all mass, confidence
    1.0) or (probabilities, confidence). Other choices pick NONE/UNKNOWN; nouls answer 0.0."""

    def __init__(self, scripts: dict[str, dict[str, Spec]] | None = None) -> None:
        self.scripts = scripts or {}
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def full_form(self) -> list[list[str]]:
        return [asked(request) for request in self.requests if asked(request)]

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        with self._lock:
            self.requests.append(request)
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
                spec = next((c for c in ("NONE", "hold", "UNKNOWN") if c in criteria), criteria[0])
            if isinstance(spec, str):
                probabilities, confidence = {c: float(c == spec) for c in criteria}, 1.0
            else:
                probabilities, confidence = {c: spec[0].get(c, 0.0) for c in criteria}, spec[1]
            answers[name] = {"type": "choice", "confidence": confidence, "probabilities": probabilities,
                             "choice": max(criteria, key=lambda c: probabilities[c])}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


def router(provider: Jev, *, timeout: float = 15) -> AIFormRouter:
    """The runtime's shape: one Jev client (15 s, one attempt) and one shared budget."""
    return AIFormRouter(BoundedDecisions(JevClient(ApiKey("synthetic-test", source="fixture"),
        transport=provider, max_attempts=1, timeout_seconds=timeout), CallBudget()))


def upload(label: str, field_id: str = "answer") -> ApplicationField:
    """A required file control as the inspector reports it: typed by the browser heuristics."""
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label, required=True,
        semantic_type=classify_semantics(label=label, control_type=ControlType.FILE),
        control_type=ControlType.FILE)


def resolve(provider: Jev, candidate: CandidateProfile, job: JobRecord, *fields: ApplicationField,
            ) -> tuple[ApplicationPacket, PacketContext, AIFormRouter]:
    r = router(provider)
    form = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=list(fields)),
                      document_id="wp10-round4")
    ctx = PacketContext(form=form, candidate=candidate, job=job, application=Application(
        id="app-wp10-r4", request_id="request-wp10-r4", job_id=job.id, candidate_id=candidate.id,
        state=ApplicationState.INSPECTING, version=1,
        created_at="2026-09-24T00:00:00Z", updated_at="2026-09-24T00:00:00Z"))
    packet = asyncio.run(DynamicPacketResolver(r.decisions, router=r).resolve(ctx))
    assert ctx.problems(packet) == []
    return packet, ctx, r


SURE_ROUTE = ({"APPROVED_DOCUMENT": 0.99, "AMBIGUOUS": 0.01}, 0.97)
ATTACHMENT = ({"APPLICATION_ATTACHMENT": 0.99, "OTHER_OR_UNCLEAR": 0.01}, 0.99)
ASHBY_SPLIT = ({"APPLICATION_ATTACHMENT": 0.52, "AUTOFILL_PARSER": 0.37, "OTHER_OR_UNCLEAR": 0.11}, 0.52)


# --- M4: an upload that merely mentions the resume ----------------------------------------

NOT_THE_RESUME = [
    "Transcript (do not attach your resume)",
    "Portfolio or resume",
    "CV / Portfolio",
    "Writing sample (not your resume)",
    "References (separate from your CV)",
    "Certificate or resume",
    "Please do not upload your CV here",
    "Don\u2019t attach a résumé here",
]


@pytest.mark.parametrize("label", NOT_THE_RESUME)
@pytest.mark.parametrize("purpose", [ATTACHMENT, ASHBY_SPLIT])
def test_a_label_that_merely_mentions_the_resume_is_not_the_resume(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, purpose: Spec,
) -> None:
    field = upload(label)
    assert field.semantic_type is SemanticType.RESUME  # the heuristics' "resume"/"CV" keyword
    packet, ctx, r = resolve(Jev({"answer": {"r": SURE_ROUTE, "d": purpose}}),
                             fictional_candidate, mock_job, field)
    decision = r.report_for(ctx.form).fields[0]
    assert decision.semantic_type is ctx.form.field("answer").semantic_type is SemanticType.UNKNOWN
    assert decision.autofill is False
    assert "resume" not in decision.reason.casefold()
    # Never the resume: nothing is attached and the required upload waits for the person.
    assert packet.answers == []
    assert [m.field_id for m in packet.missing_inputs] == ["answer"]


def test_a_jev_typed_resume_on_another_documents_label_is_not_the_resume() -> None:
    field = upload("Transcript")
    assert field.semantic_type is SemanticType.UNKNOWN
    r = router(Jev({"answer": {"r": SURE_ROUTE, "d": ATTACHMENT, "s": "RESUME"}}))
    report = r.classify_form(ApplicationForm(url="https://synthetic.test/apply", fields=[field]),
                             document_id="jev-typed")
    assert report.fields[0].semantic_type is SemanticType.UNKNOWN


@pytest.mark.parametrize("label", [
    "Resume/CV", "Upload your resume or CV", "Resume / or drag and drop here", "Attach resume",
    "Resume (PDF only)", "Resume (not required)",  # "not required" does not negate the resume
])
def test_labels_about_the_resume_are_still_attached_on_a_sure_route(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
) -> None:
    packet, ctx, r = resolve(Jev({"answer": {"r": SURE_ROUTE, "d": ASHBY_SPLIT}}),
                             fictional_candidate, mock_job, upload(label))
    decision = r.report_for(ctx.form).fields[0]
    assert (decision.route, decision.semantic_type, decision.autofill) == (
        FieldRoute.APPROVED_DOCUMENT, SemanticType.RESUME, True)
    assert [a.provenance.source for a in packet.answers] == [AnswerSource.RESUME]


# --- L7: one 60 s decisions instance per runtime -------------------------------------------

def test_the_full_form_decisions_are_kept_per_runtime_and_follow_a_new_runtime() -> None:
    r = router(Jev())
    first = r._route_decisions()
    assert first is r._route_decisions() and first is not r.decisions
    assert (first.client.timeout_seconds, first.budget) == (60.0, r.decisions.budget)
    r.decisions = BoundedDecisions(JevClient(ApiKey("synthetic-test", source="fixture"),
        transport=Jev(), max_attempts=1, timeout_seconds=15), CallBudget())
    second = r._route_decisions()
    assert second is not first and second.budget is r.decisions.budget
    assert router(Jev(), timeout=90)._route_decisions().client.timeout_seconds == 90


def form_of(count: int) -> ApplicationForm:
    return ApplicationForm(url="https://synthetic.test/apply", fields=[
        ApplicationField(id=f"q{i}", selector=f"#q{i}", label=f"Custom question {i}?",
                         control_type=ControlType.TEXT, semantic_type=SemanticType.CUSTOM_TEXT)
        for i in range(count)])


def test_repeated_identical_requests_are_answered_from_the_shared_cache() -> None:
    provider = Jev()
    r = router(provider)
    form = form_of(3)
    first = r.classify_form(form, document_id="same")
    r._reports.clear()  # drop the report cache: only the decisions cache can answer now
    again = r.classify_form(form, document_id="same")
    assert len(provider.full_form()) == first.batches == 1
    assert again.fields == first.fields


class Blocking(Jev):
    """Holds the first full-form call until ``release`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        if asked(json.loads(body)) and not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(5)
        return super().__call__(url, headers, body, timeout)


def test_concurrent_classifications_of_one_form_share_a_single_flight() -> None:
    provider = Blocking()
    r = router(provider)
    form = form_of(3)
    reports: list[Any] = []
    workers = [threading.Thread(target=lambda: reports.append(
        r.classify_form(form, document_id="same"))) for _ in range(2)]
    workers[0].start()
    assert provider.entered.wait(5)  # the first request is in flight
    workers[1].start()
    time.sleep(0.2)  # the second classification reaches the same request meanwhile
    provider.release.set()
    for worker in workers:
        worker.join(5)
    assert len(reports) == 2 and reports[0].fields == reports[1].fields
    assert provider.full_form() == [["q0", "q1", "q2"]]  # one provider call for both
