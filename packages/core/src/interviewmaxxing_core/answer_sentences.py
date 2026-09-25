"""A saved Yes/No answer typed into a free-text control as one sentence (round 11).

A text area that asks a yes/no question ("Have you signed any non-competition agreement …?
If yes, describe") gets the person's saved Yes/No answer as one sentence in their voice,
never a bare "No" and never invented detail. The sentences belong to the simple-answers
map's Yes/No questions, keyed by the saved question (``normalize_text``)."""

from __future__ import annotations

from .forms import normalize_text

_SENTENCES: dict[str, tuple[str, str]] = {
    "Were you referred to this position by a current employee?": (
        "Yes, a current employee referred me to this position.",
        "No, I was not referred to this position by a current employee."),
    "Are you above the age of 18?": (
        "Yes, I am over the age of 18.",
        "No, I am not over the age of 18."),
    "Have you previously been employed by this company?": (
        "Yes, I have previously worked for this company.",
        "No, I have not previously worked for this company."),
    "Have you previously interviewed with this company?": (
        "Yes, I have interviewed with this company before.",
        "No, I have not interviewed with this company before."),
    "Are you related to any current employee of this company?": (
        "Yes, I am related to a current employee of this company.",
        "No, I am not related to any current employee of this company."),
    "Would you like to be considered for other open positions?": (
        "Yes, I would like to be considered for other open positions.",
        "No, I would like to be considered for this position only."),
    "Are you willing to provide references?": (
        "Yes, I am willing to provide references.",
        "No, I am not able to provide references."),
    "Are you or anyone in your immediate family a government official?": (
        "Yes, I or a member of my immediate family is a government official.",
        "No, neither I nor anyone in my immediate family is a government official."),
    "Have you signed any non-competition or non-solicitation agreement that could restrict "
    "your work for this employer?": (
        "Yes, I have signed a non-competition or non-solicitation agreement.",
        "No, I have not signed any non-competition or non-solicitation agreement."),
    "Have you used AI tools to help prepare this application?": (
        "Yes, I used AI tools to help prepare this application.",
        "No, I did not use AI tools to help prepare this application."),
}
YES_NO_SENTENCES: dict[str, tuple[str, str]] = {
    normalize_text(question): pair for question, pair in _SENTENCES.items()}
"""Normalized saved question → (the Yes sentence, the No sentence)."""


def yes_no_sentence(question: str, yes: bool) -> str | None:
    """The person's Yes or No to a saved Yes/No question as one sentence, or None when the
    question is not one of the simple-answers Yes/No questions."""
    pair = YES_NO_SENTENCES.get(normalize_text(question))
    return None if pair is None else pair[0 if yes else 1]
