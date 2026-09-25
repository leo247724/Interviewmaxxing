"""Lookup (TYPEAHEAD) answers and international phone numbers (fictional data only)."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

import pytest

from interviewmaxxing_core import (
    AnswerScope,
    AnswerSource,
    ApplicationField,
    ApplicationForm,
    CandidateProfile,
    ChoiceValue,
    ControlType,
    FieldOption,
    MissingReason,
    SavedAnswer,
    SemanticType,
    TextValue,
    UserInput,
)
from interviewmaxxing_core.interfaces import SuggestionChooser
from interviewmaxxing_generation import (
    FactualPacketResolver,
    PhoneFormatError,
    international_phone,
    lookup_alternatives,
    lookup_text,
    stored_value,
)

URL = "http://127.0.0.1:9/jobs/lookup/apply"
CONFIRMED = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _with_address(candidate: CandidateProfile, *, city: str | None = "Austin",
                  region: str | None = "TX", country: str | None = "United States",
                  phone: str | None = None) -> CandidateProfile:
    identity = candidate.identity
    address = identity.address.model_copy(update={"city": city, "region": region,
                                                  "country": country})
    update: dict[str, object] = {"address": address}
    if phone is not None:
        update["phone"] = phone
    return candidate.model_copy(update={"identity": identity.model_copy(update=update)})


def _form(*fields: ApplicationField) -> ApplicationForm:
    return ApplicationForm(url=URL, fields=list(fields), is_final_step=True)


def _lookup(field_id: str, semantic: SemanticType, *, label: str | None = None,
            required: bool = True) -> ApplicationField:
    return ApplicationField(id=field_id, label=label or field_id.title(), selector=f"#{field_id}",
                            semantic_type=semantic, control_type=ControlType.TYPEAHEAD,
                            required=required)


def _phone(*, international: bool, required: bool = True) -> ApplicationField:
    return ApplicationField(id="phone", label="Phone", selector="#phone", input_type="tel",
                            semantic_type=SemanticType.PHONE, control_type=ControlType.TEXT,
                            required=required, expects_international_phone=international)


# --- lookups -----------------------------------------------------------------------------------


def test_typeahead_location_fields_are_typed_from_the_verified_address(
    fictional_candidate, make_context, resolve
):
    candidate = _with_address(fictional_candidate)
    form = _form(_lookup("location", SemanticType.LOCATION, label="Location (City)"),
                 _lookup("city", SemanticType.CITY), _lookup("state", SemanticType.STATE),
                 _lookup("country", SemanticType.COUNTRY))
    context = make_context(form, candidate)
    packet = resolve(context)
    assert context.problems(packet) == [] and packet.is_complete
    typed = {a.field_id: a.value.text for a in packet.answers if isinstance(a.value, TextValue)}
    # A city lookup types the bare city first (round 7): "City, Region" is typed only when
    # the site offers nothing for it (``lookup_alternatives``, used by the runner).
    assert typed == {"location": "Austin, TX", "city": "Austin", "state": "Texas",
                     "country": "United States"}
    for answer in packet.answers:
        assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
        assert answer.provenance.note == f"verified identity: {answer.field_id}"


def test_location_lookup_outside_the_us_names_the_country(fictional_candidate):
    identity = _with_address(fictional_candidate, city="London", region=None,
                             country="United Kingdom").identity
    assert lookup_text(identity, SemanticType.LOCATION) == "London, United Kingdom"
    canada = _with_address(fictional_candidate, city="Toronto", region="ON",
                           country="Canada").identity
    assert lookup_text(canada, SemanticType.LOCATION) == "Toronto, ON, Canada"
    assert lookup_text(canada, SemanticType.STATE) == "ON"


@pytest.mark.parametrize("region,country,expected", [
    ("TX", "United States", "Texas"),
    ("dc", "USA", "District of Columbia"),
    ("NH", None, "New Hampshire"),
    ("Texas", "United States", "Texas"),
    ("WA", "Australia", "WA"),  # Western Australia, not Washington
])
def test_state_lookup_spells_out_only_us_abbreviations(fictional_candidate, region, country,
                                                        expected):
    identity = _with_address(fictional_candidate, region=region, country=country).identity
    assert lookup_text(identity, SemanticType.STATE) == expected


def test_location_lookup_needs_a_city(fictional_candidate, make_context, resolve):
    candidate = _with_address(fictional_candidate, city=None)
    packet = resolve(make_context(_form(_lookup("location", SemanticType.LOCATION)), candidate))
    assert packet.answers == []
    [missing] = packet.missing_inputs
    assert missing.reason is MissingReason.NO_ANSWER
    assert "typed into the site's search" in missing.prompt


@pytest.mark.parametrize("semantic", [SemanticType.FIRST_NAME, SemanticType.EMAIL,
                                      SemanticType.CURRENT_COMPANY, SemanticType.UNIVERSITY,
                                      SemanticType.UNKNOWN, SemanticType.CUSTOM_TEXT])
def test_other_lookups_stay_missing_without_a_saved_answer_or_input(
    fictional_candidate, make_context, resolve, semantic
):
    form = _form(_lookup("lookup", semantic, label="Current company"))
    packet = resolve(make_context(form, fictional_candidate))
    assert packet.answers == []
    assert [m.reason for m in packet.missing_inputs] == [MissingReason.NO_ANSWER]


def test_a_saved_answer_or_user_input_answers_any_lookup(fictional_candidate, make_context,
                                                          resolve):
    school = SavedAnswer(id="sa.school", scope=AnswerScope.GLOBAL,
                         semantic_type=SemanticType.UNIVERSITY, question="School",
                         value="Fictional State University", confirmed_at=CONFIRMED)
    candidate = fictional_candidate.model_copy(update={"saved_answers": [school]})
    form = _form(_lookup("school", SemanticType.UNIVERSITY, label="School"),
                 _lookup("location", SemanticType.LOCATION))
    typed = UserInput.for_field(form, "location", TextValue(text="Austin, Texas, United States"))
    context = make_context(form, candidate, user_inputs=[typed])
    packet = resolve(context)
    assert context.problems(packet) == []
    school_answer, location_answer = packet.answer_for("school"), packet.answer_for("location")
    assert school_answer is not None and location_answer is not None
    assert school_answer.value == TextValue(text="Fictional State University")
    assert school_answer.provenance.source is AnswerSource.SAVED_ANSWER
    assert location_answer.value == TextValue(text="Austin, Texas, United States")
    assert location_answer.provenance.source is AnswerSource.USER_INPUT


def test_explicit_lookups_are_never_answered_from_the_identity(fictional_candidate, make_context,
                                                               resolve):
    form = _form(_lookup("citizenship", SemanticType.WORK_AUTHORIZATION,
                         label="Country of work authorization"))
    packet = resolve(make_context(form, _with_address(fictional_candidate)))
    assert packet.answers == []
    assert [m.reason for m in packet.missing_inputs] == [MissingReason.EXPLICIT_ANSWER_REQUIRED]


def test_the_factual_resolver_never_picks_a_site_suggestion(fictional_candidate, make_context):
    resolver = FactualPacketResolver()
    assert isinstance(resolver, SuggestionChooser)
    form = _form(_lookup("location", SemanticType.LOCATION))
    choice = asyncio.run(resolver.choose_suggestion(
        make_context(form, fictional_candidate), form.fields[0], "Austin, TX",
        ["Austin, Texas, United States", "Austin, Minnesota, United States"]))
    assert choice is None


# --- international phone ------------------------------------------------------------------------


@pytest.mark.parametrize("phone,country,expected", [
    ("512-555-0100", "United States", "+15125550100"),
    ("(512) 555-0100", "USA", "+15125550100"),
    ("1 512 555 0100", "U.S.", "+15125550100"),
    ("001 512 555 0100", "United States of America", "+15125550100"),
    ("416 555 0199", "Canada", "+14165550199"),
    ("020 7946 0018", "United Kingdom", "+442079460018"),
    ("44 20 7946 0018", "UK", "+442079460018"),
    ("0044 20 7946 0018", "England", "+442079460018"),
    ("06 1234 5678", "Italy", "+390612345678"),
    ("0412 345 678", "Australia", "+61412345678"),
    ("+1 512 555 0100", "Canada", "+1 512 555 0100"),
    ("+44 20 7946 0018", None, "+44 20 7946 0018"),
])
def test_international_phone_conversion(phone, country, expected):
    assert international_phone(phone, country) == expected


@pytest.mark.parametrize("phone,country,reason", [
    ("512-555-0100", None, "your profile has no country"),
    ("512-555-0100", "Atlantis", "dialing code for 'Atlantis' is not known"),
    ("555-0100", "United States", "does not have a valid number of digits for United States (+1)"),
    ("512-555-0100 ext 12", "United States", "valid number of digits"),
    ("4930123456", "Germany", "can be read more than one way"),
])
def test_unconvertible_phone_numbers_are_never_guessed(phone, country, reason):
    with pytest.raises(PhoneFormatError, match=re.escape(reason)):
        international_phone(phone, country)


def test_phone_with_a_country_picker_gets_the_international_number(
    fictional_candidate, make_context, resolve
):
    candidate = _with_address(fictional_candidate, phone="512-555-0100")
    form = _form(_phone(international=True))
    context = make_context(form, candidate)
    packet = resolve(context)
    assert context.problems(packet) == []
    [answer] = packet.answers
    assert answer.value == TextValue(text="+15125550100")
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
    assert "international form" in (answer.provenance.note or "")


def test_phone_without_a_country_picker_keeps_the_verified_value(
    fictional_candidate, make_context, resolve
):
    candidate = _with_address(fictional_candidate, phone="5125550100")
    packet = resolve(make_context(_form(_phone(international=False)), candidate))
    assert packet.answers[0].value == TextValue(text="5125550100")
    assert packet.answers[0].provenance.note == "verified identity: phone"


def test_a_leading_plus_number_is_kept_as_verified(fictional_candidate, make_context, resolve):
    packet = resolve(make_context(_form(_phone(international=True)), fictional_candidate))
    assert packet.answers[0].value == TextValue(text=fictional_candidate.identity.phone or "")


@pytest.mark.parametrize("country,phone", [("Atlantis", "512-555-0100"),
                                           ("United States", "555-0100")])
def test_unconvertible_phone_is_asked_with_a_clear_prompt(
    fictional_candidate, make_context, resolve, country, phone
):
    candidate = _with_address(fictional_candidate, country=country, phone=phone)
    packet = resolve(make_context(_form(_phone(international=True)), candidate))
    assert packet.answers == []
    [missing] = packet.missing_inputs
    assert missing.reason is MissingReason.NO_ANSWER
    assert "international form (+ country code)" in missing.prompt
    assert "+44 20 7946 0018" in missing.prompt
    optional = resolve(make_context(_form(_phone(international=True, required=False)), candidate))
    assert optional.answers == [] and optional.missing_inputs == []


# --- stored values for option mapping -------------------------------------------------------------


def _radio(field_id: str, label: str, semantic: SemanticType, *options: str) -> ApplicationField:
    return ApplicationField(id=field_id, label=label, selector=f"#{field_id}",
                            semantic_type=semantic, control_type=ControlType.RADIO, required=True,
                            options=[FieldOption(value=f"v{i}", label=o) for i, o in enumerate(options)])


def test_stored_value_is_the_saved_answer_for_exactly_this_wording(fictional_candidate,
                                                                   make_context):
    work = _radio("work", "Are you legally authorized to work in the United States?",
                  SemanticType.WORK_AUTHORIZATION, "Yes, I am authorized", "No, I am not")
    stored = stored_value(make_context(_form(work), fictional_candidate), work)
    assert stored is not None and stored.value == "Yes"
    assert stored.provenance.source is AnswerSource.SAVED_ANSWER
    assert stored.provenance.reference_ids == ["sa.work_auth_us"]
    other_wording = work.model_copy(update={"label": "Are you authorized to work anywhere?"})
    assert stored_value(make_context(_form(other_wording), fictional_candidate), other_wording) is None


def test_stored_value_falls_back_to_identity_but_never_for_explicit_types(fictional_candidate,
                                                                          make_context):
    country = ApplicationField(id="country", label="Country", selector="#country",
                               semantic_type=SemanticType.COUNTRY, control_type=ControlType.SELECT,
                               options=[FieldOption(value="usa", label="United States of America (USA)")])
    stored = stored_value(make_context(_form(country), fictional_candidate), country)
    assert stored is not None and stored.value == "United States"
    assert stored.provenance.source is AnswerSource.PROFILE_IDENTITY
    salary = _radio("salary", "Desired salary", SemanticType.SALARY_EXPECTATION, "100k", "150k")
    assert stored_value(make_context(_form(salary), fictional_candidate), salary) is None


def test_stored_value_yields_to_user_input_and_disagreeing_saved_answers(fictional_candidate,
                                                                        make_context):
    work = _radio("work", "Are you legally authorized to work in the United States?",
                  SemanticType.WORK_AUTHORIZATION, "Yes, I am authorized", "No, I am not")
    form = _form(work)
    answered = UserInput.for_field(form, "work",
                                   ChoiceValue(value="v0", label="Yes, I am authorized"))
    assert stored_value(make_context(form, fictional_candidate, user_inputs=[answered]), work) is None
    disagreeing = SavedAnswer(id="sa.work_auth_no", scope=AnswerScope.GLOBAL,
                              semantic_type=SemanticType.WORK_AUTHORIZATION,
                              question="Are you legally authorized to work in the United States?",
                              value="No", confirmed_at=CONFIRMED)
    candidate = fictional_candidate.model_copy(update={
        "saved_answers": [*fictional_candidate.saved_answers, disagreeing]})
    assert stored_value(make_context(form, candidate), work) is None


# --- rounds 6-7 (M3): a city lookup types the bare city, then "City, Region" ----------------


@pytest.mark.parametrize(("city", "region", "country", "expected"), [
    ("Austin", "TX", "United States", "Austin, TX"),
    ("Austin", "TX", None, "Austin, TX"),
    ("Toronto", "ON", "Canada", "Toronto, ON"),
    ("Austin", "Texas", "United States", "Austin, Texas"),
    ("  San   Antonio ", " TX ", "United States", "San Antonio, TX"),
    ("London", None, "United Kingdom", "London"),
    ("London", "   ", "United Kingdom", "London"),
    (None, "TX", "United States", None),
    ("  ", "TX", "United States", None),
], ids=["us", "no-country", "outside-the-us", "region-as-stored", "whitespace-collapsed",
        "no-region", "blank-region", "no-city", "blank-city"])
def test_a_city_lookup_types_the_bare_city_and_offers_city_region_second(
    fictional_candidate, city, region, country, expected
):
    # The bare city is typed first; "City, Region" (region as stored, never a country) is
    # the one alternative, offered only when both are known.
    identity = _with_address(fictional_candidate, city=city, region=region,
                             country=country).identity
    bare = expected.split(", ")[0] if expected else None
    assert lookup_text(identity, SemanticType.CITY) == bare
    assert lookup_alternatives(identity, SemanticType.CITY) == (
        [expected] if expected and expected != bare else [])
    assert lookup_alternatives(identity, SemanticType.LOCATION) == []


def test_city_and_location_lookups_outside_the_us_are_typed_from_the_verified_address(
    fictional_candidate, make_context, resolve
):
    candidate = _with_address(fictional_candidate, city="Toronto", region="ON", country="Canada")
    form = _form(_lookup("city", SemanticType.CITY, label="City"),
                 _lookup("location", SemanticType.LOCATION, label="Location"))
    context = make_context(form, candidate)
    packet = resolve(context)
    assert context.problems(packet) == [] and packet.is_complete
    assert {a.field_id: a.value for a in packet.answers} == {
        "city": TextValue(text="Toronto"), "location": TextValue(text="Toronto, ON, Canada")}
    assert all(a.provenance.source is AnswerSource.PROFILE_IDENTITY for a in packet.answers)


def test_a_city_lookup_without_a_region_types_the_bare_city(fictional_candidate, make_context,
                                                             resolve):
    candidate = _with_address(fictional_candidate, city="London", region=None,
                              country="United Kingdom")
    context = make_context(_form(_lookup("city", SemanticType.CITY, label="City")), candidate)
    packet = resolve(context)
    assert context.problems(packet) == []
    [answer] = packet.answers
    assert answer.value == TextValue(text="London")
    assert answer.provenance.note == "verified identity: city"


def test_a_city_lookup_without_a_city_is_asked_even_with_a_region(fictional_candidate,
                                                                   make_context, resolve):
    candidate = _with_address(fictional_candidate, city=None, region="TX")
    packet = resolve(make_context(_form(_lookup("city", SemanticType.CITY, label="City")),
                                  candidate))
    assert packet.answers == []
    [missing] = packet.missing_inputs
    assert missing.field_id == "city" and missing.reason is MissingReason.NO_ANSWER
    assert "typed into the site's search" in missing.prompt


def test_a_plain_text_city_field_still_gets_the_bare_city(fictional_candidate, make_context,
                                                          resolve):
    # A plain text city field gets the bare city (a lookup retries with the region).
    candidate = _with_address(fictional_candidate)
    city = ApplicationField(id="city", label="City", selector="#city",
                            semantic_type=SemanticType.CITY, control_type=ControlType.TEXT,
                            required=True)
    context = make_context(_form(city), candidate)
    packet = resolve(context)
    assert context.problems(packet) == []
    [answer] = packet.answers
    assert answer.value == TextValue(text="Austin")
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY


# --- round 7 (L3, L5): state codes only in lists; control characters hold --------------------

@pytest.mark.parametrize("text,codes", [
    ("ARE YOU WILLING TO RELOCATE OR NOT?", set()),  # "OR" is a word here
    ("PLEASE ANSWER IN FULL. ME AND MY FAMILY", set()),
    ("Do you live in AL, AZ, CA?", {"AL", "AZ", "CA"}),
    ("States: OR/WA", {"OR", "WA"}),
    ("AL, AZ and CA", {"AL", "AZ"}),
    ("Do you live in Texas?", {"TX"}),
    ("West Virginia", {"WV"}),
])
def test_state_codes_count_only_in_a_list_of_two_or_more(text, codes):
    from interviewmaxxing_generation.values import us_states_named

    assert us_states_named(text) == codes


@pytest.mark.parametrize("control,held", [(ControlType.TEXT, True), (ControlType.TEXTAREA, False)])
def test_a_saved_value_with_a_control_character_holds_a_single_line_field(
    fictional_candidate, make_context, resolve, control, held
):
    from interviewmaxxing_core import AnswerScope, SavedAnswer

    saved = SavedAnswer(id="sa.motto", scope=AnswerScope.GLOBAL, question="Your motto",
                        value="Line one.\nLine two.", confirmed_at="2026-09-01T12:00:00Z")
    candidate = fictional_candidate.model_copy(update={"saved_answers": [saved]})
    field = ApplicationField(id="motto", label="Your motto", selector="#motto",
                             semantic_type=SemanticType.CUSTOM_TEXT, control_type=control,
                             required=True)
    context = make_context(_form(field), candidate)
    packet = resolve(context)  # a hold, never an internal error
    assert context.problems(packet) == []
    if held:
        assert packet.answers == []
        [missing] = packet.missing_inputs
        assert missing.field_id == "motto" and "control character" in missing.prompt
    else:
        [answer] = packet.answers
        assert answer.value == TextValue(text="Line one.\nLine two.")
