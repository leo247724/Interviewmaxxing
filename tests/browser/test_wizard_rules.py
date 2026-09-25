"""Pure rules for dialog wizards, embedded application frames, resume pickers and entry
wording, checked on synthetic ``DomSnapshot`` objects (no browser, no server).

Everything is fictional (Brambleway Analytics, candidate Avery Quill). ATS hosts appear
only as the URL shapes the frame rules recognize.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from interviewmaxxing_browser.normalize import (
    DIALOG_FORM_INDEX,
    PageModel,
    _classify_buttons,
    application_dialog,
    build_page,
)
from interviewmaxxing_browser.runtime import _same_document_link, _shows_same, _still_on
from interviewmaxxing_browser.signals import APPLY_ENTRY, APPLY_LINK, ButtonIntent, button_intent
from interviewmaxxing_browser.snapshot import (
    DomButton,
    DomControl,
    DomDialog,
    DomFile,
    DomForm,
    DomFrame,
    DomHeading,
    DomLink,
    DomMeta,
    DomProgress,
    DomSnapshot,
    DomStep,
)
from interviewmaxxing_browser.wizard import (
    ApplicationFrame,
    ResumeChoice,
    application_frame,
    choose_resume,
    document_name,
    named_choice,
    progress_step,
)
from interviewmaxxing_core import (
    ApplicationField,
    ApplicationForm,
    ControlType,
    PageKind,
    SemanticType,
)

COMPANY = "Brambleway Analytics"
PAGE = "https://jobs.brambleway.example/jobs/view/4007130/"
CAREERS = "https://careers.brambleway.example/jobs/data-analyst"
MODAL = "#easy-apply-modal"
PINNED = "resume_avery_quill.pdf"
SAVED = "Avery_Quill_Resume_2025.pdf"
OTHER_CV = "AQ_CV_marketing.docx"

# --- synthetic snapshot builders ---------------------------------------------------------


def control(selector: str, label: str, *, input_type: str = "text", name: str = "",
            dom_id: str = "", form: int = -1, dialog: int = -1, **state: Any) -> DomControl:
    """One visible, enabled native control as ``inspect.js`` reports it."""
    tag = input_type if input_type in ("select", "textarea") else "input"
    return DomControl.model_validate({
        "kind": "native", "tag": tag, "type": input_type, "name": name, "id": dom_id,
        "selector": selector, "role": "", "autocomplete_list": False, "label": label,
        "label_source": "label", "described": [], "error_message": "", "legend": None,
        "legend_selector": None, "legend_described": [], "group_label": None,
        "group_described": [], "adjacent": [], "adjacent_errors": [], "label_selector": None,
        "required": False, "disabled": False, "visible": True, "label_visible": True,
        "readonly": False, "value": "", "checked": False, "files": [], "placeholder": "",
        "autocomplete": "", "accept": "", "max_length": None, "multiple": False, "options": [],
        "invalid": False, "image_alts": [], "form_index": form, "has_value": False,
        "dialog_index": dialog, **state,
    })


def button(selector: str, text: str, *, form: int = -1, dialog: int = -1, submits: bool = False,
           kind: str = "", toggle: bool = False) -> DomButton:
    return DomButton(
        text=text, type=kind or ("submit" if submits else "button"), selector=selector,
        disabled=False, form_index=form, submits_form=submits, form_no_validate=False,
        effective_method="post" if submits else "", effective_action=PAGE if submits else "",
        dialog_index=dialog, toggle=toggle,
    )


def dom_form(index: int, selector: str, method: str = "post") -> DomForm:
    return DomForm(index=index, selector=selector, method=method, action=PAGE, no_validate=False)


def dialog(label: str, *, index: int = 0, selector: str = MODAL, modal: bool = True,
           visible: bool = True, step: DomStep | None = None) -> DomDialog:
    return DomDialog(index=index, selector=selector, label=label, modal=modal, visible=visible,
                     step=step)


def bar(value: float, maximum: float = 100, *, dialog: int = -1) -> DomProgress:
    return DomProgress(value=value, max=maximum, dialog_index=dialog)


def frame(src: str, *, frame_id: str = "", visible: bool = True) -> DomFrame:
    return DomFrame(id=frame_id, src=src, title="Application", visible=visible)


def snapshot(*, url: str = PAGE, controls: Sequence[DomControl] = (),
             buttons: Sequence[DomButton] = (), links: Sequence[DomLink] = (),
             forms: Sequence[DomForm] = (), dialogs: Sequence[DomDialog] = (),
             progress: Sequence[DomProgress] = (), frames: Sequence[DomFrame] = (),
             step: DomStep | None = None, password_visible: bool = False) -> DomSnapshot:
    return DomSnapshot(
        url=url, title=f"Data Analyst | {COMPANY}",
        headings=[DomHeading(level=1, text="Data Analyst")], regions=[],
        body_text=f"Data Analyst at {COMPANY}. Denver, CO (hybrid).", record_members=[],
        ld_json=[], meta=DomMeta(og_site_name=COMPANY, og_title="Data Analyst"),
        forms=list(forms), controls=list(controls), buttons=list(buttons), links=list(links),
        step=step, password_visible=password_visible, captcha_frames=[], captcha_tokens=[],
        captcha_widget=False, document=f"1758700000000 {url}", dialogs=list(dialogs),
        progress=list(progress), frames=list(frames),
    )


APPLY_NOW = DomLink(text="Apply now", href=f"{PAGE}apply", selector="#apply-now")
EASY_APPLY_LINK = DomLink(text="Easy Apply", href=f"{PAGE}apply/?openSDUIApplyFlow=true",
                          selector="#jobs-apply-link")
WIZARD_FIELDS = (
    control("#ea-first-name", "First name", name="firstName", form=0, dialog=0, required=True),
    control("#ea-last-name", "Last name", name="lastName", form=0, dialog=0, required=True),
    control("#ea-phone", "Mobile phone number", input_type="tel", name="phoneNumber", dialog=0),
)
DISMISS = button("#ea-dismiss", "Dismiss", dialog=0)
NEXT = button("#ea-next", "Next", form=0, dialog=0, submits=True)
BEHIND = (
    control("#alert-email", "Email for job alerts", input_type="email", name="alertEmail"),
    control("#alert-city", "Preferred city", name="alertCity"),
    control("#keywords", "Search jobs", name="keywords", form=1),
)


def easy_apply_page(*, fields: Sequence[DomControl] = WIZARD_FIELDS,
                    actions: Sequence[DomButton] = (DISMISS, NEXT),
                    bars: Sequence[DomProgress] = (), dialog_step: DomStep | None = None,
                    page_step: DomStep | None = None) -> DomSnapshot:
    """A LinkedIn-like job view with the Easy Apply wizard open over it. Behind it: two
    form-less fields, a search form and an "Easy Apply" link."""
    return snapshot(
        controls=[*BEHIND, *fields],
        buttons=[button("#search-go", "Search", form=1, submits=True), *actions],
        links=[EASY_APPLY_LINK],
        forms=[dom_form(0, f"{MODAL} form"), dom_form(1, "#jobs-search", "get")],
        dialogs=[dialog(f"Apply to {COMPANY}", step=dialog_step)],
        progress=bars, step=page_step,
    )


def resume_step(cards: Sequence[tuple[str, bool]] = (), *, shared_name: str = "",
                attached: bool = False) -> DomSnapshot:
    """The wizard's resume step: an upload control and one radio card per saved resume,
    labelled the way LinkedIn labels them ("Deselect resume X" when checked)."""
    upload = control(
        "#ea-resume", "Upload resume", input_type="file", name="resume", dialog=0,
        required=True, accept=".pdf,.doc,.docx",
        files=[DomFile(name=PINNED, size=1024)] if attached else [],
    )
    radios = [
        control(f"#resume-card-{i}", f"{'Deselect' if checked else 'Select'} resume {name}",
                input_type="radio", name=shared_name, dom_id=f"resume-card-{i}", dialog=0,
                checked=checked, label_selector=f"label[for=resume-card-{i}]")
        for i, (name, checked) in enumerate(cards, 1)
    ]
    downloads = [button(f"#download-{i}", f"Download resume {name}", dialog=0)
                 for i, (name, _) in enumerate(cards, 1)]
    more = [button("#show-more", f"Show {len(cards) - 1} more resumes", dialog=0)] if len(cards) > 1 else []
    return snapshot(
        controls=[upload, *radios], buttons=[DISMISS, *downloads, *more, NEXT],
        forms=[dom_form(0, f"{MODAL} form")], dialogs=[dialog(f"Apply to {COMPANY}")],
    )


def application(snap: DomSnapshot, **kwargs: Any) -> tuple[PageModel, ApplicationForm]:
    """``build_page`` of a page that must be an application form, and its form."""
    model = build_page(snap, **kwargs)
    assert model.inspection.kind is PageKind.APPLICATION_FORM and model.form is not None
    return model, model.form


# --- 1. progress ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bars", "expected"),
    [
        pytest.param([bar(33)], 33, id="one-bar"),
        pytest.param([bar(1, 3)], 33, id="value-over-max"),
        pytest.param([bar(2, 3)], 67, id="rounded"),
        pytest.param([], None, id="none"),
        pytest.param([bar(33), bar(67)], None, id="disagreeing"),
        pytest.param([bar(33), bar(33)], 33, id="identical"),
        pytest.param([bar(1, 3), bar(33)], 33, id="same-percent-other-scale"),
        pytest.param([bar(150)], 100, id="clamped-high"),
        pytest.param([bar(-5)], 0, id="clamped-low"),
        pytest.param([bar(5, 0)], None, id="no-maximum"),
    ],
)
def test_progress_step(bars: list[DomProgress], expected: int | None) -> None:
    """A wizard step's identity is its one percentage (0 to 100), never a guess."""
    assert progress_step(bars) == expected


# --- 2. embedded application frames ----------------------------------------------------------

GREENHOUSE = "https://job-boards.greenhouse.io/embed/job_app?for=brambleway&token=4007131"


@pytest.mark.parametrize(
    ("src", "ats", "visible"),
    [
        (GREENHOUSE, "greenhouse", False),
        ("https://boards.greenhouse.io/embed/job_app?for=brambleway&token=4007131", "greenhouse", True),
        ("https://jobs.lever.co/acme/5a1c2b3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d/apply", "lever", True),
        ("https://careers-acme.icims.com/jobs/1234/title/job?in_iframe=1", "icims", True),
        ("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Denver-CO/Data-Analyst_R-4012",
         "workday", True),
        ("https://jobs.jobvite.com/acme/job/oQ4r1fw2/apply", "jobvite", True),
    ],
)
def test_application_frame_on_a_known_ats_host(src: str, ats: str, visible: bool) -> None:
    """An application page on a known ATS host counts, visible or hidden (a careers page
    may keep it in a hidden "Application" tab); its visibility is reported."""
    assert application_frame([frame(src, visible=visible)], CAREERS) == ApplicationFrame(src, ats, visible)


@pytest.mark.parametrize(
    "src",
    [
        pytest.param("https://www.google.com/recaptcha/api2/anchor?k=6LfictionalKey", id="recaptcha"),
        pytest.param("https://www.youtube-nocookie.com/embed/Brmbl3way01", id="youtube"),
        pytest.param("https://www.google.com/maps/embed?pb=!1m18!1m12", id="maps"),
        pytest.param("about:blank", id="about-blank"),
        pytest.param("", id="srcdoc"),
        pytest.param("https://boards.greenhouse.io/embed/job_board?for=brambleway", id="greenhouse-board"),
        pytest.param("https://job-boards.greenhouse.io/brambleway", id="greenhouse-listing"),
        pytest.param("https://jobs.lever.co/acme", id="lever-listing"),
    ],
)
def test_other_frames_are_never_application_pages(src: str) -> None:
    """Arbitrary embeds and non-application paths of an ATS host are never followed."""
    assert application_frame([frame(src)], CAREERS) is None


def test_two_application_frames_are_ambiguous_but_one_src_twice_is_one() -> None:
    """Several different application pages give none; the same src repeated is one page,
    visible when any copy is."""
    lever = "https://jobs.lever.co/acme/5a1c2b3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d/apply"
    assert application_frame([frame(GREENHOUSE), frame(lever)], CAREERS) is None
    for frames in ([frame(GREENHOUSE, visible=False), frame(GREENHOUSE)],
                   [frame(GREENHOUSE), frame(GREENHOUSE, visible=False)]):
        assert application_frame(frames, CAREERS) == ApplicationFrame(GREENHOUSE, "greenhouse", True)


def test_ats_embed_frame_id_counts_on_the_pages_own_origin_only() -> None:
    """``grnhse_iframe``/``icims_content_iframe`` with the application path count on the
    page's own origin ("embedded"); on another non-ATS origin, without the id, or with
    another path they do not."""
    own = "https://careers.brambleway.example/embed/job_app?token=4007131"
    foreign = "https://widgets.example.net/embed/job_app?token=4007131"
    icims = "https://careers.brambleway.example/jobs/1234/data-analyst/job?in_iframe=1"
    assert application_frame([frame(own, frame_id="grnhse_iframe")], CAREERS) == ApplicationFrame(
        own, "embedded", True)
    assert application_frame([frame(icims, frame_id="icims_content_iframe")], CAREERS) == ApplicationFrame(
        icims, "embedded", True)
    assert application_frame([frame(foreign, frame_id="grnhse_iframe")], CAREERS) is None
    assert application_frame([frame(own)], CAREERS) is None
    board = "https://careers.brambleway.example/embed/job_board?for=brambleway"
    assert application_frame([frame(board, frame_id="grnhse_iframe")], CAREERS) is None


# --- 3./4. resume pickers ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"Select resume {SAVED}", SAVED),
        (f"Deselect resume {PINNED}", PINNED),
        ("Avery Quill CV.docx 290 KB · Last used on 8/12/2026", "Avery Quill CV.docx"),
        ("Upload a new resume", None),
        ("Selected", None),
    ],
)
def test_document_name(text: str, expected: str | None) -> None:
    """The file name a resume card or its label shows, without the choice verb."""
    assert document_name(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Select resume_final.pdf", "resume_final.pdf"), ("Use CV.docx", "CV.docx")],
)
def test_document_name_keeps_a_file_name_that_starts_with_the_noun(text: str, expected: str) -> None:
    """A label of just verb and file ("Select resume_final.pdf") keeps the whole name."""
    assert document_name(text) == expected


def card(name: str, n: int, *, checked: bool = False) -> ResumeChoice:
    return ResumeChoice(name=name, selector=f"#resume-card-{n}", checked=checked)


def test_exact_name_wins_over_a_preselected_other_card() -> None:
    """The card named like the pinned file is chosen even when another is selected."""
    choices = [card(OTHER_CV, 1, checked=True), card(PINNED, 2)]
    chosen, reason = choose_resume(choices, PINNED)
    assert chosen == choices[1] and "is the pinned file" in reason
    assert named_choice(choices, PINNED) == choices[1]


@pytest.mark.parametrize(
    ("shown", "pinned", "same"),
    [
        (PINNED, "RESUME_Avery_Quill.PDF", True),
        ("resume_avery_quill", PINNED, True),
        (PINNED, "resume_avery_quill", True),
        ("resume_avery_quill.docx", PINNED, False),
        ("resume_avery", PINNED, False),
    ],
)
def test_names_match_ignoring_case_and_a_dropped_extension(shown: str, pinned: str, same: bool) -> None:
    """Case never matters; an extension missing on one side still matches, a different
    extension or name never does."""
    choices = [card(shown, 1), card(SAVED, 2)]
    assert (named_choice(choices, pinned) == choices[0]) is same


def test_only_card_is_used_when_none_is_named_like_the_pinned_file() -> None:
    only = [card(OTHER_CV, 1)]
    assert choose_resume(only, PINNED) == (only[0], f"the only resume on the site, {OTHER_CV!r}")
    assert named_choice(only, PINNED) is None


def test_several_cards_none_named_like_the_pinned_file_choose_nothing() -> None:
    """The reason names every card and the pinned file."""
    chosen, reason = choose_resume([card(SAVED, 1), card(OTHER_CV, 2)], PINNED)
    assert chosen is None
    assert all(repr(name) in reason for name in (SAVED, OTHER_CV, PINNED)) and "2 resumes" in reason


def test_duplicates_named_like_the_pinned_file_or_no_cards_choose_nothing() -> None:
    duplicates = [card(PINNED, 1), card(PINNED.upper(), 2, checked=True)]
    chosen, reason = choose_resume(duplicates, PINNED)
    assert chosen is None and "several resumes" in reason
    assert named_choice(duplicates, PINNED) is None
    assert choose_resume([], PINNED) == (None, "the site shows no previously uploaded resume")
    assert named_choice([], PINNED) is None


# --- 5. entry and step wording -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["Easy Apply", "Quick apply", "Apply without an Account", "Continue as guest", "I'm interested",
     "Start your application"],
)
def test_entry_wording_is_apply_wording(text: str) -> None:
    """Entry wording leads to an application and only ever opens one."""
    assert APPLY_LINK.search(text) and APPLY_ENTRY.search(text)


@pytest.mark.parametrize("text", ["Apply", "Apply now", "Apply for this job"])
def test_plain_apply_is_apply_wording_but_not_an_entry(text: str) -> None:
    """Bare "Apply" may also submit a form, so it is not entry wording."""
    assert APPLY_LINK.search(text) and not APPLY_ENTRY.search(text)


@pytest.mark.parametrize(
    "text", ["Apply with LinkedIn", "Apply using Indeed", "Use my Indeed resume", "Submit application"]
)
def test_third_party_and_submit_wording_is_not_apply_wording(text: str) -> None:
    assert not APPLY_LINK.search(text)


UTILITY_WORDING = ["Save", "Dismiss", f"Download resume {SAVED}", "Show 3 more resumes", "Paste resume"]


@pytest.mark.parametrize("text", UTILITY_WORDING)
def test_utility_wording_is_never_a_step_action(text: str) -> None:
    assert button_intent(text, submits_form=False) is ButtonIntent.OTHER


@pytest.mark.parametrize(
    "text",
    [
        *(t for t in UTILITY_WORDING if not t.startswith("Show")),
        "Show 3 more resumes",
    ],
)
def test_utility_wording_is_other_even_on_a_submitting_button(text: str) -> None:
    """The wording itself, not the no-submit fallback, makes these OTHER."""
    assert button_intent(text, submits_form=True) is ButtonIntent.OTHER


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Save and continue", ButtonIntent.NEXT),
        ("Review", ButtonIntent.NEXT),
        ("Continue to next step", ButtonIntent.NEXT),
        ("Submit application", ButtonIntent.SUBMIT),
        ("Save & submit", ButtonIntent.SUBMIT),
    ],
)
def test_step_actions(text: str, expected: ButtonIntent) -> None:
    assert button_intent(text, submits_form=True) is expected


# --- 6a-c. the dialog is the form --------------------------------------------------------------


def test_modal_wizard_is_the_form_and_the_page_behind_is_ignored() -> None:
    """Only the dialog's own fields and buttons count, under one synthetic form whose
    selector is the dialog; the page's fields, search form and link are ignored."""
    model, form = application(easy_apply_page())
    assert model.dialog_index == 0
    assert [f.id for f in form.fields] == ["firstName", "lastName", "phoneNumber"]
    assert form.next_selector == "#ea-next" and form.submit_selector is None
    assert form.is_final_step is False
    assert (model.form_index, model.form_selector, model.form_method) == (DIALOG_FORM_INDEX, MODAL, "dialog")
    assert {c.selector for c in model.snapshot.controls} == {"#ea-first-name", "#ea-last-name", "#ea-phone"}
    assert [(b.button.selector, b.intent) for b in model.buttons] == [
        ("#ea-dismiss", ButtonIntent.OTHER), ("#ea-next", ButtonIntent.NEXT)]
    assert model.apply_controls == [] and model.step_source is None and form.step == 0


def test_dialog_progress_bar_identifies_the_step() -> None:
    model, form = application(easy_apply_page(bars=[bar(33, dialog=0)]), fallback_step=4)
    assert form.step == 33 and model.step_source == "progress"


def test_dialog_step_text_wins_over_its_progress_bar() -> None:
    model, form = application(easy_apply_page(dialog_step=DomStep(current=2, total=4, source="text"),
                                              bars=[bar(33, dialog=0)]))
    assert form.step == 1 and model.step_source == "text"


def test_step_and_bars_of_the_page_behind_do_not_count() -> None:
    """A "Step 3 of 5" and a bar outside the dialog are the page's, not the wizard's."""
    page_step = DomStep(current=3, total=5, source="text")
    model, form = application(easy_apply_page(page_step=page_step, bars=[bar(80), bar(33, dialog=0)]))
    assert form.step == 33 and model.step_source == "progress"
    model, form = application(easy_apply_page(page_step=page_step, bars=[bar(80)]), fallback_step=2)
    assert form.step == 2 and model.step_source is None


def test_review_step_without_fields_is_the_final_step() -> None:
    """A fieldless dialog step with a progress bar and "Submit application" is the final
    step of the application."""
    submit = button("#ea-submit", "Submit application", form=0, dialog=0, submits=True)
    back = button("#ea-back", "Back", form=0, dialog=0)
    model, form = application(easy_apply_page(fields=[], actions=[DISMISS, back, submit],
                                              bars=[bar(100, dialog=0)]))
    assert model.dialog_index == 0 and form.fields == []
    assert form.is_final_step is True and form.submit_selector == "#ea-submit" and form.next_selector is None
    assert (form.step, model.step_source) == (100, "progress")


def test_fieldless_dialog_without_a_step_indicator_is_not_a_review_step() -> None:
    submit = button("#ea-submit", "Submit application", form=0, dialog=0, submits=True)
    assert application_dialog(easy_apply_page(fields=[], actions=[DISMISS, submit])) is None


def test_modal_application_dialog_is_preferred_then_the_last_one() -> None:
    """Of several application dialogs a modal one wins, then the last in document
    order; a hidden dialog never counts."""

    def wizard(index: int, *, modal: bool, visible: bool = True) -> tuple[DomDialog, list[DomControl], DomButton]:
        return (dialog(f"Apply to {COMPANY}", index=index, selector=f"#wizard-{index}", modal=modal,
                       visible=visible),
                [control(f"#w{index}-first", "First name", name=f"first{index}", dialog=index),
                 control(f"#w{index}-last", "Last name", name=f"last{index}", dialog=index)],
                button(f"#w{index}-next", "Next", dialog=index))

    def chosen(*wizards: tuple[DomDialog, list[DomControl], DomButton]) -> int | None:
        found = application_dialog(snapshot(dialogs=[w[0] for w in wizards],
                                            controls=[c for w in wizards for c in w[1]],
                                            buttons=[w[2] for w in wizards]))
        return None if found is None else found.index

    assert chosen(wizard(0, modal=True), wizard(1, modal=False)) == 0
    assert chosen(wizard(0, modal=True), wizard(1, modal=True)) == 1
    assert chosen(wizard(0, modal=False), wizard(1, modal=False)) == 1
    assert chosen(wizard(0, modal=False), wizard(1, modal=True, visible=False)) == 0
    assert chosen(wizard(0, modal=True, visible=False)) is None


# --- 6d. dialogs that are not the application --------------------------------------------------


def _cookie_banner() -> DomSnapshot:
    return snapshot(
        dialogs=[dialog("We value your privacy", selector="#cookie-banner")],
        buttons=[button("#accept", "Accept all", dialog=0), button("#decline", "Decline all", dialog=0)],
        links=[APPLY_NOW],
    )


def _sign_in(*, label: str = "Sign in to apply", password: bool = True,
             extra_button: str | None = None) -> DomSnapshot:
    """Two fields and "Continue": only the sign-in rules (wording in the label or a
    button, a visible password) keep it from being an application step."""
    controls = [control("#si-email", "Email", input_type="email", name="session_key", form=2, dialog=0),
                control("#si-phone", "Mobile phone number", input_type="tel", name="phone", form=2, dialog=0)]
    if password:
        controls.append(control("#si-password", "Password", input_type="password", name="session_password",
                                form=2, dialog=0))
    buttons = [button("#si-continue", "Continue", form=2, dialog=0, submits=True)]
    if extra_button:
        buttons.append(button("#si-extra", extra_button, form=2, dialog=0))
    return snapshot(dialogs=[dialog(label, selector="#sign-in-modal")], controls=controls, buttons=buttons,
                    forms=[dom_form(2, "#sign-in-modal form")], links=[APPLY_NOW], password_visible=password)


def _newsletter() -> DomSnapshot:
    return snapshot(
        dialogs=[dialog("Stay in touch", selector="#newsletter")],
        controls=[control("#nl-email", "Email address", input_type="email", name="email", form=2, dialog=0)],
        buttons=[button("#nl-submit", "Submit", form=2, dialog=0, submits=True)],
        forms=[dom_form(2, "#newsletter form")], links=[APPLY_NOW],
    )


@pytest.mark.parametrize(
    "snap",
    [
        pytest.param(_cookie_banner(), id="cookie-banner"),
        pytest.param(_sign_in(), id="sign-in"),
        pytest.param(_sign_in(password=False), id="sign-in-wording-only"),
        pytest.param(_sign_in(label="Welcome back"), id="password-only"),
        pytest.param(_sign_in(label=f"Apply to {COMPANY}", password=False, extra_button="Sign in"),
                     id="sign-in-button"),
        pytest.param(_newsletter(), id="newsletter"),
        pytest.param(easy_apply_page().model_copy(update={"dialogs": [dialog(f"Apply to {COMPANY}",
                                                                             visible=False)]}),
                     id="hidden-wizard"),
    ],
)
def test_non_application_dialogs_are_never_the_form(snap: DomSnapshot) -> None:
    """Cookie banners, sign-in/account dialogs, a one-field newsletter without a step,
    file or application wording, and hidden dialogs are not the application dialog."""
    assert application_dialog(snap) is None
    assert build_page(snap).dialog_index is None


@pytest.mark.parametrize(
    ("snap", "kind"),
    [
        pytest.param(_cookie_banner(), PageKind.JOB_DESCRIPTION, id="cookie-banner"),
        pytest.param(_sign_in(), PageKind.SIGN_IN_REQUIRED, id="sign-in"),
        pytest.param(_newsletter(), PageKind.JOB_DESCRIPTION, id="newsletter"),
    ],
)
def test_rejected_dialog_leaves_the_page_level_classification(snap: DomSnapshot, kind: PageKind) -> None:
    """The page is then classified as a page: the posting's apply link, or sign-in when a
    password shows."""
    model = build_page(snap)
    assert model.inspection.kind is kind and model.dialog_index is None
    if kind is PageKind.JOB_DESCRIPTION:
        assert "'Apply now'" in (model.inspection.message or "")


# --- 6e-f. resume cards and attaching files ----------------------------------------------------


@pytest.mark.parametrize("shared_name", ["", "resume-choice"], ids=["nameless-radios", "one-radio-group"])
def test_saved_resume_cards_fold_into_the_resume_field(shared_name: str) -> None:
    """Radio cards named like files are state of the one resume upload field, never a
    question or a control of their own."""
    model, form = application(resume_step([(SAVED, True), (PINNED, False)], shared_name=shared_name))
    [resume] = form.fields
    assert (resume.id, resume.control_type, resume.semantic_type) == ("resume", ControlType.FILE,
                                                                       SemanticType.RESUME)
    assert model.bindings["resume"].resume_choices == (
        ResumeChoice(SAVED, "#resume-card-1", "label[for=resume-card-1]", checked=True),
        ResumeChoice(PINNED, "#resume-card-2", "label[for=resume-card-2]", checked=False),
    )
    selectors = {c.selector for c in model.snapshot.controls}
    assert "#ea-resume" in selectors and not selectors & {"#resume-card-1", "#resume-card-2"}
    assert form.next_selector == "#ea-next" and model.unsupported_pending == []


def test_wizard_resume_without_cards_is_handed_to_the_person_without_file_attachment() -> None:
    model, form = application(resume_step(), attach_files=False)
    [resume] = form.fields
    binding = model.bindings["resume"]
    assert resume.control_type is ControlType.UNSUPPORTED and resume.required and resume.accept is None
    assert "Attach your resume in the browser window" in (resume.help_text or "")
    assert binding.attach_by_person and binding.control_type is ControlType.UNSUPPORTED
    assert not binding.user_completed and model.unsupported_pending == ["resume"]


def test_wizard_resume_whose_pinned_file_is_not_on_offer_is_handed_to_the_person() -> None:
    """Two cards, neither named like the pinned file: the person attaches it, and its
    name is in the wording."""
    model, form = application(resume_step([(SAVED, True), (OTHER_CV, False)]), attach_files=False,
                              attach_needed={"resume": PINNED})
    [resume] = form.fields
    assert resume.control_type is ControlType.UNSUPPORTED and resume.required
    assert f"({PINNED})" in (resume.help_text or "")
    assert len(model.bindings["resume"].resume_choices) == 2 and model.unsupported_pending == ["resume"]


@pytest.mark.parametrize("checked", [True, False])
def test_wizard_resume_with_the_pinned_card_on_offer_stays_automatic(checked: bool) -> None:
    """The card named like the pinned file can be selected, so no person is needed."""
    model, form = application(resume_step([(PINNED, checked), (SAVED, not checked)]), attach_files=False,
                              attach_needed={"resume": PINNED})
    [resume] = form.fields
    assert resume.control_type is ControlType.FILE and not model.bindings["resume"].attach_by_person
    assert model.unsupported_pending == []


def test_wizard_resume_with_one_card_stays_automatic_until_the_pinned_file_is_known() -> None:
    """Any card is usable while the pinned file is unknown, and the only card always is."""
    _, unknown = application(resume_step([(SAVED, True), (OTHER_CV, False)]), attach_files=False)
    _, only = application(resume_step([(OTHER_CV, True)]), attach_files=False,
                          attach_needed={"resume": PINNED})
    assert unknown.field("resume").control_type is ControlType.FILE
    assert only.field("resume").control_type is ControlType.FILE


@pytest.mark.parametrize(
    ("step", "needed"),
    [
        pytest.param(resume_step(attached=True), {}, id="file-attached"),
        pytest.param(resume_step([(PINNED, True), (PINNED.upper(), False)]), {"resume": PINNED},
                     id="duplicate-pinned-card-checked"),
    ],
)
def test_handed_over_resume_is_complete_once_the_person_did_it(step: DomSnapshot, needed: dict[str, str]) -> None:
    """A file in the input, or a checked card named like the pinned file, completes it."""
    model, form = application(step, attach_files=False, attach_needed=needed)
    [resume] = form.fields
    assert resume.control_type is ControlType.UNSUPPORTED and not resume.required
    assert model.bindings["resume"].user_completed and model.unsupported_pending == []


def test_page_form_file_field_stays_a_file_without_file_attachment() -> None:
    """Outside a dialog wizard (the OpenCLI page-form path) a file field without cards is
    still a FILE field."""
    snap = snapshot(
        forms=[dom_form(0, "#application")],
        controls=[control("#first", "First name", name="first_name", form=0, required=True),
                  control("#email", "Email", input_type="email", name="email", form=0, required=True),
                  control("#resume", "Resume/CV", input_type="file", name="resume", form=0, required=True)],
        buttons=[button("#submit", "Submit application", form=0, submits=True)],
    )
    model, form = application(snap, attach_files=False)
    assert model.dialog_index is None
    assert form.field("resume").control_type is ControlType.FILE
    assert not model.bindings["resume"].attach_by_person and model.unsupported_pending == []


# --- 6g. apply entry controls ------------------------------------------------------------------

ALERT_FIELDS = BEHIND[:2]
EASY_APPLY = button("#easy-apply", "Easy Apply")


def test_easy_apply_button_is_an_apply_control_beside_formless_fields() -> None:
    """Entry wording leads to the application wherever it sits: beside two fillable
    form-less fields "Easy Apply" is an apply control, where bare "Apply" submits."""
    [easy] = _classify_buttons([EASY_APPLY], {-1: 2})
    assert (easy.intent, easy.apply_control) == (ButtonIntent.OTHER, True)
    [bare] = _classify_buttons([button("#apply", "Apply")], {-1: 2})
    assert (bare.intent, bare.apply_control) == (ButtonIntent.SUBMIT, False)
    model = build_page(snapshot(controls=ALERT_FIELDS, buttons=[EASY_APPLY]))
    assert [b.selector for b in model.apply_controls] == ["#easy-apply"]


def test_easy_apply_page_with_two_formless_fields_is_a_job_description() -> None:
    """Two form-less fields with no submit or next of their own (a message box and a
    note beside the posting) do not make an application; the page's Easy Apply does."""
    model = build_page(snapshot(controls=ALERT_FIELDS, buttons=[EASY_APPLY]))
    assert model.inspection.kind is PageKind.JOB_DESCRIPTION
    assert "'Easy Apply'" in (model.inspection.message or "")


def test_easy_apply_page_is_a_job_description_naming_the_control() -> None:
    model = build_page(snapshot(controls=ALERT_FIELDS[:1], buttons=[EASY_APPLY]))
    assert model.inspection.kind is PageKind.JOB_DESCRIPTION
    assert model.inspection.message == "Job posting; apply control: 'Easy Apply' (button)."


def test_toggle_reading_easy_apply_is_not_an_apply_control() -> None:
    """A filter pill (``aria-pressed``) reading "Easy Apply" is never followed; the
    posting's own Easy Apply control is."""
    pill = button("#filter-easy-apply", "Easy Apply", toggle=True)
    [classified] = _classify_buttons([pill], {-1: 0})
    assert not classified.apply_control
    model = build_page(snapshot(buttons=[pill, EASY_APPLY]))
    assert model.inspection.kind is PageKind.JOB_DESCRIPTION
    assert [b.selector for b in model.apply_controls] == ["#easy-apply"]


def test_toggle_reading_easy_apply_never_submits_a_formless_field() -> None:
    """A filter pill is never a step action, not even the submit of a lone field."""
    pill = button("#filter-easy-apply", "Easy Apply", toggle=True)
    location = control("#jobs-location", "City, state, or zip code", name="location")
    model = build_page(snapshot(controls=[location], buttons=[pill, EASY_APPLY]))
    assert model.inspection.kind is PageKind.JOB_DESCRIPTION


def test_bare_apply_in_a_form_with_three_fields_submits_it() -> None:
    fields = [control("#first", "First name", name="first_name", form=0),
              control("#last", "Last name", name="last_name", form=0),
              control("#email", "Email", input_type="email", name="email", form=0)]
    apply = button("#apply", "Apply", form=0, submits=True)
    [classified] = _classify_buttons([apply], {0: 3})
    assert (classified.intent, classified.apply_control) == (ButtonIntent.SUBMIT, False)
    model, form = application(snapshot(forms=[dom_form(0, "#application")], controls=fields,
                                       buttons=[apply]))
    assert form.is_final_step is True and form.submit_selector == "#apply"
    assert model.apply_controls == []


# --- 6h. embedded application page -------------------------------------------------------------


@pytest.mark.parametrize("visible", [False, True])
def test_embedded_application_frame_makes_a_job_description(visible: bool) -> None:
    model = build_page(snapshot(url=CAREERS, frames=[frame(GREENHOUSE, frame_id="grnhse_iframe",
                                                           visible=visible)]))
    assert model.inspection.kind is PageKind.JOB_DESCRIPTION
    assert model.application_frame == ApplicationFrame(GREENHOUSE, "greenhouse", visible)
    message = model.inspection.message or ""
    assert f"embedded from {GREENHOUSE}" in message
    assert ("(in a hidden panel)" in message) is not visible


def test_page_with_an_application_form_ignores_frames() -> None:
    fields = [control("#first", "First name", name="first_name", form=0),
              control("#email", "Email", input_type="email", name="email", form=0)]
    snap = snapshot(url=CAREERS, forms=[dom_form(0, "#application")], controls=fields,
                    buttons=[button("#submit", "Submit application", form=0, submits=True)],
                    frames=[frame(GREENHOUSE)])
    assert application(snap)[0].application_frame is None
    wizard = easy_apply_page().model_copy(update={"frames": [frame(GREENHOUSE)]})
    assert application(wizard)[0].application_frame is None


# --- 6i. anchors as form buttons ----------------------------------------------------------------


def test_anchor_buttons_of_a_form_are_its_actions() -> None:
    """JazzHR-like: the form's only action controls are anchors ("link-button")."""
    snap = snapshot(
        forms=[dom_form(0, "#resumator-form")],
        controls=[control("#resumator-firstname-value", "First Name", name="first_name", form=0),
                  control("#resumator-lastname-value", "Last Name", name="last_name", form=0),
                  control("#resumator-email-value", "Email", input_type="email", name="email", form=0)],
        buttons=[button("#resumator-attach", "Attach resume", form=0, kind="link-button"),
                 button("#resumator-submit-value", "Submit Application", form=0, kind="link-button")],
    )
    model, form = application(snap)
    assert [b.intent for b in model.buttons] == [ButtonIntent.OTHER, ButtonIntent.SUBMIT]
    assert form.is_final_step is True and form.submit_selector == "#resumator-submit-value"
    assert form.next_selector is None


# --- 7. pre-filled values ------------------------------------------------------------------------


def text_field(semantic: SemanticType = SemanticType.CUSTOM_TEXT, input_type: str = "text",
               error: str | None = None) -> ApplicationField:
    return ApplicationField(id="q", label="Question", semantic_type=semantic,
                            control_type=ControlType.TEXT, selector="#q", input_type=input_type,
                            validation_error=error)


PHONE = text_field(SemanticType.PHONE, "tel")


@pytest.mark.parametrize(
    ("app_field", "shown", "wanted", "same"),
    [
        pytest.param(text_field(), "Boulder", "Boulder", True, id="exact"),
        pytest.param(text_field(), "Line one\r\nLine two", "Line one\nLine two", True, id="line-endings"),
        pytest.param(text_field(SemanticType.EMAIL, "email"), "Avery.Quill@Example.test",
                     "avery.quill@example.test", True, id="email-case"),
        pytest.param(text_field(input_type="email"), " AVERY.QUILL@example.test",
                     "avery.quill@example.test", True, id="email-by-input-type"),
        pytest.param(PHONE, "(303) 555-0142", "303.555.0142", True, id="phone-format"),
        pytest.param(PHONE, "3035550142", "+1 (303) 555-0142", True, id="phone-national-part"),
        pytest.param(PHONE, "+1 (303) 555-0142", "3035550142", False, id="phone-reverse"),
        pytest.param(PHONE, "555-0142", "+1 (303) 555-0142", False, id="phone-fragment"),
        pytest.param(text_field(error="Enter a valid city"), "Boulder", "Boulder", False, id="invalid"),
        pytest.param(text_field(SemanticType.PHONE, "tel", "Invalid phone number"), "3035550142",
                     "+1 (303) 555-0142", False, id="invalid-phone"),
        pytest.param(text_field(), "Denver", "Boulder", False, id="other-text"),
        pytest.param(text_field(), "boulder", "Boulder", False, id="text-case"),
    ],
)
def test_shows_same(app_field: ApplicationField, shown: str, wanted: str, same: bool) -> None:
    """A pre-filled value is kept only when it already says the answer and the site does
    not mark it invalid."""
    assert _shows_same(app_field, shown, wanted) is same


# --- 8. same-document links ------------------------------------------------------------------------

POSTING = "https://jobs.brambleway.example/jobs/4007130?src=board"


@pytest.mark.parametrize(
    ("href", "same"),
    [
        pytest.param(f"{POSTING}#", True, id="hash"),
        pytest.param(f"{POSTING}#apply", True, id="fragment"),
        pytest.param("javascript:void(0)", True, id="javascript"),
        pytest.param("https://jobs.brambleway.example/jobs/4007130/apply?src=board#apply", False,
                     id="other-path"),
        pytest.param("https://jobs.brambleway.example/jobs/4007130?src=email#apply", False, id="other-query"),
        pytest.param("https://apply.brambleway.example/jobs/4007130?src=board#apply", False, id="other-host"),
        pytest.param(POSTING, False, id="reload"),
    ],
)
def test_same_document_link(href: str, same: bool) -> None:
    """``DomLink.href`` is the resolved ``a.href``; "#", "#apply" and ``javascript:`` stay
    on the page, anything else loads a document."""
    link = DomLink(text="Apply now", href=href, selector="#apply-now")
    assert _same_document_link(link, POSTING) is same


# --- step identity after Next ----------------------------------------------------------------------


def test_progress_wizard_is_still_on_its_step_while_the_bar_has_not_moved() -> None:
    """A wizard identified by its bar left the step exactly when the bar moved, whatever
    fields it shows; without a bar, most of the step's fields coming back means it is
    shown again."""
    _, before = application(easy_apply_page(bars=[bar(33, dialog=0)]))
    other_fields = [control("#ea-city", "City", name="city", dialog=0),
                    control("#ea-sql", "Years of SQL", name="sqlYears", dialog=0)]
    same_bar, _ = application(easy_apply_page(fields=other_fields, bars=[bar(33, dialog=0)]))
    moved, _ = application(easy_apply_page(bars=[bar(67, dialog=0)]))
    assert _still_on(before, same_bar, by_progress=True)
    assert not _still_on(before, moved, by_progress=True)
    assert _still_on(before, moved, by_progress=False)
    assert not _still_on(before, same_bar, by_progress=False)
    assert not _still_on(before, build_page(_cookie_banner()), by_progress=True)

