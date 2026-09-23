"""O1R regressions use only an owned headless page and fictional localhost fixtures."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import DriverError, PlaywrightSessionFactory
from interviewmaxxing_browser.driver import _FILE_DIGEST
from interviewmaxxing_core import BrowserOptions


@pytest.mark.parametrize("trigger", ["answer", "unanswered_consent"])
def test_playwright_document_loss_stops_all_later_writes(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, trigger: str,
) -> None:
    url = server.url("/o1r/form")
    prefix = ('<input id="consent" name="consent" type="checkbox" checked '
              'onchange="location.href=\'/o1r/replaced\'"><label for="consent">I consent to data sharing</label>')
    event = "oninput=\"location.href='/o1r/replaced'\"" if trigger == "answer" else ""
    html = f'''<!doctype html><title>Apply: Fictional Engineer</title>
    <h1>Fictional Engineer</h1><p>Job ID FIXTURE-101</p><form method="post">
    {prefix if trigger == 'unanswered_consent' else ''}
    <label for="name">Full name</label><input id="name" name="name" {event}>
    <label for="email">Email</label><input id="email" name="email" type="email">
    <button type="submit">Submit application</button></form>'''

    async def scenario() -> None:
        browser = await PlaywrightSessionFactory().start(options)
        writes: list[str] = []
        try:
            await browser.page.route(server.origin + "/o1r/**",
                                     lambda route: route.fulfill(content_type="text/html", body=html))
            original_fill = browser.driver.fill
            original_check = browser.driver.set_checked

            async def fill(selector: str, value: str) -> None:
                writes.append(selector)
                await original_fill(selector, value)

            async def check(selector: str, value: bool, *, label_selector: str | None = None) -> None:
                writes.append(selector)
                await original_check(selector, value, label_selector=label_selector)

            browser.driver.fill = fill
            browser.driver.set_checked = check
            form = (await browser.open(url)).form
            assert form is not None
            result = await browser.fill(form, kit.build(form, {"name": "Avery Quill", "email": "avery@example.test"}).packet)
            assert not result.ok and len(writes) == 1
            assert "inspect" in " ".join(result.page_errors)
            await browser.page.wait_for_url("**/o1r/replaced")
            assert not (await browser.submit()).dispatched
            await browser.inspect()
            assert not (await browser.submit()).dispatched
        finally:
            await browser.close()
    kit.run(scenario())
    assert server.submissions()["accepted_count"] == 0


def test_playwright_checks_actual_attached_bytes_not_file_metadata(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, tmp_path: Path,
) -> None:
    selected = tmp_path / "resume.pdf"
    selected.write_bytes(b"fictional approved")
    wrong = b"fictional rejected"
    assert len(wrong) == selected.stat().st_size

    async def scenario() -> None:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.open(server.url("/jobs/validation/apply"))
            await browser.driver.set_files("#f-resume", selected)
            digest = await browser.page.evaluate(_FILE_DIGEST, "#f-resume")
            assert digest["sha256"] == hashlib.sha256(selected.read_bytes()).hexdigest()
            await browser.page.locator("#f-resume").set_input_files([])
            # Simulate a file-input change handler replacing the just-selected File.
            # This is a headless test fixture only; the OpenCLI path never sets Files in JS.
            await browser.page.evaluate('''() => document.querySelector('#f-resume').addEventListener('change', (event) => {
              const transfer = new DataTransfer();
              transfer.items.add(new File(['fictional rejected'], 'resume.pdf'));
              event.target.files = transfer.files;
            })''')
            with pytest.raises(DriverError, match="attached bytes"):
                await browser.driver.set_files("#f-resume", selected)
            digest = await browser.page.evaluate(_FILE_DIGEST, "#f-resume")
            assert digest["sha256"] == hashlib.sha256(wrong).hexdigest()
        finally:
            await browser.close()
    kit.run(scenario())


def test_final_submit_rechecks_filled_document_even_when_form_is_identical(
    kit: SimpleNamespace, server: Any, options: BrowserOptions,
) -> None:
    async def scenario() -> None:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            form = (await browser.open(server.url("/jobs/validation/apply"))).form
            assert form is not None
            assert (await browser.fill(form, kit.build(form, kit.CORE).packet)).ok
            await browser.page.reload()
            # Restore fictional required controls manually after replacement, preserving
            # the form fingerprint; the old document authorization still cannot submit.
            await browser.page.locator("#f-first_name").fill("Avery")
            await browser.page.locator("#f-last_name").fill("Quill")
            await browser.page.locator("#f-email").fill("avery@example.test")
            await browser.page.locator("#f-resume").set_input_files(str(kit.RESUME_PATH))
            action = await browser.submit()
            assert not action.dispatched
        finally:
            await browser.close()
    kit.run(scenario())
    assert server.submissions()["accepted_count"] == 0
