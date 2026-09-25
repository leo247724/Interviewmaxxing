"""Round 11 (core): a saved Yes/No as one sentence, and the applicant's own profile URL on an
untyped profile-link question (fictional data only)."""
from __future__ import annotations

import pytest

from interviewmaxxing_core import (
    PROFILE_LINK_TYPES,
    YES_NO_SENTENCES,
    AnswerSource,
    ApplicationField,
    ApplicationForm,
    ControlType,
    FieldOption,
    PacketAnswer,
    Provenance,
    SemanticType,
    TextValue,
    yes_no_sentence,
)
from interviewmaxxing_core.packets import provenance_problems

NON_COMPETE = ("Have you signed any non-competition or non-solicitation agreement that could "
               "restrict your work for this employer?")


def test_a_yes_no_question_has_one_sentence_per_answer_in_the_persons_voice():
    assert yes_no_sentence(NON_COMPETE, False) == (
        "No, I have not signed any non-competition or non-solicitation agreement.")
    assert yes_no_sentence(NON_COMPETE, True).startswith("Yes, ")
    # Case and spacing do not matter; another question has no sentence.
    assert yes_no_sentence("  are you ABOVE the age of 18?  ", False) == "No, I am not over the age of 18."
    assert yes_no_sentence("Do you like pizza?", True) is None
    for yes, no in YES_NO_SENTENCES.values():
        assert yes.startswith("Yes, ") and no.startswith("No, ")
        assert yes.endswith(".") and no.endswith(".") and "\n" not in yes + no


def _link_form(semantic: SemanticType, control: ControlType = ControlType.TEXTAREA) -> ApplicationForm:
    options = ([FieldOption(value="v0", label="https://www.linkedin.example/in/avery-example")]
               if control is ControlType.SELECT else None)
    field = ApplicationField(id="link", selector="#link",
                             label="Professional profile link (LinkedIn, portfolio, or personal site)",
                             semantic_type=semantic, control_type=control, required=True,
                             options=options)
    return ApplicationForm(url="https://example.test/apply", fields=[field])


def _link_packet(mock_packet, form: ApplicationForm, text: str, semantic: SemanticType):
    answer = PacketAnswer(field_id="link", semantic_type=semantic, value=TextValue(text=text),
                          provenance=Provenance(source=AnswerSource.PROFILE_IDENTITY,
                                                note="verified identity: linkedin url"))
    return mock_packet.model_copy(update={"answers": [answer], "missing_inputs": [],
                                          "form_url": form.url, "form_fingerprint": form.fingerprint})


@pytest.mark.parametrize("semantic", sorted(PROFILE_LINK_TYPES))
def test_the_applicants_own_profile_url_may_answer_an_untyped_profile_link_question(
    fictional_candidate, mock_job, mock_packet, semantic,
):
    form = _link_form(semantic)
    packet = _link_packet(mock_packet, form, fictional_candidate.identity.linkedin_url, semantic)
    assert packet.problems_against(form) == []
    assert provenance_problems(packet, form=form, candidate=fictional_candidate, job=mock_job) == []


def test_a_link_that_is_not_the_applicants_own_is_rejected(fictional_candidate, mock_job, mock_packet):
    form = _link_form(SemanticType.UNKNOWN)
    packet = _link_packet(mock_packet, form, "https://www.linkedin.example/in/someone-else",
                          SemanticType.UNKNOWN)
    assert packet.problems_against(form) == []  # a URL on a profile-link question
    problems = provenance_problems(packet, form=form, candidate=fictional_candidate, job=mock_job)
    assert any("not the applicant's own profile URL" in problem for problem in problems)


@pytest.mark.parametrize("semantic,text", [
    (SemanticType.UNKNOWN, "Avery Example"),  # not a URL: identity never answers it
    (SemanticType.CUSTOM_BOOLEAN, "https://www.linkedin.example/in/avery-example"),
    (SemanticType.CURRENT_TITLE, "https://www.linkedin.example/in/avery-example"),
])
def test_identity_stays_limited_to_identity_fields_otherwise(mock_packet, semantic, text):
    form = _link_form(semantic)
    packet = _link_packet(mock_packet, form, text, semantic)
    assert any("is not an identity field" in problem for problem in packet.problems_against(form))
