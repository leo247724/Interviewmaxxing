"""Custom widgets end to end: a prepare-only run of the real runner over the real store
and the React-select mock, and what the mock's server accepts from its widgets.

The fictional candidate (Avery Quill) has saved answers for every question, so the run
fills every React select, stops at the final review step and submits nothing. The
server checks below post to the localhost mock directly (test fixture traffic only).
"""

from __future__ import annotations

import contextlib
import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_cli.runner import LocalApplicationRunner, NoninteractiveInteraction
from interviewmaxxing_core import (
    ApplicationForm,
    ApplicationState,
    ApplicationStore,
    BrowserOptions,
    ChoiceValue,
    LocalPaths,
    SemanticType,
)

REPO = Path(__file__).resolve().parents[2]
RESUME_PATH = REPO / "tests" / "fixtures" / "browser" / "resume_avery_quill.pdf"
VERIFIED_AT = "2026-09-01T12:00:00Z"
REACT = "/jobs/react-select/apply"
INLINE = "/jobs/react-select-inline/apply"
REACT_MENUS = {"question_6004": "United States +1", "question_6001": "Yes", "question_6002": "No",
               "question_6003": "LinkedIn"}
INLINE_MENUS = {"question_9004": "United States +1", "question_9001": "Yes", "question_9002": "No",
                "question_9003": "LinkedIn"}
MENU_STATE = "() => JSON.parse(JSON.stringify(window.__widgetState || {}))"


async def _inspect_form(options: BrowserOptions, url: str) -> ApplicationForm:
    browser = await PlaywrightSessionFactory().start(options)
    try:
        page = await browser.open(url)
        assert page.form is not None, page.message
        return page.form
    finally:
        await browser.close()


def _write_profile(paths: LocalPaths, form: ApplicationForm, job_url: str,
                   menus: dict[str, str] = REACT_MENUS) -> None:
    """The fictional candidate with saved answers worded as this form asks them."""
    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RESUME_PATH, directory / "resume.pdf")

    def saved(answer_id: str, field_id: str, value: Any, **scope: Any) -> dict[str, Any]:
        field = form.field(field_id)
        semantic = None if field.semantic_type is SemanticType.UNKNOWN else field.semantic_type.value
        return {"id": answer_id, "scope": scope.pop("scope", "GLOBAL"), "semantic_type": semantic,
                "question": field.question_text, "value": value, "confirmed_at": VERIFIED_AT, **scope}

    profile = {
        "id": "default",
        "identity": {
            "first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
            "phone": "+1 (303) 555-0142",
            "linkedin_url": "https://www.linkedin.example.test/in/avery-quill",
            "address": {"city": "Denver", "region": "CO", "country": "United States"},
            "verified_at": VERIFIED_AT,
        },
        "resume": {"id": "resume_supplied", "path": "resume.pdf"},
        "facts": [],
        "saved_answers": [
            *(saved(f"sa.{field_id}", field_id, value) for field_id, value in menus.items()),
            saved("sa.years", "years_experience", "6 to 9 years"),
            *([saved("sa.skills", "skills", ["Python", "SQL", "Apache Spark", "dbt"])]
              if any(f.id == "skills" for f in form.fields) else []),
            saved("sa.why", "why_brambleway", "I have built data platforms for seven years and "
                  "want to work on logistics forecasting.", scope="JOB", job_url=job_url,
                  employer="Brambleway Analytics"),
        ],
    }
    (directory / "profile.json").write_text(json.dumps(profile, indent=2))


class RecordingFactory:
    """The Playwright factory, recording the widgets' page state as the run closes."""

    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = []

    async def start(self, options: BrowserOptions) -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        close = browser.close

        async def close_and_record() -> None:
            with contextlib.suppress(Exception):
                self.states.append(await browser.page.evaluate(MENU_STATE))
            await close()

        browser.close = close_and_record
        return browser


def test_prepare_only_run_fills_every_react_select_and_submits_nothing(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, isolated_imx_home: LocalPaths
) -> None:
    url = server.url(REACT)
    _write_profile(isolated_imx_home, kit.run(_inspect_form(options, url)), url)
    factory = RecordingFactory()
    runner = LocalApplicationRunner(paths=isolated_imx_home, interaction=NoninteractiveInteraction(),
                                    headless=True, browser_factory=factory, prepare_only=True)
    result = kit.run(runner.apply(url, candidate_id="default"))
    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    assert "Prepared to the final review step" in result.message, result.message
    assert result.missing_inputs == []
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        [ready] = [e for e in store.list_events(result.application_id) if e.event == "preparation.ready"]
        assert ready.metadata["submitted"] is False
        packet = store.latest_packet(result.application_id)
        assert packet is not None
        answers = {a.field_id: a.value for a in packet.answers}
        assert store.list_attempts(result.application_id) == []
    assert answers["question_6004"] == ChoiceValue(value="United States +1", label="United States +1")
    assert answers["question_6001"] == ChoiceValue(value="Yes", label="Yes")
    assert answers["question_6002"] == ChoiceValue(value="No", label="No")
    assert answers["question_6003"] == ChoiceValue(value="LinkedIn", label="LinkedIn")
    # Every combobox holds its answer in the page (React-like state, no hidden inputs).
    assert factory.states[-1] == {
        "question_6004": {"value": "us"}, "question_6001": {"value": "rs_wa_yes"},
        "question_6002": {"value": "rs_sp_no"}, "question_6003": {"value": "src_linkedin"},
    }
    assert server.submissions("react-select")["accepted_count"] == 0


@pytest.mark.parametrize("job", ["react-select-inline", "react-select-inline-async"])
def test_prepare_only_run_fills_menus_rendered_inside_the_form(
    job: str, kit: SimpleNamespace, server: Any, options: BrowserOptions, isolated_imx_home: LocalPaths
) -> None:
    """Greenhouse renders its menus inside the form and re-renders its uploader after the
    upload: neither may look like a changed page, so the run fills through every question
    and reaches the review."""
    url = server.url(f"/jobs/{job}/apply")
    _write_profile(isolated_imx_home, kit.run(_inspect_form(options, url)), url, INLINE_MENUS)
    factory = RecordingFactory()
    runner = LocalApplicationRunner(paths=isolated_imx_home, interaction=NoninteractiveInteraction(),
                                    headless=True, browser_factory=factory, prepare_only=True)
    result = kit.run(runner.apply(url, candidate_id="default"))
    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    assert "Prepared to the final review step" in result.message, result.message
    assert result.missing_inputs == []
    menus = {k: v for k, v in factory.states[-1].items() if k != "resume"}
    assert menus == {
        "question_9004": {"value": "us"}, "question_9001": {"value": "in_wa_yes"},
        "question_9002": {"value": "in_sp_no"}, "question_9003": {"value": "src_linkedin"},
    }
    assert factory.states[-1]["resume"]["value"] is not None  # the uploader holds the résumé
    assert server.submissions(job)["accepted_count"] == 0


def test_widget_state_reaches_the_form_data_without_hidden_inputs(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url(REACT))
            fill = await browser.fill(page.form, kit.build(page.form, {
                "question_6004": "India +91", "question_6003": "Other"}).packet)
            assert fill.ok, fill
            # Constructing FormData fires the form's "formdata" event (no request is sent).
            entries = await browser.page.evaluate(
                "() => ({hidden: document.querySelectorAll('input[type=hidden]').length, entries: "
                "[...new FormData(document.forms[0]).entries()].filter(([k]) => k.startsWith('question_'))"
                ".map(([k, v]) => [k, String(v)])})")
            return entries, server.submissions("react-select")["accepted_count"]
        finally:
            await browser.close()

    entries, accepted = kit.run(scenario())
    assert entries == {"hidden": 0, "entries": [["question_6004", "in"], ["question_6003", "src_other"]]}
    assert accepted == 0


# --- the mock server's own checks -------------------------------------------------------


def _post(server: Any, path: str, fields: list[tuple[str, str]], *, resume: bool = False) -> int:
    boundary = "----WidgetFixture" + uuid.uuid4().hex
    body = bytearray()
    for name, value in fields:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                 f"{value}\r\n").encode()
    if resume:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"resume\"; "
                 f"filename=\"{RESUME_PATH.name}\"\r\nContent-Type: application/pdf\r\n\r\n").encode()
        body += RESUME_PATH.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    request = urllib.request.Request(server.url(path), data=bytes(body), method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code


CONTACT = [("first_name", "Avery"), ("last_name", "Quill"), ("email", "avery.quill@example.test")]
REACT_VALID = [
    *CONTACT, ("question_6004", "us"), ("phone", "3035550142"), ("question_6001", "rs_wa_yes"),
    ("years_experience", "yrs_6_9"), ("question_6002", "rs_sp_no"), ("skills", "sk_python"),
    ("question_6003", "src_linkedin"), ("why_brambleway", "Fictional answer."),
]


@pytest.mark.parametrize(("job", "fields", "resume", "accepted"), [
    ("react-select", REACT_VALID, True, True),
    ("react-select", [f for f in REACT_VALID if f[0] != "question_6001"], True, False),
    ("react-select", [*REACT_VALID[:5], ("question_6001", "Yes"), *REACT_VALID[6:]], True, False),
    ("div-combobox", [*CONTACT, ("phone", "+13035550142"), ("phone_country_code", "+1 US"),
                      ("field-3", "Yes"), ("field-4", "No")], False, True),
    ("div-combobox", [*CONTACT, ("phone", "+13035550142"), ("field-3", "Maybe"),
                      ("field-4", "No")], False, False),
    ("typeahead", [*CONTACT, ("candidate_location", "Austin, Texas, United States"),
                   ("state", "Texas")], False, True),
    # Typed but never chosen from the suggestions: rejected.
    ("typeahead", [*CONTACT, ("candidate_location", "Austin"), ("state", "Texas")], False, False),
    ("phone-widget", [*CONTACT, ("phone", "+1 561-555-0100"), ("phone_country", "us")], False, True),
    ("phone-widget", [*CONTACT, ("phone", "+44 20 7946 0958"), ("phone_country", "gb")], False, False),
    ("phone-widget", [*CONTACT, ("phone", "+1 561-555-0100"), ("phone_country", "gb")], False, False),
    ("phone-widget", [*CONTACT, ("phone", "561-555-010"), ("phone_country", "us")], False, False),
    ("multiselect-react", [*CONTACT, ("question_8001", "ch_email"), ("question_8001", "ch_seo"),
                           ("question_8002", "src_site")], False, True),
    ("react-select-inline", [*CONTACT, ("question_9004", "us"), ("phone", "3035550142"),
                             ("question_9001", "in_wa_yes"), ("years_experience", "yrs_6_9"),
                             ("question_9002", "in_sp_no"), ("why_brambleway", "Fictional answer.")],
     True, True),
    # The empty required proxy posts "" for the country: rejected like no choice at all.
    ("react-select-inline", [*CONTACT, ("question_9004", ""), ("phone", "3035550142"),
                             ("question_9001", "in_wa_yes"), ("years_experience", "yrs_6_9"),
                             ("question_9002", "in_sp_no"), ("why_brambleway", "Fictional answer.")],
     True, False),
    ("workable-like", [*CONTACT, ("phone", "5615550100"), ("phone_country", "us")], True, True),
    ("phone-dialcode-collision", [*CONTACT, ("country", "us"), ("phone", "3035550142"),
                                  ("phone_country", "us"), ("question_9001", "in_wa_yes")], False, True),
    ("phone-dialcode-collision", [*CONTACT, ("country", "United States"), ("phone", "3035550142"),
                                  ("phone_country", "us"), ("question_9001", "in_wa_yes")], False, False),
    ("workable-like", [*CONTACT, ("phone", "5615550100"), ("phone_country", "us")], False, False),
    ("div-combobox-orphan", [*CONTACT, ("gender", "Female"), ("custom_work_authorization", "Yes"),
                             ("location", "Austin, TX, USA")], False, True),
    # Typed but never chosen from the suggestions: rejected.
    ("div-combobox-orphan", [*CONTACT, ("gender", "Female"), ("custom_work_authorization", "Yes"),
                             ("location", "Austin, TX")], False, False),
])
def test_the_mock_accepts_only_valid_widget_values(
    job: str, fields: list[tuple[str, str]], resume: bool, accepted: bool, server: Any
) -> None:
    status = _post(server, f"/jobs/{job}/apply", fields, resume=resume)
    summary = server.submissions(job)
    assert (summary["accepted_count"], summary["rejected_count"]) == ((1, 0) if accepted else (0, 1)), status
    assert status == (200 if accepted else 422)


def test_the_city_suggestions_endpoint(server: Any) -> None:
    def cities(query: str, style: str = "long") -> list[str]:
        with urllib.request.urlopen(server.url(f"/__fixture__/cities?style={style}&q="
                                               + urllib.parse.quote(query)), timeout=10) as response:
            result: list[str] = json.loads(response.read())
            return result

    assert cities("Austin") == [
        "Austin, Texas, United States", "Austin, Minnesota, United States",
        "Austintown, Ohio, United States", "Austin, Indiana, United States",
        "Austin, Arkansas, United States"]
    assert cities("Austin, Tex") == ["Austin, Texas, United States"]
    assert cities("Austin, TX") == ["Austin, Texas, United States"]
    assert cities("Austin", "short") == ["Austin, TX, USA", "Austin, MN, USA", "Austintown, OH, USA"]
    assert cities("Austin, Texas", "short") == ["Austin, TX, USA"]
    assert cities("A") == [] and cities("Zzyzx") == []
