"""The four replicated application flows of the localhost mock ATS (fictional employer and
candidate): a LinkedIn-style Easy Apply dialog, an employer careers page embedding a
Greenhouse-style iframe, a JazzHR-style form whose only actions are anchors, and a
Dayforce-style posting wrapped in a job-alert form with client-side routes.

These tests check the MOCK itself (markup, delays, what the server records) with plain
headless Chromium and raw HTTP; the runtime is tested elsewhere. Nothing leaves localhost.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Browser, Page, async_playwright

FLOWS = {
    "modal-wizard": ("BWA-LI-130", "Growth Marketing Lead"),
    "iframe-embed": ("BWA-GH-141", "Partnerships Manager"),
    "stepper-ambiguous": ("BWA-JZ-132", "Head of Paid Media"),
    "apply-in-alert-form": ("BWA-DF-133", "Marketing Project Manager"),
}
DIALOG = "[role=dialog]"
NEXT = "button[aria-label='Continue to next step']"
BACK = "button[aria-label='Back to previous step']"
REVIEW = "button[aria-label='Review your application']"
SUBMIT = "button[aria-label='Submit application']"
SQL = "How many years of work experience do you have with SQL?"
SPONSORSHIP = "Will you now or in the future require sponsorship for employment visa status?"
EASY = "() => JSON.parse(JSON.stringify(window.__easyApply))"
CARDS = """cards => cards.map((c) => ({
  name: c.querySelector('h3').textContent,
  selected: c.classList.contains('jobs-document-upload-redesign-card__container--selected'),
  label: c.getAttribute('aria-label'),
  checked: c.querySelector('input[type=radio]').checked,
  radio: [c.querySelector('input[type=radio]').id, c.querySelector('input[type=radio]').hasAttribute('name')],
  toggle: c.querySelector('label .a11y-text').textContent,
}))"""
EASY_ANSWERS = [
    ("email", "avery.quill@example.test"), ("phone_country", "United States (+1)"), ("phone", "3035550142"),
    ("city", "Boulder"), ("sql_years", "5"), ("work_authorization", "Yes"), ("sponsorship", "No"),
]
CLOCK = """window.__clock = {pushes: []};
document.addEventListener('click', () => { window.__clock.click = performance.now(); }, true);
new MutationObserver(() => {
  if (!window.__clock.dialog && document.getElementById('artdeco-modal-outlet')) window.__clock.dialog = performance.now();
}).observe(document, {childList: true, subtree: true});
const push = history.pushState.bind(history);
history.pushState = (...args) => { window.__clock.pushes.push(performance.now()); return push(...args); };"""
"""Init script: page-clock stamps of the last click, the dialog's insertion and each pushState,
so the stated delays are asserted exactly instead of by sleeping."""
TIMER_SLACK_MS = 10  # a timer may fire marginally early against performance.now()


# --- helpers ------------------------------------------------------------------------------


@asynccontextmanager
async def chromium() -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            await browser.close()


def request(server: Any, method: str, path: str, body: bytes | None = None,
            headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], str]:
    """One raw HTTP exchange (redirects are not followed)."""
    origin = urlsplit(server.origin)
    conn = http.client.HTTPConnection(origin.hostname or "127.0.0.1", origin.port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode("utf-8")
    finally:
        conn.close()


def multipart(fields: Sequence[tuple[str, str]],
              files: Sequence[tuple[str, str, bytes, str]] = ()) -> tuple[bytes, dict[str, str]]:
    boundary = "----WizardMockBoundary" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields:
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    for name, filename, data, content_type in files:
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def form_markup(document: str, form_id: str) -> str:
    start = document.index(f'<form id="{form_id}"')
    return document[start:document.index("</form>", start) + len("</form>")]


async def progress(page: Page) -> list[Any]:
    return [
        await page.locator("progress").evaluate("p => [p.value, p.getAttribute('aria-valuenow')]"),
        await page.locator("[role=note]").inner_text(),
        await page.locator("[role=region]").get_attribute("aria-label"),
    ]


async def errors(page: Page) -> list[str]:
    return await page.locator(".artdeco-inline-feedback--error").all_inner_texts()


async def answer_questions(page: Page, sql_years: str = "5") -> None:
    await page.get_by_label(SQL).fill(sql_years)
    await page.get_by_label("Yes", exact=True).check()
    await page.get_by_label(SPONSORSHIP).select_option("No")


# --- catalog ------------------------------------------------------------------------------


def test_catalog_index_and_summary_list_the_four_flows(server: Any) -> None:
    catalog = {job["job_id"]: job for job in server.api("GET", "/__test__/jobs")["jobs"]}
    assert {job: (catalog[job]["job_code"], catalog[job]["title"]) for job in FLOWS} == FLOWS
    wizard = catalog["modal-wizard"]
    assert wizard["multistep"]
    assert [step["title"] for step in wizard["steps"]] == [
        "Contact info", "Resume", "Additional Questions", "Review your application"]
    assert [[f["name"] for f in step["fields"]] for step in wizard["steps"]] == [
        ["email", "phone_country", "phone", "city"], ["resume"],
        ["sql_years", "work_authorization", "sponsorship"], ["follow_company"]]

    def names(job: str) -> list[str]:
        return [f["name"] for step in catalog[job]["steps"] for f in step["fields"]]

    assert names("iframe-embed") == names("standard")
    assert names("stepper-ambiguous") == ["first_name", "last_name", "email", "phone", "desired_salary",
                                          "heard_about", "resume", "resume_text"]
    assert names("apply-in-alert-form") == ["first_name", "last_name", "email", "phone", "linkedin_url",
                                            "resume", "work_authorization", "sponsorship"]
    _, _, index = request(server, "GET", "/")
    for job in FLOWS:
        assert f'href="/jobs/{job}"' in index
    summary = server.submissions()
    assert (summary["alert_count"], summary["alerts"], summary["accepted_count"]) == (0, [], 0)


# --- modal-wizard: the Easy Apply dialog ----------------------------------------------------


def test_easy_apply_link_opens_the_dialog_and_walks_four_steps(kit: SimpleNamespace, server: Any) -> None:
    async def scenario() -> dict[str, Any]:
        seen: dict[str, Any] = {}
        async with chromium() as browser:
            page = await browser.new_page()
            await page.add_init_script(CLOCK)
            await page.goto(server.url("/jobs/modal-wizard?resumes=match"))
            seen["closed_at_first"] = await page.locator(DIALOG).count()
            link = page.locator("a[aria-label='Easy Apply to this job']")
            seen["href"] = await link.get_attribute("href")
            await link.click()
            await page.wait_for_selector(DIALOG)
            seen["url"] = page.url
            seen["opened_at"] = await page.evaluate("window.__clock.dialog")  # since the apply URL loaded
            posts: list[str] = []  # every request the open dialog makes
            page.on("request", lambda r: None if r.url.endswith("/favicon.ico") else posts.append(f"{r.method} {r.url}"))
            seen["shape"] = await page.evaluate("""() => {
              const d = document.querySelector('[role=dialog]');
              return {
                outlet: document.getElementById('artdeco-modal-outlet').parentElement.tagName,
                overlay: d.parentElement.className, modal: d.hasAttribute('aria-modal'),
                labelledby: d.getAttribute('aria-labelledby'),
                header: document.getElementById('jobs-apply-header').textContent,
                form: d.querySelector('form').getAttributeNames(),
                dismiss: d.querySelector('[data-test-modal-close-btn]').getAttribute('aria-label'),
                footer: d.querySelector('footer p').textContent,
                buttons: Array.from(d.querySelectorAll('footer button')).map((b) =>
                  [b.getAttribute('type'), b.getAttribute('aria-label'), b.querySelector('.artdeco-button__text').textContent]),
              };
            }""")
            seen["prefilled"] = [await page.get_by_label(label, exact=True).input_value()
                                 for label in ("Email address", "Phone country code", "Mobile phone number", "City")]
            seen["email_options"] = await page.get_by_label("Email address").evaluate(
                "s => Array.from(s.options).map((o) => [o.value, o.textContent])")
            seen["progress"] = [await progress(page)]
            await page.click(NEXT)
            seen["progress"].append(await progress(page))
            seen["cards"] = await page.locator(".ui-attachment").evaluate_all(CARDS)
            await page.locator("label[for=jobsDocumentCardToggle-1]").check()  # the saved fixture resume
            seen["chosen"] = await page.locator(".ui-attachment").evaluate_all(CARDS)
            await page.click(NEXT)
            seen["progress"].append(await progress(page))
            await page.click(REVIEW)  # nothing answered yet
            seen["required"] = await errors(page)
            seen["invalid"] = await page.locator("[aria-invalid=true]").evaluate_all(
                "els => els.map((e) => e.classList.contains('fb-dash-form-element__error-field'))")
            await answer_questions(page, sql_years="seven")
            await page.get_by_label(SQL).press("Enter")  # the dialog form never navigates
            seen["after_enter"] = page.url
            await page.click(REVIEW)
            seen["numeric"] = await errors(page)
            await page.get_by_label(SQL).fill("7")
            await page.click(REVIEW)
            seen["progress"].append(await progress(page))
            seen["review"] = await page.locator(".jobs-easy-apply-review__section").all_inner_texts()
            seen["edits"] = await page.locator(".jobs-easy-apply-review__section button").evaluate_all(
                "bs => bs.map((b) => b.getAttribute('aria-label'))")
            seen["follow"] = await page.is_checked("#follow-company-checkbox")
            seen["state"] = await page.evaluate(EASY)
            seen["before_submit"] = (server.submissions("modal-wizard"), list(posts))
            await page.click(SUBMIT)
            await page.get_by_role("heading", name="Application submitted").wait_for()
            seen["confirmation"] = await page.locator(DIALOG).inner_text()
            seen["requests"] = list(posts)
            await page.get_by_role("button", name="Done").click()
            seen["done"] = (await page.locator(DIALOG).count(), await page.evaluate(EASY))
        return seen

    seen = kit.run(scenario())
    assert seen["closed_at_first"] == 0
    assert seen["href"] == "/jobs/modal-wizard/apply?openSDUIApplyFlow=true&resumes=match"
    assert seen["url"] == server.url(seen["href"])
    assert seen["opened_at"] >= 300  # opened by page script 300 ms after the apply URL loaded
    assert seen["shape"] == {
        "outlet": "BODY", "overlay": "artdeco-modal-overlay artdeco-modal-overlay--is-top-layer", "modal": False,
        "labelledby": "jobs-apply-header", "header": "Apply to Brambleway Analytics", "form": [],
        "dismiss": "Dismiss", "footer": "Submitting this application won\u2019t change your LinkedIn profile.",
        "buttons": [["button", "Continue to next step", "Next"]],
    }
    assert seen["prefilled"] == ["avery.quill@example.test", "United States (+1)", "3035550142", "Boulder"]
    assert seen["email_options"] == [["Select an option", "Select an option"],
                                     ["avery.quill@example.test", "avery.quill@example.test"],
                                     ["a.quill@example.test", "a.quill@example.test"]]
    assert [p[0][0] for p in seen["progress"]] == [0, 33, 67, 100]
    assert seen["progress"][1] == [[33, "33"], "33%", "Your job application progress is at 33 percent."]
    first, second = seen["cards"]
    assert first == {"name": "Avery_Quill_Resume_2025.pdf", "selected": True, "label": "Selected", "checked": True,
                     "radio": ["jobsDocumentCardToggle-0", False],
                     "toggle": "Deselect resume Avery_Quill_Resume_2025.pdf"}
    assert second == {"name": "resume_avery_quill.pdf", "selected": False, "label": "Select this resume",
                      "checked": False, "radio": ["jobsDocumentCardToggle-1", False],
                      "toggle": "Select resume resume_avery_quill.pdf"}
    assert [(c["name"], c["selected"], c["checked"]) for c in seen["chosen"]] == [
        ("Avery_Quill_Resume_2025.pdf", False, False), ("resume_avery_quill.pdf", True, True)]
    assert seen["required"] == ["Please enter a valid answer"] * 3
    assert seen["invalid"] == [True] * 4  # the numeric input, both radios and the select
    assert seen["after_enter"] == seen["url"]
    assert seen["numeric"] == ["Enter a whole number between 0 and 99"]
    assert "resume_avery_quill.pdf" in seen["review"][1] and "Yes" in seen["review"][2]
    assert seen["edits"] == ["Edit Contact info", "Edit Resume", "Edit Additional Questions"]
    assert seen["follow"] is True
    state = seen["state"]
    assert state["step"] == 4 and state["open"] and not state["submitted"]
    # The pre-filled contact answers were never written; the others were.
    assert {k: state["writes"][k] for k in ("email", "phone_country", "phone", "city", "follow")} == dict.fromkeys(
        ("email", "phone_country", "phone", "city", "follow"), 0)
    assert all(state["writes"][k] > 0 for k in ("sql_years", "work_authorization", "sponsorship", "resume"))
    assert state["answers"]["resume"] == "resume_avery_quill.pdf" and state["answers"]["sql_years"] == "7"
    before, requests_before = seen["before_submit"]
    assert (before["accepted_count"], before["rejected_count"], requests_before) == (0, 0, [])
    assert seen["requests"] == [f"POST {server.url('/jobs/modal-wizard/easy-apply?resumes=match')}"]
    assert "Your application for Growth Marketing Lead (Job ID BWA-LI-130) was sent to Brambleway Analytics." in (
        seen["confirmation"])
    count, after = seen["done"]
    assert count == 0 and after["submitted"] and not after["open"]
    summary = server.submissions("modal-wizard")
    assert (summary["accepted_count"], summary["rejected_count"]) == (1, 0)
    (record,) = summary["submissions"]
    assert record["fields"] == dict(EASY_ANSWERS, sql_years="7", follow_company="yes")
    assert record["files"] == {"resume": {"source": "saved_resume", "filename": "resume_avery_quill.pdf",
                                          "details": "1 KB · Last used on 3/2/2026"}}
    assert record["extra_fields"] == {}


def test_easy_apply_button_trigger_opens_after_a_delay_over_page_decoys(
    kit: SimpleNamespace, server: Any
) -> None:
    async def scenario() -> dict[str, Any]:
        seen: dict[str, Any] = {}
        async with chromium() as browser:
            page = await browser.new_page()
            await page.add_init_script(CLOCK)
            await page.goto(server.url("/jobs/modal-wizard?trigger=button"))
            trigger = page.locator("#jobs-apply-button-id")
            seen["trigger"] = await trigger.evaluate(
                "b => [b.hasAttribute('type'), b.closest('form'), b.getAttribute('aria-label'), "
                "b.hasAttribute('data-live-test-job-apply-button'), b.textContent]")
            seen["decoys"] = await page.evaluate("""() => ({
              search: Array.from(document.querySelectorAll('form[role=search]')).map((f) =>
                [f.getAttribute('method'), f.getAttribute('action'), f.querySelector('input[type=search]').name,
                 f.querySelector('input').getAttribute('aria-label')]),
              loose: Array.from(document.querySelectorAll('textarea, input[type=text]')).map((e) =>
                [e.getAttribute('aria-label'), e.closest('form') === null]),
            })""")
            pill = page.locator(".filter-pill", has_text="Easy Apply")
            await pill.click()
            seen["pill"] = await pill.get_attribute("aria-pressed")
            url = page.url
            await trigger.click()
            await page.wait_for_selector(DIALOG)
            seen["urls"] = (url, page.url)
            seen["delay"] = await page.evaluate("window.__clock.dialog - window.__clock.click")
            seen["opened"] = await page.evaluate(EASY)
            await page.click(NEXT)
            await page.click(BACK)
            seen["back"] = (await progress(page))[1]
            await page.click("button[aria-label='Dismiss']")
            seen["dismissed"] = (await page.locator(DIALOG).count(), await page.evaluate(EASY))
        return seen

    seen = kit.run(scenario())
    assert seen["trigger"] == [False, None, "Easy Apply to Growth Marketing Lead at Brambleway Analytics", True,
                               "Easy Apply"]
    assert seen["decoys"] == {
        "search": [["get", "/jobs/modal-wizard", "keywords", "Search jobs"]],
        "loose": [["Write a message to the hiring team", True], ["Add a note about this job", True]],
    }
    assert seen["pill"] == "true"  # a search filter toggle, not an apply control
    assert seen["delay"] >= 400 - TIMER_SLACK_MS  # the dialog follows a simulated 400 ms request
    url, after = seen["urls"]
    assert url == after == server.url("/jobs/modal-wizard?trigger=button")
    assert seen["opened"]["open"] and seen["opened"]["step"] == 1 and seen["opened"]["opens"] == 1
    assert seen["back"] == "0%"
    count, state = seen["dismissed"]
    assert count == 0 and not state["open"] and not state["submitted"]
    summary = server.submissions("modal-wizard")
    assert (summary["accepted_count"], summary["rejected_count"]) == (0, 0)


def test_easy_apply_resume_card_variants(kit: SimpleNamespace, server: Any) -> None:
    async def scenario() -> dict[str, Any]:
        seen: dict[str, Any] = {}
        async with chromium() as browser:
            for variant in ("match", "one", "nomatch", "none"):
                page = await browser.new_page()
                await page.goto(server.url(f"/jobs/modal-wizard/apply?resumes={variant}"))
                await page.wait_for_selector(DIALOG)
                await page.click(NEXT)
                cards = await page.locator(".ui-attachment").evaluate_all(CARDS)
                more = await page.locator(".jobs-document-upload__show-more-less-button").evaluate_all(
                    "bs => bs.map((b) => [b.getAttribute('aria-label'), b.textContent])")
                seen[variant] = ([(c["name"], c["selected"]) for c in cards], more)
                if variant == "one":
                    # The selected card's toggle deselects it; the step then requires a resume.
                    await page.locator("label[for=jobsDocumentCardToggle-0]").click()
                    await page.click(NEXT)
                    seen["deselected"] = (await errors(page), await page.evaluate(EASY),
                                          await page.locator(".ui-attachment").evaluate_all(CARDS))
                    await page.locator("label[for=jobsDocumentCardToggle-0]").check()
                    await page.click(NEXT)
                    seen["reselected"] = (await page.evaluate(EASY))["step"]
                if variant == "none":
                    await page.click(NEXT)
                    seen["no_card"] = await errors(page)
                    upload = page.locator("#jobs-document-upload-file-input-upload-resume")
                    seen["upload_input"] = await upload.evaluate(
                        "i => [i.name, i.className, getComputedStyle(i).display, i.accept]")
                    await upload.set_input_files(str(kit.RESUME_PATH))
                    seen["uploaded"] = (await page.locator(".ui-attachment").evaluate_all(CARDS),
                                        await page.locator(".ui-attachment p").all_inner_texts(),
                                        await errors(page), await page.evaluate(EASY))
                await page.close()
        return seen

    seen = kit.run(scenario())
    more = [["Show 1 more resumes", "Show 1 more resumes"]]
    assert seen["match"] == ([("Avery_Quill_Resume_2025.pdf", True), ("resume_avery_quill.pdf", False)], more)
    assert seen["one"] == ([("Avery_Quill_Resume_2025.pdf", True)], [])
    assert seen["nomatch"] == ([("Avery_Quill_Resume_2025.pdf", True), ("AQ_CV_marketing.docx", False)], more)
    assert seen["none"] == ([], [])
    required, state, cards = seen["deselected"]
    assert required == ["Please select or upload a resume"]
    assert state["step"] == 2 and state["answers"]["resume"] is None and state["writes"]["resume"] > 0
    assert cards[0]["checked"] is False and cards[0]["toggle"] == "Select resume Avery_Quill_Resume_2025.pdf"
    assert seen["reselected"] == 3
    assert seen["no_card"] == ["Please select or upload a resume"]
    assert seen["upload_input"] == [
        "file", "hidden", "none",
        "application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/pdf"]
    cards, details, after_errors, state = seen["uploaded"]
    assert [(c["name"], c["selected"], c["checked"]) for c in cards] == [("resume_avery_quill.pdf", True, True)]
    assert details == ["1 KB · Uploaded just now"] and after_errors == []
    assert state["answers"]["resume"] == "resume_avery_quill.pdf" and state["answers"]["resume_uploaded"]
    assert server.submissions("modal-wizard")["accepted_count"] == 0


def test_easy_apply_shadow_variant_uploads_and_submits_inside_an_open_shadow_root(
    kit: SimpleNamespace, server: Any
) -> None:
    host = "document.getElementById('interop-outlet')"

    async def scenario() -> dict[str, Any]:
        seen: dict[str, Any] = {}
        async with chromium() as browser:
            page = await browser.new_page()
            await page.goto(server.url("/jobs/modal-wizard/apply?shadow=1&resumes=none"))
            await page.wait_for_selector(DIALOG)
            seen["dom"] = await page.evaluate(f"""() => {{
              const host = {host}, root = host.shadowRoot, r = host.getBoundingClientRect();
              const label = root.querySelector('label[for]');
              return {{
                testid: host.getAttribute('data-testid'), parent: host.parentElement.tagName, mode: root.mode,
                fixed: getComputedStyle(host).position, box: [r.x, r.y, r.width, r.height],
                viewport: [innerWidth, innerHeight],
                light: [document.querySelector('[role=dialog]'), document.getElementById('artdeco-modal-outlet')],
                shadow: [!!root.querySelector('style'), !!root.querySelector('#artdeco-modal-outlet [role=dialog]')],
                label: label.control === root.getElementById(label.htmlFor)
                  && document.getElementById(label.htmlFor) === null,
              }};
            }}""")
            seen["phone"] = await page.get_by_label("Mobile phone number").input_value()
            await page.click(NEXT)
            await page.set_input_files("#jobs-document-upload-file-input-upload-resume", str(kit.RESUME_PATH))
            await page.click(NEXT)
            await answer_questions(page)
            await page.click(REVIEW)
            await page.locator("label[for=follow-company-checkbox]").uncheck()
            seen["before"] = server.submissions("modal-wizard")["accepted_count"]
            await page.click(SUBMIT)
            await page.get_by_role("heading", name="Application submitted").wait_for()
            await page.get_by_role("button", name="Done").click()
            seen["closed"] = await page.evaluate(f"() => [{host}.getAttribute('style'), "
                                                 f"{host}.shadowRoot.childNodes.length]")
            seen["state"] = await page.evaluate(EASY)
        return seen

    seen = kit.run(scenario())
    dom = seen["dom"]
    assert (dom["testid"], dom["parent"], dom["mode"], dom["fixed"]) == ("interop-shadowdom", "BODY", "open", "fixed")
    assert dom["box"] == [0, 0, *dom["viewport"]]
    assert dom["light"] == [None, None] and dom["shadow"] == [True, True] and dom["label"] is True
    assert seen["phone"] == "3035550142"
    assert seen["before"] == 0
    assert seen["closed"] == [None, 0]
    assert seen["state"]["writes"]["follow"] > 0 and seen["state"]["writes"]["phone"] == 0
    (record,) = server.submissions("modal-wizard")["submissions"]
    assert record["fields"] == dict(EASY_ANSWERS)  # follow_company unchecked: not posted
    resume = record["files"]["resume"]
    data = kit.RESUME_PATH.read_bytes()
    assert (resume["filename"], resume["size"], resume["sha256"]) == (
        "resume_avery_quill.pdf", len(data), hashlib.sha256(data).hexdigest())


def test_easy_apply_server_accepts_only_the_dialog_request(kit: SimpleNamespace, server: Any) -> None:
    status, _, _ = request(server, "POST", "/jobs/modal-wizard/apply", b"email=x",
                           {"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 405

    def post(fields: Sequence[tuple[str, str]], resumes: str,
             files: Sequence[tuple[str, str, bytes, str]] = ()) -> tuple[int, Any]:
        body, headers = multipart(fields, files)
        status, _, text = request(server, "POST", f"/jobs/modal-wizard/easy-apply?resumes={resumes}", body, headers)
        return status, json.loads(text)

    bad = [*EASY_ANSWERS[:4], ("sql_years", "5 years"), *EASY_ANSWERS[5:], ("resume_choice", "resume_avery_quill.pdf")]
    status, payload = post(bad, "one")  # that card is not offered in the "one" variant
    assert status == 422 and payload["accepted"] is False
    assert payload["errors"] == {"sql_years": "Enter a whole number between 0 and 99",
                                 "resume": "Select one of your saved resumes or upload a resume."}
    status, payload = post(EASY_ANSWERS, "none", [("resume", "resume.txt", b"plain text", "text/plain")])
    assert status == 422 and payload["errors"] == {"resume": "Upload a DOC, DOCX or PDF file."}
    status, payload = post(EASY_ANSWERS, "none")
    assert status == 422 and payload["errors"] == {"resume": "Attach a file."}
    summary = server.submissions("modal-wizard")
    assert (summary["accepted_count"], summary["rejected_count"]) == (0, 3)

    status, payload = post([*EASY_ANSWERS, ("resume_choice", "AQ_CV_marketing.docx")], "nomatch")
    assert status == 200 and payload["accepted"] is True
    assert (payload["job_code"], payload["confirmation_reference"]) == ("BWA-LI-130", "BWA-000001")
    (record,) = server.submissions("modal-wizard")["submissions"]
    assert record["files"]["resume"]["filename"] == "AQ_CV_marketing.docx" and record["extra_fields"] == {}


# --- iframe-embed: a careers page embedding a Greenhouse-style iframe -------------------------


def test_careers_page_injects_the_greenhouse_iframe_and_submits_inside_it(
    kit: SimpleNamespace, server: Any
) -> None:
    async def scenario() -> dict[str, Any]:
        seen: dict[str, Any] = {}
        async with chromium() as browser:
            page = await browser.new_page()
            await page.add_init_script("""new MutationObserver(() => {
              if (!window.__iframeAt && document.getElementById('grnhse_iframe')) window.__iframeAt = performance.now();
            }).observe(document, {childList: true, subtree: true});""")
            await page.goto(server.url("/jobs/iframe-embed"))
            panels = """() => [
              getComputedStyle(document.getElementById('job-detail-panel')).display,
              getComputedStyle(document.getElementById('job-application-panel')).display,
              document.getElementById('tab-overview').getAttribute('aria-selected'),
              document.getElementById('tab-application').getAttribute('aria-selected')]"""
            seen["initial"] = await page.evaluate(panels)
            seen["apply_now"] = await page.locator(".apply-btn").evaluate(
                "b => [b.textContent, b.getAttribute('aria-label'), b.hasAttribute('type'), b.closest('form')]")
            await page.wait_for_selector("#grnhse_iframe", state="attached")
            seen["iframe"] = await page.evaluate("""() => {
              const f = document.getElementById('grnhse_iframe');
              return [f.parentElement.id, ...['title', 'width', 'height', 'frameborder', 'scrolling', 'src']
                .map((a) => f.getAttribute(a)), window.__iframeAt];
            }""")
            url = page.url
            await page.click(".apply-btn")
            seen["opened"] = (await page.evaluate(panels), page.url == url)
            frame = page.frame_locator("#grnhse_iframe")
            seen["action"] = await frame.locator("form").get_attribute("action")
            await frame.get_by_label("First name").fill("Avery")
            await frame.get_by_label("Last name").fill("Quill")
            await frame.get_by_label("Email").fill("avery.quill@example.test")
            await frame.get_by_label("Phone").fill("+1 (303) 555-0142")
            await frame.get_by_label("Resume").set_input_files(str(kit.RESUME_PATH))
            await frame.get_by_label("Are you legally authorized to work in the United States?").select_option(
                "wa_authorized")
            await frame.get_by_label("Years of professional experience").select_option("yrs_6_9")
            await frame.get_by_label("No, I will not require sponsorship").check()
            await frame.get_by_label("Primary skills").select_option(["sk_python", "sk_sql"])
            await frame.get_by_label("Why do you want to work at Brambleway Analytics?").fill("Fictional answer.")
            await frame.get_by_role("button", name="Submit application").click()
            await frame.get_by_role("heading", name="Application submitted").wait_for()
            seen["after"] = (page.url == url, [f.url for f in page.frames][1])
            await page.goto(server.url("/jobs/iframe-embed?panel=visible"))
            seen["visible"] = await page.evaluate(panels)
        return seen

    seen = kit.run(scenario())
    assert seen["initial"] == ["block", "none", "true", "false"]
    assert seen["apply_now"] == ["Apply Now", "Switch to application form", False, None]
    parent, *attributes, injected_at = seen["iframe"]
    assert parent == "grnhse_app"
    assert attributes == ["Greenhouse Job Board", "100%", "1200", "0", "no",
                          "/embed/job_app?for=brambleway&token=4007131"]
    assert injected_at >= 300  # the stand-in for the Greenhouse embed script waits 300 ms
    assert seen["opened"] == (["none", "block", "false", "true"], True)  # no navigation
    assert seen["action"] == "/jobs/iframe-embed/apply"
    same_top, frame_url = seen["after"]
    assert same_top and frame_url.startswith(server.url("/applications/sub_"))
    assert seen["visible"] == ["none", "block", "false", "true"]
    (record,) = server.submissions("iframe-embed")["submissions"]
    assert record["fields"]["skills"] == ["sk_python", "sk_sql"] and record["files"]["resume"]["size"] == 802


def test_embed_route_serves_the_form_only_where_it_may(kit: SimpleNamespace, server: Any) -> None:
    base = "/embed/job_app?for=brambleway&token=4007131"
    status, _, form_page = request(server, "GET", base)
    _, _, apply_page = request(server, "GET", "/jobs/iframe-embed/apply")
    assert status == 200 and form_page == apply_page  # exactly the job's form page
    assert 'action="/jobs/iframe-embed/apply"' in form_page
    assert request(server, "GET", "/embed/job_app?for=brambleway&token=4007132")[0] == 404
    assert request(server, "GET", "/embed/job_app?for=elsewhere&token=4007131")[0] == 404
    only = base + "&embedded_only=1"
    refusal = "This application form can only be shown on the Brambleway Analytics careers page."
    for headers in ({}, {"Sec-Fetch-Dest": "document"}, {"Sec-Fetch-Dest": "empty"}):
        status, _, text = request(server, "GET", only, headers=headers)
        assert status == 403 and refusal in text and "<form" not in text, headers
    status, _, text = request(server, "GET", only, headers={"Sec-Fetch-Dest": "iframe"})
    assert status == 200 and text == apply_page

    async def scenario() -> tuple[Any, Any, Any]:
        async with chromium() as browser:
            page = await browser.new_page()
            await page.goto(server.url("/jobs/iframe-embed?embedded_only=1"))
            await page.wait_for_selector("#grnhse_iframe", state="attached")
            src = await page.get_attribute("#grnhse_iframe", "src")
            first_name = page.frame_locator("#grnhse_iframe").get_by_label("First name")
            await first_name.wait_for(state="attached")
            label = await first_name.count()
            response = await page.goto(server.url(src or ""))  # a top-level load of the frame URL
            return src, label, (response.status if response else None, await page.locator("main").inner_text())

    src, label, (top_status, top_text) = kit.run(scenario())
    assert src == only and label == 1  # the browser sends Sec-Fetch-Dest: iframe for the frame
    assert top_status == 403 and refusal in top_text


# --- stepper-ambiguous: a JazzHR-style form with anchor actions -------------------------------


def test_jazzhr_form_has_only_anchor_actions_and_submits_by_script(kit: SimpleNamespace, server: Any) -> None:
    _, _, document = request(server, "GET", "/jobs/stepper-ambiguous/apply")
    form = form_markup(document, "form_submit_new_resume")
    assert "<button" not in form
    assert 'method="post" action="/jobs/stepper-ambiguous/apply" enctype="multipart/form-data"' in form
    assert form.count('type="hidden"') == 6
    assert '<a id="resumator-submit-resume" class="btn btn-primary" href="#">Submit Application</a>' in form
    assert ('<input type="file" id="resumator-resume-file" name="resume" accept=".pdf,.doc,.docx" '
            'aria-label="Resume"') in form
    assert "<button>Dismiss</button> <button>ALLOW</button> <button>REJECT ALL</button>" in document
    assert '<a role="button" href="#" class="share">Share</a>' in document

    async def scenario() -> dict[str, Any]:
        seen: dict[str, Any] = {}
        async with chromium() as browser:
            page = await browser.new_page()
            await page.goto(server.url("/jobs/stepper-ambiguous/apply"))
            seen["mobile_apply_hidden"] = await page.locator("#resumator-mobile-apply-button").is_hidden()
            await page.click("#resumator-cookie-consent button:text-is('REJECT ALL')")
            seen["bar_hidden"] = await page.locator("#resumator-cookie-consent").is_hidden()
            await page.click("a.share")
            await page.click("#resumator-submit-resume")
            seen["client_errors"] = (await page.locator("#form_submit_new_resume [role=alert]").all_inner_texts(),
                                     page.url, server.submissions("stepper-ambiguous")["rejected_count"])
            await page.get_by_label("First Name").fill("Avery")
            await page.get_by_label("Last Name").fill("Quill")
            await page.get_by_label("Email").fill("avery.quill@example.test")
            await page.get_by_label("Phone").fill("3035550142")
            await page.get_by_label("How did you hear about this job?").select_option(label="LinkedIn")
            async with page.expect_file_chooser() as chooser:
                await page.click("#resumator-choose-upload")  # the anchor opens the hidden file input
            await (await chooser.value).set_files(str(kit.RESUME_PATH))
            seen["attached"] = await page.locator("#resumator-resume-filename").inner_text()
            async with page.expect_navigation():
                await page.click("#resumator-submit-resume")
            seen["url"] = page.url
        return seen

    seen = kit.run(scenario())
    assert seen["mobile_apply_hidden"] and seen["bar_hidden"]
    messages, url, rejected = seen["client_errors"]
    assert messages == ["This field is required."] * 5 + ["Attach or paste your resume."]
    assert url == server.url("/jobs/stepper-ambiguous/apply") and rejected == 0  # validated on the client
    assert seen["attached"] == "Attached: resume_avery_quill.pdf"
    assert seen["url"].startswith(server.url("/applications/sub_"))
    (record,) = server.submissions("stepper-ambiguous")["submissions"]
    assert record["fields"] == {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
                                "phone": "3035550142", "heard_about": "hear_linkedin"}
    assert record["extra_fields"] == {
        "resumator-job-id": "BWA-JZ-132", "resumator-board-code": "bramblewayanalytics",
        "resumator-source": "applytojob", "resumator-referrer": "", "resumator-applicant-token": "c0ffee0132",
        "resumator-form-version": "3"}
    assert record["files"]["resume"]["filename"] == "resume_avery_quill.pdf"


def test_jazzhr_sections_post_nothing_before_submit(kit: SimpleNamespace, server: Any) -> None:
    async def scenario() -> dict[str, Any]:
        seen: dict[str, Any] = {}
        async with chromium() as browser:
            page = await browser.new_page()
            posts: list[str] = []
            page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
            await page.goto(server.url("/jobs/stepper-ambiguous/apply?sections=2"))
            one, two = page.locator("#resumator-section-1"), page.locator("#resumator-section-2")
            seen["initial"] = (await one.is_visible(), await two.is_visible(),
                               await one.locator("a.btn").all_inner_texts(), await two.locator("a").all_inner_texts())
            await page.get_by_label("First Name").fill("Avery")
            await one.locator("a.btn", has_text="Save").click()
            seen["saved"] = (await page.locator("#resumator-save-status").inner_text(),
                             await page.evaluate("() => JSON.parse(sessionStorage.getItem('resumator-saved-application'))"))
            await one.locator("a.btn", has_text="Next").click()
            seen["blocked"] = (await one.locator("[role=alert]").all_inner_texts(), await two.is_visible())
            await page.get_by_label("Last Name").fill("Quill")
            await page.get_by_label("Email").fill("avery.quill@example.test")
            await page.get_by_label("Phone").fill("3035550142")
            await one.locator("a.btn", has_text="Next").click()
            seen["next"] = (await one.is_visible(), await two.is_visible())
            await two.locator("a.btn", has_text="Back").click()
            seen["back"] = (await one.is_visible(), await two.is_visible())
            await one.locator("a.btn", has_text="Next").click()
            await page.click("#resumator-choose-paste")  # a pasted resume stands in for the file
            await page.get_by_label("Paste resume").fill("Avery Quill. Paid media programs since 2019.")
            await page.get_by_label("How did you hear about this job?").select_option(label="Indeed")
            seen["posts_before"] = (list(posts), server.submissions("stepper-ambiguous")["accepted_count"])
            async with page.expect_navigation():
                await page.click("#resumator-submit-resume")
            seen["posts"] = list(posts)
        return seen

    seen = kit.run(scenario())
    assert seen["initial"] == (True, False, ["Next", "Save"],
                               ["Attach resume", "Paste resume", "Back", "Submit Application"])
    assert seen["saved"] == ("Saved", {"first_name": "Avery", "last_name": "", "email": "", "phone": ""})
    assert seen["blocked"] == (["This field is required."] * 3, False)
    assert seen["next"] == (False, True) and seen["back"] == (True, False)
    assert seen["posts_before"] == ([], 0)
    assert seen["posts"] == [server.url("/jobs/stepper-ambiguous/apply?sections=2")]
    (record,) = server.submissions("stepper-ambiguous")["submissions"]
    assert record["fields"]["resume_text"] == "Avery Quill. Paid media programs since 2019."
    assert record["fields"]["heard_about"] == "hear_indeed" and record["files"] == {}


def test_jazzhr_rejection_re_renders_the_anchor_form(server: Any) -> None:
    body, headers = multipart([("first_name", "Avery"), ("email", "not-an-email"), ("resumator-job-id", "BWA-JZ-132")])
    status, _, document = request(server, "POST", "/jobs/stepper-ambiguous/apply?sections=2", body, headers)
    assert status == 422
    form = form_markup(document, "form_submit_new_resume")
    assert "<button" not in form and 'action="/jobs/stepper-ambiguous/apply?sections=2"' in form
    assert "There is a problem with your application" in document
    assert ('<input type="email" class="form-control" id="resumator-email-value" name="email" '
            'value="not-an-email" autocomplete="email" aria-required="true" aria-invalid="true" '
            'aria-describedby="resumator-email-value-error">') in form
    assert '<span class="help-block" id="resumator-resume-file-error" role="alert">Attach a file.</span>' in form
    assert '<div class="resumator-form-section" id="resumator-section-1">' in form  # it holds the first error
    summary = server.submissions("stepper-ambiguous")
    assert (summary["accepted_count"], summary["rejected_count"]) == (0, 1)
    assert set(summary["rejections"][0]["errors"]) == {"last_name", "email", "phone", "heard_about", "resume"}


# --- apply-in-alert-form: a Dayforce-style posting inside a job-alert form --------------------


def test_alert_form_routes_record_alerts_but_never_applications(server: Any) -> None:
    _, _, posting = request(server, "GET", "/jobs/apply-in-alert-form")
    form = form_markup(posting, "aspnetForm")
    assert posting.count("<form") == 1 and "<h1>Marketing Project Manager</h1>" in form
    assert 'method="post" action="/jobs/apply-in-alert-form/start"' in form
    assert ('<button type="submit" name="apply" value="1" aria-label="Apply for Marketing Project Manager">'
            "Apply</button>") in form
    assert ('<button type="submit" formaction="/jobs/apply-in-alert-form/alerts" name="subscribe" value="1">'
            "Subscribe</button>") in form
    urlencoded = {"Content-Type": "application/x-www-form-urlencoded"}
    status, headers, _ = request(server, "POST", "/jobs/apply-in-alert-form/start", b"apply=1&alert_email=",
                                 urlencoded)
    assert (status, headers["Location"]) == (303, "/jobs/apply-in-alert-form/apply?flowSelection=true")
    assert server.submissions("apply-in-alert-form")["alert_count"] == 0
    status, _, text = request(server, "POST", "/jobs/apply-in-alert-form/alerts", b"subscribe=1&alert_email=",
                              urlencoded)
    assert status == 422 and "Enter an email address for job alerts." in text
    status, _, text = request(server, "POST", "/jobs/apply-in-alert-form/alerts",
                              b"subscribe=1&alert_email=avery.quill%40example.test", urlencoded)
    assert status == 200 and "You are subscribed" in text
    status, _, _ = request(server, "POST", "/jobs/apply-in-alert-form/start",
                           b"apply=1&alert_email=avery.quill%40example.test", urlencoded)
    assert status == 303  # Apply with the alert email typed in subscribes as well
    summary = server.submissions("apply-in-alert-form")
    assert summary["alert_count"] == 2 and summary["accepted_count"] == summary["rejected_count"] == 0
    assert [(a["job_id"], a["email"]) for a in summary["alerts"]] == [
        ("apply-in-alert-form", "avery.quill@example.test")] * 2
    assert server.submissions("standard")["alert_count"] == 0

    _, _, flow = request(server, "GET", "/jobs/apply-in-alert-form/apply?flowSelection=true")
    assert "<h1>How would you like to apply?</h1>" in flow and "<form" not in flow
    assert '<button type="button" class="ant-btn ant-btn-primary">Apply without an Account</button>' in flow
    _, _, direct = request(server, "GET", "/jobs/apply-in-alert-form/apply")
    _, _, manual = request(server, "GET", "/jobs/apply-in-alert-form/apply/manual")
    assert direct == manual and 'action="/jobs/apply-in-alert-form/apply"' in manual
    _, _, spa = request(server, "GET", "/jobs/apply-in-alert-form?nav=spa")
    assert "<form" not in spa  # its route content sits escaped in a JSON island
    assert ('<button type="button" class="ant-btn ant-btn-primary" test-id="apply-button" '
            'aria-label="Apply for Marketing Project Manager">Apply</button>') in spa


def test_alert_form_apply_leads_to_the_form_by_a_delayed_client_route(kit: SimpleNamespace, server: Any) -> None:
    async def scenario() -> dict[str, Any]:
        seen: dict[str, Any] = {}
        async with chromium() as browser:
            page = await browser.new_page()
            await page.add_init_script(CLOCK)
            await page.goto(server.url("/jobs/apply-in-alert-form"))
            async with page.expect_navigation():
                await page.get_by_role("button", name="Apply for Marketing Project Manager").click()
            seen["flow"] = (page.url, await page.locator("h1").inner_text(), await page.evaluate("document.forms.length"),
                            server.submissions("apply-in-alert-form")["alert_count"])
            await page.evaluate("() => { window.__sameDocument = true; }")
            await page.get_by_role("button", name="Apply without an Account").click()
            await page.wait_for_url("**/apply/manual")
            seen["delay"] = await page.evaluate("window.__clock.pushes.map((t) => t - window.__clock.click)")
            seen["manual"] = (await page.evaluate("window.__sameDocument === true"), await page.title(),
                              await page.locator("form").get_attribute("action"))
            await page.get_by_label("First name").fill("Avery")
            await page.get_by_label("Last name").fill("Quill")
            await page.get_by_label("Email").fill("avery.quill@example.test")
            await page.get_by_label("Phone").fill("+1 (303) 555-0142")
            await page.get_by_label("Resume").set_input_files(str(kit.RESUME_PATH))
            await page.get_by_label("Are you legally authorized to work in the United States?").select_option(
                "wa_authorized")
            await page.get_by_label("No, I will not require sponsorship").check()
            async with page.expect_navigation():
                await page.get_by_role("button", name="Submit application").click()
            seen["confirmed"] = page.url
            await page.goto(server.url("/jobs/apply-in-alert-form/apply?flowSelection=true"))
            async with page.expect_navigation():
                await page.get_by_role("button", name="Sign In").click()
            seen["sign_in"] = page.url
        return seen

    seen = kit.run(scenario())
    assert seen["flow"] == (server.url("/jobs/apply-in-alert-form/apply?flowSelection=true"),
                            "How would you like to apply?", 0, 0)
    [delay] = seen["delay"]  # one pushState, after the portal's 2.5 s
    assert delay >= 2500 - TIMER_SLACK_MS
    assert seen["manual"] == (True, "Apply: Marketing Project Manager | Brambleway Analytics Careers",
                              "/jobs/apply-in-alert-form/apply")
    assert seen["confirmed"].startswith(server.url("/applications/sub_"))
    assert seen["sign_in"] == server.url("/login?next=/jobs/apply-in-alert-form/apply%3FflowSelection%3Dtrue")
    summary = server.submissions("apply-in-alert-form")
    assert (summary["accepted_count"], summary["alert_count"]) == (1, 0)
    assert summary["submissions"][0]["fields"]["email"] == "avery.quill@example.test"


def test_alert_form_spa_apply_is_a_delayed_push_state(kit: SimpleNamespace, server: Any) -> None:
    async def scenario() -> dict[str, Any]:
        seen: dict[str, Any] = {}
        async with chromium() as browser:
            page = await browser.new_page()
            await page.add_init_script(CLOCK)
            await page.goto(server.url("/jobs/apply-in-alert-form?nav=spa"))
            seen["forms"] = await page.evaluate("document.forms.length")
            await page.evaluate("() => { window.__sameDocument = true; }")
            await page.locator("[test-id=apply-button]").click()
            await page.wait_for_url("**/apply?flowSelection=true")
            delays = "window.__clock.pushes.map((t) => t - window.__clock.click)"
            seen["delays"] = [await page.evaluate(delays)]
            seen["flow"] = (await page.evaluate("window.__sameDocument === true"),
                            await page.locator("h1").inner_text(), await page.evaluate("document.forms.length"))
            await page.get_by_role("button", name="Apply without an Account").click()
            await page.wait_for_url("**/apply/manual")
            seen["delays"].append(await page.evaluate(delays))
            seen["manual"] = (await page.evaluate("window.__sameDocument === true"), page.url,
                              await page.evaluate("document.forms.length"),
                              await page.locator("main").evaluate("m => m.innerHTML"))
            loaded = await browser.new_page()
            await loaded.goto(server.url("/jobs/apply-in-alert-form/apply/manual"))
            seen["loaded"] = await loaded.locator("main").evaluate("m => m.innerHTML")
        return seen

    seen = kit.run(scenario())
    assert seen["forms"] == 0
    [flow_delay], [_, manual_delay] = seen["delays"]  # each route waits out the portal's 2.5 s
    assert flow_delay >= 2500 - TIMER_SLACK_MS and manual_delay >= 2500 - TIMER_SLACK_MS
    assert seen["flow"] == (True, "How would you like to apply?", 0)
    same_document, url, forms, markup = seen["manual"]
    assert same_document and url == server.url("/jobs/apply-in-alert-form/apply/manual") and forms == 1
    assert markup == seen["loaded"].strip()  # exactly what the route renders on a document load
    summary = server.submissions("apply-in-alert-form")
    assert (summary["accepted_count"], summary["alert_count"]) == (0, 0)
