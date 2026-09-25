"""WP10 round 3: a full-form request that times out is retried once, halved.

Live (Greenhouse, Pomelo): one of three batched ``full_form_routes`` calls ended in "Jev
TIMEOUT" and its six fields, First Name, Email and Phone among them, were held. A timed-out
request is now sent once more as two halves (a single field as itself) before any field is
held; ``retries`` records it, and every call is reserved in the shared budget. Full-form
calls get a per-call timeout of at least 60 s (the runtime's Jev client uses 15 s).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from interviewmaxxing_browser.ai import (
    AIFormRouter,
    BoundedDecisions,
    CallBudget,
    DynamicPacketResolver,
)
from interviewmaxxing_browser.ai.classification import ROUTES_TIMEOUT_SECONDS, FieldRoute
from interviewmaxxing_browser.semantics import classify as classify_semantics
from interviewmaxxing_core import (
    AnswerSource,
    Application,
    ApplicationField,
    ApplicationForm,
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

DEFAULTS = {"r": "COPY_KNOWN", "n": "literal", "u": "APPLICANT_CURRENT", "s": "CUSTOM_BOOLEAN",
            "d": "APPLICATION_ATTACHMENT"}
TimesOut = Callable[[int, list[str]], bool]
"""(1-based full-form call number, the field ids it asks about) -> raise a timeout?"""


def asked(request: dict[str, Any]) -> list[str]:
    """Field ids a full-form request asks about, in form order."""
    indexes = sorted(int(key[1:]) for key in request["questions"] if key[0] == "r" and key[1:].isdigit())
    return [request["state"]["fields"][f"f{i}"]["field_id"] for i in indexes]


class Jev:
    """Scripted Jev over HTTP. Full-form requests (route questions ``r<i>``) raise
    ``TimeoutError`` when ``times_out`` says so, or answer with HTTP ``status``; everything
    answers its default choice with all mass (``semantic`` for meanings; other choices
    pick NONE/UNKNOWN, or ``picks``). Every call is recorded with the timeout it was given."""

    def __init__(self, times_out: TimesOut | None = None, *, status: int = 200,
                 picks: dict[str, str] | None = None, semantic: str = "CUSTOM_BOOLEAN") -> None:
        self.times_out, self.status, self.picks = times_out, status, picks or {}
        self.defaults = DEFAULTS | {"s": semantic}
        self.calls: list[tuple[dict[str, Any], float]] = []
        self.full_form = 0

    def full_form_calls(self) -> list[tuple[list[str], float]]:
        return [(asked(request), timeout) for request, timeout in self.calls if asked(request)]

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.calls.append((request, timeout))
        if asked(request):
            self.full_form += 1
            if self.times_out is not None and self.times_out(self.full_form, asked(request)):
                raise TimeoutError("scripted: no answer in time")
            if self.status != 200:
                return HttpResponse(self.status, {}, b"{}")
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.0}
                continue
            criteria = list(question["criteria"])
            choice = (self.defaults[name[0]] if name[0] in DEFAULTS and name[1:].isdigit()
                      else self.picks.get(name, next(
                          (c for c in ("NONE", "hold", "UNKNOWN") if c in criteria), criteria[0])))
            answers[name] = {"type": "choice", "choice": choice, "confidence": 1.0,
                             "probabilities": {c: float(c == choice) for c in criteria}}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


def router(provider: Jev, budget: CallBudget | None = None, *, timeout: float = 15) -> AIFormRouter:
    """The runtime's shape: one Jev client (15 s, one attempt) and one shared budget."""
    return AIFormRouter(BoundedDecisions(JevClient(ApiKey("synthetic-test", source="fixture"),
        transport=provider, max_attempts=1, timeout_seconds=timeout), budget or CallBudget()))


def observed(field_id: str, label: str, control: ControlType, *options: str,
             input_type: str | None = None, required: bool = True) -> ApplicationField:
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label,
        semantic_type=classify_semantics(label=label, control_type=control, input_type=input_type),
        control_type=control, required=required, input_type=input_type,
        options=[FieldOption(value=f"v{i}", label=o) for i, o in enumerate(options)] or None)


PRIOR = {"backend": "greenhouse", "authority": "untrusted_prior_only",
         "field_patterns": [{"label": f"Observed question family {i}: contact, location, EEO and "
                             "custom yes/no questions", "control": "select", "observations": i}
                            for i in range(70)]}


def pomelo_fields() -> list[ApplicationField]:
    """A Greenhouse-shaped form of 16 fields, as the inspector types them."""
    yes_no = ("Yes", "No")
    return [
        observed("first_name", "First Name", ControlType.TEXT),
        observed("last_name", "Last Name", ControlType.TEXT),
        observed("email", "Email", ControlType.TEXT, input_type="email"),
        observed("phone", "Phone", ControlType.TEXT, input_type="tel"),
        observed("city", "Location (City)", ControlType.TEXT),
        observed("resume", "Resume/CV", ControlType.FILE),
        observed("linkedin", "LinkedIn Profile", ControlType.TEXT, required=False),
        *(observed(f"screener_{i}", question, ControlType.SELECT, *yes_no) for i, question in enumerate([
            "Have you managed paid social budgets above $1M a year?",
            "Have you worked in a performance marketing agency environment?",
            "Do you have experience with Google Ads Performance Max?",
            "Have you led a team of five or more marketers?",
            "Have you worked with a B2B SaaS company?",
            "Have you owned a lifecycle email program end to end?"])),
        observed("sponsorship", "Will you now or in the future require visa sponsorship?",
                 ControlType.SELECT, *yes_no),
        observed("source", "How did you hear about this job?", ControlType.SELECT,
                 "LinkedIn", "Company website", "Other"),
        observed("gender", "Gender", ControlType.SELECT, "Male", "Female", "Decline to self-identify",
                 required=False),
    ]


def classify(provider: Jev, fields: list[ApplicationField], budget: CallBudget | None = None,
             ) -> tuple[Any, AIFormRouter]:
    r = router(provider, budget)
    return r.classify_form(ApplicationForm(url="https://synthetic.test/apply", fields=fields),
                           document_id="wp10-round3", schema_hints=PRIOR), r


def test_a_timed_out_batch_is_retried_as_two_halves_and_every_field_is_decided(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    fields = pomelo_fields()
    assert len(fields) == 16
    baseline, _ = classify(Jev(), fields)
    assert baseline.batches == 3 and baseline.retries == 0  # three batched calls, as live
    # The call that asks about First Name times out once, as Pomelo's did.
    provider = Jev(lambda number, ids: "first_name" in ids and number == 1)
    report, r = classify(provider, fields)
    calls = provider.full_form_calls()
    timed_out = calls[0][0]
    assert {"first_name", "email", "phone"} <= set(timed_out)
    # Retried once as its two halves, in order, before the rest of the form.
    middle = len(timed_out) // 2
    assert [ids for ids, _ in calls[1:3]] == [timed_out[:middle], timed_out[middle:]]
    assert report.retries == 1 and report.batches == baseline.batches + 2 == len(calls)
    # Both the timed-out call and its halves are reserved and recorded in the budget.
    budget = r.decisions.budget
    assert budget.calls == report.provider_calls == len(calls)
    assert [receipt.status for receipt in budget.receipts].count("TIMEOUT") == 1
    assert all(d.proposed_route is not None for d in report.fields)  # nothing left UNKNOWN
    for field_id in ("first_name", "email", "phone"):
        assert report.field(field_id).route is FieldRoute.COPY_KNOWN
        assert report.field(field_id).profile_copy_allowed
    # End to end, the contact fields are copied from the verified identity.
    form = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=fields),
                      document_id="wp10-round3", schema_hints=PRIOR)
    ctx = PacketContext(form=form, candidate=fictional_candidate, job=mock_job, application=Application(
        id="app-pomelo", request_id="request-pomelo", job_id=mock_job.id,
        candidate_id=fictional_candidate.id, state=ApplicationState.INSPECTING, version=1,
        created_at="2026-09-24T00:00:00Z", updated_at="2026-09-24T00:00:00Z"))
    packet = asyncio.run(DynamicPacketResolver(r.decisions, router=r).resolve(ctx))
    assert ctx.problems(packet) == []
    copied = {a.field_id: a for a in packet.answers if a.provenance.source is AnswerSource.PROFILE_IDENTITY}
    identity = fictional_candidate.identity
    assert copied["first_name"].value.text == identity.first_name
    assert copied["email"].value.text == identity.email
    assert "phone" in copied


def test_halves_that_time_out_again_are_held_after_one_retry() -> None:
    fields = [observed(f"q{i}", f"Custom question {i}?", ControlType.TEXT) for i in range(4)]
    provider = Jev(lambda number, ids: True)  # Jev never answers in time
    report, r = classify(provider, fields)
    assert [ids for ids, _ in provider.full_form_calls()] == [
        ["q0", "q1", "q2", "q3"], ["q0", "q1"], ["q2", "q3"]]  # one retry, never a second
    assert (report.retries, report.batches, r.decisions.budget.calls) == (1, 3, 3)
    assert all(d.route is FieldRoute.AMBIGUOUS and d.reason == "Jev TIMEOUT"
               and d.semantic_type is SemanticType.UNKNOWN for d in report.fields)


def test_a_single_field_that_times_out_is_retried_once_as_itself() -> None:
    field = observed("first_name", "First Name", ControlType.TEXT)
    provider = Jev(lambda number, ids: number == 1)
    report, _ = classify(provider, [field])
    assert [ids for ids, _ in provider.full_form_calls()] == [["first_name"], ["first_name"]]
    assert (report.retries, report.batches, report.provider_calls) == (1, 2, 2)
    assert report.fields[0].route is FieldRoute.COPY_KNOWN


def test_other_provider_failures_are_held_without_a_retry() -> None:
    fields = [observed(f"q{i}", f"Custom question {i}?", ControlType.TEXT) for i in range(2)]
    provider = Jev(status=503)
    report, _ = classify(provider, fields)
    assert len(provider.full_form_calls()) == 1
    assert report.retries == 0
    assert {d.reason for d in report.fields} == {"Jev UNAVAILABLE"}


def test_a_retry_the_budget_cannot_pay_for_is_held_with_the_budget_reason() -> None:
    fields = [observed(f"q{i}", f"Custom question {i}?", ControlType.TEXT) for i in range(4)]
    provider = Jev(lambda number, ids: number == 1)
    report, r = classify(provider, fields, CallBudget(max_calls=2))
    assert [ids for ids, _ in provider.full_form_calls()] == [["q0", "q1", "q2", "q3"], ["q0", "q1"]]
    assert report.retries == 1 and r.decisions.budget.calls == 2
    assert [d.proposed_route is not None for d in report.fields] == [True, True, False, False]
    assert {report.field(f"q{i}").reason for i in (2, 3)} == {"AI call or cost budget exhausted"}


@pytest.mark.parametrize("client_timeout,routes_timeout", [(15, 60.0), (90, 90)])
def test_full_form_calls_get_at_least_sixty_seconds_and_other_calls_keep_the_client_timeout(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    client_timeout: float, routes_timeout: float,
) -> None:
    assert ROUTES_TIMEOUT_SECONDS == 60.0
    field = observed("reside", "Do you currently reside in the US?", ControlType.SELECT, "Yes", "No")
    # Typed LOCATION by Jev, so the residence screener makes a second, ordinary Jev call.
    provider = Jev(picks={"residence": "o0"}, semantic="LOCATION")
    r = router(provider, timeout=client_timeout)
    form = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=[field]),
                      document_id="timeouts")
    ctx = PacketContext(form=form, candidate=fictional_candidate, job=mock_job, application=Application(
        id="app-timeouts", request_id="request-timeouts", job_id=mock_job.id,
        candidate_id=fictional_candidate.id, state=ApplicationState.INSPECTING, version=1,
        created_at="2026-09-24T00:00:00Z", updated_at="2026-09-24T00:00:00Z"))
    packet = asyncio.run(DynamicPacketResolver(r.decisions, router=r).resolve(ctx))
    assert [a.value.label for a in packet.answers] == ["Yes"]
    timeouts = {("full_form" if asked(request) else next(iter(request["questions"]))): timeout
                for request, timeout in provider.calls}
    assert timeouts == {"full_form": routes_timeout, "residence": client_timeout}
    assert r.decisions.client.timeout_seconds == client_timeout  # the runtime client is unchanged
