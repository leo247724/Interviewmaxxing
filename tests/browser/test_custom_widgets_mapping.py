"""The applicant's "United States" becomes "United States +1", never "Canada +1".

End to end on the ``react-select`` scenario, whose phone "Country" select lists both
"United States +1" and "Canada +1" and shows only "+1" after a choice: the real
inspection (a probed menu), WP2's router and ``DynamicPacketResolver`` (option
equivalence) and the browser's select-and-verify on the live localhost mock page.

The Jev decision itself is answered by a scripted transport, as in WP2's own tests (no
provider, no credentials, no network): it picks the option whose label is the scripted
one. What is verified is everything around that decision: the deterministic mapper
picks neither "+1" option by itself, the question Jev receives, the answer the resolver
builds from Jev's key, and the fill and readback through the shared "+1" display.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.ai import (
    AIFormRouter,
    BoundedDecisions,
    CallBudget,
    DynamicPacketResolver,
)
from interviewmaxxing_core import (
    AnswerScope,
    AnswerSource,
    Application,
    ApplicationForm,
    ApplicationState,
    BrowserOptions,
    CandidateProfile,
    ChoiceValue,
    FieldFillStatus,
    JobRecord,
    PacketContext,
    SavedAnswer,
    SelectiveFill,
    SemanticType,
    utc_now,
)
from interviewmaxxing_generation.values import match_options
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

REACT = "/jobs/react-select/apply"
US, CANADA = "United States +1", "Canada +1"


class ScriptedJev:
    """The Jev decision API, scripted: every field is the applicant's literal current
    datum (COPY_KNOWN, APPLICANT_CURRENT), an option-equivalence question is answered
    with the option labelled ``label``, and anything else is held."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 1.0}
                continue
            criteria = list(question["criteria"])
            if name.startswith("equivalent_"):
                choice = next(key for key, label in request["state"]["options"].items()
                              if label == self.label)
            elif name[0] in "rnud" and name[1:].isdigit():
                choice = {"r": "COPY_KNOWN", "n": "literal", "u": "APPLICANT_CURRENT",
                          "d": "APPLICATION_ATTACHMENT"}[name[0]]
            else:
                choice = next((k for k in ("hold", "NONE", "UNKNOWN") if k in criteria), criteria[0])
            answers[name] = {"type": "choice", "choice": choice, "confidence": 0.99,
                             "probabilities": {key: float(key == choice) for key in criteria}}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0}}).encode())

    def asked(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if name in r["questions"]]


def _context(form: ApplicationForm, candidate: CandidateProfile, job: JobRecord) -> PacketContext:
    app = Application(id="app-country-mapping", request_id="request-country-mapping", job_id=job.id,
                      candidate_id=candidate.id, state=ApplicationState.INSPECTING, version=2,
                      created_at="2026-09-24T00:00:00Z", updated_at="2026-09-24T00:00:00Z")
    return PacketContext(application=app, job=job, form=form, candidate=candidate)


@pytest.mark.parametrize("source", ["identity", "saved answer"])
def test_united_states_maps_to_the_us_dial_code_option_and_is_read_back(
    source: str, kit: SimpleNamespace, server: Any, options: BrowserOptions,
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url(REACT))
            transport = ScriptedJev(US)
            router = AIFormRouter(BoundedDecisions(JevClient(
                ApiKey("synthetic-key", source="test"), transport=transport, max_attempts=1),
                CallBudget()))
            form = router.annotate(page.form, document_id="react-select-country")
            candidate = fictional_candidate
            if source == "saved answer":
                candidate = candidate.model_copy(update={"saved_answers": [
                    *candidate.saved_answers,
                    SavedAnswer(id="sa.phone_country", scope=AnswerScope.GLOBAL,
                                question=form.field("question_6004").question_text,
                                value="United States", confirmed_at=utc_now())]})
            assert candidate.identity.address is not None
            assert candidate.identity.address.country == "United States"
            resolver = DynamicPacketResolver(router.decisions, router=router)
            packet = await resolver.resolve(_context(form, candidate, mock_job))
            fill = await browser.fill_fields(form, packet, ["question_6004"])
            state = await browser.page.evaluate("() => window.__widgetState.question_6004")
            shown = await browser.page.evaluate(
                "() => document.querySelector('.select__single-value').textContent")
            return form, packet, fill, state, shown, transport, isinstance(browser, SelectiveFill)
        finally:
            await browser.close()

    form, packet, fill, state, shown, transport, selective = kit.run(scenario())
    country = form.field("question_6004")
    assert country.semantic_type is SemanticType.COUNTRY
    assert {US, CANADA} <= {o.label for o in country.options or []}
    # Spelling alone matches neither "+1" option. Until WP2 round 11 the mapping was one Jev
    # option decision; the closed country vocabulary now maps "United States" onto its dial-code
    # option without a call, so Jev is asked only when that deterministic match finds nothing.
    assert match_options(country, "United States") == []
    asked = transport.asked("equivalent_0")
    if asked:
        [request] = asked
        assert request["state"]["stored_answers"] == {"equivalent_0": "United States"}
        assert {US, CANADA} <= set(request["state"]["options"].values())
    answer = packet.answer_for("question_6004")
    assert answer is not None and answer.value == ChoiceValue(value=US, label=US)
    expected = AnswerSource.PROFILE_IDENTITY if source == "identity" else AnswerSource.SAVED_ANSWER
    assert answer.provenance.source is expected
    # Filled through the shared "+1" display and confirmed by the reopened menu.
    assert selective and [(f.field_id, f.status) for f in fill.fields] == [
        ("question_6004", FieldFillStatus.FILLED)]
    assert state == {"value": "us"} and shown == "+1"
