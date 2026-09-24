"""The mock ATS's upload and autofill scenarios: what its server accepts (only the fixture
candidate's own values and resume) and what their page scripts do in real headless
Chromium, driven directly through Playwright rather than through the runtime.

Everything is local and fictional (Brambleway Analytics, candidate Avery Quill). The
server checks post to the localhost mock directly (test fixture traffic only).
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from playwright.async_api import FilePayload, Page, async_playwright

REPO = Path(__file__).resolve().parents[2]
RESUME_PATH = REPO / "tests" / "fixtures" / "browser" / "resume_avery_quill.pdf"
RESUME_BYTES = RESUME_PATH.read_bytes()
RESUME_SHA256 = hashlib.sha256(RESUME_BYTES).hexdigest()
OTHER_PDF = b"%PDF-1.4 other"
MOCK = "() => JSON.parse(JSON.stringify({...window.__mock, files: undefined}))"
NOT_ENTERED = "This value was not entered by the candidate."
WRONG_RESUME = "The attached resume is not the file the candidate chose."

Upload = tuple[str, str, bytes]
"""A multipart file part: field name, file name, bytes."""

RESUME: Upload = ("resume", RESUME_PATH.name, RESUME_BYTES)
OTHER_RESUME: Upload = ("resume", "resume.pdf", OTHER_PDF)
COVER_LETTER: Upload = ("cover_letter", "cover_letter.txt", b"Dear Brambleway Analytics team")
AVERY = [("first_name", "Avery"), ("last_name", "Quill"), ("email", "avery.quill@example.test")]
PHONE = ("phone", "+1 (303) 555-0142")
LINKEDIN_URL = "https://www.linkedin.example.test/in/avery-quill"
PARSED = [("first_name", "A."), ("last_name", "Quill (resume)"),
          ("email", "a.quill@resume-parser.example.test")]
"""What the autofill-upload resume parser writes into the form."""
LINKEDIN_MEMBER = [("name", "LinkedIn Member"), ("email", "member@linkedin.example.test")]
"""What the linkedin-autofill button and prompt write into the form."""
VALID: dict[str, list[tuple[str, str]]] = {
    "autofill-upload": [*AVERY, PHONE, ("linkedin_url", LINKEDIN_URL)],
    "custom-uploader": AVERY,
    "linkedin-autofill": [("name", "Avery Quill"), ("email", "avery.quill@example.test"), PHONE,
                          ("location", "Denver, CO"), ("urls[LinkedIn]", LINKEDIN_URL)],
    "react-controlled": [*AVERY, PHONE],
}


def _post(server: Any, job: str, fields: list[tuple[str, str]],
          files: list[Upload] | None = None) -> tuple[int, str, str]:
    """POST a multipart application without following the redirect: (status, Location, body)."""
    boundary = "----UploadsFixture" + uuid.uuid4().hex
    body = bytearray()
    for name, value in fields:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                 f"{value}\r\n").encode()
    for name, filename, data in files or []:
        ctype = "application/pdf" if filename.endswith(".pdf") else "application/octet-stream"
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                 f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n').encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    origin = urlsplit(server.origin)
    connection = http.client.HTTPConnection(origin.hostname or "127.0.0.1", origin.port, timeout=10)
    try:
        connection.request("POST", f"/jobs/{job}/apply", body=bytes(body),
                           headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        response = connection.getresponse()
        return response.status, response.getheader("Location") or "", response.read().decode()
    finally:
        connection.close()


def _retained_upload(page: str) -> str | None:
    match = re.search(r'name="resume_upload_id" value="(upl_\d{6})"', page)
    return match.group(1) if match else None


# --- the server: only the candidate's own values and resume ------------------------------


@pytest.mark.parametrize("job", sorted(VALID))
def test_the_candidates_own_values_and_resume_are_accepted(job: str, server: Any) -> None:
    status, location, _ = _post(server, job, VALID[job], [RESUME])
    assert status == 303 and re.fullmatch(r"/applications/sub_\d{6}", location), (status, location)
    summary = server.submissions(job)
    assert (summary["accepted_count"], summary["rejected_count"]) == (1, 0)
    [record] = summary["submissions"]
    assert record["fields"] == dict(VALID[job])
    assert record["files"]["resume"]["sha256"] == RESUME_SHA256


@pytest.mark.parametrize(("job", "fields", "files", "errors"), [
    ("autofill-upload", [*PARSED, PHONE], [RESUME],
     {"first_name": NOT_ENTERED, "last_name": NOT_ENTERED, "email": NOT_ENTERED}),
    ("autofill-upload", VALID["autofill-upload"], [OTHER_RESUME], {"resume": WRONG_RESUME}),
    ("custom-uploader", AVERY, [OTHER_RESUME], {"resume": WRONG_RESUME}),
    ("custom-uploader", AVERY, [], {"resume": "Attach a file."}),
    ("custom-uploader", [*AVERY[:2], PARSED[2]], [RESUME], {"email": NOT_ENTERED}),
    ("linkedin-autofill", [*LINKEDIN_MEMBER, PHONE], [RESUME], {"name": NOT_ENTERED, "email": NOT_ENTERED}),
    ("linkedin-autofill", VALID["linkedin-autofill"], [OTHER_RESUME], {"resume": WRONG_RESUME}),
    ("react-controlled", [AVERY[0], PARSED[1], AVERY[2], PHONE], [RESUME], {"last_name": NOT_ENTERED}),
    ("react-controlled", VALID["react-controlled"], [OTHER_RESUME], {"resume": WRONG_RESUME}),
    # The shared validation speaks first: a missing value is not also "not entered".
    ("react-controlled", [AVERY[0], AVERY[2], PHONE], [OTHER_RESUME],
     {"last_name": "This field is required.", "resume": WRONG_RESUME}),
])
def test_values_the_candidate_did_not_enter_are_rejected(
    job: str, fields: list[tuple[str, str]], files: list[Upload], errors: dict[str, str], server: Any
) -> None:
    status, _, page = _post(server, job, fields, files)
    assert status == 422
    assert "There is a problem with your application" in page
    assert all(message in page for message in errors.values())
    summary = server.submissions(job)
    assert (summary["accepted_count"], summary["rejected_count"]) == (0, 1)
    assert summary["rejections"][0]["errors"] == errors


def test_the_custom_uploader_takes_an_optional_cover_letter_validated_like_a_file(server: Any) -> None:
    status, _, _ = _post(server, "custom-uploader", AVERY, [RESUME, COVER_LETTER])
    assert status == 303
    status, _, page = _post(server, "custom-uploader", AVERY,
                            [RESUME, ("cover_letter", "cover_letter.exe", b"MZ")])
    assert status == 422 and "Upload a PDF, DOC, DOCX or TXT file." in page
    assert 'href="#cover-letter-field"' in page  # the error summary links to the uploader
    assert "id=&quot;cover-letter-error&quot;" in page  # its inline error, in the mounted markup
    summary = server.submissions("custom-uploader")
    assert (summary["accepted_count"], summary["rejected_count"]) == (1, 1)
    files = summary["submissions"][0]["files"]
    assert files["resume"]["sha256"] == RESUME_SHA256
    assert (files["cover_letter"]["filename"], files["cover_letter"]["size"]) == (
        COVER_LETTER[1], len(COVER_LETTER[2]))
    assert summary["rejections"][0]["errors"] == {"cover_letter": "Upload a PDF, DOC, DOCX or TXT file."}


def test_a_retained_resume_is_compared_by_its_stored_hash(server: Any) -> None:
    # The fixture resume of a rejected POST is kept, and posting its id is accepted.
    status, _, page = _post(server, "autofill-upload", [*PARSED, PHONE], [RESUME])
    assert status == 422 and "Currently attached: resume_avery_quill.pdf" in page
    upload_id = _retained_upload(page)
    assert upload_id is not None
    status, _, _ = _post(server, "autofill-upload", [*AVERY, PHONE, ("resume_upload_id", upload_id)])
    assert status == 303
    [record] = server.submissions("autofill-upload")["submissions"]
    assert record["files"]["resume"]["upload_id"] == upload_id

    # Another file kept by an ordinary job is not the candidate's resume here, and a
    # rejected file is not kept for the next attempt.
    status, _, page = _post(server, "standard", [], [OTHER_RESUME])
    other_id = _retained_upload(page)
    assert status == 422 and other_id is not None
    status, _, page = _post(server, "react-controlled", [*AVERY, PHONE, ("resume_upload_id", other_id)])
    assert status == 422 and WRONG_RESUME in page
    assert _retained_upload(page) is None
    assert server.submissions("react-controlled")["rejections"][0]["errors"] == {"resume": WRONG_RESUME}


def test_without_the_fixture_file_no_resume_is_the_candidates(
    kit: SimpleNamespace, server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(kit.mock_ats, "FIXTURE_RESUME", tmp_path / "missing.pdf")
    kit.mock_ats.fixture_resume_sha256.cache_clear()
    try:
        status, _, _ = _post(server, "react-controlled", VALID["react-controlled"], [RESUME])
    finally:
        monkeypatch.undo()
        kit.mock_ats.fixture_resume_sha256.cache_clear()
    assert status == 422
    assert server.submissions("react-controlled")["rejections"][0]["errors"] == {"resume": WRONG_RESUME}


def test_the_catalog_marks_the_upload_scenarios_fixture_identity(server: Any) -> None:
    jobs = {job["job_id"]: job for job in server.api("GET", "/__test__/jobs")["jobs"]}
    expected = {
        "autofill-upload": ("BWA-AS-130", ["first_name", "last_name", "email", "phone",
                                           "linkedin_url", "resume"]),
        "custom-uploader": ("BWA-GH-131", ["first_name", "last_name", "email", "resume",
                                           "cover_letter"]),
        "linkedin-autofill": ("BWA-LV-132", ["resume", "name", "email", "phone", "location",
                                             "urls[LinkedIn]"]),
        "react-controlled": ("BWA-RC-133", ["first_name", "last_name", "email", "phone", "resume"]),
    }
    for job_id, (code, names) in expected.items():
        job = jobs[job_id]
        assert (job["job_code"], job["fixture_identity"]) == (code, True)
        assert [f["name"] for step in job["steps"] for f in step["fields"]] == names
    uploader = {f["name"]: (f["kind"], f["required"]) for f in jobs["custom-uploader"]["steps"][0]["fields"]}
    assert uploader["resume"] == ("custom_file", True)
    assert uploader["cover_letter"] == ("label_file", False)
    assert not any(job["fixture_identity"] for job_id, job in jobs.items() if job_id not in expected)


# --- the page scripts in real headless Chromium ------------------------------------------


def _in_chromium[T](url: str, check: Callable[[Page], Awaitable[T]]) -> T:
    async def scenario() -> T:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url)
                return await check(page)
            finally:
                await browser.close()

    return asyncio.run(scenario())


async def _mock(page: Page) -> dict[str, Any]:
    result: dict[str, Any] = await page.evaluate(MOCK)
    return result


async def _wait_for(page: Page, event: str, timeout: float = 3000) -> None:
    await page.wait_for_function("event => window.__mock.log.some(e => e.event === event)",
                                 arg=event, timeout=timeout)


def _entry(mock: dict[str, Any], event: str) -> dict[str, Any]:
    return next(e for e in mock["log"] if e["event"] == event)


def _events(mock: dict[str, Any]) -> list[str]:
    return [e["event"] for e in mock["log"]]


PARSE_STATUS = """() => {
  const status = document.getElementById('resume-parse-status');
  return {text: status.textContent, busy: status.getAttribute('aria-busy'), role: status.getAttribute('role'),
          live: status.getAttribute('aria-live'), after: status.previousElementSibling.id,
          spinner: !!status.querySelector('span.spinner[aria-hidden=true]'),
          count: document.querySelectorAll('#resume-parse-status').length};
}"""
CONTACT_VALUES = "() => ['f-first_name', 'f-last_name', 'f-email'].map(id => document.getElementById(id).value)"


def test_autofill_upload_overwrites_typed_names_600_ms_after_the_resume(server: Any) -> None:
    async def check(page: Page) -> None:
        for selector, value in (("#f-first_name", "Avery"), ("#f-last_name", "Quill"),
                                ("#f-email", "avery.quill@example.test")):
            await page.fill(selector, value)
        await page.set_input_files("#f-resume", RESUME_PATH)
        assert await page.evaluate(PARSE_STATUS) == {
            "text": "Parsing your resume\u2026", "busy": "true", "role": "status", "live": "polite",
            "after": "f-resume", "spinner": True, "count": 1}
        await page.wait_for_function("() => window.__mock.autofills === 1", timeout=3000)
        assert await page.evaluate(CONTACT_VALUES) == [value for _, value in PARSED]
        status = await page.evaluate(PARSE_STATUS)
        assert (status["text"], status["busy"]) == ("We filled in some fields from your resume.", "false")
        assert await page.evaluate("() => [...document.getElementById('f-resume').files].map(f => f.name)") == [
            RESUME_PATH.name]  # the input keeps its file
        mock = await _mock(page)
        # Trusted input events are logged: typing, and set_input_files with a file path (the
        # browser sets the files). The parser's script-made events are not.
        assert _events(mock) == ["input:first_name", "input:last_name", "input:email", "input:resume",
                                 "upload", "autofill"]
        assert _entry(mock, "upload")["detail"] == RESUME_PATH.name
        assert 590 <= _entry(mock, "autofill")["t"] - _entry(mock, "upload")["t"] <= 1500
        assert (mock["uploads"], mock["autofills"]) == (1, 1)

        # A second change starts over and counts again. (Playwright sets an in-memory file
        # by script, with untrusted events: no "input:resume" this time.)
        await page.set_input_files("#f-resume", files=[
            FilePayload(name="resume_v2.pdf", mimeType="application/pdf", buffer=OTHER_PDF)])
        status = await page.evaluate(PARSE_STATUS)
        assert (status["text"], status["busy"], status["count"]) == ("Parsing your resume\u2026", "true", 1)
        await page.wait_for_function("() => window.__mock.autofills === 2", timeout=3000)
        mock = await _mock(page)
        assert (mock["uploads"], mock["autofills"]) == (2, 2)
        assert _events(mock)[-2:] == ["upload", "autofill"]

    _in_chromium(server.url("/jobs/autofill-upload/apply"), check)


UPLOADER_STATE = """() => {
  const chip = document.getElementById('resume-chip'), notice = document.getElementById('resume-notice');
  const text = selector => { const e = chip.querySelector(selector); return e ? e.textContent : null; };
  const remove = chip.querySelector('button.file-chip__remove');
  return {files: document.getElementById('resume-input').files.length, chipHidden: chip.hidden,
          chip: chip.textContent, name: text('.file-chip__name'), size: text('.file-chip__size'),
          remove: remove && [remove.type, remove.getAttribute('aria-label')],
          notice: notice.textContent, busy: notice.getAttribute('aria-busy')};
}"""
FORM_FILE = """async name => {
  const file = new FormData(document.forms[0]).get(name);
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer()));
  return {name: file.name, size: file.size,
          sha256: [...digest].map(b => b.toString(16).padStart(2, '0')).join('')};
}"""


def test_custom_uploader_moves_the_file_into_page_state_and_posts_it(server: Any) -> None:
    async def check(page: Page) -> tuple[dict[str, Any], str]:
        markup = await page.evaluate("""() => {
          const input = document.getElementById('resume-input');
          const cover = document.getElementById('cover-letter-input');
          return {mounts: document.querySelectorAll('[data-mount-html]').length,
                  display: getComputedStyle(input).display, required: input.required,
                  labels: input.labels.length, named: input.hasAttribute('aria-label'),
                  inForm: input.form === document.forms[0],
                  group: document.getElementById('resume-field').getAttribute('aria-required'),
                  chipHidden: document.getElementById('resume-chip').hidden,
                  cover: [...cover.labels].map(l => l.dataset.testid), coverClass: cover.className};
        }""")
        assert markup == {"mounts": 0, "display": "none", "required": False, "labels": 0,
                          "named": False, "inForm": True, "group": "true", "chipHidden": True,
                          "cover": ["cover_letter"], "coverClass": "visually-hidden"}

        await page.set_input_files("#resume-input", RESUME_PATH)
        size = f"({len(RESUME_BYTES)} bytes)"
        assert await page.evaluate(UPLOADER_STATE) == {
            "files": 0, "chipHidden": False, "chip": f"{RESUME_PATH.name} {size} \u00d7",
            "name": RESUME_PATH.name, "size": size, "remove": ["button", "Remove file"],
            "notice": "Uploading\u2026", "busy": "true"}
        await _wait_for(page, "uploaded")
        state = await page.evaluate(UPLOADER_STATE)
        assert (state["notice"], state["busy"]) == (f"{RESUME_PATH.name} uploaded", "false")
        # The form's data carries the stored file although the input is empty.
        assert await page.evaluate(FORM_FILE, "resume") == {
            "name": RESUME_PATH.name, "size": len(RESUME_BYTES), "sha256": RESUME_SHA256}

        # The label-wrapped cover-letter input keeps its file.
        await page.set_input_files("#cover-letter-input", files=[
            FilePayload(name=COVER_LETTER[1], mimeType="text/plain", buffer=COVER_LETTER[2])])
        assert await page.evaluate("""() => [document.getElementById('cover-letter-input').files.length,
          document.getElementById('cover-letter-chip').hidden,
          document.getElementById('cover-letter-chip').textContent]""") == [1, False, COVER_LETTER[1]]

        mock = await _mock(page)
        for selector, value in AVERY:
            await page.fill(f"#f-{selector}", value)
        async with page.expect_navigation():
            await page.click("button[type=submit]")
        return mock, page.url

    mock, landed = _in_chromium(server.url("/jobs/custom-uploader/apply?upload_ms=200"), check)
    assert _events(mock) == ["input:resume", "upload", "uploaded", "cover-upload"]
    assert 190 <= _entry(mock, "uploaded")["t"] - _entry(mock, "upload")["t"] <= 1200
    assert (mock["uploads"], mock["coverUploads"]) == (1, 1)
    assert re.search(r"/applications/sub_\d{6}$", landed), landed
    [record] = server.submissions("custom-uploader")["submissions"]
    assert record["fields"] == dict(AVERY)
    assert record["files"]["resume"]["sha256"] == RESUME_SHA256
    assert record["files"]["cover_letter"]["filename"] == COVER_LETTER[1]


def test_custom_uploader_remove_button_and_drop_zone(server: Any) -> None:
    async def check(page: Page) -> dict[str, Any]:
        await page.set_input_files("#resume-input", RESUME_PATH)
        await page.click("#resume-chip .file-chip__remove")
        assert await page.evaluate(UPLOADER_STATE) == {
            "files": 0, "chipHidden": True, "chip": "", "name": None, "size": None, "remove": None,
            "notice": "", "busy": None}
        empty = await page.evaluate(FORM_FILE, "resume")  # only the cleared input is posted
        assert (empty["name"], empty["size"]) == ("", 0)

        transfer = await page.evaluate_handle("""() => {
          const transfer = new DataTransfer();
          transfer.items.add(new File(['%PDF-1.4 dropped'], 'dropped.pdf', {type: 'application/pdf'}));
          return transfer;
        }""")
        await page.dispatch_event("#resume-dropzone", "drop", {"dataTransfer": transfer})
        state = await page.evaluate(UPLOADER_STATE)
        assert (state["files"], state["chipHidden"], state["name"], state["notice"]) == (
            0, False, "dropped.pdf", "Uploading\u2026")
        assert (await page.evaluate(FORM_FILE, "resume"))["name"] == "dropped.pdf"
        return await _mock(page)

    # A long upload_ms keeps the notice pending, so the log holds no "uploaded".
    mock = _in_chromium(server.url("/jobs/custom-uploader/apply?upload_ms=5000"), check)
    assert _events(mock) == ["input:resume", "upload", "removed", "upload"]
    assert [e.get("detail") for e in mock["log"]] == [None, RESUME_PATH.name, None, "dropped.pdf"]
    assert mock["uploads"] == 2


BUTTON = """() => { const b = document.getElementById('linkedin-apply');
  return [b.textContent, b.getAttribute('aria-busy')]; }"""
MAIN = """() => { const main = document.getElementById('main'), prompt = document.getElementById('autofill-prompt');
  return {prompt: !!prompt, inBody: !!prompt && prompt.parentElement === document.body,
          inMain: !!prompt && main.contains(prompt), inert: main.hasAttribute('inert'),
          hidden: main.getAttribute('aria-hidden')}; }"""
LOAD_END = "() => performance.getEntriesByType('navigation')[0].loadEventStart"
NAME_EMAIL = "() => [document.getElementById('f-name').value, document.getElementById('f-email').value]"


def test_linkedin_button_loads_late_under_a_transparent_overlay(server: Any) -> None:
    async def check(page: Page) -> tuple[dict[str, Any], float]:
        assert await page.evaluate(BUTTON) == ["Loading\u2026", "true"]
        boxes = await page.evaluate("""() => ['linkedin-apply', 'linkedin-overlay'].map(id => {
          const r = document.getElementById(id).getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; })""")
        assert boxes[0] == boxes[1] and boxes[0][2] > 0, boxes  # the overlay covers the button exactly
        await _wait_for(page, "linkedin-ready")
        assert await page.evaluate(BUTTON) == ["Apply with LinkedIn", None]
        # A pointer click on the button lands on the overlay.
        box = await page.locator("#linkedin-apply").bounding_box()
        assert box is not None
        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        assert await page.evaluate(NAME_EMAIL) == ["", ""]
        # The button itself fills in a LinkedIn member.
        await page.dispatch_event("#linkedin-apply", "click")
        assert await page.evaluate(NAME_EMAIL) == [value for _, value in LINKEDIN_MEMBER]
        assert (await page.evaluate(MAIN))["prompt"] is False  # ?prompt=none
        return await _mock(page), await page.evaluate(LOAD_END)

    mock, loaded = _in_chromium(
        server.url("/jobs/linkedin-autofill/apply?loading_ms=300&prompt=none"), check)
    assert _events(mock) == ["linkedin-ready", "overlay-click", "linkedin-click"]
    assert 290 <= _entry(mock, "linkedin-ready")["t"] - loaded <= 1500
    assert (mock["overlayClicks"], mock["linkedinClicks"], mock["promptShown"]) == (1, 1, 0)


def test_the_autofill_prompt_makes_main_inert_until_no_thanks(server: Any) -> None:
    async def check(page: Page) -> tuple[dict[str, Any], float]:
        await page.wait_for_selector("#autofill-prompt", timeout=3000)
        assert await page.evaluate(MAIN) == {
            "prompt": True, "inBody": True, "inMain": False, "inert": True, "hidden": "true"}
        dialog = page.locator("#autofill-prompt")
        assert [await dialog.get_attribute(a) for a in ("role", "aria-modal", "aria-labelledby")] == [
            "dialog", "true", "autofill-prompt-title"]
        assert await page.text_content("#autofill-prompt-title") == "Autofill your application?"
        await page.click("#autofill-prompt-dismiss")
        assert await page.evaluate(MAIN) == {
            "prompt": False, "inBody": False, "inMain": False, "inert": False, "hidden": None}
        await page.wait_for_timeout(300)  # a dismissed prompt never returns
        assert (await page.evaluate(MAIN))["prompt"] is False
        await page.fill("#f-name", "Avery Quill")  # main is usable again
        return await _mock(page), await page.evaluate(LOAD_END)

    mock, loaded = _in_chromium(
        server.url("/jobs/linkedin-autofill/apply?loading_ms=300&prompt_ms=100"), check)
    assert (mock["promptShown"], mock["promptDismissed"], mock["promptAccepted"]) == (1, 1, 0)
    assert _events(mock)[:2] == ["prompt-shown", "prompt-dismissed"]
    assert 90 <= _entry(mock, "prompt-shown")["t"] - loaded <= 1200


def test_the_prompt_can_follow_the_upload_and_accepting_it_fills_in_linkedin(server: Any) -> None:
    async def check(page: Page) -> dict[str, Any]:
        await page.wait_for_timeout(500)  # longer than the on-load delay: no prompt yet
        assert (await page.evaluate(MAIN))["prompt"] is False
        await page.set_input_files("#f-resume", RESUME_PATH)
        await page.wait_for_selector("#autofill-prompt", timeout=3000)
        assert (await page.evaluate(MAIN))["inert"] is True
        await page.click("#autofill-prompt-accept")
        assert await page.evaluate(MAIN) == {
            "prompt": False, "inBody": False, "inMain": False, "inert": False, "hidden": None}
        assert await page.evaluate(NAME_EMAIL) == [value for _, value in LINKEDIN_MEMBER]
        return await _mock(page)

    mock = _in_chromium(server.url("/jobs/linkedin-autofill/apply?loading_ms=300&prompt=upload"), check)
    assert [e for e in _events(mock) if e != "linkedin-ready"] == [
        "input:resume", "upload", "prompt-shown", "prompt-accepted"]
    assert 290 <= _entry(mock, "prompt-shown")["t"] - _entry(mock, "upload")["t"] <= 1200
    assert (mock["uploads"], mock["promptShown"], mock["promptAccepted"], mock["promptDismissed"]) == (
        1, 1, 1, 0)


REACT_VALUES = "() => ['f-first_name', 'f-last_name', 'f-email', 'f-phone'].map(id => document.getElementById(id).value)"
ROOT_HTML = "() => document.getElementById('react-root').innerHTML"


def test_react_controlled_reverts_script_values_and_rerenders_once(server: Any) -> None:
    async def check(page: Page) -> dict[str, Any]:
        before = await page.evaluate(ROOT_HTML)
        last_name = await page.query_selector("#f-last_name")
        assert last_name is not None
        # A value assigned by script, even with an input event, is put back.
        assigned = await page.evaluate("""() => { const input = document.getElementById('f-first_name');
          input.value = 'Scripted'; input.dispatchEvent(new Event('input', {bubbles: true}));
          return performance.now(); }""")
        await page.wait_for_function("() => document.getElementById('f-first_name').value === ''", timeout=2000)
        mock = await _mock(page)
        assert _events(mock) == ["revert:first_name"] and mock["renders"] == 0
        assert _entry(mock, "revert:first_name")["t"] - assigned <= 300

        # Typed values are kept; the first one re-renders the fields once, as new elements.
        await page.fill("#f-first_name", "Avery")
        saving = await page.wait_for_selector("#react-saving", timeout=2000)
        assert saving is not None
        assert [await saving.text_content(), await saving.get_attribute("aria-busy")] == [
            "Saving draft\u2026", "true"]
        await page.wait_for_function("() => window.__mock.renders === 1", timeout=3000)
        assert await last_name.evaluate("e => e.isConnected") is False
        assert await page.evaluate(ROOT_HTML) == before  # same ids, names, labels and attributes
        await page.fill("#f-last_name", "Quill")
        await page.locator("#f-email").press_sequentially("avery.quill@example.test")
        await page.wait_for_timeout(350)  # two reconcile rounds
        assert await page.evaluate(REACT_VALUES) == ["Avery", "Quill", "avery.quill@example.test", ""]
        # The form posts React's state, not a value written into the DOM behind its back.
        posted = await page.evaluate("""() => { document.getElementById('f-last_name').value = 'Quill (resume)';
          return Object.fromEntries([...new FormData(document.forms[0])].filter(([k]) => k !== 'resume')); }""")
        assert posted == {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
                          "phone": ""}
        return await _mock(page)

    mock = _in_chromium(server.url("/jobs/react-controlled/apply?unmount_ms=150"), check)
    assert mock["renders"] == 1
    assert mock["state"] == {"first_name": "Avery", "last_name": "Quill",
                             "email": "avery.quill@example.test", "phone": ""}
    events = _events(mock)
    assert events.count("rerender") == 1
    assert [e for e in events if e.startswith("revert:")] == ["revert:first_name"]
    assert events[:3] == ["revert:first_name", "input:first_name", "rerender"]


def test_a_rejected_react_form_keeps_the_values_it_posted(server: Any) -> None:
    async def check(page: Page) -> dict[str, Any]:
        for name, value in [*AVERY, PHONE]:
            await page.fill(f"#f-{name}", value)  # waits for the re-rendered field when needed
        await page.wait_for_function("() => window.__mock.renders === 1", timeout=3000)
        await page.evaluate("() => { document.forms[0].noValidate = true; }")  # post without a resume
        async with page.expect_navigation():
            await page.click("button[type=submit]")
        assert "Resume: Attach a file." in (await page.text_content("[role=alert]") or "")
        await page.wait_for_timeout(350)  # the new page's state starts from the rendered values
        assert await page.evaluate(REACT_VALUES) == [value for _, value in [*AVERY, PHONE]]
        return await _mock(page)

    mock = _in_chromium(server.url("/jobs/react-controlled/apply?unmount_ms=50"), check)
    assert mock["state"] == dict([*AVERY, PHONE]) and mock["renders"] == 0
    assert mock["log"] == []
    assert server.submissions("react-controlled")["rejections"][0]["errors"] == {"resume": "Attach a file."}


def test_react_controlled_lose_first_drops_the_first_typed_value(server: Any) -> None:
    async def check(page: Page) -> dict[str, Any]:
        await page.fill("#f-first_name", "Avery")
        await page.wait_for_function("() => window.__mock.renders === 1", timeout=3000)
        assert await page.input_value("#f-first_name") == ""
        await page.fill("#f-first_name", "Avery")  # typed again after the re-render: kept
        await page.wait_for_timeout(350)
        assert await page.input_value("#f-first_name") == "Avery"
        return await _mock(page)

    mock = _in_chromium(server.url("/jobs/react-controlled/apply?lose_first=1&unmount_ms=100"), check)
    assert (mock["renders"], mock["state"]["first_name"]) == (1, "Avery")
    assert [e for e in _events(mock) if e.startswith("input:")] == ["input:first_name", "input:first_name"]
