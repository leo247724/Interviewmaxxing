"""C4R regressions: the seven independent-review probes, in real headless Chromium.

Each probe serves fictional pages on the localhost mock origin through Playwright
routing and records every request the page sends, so "no POST was dispatched" and
"exactly one POST" are asserted from the network, not from the runtime's word.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from interviewmaxxing_browser import (
    ConfirmationTie,
    PlaywrightSessionFactory,
    SubmissionRefused,
    classify,
    reconciliation_from,
    unsupported_control_needs,
)
from interviewmaxxing_core import (
    AnswerSource,
    BrowserOptions,
    ControlType,
    FieldFillStatus,
    NotSubmittedNext,
    PageKind,
    Provenance,
    SemanticType,
    SubmissionOutcome,
)

TIE = ConfirmationTie(external_job_id="ABC-123", job_title="Widget Engineer")


@dataclass
class Site:
    """Routed fictional pages: path -> (status, html) or a callable of the request."""

    pages: Mapping[str, Any]
    requests: list[tuple[str, str, dict[str, list[str]]]] = field(default_factory=list)

    def posts(self, path: str | None = None) -> list[dict[str, list[str]]]:
        return [body for method, p, body in self.requests
                if method == "POST" and (path is None or p == path)]

    def hits(self, path: str) -> list[str]:
        return [method for method, p, _ in self.requests if p == path]


def _html(body: str) -> str:
    return f"<!doctype html><html><head><title>Careers</title></head><body>{body}</body></html>"


async def _serve(browser: Any, origin: str, site: Site) -> None:
    async def handler(route: Any) -> None:
        request = route.request
        split = urlsplit(request.url)
        data = request.post_data or ""
        body = parse_qs(data, keep_blank_values=True) if request.method == "POST" else parse_qs(split.query)
        site.requests.append((request.method, split.path, body))
        entry = site.pages.get(f"{request.method} {split.path}", site.pages.get(split.path))
        if entry is None:
            await route.fulfill(status=404, content_type="text/html", body=_html("<h1>Not found</h1>"))
            return
        status, html = entry(body) if callable(entry) else entry
        await route.fulfill(status=status, content_type="text/html", body=html)

    await browser.page.route(f"{origin}/c4r/**", handler)


async def _session(options: BrowserOptions, origin: str, site: Site) -> Any:
    browser = await PlaywrightSessionFactory(settle_timeout_s=5).start(options)
    await _serve(browser, origin, site)
    return browser


def _run(kit: SimpleNamespace, options: BrowserOptions, origin: str, site: Site,
         steps: Callable[[Any], Awaitable[Any]]) -> Any:
    async def scenario() -> Any:
        browser = await _session(options, origin, site)
        try:
            return await steps(browser)
        finally:
            await browser.close()

    return kit.run(scenario())


# --- 1. questions revealed while filling are not authorized by the packet ------------

REVEAL_FORM = _html("""
<h1>Widget Engineer</h1><p>Job ID ABC-123</p>
<form method="post" action="/c4r/apply">
  <label for="email">Email</label><input id="email" name="email" type="email" required>
  <div id="more"></div>
  <button type="submit">Submit application</button>
</form>
<script>
  document.getElementById('email').addEventListener('input', () => {
    if (document.getElementById('attest')) return;
    document.getElementById('more').innerHTML =
      '<input type="checkbox" id="attest" name="attest" value="yes" required checked>' +
      '<label for="attest">I certify that I have never been dismissed for misconduct.</label>';
  });
</script>""")


def test_1_question_revealed_during_fill_is_contained_and_blocks_submit(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    site = Site({"GET /c4r/form": (200, REVEAL_FORM),
                 "POST /c4r/apply": (200, _html("<h1>Application submitted</h1><p>Job ID ABC-123</p>"))})

    async def steps(browser: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        form = (await browser.open(server.url("/c4r/form"))).form
        packet = kit.build(form, {"email": kit.CORE["email"]}).packet
        assert packet.is_complete and packet.problems_against(form) == []
        out["fill"] = await browser.fill(form, packet)
        out["still_checked"] = await browser.page.is_checked("#attest")
        out["action"] = await browser.submit()
        out["refused"] = await browser.confirm()
        # A fresh inspection shows the new question; the old packet no longer fits it.
        fresh = (await browser.inspect()).form
        out["fresh"] = fresh
        out["old_packet_problems"] = packet.problems_against(fresh)
        # Only the user's explicit answer to the new attestation lets it be submitted.
        answered = kit.build(fresh, {"email": kit.CORE["email"], "attest": True})
        out["fill2"] = await browser.fill(fresh, answered.packet)
        out["action2"] = await browser.submit()
        out["accepted"] = await browser.confirm()
        return out

    out = _run(kit, options, server.origin, site, steps)
    fill = out["fill"]
    assert not fill.ok
    assert any("changed while filling" in e for e in fill.page_errors)
    attest = next(r for r in fill.fields if r.field_id == "attest")
    assert attest.status is FieldFillStatus.FAILED and "cleared" in (attest.detail or "")
    assert out["still_checked"] is False  # a pre-checked, unanswered attestation is never left on
    assert not out["action"].dispatched and "changed after filling" in (out["action"].detail or "")
    assert out["refused"].outcome is SubmissionOutcome.NOT_SUBMITTED
    assert out["fresh"].field("attest").semantic_type is SemanticType.ATTESTATION
    assert out["old_packet_problems"]  # fingerprint changed, attest unanswered
    assert out["fill2"].ok and out["action2"].dispatched
    assert out["accepted"].outcome is SubmissionOutcome.ACCEPTED
    posts = site.posts("/c4r/apply")
    assert len(posts) == 1 and posts[0]["attest"] == ["yes"]  # only after the explicit answer


# --- 2. an ambiguous error page is not a definite rejection ---------------------------

def _apply_form(extra: str = "") -> str:
    return _html(f"""
<h1>Widget Engineer</h1><p>Job ID ABC-123</p>{extra}
<form method="post" action="/c4r/apply">
  <label for="email">Email</label><input id="email" name="email" type="email" required>
  <button type="submit">Submit application</button>
</form>""")


FIELD_ERROR_FORM = _html("""
<h1>Widget Engineer</h1><p>Job ID ABC-123</p>
<div role="alert">There is a problem with your application</div>
<form method="post" action="/c4r/apply">
  <label for="email">Email</label>
  <input id="email" name="email" type="email" required aria-invalid="true" aria-describedby="email-error">
  <p id="email-error" class="error">Use your work email address.</p>
  <button type="submit">Submit application</button>
</form>""")


@pytest.mark.parametrize(
    ("status", "page", "expected"),
    [
        # The review probe: HTTP 502, same form, alert saying the outcome is unknown.
        (502, _apply_form('<div role="alert">The connection timed out. We cannot confirm whether '
                          'your application was submitted.</div>'), SubmissionOutcome.UNKNOWN),
        # A generic page-level alert on a 200 is still not proof of rejection.
        (200, _apply_form('<div role="alert">Please try again.</div>'), SubmissionOutcome.UNKNOWN),
        # Field errors on a 5xx page are not trusted either.
        (503, FIELD_ERROR_FORM, SubmissionOutcome.UNKNOWN),
        # Definite: the step re-rendered marking a specific field invalid.
        (422, FIELD_ERROR_FORM, SubmissionOutcome.NOT_SUBMITTED),
    ],
)
def test_2_only_field_level_rejection_unlocks_retry(
    kit: SimpleNamespace, server: Any, options: BrowserOptions,
    status: int, page: str, expected: SubmissionOutcome,
) -> None:
    site = Site({"GET /c4r/form": (200, _apply_form()), "POST /c4r/apply": (status, page)})

    async def steps(browser: Any) -> tuple[Any, bool]:
        form = (await browser.open(server.url("/c4r/form"))).form
        await browser.fill(form, kit.build(form, {"email": kit.CORE["email"]}).packet)
        assert (await browser.submit()).dispatched
        observation = await browser.confirm()
        try:
            await browser.submit()
            retried = True
        except SubmissionRefused:
            retried = False
        return observation, retried

    observation, retried = _run(kit, options, server.origin, site, steps)
    assert observation.outcome is expected, observation.signals
    if expected is SubmissionOutcome.UNKNOWN:
        assert not retried  # no second dispatch after uncertainty
        assert len(site.posts("/c4r/apply")) == 1
    else:
        assert observation.next_state is NotSubmittedNext.NEEDS_INPUT
        assert "Email: Use your work email address." in observation.validation_errors


# --- 3. negated or conditional wording is not acceptance ------------------------------

NEGATIVE = [
    "No application received",
    "We have not received your application",
    "Application never submitted",
    "If your application was submitted, you will receive an email.",
    "Once your application is received we will contact you.",
    "Application submitted?",
    "Application submitted: no",
    "Click submit to have your application received.",
    "Application completeness check",
]


@pytest.mark.parametrize("wording", NEGATIVE)
def test_3_negated_wording_is_not_acceptance_on_reconcile(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, wording: str
) -> None:
    site = Site({"/c4r/status": (200, _html(f"<h1>Application status</h1><p>{wording}</p>"
                                            "<p>Widget Engineer · Job ID ABC-123</p>"))})
    observation = _run(kit, options, server.origin, site,
                       lambda b: b.reconcile(server.url("/c4r/status"), tie=TIE))
    assert observation.outcome is SubmissionOutcome.UNKNOWN, observation.signals
    assert reconciliation_from(observation) is None


def test_3_negated_wording_is_not_acceptance_after_submit(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    site = Site({"GET /c4r/form": (200, _apply_form()),
                 "POST /c4r/apply": (200, _html("<h1>No application received</h1>"
                                                "<p>Widget Engineer · Job ID ABC-123 · Reference: APP-5550</p>"))})

    async def steps(browser: Any) -> Any:
        form = (await browser.open(server.url("/c4r/form"))).form
        await browser.fill(form, kit.build(form, {"email": kit.CORE["email"]}).packet)
        await browser.submit()
        inspection = await browser.inspect()
        return await browser.confirm(), inspection

    observation, inspection = _run(kit, options, server.origin, site, steps)
    assert observation.outcome is SubmissionOutcome.UNKNOWN
    assert inspection.kind is not PageKind.CONFIRMATION


def test_3_affirmative_wording_tied_to_the_job_is_accepted_on_reconcile(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    site = Site({"/c4r/status": (200, _html("<h1>Application submitted</h1>"
                                            "<p>Widget Engineer · Job ID ABC-123</p>"
                                            "<p>Reference: APP-1234</p>"))})
    observation = _run(kit, options, server.origin, site,
                       lambda b: b.reconcile(server.url("/c4r/status"), tie=TIE))
    assert observation.outcome is SubmissionOutcome.ACCEPTED
    assert observation.confirmation_reference == "APP-1234"
    assert reconciliation_from(observation) is not None


# --- 4. a later portal must identify this job; records are never mixed ---------------

@pytest.mark.parametrize(
    "body",
    [
        # The review probe: an accepted record for another role; only its reference is new.
        "<h1>Application submitted</h1><p>Role: Graphic Designer</p><p>Reference: APP-9876</p>",
        # Two records: ours is not submitted, another one is.
        "<h1>My applications</h1><ul>"
        "<li>Widget Engineer (Job ID ABC-123): not submitted</li>"
        "<li>Graphic Designer: Application submitted. Reference: APP-9876</li></ul>",
        # Our title appears only in a different record than the acceptance.
        "<h1>My applications</h1><table>"
        "<tr><td>Widget Engineer</td><td>Draft</td></tr>"
        "<tr><td>Graphic Designer</td><td>Application received</td><td>Reference: APP-9876</td></tr>"
        "</table>",
    ],
)
def test_4_reconcile_does_not_accept_another_record(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, body: str
) -> None:
    site = Site({"/c4r/portal": (200, _html(body))})
    observation = _run(kit, options, server.origin, site,
                       lambda b: b.reconcile(server.url("/c4r/portal"), tie=TIE))
    assert observation.outcome is SubmissionOutcome.UNKNOWN, observation.signals


def test_4_reconcile_accepts_our_record_among_others(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    body = ("<h1>My applications</h1><ul>"
            "<li>Graphic Designer: Application submitted. Reference: APP-9876</li>"
            "<li>Widget Engineer (Job ID ABC-123): Application submitted. Reference: APP-1111</li></ul>")
    site = Site({"/c4r/portal": (200, _html(body))})
    observation = _run(kit, options, server.origin, site,
                       lambda b: b.reconcile(server.url("/c4r/portal"), tie=TIE))
    assert observation.outcome is SubmissionOutcome.ACCEPTED
    assert observation.confirmation_reference == "APP-1111"


def test_4_fresh_reference_ties_only_the_immediate_result(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    result = _html("<h1>Application submitted</h1><p>Reference: APP-9876</p>")
    site = Site({"GET /c4r/form": (200, _apply_form()), "POST /c4r/apply": (200, result),
                 "GET /c4r/portal": (200, result)})

    async def steps(browser: Any) -> tuple[Any, Any]:
        form = (await browser.open(server.url("/c4r/form"))).form
        await browser.fill(form, kit.build(form, {"email": kit.CORE["email"]}).packet)
        await browser.submit()
        immediate = await browser.confirm()
        later = await browser.reconcile(server.url("/c4r/portal"), tie=TIE)
        return immediate, later

    immediate, later = _run(kit, options, server.origin, site, steps)
    assert immediate.outcome is SubmissionOutcome.ACCEPTED  # appeared right after our click
    assert immediate.confirmation_reference == "APP-9876"
    assert later.outcome is SubmissionOutcome.UNKNOWN  # a later page must name the job


# --- 5. a status lookup may never send anything but a same-origin GET ------------------

def _status_page(button: str) -> str:
    return _html(f"""
<h1>Check your application status</h1><p>Widget Engineer · Job ID ABC-123</p>
<form method="get" action="/c4r/lookup">
  <label for="e">Email used on your application</label><input id="e" name="email" type="email">
  {button}
</form>""")


LOOKUP_RESULT = _html("<h1>Status</h1><p>Widget Engineer · Job ID ABC-123</p>"
                      "<p>Application received. Reference: APP-7777</p>")


@pytest.mark.parametrize(
    "button",
    [
        '<button type="submit" formmethod="post" formaction="/c4r/application">Check status</button>',
        '<button type="submit" formaction="https://elsewhere.example.test/lookup">Check status</button>',
        '<button type="submit" formtarget="_blank">Check status</button>',
    ],
)
def test_5_lookup_overrides_are_never_dispatched(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, button: str
) -> None:
    site = Site({"/c4r/status": (200, _status_page(button)), "/c4r/application": (200, LOOKUP_RESULT),
                 "/c4r/lookup": (200, LOOKUP_RESULT)})
    observation = _run(kit, options, server.origin, site,
                       lambda b: b.reconcile(server.url("/c4r/status"), tie=TIE,
                                             lookup_email=kit.CORE["email"]))
    assert observation.outcome is SubmissionOutcome.UNKNOWN
    assert site.posts() == []
    assert site.hits("/c4r/application") == [] and site.hits("/c4r/lookup") == []


def test_5_plain_get_lookup_is_used(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    site = Site({"/c4r/status": (200, _status_page('<button type="submit">Check status</button>')),
                 "/c4r/lookup": (200, LOOKUP_RESULT)})
    observation = _run(kit, options, server.origin, site,
                       lambda b: b.reconcile(server.url("/c4r/status"), tie=TIE,
                                             lookup_email=kit.CORE["email"]))
    assert observation.outcome is SubmissionOutcome.ACCEPTED
    assert site.hits("/c4r/lookup") == ["GET"] and site.posts() == []
    lookup = next(body for method, path, body in site.requests if path == "/c4r/lookup")
    assert lookup["email"] == [kit.CORE["email"]]


# --- 6. attestation/consent wording on any statement control needs an explicit answer --

RADIO_ATTEST = _html("""
<h1>Widget Engineer</h1><p>Job ID ABC-123</p>
<form method="post" action="/c4r/apply">
  <label for="email">Email</label><input id="email" name="email" type="email" required>
  <fieldset><legend>I certify that I have never been dismissed for misconduct.</legend>
    <input type="radio" id="d1" name="dismissal" value="yes" required><label for="d1">Yes</label>
    <input type="radio" id="d2" name="dismissal" value="no" required><label for="d2">No</label>
  </fieldset>
  <label for="bg">Do you consent to a background check?</label>
  <select id="bg" name="background" required><option value="">Select</option>
    <option value="y">Yes</option><option value="n">No</option></select>
  <label for="sig">Electronic signature (type your full name to certify)</label>
  <input id="sig" name="signature" required>
  <button type="submit">Submit application</button>
</form>""")


def test_6_statement_controls_are_explicit_answer_questions(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    site = Site({"GET /c4r/form": (200, RADIO_ATTEST)})
    fact = Provenance(source=AnswerSource.CANDIDATE_FACT, reference_ids=["fact.no_dismissal"])

    async def steps(browser: Any) -> tuple[Any, str]:
        form = (await browser.open(server.url("/c4r/form"))).form
        # A fact, mislabelled as an ordinary choice, must not answer the attestation.
        bad = kit.build(
            form,
            {"email": kit.CORE["email"], "dismissal": "Yes"},
            source_overrides={"dismissal": fact},
            semantic_overrides={"dismissal": SemanticType.CUSTOM_SELECT},
        )
        with pytest.raises(ValueError, match="ATTESTATION"):
            await browser.fill(form, bad.packet)
        return form, await browser.page.input_value("#email")

    form, typed = _run(kit, options, server.origin, site, steps)
    assert form.field("dismissal").control_type is ControlType.RADIO
    assert form.field("dismissal").semantic_type is SemanticType.ATTESTATION
    assert form.field("background").semantic_type is SemanticType.CONSENT
    assert form.field("signature").semantic_type is SemanticType.ATTESTATION
    assert typed == ""  # nothing was filled
    assert site.posts() == []


def test_6_classification_keeps_factual_questions_ordinary() -> None:
    assert classify(label="Do you hold an active FAA Part 107 remote pilot certificate?",
                    control_type=ControlType.RADIO) is SemanticType.CUSTOM_SELECT
    assert classify(label="What is your notice period?",
                    control_type=ControlType.SELECT) is SemanticType.CUSTOM_SELECT
    assert classify(label="I certify that the above is true", control_type=ControlType.SELECT) \
        is SemanticType.ATTESTATION
    assert classify(label="Why do you want to work here?",
                    control_type=ControlType.TEXTAREA) is SemanticType.CUSTOM_LONG_TEXT


# --- 7. a <button role="combobox"> is a control that blocks submit until operated -------

BUTTON_COMBO = _html("""
<h1>Widget Engineer</h1><p>Job ID ABC-123</p>
<form method="post" action="/c4r/apply">
  <label for="email">Email</label><input id="email" name="email" type="email" required>
  <div class="field">
    <button type="button" id="office" role="combobox" aria-label="Preferred office"
            aria-required="true" aria-controls="office-list" aria-expanded="false">Select an office</button>
    <ul id="office-list" role="listbox" hidden>
      <li role="option" aria-selected="false" data-value="den">Denver</li>
    </ul>
    <input type="hidden" name="office" value="">
  </div>
  <button type="button" id="add">Add another reference</button>
  <button type="submit">Submit application</button>
</form>""")


def test_7_button_combobox_is_unsupported_and_blocks_submit(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    site = Site({"GET /c4r/form": (200, BUTTON_COMBO),
                 "POST /c4r/apply": (200, _html("<h1>Application submitted</h1><p>Job ID ABC-123</p>"))})

    async def steps(browser: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        inspection = await browser.open(server.url("/c4r/form"))
        form = inspection.form
        out["form"] = form
        out["buttons"] = [b.button.text for b in browser.last_page.buttons]
        await browser.fill(form, kit.build(form, {"email": kit.CORE["email"]}).packet)
        out["blocked"] = await browser.submit()
        out["blocked_obs"] = await browser.confirm()
        # The person operates the widget; the page records the choice.
        await browser.page.evaluate("""() => {
            document.querySelector('[role=option]').setAttribute('aria-selected', 'true');
            document.querySelector('input[name=office]').value = 'den';
            document.getElementById('office').textContent = 'Denver';
        }""")
        ready = (await browser.wait_for_user("choose an office", timeout_s=5)).form
        out["ready"] = ready
        await browser.fill(ready, kit.build(ready, {"email": kit.CORE["email"]}).packet)
        out["action"] = await browser.submit()
        out["accepted"] = await browser.confirm()
        return out

    out = _run(kit, options, server.origin, site, steps)
    office = out["form"].field("office")
    assert office.control_type is ControlType.UNSUPPORTED and office.required
    assert office.label == "Preferred office"
    assert [n.field_id for n in unsupported_control_needs(out["form"])] == ["office"]
    assert "Select an office" not in out["buttons"]  # a widget, not an action
    assert "Add another reference" in out["buttons"]  # ordinary buttons stay actions
    assert not out["blocked"].dispatched
    assert out["blocked_obs"].next_state is NotSubmittedNext.NEEDS_INPUT
    assert not out["ready"].field("office").required
    assert out["action"].dispatched and out["accepted"].outcome is SubmissionOutcome.ACCEPTED
    posts = site.posts("/c4r/apply")
    assert len(posts) == 1 and posts[0]["office"] == ["den"]
