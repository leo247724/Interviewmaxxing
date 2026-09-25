"""``batch-report`` offline: ledgers are written directly (``LedgerEntry`` lines, plus raw
lines in the format from before ``missing_items`` and the card link fields), so hold
categories, row selection across batches, durations, pipeline counts, the application
store fallback for old lines, the Markdown and the CLI run without a batch or a browser.
Every company, URL, id and message here is fictional."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_cli.batch import (
    HOLD_CATEGORIES,
    REPORT_LABEL_LIMIT,
    BackendDurations,
    BatchReport,
    LedgerEntry,
    MissingItem,
    append_ledger,
    build_report,
    categorize_hold,
    list_batches,
    read_ledger,
    render_report_markdown,
)
from interviewmaxxing_cli.main import EXIT_ERROR, EXIT_OK, EXIT_USAGE, build_parser, main
from interviewmaxxing_core import (
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    ControlType,
    LocalPaths,
    MissingInput,
    MissingReason,
)

S = ApplicationState
ORIGIN = "https://jobs.fictional.example/brambleway"
T0 = datetime(2026, 9, 23, 2, 0, tzinfo=UTC)
B1 = "batch-20260923T020000Z"
B2 = "batch-20260924T020000Z"
ELLIPSIS = "…"
SECRET = "PRIVATE fictional note: recruiter Quill Example offered 123456 on the phone"
"""The ``message`` of every fixture line; messages never reach the report."""
REPORT_KEYS = frozenset({
    "batches", "batches_dir", "rows", "totals", "by_backend", "durations", "duration_median_s",
    "duration_p95_s", "holds", "pipeline",
})

SPONSOR = "Do you require visa sponsorship?"
REMOTE = "Have you worked remotely before?"
ADULT = "Are you at least 18 years old?"
SUBMARINE = "Do you own a submarine?"
CONSENT = "Do you consent to a background check?"
ATTEST = "I confirm the information above is accurate."
CITY = "Which city are you based in?"
SCHOOL = "Which school did you attend?"
WIDGET = "Pick your preferred start date"
LONG = ("Describe a campaign at the fictional Brambleway studio that you rescued, what you "
        "changed first, and what you would change if you ran it all once more")
"""A 150-character question; the report shows its first 79 characters and an ellipsis."""
LONG_SHOWN = LONG[:79] + ELLIPSIS
LOOKUP_QUESTION = ("Are you based near the fictional Brambleway studio?\n"
                   "Start typing your city, then pick it from the suggestions that this site "
                   "shows you (fictional help text).")
"""A lookup's full wording as the store keeps it (label and help text, over 120 characters)."""
LOOKUP_LEDGER = " ".join(LOOKUP_QUESTION.split())[:119] + ELLIPSIS
"""The same question as an old ledger line recorded it (``LABEL_LIMIT``, 120 characters)."""
LOOKUP_SHOWN = LOOKUP_LEDGER[:79] + ELLIPSIS

STATES = {"prepared": S.NEEDS_INPUT, "needs_input": S.NEEDS_INPUT,
          "failed_retryable": S.FAILED_RETRYABLE, "closed": S.FAILED_PERMANENT,
          "duplicate": S.DUPLICATE, "already_recorded": S.NEEDS_INPUT}
OLD_KEYS = frozenset({
    "batch_id", "listing_id", "pipeline_id", "company", "title", "application_url", "backend",
    "status", "attempt", "worker_slot", "application_id", "state", "outcome", "message",
    "missing_reasons", "missing_labels", "exit_code", "started_at", "finished_at", "duration_s",
})
"""Every key of a ledger line written before ``missing_items`` and the card link fields."""


@pytest.fixture
def paths(tmp_path: Path) -> LocalPaths:
    return LocalPaths.from_env({}, home=tmp_path / "home")


def ledger_path(paths: LocalPaths, batch_id: str) -> Path:
    return paths.home / "batches" / batch_id / "ledger.jsonl"


def item(label: str, reason: str = "NO_ANSWER", control: str | None = None) -> MissingItem:
    return MissingItem(label=label, reason=reason, control_type=control)


def entry(batch_id: str, listing: str, outcome: str, *, minute: float, duration: float = 0.0,
          backend: str = "greenhouse", app: str | None = None, attempt: int = 1,
          items: Iterable[MissingItem] = (), **fields: Any) -> LedgerEntry:
    """A ledger line that finished ``minute`` minutes after ``T0``."""
    finished = T0 + timedelta(minutes=minute)
    missing = list(items)
    fields.setdefault("state", STATES.get(outcome))
    fields.setdefault("message", SECRET)
    return LedgerEntry(
        batch_id=batch_id, listing_id=listing, company="Brambleway", title="Fictional role",
        application_url=f"{ORIGIN}/{listing}", backend=backend, status="resolved",
        attempt=attempt, application_id=app, outcome=outcome,
        missing_items=missing, missing_reasons=sorted({i.reason for i in missing}),
        missing_labels=[i.label for i in missing],
        started_at=finished - timedelta(seconds=duration), finished_at=finished,
        duration_s=duration, **fields,
    )


def write(paths: LocalPaths, *entries: LedgerEntry) -> None:
    for line in entries:
        append_ledger(ledger_path(paths, line.batch_id), line)


def append_old_line(paths: LocalPaths, batch_id: str, listing: str, *, minute: float,
                    app: str, reasons: list[str], labels: list[str],
                    outcome: str = "needs_input") -> None:
    """A raw ledger line in the format from before ``missing_items`` and card links."""
    finished = T0 + timedelta(minutes=minute)
    line = {
        "batch_id": batch_id, "listing_id": listing, "pipeline_id": None,
        "company": "Brambleway", "title": "Fictional role",
        "application_url": f"{ORIGIN}/{listing}", "backend": "lever", "status": "resolved",
        "attempt": 1, "worker_slot": 0, "application_id": app, "state": "NEEDS_INPUT",
        "outcome": outcome, "message": SECRET, "missing_reasons": reasons,
        "missing_labels": labels, "exit_code": 3,
        "started_at": (finished - timedelta(seconds=30)).isoformat(),
        "finished_at": finished.isoformat(), "duration_s": 30.0,
    }
    assert set(line) == OLD_KEYS
    path = ledger_path(paths, batch_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")


def snapshot(root: Path) -> dict[str, bytes | None]:
    """Every path under ``root`` with its content (None for a directory)."""
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


def hold_table(report: BatchReport) -> dict[str, tuple[int, int, dict[str, int], list[str]]]:
    """category -> (holds, applications, {label: count}, application ids); label order
    among equal counts is left open."""
    return {c.category: (c.holds, c.applications, {x.label: x.count for x in c.top_labels},
                         c.application_ids) for c in report.holds}


# --- fixtures ---------------------------------------------------------------------------------


def write_outcomes(paths: LocalPaths) -> None:
    """Seven listings over two batches. B2 is written first: batches are read in sorted
    order, not in the order they were created."""
    write(paths,
          entry(B2, "l-prep", "already_recorded", minute=90, app="app_prep"),
          entry(B2, "l-recorded", "already_recorded", minute=91, backend="lever",
                app="app_rec"),
          entry(B2, "l-late", "failed_retryable", minute=95, duration=60.0, backend="lever",
                app="app_late"),
          entry(B2, "l-tie", "closed", minute=100, duration=14.6, backend="lever",
                app="app_tie"),
          entry(B2, "l-line-tie", "error", minute=110, duration=7.0, backend="workday"),
          entry(B2, "l-line-tie", "duplicate", minute=110, duration=9.8, backend="workday",
                app="app_dup"))
    write(paths,
          entry(B1, "l-prep", "prepared", minute=1, duration=12.3, app="app_prep"),
          entry(B1, "l-retry", "failed_retryable", minute=2, duration=31.7, app="app_retry"),
          entry(B1, "l-retry", "prepared", minute=3, duration=44.13, app="app_retry",
                attempt=2),
          entry(B1, "l-recorded", "already_recorded", minute=4, backend="lever",
                app="app_rec"),
          entry(B1, "l-no-backend", "error", minute=5, duration=5.5, backend=""),
          entry(B1, "l-late", "needs_input", minute=200, duration=21.4, backend="lever",
                app="app_late"),
          entry(B1, "l-tie", "needs_input", minute=100, duration=8.2, backend="lever",
                app="app_tie"))


def write_holds(paths: LocalPaths) -> None:
    """Six listings with holds; h-superseded's B1 questions were settled by its B2 run and
    h-alias shares app_zulu with h1."""
    write(paths, entry(B1, "h-superseded", "needs_input", minute=1, duration=20.0,
                       app="app_superseded",
                       items=[item(REMOTE, control="RADIO"), item(SUBMARINE, control="SELECT")]))
    write(paths,
          entry(B2, "h1", "needs_input", minute=10, duration=20.0, app="app_zulu", items=[
              item(SPONSOR, control="SELECT"), item(REMOTE, control="RADIO"),
              item(CONSENT, "EXPLICIT_ANSWER_REQUIRED", "CHECKBOX"),
              item(CITY, control="TYPEAHEAD")]),
          entry(B2, "h2", "needs_input", minute=11, duration=20.0, app="app_alpha", items=[
              item(SPONSOR), item(REMOTE), item(WIDGET, "UNSUPPORTED_CONTROL", "UNSUPPORTED")]),
          entry(B2, "h3", "needs_input", minute=12, duration=20.0, app="app_mike", items=[
              item(SPONSOR, control="SELECT"), item(ADULT, control="SELECT"),
              item(LONG, control="TEXTAREA")]),
          entry(B2, "h4", "needs_input", minute=13, duration=20.0, app="app_kilo", items=[
              item(ATTEST, "UNCOVERED_ATTESTATION", "CHECKBOX"),
              item(SCHOOL, control="TYPEAHEAD")]),
          entry(B2, "h-alias", "needs_input", minute=14, duration=20.0, app="app_zulu",
                items=[item(SPONSOR, control="SELECT")]),
          entry(B2, "h-superseded", "prepared", minute=15, duration=20.0,
                app="app_superseded"))


def write_cards(paths: LocalPaths) -> None:
    """Eight listings, seven with a pipeline card; p-late-link and p-moved settle in B2."""
    write(paths,
          entry(B1, "p-late-link", "needs_input", minute=1, duration=20.0, app="app_p1",
                pipeline_id="pc1", linked=False, link_reason="no pipeline database"),
          entry(B1, "p-moved", "closed", minute=2, duration=20.0, app="app_p2",
                pipeline_id="pc2", linked=True, linked_application_id="app_p2",
                closed_synced=False, closed_sync_reason="board has no Closed lane"),
          entry(B1, "p-missing", "prepared", minute=3, duration=20.0, app="app_p3",
                pipeline_id="pc3", linked=False, link_reason="card not found"),
          entry(B1, "p-no-card", "prepared", minute=4, duration=20.0, app="app_p4"),
          entry(B1, "p-before-links", "needs_input", minute=5, duration=20.0, app="app_p5",
                pipeline_id="pc5"))
    write(paths,
          entry(B2, "p-late-link", "already_recorded", minute=60, app="app_p1",
                pipeline_id="pc1", linked=True, linked_application_id="app_p1"),
          entry(B2, "p-moved", "already_recorded", minute=61, app="app_p2",
                pipeline_id="pc2", state=S.FAILED_PERMANENT, linked=True,
                linked_application_id="app_p2", closed_synced=True),
          entry(B2, "p-not-saved", "closed", minute=62, duration=20.0, app="app_p6",
                pipeline_id="pc6", linked=True, linked_application_id="app_p6",
                closed_synced=False, closed_sync_reason="card not in Saved (in applied)"),
          entry(B2, "p-no-app", "error", minute=63, duration=20.0, pipeline_id="pc7",
                linked=False, link_reason="no application id"),
          entry(B2, "p-missing-2", "needs_input", minute=64, duration=20.0, app="app_p8",
                pipeline_id="pc8", linked=False, link_reason="card not found"))


# --- hold categories --------------------------------------------------------------------------


def test_hold_categories_and_report_label_limit():
    assert HOLD_CATEGORIES == ("custom_control", "explicit_answer", "screener_yes_no", "lookup",
                               "narrative", "other")
    assert REPORT_LABEL_LIMIT == 80
    assert categorize_hold(SPONSOR, "NO_ANSWER") == "screener_yes_no"  # control type optional
    assert categorize_hold(CITY, "NO_ANSWER") == "other"


@pytest.mark.parametrize(("label", "reason", "control", "expected"), [
    # Rule order: a custom control first, then explicit answers, then lookups.
    (WIDGET, "UNSUPPORTED_CONTROL", "UNSUPPORTED", "custom_control"),
    ("Do you have a portfolio site?", "UNSUPPORTED_CONTROL", None, "custom_control"),
    ("Describe your portfolio", "UNSUPPORTED_CONTROL", "TYPEAHEAD", "custom_control"),
    (CONSENT, "EXPLICIT_ANSWER_REQUIRED", "CHECKBOX", "explicit_answer"),
    ("Which city are you in?", "EXPLICIT_ANSWER_REQUIRED", "TYPEAHEAD", "explicit_answer"),
    (ATTEST, "UNCOVERED_ATTESTATION", "CHECKBOX", "explicit_answer"),
    ("Have you read our privacy notice?", "UNCOVERED_ATTESTATION", None, "explicit_answer"),
    # A TYPEAHEAD question is a lookup whatever its wording.
    (CITY, "NO_ANSWER", "TYPEAHEAD", "lookup"),
    ("Are you located near our fictional studio?", "NO_ANSWER", "TYPEAHEAD", "lookup"),
    ("Describe where you live", "NO_ANSWER", "TYPEAHEAD", "lookup"),
    # Yes/no screeners, whatever the decoration, spacing or capitalization.
    ("Do you have 5+ years of paid media experience?", "NO_ANSWER", None, "screener_yes_no"),
    (REMOTE, "NO_ANSWER", "RADIO", "screener_yes_no"),
    ("Are you willing to travel?", "NO_ANSWER", "SELECT", "screener_yes_no"),
    ("* Do you hold a driving licence?", "NO_ANSWER", None, "screener_yes_no"),
    ("✱ Have you used Brambleway before?", "NO_ANSWER", None, "screener_yes_no"),
    ('"Are you open to hybrid work?"', "NO_ANSWER", None, "screener_yes_no"),
    ("'Do you speak Esperanto?'", "NO_ANSWER", None, "screener_yes_no"),
    ("“Have you led a team?”", "NO_ANSWER", None, "screener_yes_no"),
    ("  DO   YOU\n need a visa?", "NO_ANSWER", None, "screener_yes_no"),
    ("*  ARE you over 18?", "NO_ANSWER", "TEXT", "screener_yes_no"),
    # ... but only as the opening words.
    ("Dove Street or Elm Street office?", "NO_ANSWER", None, "other"),
    ("Doyou have a car?", "NO_ANSWER", None, "other"),
    ("Do your references know you applied?", "NO_ANSWER", None, "other"),
    ("When do you want to start?", "NO_ANSWER", None, "other"),
    ("Why do you want to join Brambleway?", "NO_ANSWER", "TEXTAREA", "narrative"),
    # Narrative questions: one of the words anywhere in the label.
    ("Describe a campaign you are proud of.", "NO_ANSWER", "TEXTAREA", "narrative"),
    ("Tell us about yourself", "NO_ANSWER", None, "narrative"),
    ("Please explain any gaps in your employment.", "NO_ANSWER", None, "narrative"),
    ("In one sentence, why Brambleway?", "NO_ANSWER", None, "narrative"),
    ("Which Telluride office would you prefer?", "NO_ANSWER", None, "other"),
    # Everything else.
    ("What is your notice period?", "NO_ANSWER", "TEXT", "other"),
    ("", "NO_ANSWER", None, "other"),
    (SPONSOR, "AMBIGUOUS", "SELECT", "other"),
    ("Which city?", "AMBIGUOUS", "TYPEAHEAD", "other"),
    ("Sign in to continue", "USER_ACTION", None, "other"),
    ("Do you have a licence?", None, None, "other"),
    ("Describe your work", None, "TEXTAREA", "other"),
    (CITY, None, "TYPEAHEAD", "other"),
])
def test_categorize_hold(label, reason, control, expected):
    assert categorize_hold(label, reason, control) == expected


# --- rows, totals and durations ---------------------------------------------------------------


def test_each_listing_counts_once_by_its_latest_launched_entry(paths):
    write_outcomes(paths)
    before = snapshot(paths.home)
    report = build_report(paths)
    render_report_markdown(report)
    assert snapshot(paths.home) == before  # nothing created or rewritten
    assert not paths.state_db.exists()

    assert report.batches == [B1, B2]
    assert report.batches_dir == str(paths.home / "batches")
    assert report.rows == 7
    # l-prep: prepared in B1 beats its later already_recorded in B2; l-recorded: only ever
    # already_recorded; l-retry: the prepared retry; l-late: B1's entry finished last (not
    # batch order); l-tie: equal finish, the later batch; l-line-tie: equal finish in one
    # batch, the later line.
    assert report.totals == {"prepared": 2, "needs_input": 1, "closed": 1, "duplicate": 1,
                             "error": 1, "already_recorded": 1}
    assert list(report.totals) == ["prepared", "needs_input", "closed", "duplicate", "error",
                                   "already_recorded"]
    assert report.by_backend == {
        "greenhouse": {"prepared": 2},
        "lever": {"needs_input": 1, "closed": 1, "already_recorded": 1},
        "workday": {"duplicate": 1},
        "(none)": {"error": 1},
    }
    assert report.holds == []
    assert build_report(paths, [B2, B1]).batches == [B1, B2]


def test_durations_cover_every_launched_attempt(paths):
    write_outcomes(paths)
    report = build_report(paths)
    # Superseded attempts count too; already_recorded entries never do. Same interpolation
    # as the batch summary, rounded to 2 decimals.
    assert report.durations == {
        "greenhouse": BackendDurations(runs=3, median_s=31.7, p95_s=42.89),  # 12.3 31.7 44.13
        "lever": BackendDurations(runs=4, median_s=18.0, p95_s=54.21),  # 8.2 14.6 21.4 60
        "workday": BackendDurations(runs=2, median_s=8.4, p95_s=9.66),  # 7.0 9.8
        "(none)": BackendDurations(runs=1, median_s=5.5, p95_s=5.5),
    }
    assert (report.duration_median_s, report.duration_p95_s) == (13.45, 52.86)

    only = build_report(paths, [B2])
    assert only.batches == [B2] and only.rows == 5
    assert only.totals == {"failed_retryable": 1, "closed": 1, "duplicate": 1,
                           "already_recorded": 2}
    assert only.by_backend == {"greenhouse": {"already_recorded": 1},
                               "lever": {"failed_retryable": 1, "closed": 1,
                                         "already_recorded": 1},
                               "workday": {"duplicate": 1}}
    assert only.durations["lever"] == BackendDurations(runs=2, median_s=37.3, p95_s=57.73)
    assert only.durations["workday"] == BackendDurations(runs=2, median_s=8.4, p95_s=9.66)
    assert "(none)" not in only.durations
    greenhouse = only.durations.get("greenhouse")  # no launched attempt in B2
    assert greenhouse is None or (greenhouse.runs, greenhouse.median_s, greenhouse.p95_s) == \
        (0, None, None)
    assert (only.duration_median_s, only.duration_p95_s) == (12.2, 53.19)


# --- holds ----------------------------------------------------------------------------------------


def test_holds_are_grouped_by_category(paths):
    write_holds(paths)
    report = build_report(paths)
    assert report.rows == 6
    assert [c.model_dump() for c in report.holds] == [
        {"category": "screener_yes_no", "holds": 7, "applications": 3,
         "top_labels": [{"label": SPONSOR, "count": 4}, {"label": REMOTE, "count": 2},
                        {"label": ADULT, "count": 1}],
         "application_ids": ["app_zulu", "app_alpha", "app_mike"]},
        {"category": "explicit_answer", "holds": 2, "applications": 2,
         "top_labels": [{"label": CONSENT, "count": 1}, {"label": ATTEST, "count": 1}],
         "application_ids": ["app_zulu", "app_kilo"]},
        {"category": "lookup", "holds": 2, "applications": 2,
         "top_labels": [{"label": CITY, "count": 1}, {"label": SCHOOL, "count": 1}],
         "application_ids": ["app_zulu", "app_kilo"]},
        {"category": "custom_control", "holds": 1, "applications": 1,
         "top_labels": [{"label": WIDGET, "count": 1}], "application_ids": ["app_alpha"]},
        {"category": "narrative", "holds": 1, "applications": 1,
         "top_labels": [{"label": LONG_SHOWN, "count": 1}], "application_ids": ["app_mike"]},
    ]  # "other" has no hold, so it is left out
    # h-superseded's B1 questions hold nothing any more: its latest run prepared it.
    assert SUBMARINE not in report.model_dump_json()
    assert report.pipeline.rows_with_card == 0
    assert "## Pipeline cards" not in render_report_markdown(report).splitlines()

    capped = build_report(paths, top=2)
    assert [(x.label, x.count) for x in capped.holds[0].top_labels] == [(SPONSOR, 4), (REMOTE, 2)]
    assert all(len(c.top_labels) <= 2 for c in capped.holds)
    assert [(c.category, c.holds, c.applications, c.application_ids) for c in capped.holds] == \
        [(c.category, c.holds, c.applications, c.application_ids) for c in report.holds]

    first = build_report(paths, [B1])  # the superseded questions, alone
    assert hold_table(first) == {
        "screener_yes_no": (2, 1, {REMOTE: 1, SUBMARINE: 1}, ["app_superseded"])}


def test_labels_are_truncated_to_80_characters_everywhere(paths):
    write_holds(paths)
    report = build_report(paths)
    text = render_report_markdown(report)
    data = report.model_dump(mode="json")
    for output in (text, report.model_dump_json(), json.dumps(data),
                   json.dumps(data, ensure_ascii=False)):
        assert LONG[:79] in output
        assert LONG[:REPORT_LABEL_LIMIT] not in output  # never more than 79 of its characters
    assert len(LONG) == 150 and len(LONG_SHOWN) == 80 and LONG_SHOWN in text
    [narrative] = [c for c in data["holds"] if c["category"] == "narrative"]
    assert narrative["top_labels"] == [{"label": LONG_SHOWN, "count": 1}]
    assert all(len(x["label"]) <= REPORT_LABEL_LIMIT for c in data["holds"]
               for x in c["top_labels"])


# --- old ledger lines -----------------------------------------------------------------------------


def test_old_lines_use_their_single_reason_else_other_without_a_store(paths):
    append_old_line(paths, B1, "old-custom", minute=1, app="app_old_custom",
                    reasons=["UNSUPPORTED_CONTROL"], labels=["Start date", "Preferred office"])
    append_old_line(paths, B1, "old-single", minute=2, app="app_old_single",
                    reasons=["NO_ANSWER"],
                    labels=["Do you have a driving licence?", "What is your notice period?"])
    append_old_line(paths, B1, "old-multi", minute=3, app="app_old_multi",
                    reasons=["EXPLICIT_ANSWER_REQUIRED", "NO_ANSWER"], labels=[CONSENT, CITY])
    append_old_line(paths, B1, "old-prepared", minute=4, app="app_old_prepared", reasons=[],
                    labels=[], outcome="prepared")
    old = read_ledger(ledger_path(paths, B1))  # lines without the new keys still read
    assert len(old) == 4
    assert all(e.missing_items == [] and e.linked is None and e.closed_synced is None
               for e in old)

    report = build_report(paths)
    assert not paths.state_db.exists() and not paths.state_db.parent.exists()
    assert report.totals == {"prepared": 1, "needs_input": 3}
    assert [c.category for c in report.holds] == ["other", "custom_control", "screener_yes_no"]
    assert hold_table(report) == {
        "other": (3, 2, {"What is your notice period?": 1, CONSENT: 1, CITY: 1},
                  ["app_old_single", "app_old_multi"]),
        "custom_control": (2, 1, {"Start date": 1, "Preferred office": 1}, ["app_old_custom"]),
        "screener_yes_no": (1, 1, {"Do you have a driving licence?": 1}, ["app_old_single"]),
    }
    assert SECRET not in render_report_markdown(report)
    assert SECRET not in report.model_dump_json()


def missing(url: str, field_id: str, label: str, reason: MissingReason,
            control: ControlType) -> MissingInput:
    return MissingInput(field_id=field_id, form_url=url, form_step=0,
                        field_fingerprint=hashlib.sha256(field_id.encode()).hexdigest(),
                        label=label, reason=reason, prompt="Please answer this question.",
                        control_type=control)


def test_old_lines_with_several_reasons_are_resolved_from_the_store(paths):
    lookup_url, packet_url = f"{ORIGIN}/old-lookup", f"{ORIGIN}/old-packet"
    no_answer, explicit = MissingReason.NO_ANSWER, MissingReason.EXPLICIT_ANSWER_REQUIRED

    def recorded(control: ControlType) -> list[dict[str, Any]]:
        return [missing(lookup_url, "city", LOOKUP_QUESTION, no_answer, control)
                .model_dump(mode="json"),
                missing(lookup_url, "consent", CONSENT, explicit, ControlType.CHECKBOX)
                .model_dump(mode="json")]

    with ApplicationStore.open(paths.state_db) as store:
        looked = store.record_request("default", lookup_url).application
        claim = store.claim(looked.id, "test")
        store.transition(claim, S.INSPECTING)
        store.transition(claim, S.NEEDS_INPUT,
                         metadata={"missing_inputs": recorded(ControlType.TEXT), "reason": "x"})
        store.transition(claim, S.INSPECTING)
        # The latest NEEDS_INPUT event is the one that counts: the city is now a lookup.
        store.transition(claim, S.NEEDS_INPUT, metadata={
            "missing_inputs": recorded(ControlType.TYPEAHEAD), "reason": "x"})
        store.release(claim)

        packed = store.record_request("default", packet_url).application
        claim = store.claim(packed.id, "test")
        store.transition(claim, S.INSPECTING)
        store.save_packet(claim, ApplicationPacket(
            application_id=packed.id, job_id=packed.job_id, candidate_id="default",
            form_url=packet_url, form_step=0, form_fingerprint=hashlib.sha256(b"f").hexdigest(),
            missing_inputs=[
                missing(packet_url, "school", SCHOOL, no_answer, ControlType.TYPEAHEAD),
                missing(packet_url, "attest", ATTEST, MissingReason.UNCOVERED_ATTESTATION,
                        ControlType.CHECKBOX)]))
        # An older record: the event carries no missing_inputs, the latest packet does.
        store.transition(claim, S.NEEDS_INPUT, metadata={"reason": "x"})
        store.release(claim)
        events = {app.id: len(store.list_events(app.id)) for app in (looked, packed)}

    append_old_line(paths, B1, "old-lookup", minute=1, app=looked.id,
                    reasons=["EXPLICIT_ANSWER_REQUIRED", "NO_ANSWER"],
                    labels=[LOOKUP_LEDGER, CONSENT])
    append_old_line(paths, B1, "old-packet", minute=2, app=packed.id,
                    reasons=["NO_ANSWER", "UNCOVERED_ATTESTATION"], labels=[SCHOOL, ATTEST])
    append_old_line(paths, B1, "old-unknown", minute=3, app="app_not_in_store",
                    reasons=["AMBIGUOUS", "NO_ANSWER"],
                    labels=["Do you have a licence?", "Pick a T-shirt size"])

    report = build_report(paths)
    assert [c.category for c in report.holds] == ["explicit_answer", "lookup", "other"]
    assert hold_table(report) == {
        "explicit_answer": (2, 2, {CONSENT: 1, ATTEST: 1}, [looked.id, packed.id]),
        "lookup": (2, 2, {LOOKUP_SHOWN: 1, SCHOOL: 1}, [looked.id, packed.id]),
        "other": (2, 1, {"Do you have a licence?": 1, "Pick a T-shirt size": 1},
                  ["app_not_in_store"]),
    }
    with ApplicationStore.open(paths.state_db) as store:  # read only
        assert {app.id: len(store.list_events(app.id)) for app in (looked, packed)} == events


# --- pipeline cards -------------------------------------------------------------------------------


def test_pipeline_counts_use_each_listings_latest_link_and_move(paths):
    write_cards(paths)
    report = build_report(paths)
    assert report.rows == 8
    counts = report.pipeline.model_dump()
    problems = {p["label"]: p["count"] for p in counts.pop("problems")}
    # p-late-link: not linked in B1, linked by its B2 already_recorded line; p-moved: its
    # B2 line moved the card; p-before-links has a card but no link field; p-no-card has none.
    assert counts == {"rows_with_card": 7, "linked": 3, "not_linked": 3, "closed_moved": 1,
                      "closed_skipped": 1}
    # Reasons of superseded lines ("no pipeline database", "board has no Closed lane") are gone.
    assert problems == {"card not found": 2, "no application id": 1,
                        "card not in Saved (in applied)": 1}
    assert "## Pipeline cards" in render_report_markdown(report).splitlines()


def test_a_closed_move_skipped_for_a_failed_link_counts_once(paths):
    # A closed job whose card could not be linked also skips the Closed move ("card not
    # linked"). As in the batch summary (BatchSummary.pipeline_problems), that listing's
    # problem is counted once, under the link reason.
    write(paths, entry(B1, "p-closed", "closed", minute=1, duration=20.0, app="app_c1",
                       pipeline_id="pc1", linked=False, link_reason="no pipeline database",
                       closed_synced=False, closed_sync_reason="card not linked"))
    assert build_report(paths).pipeline.model_dump() == {
        "rows_with_card": 1, "linked": 0, "not_linked": 1, "closed_moved": 0,
        "closed_skipped": 1, "problems": [{"label": "no pipeline database", "count": 1}]}


# --- batch ids, empty homes and side effects ------------------------------------------------------


def test_invalid_and_unknown_batch_ids(paths):
    write(paths, entry(B1, "l1", "prepared", minute=1, duration=10.0))
    before = snapshot(paths.home)
    for bad in ("", ".", "..", "../x", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            build_report(paths, [bad])
        with pytest.raises(ValueError):
            build_report(paths, [B1, bad])
    for unknown in (["nope"], [B1, "nope"]):
        with pytest.raises(FileNotFoundError):
            build_report(paths, unknown)
    assert snapshot(paths.home) == before  # nothing created, not even the unknown batch dir
    (paths.home / "batches" / "no-ledger").mkdir()
    with pytest.raises(FileNotFoundError):
        build_report(paths, ["no-ledger"])
    assert build_report(paths, [B1]).rows == 1


def test_list_batches_and_empty_reports_create_nothing(tmp_path):
    paths = LocalPaths.from_env({}, home=tmp_path / "absent")
    assert list_batches(paths) == []
    report = build_report(paths)
    text = render_report_markdown(report)
    assert not paths.home.exists()
    batches_dir = paths.home / "batches"
    assert report.model_dump() == {
        "batches": [], "batches_dir": str(batches_dir), "rows": 0, "totals": {},
        "by_backend": {}, "durations": {}, "duration_median_s": None, "duration_p95_s": None,
        "holds": [], "pipeline": {"rows_with_card": 0, "linked": 0, "not_linked": 0,
                                  "closed_moved": 0, "closed_skipped": 0, "problems": []},
        "provider_cost_usd": None, "provider_calls": 0, "provider_cost_rows": 0,
        "cost_per_prepared_usd": None, "since": None, "questions": [], "fill_failures": [],
        "backends": [], "ledger_lines_ignored": 0,
        "submissions": None,
    }
    assert f"No batch ledgers under {batches_dir}." in text

    (batches_dir / "scratch").mkdir(parents=True)  # a directory without a ledger
    (batches_dir / "notes.txt").write_text("not a batch\n")
    assert list_batches(paths) == []
    assert build_report(paths).rows == 0
    write(paths, entry("zeta", "z1", "prepared", minute=1, duration=10.0),
          entry("alpha", "a1", "error", minute=2, duration=5.0, backend=""))
    assert list_batches(paths) == ["alpha", "zeta"]
    report = build_report(paths)
    assert report.batches == ["alpha", "zeta"] and report.rows == 2
    assert report.totals == {"prepared": 1, "error": 1}
    assert sorted(p.name for p in batches_dir.iterdir()) == ["alpha", "notes.txt", "scratch",
                                                             "zeta"]


# --- Markdown -------------------------------------------------------------------------------------


def test_markdown_report(paths):
    write_outcomes(paths)
    write_holds(paths)
    write_cards(paths)
    report = build_report(paths)
    assert report.rows == 21
    text = render_report_markdown(report)
    lines = text.splitlines()
    assert "# Batch report" in lines
    assert any(B1 in line and B2 in line for line in lines)
    assert re.search(r"rows:\s*21\b", text)
    assert "nothing was submitted (preparation only)" in text
    for header in ("## Totals", "## By backend", "## Durations", "## Holds by category",
                   "## Pipeline cards"):
        assert header in lines
    assert "| outcome | count |" in lines and "| **all** | 21 |" in lines
    assert "| backend | runs | median s | p95 s |" in lines
    assert any(line.startswith("| **all** | 25 |") for line in lines)  # launched attempts
    assert [line.split(":")[0] for line in lines if line.startswith("### ")] == [
        "### screener_yes_no", "### explicit_answer", "### lookup", "### custom_control",
        "### narrative"]
    assert "### screener_yes_no: 7 hold(s) in 3 application(s)" in lines
    assert "### lookup: 2 hold(s) in 2 application(s)" in lines
    assert "| question | count |" in lines and f"| {SPONSOR} | 4 |" in lines
    assert "applications: app_zulu, app_alpha, app_mike" in text
    assert "applications: app_zulu, app_kilo" in text
    assert LONG_SHOWN in text and LONG[:REPORT_LABEL_LIMIT] not in text
    assert SECRET not in text and SECRET not in report.model_dump_json()
    assert "123456" not in text


# --- CLI ------------------------------------------------------------------------------------------


def test_batch_report_parser():
    parser = build_parser()
    args = parser.parse_args(["batch-report", "b1", "--top", "3", "--json"])
    assert args.command == "batch-report" and args.top == 3 and args.json is True
    assert args.batch_ids == ["b1"] and args.since is None
    defaults = parser.parse_args(["batch-report"])
    assert defaults.top == 10 and not defaults.json and defaults.batch_ids == []
    for bad in ("0", "101", "ten"):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["batch-report", "--top", bad])
        assert exc.value.code == EXIT_USAGE


def test_batch_report_command(paths, capsys):
    write_holds(paths)
    home = str(paths.home)
    assert main(["--home", home, "batch-report"]) == EXIT_OK
    out, err = capsys.readouterr()
    assert "# Batch report" in out and B1 in out and B2 in out
    assert "### screener_yes_no: 7 hold(s) in 3 application(s)" in out
    assert SECRET not in out + err

    assert main(["--home", home, "batch-report", "--json"]) == EXIT_OK
    out = capsys.readouterr().out
    data = json.loads(out)
    assert out.startswith("{\n")  # indented
    assert set(data) == set(BatchReport.model_fields)
    assert set(data) >= REPORT_KEYS
    assert data["batches"] == [B1, B2] and data["rows"] == 6
    assert data["totals"] == {"prepared": 1, "needs_input": 5}
    assert data["holds"][0]["top_labels"][:2] == [{"label": SPONSOR, "count": 4},
                                                  {"label": REMOTE, "count": 2}]
    assert SECRET not in out

    assert main(["--home", home, "batch-report", B1, "--json"]) == EXIT_OK
    one = json.loads(capsys.readouterr().out)
    assert one["batches"] == [B1] and one["rows"] == 1 and one["totals"] == {"needs_input": 1}
    assert [(c["category"], c["holds"]) for c in one["holds"]] == [("screener_yes_no", 2)]

    assert main(["--home", home, "batch-report", "--top", "1", "--json"]) == EXIT_OK
    capped = json.loads(capsys.readouterr().out)
    assert [len(c["top_labels"]) for c in capped["holds"]] == [1, 1, 1, 1, 1]
    assert capped["holds"][0]["top_labels"] == [{"label": SPONSOR, "count": 4}]
    assert [c["holds"] for c in capped["holds"]] == [7, 2, 2, 1, 1]


def test_batch_report_command_errors_and_read_only(paths, tmp_path, capsys):
    write_holds(paths)
    home = str(paths.home)
    before = snapshot(paths.home)
    assert main(["--home", home, "batch-report", "nope"]) == EXIT_ERROR
    assert "nope" in capsys.readouterr().err
    for bad in ("..", ".", "a/b", ""):
        assert main(["--home", home, "batch-report", bad]) == EXIT_USAGE
        assert capsys.readouterr().err.strip()
    assert snapshot(paths.home) == before

    absent = tmp_path / "absent"
    assert main(["--home", str(absent), "batch-report"]) == EXIT_OK
    assert f"No batch ledgers under {absent / 'batches'}." in capsys.readouterr().out
    assert main(["--home", str(absent), "batch-report", "--json"]) == EXIT_OK
    empty = json.loads(capsys.readouterr().out)
    assert empty["rows"] == 0 and empty["batches"] == [] and empty["holds"] == []
    assert main(["--home", str(absent), "batch-report", "nope"]) == EXIT_ERROR
    assert capsys.readouterr().err.strip()
    assert main(["--home", str(absent), "batch-report", ".."]) == EXIT_USAGE
    assert not absent.exists()


# --- provider cost --------------------------------------------------------------------------------

B3 = "batch-20260925T020000Z"


def test_provider_cost_totals_and_cost_per_prepared_application(paths, capsys):
    write(paths,
          entry(B1, "l-a", "prepared", minute=1, app="app_a", provider_cost_usd=0.10,
                provider_calls=4),
          entry(B1, "l-b", "prepared", minute=2, app="app_b", provider_cost_usd=0.30,
                provider_calls=9),
          entry(B1, "l-c", "needs_input", minute=3, app="app_c", provider_cost_usd=0.05,
                provider_calls=2),
          entry(B1, "l-d", "closed", minute=4, app="app_d"))  # used no provider
    report = build_report(paths)
    assert report.rows == 4
    assert report.totals == {"prepared": 2, "needs_input": 1, "closed": 1}
    assert report.provider_cost_usd == 0.45
    assert (report.provider_calls, report.provider_cost_rows) == (15, 3)
    assert report.cost_per_prepared_usd == 0.225
    lines = render_report_markdown(report).splitlines()
    assert "## Provider cost" in lines
    assert "- known cost: USD 0.4500 over 15 call(s) in 3 application(s)" in lines
    assert "- per prepared application: USD 0.2250" in lines

    home = str(paths.home)
    assert main(["--home", home, "batch-report", "--json"]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert (data["provider_cost_usd"], data["provider_calls"], data["provider_cost_rows"],
            data["cost_per_prepared_usd"]) == (0.45, 15, 3, 0.225)
    assert main(["--home", home, "batch-report"]) == EXIT_OK
    out = capsys.readouterr().out.splitlines()
    assert "## Provider cost" in out and "- per prepared application: USD 0.2250" in out


def test_a_retried_application_counts_its_cost_once(paths):
    """Every launched line carries its application's cost so far, so only the listing's
    reported line counts; a later already-recorded line (no cost) does not hide it."""
    write(paths,
          entry(B1, "l-retry", "failed_retryable", minute=1, app="app_retry",
                provider_cost_usd=0.05, provider_calls=1),
          entry(B2, "l-retry", "prepared", minute=60, app="app_retry", attempt=2,
                provider_cost_usd=0.12, provider_calls=3),
          entry(B3, "l-retry", "already_recorded", minute=120, app="app_retry"))
    report = build_report(paths)
    assert report.rows == 1 and report.totals == {"prepared": 1}
    assert (report.provider_cost_usd, report.provider_calls, report.provider_cost_rows,
            report.cost_per_prepared_usd) == (0.12, 3, 1, 0.12)

    first = build_report(paths, [B1])  # the first attempt alone: nothing prepared yet
    assert first.totals == {"failed_retryable": 1}
    assert (first.provider_cost_usd, first.provider_calls, first.provider_cost_rows,
            first.cost_per_prepared_usd) == (0.05, 1, 1, None)
    assert "- per prepared application: -" in render_report_markdown(first).splitlines()


def test_reports_without_provider_costs_have_no_cost_section(paths):
    write_outcomes(paths)
    append_old_line(paths, B1, "l-old", minute=6, app="app_old", reasons=[], labels=[])
    report = build_report(paths)
    assert report.rows == 8 and report.totals["prepared"] == 2
    assert (report.provider_cost_usd, report.provider_calls, report.provider_cost_rows,
            report.cost_per_prepared_usd) == (None, 0, 0, None)
    text = render_report_markdown(report)
    assert "## Provider cost" not in text and "per prepared application" not in text


def test_a_crashed_retry_keeps_the_listings_earlier_cost(paths):
    """A later attempt that crashed before an application id was known carries no cost;
    the listing keeps the cost its application recorded so far."""
    write(paths,
          entry(B1, "l-crash", "needs_input", minute=1, app="app_crash",
                provider_cost_usd=0.07, provider_calls=2),
          entry(B2, "l-crash", "error", minute=60, attempt=2))
    report = build_report(paths)
    assert report.rows == 1 and report.totals == {"error": 1}
    assert (report.provider_cost_usd, report.provider_calls, report.provider_cost_rows,
            report.cost_per_prepared_usd) == (0.07, 2, 1, None)


# --- questions grouped by wording (WP9) -------------------------------------------------------

HOME_CLI = ["interviewmaxxing", "--home", "/tmp/fictional home"]
"""The command prefix of a report made with ``--home '/tmp/fictional home'``."""
QUESTION_KEYS = frozenset({
    "question", "holds", "applications", "backends", "reason", "reasons", "semantic_type",
    "control_type", "category", "sample_application_id", "field_id", "answer", "act",
    "application_ids",
})
FAILURE_KEYS = frozenset({
    "kind", "status", "detail", "failures", "applications", "backends", "sample_application_id",
    "labels", "application_ids",
})
BACKEND_KEYS = frozenset({
    "backend", "applications", "prepared", "needs_input", "failed", "closed", "no_form",
    "other", "prepared_rate", "median_duration_s", "provider_cost_usd",
})


def held(label: str, field_id: str | None, reason: str = "NO_ANSWER", control: str | None = None,
         semantic: str | None = None) -> MissingItem:
    return MissingItem(label=label, reason=reason, control_type=control, field_id=field_id,
                       semantic_type=semantic)


def write_questions(paths: LocalPaths) -> None:
    write(paths,
          entry(B1, "g1", "needs_input", minute=1, duration=10.0, app="app_one", items=[
              held(SPONSOR, "question_11", "EXPLICIT_ANSWER_REQUIRED", "SELECT", "SPONSORSHIP"),
              held("Sign in", None, "USER_ACTION")]),
          entry(B1, "g2", "needs_input", minute=2, duration=10.0, app="app_two", backend="lever",
                items=[held("do you require VISA sponsorship", "cards[abc][field0]",
                            "EXPLICIT_ANSWER_REQUIRED", "RADIO", "UNKNOWN"),
                       held(WIDGET, "start", "UNSUPPORTED_CONTROL", "UNSUPPORTED")]))
    write(paths,
          entry(B2, "g3", "needs_input", minute=3, duration=10.0, app="app_three", items=[
              held("Do you require visa sponsorship? *", "question_31", "AMBIGUOUS", "SELECT",
                   "SPONSORSHIP"),
              held(CITY, "city", "NO_ANSWER", "TYPEAHEAD", "CITY")]))


def test_questions_are_grouped_by_wording_with_the_line_that_answers_them(paths):
    write_questions(paths)
    report = build_report(paths, cli=HOME_CLI)
    assert [q.model_dump() for q in report.questions] == [
        {"question": SPONSOR, "holds": 3, "applications": 3, "backends": ["greenhouse", "lever"],
         "reason": "EXPLICIT_ANSWER_REQUIRED",
         "reasons": {"EXPLICIT_ANSWER_REQUIRED": 2, "AMBIGUOUS": 1},
         "semantic_type": "SPONSORSHIP", "control_type": "SELECT", "category": "explicit_answer",
         # The untyped sample saves an untyped global answer, which every field with this
         # wording can use, whatever its semantic type.
         "sample_application_id": "app_two", "field_id": "cards[abc][field0]",
         "answer": "interviewmaxxing --home '/tmp/fictional home' answer app_two --set "
                   "'cards[abc][field0]=VALUE' --reuse global",
         "act": None, "application_ids": ["app_one", "app_two", "app_three"]},
        {"question": "Sign in", "holds": 1, "applications": 1, "backends": ["greenhouse"],
         "reason": "USER_ACTION", "reasons": {"USER_ACTION": 1}, "semantic_type": None,
         "control_type": None, "category": "other", "sample_application_id": "app_one",
         "field_id": None, "answer": None,
         "act": "interviewmaxxing --home '/tmp/fictional home' resume app_one --act",
         "application_ids": ["app_one"]},
        {"question": WIDGET, "holds": 1, "applications": 1, "backends": ["lever"],
         "reason": "UNSUPPORTED_CONTROL", "reasons": {"UNSUPPORTED_CONTROL": 1},
         "semantic_type": None, "control_type": "UNSUPPORTED", "category": "custom_control",
         "sample_application_id": "app_two", "field_id": "start", "answer": None,
         "act": "interviewmaxxing --home '/tmp/fictional home' resume app_two --act",
         "application_ids": ["app_two"]},
        {"question": CITY, "holds": 1, "applications": 1, "backends": ["greenhouse"],
         "reason": "NO_ANSWER", "reasons": {"NO_ANSWER": 1}, "semantic_type": "CITY",
         "control_type": "TYPEAHEAD", "category": "lookup", "sample_application_id": "app_three",
         "field_id": "city",
         "answer": "interviewmaxxing --home '/tmp/fictional home' answer app_three --set "
                   "city=VALUE --reuse global",
         "act": None, "application_ids": ["app_three"]},
    ]
    plain = build_report(paths, [B2])  # the default prefix, and one batch alone
    assert [q.answer for q in plain.questions] == [
        "interviewmaxxing answer app_three --set question_31=VALUE --reuse global",
        "interviewmaxxing answer app_three --set city=VALUE --reuse global"]

    text = render_report_markdown(report, top=2)
    lines = text.splitlines()
    assert "## Questions" in lines
    assert ("| 1 | Do you require visa sponsorship? | 3 | 3 | EXPLICIT_ANSWER_REQUIRED | SELECT | "
            "SPONSORSHIP | greenhouse, lever |") in lines
    assert ("1. `interviewmaxxing --home '/tmp/fictional home' answer app_two --set "
            "'cards[abc][field0]=VALUE' --reuse global`") in lines
    assert ("2. `interviewmaxxing --home '/tmp/fictional home' resume app_one --act`  "
            "(complete it in the browser window)") in lines
    assert "... and 2 more question(s) (all are in --json)" in lines
    assert "--set city=VALUE" not in text  # beyond --top 2
    assert SECRET not in text and SECRET not in report.model_dump_json()


def test_long_question_wording_is_cut_and_groups_by_the_ledger_cut(paths):
    long_a = LONG + " (first fictional variant)"
    long_b = LONG + " (second fictional variant, which the ledger's 120 characters hide)"
    write(paths,
          entry(B1, "long-a", "needs_input", minute=1, app="app_a", items=[
              held(long_a[:119] + ELLIPSIS, "why", control="TEXTAREA")]),
          entry(B1, "long-b", "needs_input", minute=2, app="app_b", items=[
              held(long_b[:119] + ELLIPSIS, "why", control="TEXTAREA")]))
    [group] = build_report(paths).questions
    assert (group.question, group.holds, group.applications) == (LONG_SHOWN, 2, 2)


def test_older_lines_take_field_ids_from_the_store(paths):
    url = f"{ORIGIN}/older"
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request("default", url).application
        claim = store.claim(app.id, "test")
        store.transition(claim, S.INSPECTING)
        store.transition(claim, S.NEEDS_INPUT, metadata={"reason": "x", "missing_inputs": [
            missing(url, "question_77", SPONSOR, MissingReason.EXPLICIT_ANSWER_REQUIRED,
                    ControlType.SELECT).model_dump(mode="json"),
            missing(url, "city_lookup", CITY, MissingReason.NO_ANSWER,
                    ControlType.TYPEAHEAD).model_dump(mode="json")]})
        store.release(claim)
    # A line from before field ids (missing_items without them) and one from before
    # missing_items (labels and reasons only), both for the same stored application.
    write(paths, entry(B1, "older", "needs_input", minute=1, app=app.id, items=[
        item(SPONSOR, "EXPLICIT_ANSWER_REQUIRED", "SELECT")]))
    append_old_line(paths, B2, "oldest", minute=2, app=app.id,
                    reasons=["EXPLICIT_ANSWER_REQUIRED", "NO_ANSWER"], labels=[SPONSOR, CITY])
    by_question = {q.question: q for q in build_report(paths, [B1]).questions}
    assert by_question[SPONSOR].answer == \
        f"interviewmaxxing answer {app.id} --set question_77=VALUE --reuse global"
    by_question = {q.question: q for q in build_report(paths, [B2]).questions}
    assert by_question[CITY].answer == \
        f"interviewmaxxing answer {app.id} --set city_lookup=VALUE --reuse global"
    assert by_question[CITY].control_type == "TYPEAHEAD"
    without_store = LocalPaths.from_env({}, home=paths.home.parent / "no-store")
    write(without_store, entry(B1, "older", "needs_input", minute=1, app="app_gone", items=[
        item(SPONSOR, "EXPLICIT_ANSWER_REQUIRED", "SELECT")]))
    [group] = build_report(without_store).questions
    assert (group.sample_application_id, group.field_id, group.answer, group.act) == \
        ("app_gone", None, None, None)
    assert ("1. no field id recorded; `status app_gone` shows the question and its field id"
            in render_report_markdown(build_report(without_store)).splitlines())


# --- fill failures (WP9) ----------------------------------------------------------------------

NOTICE = "What is your notice period?"


def failed(store: ApplicationStore, url: str, reason: str, fields: Any = None) -> str:
    app = store.record_request("default", url).application
    claim = store.claim(app.id, "test")
    store.transition(claim, S.INSPECTING)
    metadata = {} if fields is None else {"failed_fields": fields}
    store.transition(claim, S.FAILED_RETRYABLE, failure_reason=reason, metadata=metadata)
    store.release(claim)
    return app.id


def test_fill_failures_are_grouped_by_failed_field_detail(paths, clock):
    mismatch = {"field_id": "notice", "label": NOTICE, "status": "VERIFICATION_MISMATCH"}
    with ApplicationStore.open(paths.state_db, clock=clock) as store:  # 2026-09-22, before T0
        one = failed(store, f"{ORIGIN}/f1", "Could not fill notice, city reliably; nothing was "
                     "submitted.", [mismatch | {"detail": "read back 'One month' instead of "
                                                          "'Two weeks'"},
                                    {"field_id": "city", "label": CITY, "status": "FAILED",
                                     "detail": "no option matched \"Austin, TX\""}])
        two = failed(store, f"{ORIGIN}/f2", "Could not fill notice reliably.",
                     [mismatch | {"detail": "read back 'Three months' instead of 'Two weeks'"}])
        timeout = ("Stopped by a browser error (TimeoutError: Timeout 30000ms exceeded.). "
                   "Nothing was submitted; resume to retry.")
        three = failed(store, f"{ORIGIN}/f3", timeout, "not a list")
        four = failed(store, f"{ORIGIN}/f4", timeout.replace("30000", "45000"),
                      ["not an object", {"status": "FAILED"}])
        five = failed(store, f"{ORIGIN}/f5", "First failure.", [mismatch | {"detail": "first"}])
        clock.advance(days=3)  # after the ledger lines: a later run's failure is not theirs
        claim = store.claim(five, "test")
        store.transition(claim, S.INSPECTING)
        store.transition(claim, S.FAILED_RETRYABLE, failure_reason="Later failure.",
                         metadata={"failed_fields": [mismatch | {"detail": "later"}]})
        store.release(claim)
    write(paths,
          *(entry(B1, f"f{n}", "failed_retryable", minute=n, duration=5.0, app=app)
            for n, app in enumerate((one, two, three, four, five), 1)),
          entry(B1, "f6", "failed_retryable", minute=6, duration=5.0, app="app_not_stored",
                backend="lever", message="Could not fill email for avery@example.test "
                "reliably; nothing was submitted. Provider cost: USD 0.0100 for 2 call(s)."),
          entry(B1, "e1", "error", minute=7, duration=900.0,
                message="timed out after 900 s; the run was stopped (SIGTERM); nothing was "
                        "submitted"),
          entry(B1, "e2", "error", minute=8, duration=1.0,
                message="Traceback: /Users/fictional/private/path.py line 1"))
    report = build_report(paths)
    groups = [(g.kind, g.status, g.detail, g.failures, g.applications, g.labels)
              for g in report.fill_failures]
    assert groups == [
        ("field", "VERIFICATION_MISMATCH", "read back '…' instead of '…'", 2, 2, [NOTICE]),
        ("run", None, "Stopped by a browser error (TimeoutError: Timeout #ms exceeded.). Nothing "
                      "was submitted; resume to retry.", 2, 2, []),
        ("field", "FAILED", "no option matched '…'", 1, 1, [CITY]),
        ("field", "VERIFICATION_MISMATCH", "first", 1, 1, [NOTICE]),
        ("run", None, "Could not fill email for <email> reliably; nothing was submitted.", 1, 1,
         []),
        ("error", None, "timed out; the job was stopped", 1, 0, []),
        ("error", None, "the CLI printed no readable outcome", 1, 0, []),
    ]
    first = report.fill_failures[0]
    assert (first.sample_application_id, first.application_ids, first.backends) == \
        (one, [one, two], ["greenhouse"])
    text = render_report_markdown(report)
    assert "## Fill failures" in text.splitlines()
    for private in ("One month", "Three months", "Austin", "avery@example.test", "fictional/private",
                    "Later failure", "Provider cost", "30000"):
        assert private not in text and private not in report.model_dump_json(), private


# --- per-backend readiness (WP9) -------------------------------------------------------------


def test_backend_readiness_table(paths):
    write(paths,
          entry(B1, "r1", "prepared", minute=1, duration=10.0, app="a1", provider_cost_usd=0.01,
                provider_calls=1),
          entry(B1, "r2", "prepared", minute=2, duration=20.0, app="a2", provider_cost_usd=0.03,
                provider_calls=2),
          entry(B1, "r3", "needs_input", minute=3, duration=30.0, app="a3"),
          entry(B1, "r4", "failed_retryable", minute=4, duration=5.0, app="a4",
                message="Could not reach the application form: the page is UNKNOWN, not an "
                        "application form."),
          entry(B1, "r5", "failed_retryable", minute=5, duration=7.0, app="a5",
                message="Could not fill resume reliably; nothing was submitted."),
          entry(B1, "r6", "error", minute=6, duration=900.0,
                message="timed out after 900 s; the run was stopped (SIGTERM); nothing was "
                        "submitted"),
          entry(B1, "r7", "closed", minute=7, duration=3.0, app="a7"),
          entry(B1, "r8", "duplicate", minute=8, duration=4.0, app="a8"),
          entry(B1, "r9", "already_recorded", minute=9, app="a9"),
          entry(B1, "l1", "needs_input", minute=10, duration=12.0, app="b1", backend="lever"))
    report = build_report(paths)
    assert [b.model_dump() for b in report.backends] == [
        {"backend": "greenhouse", "applications": 9, "prepared": 2, "needs_input": 1,
         "failed": 2, "closed": 1, "no_form": 1, "other": 2, "prepared_rate": 0.286,
         "median_duration_s": 8.5, "provider_cost_usd": 0.04},
        {"backend": "lever", "applications": 1, "prepared": 0, "needs_input": 1, "failed": 0,
         "closed": 0, "no_form": 0, "other": 0, "prepared_rate": 0.0,
         "median_duration_s": 12.0, "provider_cost_usd": None},
    ]
    lines = render_report_markdown(report).splitlines()
    assert "## Backend readiness" in lines
    assert "| greenhouse | 9 | 2 | 1 | 2 | 1 | 1 | 2 | 29% | 8.5 | 0.0400 |" in lines
    assert "| lever | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0% | 12.0 | - |" in lines


# --- several batches, --since and the JSON schema (WP9) --------------------------------------


def write_three_batches(paths: LocalPaths) -> None:
    write(paths, entry(B1, "x1", "prepared", minute=1, duration=5.0, app="app_x1"),
          entry(B2, "x2", "needs_input", minute=1500, duration=5.0, app="app_x2",
                items=[held(SPONSOR, "q", "EXPLICIT_ANSWER_REQUIRED", "SELECT")]),
          entry(B3, "x3", "failed_retryable", minute=2900, duration=5.0, app="app_x3"),
          entry(B3, "x1", "needs_input", minute=2901, duration=5.0, app="app_x1", attempt=2))


def test_several_batches_or_since_combine_ledgers(paths, capsys):
    write_three_batches(paths)
    both = build_report(paths, [B3, B1])
    assert both.batches == [B1, B3] and both.rows == 2 and both.totals == {
        "needs_input": 1, "failed_retryable": 1}  # x1's latest line is B3's
    since = build_report(paths, since=datetime(2026, 9, 24, tzinfo=UTC))
    assert since.batches == [B2, B3] and since.rows == 3
    assert since.since == datetime(2026, 9, 24, tzinfo=UTC)
    naive = build_report(paths, since=datetime(2026, 9, 25))  # read as UTC
    assert naive.batches == [B3]
    assert build_report(paths, since=datetime(2031, 1, 1, tzinfo=UTC)).batches == []

    home = str(paths.home)
    assert main(["--home", home, "batch-report", B1, B3, "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["batches"] == [B1, B3]
    assert main(["--home", home, "batch-report", "--since", "2026-09-24", "--json"]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["batches"] == [B2, B3] and data["since"].startswith("2026-09-24T00:00:00")
    assert main(["--home", home, "batch-report", "--since", "2026-09-25T00:30:00+02:00",
                 "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["batches"] == [B3]
    assert main(["--home", home, "batch-report", "--since", "2031-01-01"]) == EXIT_OK
    assert "with a line finished since 2031-01-01" in capsys.readouterr().out
    assert main(["--home", home, "batch-report", B1, "--since", "2026-09-24"]) == EXIT_USAGE
    assert "not both" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main(["--home", home, "batch-report", "--since", "yesterday"])
    assert exc.value.code == EXIT_USAGE
    assert main(["--home", home, "batch-report", B1, "nope"]) == EXIT_ERROR


def test_report_json_schema(paths, capsys):
    write_questions(paths)
    write_three_batches(paths)
    write(paths, entry(B1, "fx", "failed_retryable", minute=9, duration=2.0, app="app_fx",
                       message="Could not reach the application form: HTTP 403."))
    assert main(["--home", str(paths.home), "batch-report", "--json"]) == EXIT_OK
    out = capsys.readouterr().out
    data = json.loads(out)
    assert set(data) == set(BatchReport.model_fields) >= REPORT_KEYS | {
        "since", "questions", "fill_failures", "backends", "ledger_lines_ignored"}
    assert data["questions"] and all(set(q) == QUESTION_KEYS for q in data["questions"])
    assert data["fill_failures"] and all(set(f) == FAILURE_KEYS for f in data["fill_failures"])
    assert data["backends"] and all(set(b) == BACKEND_KEYS for b in data["backends"])
    assert BatchReport.model_validate_json(out).model_dump(mode="json") == data
    schema = BatchReport.model_json_schema()
    assert set(schema["properties"]) == set(data)
    definitions = schema["$defs"]
    for name, keys in (("HoldGroup", QUESTION_KEYS), ("FailureGroup", FAILURE_KEYS),
                       ("BackendReadiness", BACKEND_KEYS)):
        assert set(definitions[name]["properties"]) == keys, name
