"""WP3 lookups: a site lookup (``TYPEAHEAD``) the user must answer is flagged, offers the
site's suggestions as a select, and accepts a suggestion or any other text."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_core import (
    AnswerSource,
    ControlType,
    LocalPaths,
    SemanticType,
    TextValue,
)

from .conftest import SITE_URL, FakeCandidates, FictionalSite, Harness, serve
from .preparation_support import RunContext, Scenario, answer, make_field, make_form, question

T = ControlType
SUGGESTIONS = ["Austin, TX, USA", "Austin, MN, USA"]
FORM = make_form(SITE_URL, 0, [
    make_field("fld_lk_email", "Email", T.TEXT, SemanticType.EMAIL, required=True),
    make_field("fld_lk_city", "Location (city)", T.TYPEAHEAD, SemanticType.LOCATION,
               required=True, help_text="Start typing, then choose from the list."),
    make_field("fld_lk_school", "School", T.TYPEAHEAD, SemanticType.UNIVERSITY, required=True),
    make_field("fld_lk_notes", "Anything else?", T.TEXT, SemanticType.CUSTOM_TEXT, required=True),
    make_field("fld_lk_team", "Preferred team", T.SELECT, SemanticType.CUSTOM_SELECT,
               required=True, options=[("", "Select..."), ("growth", "Growth"),
                                       ("brand", "Brand")]),
], final=True)
ALL_LABELS = {"Location (city)", "School", "Anything else?", "Preferred team"}


@pytest.fixture
def scenario(isolated_imx_home: LocalPaths) -> Scenario:
    return Scenario(isolated_imx_home)


@pytest.fixture
def asked(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, scenario: Scenario,
    tmp_path: Path,
) -> Iterator[tuple[Harness, str, dict[str, dict[str, Any]]]]:
    """An application stopped with a lookup that has the site's suggestions, one without
    suggestions, a text question and a select. Yields the harness, the application id and
    the questions by label."""

    def run(ctx: RunContext) -> None:
        ctx.ask(FORM, [
            answer(FORM, "fld_lk_email", TextValue(text="avery@example.test"),
                   AnswerSource.PROFILE_IDENTITY),
        ], [
            question(FORM, "fld_lk_city", suggestions=SUGGESTIONS),
            question(FORM, "fld_lk_school"),
            question(FORM, "fld_lk_notes"),
            question(FORM, "fld_lk_team"),
        ])

    scenario.then(run)
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "private-profile"),
               executor_factory=scenario.factory) as h:
        started = h.start()
        assert started.status == 201, started.json
        app_id = str(started.json["id"])
        scenario.settle(h)
        need = h.client.get(f"/applications/{app_id}").json["needs"]
        assert need["kind"] == "questions"
        questions = {q["label"]: q for q in need["questions"]}
        assert set(questions) == ALL_LABELS
        yield h, app_id, questions


def post_answers(h: Harness, app_id: str, answers: dict[str, Any]) -> dict[str, Any]:
    saved = h.client.post(f"/applications/{app_id}/answers",
                          {"answers": answers, "attestations": {}})
    assert saved.status == 200, saved.json
    body: dict[str, Any] = saved.json
    return body


def stored_values(h: Harness, app_id: str) -> dict[str, Any]:
    with h.store() as store:
        return {u.field_id: u.value for u in store.list_user_inputs(app_id)}


def test_lookup_with_suggestions_is_offered_as_a_select(
    asked: tuple[Harness, str, dict[str, dict[str, Any]]],
) -> None:
    _h, _app_id, questions = asked
    city = questions["Location (city)"]
    assert city["lookup"] is True
    assert city["control"] == "single_select"
    assert city["options"] == [{"value": s, "label": s} for s in SUGGESTIONS]


def test_lookup_without_suggestions_is_typed(
    asked: tuple[Harness, str, dict[str, dict[str, Any]]],
) -> None:
    _h, _app_id, questions = asked
    school = questions["School"]
    assert school["lookup"] is True
    assert school["control"] == "text"
    assert school["options"] is None


def test_other_questions_are_not_lookups(
    asked: tuple[Harness, str, dict[str, dict[str, Any]]],
) -> None:
    _h, _app_id, questions = asked
    assert questions["Anything else?"]["lookup"] is False
    assert questions["Preferred team"]["lookup"] is False
    assert questions["Preferred team"]["control"] == "single_select"


def test_lookup_accepts_a_suggestion(
    asked: tuple[Harness, str, dict[str, dict[str, Any]]],
) -> None:
    h, app_id, questions = asked
    body = post_answers(h, app_id, {questions["Location (city)"]["id"]: "Austin, TX, USA"})
    assert body["needs"]["errors"] == {}
    assert stored_values(h, app_id) == {"fld_lk_city": TextValue(text="Austin, TX, USA")}


def test_lookup_accepts_text_that_is_not_a_suggestion(
    asked: tuple[Harness, str, dict[str, dict[str, Any]]],
) -> None:
    h, app_id, questions = asked
    body = post_answers(h, app_id, {
        questions["Location (city)"]["id"]: "Round Rock, TX",
        questions["School"]["id"]: "Fictional State University",
    })
    assert body["needs"]["errors"] == {}
    assert stored_values(h, app_id) == {
        "fld_lk_city": TextValue(text="Round Rock, TX"),
        "fld_lk_school": TextValue(text="Fictional State University"),
    }


def test_lookup_answer_can_change_from_a_suggestion_to_other_text(
    asked: tuple[Harness, str, dict[str, dict[str, Any]]],
) -> None:
    h, app_id, questions = asked
    qid = questions["Location (city)"]["id"]
    assert post_answers(h, app_id, {qid: "Austin, MN, USA"})["needs"]["errors"] == {}
    assert post_answers(h, app_id, {qid: "Pflugerville, TX"})["needs"]["errors"] == {}
    assert stored_values(h, app_id) == {"fld_lk_city": TextValue(text="Pflugerville, TX")}


@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "whitespace"])
def test_blank_lookup_answer_is_a_draft_and_blocks_continuing(
    asked: tuple[Harness, str, dict[str, dict[str, Any]]], scenario: Scenario, value: str,
) -> None:
    """A blank answer is skipped like any draft (apps/service/README.md); the required
    lookup is enforced when continuing, before anything is dispatched."""
    h, app_id, questions = asked
    qid = questions["Location (city)"]["id"]
    body = post_answers(h, app_id, {qid: value})
    assert body["needs"]["errors"] == {}
    assert stored_values(h, app_id) == {}

    refused = h.client.post(f"/applications/{app_id}/resume", {})
    assert refused.status == 422, refused.json
    assert refused.json["error"]["code"] == "invalid"
    assert refused.json["error"]["fieldErrors"][qid] == "Answer this question to continue."
    assert len(scenario.interactions) == 1  # only the first run: the resume never started
    assert scenario.errors == []
    assert h.service.dispatcher.current is None
    assert stored_values(h, app_id) == {}


def test_lookup_refuses_a_list(asked: tuple[Harness, str, dict[str, dict[str, Any]]]) -> None:
    h, app_id, questions = asked
    qid = questions["Location (city)"]["id"]
    body = post_answers(h, app_id, {qid: ["Austin, TX, USA"]})
    assert qid in body["needs"]["errors"]
    assert stored_values(h, app_id) == {}
