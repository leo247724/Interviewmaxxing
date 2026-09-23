"""C4R2 regressions in real headless Chromium.

1. A page listing several applications (div cards, articles with nested status
   lists) must never tie one application's accepted status to another record's
   job identity; ambiguous boundaries are UNKNOWN.
2. Consent/attestation wording in a checkbox group's legend or in a text field's
   help text makes the question explicit-answer-only, before identity or fact
   classification; ordinary factual controls keep their types.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from interviewmaxxing_browser import ConfirmationTie, PlaywrightSessionFactory
from interviewmaxxing_core import (
    AnswerSource,
    BrowserOptions,
    ControlType,
    Provenance,
    SemanticType,
    SubmissionOutcome,
)

TIE = ConfirmationTie(external_job_id="ABC-123", job_title="Widget Engineer")


@dataclass
class Site:
    pages: Mapping[str, tuple[int, str]]
    requests: list[tuple[str, str, dict[str, list[str]]]] = field(default_factory=list)

    def posts(self) -> list[dict[str, list[str]]]:
        return [body for method, _, body in self.requests if method == "POST"]


def _html(body: str) -> str:
    return f"<!doctype html><html><head><title>Careers</title></head><body>{body}</body></html>"


def _run(kit: SimpleNamespace, options: BrowserOptions, origin: str, site: Site,
         steps: Callable[[Any], Awaitable[Any]]) -> Any:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory(settle_timeout_s=5).start(options)

        async def handler(route: Any) -> None:
            request = route.request
            split = urlsplit(request.url)
            body = (parse_qs(request.post_data or "", keep_blank_values=True)
                    if request.method == "POST" else parse_qs(split.query))
            site.requests.append((request.method, split.path, body))
            entry = site.pages.get(f"{request.method} {split.path}", site.pages.get(split.path))
            status, html = entry or (404, _html("<h1>Not found</h1>"))
            await route.fulfill(status=status, content_type="text/html", body=html)

        await browser.page.route(f"{origin}/c4r2/**", handler)
        try:
            return await steps(browser)
        finally:
            await browser.close()

    return kit.run(scenario())


def _reconcile(kit: SimpleNamespace, server: Any, options: BrowserOptions, body: str) -> Any:
    site = Site({"/c4r2/portal": (200, _html(body))})
    return _run(kit, options, server.origin, site,
                lambda b: b.reconcile(server.url("/c4r2/portal"), tie=TIE))


# --- 1. records ----------------------------------------------------------------------

DIV_CARDS = """<h1>My applications</h1>
<div class="card"><h3>Widget Engineer</h3><p>Job ID ABC-123</p><p>Status: Draft</p></div>
<div class="card"><h3>Graphic Designer</h3><p>Application submitted</p><p>Reference: APP-9876</p></div>"""

ARTICLES = """<h1>My applications</h1>
<article><h2>Widget Engineer (Job ID ABC-123)</h2><ul><li>Status: Draft</li></ul></article>
<article><h2>Graphic Designer</h2><ul><li>Application submitted</li><li>Reference: APP-9876</li></ul></article>"""


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(DIV_CARDS, id="div-cards"),
        pytest.param(ARTICLES, id="articles-with-nested-status-lists"),
        # One record showing both a draft and a submitted status is ambiguous.
        pytest.param("""<h1>My applications</h1>
<div class="card"><h3>Widget Engineer (Job ID ABC-123)</h3><p>Draft saved</p><p>Application submitted</p></div>
<div class="card"><h3>Graphic Designer</h3><p>Status: In review</p></div>""", id="conflicting-statuses-in-one-record"),
        # A page-level heading cannot confirm one of several listed applications.
        pytest.param("""<h1>Application submitted</h1><p>Widget Engineer · Job ID ABC-123</p>
<div class="card"><h3>Widget Engineer</h3><p>Status: Draft</p></div>
<div class="card"><h3>Graphic Designer</h3><p>Status: Submitted</p></div>""", id="page-heading-over-records"),
    ],
)
def test_1_status_of_another_record_never_confirms_the_target(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, body: str
) -> None:
    observation = _reconcile(kit, server, options, body)
    assert observation.outcome is SubmissionOutcome.UNKNOWN, observation.signals


@pytest.mark.parametrize(
    ("body", "reference"),
    [
        pytest.param("""<h1>My applications</h1>
<div class="card"><h3>Graphic Designer</h3><p>Status: Draft</p></div>
<div class="card"><h3>Widget Engineer</h3><p>Job ID ABC-123</p><p>Application submitted</p><p>Reference: APP-1111</p></div>""",
                     "APP-1111", id="div-cards"),
        pytest.param("""<h1>My applications</h1>
<article><h2>Graphic Designer</h2><ul><li>Status: Draft</li></ul></article>
<article><h2>Widget Engineer (Job ID ABC-123)</h2><ul><li>Application submitted</li><li>Reference: APP-2222</li></ul></article>""",
                     "APP-2222", id="articles-with-nested-status-lists"),
    ],
)
def test_1_status_inside_the_target_record_confirms_it(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, body: str, reference: str
) -> None:
    observation = _reconcile(kit, server, options, body)
    assert observation.outcome is SubmissionOutcome.ACCEPTED, observation.signals
    assert observation.confirmation_reference == reference


def test_1_fresh_reference_in_another_card_after_submit_is_not_ours(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    form = _html("""<h1>Widget Engineer</h1><p>Job ID ABC-123</p>
<form method="post" action="/c4r2/apply">
  <label for="email">Email</label><input id="email" name="email" type="email" required>
  <button type="submit">Submit application</button></form>""")
    site = Site({"GET /c4r2/form": (200, form), "POST /c4r2/apply": (200, _html(DIV_CARDS))})

    async def steps(browser: Any) -> Any:
        page = (await browser.open(server.url("/c4r2/form"))).form
        await browser.fill(page, kit.build(page, {"email": kit.CORE["email"]}).packet)
        await browser.submit()
        return await browser.confirm()

    observation = _run(kit, options, server.origin, site, steps)
    assert observation.outcome is SubmissionOutcome.UNKNOWN, observation.signals
    assert len(site.posts()) == 1


# --- 2. consent groups and signature text ----------------------------------------------

STATEMENT_FORM = _html("""<h1>Widget Engineer</h1><p>Job ID ABC-123</p>
<form method="post" action="/c4r2/apply">
  <label for="full_name">Full name</label><input id="full_name" name="full_name" required>
  <fieldset><legend>I consent to the following uses of my application data</legend>
    <input type="checkbox" id="u1" name="data_uses" value="talent_pool"><label for="u1">Keep me in the talent pool</label>
    <input type="checkbox" id="u2" name="data_uses" value="other_roles"><label for="u2">Consider me for other roles</label>
  </fieldset>
  <fieldset><legend>Which work arrangements would you consider?</legend>
    <input type="checkbox" id="w1" name="arrangements" value="remote"><label for="w1">Remote</label>
    <input type="checkbox" id="w2" name="arrangements" value="hybrid"><label for="w2">Hybrid</label>
  </fieldset>
  <label for="signature">Full name</label>
  <input id="signature" name="signature" required aria-describedby="sig-help">
  <p id="sig-help">By typing your name, you certify all information is true and complete.</p>
  <button type="submit">Submit application</button>
</form>""")

ACCEPTED = _html("<h1>Application submitted</h1><p>Widget Engineer · Job ID ABC-123</p>")


def test_2_statement_questions_are_explicit_and_factual_controls_are_not(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    site = Site({"GET /c4r2/form": (200, STATEMENT_FORM), "POST /c4r2/apply": (200, ACCEPTED)})
    fact = Provenance(source=AnswerSource.CANDIDATE_FACT, reference_ids=["fact.arrangements"])
    profile = Provenance(source=AnswerSource.PROFILE_IDENTITY)
    name = kit.CANDIDATE["identity"]["first_name"] + " " + kit.CANDIDATE["identity"]["last_name"]

    async def steps(browser: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        form = (await browser.open(server.url("/c4r2/form"))).form
        out["form"] = form
        # The consent group answered from a fact, mislabelled as an ordinary multi-choice.
        consent_from_fact = kit.build(
            form,
            {"full_name": name, "data_uses": ["Keep me in the talent pool"], "signature": name},
            source_overrides={"data_uses": fact},
            semantic_overrides={"data_uses": SemanticType.CUSTOM_MULTISELECT},
        )
        with pytest.raises(ValueError, match="CONSENT"):
            await browser.fill(form, consent_from_fact.packet)
        # The signature box autofilled from the profile, mislabelled as a name.
        signature_from_profile = kit.build(
            form,
            {"full_name": name, "signature": name},
            source_overrides={"signature": profile},
            semantic_overrides={"signature": SemanticType.FULL_NAME},
        )
        with pytest.raises(ValueError, match="ATTESTATION"):
            await browser.fill(form, signature_from_profile.packet)
        out["untouched"] = await browser.page.input_value("#signature")
        # The user's own answers are accepted; the ordinary group may come from a fact.
        explicit = kit.build(
            form,
            {"full_name": name, "data_uses": ["Keep me in the talent pool"],
             "arrangements": ["Remote"], "signature": name},
            source_overrides={"arrangements": fact},
        )
        assert explicit.packet.problems_against(form) == []
        out["fill"] = await browser.fill(form, explicit.packet)
        await browser.submit()
        out["observation"] = await browser.confirm()
        return out

    out = _run(kit, options, server.origin, site, steps)
    form = out["form"]
    assert form.field("data_uses").control_type is ControlType.CHECKBOX_GROUP
    assert form.field("data_uses").semantic_type is SemanticType.CONSENT
    assert form.field("signature").semantic_type is SemanticType.ATTESTATION
    assert "certify" in (form.field("signature").help_text or "")
    assert form.field("full_name").semantic_type is SemanticType.FULL_NAME
    assert form.field("arrangements").semantic_type is SemanticType.CUSTOM_MULTISELECT
    assert out["untouched"] == ""
    assert out["fill"].ok and out["observation"].outcome is SubmissionOutcome.ACCEPTED
    (post,) = site.posts()
    assert post["data_uses"] == ["talent_pool"] and post["arrangements"] == ["remote"]
    assert post["signature"] == [name] and post["full_name"] == [name]


# --- C4R3: record boundaries that are mixed, nested or absent -----------------------------

MIXED_TAGS = """<h1>My applications</h1>
<div><h2>Widget Engineer</h2><p>Job ID ABC-123</p><p>Draft</p></div>
<article><h2>Graphic Designer</h2><p>Application submitted.</p><p>Reference: APP-9876</p></article>"""

MIXED_NESTED = """<h1>My applications</h1>
<div><h2>Widget Engineer</h2><p>Job ID ABC-123</p><ul><li>Status: Draft</li></ul></div>
<article><h2>Graphic Designer</h2><ul><li>Application submitted</li><li>Reference: APP-9876</li></ul></article>"""

NO_WRAPPERS = """<h1>My applications</h1>
<h2>Widget Engineer</h2><p>Job ID ABC-123</p><p>Draft</p>
<h2>Graphic Designer</h2><p>Application submitted.</p><p>Reference: APP-9876</p>"""

UNKNOWN_STATUS = """<h1>My applications</h1>
<div><h2>Widget Engineer</h2><p>Job ID ABC-123</p><p>Status: Queued for screening</p></div>
<section><h2>Graphic Designer</h2><p>Application submitted.</p><p>Reference: APP-9876</p></section>"""

TWO_IDS_FLAT = """<h1>Application submitted</h1>
<p>Job ID ABC-123</p><p>Job ID XYZ-999</p><p>Reference: APP-9876</p>"""


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(MIXED_TAGS, id="div-next-to-article"),
        pytest.param(MIXED_NESTED, id="mixed-tags-with-nested-lists"),
        pytest.param(NO_WRAPPERS, id="no-record-wrappers-at-all"),
        pytest.param(UNKNOWN_STATUS, id="target-has-unrecognized-status"),
        pytest.param(TWO_IDS_FLAT, id="two-job-ids-without-boundaries"),
    ],
)
def test_3_unestablished_or_mixed_boundaries_never_mix_records(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, body: str
) -> None:
    observation = _reconcile(kit, server, options, body)
    assert observation.outcome is SubmissionOutcome.UNKNOWN, observation.signals


@pytest.mark.parametrize(
    ("body", "reference"),
    [
        pytest.param("""<h1>My applications</h1>
<div><h2>Graphic Designer</h2><p>Draft</p></div>
<article><h2>Widget Engineer</h2><p>Job ID ABC-123</p><p>Application submitted.</p><p>Reference: APP-3333</p></article>""",
                     "APP-3333", id="target-in-article-beside-div"),
        pytest.param("""<h1>My applications</h1>
<section><h2>Graphic Designer</h2><ul><li>Status: Draft</li></ul></section>
<div><h2>Widget Engineer (Job ID ABC-123)</h2><ul><li>Application submitted</li><li>Reference: APP-4444</li></ul></div>""",
                     "APP-4444", id="target-in-div-with-nested-list"),
        pytest.param("<h1>Application submitted</h1><p>Widget Engineer · Job ID ABC-123</p><p>Reference: APP-5555</p>",
                     "APP-5555", id="single-record-page-still-confirms"),
    ],
)
def test_3_target_record_still_confirms_across_mixed_tags(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, body: str, reference: str
) -> None:
    observation = _reconcile(kit, server, options, body)
    assert observation.outcome is SubmissionOutcome.ACCEPTED, observation.signals
    assert observation.confirmation_reference == reference
