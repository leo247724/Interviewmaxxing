"""Upload-first preparation end to end: a prepare-only run of the real runner over the
real store on the Ashby-like ``autofill-upload`` mock, whose resume parser overwrites the
name and email fields shortly after a resume is attached.

The fictional candidate (Avery Quill) has a verified identity and a resume, so the run
attaches the resume once, lets the parser finish, types the candidate's own values over
the parsed ones, stops at the final review step and submits nothing.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_cli.runner import LocalApplicationRunner, NoninteractiveInteraction
from interviewmaxxing_core import ApplicationState, ApplicationStore, BrowserOptions, LocalPaths

REPO = Path(__file__).resolve().parents[2]
RESUME_PATH = REPO / "tests" / "fixtures" / "browser" / "resume_avery_quill.pdf"
VERIFIED_AT = "2026-09-01T12:00:00Z"
AUTOFILL = "/jobs/autofill-upload/apply"
PAGE_STATE = """() => ({
  values: Object.fromEntries(['first_name', 'last_name', 'email'].map((name) => {
    const control = document.querySelector(`form [name="${name}"]`);
    return [name, control ? control.value : null];
  })),
  files: Array.from((document.querySelector('#f-resume') || {files: []}).files)
    .map((f) => ({name: f.name, size: f.size})),
  mock: JSON.parse(JSON.stringify({...window.__mock, files: undefined})),
})"""


def _write_profile(paths: LocalPaths) -> None:
    """The fictional candidate: a verified identity and a copy of the fixture resume.
    This form asks nothing else, so no saved answers are needed."""
    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RESUME_PATH, directory / "resume.pdf")
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
        "saved_answers": [],
    }
    (directory / "profile.json").write_text(json.dumps(profile, indent=2))


class RecordingFactory:
    """The Playwright factory, recording the page's state as the run closes it."""

    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = []

    async def start(self, options: BrowserOptions) -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        close = browser.close

        async def close_and_record() -> None:
            with contextlib.suppress(Exception):
                self.states.append(await browser.page.evaluate(PAGE_STATE))
            await close()

        browser.close = close_and_record
        return browser


def test_prepare_only_run_attaches_the_resume_first_and_submits_nothing(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home)
    factory = RecordingFactory()
    runner = LocalApplicationRunner(paths=isolated_imx_home, interaction=NoninteractiveInteraction(),
                                    headless=True, browser_factory=factory, prepare_only=True)
    result = kit.run(runner.apply(server.url(AUTOFILL), candidate_id="default"))
    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    assert "Prepared to the final review step" in result.message, result.message
    assert result.missing_inputs == []
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        [ready] = [e for e in store.list_events(result.application_id) if e.event == "preparation.ready"]
        assert ready.metadata["submitted"] is False
        assert store.list_attempts(result.application_id) == []
    assert factory.states, "the run's page state was not recorded"
    state = factory.states[-1]
    # The candidate's own values, not the resume parser's, are in the page at the end.
    assert state["values"] == {"first_name": "Avery", "last_name": "Quill",
                               "email": "avery.quill@example.test"}
    # The input holds the profile's copy of the resume, attached exactly once, before
    # anything was typed and before the parser's values landed.
    assert state["files"] == [{"name": "resume.pdf", "size": 802}]
    assert state["mock"]["uploads"] == 1
    events = [entry["event"] for entry in state["mock"]["log"]]
    typed = [i for i, event in enumerate(events)
             if event in ("input:first_name", "input:last_name", "input:email")]
    assert typed and {"upload", "autofill"} <= set(events), events
    assert events.index("upload") < events.index("autofill") < min(typed), events
    assert server.submissions("autofill-upload")["accepted_count"] == 0
