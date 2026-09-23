"""Deterministic semantic classification of form fields (ARCHITECTURE.md section 7).

Classification uses only the field's own attributes: visible label, help text,
``name``/``id`` tokens, ``autocomplete`` and input type. It never reads other page
text, so instructions elsewhere on a page cannot change what a field is taken to be.

The rules are deliberately conservative where it matters: anything that reads like
consent, a personal attestation, eligibility, salary or a protected attribute gets
the corresponding type, which ``interviewmaxxing_core`` then only allows to be
answered from an explicit saved answer or user input.
"""

from __future__ import annotations

import re

from interviewmaxxing_core import ControlType, SemanticType

_AUTOCOMPLETE: dict[str, SemanticType] = {
    "given-name": SemanticType.FIRST_NAME,
    "family-name": SemanticType.LAST_NAME,
    "name": SemanticType.FULL_NAME,
    "nickname": SemanticType.PREFERRED_NAME,
    "email": SemanticType.EMAIL,
    "tel": SemanticType.PHONE,
    "tel-national": SemanticType.PHONE,
    "street-address": SemanticType.ADDRESS,
    "address-line1": SemanticType.ADDRESS,
    "address-level2": SemanticType.CITY,
    "address-level1": SemanticType.STATE,
    "postal-code": SemanticType.ZIP,
    "country": SemanticType.COUNTRY,
    "country-name": SemanticType.COUNTRY,
    "organization": SemanticType.CURRENT_COMPANY,
    "organization-title": SemanticType.CURRENT_TITLE,
}


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# Order matters: the first matching rule wins.
_RULES: list[tuple[re.Pattern[str], SemanticType]] = [
    (_rx(r"\bgender\b|\bsex\b"), SemanticType.EEO_GENDER),
    (_rx(r"\brace\b|ethnicit|hispanic|latin[oax]"), SemanticType.EEO_RACE_ETHNICITY),
    (_rx(r"veteran"), SemanticType.EEO_VETERAN_STATUS),
    (_rx(r"disabilit|\bdisabled\b"), SemanticType.EEO_DISABILITY_STATUS),
    (_rx(r"pronoun"), SemanticType.PRONOUNS),
    (_rx(r"sponsor"), SemanticType.SPONSORSHIP),
    (
        _rx(r"authori[sz]ed to work|work authori[sz]ation|legally (?:authori[sz]ed|eligible|"
            r"permitted)|right to work|eligib\w* to work"),
        SemanticType.WORK_AUTHORIZATION,
    ),
    (_rx(r"salary|compensation|pay expectation|desired pay|expected pay"), SemanticType.SALARY_EXPECTATION),
    (_rx(r"linkedin"), SemanticType.LINKEDIN),
    (_rx(r"github"), SemanticType.GITHUB),
    (_rx(r"website|portfolio|personal (?:site|url)|blog"), SemanticType.WEBSITE),
    (_rx(r"cover letter"), SemanticType.COVER_LETTER),
    (_rx(r"\bresume\b|résumé|\bcv\b|curriculum vitae"), SemanticType.RESUME),
    (_rx(r"preferred (?:first )?name|nickname"), SemanticType.PREFERRED_NAME),
    (_rx(r"first name|given name|\bfname\b|forename"), SemanticType.FIRST_NAME),
    (_rx(r"last name|surname|family name|\blname\b"), SemanticType.LAST_NAME),
    (_rx(r"full name|legal name|^\s*name\s*$"), SemanticType.FULL_NAME),
    (_rx(r"e-?mail"), SemanticType.EMAIL),
    (_rx(r"phone|mobile|telephone|\bcell\b"), SemanticType.PHONE),
    (_rx(r"relocat"), SemanticType.RELOCATION),
    (_rx(r"years (?:of )?(?:professional |relevant |work )?experience"), SemanticType.YEARS_EXPERIENCE),
    (_rx(r"\bstart date\b|available to start|when can you start|earliest start"), SemanticType.START_DATE),
    (_rx(r"hear about|how did you find|referr|referral source|source of application"), SemanticType.REFERRAL_SOURCE),
    (_rx(r"current (?:company|employer)|most recent (?:company|employer)"), SemanticType.CURRENT_COMPANY),
    (_rx(r"current (?:job )?title|current position|most recent title"), SemanticType.CURRENT_TITLE),
    (_rx(r"highest (?:level of )?education|education level|degree level"), SemanticType.EDUCATION_LEVEL),
    (_rx(r"universit|college|school"), SemanticType.UNIVERSITY),
    (_rx(r"\bdegree\b|field of study|\bmajor\b"), SemanticType.DEGREE),
    (_rx(r"street|address line|^\s*address\s*$|mailing address"), SemanticType.ADDRESS),
    (_rx(r"\bcity\b|\btown\b"), SemanticType.CITY),
    (_rx(r"\bstate\b|province|region"), SemanticType.STATE),
    (_rx(r"\bzip\b|postal code|postcode"), SemanticType.ZIP),
    (_rx(r"\bcountry\b"), SemanticType.COUNTRY),
    (_rx(r"\blocation\b|where are you (?:based|located)"), SemanticType.LOCATION),
]

_CONSENT = _rx(
    r"consent|privacy|data (?:processing|protection)|processing of (?:my|your) (?:personal )?data|"
    r"terms (?:of|and)|terms & conditions|gdpr|marketing|newsletter|subscribe|contact me|"
    r"keep my application|"
    r"retain my|store my"
)
_ATTESTATION = _rx(
    r"\bcertif(?:y|ies|ied)\b|attest|affirm|declar|true and (?:complete|correct|accurate)|"
    r"accurate and complete|"
    r"acknowledg|i understand|i agree|i confirm|i have read|to the best of my knowledge|"
    r"never been|signature"
)
_FIRST_PERSON = _rx(r"^\s*(?:i|i'm|i am|i have|i will|i do|my)\b")
# Free-text questions that ask for a signature or a sworn statement.
_TEXT_ATTESTATION = _rx(
    r"\bcertif(?:y|ies|ied)\b|attest|signature|\bsign(?:ed)? (?:here|below)|i agree|i confirm|"
    r"i acknowledge|i declare|to the best of my knowledge"
)
_TEXT_CONSENT = _rx(r"\bconsent\b")
# Help text that turns an ordinary-looking text box into a signature or sworn
# statement: "By typing your name, you certify all information is true and complete".
_SIGNING_HELP = _rx(
    r"\bby (?:typing|entering|providing|signing|submitting|writing)\b[^.]{0,120}?\b(certif|attest|"
    r"declar|swear|affirm|confirm|agree|consent|acknowledg)"
)

_STATEMENT_CONTROLS = frozenset(
    {ControlType.RADIO, ControlType.SELECT, ControlType.CHECKBOX_GROUP, ControlType.MULTISELECT}
)


def _tokens(*parts: str) -> str:
    out = []
    for part in parts:
        spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", part)
        out.append(re.sub(r"[_\-\[\].:/]+", " ", spaced))
    return " ".join(out)


def classify(
    *,
    label: str,
    help_text: str = "",
    name: str = "",
    element_id: str = "",
    autocomplete: str = "",
    input_type: str | None = None,
    control_type: ControlType,
) -> SemanticType:
    """The semantic type of one field. ``help_text`` is used only for checkboxes,
    where the terms being agreed to often sit outside the label ("I agree")."""
    if control_type is ControlType.UNSUPPORTED:
        return SemanticType.UNKNOWN
    identifiers = _tokens(name, element_id)
    text = f"{label} {identifiers}"

    if control_type is ControlType.FILE:
        if re.search(r"cover letter", text, re.IGNORECASE):
            return SemanticType.COVER_LETTER
        if re.search(r"\bresume\b|résumé|\bcv\b|curriculum", text, re.IGNORECASE):
            return SemanticType.RESUME
        return SemanticType.UNKNOWN

    if control_type is ControlType.CHECKBOX:
        statement = f"{label} {help_text}"
        for pattern, semantic in _RULES[:5]:  # protected attributes first
            if pattern.search(statement):
                return semantic
        if _CONSENT.search(statement):
            return SemanticType.CONSENT
        if _ATTESTATION.search(statement):
            return SemanticType.ATTESTATION
        for pattern, semantic in _RULES[5:]:
            if pattern.search(label):
                return semantic
        # An unexplained first-person statement ("I am ...") is a personal attestation.
        if _FIRST_PERSON.search(label):
            return SemanticType.ATTESTATION
        return SemanticType.CUSTOM_BOOLEAN

    # Consent and attestation are explicit-answer questions whatever the control:
    # a Yes/No radio or select "I certify that ..." is still a personal attestation.
    if control_type in _STATEMENT_CONTROLS:
        statement = f"{label} {help_text}"
        for pattern, semantic in _RULES[:8]:  # protected, sponsorship, authorization, salary
            if pattern.search(label):
                return semantic
        if _CONSENT.search(statement):
            return SemanticType.CONSENT
        if _ATTESTATION.search(statement):
            return SemanticType.ATTESTATION
    if control_type in (ControlType.TEXT, ControlType.TEXTAREA):
        signing = _SIGNING_HELP.search(help_text)
        if signing:
            verb = signing.group(1).lower()
            return SemanticType.CONSENT if verb.startswith("consent") else SemanticType.ATTESTATION
        if _TEXT_CONSENT.search(label):
            return SemanticType.CONSENT
        if _TEXT_ATTESTATION.search(label) or _TEXT_ATTESTATION.search(help_text):
            return SemanticType.ATTESTATION

    auto = autocomplete.split()[-1] if autocomplete else ""
    if auto in _AUTOCOMPLETE and control_type in (ControlType.TEXT, ControlType.SELECT):
        return _AUTOCOMPLETE[auto]
    if control_type is ControlType.TEXT and input_type == "email":
        return SemanticType.EMAIL
    if control_type is ControlType.TEXT and input_type == "tel":
        return SemanticType.PHONE

    for pattern, semantic in _RULES:
        if pattern.search(label) or pattern.search(identifiers):
            if semantic is SemanticType.RESUME or semantic is SemanticType.COVER_LETTER:
                if control_type is ControlType.TEXTAREA and semantic is SemanticType.COVER_LETTER:
                    return semantic
                continue
            return semantic

    if control_type is ControlType.TEXT and input_type == "url":
        return SemanticType.WEBSITE
    return {
        ControlType.TEXT: SemanticType.CUSTOM_TEXT,
        ControlType.TEXTAREA: SemanticType.CUSTOM_LONG_TEXT,
        ControlType.SELECT: SemanticType.CUSTOM_SELECT,
        ControlType.RADIO: SemanticType.CUSTOM_SELECT,
        ControlType.MULTISELECT: SemanticType.CUSTOM_MULTISELECT,
        ControlType.CHECKBOX_GROUP: SemanticType.CUSTOM_MULTISELECT,
    }.get(control_type, SemanticType.UNKNOWN)
