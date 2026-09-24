"""Upload-first filling on the localhost mock ATS (fictional employer, fictional data).

``autofill-upload`` (Ashby-like) parses the attached resume and, a moment later,
overwrites the name and email fields with what it parsed. ``custom-uploader``
(Greenhouse-like) hides its resume input behind an "Upload resume" button, moves the
chosen file into page state (clearing the input) and shows a chip and an "Uploading…"
notice, beside an optional cover letter whose visually hidden input is wrapped in its
label.

The runtime attaches every file before typing anything, waits for the site to finish
with it, reads it back from the input, the chip or the notice, and only then types the
candidate's own values over whatever the site filled in; a second fill of the same
document attaches nothing. Real headless Chromium; nothing is ever submitted.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_core import (
    AnswerSource,
    ApplicationForm,
    ApplicationPacket,
    ArtifactRef,
    BrowserOptions,
    ControlType,
    FieldFillStatus,
    FileValue,
    PacketAnswer,
    PageKind,
    Provenance,
    SemanticType,
    UserInput,
)

AUTOFILL = "/jobs/autofill-upload/apply"
UPLOADER = "/jobs/custom-uploader/apply"
OURS = {
    "first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
    "phone": "+1 (303) 555-0142", "linkedin_url": "https://www.linkedin.example.test/in/avery-quill",
}
"""The candidate's own values (tests/fixtures/browser/candidate.json) by field name."""
AUTOFILL_IDS = ["first_name", "last_name", "email", "phone", "linkedin_url", "resume"]
CONTACT = ["first_name", "last_name", "email"]
RESUME_FILE = {"name": "resume_avery_quill.pdf", "size": 802}
"""The pinned fixture resume as the page sees it."""
MOCK = "() => JSON.parse(JSON.stringify({...window.__mock, files: undefined}))"
VALUES = """(names) => Object.fromEntries(names.map((name) => {
  const control = document.querySelector(`form [name="${name}"]`);
  return [name, control ? control.value : null];
}))"""
FILES = "(input) => Array.from(input.files).map((f) => ({name: f.name, size: f.size}))"
TARGETS = """(selector) => Array.from(document.querySelectorAll(selector))
  .map((el) => [el.textContent.trim(), el.getAttribute('type')])"""
UPLOADER_STATE = """() => {
  const kept = (window.__mock.files || {}).resume;
  const chip = document.querySelector('#resume-chip');
  const notice = document.querySelector('#resume-notice');
  return {
    input: Array.from(document.querySelector('#resume-input').files).map((f) => f.name),
    page: kept ? {name: kept.name, size: kept.size} : null,
    chip: chip.hidden ? null : (chip.querySelector('.file-chip__name') || chip).textContent.trim(),
    notice: [notice.textContent.trim(), notice.getAttribute('aria-busy')],
    cover: Array.from(document.querySelector('#cover-letter-input').files)
      .map((f) => ({name: f.name, size: f.size})),
  };
}"""


async def _session(options: BrowserOptions) -> Any:
    return await PlaywrightSessionFactory().start(options)


def _events(mock: dict[str, Any]) -> list[str]:
    return [entry["event"] for entry in mock["log"]]


def _at(events: list[str], event: str) -> int:
    """Position of the first ``event`` in the page's log (which must contain it)."""
    assert event in events, (event, events)
    return events.index(event)


def _typed(events: list[str], names: Iterable[str]) -> list[int]:
    """Positions of the trusted input events on the named text controls."""
    wanted = {f"input:{name}" for name in names}
    return [i for i, event in enumerate(events) if event in wanted]


def _cover_letter(tmp_path: Path) -> ArtifactRef:
    path = tmp_path / "cover_letter_avery_quill.txt"
    path.write_text("Avery Quill: a fictional cover letter for Brambleway Analytics (test fixture).\n")
    return ArtifactRef.from_file(path, id="cover-letter.avery-quill", media_type="text/plain")


def _packet(kit: SimpleNamespace, form: ApplicationForm, cover: ArtifactRef) -> ApplicationPacket:
    """Identity and resume answers, plus the cover letter the (fictional) user supplied
    for this application when the form asks for one."""
    packet: ApplicationPacket = kit.build(form, kit.pick(form, kit.CORE)).packet
    field = form.find("cover_letter")
    if field is None:
        return packet
    value = FileValue(artifact=cover)
    supplied = UserInput.for_field(form, field.id, value)
    answer = PacketAnswer(field_id=field.id, semantic_type=field.semantic_type, value=value,
                          provenance=Provenance(source=AnswerSource.USER_INPUT,
                                                reference_ids=[supplied.id]))
    return packet.model_copy(update={"answers": [*packet.answers, answer]})


def test_hidden_file_inputs_are_file_questions_and_the_upload_button_is_no_action(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, list[Any], dict[str, Any]]:
        browser = await _session(options)
        try:
            page = await browser.open(server.url(UPLOADER))
            assert page.form is not None, page.message
            targets = await browser.page.evaluate(TARGETS, page.form.submit_selector)
            return page, targets, await browser.page.evaluate(MOCK)
        finally:
            await browser.close()

    page, targets, mock = kit.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM, page.message
    form = page.form
    assert [f.id for f in form.fields] == [*CONTACT, "resume", "cover_letter"]
    resume, cover = form.field("resume"), form.field("cover_letter")
    # A display:none input with no label of its own, named by its "Resume/CV" group
    # (aria-required, while the input itself is not required).
    assert (resume.control_type, resume.semantic_type, resume.label, resume.required) == (
        ControlType.FILE, SemanticType.RESUME, "Resume/CV", True)
    # A visually hidden input wrapped in its "Attach cover letter" label.
    assert (cover.control_type, cover.semantic_type, cover.required) == (
        ControlType.FILE, SemanticType.COVER_LETTER, False)
    assert "cover letter" in cover.label.lower(), cover.label
    # "Upload resume" and the drop zone are neither questions nor the form's action.
    assert form.is_final_step is True and form.next_selector is None
    assert targets == [["Submit application", "submit"]]
    # Inspection chose no file.
    assert (mock["uploads"], mock["coverUploads"]) == (0, 0)
    assert server.submissions("custom-uploader")["accepted_count"] == 0


@pytest.mark.parametrize("query", ["", "?autofill_ms=1500"], ids=["parser", "slow-parser"])
def test_the_resume_goes_first_and_the_parsed_values_are_replaced_by_ours(
    query: str, kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, dict[str, Any], dict[str, Any], list[Any]]:
        browser = await _session(options)
        try:
            page = await browser.open(server.url(AUTOFILL + query))
            assert page.form is not None, page.message
            packet = kit.build(page.form, kit.pick(page.form, kit.CORE)).packet
            fill = await browser.fill(page.form, packet)
            return (page, fill, await browser.page.evaluate(MOCK),
                    await browser.page.evaluate(VALUES, list(OURS)),
                    await browser.page.eval_on_selector("#f-resume", FILES))
        finally:
            await browser.close()

    page, fill, mock, values, files = kit.run(scenario())
    assert [f.id for f in page.form.fields] == AUTOFILL_IDS
    assert fill.ok, [f for f in fill.fields if f.status is not FieldFillStatus.FILLED]
    assert {f.field_id: f.status for f in fill.fields} == dict.fromkeys(
        AUTOFILL_IDS, FieldFillStatus.FILLED)
    events = _events(mock)
    typed = _typed(events, OURS)
    assert typed, events
    # Attached once and parsed once, the resume first although it is the last field, and
    # nothing of ours typed before the parser's values had landed.
    assert (events.count("upload"), events.count("autofill")) == (1, 1), events
    assert _at(events, "upload") < _at(events, "autofill") < min(typed), events
    assert (mock["uploads"], mock["autofills"]) == (1, 1)
    assert values == OURS  # every parsed value was overwritten with the candidate's own
    assert files == [RESUME_FILE]
    assert server.submissions("autofill-upload")["accepted_count"] == 0


def test_a_resume_moved_into_page_state_is_read_back_from_its_chip_and_notice(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, tmp_path: Path
) -> None:
    cover = _cover_letter(tmp_path)

    async def scenario() -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]:
        browser = await _session(options)
        try:
            page = await browser.open(server.url(UPLOADER))  # the default 800 ms "Uploading…"
            assert page.form is not None, page.message
            fill = await browser.fill(page.form, _packet(kit, page.form, cover))
            return (fill, await browser.page.evaluate(MOCK),
                    await browser.page.evaluate(UPLOADER_STATE),
                    await browser.page.evaluate(VALUES, CONTACT))
        finally:
            await browser.close()

    fill, mock, state, values = kit.run(scenario())
    assert fill.ok, [f for f in fill.fields if f.status is not FieldFillStatus.FILLED]
    assert {f.field_id: f.status for f in fill.fields} == dict.fromkeys(
        [*CONTACT, "resume", "cover_letter"], FieldFillStatus.FILLED)
    # The site cleared its input: only the chip and the notice show the resume, and the
    # page state holds exactly the pinned file.
    assert state["input"] == [] and state["page"] == RESUME_FILE
    assert state["chip"] == RESUME_FILE["name"]
    assert state["notice"] == [f"{RESUME_FILE['name']} uploaded", "false"]
    # The label-wrapped cover-letter input keeps the candidate's own document.
    assert state["cover"] == [{"name": cover.filename, "size": cover.size_bytes}]
    events = _events(mock)
    typed = _typed(events, CONTACT)
    assert typed, events
    # Files first, in DOM order; nothing typed before the resume upload had finished.
    assert _at(events, "upload") < _at(events, "cover-upload") < min(typed), events
    assert _at(events, "uploaded") < min(typed), events
    assert (mock["uploads"], mock["coverUploads"]) == (1, 1)
    assert "removed" not in events  # the chip's "Remove file" was never pressed
    assert values == {name: OURS[name] for name in CONTACT}
    assert server.submissions("custom-uploader")["accepted_count"] == 0


@pytest.mark.parametrize(("job", "query", "file_ids"), [
    ("autofill-upload", "?autofill_ms=300", ["resume"]),  # the input keeps its file
    ("custom-uploader", "?upload_ms=300", ["resume", "cover_letter"]),  # a chip shows the resume
], ids=["kept-by-the-input", "moved-into-page-state"])
def test_a_second_fill_attaches_nothing_again(
    job: str, query: str, file_ids: list[str], kit: SimpleNamespace, server: Any,
    options: BrowserOptions, tmp_path: Path,
) -> None:
    cover = _cover_letter(tmp_path)

    async def scenario() -> tuple[Any, Any, dict[str, Any], dict[str, Any], dict[str, Any]]:
        browser = await _session(options)
        try:
            page = await browser.open(server.url(f"/jobs/{job}/apply{query}"))
            assert page.form is not None, page.message
            first = await browser.fill(page.form, _packet(kit, page.form, cover))
            before = await browser.page.evaluate(MOCK)
            again = await browser.inspect()  # as the runner re-inspects before filling again
            assert again.form is not None, again.message
            second = await browser.fill(again.form, _packet(kit, again.form, cover))
            names = [f.id for f in again.form.fields if f.id in OURS]
            return (first, second, before, await browser.page.evaluate(MOCK),
                    await browser.page.evaluate(VALUES, names))
        finally:
            await browser.close()

    first, second, before, after, values = kit.run(scenario())
    assert first.ok, [f for f in first.fields if f.status is not FieldFillStatus.FILLED]
    assert second.ok, [f for f in second.fields if f.status is not FieldFillStatus.FILLED]
    for field_id in file_ids:
        [attached] = [f for f in first.fields if f.field_id == field_id]
        [kept] = [f for f in second.fields if f.field_id == field_id]
        assert attached.status is FieldFillStatus.FILLED
        assert "already attached" not in (attached.detail or ""), attached
        assert kept.status is FieldFillStatus.FILLED
        assert "already attached" in (kept.detail or ""), kept
    # Nothing reached the page again: one upload (and one parse) for the whole document.
    counts = {key: after[key] for key in ("uploads", "autofills", "coverUploads") if key in after}
    assert counts == {key: before[key] for key in counts}
    assert set(counts.values()) == {1}, counts
    assert _events(after).count("upload") == 1
    assert values and values == {name: OURS[name] for name in values}
    assert server.submissions(job)["accepted_count"] == 0
