"""WP3 review list: the answers the service filled, in form order, with recorded wording
where there is some and never an internal id, note or path."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_core import (
    AnswerScope,
    AnswerSource,
    ApplicationForm,
    BooleanValue,
    CandidateIdentity,
    CandidateProfile,
    ChoiceValue,
    ControlType,
    FieldOption,
    LocalPaths,
    MissingReason,
    MultiChoiceValue,
    PacketAnswer,
    ResumeArtifact,
    SavedAnswer,
    SemanticType,
    TextValue,
    UserInput,
)
from interviewmaxxing_service import ServiceInteraction

from .conftest import SITE_URL, FakeCandidates, FictionalSite, Harness, serve
from .preparation_support import (
    RunContext,
    Scenario,
    answer,
    make_field,
    make_form,
    question,
)
from .test_http_flow import answer_all, poll

T = ControlType
ST = AnswerSource
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
REVIEW_KEYS = {"question", "wordingRecorded", "page", "control", "value", "source", "confidence"}
QUESTIONS_URL = "http://127.0.0.1:9/fictional-co/4012/apply/questions?src=desk"

PROFILE_PAGE = make_form(SITE_URL, 0, [
    make_field("fld_rv_first", "Legal first name", T.TEXT, SemanticType.FIRST_NAME,
               required=True),
    make_field("fld_rv_email", "Email address", T.TEXT, SemanticType.EMAIL, required=True),
    make_field("fld_rv_resume", "Resume/CV", T.FILE, SemanticType.RESUME, required=True,
               accept=[".pdf"]),
    make_field("fld_rv_years", "Years in lifecycle marketing", T.TEXT, required=True),
], final=False)
QUESTIONS_PAGE = make_form(QUESTIONS_URL, 1, [
    make_field("fld_rv_auth", "Are you legally authorized to work in the United States?",
               T.RADIO, SemanticType.WORK_AUTHORIZATION, required=True,
               options=[("1", "Yes, I am authorized"), ("0", "No")]),
    make_field("fld_rv_channels", "Which channels have you run?", T.MULTISELECT,
               SemanticType.CUSTOM_MULTISELECT,
               options=[("search", "Paid search"), ("social", "Paid social"),
                        ("display", "Display")]),
    make_field("fld_rv_cover", "Cover letter", T.TEXTAREA, SemanticType.COVER_LETTER),
    make_field("fld_rv_why", "Why do you want to join Fictional Co?", T.TEXTAREA,
               SemanticType.CUSTOM_TEXT, required=True, help_text="A few sentences are enough."),
    # A text area (a single-line field refuses a typed line break, f850949) that is never
    # asked, so only its line break makes it long text.
    make_field("fld_rv_summary", "Summarize a recent campaign", T.TEXTAREA,
               SemanticType.CUSTOM_TEXT),
    make_field("fld_rv_story", "Tell us a short story", T.TEXT, SemanticType.CUSTOM_LONG_TEXT),
    make_field("fld_rv_hobby", "What do you do outside work?", T.TEXTAREA,
               SemanticType.CUSTOM_TEXT),
    make_field("fld_rv_start", "When could you start?", T.TEXT, SemanticType.START_DATE,
               help_text="Month and year."),
    make_field("fld_rv_alerts", "Send me future job alerts", T.CHECKBOX,
               SemanticType.CUSTOM_BOOLEAN),
    make_field("fld_rv_privacy", "I consent to Fictional Co processing my data", T.CHECKBOX,
               SemanticType.CONSENT, required=True, help_text="See the fictional privacy notice."),
    make_field("fld_rv_referrer", "Referrer code", T.TEXT),
], final=True)

SAVED_ANSWERS = [
    SavedAnswer(id="sa_rv_auth", scope=AnswerScope.GLOBAL,
                semantic_type=SemanticType.WORK_AUTHORIZATION,
                question="Are you authorized to work in the US?\nAnswered once for all jobs.",
                value="Yes", confirmed_at=NOW),
    SavedAnswer(id="sa_rv_alerts_primary", scope=AnswerScope.GLOBAL,
                question="Would you like job alerts from employers?", value=False,
                confirmed_at=NOW),
    SavedAnswer(id="sa_rv_alerts_other", scope=AnswerScope.GLOBAL,
                question="Subscribe to the fictional newsletter?", value=False, confirmed_at=NOW),
]
FACT_IDS = ["fact_rv_years", "fact_rv_channels", "fact_rv_cover", "fact_rv_summary",
            "fact_rv_story", "fact_rv_hobby", "fact_rv_start"]
SAVED_IDS = ["sa_rv_auth", "sa_rv_alerts_primary", "sa_rv_alerts_other", "sa_rv_gone"]
NOTE = "RVNOTE"
"""Every provenance note in this module carries this marker; none may reach a view."""


class _PlainName:
    """A plain name for a question whose wording was not recorded: non-empty text that is
    no field id. Only EMAIL, FIRST_NAME, RESUME and UNKNOWN have names the tests pin."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and bool(other.strip()) and "fld_" not in other

    def __hash__(self) -> int:
        return 0

    def __repr__(self) -> str:
        return "<a plain name>"


PLAIN = _PlainName()


# --- fixtures and helpers ------------------------------------------------------------------


@pytest.fixture
def scenario(isolated_imx_home: LocalPaths) -> Scenario:
    return Scenario(isolated_imx_home)


@pytest.fixture
def served(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, scenario: Scenario,
    tmp_path: Path,
) -> Iterator[Harness]:
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "private-profile"),
               executor_factory=scenario.factory) as h:
        yield h


def view(h: Harness, app_id: str) -> dict[str, Any]:
    got = h.client.get(f"/applications/{app_id}")
    assert got.status == 200, got.json
    body: dict[str, Any] = got.json
    return body


def resume(h: Harness, scenario: Scenario, app_id: str) -> None:
    resumed = h.client.post(f"/applications/{app_id}/resume", {})
    assert resumed.status == 200, resumed.json
    scenario.settle(h)


def add_saved_answers(h: Harness, saved: Sequence[SavedAnswer]) -> None:
    """Put ``saved`` into the candidate profile the service loads (``profile_loader``)."""
    cid = h.paths.candidate_id
    candidates: FakeCandidates = h.candidates
    profile = candidates.profiles.get(cid) or CandidateProfile(
        id=cid,
        identity=CandidateIdentity(first_name="Avery", last_name="Example",
                                   email="avery@example.test", verified_at=NOW),
        resume=ResumeArtifact(id="res_rv_seed", path=str(candidates.root / "seed.pdf"),
                              filename="Avery Example.pdf", media_type="application/pdf",
                              sha256="0" * 64, size_bytes=1),
    )
    candidates.profiles[cid] = profile.model_copy(
        update={"saved_answers": [*profile.saved_answers, *saved]}
    )


@contextmanager
def seeded(h: Harness, url: str) -> Iterator[RunContext]:
    """Direct store seeding of one prepare-only application of this candidate."""
    with h.store() as store:
        app = store.record_request(h.paths.candidate_id, url).application
        claim = store.claim(app.id, "fictional-seed")
        try:
            store.require_preparation_only(claim)
            yield RunContext(store=store, claim=claim, paths=h.paths,
                             interaction=ServiceInteraction(allow_browser_action=False), index=1)
        finally:
            store.release(claim)


def wording(item: dict[str, Any]) -> tuple[str, bool]:
    return item["question"], item["wordingRecorded"]


def profile_answers(ctx: RunContext) -> list[PacketAnswer]:
    f = PROFILE_PAGE
    return [
        answer(f, "fld_rv_first", TextValue(text="Avery"), ST.PROFILE_IDENTITY),
        answer(f, "fld_rv_email", TextValue(text="avery@example.test"), ST.PROFILE_IDENTITY,
               note=f"{NOTE} verified email"),
        answer(f, "fld_rv_resume", ctx.pinned_resume(), ST.RESUME,
               [ctx.pinned_resume().artifact.id]),
        answer(f, "fld_rv_years", TextValue(text="7"), ST.CANDIDATE_FACT, ["fact_rv_years"],
               confidence=0.9, note=f"{NOTE} years from a verified fact"),
    ]


def in_form_order(form: ApplicationForm, by_field: dict[str, PacketAnswer]) -> list[PacketAnswer]:
    return [by_field[f.id] for f in form.fields if f.id in by_field]


def resolved_answers() -> dict[str, PacketAnswer]:
    """Answers on the questions page that do not come from the user."""
    f = QUESTIONS_PAGE
    return {a.field_id: a for a in [
        answer(f, "fld_rv_auth", ChoiceValue(value="1", label="Yes, I am authorized"),
               ST.SAVED_ANSWER, ["sa_rv_auth"], confidence=0.97,
               note=f"{NOTE} option wording mapped"),
        answer(f, "fld_rv_channels", MultiChoiceValue(choices=[
            FieldOption(value="search", label="Paid search"),
            FieldOption(value="social", label="Paid social"),
        ]), ST.CANDIDATE_FACT, ["fact_rv_channels"], confidence=0.88),
        answer(f, "fld_rv_cover", TextValue(text="I led fictional lifecycle programs."),
               ST.GENERATED_FROM_FACTS, ["fact_rv_cover"], confidence=0.8,
               note=f"{NOTE} drafted"),
        answer(f, "fld_rv_summary", TextValue(text="Line one.\nLine two."),
               ST.GENERATED_FROM_FACTS, ["fact_rv_summary"], confidence=0.75),
        answer(f, "fld_rv_story", TextValue(text="A short fictional story."),
               ST.GENERATED_FROM_FACTS, ["fact_rv_story"], confidence=0.7),
        answer(f, "fld_rv_hobby", TextValue(text="Cartography."), ST.GENERATED_FROM_FACTS,
               ["fact_rv_hobby"], confidence=0.65),
        answer(f, "fld_rv_start", TextValue(text="January 2027"), ST.GENERATED_FROM_FACTS,
               ["fact_rv_start"], confidence=0.85),
        answer(f, "fld_rv_alerts", BooleanValue(checked=False), ST.SAVED_ANSWER,
               ["sa_rv_alerts_primary", "sa_rv_alerts_other"]),
        answer(f, "fld_rv_referrer", TextValue(text="FIC-REF-2"), ST.SAVED_ANSWER,
               ["sa_rv_gone"], note=f"{NOTE} saved answer since deleted"),
    ]}


# --- the full flow: questions, answers, then a prepared final review ----------------------------


@dataclass
class PreparedFlow:
    h: Harness
    scenario: Scenario
    app_id: str
    body: dict[str, Any]
    raw: str


@pytest.fixture
def prepared_flow(served: Harness, scenario: Scenario) -> PreparedFlow:
    """Run 1 fills page 1 and stops on page 2 with questions; the user answers two; run 2
    fills both pages from the start and stops prepared at the final review step."""

    def asks(ctx: RunContext) -> None:
        ctx.fill(PROFILE_PAGE, profile_answers(ctx))
        resolved = resolved_answers()
        del resolved["fld_rv_start"]  # not known yet: asked below
        ctx.ask(QUESTIONS_PAGE, in_form_order(QUESTIONS_PAGE, resolved),
                [question(QUESTIONS_PAGE, "fld_rv_why"),
                 question(QUESTIONS_PAGE, "fld_rv_start"),
                 question(QUESTIONS_PAGE, "fld_rv_privacy",
                          reason=MissingReason.EXPLICIT_ANSWER_REQUIRED)])

    def prepares(ctx: RunContext) -> None:
        ctx.fill(PROFILE_PAGE, profile_answers(ctx))
        packet = ctx.fill(QUESTIONS_PAGE, in_form_order(QUESTIONS_PAGE, {
            **resolved_answers(),
            "fld_rv_why": ctx.from_user(QUESTIONS_PAGE, "fld_rv_why"),
            "fld_rv_privacy": ctx.from_user(QUESTIONS_PAGE, "fld_rv_privacy"),
        }))
        ctx.prepare(QUESTIONS_PAGE, packet)

    scenario.then(asks, prepares)
    started = served.start()
    assert started.status == 201, started.json
    app_id = str(started.json["id"])
    scenario.settle(served)
    add_saved_answers(served, SAVED_ANSWERS)

    asked = view(served, app_id)
    assert asked["needs"]["kind"] == "questions"
    [why] = [q for q in asked["needs"]["questions"] if q["control"] == "long_text"]
    [privacy] = asked["needs"]["attestations"]
    saved = served.client.post(f"/applications/{app_id}/answers", {
        "answers": {why["id"]: "I want to build calm lifecycle programs."},
        "attestations": {privacy["id"]: True},
    })
    assert saved.status == 200 and saved.json["needs"]["errors"] == {}, saved.json
    resume(served, scenario, app_id)

    raw = served.client.get(f"/applications/{app_id}")
    assert raw.status == 200
    body: dict[str, Any] = raw.json
    assert body["preparation"] is not None, body["state"]
    return PreparedFlow(served, scenario, app_id, body, raw.body.decode())


def test_review_covers_every_page_of_the_preparing_run(prepared_flow: PreparedFlow) -> None:
    review = prepared_flow.body["review"]
    assert all(set(item) == REVIEW_KEYS for item in review)
    assert review == [
        {"question": "First name", "wordingRecorded": False, "page": 1, "control": "text",
         "value": "Avery", "source": "identity", "confidence": 1.0},
        {"question": "Email", "wordingRecorded": False, "page": 1, "control": "text",
         "value": "avery@example.test", "source": "identity", "confidence": 1.0},
        {"question": "Resume", "wordingRecorded": False, "page": 1, "control": "file",
         "value": "Avery Example.pdf", "source": "resume", "confidence": 1.0},
        {"question": "Question on the form", "wordingRecorded": False, "page": 1,
         "control": "text", "value": "7", "source": "fact", "confidence": 0.9},
        # The saved answer's own wording (first line).
        {"question": "Are you authorized to work in the US?", "wordingRecorded": True,
         "page": 2, "control": "single_select", "value": "Yes, I am authorized",
         "source": "saved_answer", "confidence": 0.97},
        {"question": PLAIN, "wordingRecorded": False, "page": 2, "control": "multi_select",
         "value": ["Paid search", "Paid social"], "source": "fact", "confidence": 0.88},
        # A cover letter is long text by type.
        {"question": PLAIN, "wordingRecorded": False, "page": 2, "control": "long_text",
         "value": "I led fictional lifecycle programs.", "source": "generated",
         "confidence": 0.8},
        # Recorded as a TEXTAREA when it was asked; the user's own wording (first line).
        {"question": "Why do you want to join Fictional Co?", "wordingRecorded": True,
         "page": 2, "control": "long_text", "value": "I want to build calm lifecycle programs.",
         "source": "user", "confidence": 1.0},
        # Text with a line break is long text.
        {"question": PLAIN, "wordingRecorded": False, "page": 2, "control": "long_text",
         "value": "Line one.\nLine two.", "source": "generated", "confidence": 0.75},
        # Long text by type.
        {"question": PLAIN, "wordingRecorded": False, "page": 2, "control": "long_text",
         "value": "A short fictional story.", "source": "generated", "confidence": 0.7},
        # A TEXTAREA that was never asked (so never recorded) is plain text.
        {"question": PLAIN, "wordingRecorded": False, "page": 2, "control": "text",
         "value": "Cartography.", "source": "generated", "confidence": 0.65},
        # Asked (recorded) in the earlier stop, filled from facts in the preparing run.
        {"question": "When could you start?", "wordingRecorded": True, "page": 2,
         "control": "text", "value": "January 2027", "source": "generated", "confidence": 0.85},
        # Two saved answers referenced: the first one's wording.
        {"question": "Would you like job alerts from employers?", "wordingRecorded": True,
         "page": 2, "control": "boolean", "value": "No", "source": "saved_answer",
         "confidence": 1.0},
        {"question": "I consent to Fictional Co processing my data", "wordingRecorded": True,
         "page": 2, "control": "boolean", "value": "Yes", "source": "user", "confidence": 1.0},
        # The saved answer is no longer in the profile: no wording was recorded.
        {"question": "Question on the form", "wordingRecorded": False, "page": 2,
         "control": "text", "value": "FIC-REF-2", "source": "saved_answer", "confidence": 1.0},
    ]


def test_review_carries_no_ids_notes_or_resume_path(prepared_flow: PreparedFlow) -> None:
    raw, h, app_id = prepared_flow.raw, prepared_flow.h, prepared_flow.app_id
    with h.store() as store:
        user_input_ids = [u.id for u in store.list_user_inputs(app_id)]
        pinned = store.pinned_resume(app_id)
    assert pinned is not None and len(user_input_ids) == 2
    forbidden = {
        *FACT_IDS, *SAVED_IDS, *user_input_ids, NOTE,
        *(f.id for f in (*PROFILE_PAGE.fields, *QUESTIONS_PAGE.fields)),
        pinned.path, pinned.id,
    }
    leaked = sorted(item for item in forbidden if item in raw)
    assert leaked == []
    # The whole review serializes as plain JSON values.
    assert json.loads(json.dumps(prepared_flow.body["review"])) == prepared_flow.body["review"]


# --- which packets the review uses ---------------------------------------------------------------

STEP0 = make_form(SITE_URL, 0, [
    make_field("fld_rs_first", "First name", T.TEXT, SemanticType.FIRST_NAME, required=True),
], final=False)
STEP1 = make_form(QUESTIONS_URL, 1, [
    make_field("fld_rs_city", "City", T.TYPEAHEAD, SemanticType.CITY, required=True),
    make_field("fld_rs_motto", "Your motto", T.TEXT, SemanticType.CUSTOM_TEXT, required=True),
], final=False)
STEP2 = make_form(QUESTIONS_URL + "&page=3", 2, [
    make_field("fld_rs_email", "Email", T.TEXT, SemanticType.EMAIL, required=True),
], final=True)


def first_name(value: str) -> PacketAnswer:
    return answer(STEP0, "fld_rs_first", TextValue(text=value), ST.PROFILE_IDENTITY)


def city(value: str) -> PacketAnswer:
    return answer(STEP1, "fld_rs_city", TextValue(text=value), ST.PROFILE_IDENTITY)


def motto(value: str) -> PacketAnswer:
    return answer(STEP1, "fld_rs_motto", TextValue(text=value), ST.GENERATED_FROM_FACTS,
                  ["fact_rs_motto"], confidence=0.7)


def email() -> PacketAnswer:
    return answer(STEP2, "fld_rs_email", TextValue(text="avery@example.test"),
                  ST.PROFILE_IDENTITY)


def pages_and_values(review: list[dict[str, Any]]) -> list[tuple[int, Any]]:
    return [(item["page"], item["value"]) for item in review]


def test_review_takes_the_latest_packet_of_each_step_of_the_run(
    served: Harness, scenario: Scenario
) -> None:
    def run(ctx: RunContext) -> None:
        ctx.fill(STEP0, [first_name("Avery")])
        ctx.fill(STEP1, [city("Austin"), motto("Calm is fast.")])
        # The lookup committed the site's own suggestion: the step's packet is saved again.
        ctx.save(STEP1, [city("Austin, TX, USA"), motto("Calm is fast.")])
        packet = ctx.fill(STEP2, [email()])
        ctx.prepare(STEP2, packet)

    scenario.then(run)
    started = served.start()
    scenario.settle(served)
    review = view(served, started.json["id"])["review"]
    assert pages_and_values(review) == [
        (1, "Avery"), (2, "Austin, TX, USA"), (2, "Calm is fast."), (3, "avery@example.test"),
    ]


def answer_the_motto(h: Harness, scenario: Scenario, app_id: str) -> None:
    [motto_question] = view(h, app_id)["needs"]["questions"]
    saved = h.client.post(f"/applications/{app_id}/answers", {
        "answers": {motto_question["id"]: "Calm is fast."}, "attestations": {},
    })
    assert saved.status == 200 and saved.json["needs"]["errors"] == {}, saved.json
    resume(h, scenario, app_id)


def test_review_keeps_pages_filled_before_a_question_round(
    served: Harness, scenario: Scenario
) -> None:
    """Page 1 is filled, the run stops for a page-2 question, and the resumed run carries
    on in the draft the site kept, from page 2. The site still holds page 1's answers, so
    the review lists them (WP11 M5); the evidence is still the preparing run's only."""

    def asks(ctx: RunContext) -> None:
        ctx.fill(STEP0, [first_name("Avery")])
        ctx.screenshot("question-1.png", "page 2 when the run stopped for a question")
        ctx.ask(STEP1, [city("Austin, TX, USA")], [question(STEP1, "fld_rs_motto")])

    def resumes_on_page_two(ctx: RunContext) -> None:
        # The site kept the draft: the run starts on page 2 and never sees page 1 again.
        packet = ctx.fill(STEP1, [city("Austin, TX, USA"), ctx.from_user(STEP1, "fld_rs_motto")])
        final = ctx.fill(STEP2, [email()])
        ctx.prepare(STEP2, final)
        assert packet.form_step == 1

    scenario.then(asks, resumes_on_page_two)
    started = served.start()
    app_id = str(started.json["id"])
    scenario.settle(served)
    answer_the_motto(served, scenario, app_id)
    body = view(served, app_id)
    assert body["preparation"] is not None
    assert pages_and_values(body["review"]) == [
        (1, "Avery"), (2, "Austin, TX, USA"), (2, "Calm is fast."), (3, "avery@example.test"),
    ]
    [question_shot] = scenario.contexts[0].evidence
    [review_shot] = scenario.contexts[1].evidence
    assert [e["href"] for e in body["preparation"]["evidence"]] == [
        f"/api/imx/applications/{app_id}/evidence/{review_shot.id}"
    ]
    assert question_shot.id not in str(body["preparation"])


def test_review_starts_over_after_a_failure(served: Harness, scenario: Scenario) -> None:
    """A failed run is a new start: its pages are not part of the prepared form's review."""

    def fails_on_page_two(ctx: RunContext) -> None:
        ctx.fill(STEP0, [first_name("Ashley")])
        ctx.fail("Stopped by a fictional browser error on page 2.")

    def retries_from_page_two(ctx: RunContext) -> None:
        ctx.fill(STEP1, [city("Austin, TX, USA"), motto("Calm is fast.")])
        ctx.prepare(STEP2, ctx.fill(STEP2, [email()]))

    scenario.then(fails_on_page_two, retries_from_page_two)
    started = served.start()
    app_id = str(started.json["id"])
    scenario.settle(served)
    assert view(served, app_id)["state"] == "FAILED_RETRYABLE"
    resume(served, scenario, app_id)
    body = view(served, app_id)
    assert body["preparation"] is not None
    assert pages_and_values(body["review"]) == [
        (2, "Austin, TX, USA"), (2, "Calm is fast."), (3, "avery@example.test"),
    ]


def test_review_leaves_out_pages_after_the_final_step(served: Harness, scenario: Scenario) -> None:
    """An earlier round saw a longer form; the prepared form ends at page 2, so the
    earlier round's page 3 is not a page of it."""
    short_final = make_form(QUESTIONS_URL, 1, list(STEP1.fields), final=True)
    step1_open = make_form(QUESTIONS_URL, 1, list(STEP1.fields), final=False)

    def asks_on_page_three(ctx: RunContext) -> None:
        ctx.fill(STEP0, [first_name("Avery")])
        ctx.fill(step1_open, [city("Austin, TX, USA"), motto("Calm is fast.")])
        ctx.ask(STEP2, [], [question(STEP2, "fld_rs_email")])

    def prepares_two_pages(ctx: RunContext) -> None:
        ctx.fill(STEP0, [first_name("Avery")])
        packet = ctx.fill(short_final, [city("Austin, TX, USA"), motto("Calm is fast.")])
        ctx.prepare(short_final, packet)

    scenario.then(asks_on_page_three, prepares_two_pages)
    started = served.start()
    app_id = str(started.json["id"])
    scenario.settle(served)
    [email_question] = view(served, app_id)["needs"]["questions"]
    saved = served.client.post(f"/applications/{app_id}/answers", {
        "answers": {email_question["id"]: "avery@example.test"}, "attestations": {},
    })
    assert saved.status == 200 and saved.json["needs"]["errors"] == {}, saved.json
    resume(served, scenario, app_id)
    body = view(served, app_id)
    assert body["preparation"]["formStep"] == 1
    assert pages_and_values(body["review"]) == [
        (1, "Avery"), (2, "Austin, TX, USA"), (2, "Calm is fast."),
    ]


def test_review_of_a_stop_that_is_not_prepared_is_the_latest_packet(
    served: Harness, scenario: Scenario
) -> None:
    def asks(ctx: RunContext) -> None:
        ctx.fill(STEP0, [first_name("Avery")])
        ctx.ask(STEP1, [city("Austin, TX, USA")], [question(STEP1, "fld_rs_motto")])

    scenario.then(asks)
    started = served.start()
    scenario.settle(served)
    body = view(served, started.json["id"])
    assert body["preparation"] is None
    assert pages_and_values(body["review"]) == [(2, "Austin, TX, USA")]


def test_review_is_empty_without_a_packet(served: Harness, scenario: Scenario) -> None:
    scenario.then(lambda ctx: ctx.sign_in())
    started = served.start()
    scenario.settle(served)
    body = view(served, started.json["id"])
    assert body["needs"]["kind"] == "interaction"
    assert body["review"] == []


def test_submitted_application_reviews_its_latest_packet(harness: Harness) -> None:
    app_id = harness.start().json["id"]
    answer_all(harness, app_id, poll(harness, app_id, {"NEEDS_INPUT"}))
    harness.client.post(f"/applications/{app_id}/resume", {})
    done = poll(harness, app_id, {"SUBMITTED"})
    form = harness.site.form
    heard, gender = form.field("heard_from"), form.field("gender")
    assert heard.options is not None and gender.options is not None
    assert [
        (item["question"], item["wordingRecorded"], item["page"], item["control"],
         item["value"], item["source"])
        for item in done["review"]
    ] == [
        (heard.label, True, 1, "multi_select", [heard.options[0].label], "user"),
        (gender.label, True, 1, "single_select", gender.options[-1].label, "user"),
        (form.field("why_us").label, True, 1, "long_text",
         "Because the fictional role fits my verified experience.", "user"),
        (form.field("privacy").label, True, 1, "boolean", "Yes", "user"),
    ]


# --- where recorded wording comes from (direct store seeding) -----------------------------------

SEED_URL = "http://127.0.0.1:9/fictional-co/6001/apply"


def one_field_form(field_id: str, label: str, semantic: SemanticType = SemanticType.CUSTOM_TEXT,
                   help_text: str | None = None) -> ApplicationForm:
    return make_form(SEED_URL, 0, [
        make_field(field_id, label, T.TEXT, semantic, required=True, help_text=help_text),
    ], final=True)


def test_user_answer_wording_comes_from_the_user_input_first(served: Harness) -> None:
    form = one_field_form("fld_rw_motto", "Your motto")
    asked = question(form, "fld_rw_motto")
    with seeded(served, SEED_URL) as ctx:
        ctx.ask(form, [], [asked])  # the stop records "Your motto"
        worded = asked.model_copy(update={"label": "Your motto, in one line\nKeep it short."})
        given = UserInput.answering(worded, TextValue(text="Calm is fast."))
        ctx.store.save_user_inputs(ctx.claim, [given])
        packet = ctx.fill(form, [answer(form, "fld_rw_motto", given.value, ST.USER_INPUT,
                                        [given.id])])
        ctx.prepare(form, packet)
        app_id = ctx.app_id
    [item] = view(served, app_id)["review"]
    assert wording(item) == ("Your motto, in one line", True)


def test_user_answer_without_its_reference_uses_the_answer_to_that_question(
    served: Harness,
) -> None:
    form = one_field_form("fld_rw_days", "Which weekdays suit you for interviews?")
    with seeded(served, SEED_URL) as ctx:
        given = UserInput.answering(question(form, "fld_rw_days"), TextValue(text="Tuesdays"))
        ctx.store.save_user_inputs(ctx.claim, [given])
        packet = ctx.fill(form, [answer(form, "fld_rw_days", given.value, ST.USER_INPUT,
                                        ["ui_fictional_elsewhere"])])
        ctx.prepare(form, packet)
        app_id = ctx.app_id
    [item] = view(served, app_id)["review"]
    assert wording(item) == ("Which weekdays suit you for interviews?", True)


def test_recorded_question_comes_before_the_saved_answers_wording(served: Harness) -> None:
    add_saved_answers(served, [SavedAnswer(
        id="sa_rw_school", scope=AnswerScope.GLOBAL, semantic_type=SemanticType.UNIVERSITY,
        question="Where did you study?", value="Fictional State University", confirmed_at=NOW,
    )])
    form = one_field_form("fld_rw_school", "University attended", SemanticType.UNIVERSITY,
                          help_text="As on your diploma.")
    with seeded(served, SEED_URL) as ctx:
        ctx.ask(form, [], [question(form, "fld_rw_school")])
        packet = ctx.fill(form, [answer(form, "fld_rw_school",
                                        TextValue(text="Fictional State University"),
                                        ST.SAVED_ANSWER, ["sa_rw_school"])])
        ctx.prepare(form, packet)
        app_id = ctx.app_id
    [item] = view(served, app_id)["review"]
    assert wording(item) == ("University attended", True)


def test_saved_answers_wording_when_nothing_was_recorded(served: Harness) -> None:
    add_saved_answers(served, [SavedAnswer(
        id="sa_rw_school", scope=AnswerScope.GLOBAL, semantic_type=SemanticType.UNIVERSITY,
        question="Where did you study?\nYour most recent degree.",
        value="Fictional State University", confirmed_at=NOW,
    )])
    form = one_field_form("fld_rw_school", "University attended", SemanticType.UNIVERSITY)
    with seeded(served, SEED_URL) as ctx:
        packet = ctx.fill(form, [answer(form, "fld_rw_school",
                                        TextValue(text="Fictional State University"),
                                        ST.SAVED_ANSWER, ["sa_rw_school"])])
        ctx.prepare(form, packet)
        app_id = ctx.app_id
    [item] = view(served, app_id)["review"]
    assert wording(item) == ("Where did you study?", True)


def test_latest_recorded_wording_wins(served: Harness) -> None:
    before = one_field_form("fld_rw_notice", "Notice period")
    after = one_field_form("fld_rw_notice", "Notice period in weeks", help_text="Enter a number.")
    with seeded(served, SEED_URL) as ctx:
        ctx.ask(before, [], [question(before, "fld_rw_notice")])
        ctx.ask(after, [], [question(after, "fld_rw_notice")])
        packet = ctx.fill(after, [answer(after, "fld_rw_notice", TextValue(text="4"),
                                         ST.GENERATED_FROM_FACTS, ["fact_rw_notice"],
                                         confidence=0.9)])
        ctx.prepare(after, packet)
        app_id = ctx.app_id
    [item] = view(served, app_id)["review"]
    assert wording(item) == ("Notice period in weeks", True)
    assert item["source"] == "generated"


def test_recorded_wording_belongs_to_its_own_page(served: Harness, scenario: Scenario) -> None:
    """Two pages both name a field ``question_0``; wording recorded for page 1 is not
    the wording of page 2's unrelated question."""
    page1 = make_form(SITE_URL, 0, [
        make_field("question_0", "Are you willing to relocate to Austin, TX?", T.RADIO,
                   SemanticType.RELOCATION, required=True, options=[("yes", "Yes"), ("no", "No")]),
    ], final=False)
    page2 = make_form(QUESTIONS_URL, 1, [
        make_field("question_0", "How many years of paid search experience do you have?",
                   T.TEXT, required=True),
    ], final=True)

    def asks(ctx: RunContext) -> None:
        ctx.ask(page1, [], [question(page1, "question_0")])

    def prepares(ctx: RunContext) -> None:
        ctx.fill(page1, [ctx.from_user(page1, "question_0")])
        packet = ctx.fill(page2, [answer(page2, "question_0", TextValue(text="6"),
                                         ST.CANDIDATE_FACT, ["fact_rs_years"], confidence=0.9)])
        ctx.prepare(page2, packet)

    scenario.then(asks, prepares)
    started = served.start()
    app_id = str(started.json["id"])
    scenario.settle(served)
    [relocate] = view(served, app_id)["needs"]["questions"]
    saved = served.client.post(f"/applications/{app_id}/answers", {
        "answers": {relocate["id"]: "yes"}, "attestations": {},
    })
    assert saved.status == 200 and saved.json["needs"]["errors"] == {}, saved.json
    resume(served, scenario, app_id)
    review = view(served, app_id)["review"]
    assert [(item["page"], *wording(item), item["value"]) for item in review] == [
        (1, "Are you willing to relocate to Austin, TX?", True, "Yes"),
        (2, "Question on the form", False, "6"),
    ]
