"""Round 10 (review pass 4, M10): values typed or read back are quoted in fill details, so
the runner's redaction (``redact_detail``) removes them before a failure's metadata is
stored. Fictional data, headless Chromium, nothing submitted.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import async_playwright

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.aria import fill_phone
from interviewmaxxing_browser.driver import DriverError, PlaywrightDriver
from interviewmaxxing_cli.runner import redact_detail
from interviewmaxxing_core import BrowserOptions, FieldFillStatus

BROWSER = Path(__file__).resolve().parents[2] / "packages" / "browser" / "src" / "interviewmaxxing_browser"

# An intl-tel-input phone whose page keeps only the last four digits typed: the readback
# fails, and its detail names what the input and the picker show.
PHONE = """<!doctype html><title>Fictional form</title><form><label for="phone">Phone</label>
<div class="iti iti--allow-dropdown iti--separate-dial-code"><div class="iti__flag-container">
<div class="iti__selected-flag" role="combobox" aria-haspopup="listbox" aria-expanded="false" title="United States: +1">
<div class="iti__selected-dial-code">+1</div></div></div>
<input type="tel" id="phone" name="phone"></div></form>
<script>
const input = document.getElementById("phone");
input.addEventListener("input", () => { if (window.__keepLast) input.value = input.value.replace(/\\D/g, "").slice(-4); });
</script>"""


@pytest.mark.parametrize("keep_last", [True, False], ids=["mismatch", "match"])
def test_a_phone_readback_is_quoted_so_it_is_redacted(keep_last: bool) -> None:
    async def scenario() -> tuple[bool, str]:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(PHONE)
                await page.evaluate(f"() => {{ window.__keepLast = {str(keep_last).lower()}; }}")
                return await fill_phone(PlaywrightDriver(page), "#phone", "+1 512 555 0142")
            finally:
                await browser.close()

    ok, detail = asyncio.run(scenario())
    assert ok is not keep_last
    shown = "0142" if keep_last else "+1 512 555 0142"
    assert detail.startswith(("'", '"'))  # the number read back is quoted
    assert shown in detail
    # The runtime reports it as "reads back <detail!r>"; either form loses the number and
    # the picker's text once redacted.
    for reported in (f"reads back {detail!r}", detail):
        redacted = redact_detail(reported) or ""
        assert shown not in redacted and "0142" not in redacted and "+1" not in redacted, redacted


# An uploader that takes the file and keeps no readable trace of it: the driver cannot
# verify the attach, and its error names the pinned file.
LOST_UPLOAD = """<!doctype html><title>Fictional form</title><form>
<label for="resume">Resume</label><input type="file" id="resume" name="resume"></form>
<script>
const input = document.getElementById("resume");
input.addEventListener("change", () => { input.value = ""; });
</script>"""


def test_an_unverified_attach_names_the_file_quoted(kit: SimpleNamespace) -> None:
    async def scenario() -> str:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(LOST_UPLOAD)
                await page.evaluate("() => { Object.defineProperty(window.crypto, 'subtle', {value: undefined}); }")
                try:
                    await PlaywrightDriver(page).set_files("#resume", kit.RESUME_PATH)
                except DriverError as exc:
                    return str(exc)
                return ""
            finally:
                await browser.close()

    message = asyncio.run(scenario())
    name = kit.RESUME_PATH.name
    assert repr(name) in message, message
    assert name not in (redact_detail(message) or "")


@pytest.mark.parametrize("keep", [True, False], ids=["input-keeps-file", "input-emptied"])
def test_the_sites_own_upload_error_is_quoted(
    keep: bool, kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """Workable's dropzone rejects the file with a message naming it. When its input keeps
    the file, the detail quotes the site's text; when it empties the input, the attach is
    not verified and the detail quotes the pinned file's name. Redaction leaves neither."""
    hooks = "uploadError: {resume: '{file} is too large (the limit is 5 MB)'}" + (", keepFile: {resume: true}" if keep else "")

    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.page.add_init_script(f"window.__widgetHooks = {{selectNext: {{}}, {hooks}}};")
            page = await browser.open(server.url("/jobs/workable-like/apply"))
            return await browser.fill(page.form, kit.build(page.form, {
                "first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
                "input_files_input_resume": kit.RESUME}).packet)
        finally:
            await browser.close()

    fill = kit.run(scenario())
    resume = next(f for f in fill.fields if f.field_id == "input_files_input_resume")
    name = kit.RESUME_PATH.name
    if keep:
        assert resume.status is FieldFillStatus.VERIFICATION_MISMATCH, resume
        assert resume.detail == f"the site reports: {name + ' is too large (the limit is 5 MB)'!r}"
        assert redact_detail(resume.detail) == "the site reports: '…'"
    else:
        assert resume.status is FieldFillStatus.FAILED, resume
        assert repr(name) in (resume.detail or "")
        assert name not in (redact_detail(resume.detail) or "")


# Names that hold a value typed into the page or read back from it (or the candidate's
# file name): wherever an f-string in these modules interpolates one, it is quoted (!r).
VALUE_NAMES = {"value", "text", "typed", "display", "chosen", "picker_text", "shown", "observed",
               "got", "name", "path.name", "artifact.filename", "state.error", "state.files"}
MODULES = ["aria.py", "runtime.py", "uploads.py", "driver.py", "opencli.py"]


@pytest.mark.parametrize("module", MODULES)
def test_values_in_detail_strings_are_always_quoted(module: str) -> None:
    source = (BROWSER / module).read_text()
    unquoted = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.FormattedValue) and part.conversion != ord("r"):
                    expression = ast.get_source_segment(source, part.value)
                    if expression in VALUE_NAMES:
                        unquoted.append(f"{module}:{node.lineno} {{{expression}}}")
    assert unquoted == []
