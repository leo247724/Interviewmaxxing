"""The raw DOM snapshot: the seam between a page driver and normalization.

``inspect.js`` runs in the page and returns this structure. Everything that turns
it into canonical contracts (``ApplicationForm``, ``PageInspection``) is pure Python
in :mod:`interviewmaxxing_browser.normalize`, so any driver that can evaluate the same
script (Playwright today, an OpenCLI session later) gets identical field identities,
fingerprints and page classification.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Raw(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DomDescription(_Raw):
    text: str
    error: bool


class DomOption(_Raw):
    value: str
    label: str
    disabled: bool
    selected: bool


class DomFile(_Raw):
    name: str
    size: int


class DomControl(_Raw):
    kind: str
    """``native`` (input/select/textarea) or ``custom`` (ARIA widget, contenteditable)."""
    tag: str
    type: str
    """Input type, ``select``/``textarea``, or the custom widget's role."""
    name: str
    id: str
    selector: str
    role: str
    autocomplete_list: bool
    label: str
    label_source: str
    described: list[DomDescription]
    error_message: str
    legend: str | None
    legend_selector: str | None
    legend_described: list[DomDescription]
    group_label: str | None
    group_described: list[DomDescription]
    adjacent: list[str]
    adjacent_errors: list[str]
    section_context: list[str] = Field(default_factory=list)
    preceding: str = ""
    """Visible text of the nearest previous sibling block of the control's own box (or
    of its group's container): a question shown before an unlabeled control."""
    label_selector: str | None
    required: bool
    disabled: bool
    visible: bool
    label_visible: bool
    readonly: bool
    value: str
    checked: bool
    files: list[DomFile]
    placeholder: str
    autocomplete: str
    accept: str
    max_length: int | None
    multiple: bool
    options: list[DomOption]
    invalid: bool
    image_alts: list[str]
    form_index: int
    has_value: bool
    aria: dict[str, Any] | None = None
    """Ephemeral, exact owned-listbox observation (``version`` 1), the facts of a menu
    control whose options are not observable yet (``combo``: role, popup, whether it
    is editable, its display text as ``value``), or, after probing, the probed menu
    (``probed``: ``kind`` select or lookup, options, open method, closed display).
    ``value``, ``expanded`` and ``visible`` are state, never structure."""
    phone_picker: str = ""
    """For a ``tel`` input: the country picker of its own widget, if any. ``iti`` (an
    intl-tel-input-style container), ``dialog`` (a preceding ``aria-haspopup=dialog``
    button) or ``combobox:<id>`` (a sibling combobox; it is a dial-code picker only while
    its display text looks like ``+<code>``)."""


class DomButton(_Raw):
    text: str
    type: str
    selector: str
    disabled: bool
    form_index: int
    submits_form: bool
    form_no_validate: bool
    effective_method: str
    """Method the button would submit with (form method or its ``formmethod``)."""
    effective_action: str
    """URL the button would submit to (form action or its ``formaction``)."""


class DomLink(_Raw):
    text: str
    href: str
    selector: str


class DomHeading(_Raw):
    level: int
    text: str


class DomRegion(_Raw):
    role: str
    text: str


class DomForm(_Raw):
    index: int
    selector: str
    method: str
    action: str
    no_validate: bool


class DomStep(_Raw):
    current: int
    total: int
    source: str


class DomMeta(_Raw):
    og_site_name: str
    og_title: str


class DomCaptchaFrame(_Raw):
    src: str
    title: str
    visible: bool


class DomCaptchaToken(_Raw):
    name: str
    filled: bool


class DomRecord(_Raw):
    group: int
    """Sibling group this element belongs to (same parent, possibly mixed tags/roles)."""
    text: str
    ancestors: list[int]
    """Indexes of enclosing record candidates, nearest first."""


class DomConfirmationScope(_Raw):
    heading: str | None
    text: str
    """One heading-delimited section, or one ungrouped leaf statement."""


class DomSnapshot(_Raw):
    url: str
    title: str
    headings: list[DomHeading]
    regions: list[DomRegion]
    body_text: str
    record_members: list[DomRecord]
    """Members of repeated sibling groups of block elements (record candidates)."""
    confirmation_scopes: list[DomConfirmationScope] = Field(default_factory=list)
    """Local scopes only; missing scope evidence never implies a single-record page."""
    ld_json: list[str]
    meta: DomMeta
    forms: list[DomForm]
    controls: list[DomControl]
    buttons: list[DomButton]
    links: list[DomLink]
    step: DomStep | None
    password_visible: bool
    captcha_frames: list[DomCaptchaFrame]
    captcha_tokens: list[DomCaptchaToken]
    captcha_widget: bool
    loading_indicator: bool = False
    """Visible "loading"/"fetching" wording, an ``aria-busy="true"`` element or a
    progress bar: the page may still be rendering (used only to delay readiness)."""
    document: str = ""
    """``performance.timeOrigin`` and ``location.href`` of the inspected document, the
    identity that scopes probed menu observations."""


@cache
def inspector_script() -> str:
    """The JavaScript function expression that produces a ``DomSnapshot``."""
    from .aria import ARIA_HELPERS

    script = files("interviewmaxxing_browser").joinpath("inspect.js").read_text(encoding="utf-8")
    return script.replace("/* ARIA_HELPERS */", ARIA_HELPERS)
