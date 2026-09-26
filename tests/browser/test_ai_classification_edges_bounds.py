"""Adversarial edges of the bounded full-form requests (WP10 item 2).

Random synthetic forms with non-ASCII labels, emoji, long help texts and unicode schema
priors check the packing invariants: no body over the bound, multi-field requests under
85% of it, one shared state, every field listed in form order, and a field that cannot
fit held with its reason rather than lost. The option cap, the shape hint and the
provider/budget failure paths are probed at their boundaries.
"""
from __future__ import annotations

import json
import random
from typing import Any

import pytest

from interviewmaxxing_browser.ai import AIFormRouter, BoundedDecisions, CallBudget
from interviewmaxxing_browser.ai.classification import (
    MAX_FIELD_OPTIONS,
    _json_bytes,
    field_data,
    option_shape,
)
from interviewmaxxing_core import (
    ApplicationField,
    ApplicationForm,
    ControlType,
    FieldOption,
    SemanticType,
)
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import DecisionRequest, HttpResponse, JevClient

BOUND_REASON = "AI request exceeds the bounded context size"
DEFAULTS = {"r": "COPY_KNOWN", "n": "literal", "u": "APPLICANT_CURRENT", "s": "CUSTOM_TEXT",
            "d": "APPLICATION_ATTACHMENT"}


class Jev:
    """Scripted Jev recording raw bodies; ``fail_on`` lists 1-based calls answered HTTP 500."""

    def __init__(self, fail_on: tuple[int, ...] = ()) -> None:
        self.fail_on = set(fail_on)
        self.requests: list[dict[str, Any]] = []
        self.bodies: list[bytes] = []

    def asked(self) -> list[list[int]]:
        """The field indices each request asked about, in request order."""
        return [sorted(int(key[1:]) for key in request["questions"] if key[0] == "r")
                for request in self.requests]

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        self.bodies.append(body)
        if len(self.requests) in self.fail_on:
            return HttpResponse(500, {}, b"{}")
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            criteria = list(question["criteria"])
            choice = DEFAULTS[name[0]]
            answers[name] = {"type": "choice", "choice": choice, "confidence": 1.0,
                             "probabilities": {c: float(c == choice) for c in criteria}}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


def router(provider: Jev, budget: CallBudget | None = None, batch_size: int = 16) -> AIFormRouter:
    return AIFormRouter(BoundedDecisions(JevClient(ApiKey("synthetic-test", source="fixture"),
        transport=provider, max_attempts=1), budget or CallBudget(max_calls=10_000, max_usd=1_000.0)),
        batch_size=batch_size)


def menu(field_id: str, count: int, *, label: str = "Pick one", semantic: SemanticType = SemanticType.CUSTOM_SELECT,
         option: str = "Option {i} for {id}") -> ApplicationField:
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label, semantic_type=semantic,
        control_type=ControlType.SELECT, required=True,
        options=[FieldOption(value=f"v{i}", label=option.format(i=i, id=field_id)) for i in range(count)])


def text(field_id: str, label: str = "Custom question?", *, help_text: str | None = None) -> ApplicationField:
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label, help_text=help_text,
        semantic_type=SemanticType.CUSTOM_TEXT, control_type=ControlType.TEXT, required=True)


def form(*fields: ApplicationField) -> ApplicationForm:
    return ApplicationForm(url="https://synthetic.test/apply", fields=list(fields))


def labels(*items: str) -> list[FieldOption]:
    return [FieldOption(value=f"v{i}", label=label) for i, label in enumerate(items)]


# --- random forms -------------------------------------------------------------------------

WORDS = ["Résumé", "Straße", "日本語の質問", "emoji 🚀✨", "Ünïcödé", "naïve café", "Ελληνικά", "עברית",
         "حقل", "long…", "plain", "question", "experience", "🇺🇸", "Ω≈ç√", "“quoted”", "tab\there"]
CONTROLS = [ControlType.TEXT, ControlType.TEXTAREA, ControlType.SELECT, ControlType.RADIO,
            ControlType.MULTISELECT, ControlType.CHECKBOX_GROUP, ControlType.CHECKBOX, ControlType.FILE]
CHOICE = {ControlType.SELECT, ControlType.RADIO, ControlType.MULTISELECT, ControlType.CHECKBOX_GROUP}
SEMANTICS = [SemanticType.CUSTOM_TEXT, SemanticType.CUSTOM_SELECT, SemanticType.UNKNOWN,
             SemanticType.FIRST_NAME, SemanticType.EMAIL, SemanticType.CUSTOM_BOOLEAN]


def words(rng: random.Random, count: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(count))


def random_form(rng: random.Random) -> ApplicationForm:
    fields = []
    for i in range(rng.randint(1, 60)):
        control = rng.choice(CONTROLS)
        options = None
        if control in CHOICE:
            options = [FieldOption(value=f"v{j}", disabled=rng.random() < 0.05,
                                   label=f"{words(rng, 2)} +{j}" if rng.random() < 0.3 else words(rng, rng.randint(1, 6)))
                       for j in range(rng.choice([1, 2, 5, 39, 40, 41, 120, 300]))]
        fields.append(ApplicationField(id=f"f{i}", selector=f"#f{i}", label=words(rng, rng.randint(1, 12)),
            control_type=control, semantic_type=rng.choice(SEMANTICS), options=options,
            required=rng.random() < 0.6,
            help_text=words(rng, rng.randint(0, 400)) if rng.random() < 0.3 else None,
            section_context=[words(rng, 3) for _ in range(rng.randint(0, 3))]))
    return form(*fields)


@pytest.mark.parametrize("seed", range(0, 60, 6))
def test_random_unicode_forms_keep_every_packing_invariant(seed: int) -> None:
    for offset in range(6):
        rng = random.Random(seed + offset)
        observed = random_form(rng)
        bound = rng.choice([15_000, 30_000, 60_000])
        hints = ({"backend": "synthétique", "notes": [words(rng, 20) for _ in range(rng.randint(0, 60))]}
                 if rng.random() < 0.5 else None)
        provider = Jev()
        r = router(provider, CallBudget(max_calls=10_000, max_usd=1_000.0, max_request_bytes=bound),
                   batch_size=rng.choice([1, 16, 32]))
        report = r.classify_form(observed, document_id=f"random-{seed + offset}", schema_hints=hints)
        target = int(bound * 0.85)
        assert [d.field_id for d in report.fields] == [f.id for f in observed.fields]
        held = [i for i, d in enumerate(report.fields) if d.proposed_route is None]
        assert all(report.fields[i].reason == BOUND_REASON for i in held)
        asked = provider.asked()
        # Every field is asked about exactly once, or held on its own for the bound.
        assert sorted([i for batch in asked for i in batch] + held) == list(range(len(observed.fields)))
        assert report.batches == len(asked) + len(held)
        for batch, body in zip(asked, provider.bodies, strict=True):
            assert len(body) <= bound
            assert len(batch) <= r.batch_size
            assert batch == list(range(batch[0], batch[-1] + 1))
            if len(batch) > 1:
                assert len(body) <= target
        # One state for every request of the form, byte for byte.
        assert len({json.dumps(request["state"], ensure_ascii=False, sort_keys=True).encode()
                    for request in provider.requests}) <= 1
        # The shared context falls back only as far as it has to.
        listed = report.options_per_field
        assert listed in (40, 10, 0)
        if listed < 40:
            wider = 40 if listed == 10 else 10
            state = {"version": "full-form-routing-v14", "observation": report.context_hash,
                     "schema_prior_untrusted": hints or {},
                     "fields": {f"f{i}": field_data(f, max_options=wider) for i, f in enumerate(observed.fields)}}
            assert 2 * _json_bytes({"model": r.decisions.model, "questions": {}, "state": state}) > target
        for request in provider.requests:
            assert all(len(f["options"]) <= listed for f in request["state"]["fields"].values())


def test_the_size_estimate_is_conservative_by_at_most_two_bytes() -> None:
    rng = random.Random(7)
    observed = random_form(rng)
    r = router(Jev())
    state = {"version": "v", "observation": "ø" * 64, "schema_prior_untrusted": {"note": words(rng, 50)},
             "fields": {f"f{i}": field_data(f) for i, f in enumerate(observed.fields)}}
    questions = [r._questions(i, f) for i, f in enumerate(observed.fields)]
    sizes = [_json_bytes({k: q.model_dump(mode="json") for k, q in asked.items()}) for asked in questions]
    header = _json_bytes({"model": r.decisions.model, "questions": {}, "state": state})
    for start in range(0, len(questions), 3):
        batch = list(range(start, min(len(questions), start + rng.randint(1, 5))))
        body = DecisionRequest(model=r.decisions.model, state=state,
            questions={k: q for i in batch for k, q in questions[i].items()}).body()
        estimate = header + sum(sizes[i] for i in batch)
        assert 0 <= estimate - len(body) <= 2


# --- the option cap -----------------------------------------------------------------------

def test_forty_options_are_listed_whole_and_the_forty_first_brings_the_note_and_shape() -> None:
    options = [FieldOption(value="", label="Select…", disabled=True),
               *(FieldOption(value=f"v{i}", label=f"Choice {i}", disabled=i % 7 == 0) for i in range(1, 40))]
    forty = ApplicationField(id="forty", selector="#forty", label="Pick", control_type=ControlType.SELECT,
                             options=options)
    data = field_data(forty)
    assert len(data["options"]) == data["option_count"] == MAX_FIELD_OPTIONS
    assert data["options"][0] == {"value": "", "label": "Select…", "disabled": True}
    assert [o["disabled"] for o in data["options"]] == [o.disabled for o in options]
    assert "options_note" not in data and "option_shape" not in data
    more = forty.model_copy(update={"options": [*options, FieldOption(value="v40", label="Choice 40")]})
    data = field_data(more)
    assert len(data["options"]) == 40 and data["option_count"] == 41
    assert (data["options_note"], data["option_shape"]) == ("… and 1 more", "other")
    assert data["options"][-1]["label"] == "Choice 39"


def test_no_listed_options_keeps_the_count_note_and_shape() -> None:
    data = field_data(menu("codes", 3, option="Country {i} +{i}"), max_options=0)
    assert data["options"] == [] and data["option_count"] == 3
    assert (data["options_note"], data["option_shape"]) == ("… and 3 more", "dial_codes")
    plain = field_data(text("plain"), max_options=0)
    assert plain["options"] == [] and plain["option_count"] == 0 and "options_note" not in plain


# --- batch size and field-count bounds ---------------------------------------------------

@pytest.mark.parametrize("batch_size", [1, 32])
def test_batch_size_caps_fields_per_request_and_bytes_still_bound_it(batch_size: int) -> None:
    provider = Jev()
    r = router(provider, batch_size=batch_size)
    report = r.classify_form(form(*(text(f"q{i}") for i in range(40))), document_id=f"size-{batch_size}")
    asked = provider.asked()
    assert [i for batch in asked for i in batch] == list(range(40))
    assert max(len(batch) for batch in asked) <= batch_size
    if batch_size == 1:
        assert len(asked) == 40
    else:  # 32 short fields would be about 240 KB: the byte bound splits them first
        assert max(len(batch) for batch in asked) < 32
        assert max(len(body) for body in provider.bodies) <= int(CallBudget().max_request_bytes * 0.85)
    assert report.batches == len(asked) and all(d.proposed_route is not None for d in report.fields)


def test_a_form_at_the_field_bound_is_classified_and_one_more_field_holds_it_whole() -> None:
    provider = Jev()
    r = router(provider)
    at_bound = form(*(text(f"q{i}", f"Question {i}: ¿qué 🚀?") for i in range(100)))
    report = r.classify_form(at_bound, document_id="hundred")
    assert all(d.proposed_route is not None for d in report.fields)
    assert max(len(body) for body in provider.bodies) <= CallBudget().max_request_bytes
    calls = len(provider.requests)
    over = form(*at_bound.fields, text("q100"))
    held = r.classify_form(over, document_id="hundred-and-one")
    assert len(provider.requests) == calls
    assert (held.batches, held.provider_calls) == (0, 0)
    assert [d.field_id for d in held.fields] == [f.id for f in over.fields]
    assert {d.reason for d in held.fields} == {"Full form exceeds the field-count bound"}


def test_a_context_too_large_even_without_options_holds_every_field_in_order() -> None:
    hints = {"backend": "custom", "families": ["Größe ✓ " * 40 for _ in range(250)]}
    assert _json_bytes(hints) > CallBudget().max_request_bytes
    provider = Jev()
    fields = [menu("m0", 80), text("t1"), menu("m2", 300), text("t3")]
    report = router(provider).classify_form(form(*fields), document_id="huge", schema_hints=hints)
    assert provider.requests == []
    assert report.options_per_field == 0 and report.batches == 4 and report.provider_calls == 0
    assert [d.field_id for d in report.fields] == ["m0", "t1", "m2", "t3"]
    assert all(d.route.value == "AMBIGUOUS" and d.reason == BOUND_REASON for d in report.fields)


# --- provider and budget failures --------------------------------------------------------

def test_a_provider_failure_holds_only_its_own_batch() -> None:
    provider = Jev(fail_on=(2,))
    r = router(provider, CallBudget())
    report = r.classify_form(form(*(menu(f"m{i}", 3) for i in range(20))), document_id="failure")
    asked = provider.asked()
    assert len(asked) >= 3 and report.batches == report.provider_calls == len(asked)
    failed = set(asked[1])
    for i, decision in enumerate(report.fields):
        if i in failed:
            assert (decision.proposed_route, decision.reason) == (None, "Jev UNAVAILABLE")
        else:
            assert decision.proposed_route is not None
    assert report.cost_usd is None and report.unknown_cost_calls == 1


def test_call_budget_exhaustion_mid_form_holds_the_remaining_batches_in_order() -> None:
    provider = Jev()
    r = router(provider, CallBudget(max_calls=2))
    fields = [menu(f"m{i}", 3) for i in range(30)]
    report = r.classify_form(form(*fields), document_id="budget")
    asked = provider.asked()
    assert len(asked) == report.provider_calls == 2 and report.batches > 2
    sent = {i for batch in asked for i in batch}
    assert [d.field_id for d in report.fields] == [f.id for f in fields]
    for i, decision in enumerate(report.fields):
        if i in sent:
            assert decision.proposed_route is not None
        else:
            assert decision.reason == "AI call or cost budget exhausted"


def test_a_cache_hit_keeps_the_batches_and_the_listed_options() -> None:
    provider = Jev()
    r = router(provider)
    long_menus = form(*(menu(f"m{i}", 200, option="University of Somewhere, campus {i}") for i in range(12)))
    report = r.classify_form(long_menus, document_id="menus")
    calls = len(provider.requests)
    cached = r.classify_form(long_menus, document_id="menus")
    assert len(provider.requests) == calls
    assert (cached.batches, cached.options_per_field, cached.provider_calls) == (
        report.batches, report.options_per_field, 0)
    assert report.options_per_field == 10


# --- the shape hint ------------------------------------------------------------------------

LANGUAGES = ["English", "Spanish", "French", "German", "Italian", "Portuguese", "Russian",
             "Mandarin Chinese", "Cantonese", "Japanese", "Korean", "Arabic", "Hindi", "Bengali",
             "Punjabi", "Urdu", "Turkish", "Vietnamese", "Thai", "Dutch", "Swedish", "Norwegian",
             "Danish", "Finnish", "Polish", "Czech", "Greek", "Hebrew", "Hungarian", "Romanian",
             "Ukrainian", "Indonesian", "Malay", "Tagalog", "Swahili", "Persian", "Tamil", "Telugu",
             "Marathi", "Gujarati", "Catalan", "Croatian", "Serbian", "Slovak"]
CITIES = ["New York, NY", "Los Angeles, CA", "Chicago, IL", "Houston, TX", "Phoenix, AZ",
          "Philadelphia, PA", "San Antonio, TX", "San Diego, CA", "Dallas, TX", "Austin, TX",
          "Jacksonville, FL", "Columbus, OH", "Charlotte, NC", "Seattle, WA", "Denver, CO",
          "Boston, MA", "Portland, OR", "Atlanta, GA", "Miami, FL", "Minneapolis, MN",
          "London", "Paris", "Berlin", "Toronto", "Sydney", "Tokyo", "Madrid", "Dublin",
          "Amsterdam", "Lisbon", "Warsaw", "Prague", "Vienna", "Zurich", "Oslo", "Stockholm",
          "Copenhagen", "Helsinki", "Brussels", "Munich", "Milan", "Barcelona"]


@pytest.mark.parametrize("options", [
    labels(*(str(n) for n in range(60))),  # a count, not years
    labels(*(f"{n}+ years" for n in range(50))),  # "+" after a number is no dial code
    labels(*(f"${n * 5}k+" for n in range(50))),
    labels(*LANGUAGES),
    labels(*(f"{name} (native)" for name in LANGUAGES)),
    labels(*CITIES),
    labels(*(f"Europe/City{n}" for n in range(25)), *(f"America/City{n}" for n in range(25))),
    labels(*(code for code in ("en", "es", "fr", "de", "it", "pt", "ru", "zh", "ja", "ko", "ar", "hi",
        "bn", "pa", "ur", "tr", "vi", "th", "nl", "sv", "no", "da", "fi", "pl", "cs", "el", "he",
        "hu", "ro", "uk", "id", "ms", "tl", "sw", "fa", "ta", "te", "mr", "gu", "ca", "hr", "sr"))),
    labels(*(f"Afghan{n}" for n in range(45))),
])
def test_long_lists_that_are_not_a_known_shape_stay_other(options: list[FieldOption]) -> None:
    assert len(options) > 40
    assert option_shape(options) == "other"


@pytest.mark.parametrize("options,shape", [
    (labels(*(f"🇺🇳 {name}" for name in ("United States", "Canada", "Mexico", "France", "Germany",
        "Spain", "Italy", "India", "China", "Japan", "Brazil", "Kenya")), *(f"Region {n}" for n in range(30))),
     "countries"),
    (labels("United States of America", "Canada", "Mexico", "France", "Germany", "Spain", "Italy",
            "India", "Japan", *(f"Territory {n}" for n in range(35))), "countries"),
    (labels(*(f"(+{n}) Country {n}" for n in range(50))), "dial_codes"),
    ([FieldOption(value=f"+{n}", label=f"Country {n}") for n in range(50)], "dial_codes"),
    (labels(*(f"🏳 + {n}" for n in range(50))), "dial_codes"),
    (labels("Select a year", *(f" {year} " for year in range(2030, 1970, -1)), "Before 1970"), "years"),
])
def test_shapes_survive_flags_parentheses_and_long_names(options: list[FieldOption], shape: str) -> None:
    assert option_shape(options) == shape
