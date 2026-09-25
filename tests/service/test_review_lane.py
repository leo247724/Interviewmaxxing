"""WP11 round 3, the review lane: the review page's answers (form order, provenance,
citations, blanks), approving the reviewed packet, the submission guard, and changing an
answer through the answer path and preparing again.

Every run is fictional (``preparation_support``): the store operations the runner
records, on ``127.0.0.1:9`` pages nothing opens. A submission run is the fictional
submission runner's store operations; nothing is ever sent anywhere.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_core import (
    WORK_AUTHORIZATION_STATUS_QUESTION,
    AnswerScope,
    AnswerSource,
    ApplicationPacket,
    ApplicationState,
    BooleanValue,
    ChoiceValue,
    ControlType,
    LocalPaths,
    PacketAnswer,
    Provenance,
    SavedAnswer,
    SemanticType,
    TextValue,
    UserInput,
)
from interviewmaxxing_service.answers import SCOPE_NOT_OFFERED
from interviewmaxxing_service.review import (
    APPLICATION_ONLY,
    CONTACT_DETAILS_ONLY,
    NARRATIVE_NOT_GLOBAL,
    NO_FILES,
    NO_OPTIONS,
    NO_RECORDS,
    PROVENANCE_LABELS,
    SavedAnswerRef,
    provenance_kind,
)
from interviewmaxxing_service.service import SUBMISSION_OFF

from .conftest import SITE_URL, FakeCandidates, FictionalSite, Harness, serve
from .preparation_support import RunContext, Scenario, answer, make_field, make_form, question
from .test_review_answers import add_saved_answers, seeded

T = ControlType
ST = AnswerSource
S = ApplicationState
NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
QUESTIONS_URL = "http://127.0.0.1:9/fictional-co/4012/apply/questions?src=desk&draft=tok_fic"

CONTACT = make_form(SITE_URL, 0, [
    make_field("fld_c_first", "First name", T.TEXT, SemanticType.FIRST_NAME, required=True),
    make_field("fld_c_resume", "Resume/CV", T.FILE, SemanticType.RESUME, required=True,
               accept=[".pdf"]),
    make_field("fld_c_linkedin", "LinkedIn profile", T.TEXT, SemanticType.LINKEDIN),
], final=False)
QUESTIONS = make_form(QUESTIONS_URL, 1, [
    make_field("fld_q_auth", "Are you legally authorized to work in the United States?",
               T.SELECT, SemanticType.WORK_AUTHORIZATION, required=True,
               options=[("yes", "Yes"), ("no", "No")]),
    make_field("fld_q_salary", "Desired salary", T.TEXT, SemanticType.SALARY_EXPECTATION,
               required=True),
    make_field("fld_q_heard", "How did you hear about us?", T.SELECT,
               SemanticType.REFERRAL_SOURCE, required=True,
               options=[("careers", "Company careers page"), ("board", "Job board"),
                        ("other", "Other")]),
    make_field("fld_q_relocate", "Are you willing to relocate to Fictional City?", T.RADIO,
               SemanticType.RELOCATION, required=True, options=[("y", "Yes"), ("n", "No")]),
    make_field("fld_q_pronouns", "Pronouns", T.TEXT, SemanticType.PRONOUNS),
    make_field("fld_q_years", "Do you have 5+ years of paid media experience?", T.SELECT,
               SemanticType.CUSTOM_BOOLEAN, required=True, options=[("1", "Yes"), ("0", "No")]),
    make_field("fld_q_why", "Why are you a good fit for this role?", T.TEXTAREA,
               SemanticType.CUSTOM_LONG_TEXT, required=True),
    make_field("fld_q_start", "Earliest start date", T.TEXT, SemanticType.START_DATE,
               help_text="Month and year."),
    make_field("fld_q_consent", "I agree to the fictional privacy notice", T.CHECKBOX,
               SemanticType.CONSENT, required=True),
    make_field("fld_q_github", "GitHub profile", T.TEXT, SemanticType.GITHUB),
], final=True)

NARRATIVE = (
    "For seven years I have run fictional paid media programs, most recently cutting a "
    "fictional retailer's cost per acquisition by a third.\n\n"
    "Your role asks for someone who owns budgets end to end, which is the work I do now."
)
NARRATIVE_NOTE = (
    "Opus draft with per-sentence citations and question completeness checked by Jev"
    "; job context (not candidate facts): "
    + json.dumps([{"id": "jd_fic_req_2", "source_version": "v1"}], sort_keys=True)
    + "; story evidence (the candidate's own account, not canonical facts): "
    + json.dumps([{"id": "story_fic_chunk_7", "source_version": "s3"}], sort_keys=True)
)
SAVED = [
    SavedAnswer(id="simple_answer_fic_pronouns", scope=AnswerScope.GLOBAL,
                semantic_type=SemanticType.PRONOUNS, question="Pronouns", value="they/them",
                confirmed_at=NOW),
    SavedAnswer(id="simple_answer_fic_referral", scope=AnswerScope.GLOBAL,
                semantic_type=SemanticType.REFERRAL_SOURCE,
                question="How did you hear about this job?", value="Company careers page",
                confirmed_at=NOW),
    SavedAnswer(id="sa_fic_consent", scope=AnswerScope.GLOBAL, semantic_type=SemanticType.CONSENT,
                question="I agree to the fictional privacy notice", value=True, confirmed_at=NOW),
    SavedAnswer(id="sa_fic_status", scope=AnswerScope.GLOBAL,
                question=WORK_AUTHORIZATION_STATUS_QUESTION, value="us_citizen", confirmed_at=NOW),
    SavedAnswer(id="simple_answer_fic_salary", scope=AnswerScope.GLOBAL,
                semantic_type=SemanticType.SALARY_EXPECTATION, question="Desired salary",
                value="150,000 USD per year", confirmed_at=NOW),
]
ROW_ORDER = [
    "First name", "Resume/CV", "LinkedIn profile",
    "Are you legally authorized to work in the United States?", "Desired salary",
    "How did you hear about us?", "Are you willing to relocate to Fictional City?", "Pronouns",
    "Do you have 5+ years of paid media experience?", "Why are you a good fit for this role?",
    "Earliest start date", "I agree to the fictional privacy notice", "GitHub profile",
]
KINDS = {
    "First name": "identity",
    "Resume/CV": "resume",
    "LinkedIn profile": "blank",
    "Are you legally authorized to work in the United States?": "derived",
    "Desired salary": "derived",
    "How did you hear about us?": "saved_policy",
    "Are you willing to relocate to Fictional City?": "user",
    "Pronouns": "saved_answer",
    "Do you have 5+ years of paid media experience?": "fact_screener",
    "Why are you a good fit for this role?": "narrative",
    "Earliest start date": "user",
    "I agree to the fictional privacy notice": "saved_policy",
    "GitHub profile": "blank",
}


def contact_answers(ctx: RunContext) -> list[PacketAnswer]:
    f = CONTACT
    return [
        answer(f, "fld_c_first", TextValue(text="Avery"), ST.PROFILE_IDENTITY,
               note="verified identity: first_name"),
        answer(f, "fld_c_resume", ctx.pinned_resume(), ST.RESUME, [ctx.pinned_resume().artifact.id]),
    ]


def question_answers(*, why: str = NARRATIVE) -> dict[str, PacketAnswer]:
    """The questions page's answers that don't come from the person."""
    f = QUESTIONS
    return {a.field_id: a for a in [
        answer(f, "fld_q_auth", ChoiceValue(value="yes", label="Yes"), ST.SAVED_ANSWER,
               ["sa_fic_status"], note="derived from the stated U.S. work authorization status (table)"),
        answer(f, "fld_q_salary", TextValue(text="150000"), ST.SAVED_ANSWER,
               ["simple_answer_fic_salary"],
               note="derived from the saved desired salary for 'Desired salary'"),
        answer(f, "fld_q_heard", ChoiceValue(value="careers", label="Company careers page"),
               ST.SAVED_ANSWER, ["simple_answer_fic_referral"], confidence=0.97,
               note="standing referral answer 'How did you hear about this job?'"),
        answer(f, "fld_q_pronouns", TextValue(text="they/them"), ST.SAVED_ANSWER,
               ["simple_answer_fic_pronouns"], note="saved answer for 'Pronouns'"),
        answer(f, "fld_q_years", ChoiceValue(value="1", label="Yes"), ST.GENERATED_FROM_FACTS,
               ["fact_fic_paid_media_years"], confidence=0.93,
               note="yes/no experience answered YES by Jev from verified facts"),
        answer(f, "fld_q_why", TextValue(text=why), ST.GENERATED_FROM_FACTS,
               ["fact_fic_role", "fact_fic_cpa"], confidence=0.86, note=NARRATIVE_NOTE),
        answer(f, "fld_q_consent", BooleanValue(checked=True), ST.SAVED_ANSWER,
               ["sa_fic_consent"],
               note="saved statement 'I agree to the fictional privacy notice' fully covers "
                    "this statement (Jev)"),
    ]}


def in_order(by_field: dict[str, PacketAnswer]) -> list[PacketAnswer]:
    return [by_field[f.id] for f in QUESTIONS.fields if f.id in by_field]


def asks(ctx: RunContext) -> None:
    """Run 1: the contact page, then the questions page stops for two questions."""
    ctx.fill(CONTACT, contact_answers(ctx))
    ctx.budget(calls=3, known_cost_usd=0.05, unknown_cost_calls=1)
    ctx.ask(QUESTIONS, in_order(question_answers()),
            [question(QUESTIONS, "fld_q_relocate"), question(QUESTIONS, "fld_q_start")])


def prepares(*, user_fields: tuple[str, ...] = ()) -> Any:
    """A preparing run: both pages, the person's answers where they gave one, then the
    final review step with the pages recorded as the runner records them."""

    def run(ctx: RunContext) -> None:
        contact = ctx.fill(CONTACT, contact_answers(ctx))
        answers = question_answers()
        for field_id in ("fld_q_relocate", "fld_q_start", *user_fields):
            answers[field_id] = ctx.from_user(QUESTIONS, field_id)
        questions = ctx.fill(QUESTIONS, in_order(answers))
        ctx.budget(calls=31, known_cost_usd=0.42)
        ctx.prepare(QUESTIONS, questions, steps=[(CONTACT, contact), (QUESTIONS, questions)])

    return run


@dataclass
class Flow:
    h: Harness
    scenario: Scenario
    app_id: str

    @property
    def packets(self) -> list[ApplicationPacket]:
        """The pages of the latest preparing run, in step order."""
        return self.scenario.contexts[-1].packets

    def review(self) -> dict[str, Any]:
        got = self.h.client.get(f"/applications/{self.app_id}/review")
        assert got.status == 200, got.json
        body: dict[str, Any] = got.json
        return body

    def row(self, question: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        rows = (body or self.review())["answers"]
        [found] = [r for r in rows if r["question"] == question]
        return dict(found)

    def approve(self, packet_id: str | None = None) -> Any:
        return self.h.client.post(f"/applications/{self.app_id}/approve", {
            "packetId": packet_id or self.review()["preparedPacketId"]})

    def prepare_again(self) -> None:
        resumed = self.h.client.post(f"/applications/{self.app_id}/resume", {})
        assert resumed.status == 200, resumed.json
        self.scenario.settle(self.h)


def run_flow(h: Harness, scenario: Scenario) -> Flow:
    """Run 1 stops for two questions, the person answers them, run 2 prepares."""
    scenario.then(asks, prepares())
    started = h.start()
    assert started.status == 201, started.json
    app_id = str(started.json["id"])
    scenario.settle(h)
    add_saved_answers(h, SAVED)
    asked = h.client.get(f"/applications/{app_id}").json
    questions = {q["label"]: q for q in asked["needs"]["questions"]}
    relocate = questions["Are you willing to relocate to Fictional City?"]
    start = questions["Earliest start date"]
    saved = h.client.post(f"/applications/{app_id}/answers", {
        "answers": {relocate["id"]: "y", start["id"]: "January 2027"}, "attestations": {}})
    assert saved.status == 200 and saved.json["needs"]["errors"] == {}, saved.json
    flow = Flow(h, scenario, app_id)
    flow.prepare_again()
    return flow


@pytest.fixture
def scenario(isolated_imx_home: LocalPaths) -> Scenario:
    return Scenario(isolated_imx_home)


def _serve(paths: LocalPaths, site: FictionalSite, scenario: Scenario, root: Path,
           **config: Any) -> Any:
    return serve(paths, site, FakeCandidates(root / "private-profile"),
                 executor_factory=scenario.factory, submission_factory=scenario.factory, **config)


@pytest.fixture
def flow(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, scenario: Scenario,
    tmp_path: Path,
) -> Iterator[Flow]:
    """A prepared application in a service without submission (the default)."""
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "private-profile"),
               executor_factory=scenario.factory) as h:
        yield run_flow(h, scenario)


@pytest.fixture
def live_flow(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, scenario: Scenario,
    tmp_path: Path,
) -> Iterator[Flow]:
    """A prepared application in a service started with submission enabled."""
    with _serve(isolated_imx_home, fictional_site, scenario, tmp_path, allow_submission=True) as h:
        yield run_flow(h, scenario)


# --- the review page ---------------------------------------------------------------------------


def test_review_lists_every_question_in_form_order_with_its_provenance(flow: Flow) -> None:
    body = flow.review()
    assert body["stage"] == "prepared"
    assert body["application"]["id"] == flow.app_id
    assert body["application"]["preparation"]["ready"] is True
    rows = body["answers"]
    assert [r["question"] for r in rows] == ROW_ORDER
    assert {r["question"]: r["provenance"]["kind"] for r in rows} == KINDS
    assert all(r["provenance"]["label"] == PROVENANCE_LABELS[r["provenance"]["kind"]] for r in rows)
    assert [r["page"] for r in rows] == [1] * 3 + [2] * 10

    # Blank rows: the optional questions nothing answered.
    for blank in ("LinkedIn profile", "GitHub profile"):
        row = flow.row(blank, body)
        assert row["value"] is None and row["confidence"] is None and row["required"] is False
        assert row["provenance"] == {"kind": "blank", "label": "Left blank", "detail": None}
        assert row["edit"]["value"] is None and row["questionId"]

    # The narrative in full, its cited facts, passages and job evidence, and the
    # resolver's note without the citation lists.
    why = flow.row("Why are you a good fit for this role?", body)
    assert why["value"] == NARRATIVE and why["control"] == "long_text"
    assert why["citations"] == {"facts": ["fact_fic_role", "fact_fic_cpa"],
                                "passages": ["story_fic_chunk_7"],
                                "jobEvidence": ["jd_fic_req_2"]}
    assert why["provenance"]["detail"] == (
        "Opus draft with per-sentence citations and question completeness checked by Jev")
    screener = flow.row("Do you have 5+ years of paid media experience?", body)
    assert screener["citations"] == {"facts": ["fact_fic_paid_media_years"], "passages": [],
                                     "jobEvidence": []}
    assert screener["value"] == "Yes" and screener["confidence"] == 0.93
    derived = flow.row("Desired salary", body)
    assert derived["provenance"]["detail"] == (
        "derived from the saved desired salary for 'Desired salary'")
    assert derived["citations"] is None

    # What the approval would pin, and what it cost.
    assert body["preparedPacketId"] == flow.packets[-1].id
    assert body["approval"] is None and body["changedSincePreparation"] is False
    assert body["providerCost"] == {"knownUsd": 0.47, "calls": 34, "unknownCostCalls": 1}
    assert body["editNote"] is None
    # No local paths, and no draft token: evidence descriptions name page addresses only.
    assert str(flow.h.paths.home) not in json.dumps(body)
    assert "tok_fic" not in json.dumps(body)
    [shot] = body["application"]["preparation"]["evidence"]
    assert shot["value"] == (
        "prepared-review at http://127.0.0.1:9/fictional-co/4012/apply/questions (screenshot)")


def test_each_provenance_kind_is_read_from_the_answer_and_its_saved_record() -> None:
    saved = {
        "sa_global": SavedAnswerRef(id="sa_global", question="Q", scope=AnswerScope.GLOBAL),
        "sa_job": SavedAnswerRef(id="sa_job", question="Q", scope=AnswerScope.JOB),
        "simple_answer_x": SavedAnswerRef(id="simple_answer_x", question="Q",
                                          scope=AnswerScope.GLOBAL),
    }

    def kind(source: AnswerSource, refs: list[str] | None = None, note: str | None = None) -> str:
        value = TextValue(text="fictional")
        return provenance_kind(PacketAnswer(
            field_id="f", semantic_type=SemanticType.UNKNOWN, value=value,
            provenance=Provenance(source=source, reference_ids=refs or ["ref"], note=note),
        ), saved)

    assert provenance_kind(PacketAnswer(
        field_id="f", semantic_type=SemanticType.FIRST_NAME, value=TextValue(text="A"),
        provenance=Provenance(source=ST.PROFILE_IDENTITY)), saved) == "identity"
    assert kind(ST.RESUME) == "resume"
    assert kind(ST.USER_INPUT) == "user"
    assert kind(ST.SAVED_ANSWER, ["sa_job"], "saved answer for 'Q'") == "saved_answer"
    assert kind(ST.SAVED_ANSWER, ["simple_answer_x"]) == "saved_answer"
    assert kind(ST.SAVED_ANSWER, ["sa_gone"]) == "saved_answer"  # record not readable
    assert kind(ST.SAVED_ANSWER, ["sa_global"]) == "saved_policy"  # global reuse
    assert kind(ST.SAVED_ANSWER, ["simple_answer_x", "sa_global"]) == "saved_answer"
    assert kind(ST.SAVED_ANSWER, ["policy:experience_yes"]) == "saved_policy"
    assert kind(ST.SAVED_ANSWER, ["simple_answer_x"], "standing referral answer 'Q'") == "saved_policy"
    for note in ("derived from the saved desired salary for 'Q' (converted to per year)",
                 "derived from the stated U.S. work authorization status (Jev)",
                 "pay period stated in the saved answer for 'Q'",
                 "the saved earliest start date for 'Q', in words",
                 "availability bucket containing the saved earliest start date for 'Q'",
                 "derived from the saved work-arrangement preference and the verified city",
                 "saved work-arrangement preference for 'Q' on a work-mode choice",
                 "saved relocation answer for 'Q', used for a relocation question"):
        assert kind(ST.SAVED_ANSWER, ["sa_job"], note) == "derived", note
    assert kind(ST.GENERATED_FROM_FACTS, ["fact"], NARRATIVE_NOTE) == "narrative"
    assert kind(ST.GENERATED_FROM_FACTS, ["fact"],
                "yes/no experience answered NO by Jev from verified facts") == "fact_screener"
    assert kind(ST.GENERATED_FROM_FACTS, ["fact"], "verified facts: years") == "fact_screener"
    assert kind(ST.CANDIDATE_FACT, ["fact"],
                "Jev mapped the question; verified value copied locally") == "fact_screener"


def test_edits_offer_what_the_store_recorded_about_each_question(flow: Flow) -> None:
    body = flow.review()
    # A choice whose options were recorded when it was asked: its options, current value.
    relocate = flow.row("Are you willing to relocate to Fictional City?", body)
    assert relocate["edit"] == {
        "control": "single_select", "options": [{"value": "y", "label": "Yes"},
                                                {"value": "n", "label": "No"}],
        "lookup": False, "attestation": False, "required": True, "value": "y",
        "reuse": ["application", "job", "global"], "note": None}
    # A choice filled without ever being asked: its options weren't recorded.
    for question_text in ("How did you hear about us?",
                          "Are you legally authorized to work in the United States?",
                          "Do you have 5+ years of paid media experience?"):
        row = flow.row(question_text, body)
        assert row["edit"] is None and row["questionId"] is None
        assert row["noEditReason"] == NO_OPTIONS
    resume = flow.row("Resume/CV", body)
    assert resume["edit"] is None and resume["noEditReason"] == NO_FILES
    consent = flow.row("I agree to the fictional privacy notice", body)
    assert consent["edit"]["attestation"] is True and consent["edit"]["value"] is True
    assert consent["edit"]["control"] == "boolean"
    why = flow.row("Why are you a good fit for this role?", body)
    assert why["edit"]["control"] == "long_text" and why["edit"]["value"] == NARRATIVE
    assert why["noEditReason"] is None
    # A written answer drafted for this job isn't offered for every application.
    assert why["edit"]["reuse"] == ["application", "job"]
    assert why["edit"]["note"] == NARRATIVE_NOT_GLOBAL
    first = flow.row("First name", body)
    assert first["edit"]["reuse"] == ["application"]
    assert first["edit"]["note"] == CONTACT_DETAILS_ONLY
    assert flow.row("Desired salary", body)["edit"]["reuse"] == ["application", "job", "global"]


# --- approving -----------------------------------------------------------------------------------


def test_approve_pins_every_page_of_the_reviewed_preparation(flow: Flow) -> None:
    contact, questions = flow.packets
    approved = flow.approve()
    assert approved.status == 200, approved.json
    body = approved.json
    assert body["approval"]["packetId"] == questions.id and body["approval"]["pages"] == 2
    assert body["approval"]["approver"].startswith("dashboard")
    with flow.h.store() as store:
        approval = store.submission_approval(flow.app_id)
        assert approval is not None
        assert [(s.form_step, s.packet_id) for s in approval.steps] == [
            (0, contact.id), (1, questions.id)]
        # Approving submits nothing and keeps the no-submit restriction.
        assert store.is_preparation_only(flow.app_id)
        assert store.list_attempts(flow.app_id) == []
        first = approval.event_id
    again = flow.approve()
    assert again.status == 200
    with flow.h.store() as store:
        repeated = store.submission_approval(flow.app_id)
        assert repeated is not None and repeated.event_id == first
        assert sum(e.event == "application.approved" for e in store.list_events(flow.app_id)) == 1
    queue = flow.h.client.get("/review").json["applications"]
    assert [(i["id"], i["approved"], i["hold"]["kind"]) for i in queue] == [
        (flow.app_id, True, "approved")]


def test_approve_refuses_another_packet_than_the_prepared_one(flow: Flow) -> None:
    contact = flow.packets[0]
    refused = flow.approve(contact.id)
    assert refused.status == 409
    assert "prepared again since this page was loaded" in refused.json["error"]["message"]
    with flow.h.store() as store:
        assert store.submission_approval(flow.app_id) is None


def test_an_edit_lapses_the_approval_until_the_new_preparation_is_approved(flow: Flow) -> None:
    assert flow.approve().status == 200
    why = flow.row("Why are you a good fit for this role?")
    edited = flow.h.client.post(f"/applications/{flow.app_id}/answers", {
        "answers": {why["questionId"]: "A shorter, fictional answer."}, "attestations": {},
        "reuse": {why["questionId"]: "application"}})
    assert edited.status == 200, edited.json
    body = flow.review()
    assert body["approval"] is None and body["changedSincePreparation"] is True
    assert any("Prepare it again" in p for p in body["submit"]["problems"])
    refused = flow.approve()
    assert refused.status == 409 and "Answers changed" in refused.json["error"]["message"]
    [item] = flow.h.client.get("/review").json["applications"]
    assert (item["approved"], item["hold"]["kind"]) == (False, "edited")

    flow.scenario.then(prepares(user_fields=("fld_q_why",)))
    flow.prepare_again()
    fresh = flow.review()
    assert fresh["changedSincePreparation"] is False
    assert fresh["preparedPacketId"] == flow.packets[-1].id != body["preparedPacketId"]
    row = flow.row("Why are you a good fit for this role?", fresh)
    assert row["value"] == "A shorter, fictional answer."
    assert row["provenance"]["kind"] == "user" and row["citations"] is None
    assert flow.approve().status == 200


# --- the submission guard --------------------------------------------------------------------------


def test_submit_is_refused_while_submission_is_off(flow: Flow) -> None:
    assert flow.approve().status == 200
    body = flow.review()
    assert body["submit"]["enabled"] is False and body["submit"]["allowed"] is False
    assert body["submit"]["problems"] == [SUBMISSION_OFF]
    assert body["submit"]["command"] == (
        f"IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit {flow.app_id} --yes")
    refused = flow.h.client.post(f"/applications/{flow.app_id}/submit", {
        "packetId": body["approval"]["packetId"], "confirm": True})
    assert refused.status == 403 and "IMX_ALLOW_SUBMISSION=1" in refused.json["error"]["message"]
    assert flow.h.client.get("/healthz").json["submission"] == "disabled"
    assert flow.scenario.submissions == []
    with flow.h.store() as store:
        events = [e.event for e in store.list_events(flow.app_id)]
        assert "application.submission_authorized" not in events
        assert store.list_attempts(flow.app_id) == []


def test_submit_needs_the_confirmation_an_approval_and_the_approved_packet(
    live_flow: Flow,
) -> None:
    h, app_id = live_flow.h, live_flow.app_id
    assert h.client.get("/healthz").json["submission"] == "enabled"
    body = live_flow.review()
    assert body["submit"]["enabled"] is True and body["submit"]["allowed"] is False
    assert body["submit"]["problems"] == ["Approve this preparation first."]
    packet_id = body["preparedPacketId"]
    not_approved = h.client.post(f"/applications/{app_id}/submit",
                                 {"packetId": packet_id, "confirm": True})
    assert not_approved.status == 409
    assert live_flow.approve().status == 200
    for payload in ({"packetId": packet_id}, {"packetId": packet_id, "confirm": False},
                    {"packetId": packet_id, "confirm": "yes"}, {"packetId": packet_id, "confirm": 1},
                    {"packetId": packet_id, "confirm": "true"},
                    {"packetId": packet_id, "confirm": True, "extra": 1}):
        invalid = h.client.post(f"/applications/{app_id}/submit", payload)
        assert invalid.status == 400, payload
    stale = h.client.post(f"/applications/{app_id}/submit",
                          {"packetId": live_flow.packets[0].id, "confirm": True})
    assert stale.status == 409 and "approval changed" in stale.json["error"]["message"]
    assert live_flow.scenario.submissions == []
    with h.store() as store:
        assert "application.submission_authorized" not in [
            e.event for e in store.list_events(app_id)]
    ready = live_flow.review()
    assert ready["submit"]["allowed"] is True and ready["submit"]["problems"] == []
    assert ready["submit"]["opensBrowser"] is True


def test_submit_sends_exactly_the_approved_packet(live_flow: Flow) -> None:
    h, app_id = live_flow.h, live_flow.app_id
    questions = live_flow.packets[-1]
    assert live_flow.approve().status == 200
    live_flow.scenario.then(lambda ctx: ctx.submit_approved(questions))
    submitted = h.client.post(f"/applications/{app_id}/submit",
                              {"packetId": questions.id, "confirm": True})
    assert submitted.status == 200, submitted.json
    live_flow.scenario.settle(h)
    body = live_flow.review()
    assert body["application"]["state"] == "SUBMITTED"
    assert body["application"]["receipt"]["confirmationReference"] == "FIC-000321"
    assert body["stage"] == "other" and body["submit"]["allowed"] is False
    assert live_flow.scenario.submissions == [app_id]
    # The submission run could act in the visible browser window (the ``--act`` rule).
    assert live_flow.scenario.interactions[-1].allow_browser_action is True
    with h.store() as store:
        events = store.list_events(app_id)
        [authorized] = [e for e in events if e.event == "application.submission_authorized"]
        assert authorized.metadata["packet_id"] == questions.id
        [attempt] = store.list_attempts(app_id)
        assert attempt.packet_id == questions.id
    again = h.client.post(f"/applications/{app_id}/submit",
                          {"packetId": questions.id, "confirm": True})
    assert again.status == 409 and "never submitted again" in again.json["error"]["message"]
    assert h.client.get("/review").json["applications"] == []


def test_a_headless_service_names_the_captcha_it_cannot_show(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, scenario: Scenario,
    tmp_path: Path,
) -> None:
    def captcha_prepares(ctx: RunContext) -> None:
        contact = ctx.fill(CONTACT, contact_answers(ctx))
        answers = question_answers()
        answers["fld_q_relocate"] = answer(QUESTIONS, "fld_q_relocate",
                                           ChoiceValue(value="y", label="Yes"), ST.SAVED_ANSWER,
                                           ["sa_fic_relocate"])
        questions = ctx.fill(QUESTIONS, in_order(answers))
        ctx.prepare(QUESTIONS, questions, captcha_pending=True,
                    steps=[(CONTACT, contact), (QUESTIONS, questions)])

    with _serve(isolated_imx_home, fictional_site, scenario, tmp_path,
                allow_submission=True, headless=True) as h:
        scenario.then(captcha_prepares)
        app_id = str(h.start().json["id"])
        scenario.settle(h)
        flow = Flow(h, scenario, app_id)
        assert flow.approve().status == 200
        body = flow.review()
        assert body["submit"]["opensBrowser"] is False and body["submit"]["allowed"] is False
        assert any("headless" in p for p in body["submit"]["problems"])
        assert body["submit"]["command"].endswith("--yes --act")
        assert body["browser"]["available"] is False
        assert body["browser"]["command"] == f"interviewmaxxing resume {app_id} --act"
        [item] = h.client.get("/review").json["applications"]
        assert item["captchaPending"] is True
        assert item["hold"]["summary"] == (
            "Approved: waiting to be submitted · CAPTCHA to solve when submitting")


def test_a_real_employer_site_is_never_submitted_in_test_only_mode(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, scenario: Scenario,
    tmp_path: Path,
) -> None:
    real = "https://jobs.example.test/fictional-co/4012/apply"
    with _serve(isolated_imx_home, fictional_site, scenario, tmp_path, allow_submission=True) as h:
        form = make_form(real, 0, [make_field("fld_x_why", "Why us?", T.TEXTAREA,
                                              SemanticType.CUSTOM_LONG_TEXT, required=True)],
                         final=True)
        with seeded(h, real) as ctx:
            packet = ctx.fill(form, [answer(form, "fld_x_why", TextValue(text="Fictional."),
                                            ST.GENERATED_FROM_FACTS, ["fact_fic_role"])])
            ctx.prepare(form, packet, steps=[(form, packet)])
            app_id = ctx.app_id
        flow = Flow(h, scenario, app_id)
        assert flow.approve().status == 200
        body = flow.review()
        assert any("TEST_ONLY" in p for p in body["submit"]["problems"])
        assert body["browser"]["available"] is False
        refused = h.client.post(f"/applications/{app_id}/submit",
                                {"packetId": packet.id, "confirm": True})
        assert refused.status == 403 and "TEST_ONLY" in refused.json["error"]["message"]
        assert scenario.submissions == []
        with h.store() as store:
            assert "application.submission_authorized" not in [
                e.event for e in store.list_events(app_id)]


# --- changing an answer ------------------------------------------------------------------------------


def test_an_edit_is_saved_as_the_persons_answer_to_that_exact_question(flow: Flow) -> None:
    h, app_id = flow.h, flow.app_id
    salary = flow.row("Desired salary")
    relocate = flow.row("Are you willing to relocate to Fictional City?")
    github = flow.row("GitHub profile")
    saved = h.client.post(f"/applications/{app_id}/answers", {
        "answers": {salary["questionId"]: "160000", relocate["questionId"]: "n",
                    github["questionId"]: "https://github.example.test/avery"},
        "attestations": {},
        "reuse": {salary["questionId"]: "global", relocate["questionId"]: "job"},
    })
    assert saved.status == 200, saved.json
    questions = flow.packets[-1]
    with h.store() as store:
        inputs = {u.field_id: u for u in store.list_user_inputs(app_id)}
    by_field = {f.id: f for f in QUESTIONS.fields}
    for field_id, value in (("fld_q_salary", TextValue(text="160000")),
                            ("fld_q_relocate", ChoiceValue(value="n", label="No")),
                            ("fld_q_github", TextValue(text="https://github.example.test/avery"))):
        given: UserInput = inputs[field_id]
        assert given.value == value
        assert given.form_step == 1 and given.field_fingerprint == by_field[field_id].fingerprint
        assert given.matches(QUESTIONS), field_id  # the next preparation fills it in
    assert inputs["fld_q_salary"].question == "Desired salary"
    assert inputs["fld_q_relocate"].question == by_field["fld_q_relocate"].question_text
    # Saved for reuse as asked: salary for every job, relocation for this job; the
    # GitHub answer stays with this application.
    candidates: FakeCandidates = h.candidates
    reused = {a.question: a for a in candidates.saved_answers}
    assert reused["Desired salary"].scope is AnswerScope.GLOBAL
    assert reused["Desired salary"].value == "160000"
    assert reused[by_field["fld_q_relocate"].question_text].scope is AnswerScope.JOB
    assert reused[by_field["fld_q_relocate"].question_text].value == "No"
    assert len(reused) == 2
    assert questions.answer_for("fld_q_salary") is not None  # the prepared packet is untouched


def test_an_edit_that_does_not_fit_answers_422_and_saves_nothing(flow: Flow) -> None:
    h, app_id = flow.h, flow.app_id
    relocate = flow.row("Are you willing to relocate to Fictional City?")
    consent = flow.row("I agree to the fictional privacy notice")
    first = flow.row("First name")
    with h.store() as store:
        before = len(store.list_user_inputs(app_id))
    bad_choice = h.client.post(f"/applications/{app_id}/answers", {
        "answers": {relocate["questionId"]: "maybe"}, "attestations": {}})
    assert bad_choice.status == 422
    assert bad_choice.json["error"]["fieldErrors"] == {
        relocate["questionId"]: "Choose one of the listed options."}
    declined = h.client.post(f"/applications/{app_id}/answers", {
        "answers": {}, "attestations": {consent["questionId"]: False}})
    assert declined.status == 422
    assert "Accept this statement" in declined.json["error"]["fieldErrors"][consent["questionId"]]
    line_break = h.client.post(f"/applications/{app_id}/answers", {
        "answers": {first["questionId"]: "Avery\nQuill"}, "attestations": {}})
    assert line_break.status == 422
    # A consent sent as an answer, or a question the review doesn't offer, is stale.
    misplaced = h.client.post(f"/applications/{app_id}/answers", {
        "answers": {consent["questionId"]: True}, "attestations": {}})
    assert misplaced.status == 409
    unknown = h.client.post(f"/applications/{app_id}/answers", {
        "answers": {"q_" + "0" * 32: "x"}, "attestations": {}})
    assert unknown.status == 409
    with h.store() as store:
        assert len(store.list_user_inputs(app_id)) == before
        assert store.submission_approval(app_id) is None
        assert "input.received" not in [
            e.event for e in store.list_events(app_id)][-3:]


def test_reuse_needs_the_full_wording_of_the_question(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, scenario: Scenario,
    tmp_path: Path,
) -> None:
    long_label = ("Please describe, in your own words and with one concrete fictional example, how "
                  "you would plan the first ninety days of this role")
    form = make_form(SITE_URL, 0, [
        make_field("fld_l_plan", long_label, T.TEXTAREA, SemanticType.CUSTOM_LONG_TEXT,
                   required=True),
        make_field("fld_l_city", "City", T.TEXT, SemanticType.CITY, required=True),
        make_field("fld_l_notice", "Notice period", T.TEXT, SemanticType.START_DATE),
    ], final=True)

    def prepares_long(ctx: RunContext) -> None:
        packet = ctx.fill(form, [
            answer(form, "fld_l_plan", TextValue(text="Listen, then plan."),
                   ST.GENERATED_FROM_FACTS, ["fact_fic_role"], note=NARRATIVE_NOTE),
            answer(form, "fld_l_city", TextValue(text="Fictional City"), ST.PROFILE_IDENTITY),
            answer(form, "fld_l_notice", TextValue(text="Two weeks"), ST.SAVED_ANSWER,
                   ["simple_answer_fic_notice"]),
        ])
        ctx.prepare(form, packet, steps=[(form, packet)])

    with _serve(isolated_imx_home, fictional_site, scenario, tmp_path) as h:
        scenario.then(prepares_long)
        app_id = str(h.start().json["id"])
        scenario.settle(h)
        flow = Flow(h, scenario, app_id)
        body = flow.review()
        plan = body["answers"][0]
        assert plan["question"].endswith("…") and len(plan["question"]) == 80
        assert plan["edit"]["reuse"] == ["application"]
        assert plan["edit"]["note"] == APPLICATION_ONLY
        # A contact detail is kept for this application only too: a reusable answer
        # would shadow the verified profile on every form.
        city = body["answers"][1]
        assert city["edit"]["reuse"] == ["application"]
        assert city["edit"]["note"] == CONTACT_DETAILS_ONLY
        assert body["answers"][2]["edit"]["reuse"] == ["application", "job", "global"]
        assert body["answers"][2]["edit"]["note"] is None
        refused = h.client.post(f"/applications/{app_id}/answers", {
            "answers": {plan["questionId"]: "Listen first."}, "attestations": {},
            "reuse": {plan["questionId"]: "global"}})
        assert refused.status == 422
        assert refused.json["error"]["fieldErrors"] == {plan["questionId"]: SCOPE_NOT_OFFERED}
        contact = h.client.post(f"/applications/{app_id}/answers", {
            "answers": {city["questionId"]: "Another Fictional City"}, "attestations": {},
            "reuse": {city["questionId"]: "global"}})
        assert contact.status == 422
        kept = h.client.post(f"/applications/{app_id}/answers", {
            "answers": {plan["questionId"]: "Listen first."}, "attestations": {},
            "reuse": {plan["questionId"]: "application"}})
        assert kept.status == 200, kept.json
        candidates: FakeCandidates = h.candidates
        assert candidates.saved_answers == []


def test_an_older_preparation_lists_its_answers_but_offers_no_edits(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, scenario: Scenario,
    tmp_path: Path,
) -> None:
    def prepares_without_steps(ctx: RunContext) -> None:
        ctx.fill(CONTACT, contact_answers(ctx))
        answers = question_answers()
        answers["fld_q_relocate"] = answer(QUESTIONS, "fld_q_relocate",
                                           ChoiceValue(value="y", label="Yes"), ST.SAVED_ANSWER,
                                           ["sa_fic_relocate"])
        packet = ctx.fill(QUESTIONS, in_order(answers))
        ctx.prepare(QUESTIONS, packet)  # recorded before preparations named their steps

    with _serve(isolated_imx_home, fictional_site, scenario, tmp_path) as h:
        scenario.then(prepares_without_steps)
        app_id = str(h.start().json["id"])
        scenario.settle(h)
        body = Flow(h, scenario, app_id).review()
        assert body["editNote"] == NO_RECORDS
        rows = body["answers"]
        assert [r["question"] for r in rows][:2] == ["First name", "Resume"]
        assert not any(r["provenance"]["kind"] == "blank" for r in rows)
        assert all(r["edit"] is None and r["questionId"] is None for r in rows)
        assert all(r["noEditReason"] == NO_RECORDS for r in rows)


def test_a_browser_step_is_offered_in_the_browser_or_as_a_command(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, scenario: Scenario,
    tmp_path: Path,
) -> None:
    with _serve(isolated_imx_home, fictional_site, scenario, tmp_path) as h:
        scenario.then(lambda ctx: ctx.sign_in())
        app_id = str(h.start().json["id"])
        scenario.settle(h)
        body = Flow(h, scenario, app_id).review()
        assert body["stage"] == "browser_action"
        assert body["browser"] == {"available": True, "reason": None,
                                   "command": f"interviewmaxxing resume {app_id} --act"}
        assert body["application"]["needs"]["kind"] == "interaction"
        assert body["answers"] == [] and body["preparedPacketId"] is None
        assert body["submit"]["allowed"] is False
        [item] = h.client.get("/review").json["applications"]
        assert (item["stage"], item["hold"]) == (
            "browser_action", {"kind": "sign_in", "summary": "Sign-in needed in the browser"})
        # Resume in browser: the run may act in the visible window, then prepares.
        def prepares_after_sign_in(ctx: RunContext) -> None:
            contact = ctx.fill(CONTACT, contact_answers(ctx))
            answers = question_answers()
            answers["fld_q_relocate"] = answer(QUESTIONS, "fld_q_relocate",
                                               ChoiceValue(value="y", label="Yes"),
                                               ST.SAVED_ANSWER, ["sa_fic_relocate"])
            questions = ctx.fill(QUESTIONS, in_order(answers))
            ctx.prepare(QUESTIONS, questions, steps=[(CONTACT, contact), (QUESTIONS, questions)])

        scenario.then(prepares_after_sign_in)
        resumed = h.client.post(f"/applications/{app_id}/resume", {})
        assert resumed.status == 200
        scenario.settle(h)
        assert scenario.interactions[-1].allow_browser_action is True
        assert Flow(h, scenario, app_id).review()["stage"] == "prepared"


def test_review_routes_refuse_what_they_do_not_serve(flow: Flow) -> None:
    h = flow.h
    assert h.client.get("/applications/not%20a%20valid%20id/review").status == 400
    assert h.client.get("/applications/app_missing/review").status == 404
    assert h.client.get("/review?stage=prepared").status == 400
    assert h.client.request("POST", "/review", b"{}", {"Content-Type": "application/json"}).status == 405
    foreign = h.client.post(f"/applications/{flow.app_id}/approve",
                            {"packetId": "pkt_x"}, origin="http://evil.example.test")
    assert foreign.status == 403
    with h.store() as store:
        assert store.submission_approval(flow.app_id) is None


def test_an_application_that_is_not_prepared_shows_its_answers_without_edits(flow: Flow) -> None:
    def fails(ctx: RunContext) -> None:
        ctx.fill(CONTACT, contact_answers(ctx))
        ctx.fail("Stopped by a fictional browser error.")

    flow.scenario.then(fails)
    flow.prepare_again()
    body = flow.review()
    assert body["application"]["state"] == "FAILED_RETRYABLE"
    assert body["stage"] == "other" and body["preparedPacketId"] is None
    assert [row["question"] for row in body["answers"]] == ["First name", "Resume"]
    assert all(row["edit"] is None and row["questionId"] is None and row["noEditReason"] is None
               for row in body["answers"])
    assert body["editNote"] == "Only a prepared application's answers can be changed here."
    assert "Only an application prepared to its final review step and approved can be submitted." \
        in body["submit"]["problems"]
    assert flow.h.client.get("/review").json["applications"] == []
    edit = flow.h.client.post(f"/applications/{flow.app_id}/answers", {
        "answers": {"q_" + "0" * 32: "x"}, "attestations": {}})
    assert edit.status == 409
