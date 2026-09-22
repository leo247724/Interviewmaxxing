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

from pydantic import BaseModel, ConfigDict


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


class DomButton(_Raw):
    text: str
    type: str
    selector: str
    disabled: bool
    form_index: int
    submits_form: bool
    form_no_validate: bool


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


class DomSnapshot(_Raw):
    url: str
    title: str
    headings: list[DomHeading]
    regions: list[DomRegion]
    body_text: str
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


@cache
def inspector_script() -> str:
    """The JavaScript function expression that produces a ``DomSnapshot``."""
    return files("interviewmaxxing_browser").joinpath("inspect.js").read_text(encoding="utf-8")
