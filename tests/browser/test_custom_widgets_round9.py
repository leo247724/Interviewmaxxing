"""Round 9: a choice question is never a profile URL, and "how did you hear about" is the
referral question on any control (Ashby, Base Power Company; fictional data, headless
Chromium, nothing submitted).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.semantics import classify
from interviewmaxxing_core import BrowserOptions, ControlType, PageKind, SemanticType

QUESTION = "How did you hear about Base Power Company?"
OPTIONS = ["Company website", "LinkedIn", "Referral", "Other"]
PATH = "3f1c2b7a-0000-4000-8000-00000000b901"


def _ashby_group(kind: str) -> str:
    """Ashby's checkbox (or radio) group as it renders live: a fieldset titled by a
    <label for="<field path>"> that labels nothing, each option named after its own text."""
    items = "".join(
        f'<div class="ashby-application-form-input-{kind}-group-option"><span><input type="{kind}" '
        f'id="{PATH}-labeled-{kind}-{i}" name="{o}"></span><label for="{PATH}-labeled-{kind}-{i}">{o}</label></div>'
        for i, o in enumerate(OPTIONS))
    return (f'<div id="form"><div class="ashby-application-form-field-entry"><label for="_systemfield_name">Name</label>'
            '<input id="_systemfield_name" name="_systemfield_name" required></div>'
            f'<fieldset class="ashby-application-form-input-{kind}-group"><label class="ashby-application-form-question-title" '
            f'for="{PATH}">{QUESTION}</label>{items}</fieldset>'
            '<button type="button">Submit Application</button></div>')


def _inspect(options: BrowserOptions, html: str) -> Any:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.page.route("https://example.test/apply", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8", body=f"<title>Apply</title><h1>Fictional role</h1>{html}"))
            return await browser.open("https://example.test/apply")
        finally:
            await browser.close()

    page = asyncio.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM, page.message
    return page.form


@pytest.mark.parametrize(("kind", "control"), [("checkbox", ControlType.CHECKBOX_GROUP), ("radio", ControlType.RADIO)])
def test_the_live_referral_group_is_the_referral_question(options: BrowserOptions, kind: str, control: ControlType) -> None:
    """Base Power Company: the group's first option is "Company website" and Ashby names each
    option after its text. It was typed WEBSITE (from that name), so the resolver tried to
    copy the candidate's website URL into a choice and held."""
    form = _inspect(options, _ashby_group(kind))
    field = form.field(PATH)
    assert (field.label, field.control_type, [o.label for o in field.options or []]) == (QUESTION, control, OPTIONS)
    assert field.semantic_type is SemanticType.REFERRAL_SOURCE


@pytest.mark.parametrize(("label", "control", "name", "input_type", "expected"), [
    # "How did you hear about" is the referral question on any control.
    (QUESTION, ControlType.CHECKBOX_GROUP, "Company website", None, SemanticType.REFERRAL_SOURCE),
    (QUESTION, ControlType.SELECT, "", None, SemanticType.REFERRAL_SOURCE),
    (QUESTION, ControlType.RADIO, "", None, SemanticType.REFERRAL_SOURCE),
    (QUESTION, ControlType.TEXT, "", None, SemanticType.REFERRAL_SOURCE),
    ("Where did you first hear about Sanity?", ControlType.SELECT, "", None, SemanticType.REFERRAL_SOURCE),
    ("How did you find out about this role?", ControlType.MULTISELECT, "", None, SemanticType.REFERRAL_SOURCE),
    # A profile URL type is typed into one text input, never a choice's type.
    ("Which of these do you use?", ControlType.CHECKBOX_GROUP, "Company website", None, SemanticType.CUSTOM_MULTISELECT),
    ("Do you have a LinkedIn profile?", ControlType.RADIO, "", None, SemanticType.CUSTOM_SELECT),
    ("Portfolio review format", ControlType.SELECT, "", None, SemanticType.CUSTOM_SELECT),
    ("GitHub", ControlType.CHECKBOX, "", None, SemanticType.CUSTOM_BOOLEAN),
    # Single text inputs keep them.
    ("LinkedIn Profile", ControlType.TEXT, "", None, SemanticType.LINKEDIN),
    ("GitHub URL", ControlType.TEXT, "", "url", SemanticType.GITHUB),
    ("Website/Portfolio/Writing Sample", ControlType.TEXT, "", None, SemanticType.WEBSITE),
    ("Personal site", ControlType.TEXT, "", "url", SemanticType.WEBSITE),
])
def test_profile_urls_are_text_answers_and_referral_wording_wins(
    label: str, control: ControlType, name: str, input_type: str | None, expected: SemanticType
) -> None:
    assert classify(label=label, name=name, input_type=input_type, control_type=control) is expected
