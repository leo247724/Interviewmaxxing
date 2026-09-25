"""Interest, motivation and fit wordings are cover-letter narratives; preferences stay
explicit answers."""
import pytest

from interviewmaxxing_generation.questions import motivation_question


@pytest.mark.parametrize("text", [
    "What interests you about Pacvue?", "Why do you want to work here?",
    "What draws you to this role?", "Why this company?", "Why Pacvue?",
    "What motivates you in marketing?", "Tell us why you are a great fit for this role.",
    "Why are you interested in joining our team?", "Why would you be a good fit for this position?",
    "What excites you about the opportunity?",
])
def test_motivation_wordings(text: str) -> None:
    assert motivation_question(text)


@pytest.mark.parametrize("text", [
    "What are your salary expectations?", "Why do you want to relocate to Austin?",
    "What hours are you available to work?", "Describe a campaign you led.",
    "Why are you leaving your current role?", "Are you authorized to work in the US?",
    "What interests you about a hybrid schedule?", "When can you start?", "", None,
])
def test_non_motivation_wordings(text: str | None) -> None:
    assert not motivation_question(text)
