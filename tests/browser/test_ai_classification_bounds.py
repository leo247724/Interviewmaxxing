"""Bounded full-form requests that never lose a form (WP10 item 2).

Pilot 7 lost a whole Greenhouse form (Appspace) to the request bound: a "Country / Phone"
dial-code select with about 240 options went into every request with every option, every
single-field request exceeded ``CallBudget.max_request_bytes`` and all 13 required fields,
First Name to Phone, were held. Fields now list at most 40 options (with the count, a note
and a shape hint), are packed into requests under 85% of the bound, and a request that
still exceeds it is halved down to single fields.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from interviewmaxxing_browser.ai import (
    AIFormRouter,
    BoundedDecisions,
    CallBudget,
    DynamicPacketResolver,
)
from interviewmaxxing_browser.ai.classification import (
    MAX_FIELD_OPTIONS,
    FieldRoute,
    field_data,
    option_shape,
)
from interviewmaxxing_browser.semantics import classify as classify_semantics
from interviewmaxxing_core import (
    AnswerSource,
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationState,
    CandidateProfile,
    ControlType,
    FieldOption,
    JobRecord,
    PacketContext,
    SemanticType,
)
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import DecisionRequest, HttpResponse, JevClient

BOUND = CallBudget().max_request_bytes
TARGET = int(BOUND * 0.85)
DEFAULTS = {"r": "COPY_KNOWN", "n": "literal", "u": "APPLICANT_CURRENT", "s": "CUSTOM_TEXT",
            "d": "APPLICATION_ATTACHMENT"}


class Jev:
    """Scripted Jev recording every request body. ``scripts[field_id][kind]`` is the choice
    for that field's r/n/u/s/d question (all mass, confidence 1.0); other choices pick
    NONE/UNKNOWN and nouls answer 0.0."""

    def __init__(self, scripts: dict[str, dict[str, str]] | None = None) -> None:
        self.scripts = scripts or {}
        self.requests: list[dict[str, Any]] = []
        self.sizes: list[int] = []

    def classifications(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if any(k.startswith("r") and k[1:].isdigit()
                                                for k in r["questions"])]

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        self.sizes.append(len(body))
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.0}
                continue
            criteria = list(question["criteria"])
            if name[0] in DEFAULTS and name[1:].isdigit():
                field_id = request["state"]["fields"][f"f{name[1:]}"]["field_id"]
                choice = self.scripts.get(field_id, {}).get(name[0], DEFAULTS[name[0]])
            else:
                choice = next((c for c in ("NONE", "hold", "UNKNOWN") if c in criteria), criteria[0])
            answers[name] = {"type": "choice", "choice": choice, "confidence": 1.0,
                             "probabilities": {c: float(c == choice) for c in criteria}}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


def router(provider: Jev, budget: CallBudget | None = None, cls: type[AIFormRouter] = AIFormRouter,
           ) -> AIFormRouter:
    return cls(BoundedDecisions(JevClient(ApiKey("synthetic-test", source="fixture"),
        transport=provider, max_attempts=1), budget or CallBudget()))


def observed(field_id: str, label: str, control: ControlType, options: list[FieldOption] | None = None,
             *, input_type: str | None = None, required: bool = True) -> ApplicationField:
    """A field as the inspector reports it: typed by the browser heuristics."""
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label,
        semantic_type=classify_semantics(label=label, control_type=control, input_type=input_type),
        control_type=control, required=required, input_type=input_type, options=options)


def labels(*items: str) -> list[FieldOption]:
    return [FieldOption(value=f"v{i}", label=label) for i, label in enumerate(items)]


COUNTRIES = ["Afghanistan", "Åland Islands", "Albania", "Algeria", "Argentina", "Armenia",
    "Australia", "Austria", "Azerbaijan", "Bahamas", "Bangladesh", "Belarus", "Belgium", "Bolivia",
    "Brazil", "Bulgaria", "Cambodia", "Cameroon", "Canada", "Chile", "China", "Colombia",
    "Costa Rica", "Croatia", "Cuba", "Cyprus", "Czechia", "Côte d'Ivoire", "Denmark", "Ecuador",
    "Egypt", "Estonia", "Ethiopia", "Finland", "France", "Germany", "Ghana", "Greece", "Guatemala",
    "Hungary", "Iceland", "India", "Indonesia", "Ireland", "Israel", "Italy", "Jamaica", "Japan",
    "Jordan", "Kenya", "Latvia", "Lithuania", "Luxembourg", "Malaysia", "Mexico", "Morocco",
    "Netherlands", "New Zealand", "Nigeria", "Norway", "Pakistan", "Panama", "Peru", "Philippines",
    "Poland", "Portugal", "Romania", "Serbia", "Singapore", "Slovakia", "Slovenia", "South Africa",
    "Spain", "Sri Lanka", "Sweden", "Switzerland", "Thailand", "Tunisia", "Turkey", "Uganda",
    "Ukraine", "United Arab Emirates", "United Kingdom", "United States", "Uruguay", "Vietnam"]


def dial_codes(count: int) -> list[FieldOption]:
    """A Greenhouse-style country/dial-code menu: "United States +1", "Åland Islands +358", ..."""
    names = [f"{COUNTRIES[i % len(COUNTRIES)]}{'' if i < len(COUNTRIES) else f' ({i})'}"
             for i in range(count)]
    return [FieldOption(value=f"{name} +{i + 1}", label=f"{name} +{i + 1}")
            for i, name in enumerate(names)]


# --- field data -------------------------------------------------------------------------

def test_field_data_lists_forty_options_with_the_count_a_note_and_a_shape() -> None:
    menu = observed("phone_country", "Country / Phone", ControlType.SELECT, dial_codes(250))
    data = field_data(menu)
    assert len(data["options"]) == MAX_FIELD_OPTIONS == 40
    assert data["options"][0] == {"value": "Afghanistan +1", "label": "Afghanistan +1", "disabled": False}
    assert data["option_count"] == 250
    assert data["options_note"] == "… and 210 more"
    assert data["option_shape"] == "dial_codes"
    short = field_data(observed("source", "How did you hear about us?", ControlType.SELECT,
                                labels(*(f"Channel {i}" for i in range(40)))))
    assert len(short["options"]) == short["option_count"] == 40
    assert "options_note" not in short and "option_shape" not in short
    text = field_data(observed("first", "First Name", ControlType.TEXT))
    assert text["options"] == [] and text["option_count"] == 0 and "options_note" not in text


US_STATES = ["Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
    "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts",
    "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota",
    "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington", "West Virginia",
    "Wisconsin", "Wyoming"]


@pytest.mark.parametrize("options,shape", [
    (dial_codes(240), "dial_codes"),
    (labels(*(f"+{i} ({name})" for i, name in enumerate(COUNTRIES))), "dial_codes"),
    (labels("Select...", *COUNTRIES), "countries"),
    (labels("Select a state", *US_STATES, "Puerto Rico", "Guam"), "us_states"),
    (labels(*(code for code in ("AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
        "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
        "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD"))),
     "us_states"),
    (labels(*(str(year) for year in range(2030, 1949, -1))), "years"),
    (labels(*(f"University number {i}" for i in range(60))), "other"),
    (labels(*(str(n) for n in range(60))), "other"),
])
def test_option_shape_names_the_common_long_lists(options: list[FieldOption], shape: str) -> None:
    assert option_shape(options) == shape


# --- packing ------------------------------------------------------------------------------

GREENHOUSE_HINT = {"backend": "greenhouse", "authority": "untrusted_prior_only",
    "field_patterns": [{"label": f"Observed question family {i}: phone, location, EEO and "
                        "custom yes/no questions", "control": "select", "observations": i}
                       for i in range(70)]}


def twenty_fields() -> list[ApplicationField]:
    yes_no = labels("Yes", "No")
    return [
        observed("first", "First Name", ControlType.TEXT),
        observed("last", "Last Name", ControlType.TEXT),
        observed("email", "Email", ControlType.TEXT, input_type="email"),
        observed("phone", "Phone", ControlType.TEXT, input_type="tel"),
        observed("phone_country", "Country / Phone", ControlType.SELECT, dial_codes(250)),
        observed("city", "Location (City)", ControlType.TEXT),
        observed("linkedin", "LinkedIn Profile", ControlType.TEXT),
        observed("website", "Website", ControlType.TEXT, input_type="url", required=False),
        observed("resume", "Resume/CV", ControlType.FILE),
        observed("cover", "Cover Letter", ControlType.FILE, required=False),
        *(observed(f"yes_no_{i}", question, ControlType.SELECT, yes_no) for i, question in enumerate([
            "Have you managed paid social budgets above $1M a year?",
            "Have you worked in a performance marketing agency environment?",
            "Do you have experience with Google Ads Performance Max?",
            "Have you led a team of five or more marketers?",
            "Have you worked with a B2B SaaS company?"])),
        observed("platform", "Which ad platform have you used most?", ControlType.TEXT),
        observed("tools", "Which analytics tool do you prefer?", ControlType.TEXT),
        observed("why", "Why do you want to work at Brambleway?", ControlType.TEXTAREA),
        observed("gender", "Gender", ControlType.SELECT, labels("Male", "Female", "Decline to self-identify"),
                 required=False),
        observed("source", "How did you hear about us?", ControlType.SELECT,
                 labels("LinkedIn", "Company website", "Referral", "Other")),
    ]


def twenty_scripts() -> dict[str, dict[str, str]]:
    scripts: dict[str, dict[str, str]] = {f"yes_no_{i}": {"s": "CUSTOM_BOOLEAN", "u": "HISTORICAL_OR_CONTEXTUAL"}
                                          for i in range(5)}
    scripts["why"] = {"r": "WRITER", "n": "prose", "s": "CUSTOM_LONG_TEXT", "u": "HISTORICAL_OR_CONTEXTUAL"}
    scripts["resume"] = scripts["cover"] = {"r": "APPROVED_DOCUMENT"}
    return scripts


def test_a_twenty_field_form_with_a_250_option_select_is_classified_in_bounded_batches() -> None:
    fields = twenty_fields()
    assert len(fields) == 20
    provider = Jev(twenty_scripts())
    r = router(provider)
    form = ApplicationForm(url="https://synthetic.test/apply", fields=fields)
    report = r.classify_form(form, document_id="twenty", schema_hints=GREENHOUSE_HINT)
    requests = provider.classifications()
    assert len(requests) >= 2
    assert report.batches == len(requests) == report.provider_calls
    assert max(provider.sizes) <= TARGET < BOUND  # packed with the 15% margin
    # Every batch carries the same form header and whole-form context.
    assert all(request["state"] == requests[0]["state"] for request in requests)
    state = requests[0]["state"]
    assert state["version"] == "full-form-routing-v13"
    assert state["schema_prior_untrusted"] == GREENHOUSE_HINT
    assert [state["fields"][f"f{i}"]["field_id"] for i in range(20)] == [f.id for f in fields]
    menu = state["fields"]["f4"]
    assert (len(menu["options"]), menu["option_count"], menu["options_note"], menu["option_shape"]) == (
        40, 250, "… and 210 more", "dial_codes")
    # The batches are consecutive fields and together ask about every field exactly once.
    asked = [sorted(int(key[1:]) for key in request["questions"] if key.startswith("r"))
             for request in requests]
    assert [i for batch in asked for i in batch] == list(range(20))
    assert all(batch == list(range(batch[0], batch[-1] + 1)) for batch in asked)
    # The report merges the batches in form order; every field is decided and typed.
    assert [d.field_id for d in report.fields] == [f.id for f in fields]
    assert all(d.proposed_route is not None for d in report.fields)
    assert all(d.semantic_type is not SemanticType.UNKNOWN for d in report.fields)
    assert report.options_per_field == 40
    assert report.field("phone_country").semantic_type is SemanticType.PHONE
    assert report.field("yes_no_1").semantic_type is SemanticType.CUSTOM_BOOLEAN
    assert report.field("why").route is FieldRoute.WRITER
    # A cached observation keeps its batch count but reports no new calls.
    cached = r.classify_form(form, document_id="twenty", schema_hints=GREENHOUSE_HINT)
    assert (cached.batches, cached.provider_calls) == (report.batches, 0)


def appspace_fields() -> list[ApplicationField]:
    """The Appspace shape from pilot 7: 13 required fields and two long menus."""
    yes_no = labels("Yes", "No")
    countries = [FieldOption(value=f"country-{i:03d}-{name.lower().replace(' ', '-')}-residence",
                             label=f"{name} (country or region of residence #{i})")
                 for i, name in enumerate(COUNTRIES * 3)]
    return [
        observed("first_name", "First Name", ControlType.TEXT),
        observed("last_name", "Last Name", ControlType.TEXT),
        observed("email", "Email", ControlType.TEXT, input_type="email"),
        observed("phone", "Phone", ControlType.TEXT, input_type="tel"),
        observed("phone_country", "Country / Phone", ControlType.SELECT, dial_codes(240)),
        observed("country", "Country", ControlType.SELECT, countries[:240]),
        observed("resume", "Resume/CV", ControlType.FILE),
        observed("linkedin", "LinkedIn Profile", ControlType.TEXT),
        observed("authorized", "Are you legally authorized to work in the United States?",
                 ControlType.SELECT, yes_no),
        observed("sponsorship", "Will you now or in the future require visa sponsorship?",
                 ControlType.SELECT, yes_no),
        observed("source", "How did you hear about this job?", ControlType.SELECT,
                 labels("LinkedIn", "Company website", "Other")),
        observed("reside", "Do you currently reside in the US?", ControlType.SELECT, yes_no),
        observed("why", "Why do you want to join Appspace?", ControlType.TEXTAREA),
    ]


def test_the_appspace_form_is_no_longer_lost_to_the_request_bound(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    fields = appspace_fields()
    provider = Jev({"reside": {"s": "LOCATION"}, "why": {"r": "WRITER", "n": "prose",
                    "s": "CUSTOM_LONG_TEXT", "u": "HISTORICAL_OR_CONTEXTUAL"},
                    "resume": {"r": "APPROVED_DOCUMENT"}})
    r = router(provider)
    # The old request, which listed every option of every field, was over the bound even
    # for the first field alone: every field was held.
    old = DecisionRequest(model=r.decisions.model, questions=r._questions(0, fields[0]), state={
        "version": "full-form-routing-v12", "observation": "0" * 64,
        "schema_prior_untrusted": GREENHOUSE_HINT,
        "fields": {f"f{i}": field_data(f, max_options=10_000) for i, f in enumerate(fields)}})
    assert len(old.body()) > BOUND
    form = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=fields),
                      document_id="appspace", schema_hints=GREENHOUSE_HINT)
    report = r.report_for(form)
    assert report is not None and report.batches >= 2
    assert max(provider.sizes) <= TARGET
    assert all(d.proposed_route is not None and "bounded context" not in d.reason
               for d in report.fields)
    for field_id in ("first_name", "last_name", "email", "phone"):
        assert report.field(field_id).route is FieldRoute.COPY_KNOWN
        assert report.field(field_id).profile_copy_allowed
    ctx = PacketContext(form=form, candidate=fictional_candidate, job=mock_job, application=Application(
        id="app-appspace", request_id="request-appspace", job_id=mock_job.id,
        candidate_id=fictional_candidate.id, state=ApplicationState.INSPECTING, version=1,
        created_at="2026-09-24T00:00:00Z", updated_at="2026-09-24T00:00:00Z"))
    packet = asyncio.run(DynamicPacketResolver(r.decisions, router=r).resolve(ctx))
    assert ctx.problems(packet) == []
    identity = fictional_candidate.identity
    copied = {a.field_id: a for a in packet.answers if a.provenance.source is AnswerSource.PROFILE_IDENTITY}
    assert copied["first_name"].value.text == identity.first_name
    assert copied["last_name"].value.text == identity.last_name
    assert copied["email"].value.text == identity.email
    assert "phone" in copied
    assert [a.provenance.source for a in packet.answers if a.field_id == "resume"] == [AnswerSource.RESUME]


class OneRequest(AIFormRouter):
    """Packs every field into one request, so the bound check has to split it."""

    def _pack(self, sizes: list[int], header: int, target: int) -> list[list[int]]:
        return [list(range(len(sizes)))]


def test_a_request_that_still_exceeds_the_bound_is_halved_down_to_single_fields() -> None:
    fields = twenty_fields()
    provider = Jev(twenty_scripts())
    r = router(provider, cls=OneRequest)
    r.batch_size = 32
    report = r.classify_form(ApplicationForm(url="https://synthetic.test/apply", fields=fields),
                             document_id="halved", schema_hints=GREENHOUSE_HINT)
    requests = provider.classifications()
    assert len(requests) >= 3 and max(provider.sizes) <= BOUND
    asked = [sorted(int(key[1:]) for key in request["questions"] if key.startswith("r"))
             for request in requests]
    assert [i for batch in asked for i in batch] == list(range(20))  # halves, in form order
    assert report.batches == len(requests)
    assert all(d.proposed_route is not None for d in report.fields)


def test_a_single_field_over_the_bound_is_held_without_a_provider_call() -> None:
    fields = [observed(f"q{i}", f"Custom question {i}?", ControlType.TEXT) for i in range(3)]
    provider = Jev()
    r = router(provider, budget=CallBudget(max_request_bytes=4000))
    report = r.classify_form(ApplicationForm(url="https://synthetic.test/apply", fields=fields),
                             document_id="tiny")
    assert provider.requests == []
    assert report.batches == 3 and report.provider_calls == 0
    assert [d.field_id for d in report.fields] == ["q0", "q1", "q2"]
    assert all(d.route is FieldRoute.AMBIGUOUS and d.reason == "AI request exceeds the bounded context size"
               for d in report.fields)


@pytest.mark.parametrize("menus,listed", [(12, 10), (40, 0)])
def test_a_form_of_long_menus_lists_fewer_options_in_the_shared_context(menus: int, listed: int) -> None:
    options = [FieldOption(value=f"school-{i:03d}", label=f"University of Somewhere, campus number {i}")
               for i in range(200)]
    fields = [observed(f"menu{i}", f"Which campus did you attend for program {i}?", ControlType.SELECT,
                       options) for i in range(menus)]
    provider = Jev({f"menu{i}": {"s": "CUSTOM_SELECT"} for i in range(menus)})
    r = router(provider)
    report = r.classify_form(ApplicationForm(url="https://synthetic.test/apply", fields=fields),
                             document_id=f"menus-{menus}")
    assert report.options_per_field == listed
    requests = provider.classifications()
    assert max(provider.sizes) <= TARGET
    menu = requests[0]["state"]["fields"]["f0"]
    assert (len(menu["options"]), menu["option_count"], menu["options_note"], menu["option_shape"]) == (
        listed, 200, f"… and {200 - listed} more", "other")
    assert all(d.semantic_type is SemanticType.CUSTOM_SELECT for d in report.fields)
    assert report.batches == len(requests) >= 2


OFFSETS = ([f"-{h:02d}:00" for h in range(12, 0, -1)] + [f"+{h:02d}:00" for h in range(0, 15)]
           + ["+03:30", "+04:30", "+05:30", "+05:45", "+06:30", "+09:30", "+10:30"])


@pytest.mark.parametrize("zone", ["(UTC{}) Zone {}", "(GMT{}) Zone {}", "UTC {} Zone {}"])
def test_a_long_time_zone_menu_is_not_dial_codes(zone: str) -> None:
    options = labels(*(zone.format(offset, i) for i, offset in enumerate(OFFSETS * 2)))
    assert option_shape(options) == "other"


def test_a_broken_emoji_in_page_text_is_sent_as_encodable_text() -> None:
    # A lone surrogate (half an emoji) cannot be encoded as UTF-8; field data lists it as "?"
    # so the request body can be built. (Core's field fingerprint still fails on it; see the
    # WP10 report.)
    menu = observed("pick", "Tell us a fun fact \ud83d", ControlType.SELECT, labels("Yes \ud83d", "No"))
    data = field_data(menu)
    assert data["question"] == "Tell us a fun fact ?"
    assert data["options"][0]["label"] == "Yes ?"
    json.dumps(data, ensure_ascii=False).encode()  # encodable
