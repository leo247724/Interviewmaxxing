"""Typed enum adapter checks with actual packet contracts and mocked AI runtime."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from interviewmaxxing_core import (
    ApplicationField,
    ApplicationPacket,
    ChoiceValue,
    ControlType,
    FieldOption,
    MissingInput,
    MissingReason,
    MultiChoiceValue,
    PacketAnswer,
    Provenance,
    SemanticType,
    TextValue,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/rag_answers.py"


@pytest.fixture
def adapter():
    spec = importlib.util.spec_from_file_location("rag_field_adapter_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def observed():
    return ApplicationField(id="duration", label="How many years of paid media experience?",
                            control_type=ControlType.SELECT, selector="#observed-duration", required=True,
                            options=[FieldOption(value="level_2", label="5\u20137 years"),
                                     FieldOption(value="level_3", label="8\u201310 years"),
                                     FieldOption(value="", label="Select...")])


def install_runtime(monkeypatch, adapter, *, value=None, references=None, mutate_packet=None,
                    annotate=None, missing=False, source="CANDIDATE_FACT"):
    contexts = []
    router = Mock()
    router.annotate.side_effect = annotate or (lambda form, **_: form)
    router.report_for.return_value = None

    async def resolve(context):
        contexts.append(context)
        field = context.form.fields[0]
        answer = PacketAnswer(field_id=field.id, semantic_type=field.semantic_type,
                              value=value or ChoiceValue(value="level_2", label="5\u20137 years"),
                              provenance=Provenance(source=source,
                                                    reference_ids=references or ["fact.years_paid_media"]))
        packet = ApplicationPacket(application_id=context.application.id, job_id=context.job.id,
                                   candidate_id=context.candidate.id, form_url=context.form.url,
                                   form_step=context.form.step, form_fingerprint=context.form.fingerprint,
                                   answers=[] if missing else [answer],
                                   missing_inputs=[MissingInput.for_field(context.form, field,
                                                   reason=MissingReason.NO_ANSWER,
                                                   prompt="Need verified evidence")] if missing else [])
        return mutate_packet(packet) if mutate_packet else packet

    resolver = SimpleNamespace(resolve=resolve, writer=None, provider_usage=lambda: {"calls":0,"known_cost_usd":0.0},
                               retrieval_receipts=[{"fixture":"retrieval"}], narrative_traces=[])
    factory = Mock(return_value=(router, resolver))
    monkeypatch.setattr(adapter, "build_ai_runtime", factory)
    return contexts, factory, router


def run(adapter, candidate, job, field):
    return asyncio.run(adapter.resolve_field(candidate=candidate, job=job, field=field,
                       env_file=Path("unused.env"), connection_file=Path("unused.json"), max_usd=0.5))


@pytest.mark.parametrize("kind", [ControlType.SELECT, ControlType.RADIO, ControlType.MULTISELECT, ControlType.CHECKBOX_GROUP])
def test_ready_returns_exact_typed_choices_and_provenance(adapter, monkeypatch, fictional_candidate, mock_job, observed, kind):
    observed = observed.model_copy(update={"control_type":kind})
    multi = kind in (ControlType.MULTISELECT, ControlType.CHECKBOX_GROUP)
    value = (MultiChoiceValue(choices=[observed.options[0]]) if multi
             else ChoiceValue(value="level_2", label="5\u20137 years"))
    contexts, _, router = install_runtime(monkeypatch, adapter, value=value)
    result = run(adapter, fictional_candidate, mock_job, observed)
    assert result["status"] == "READY"
    assert result["typedChoiceValue"] == value.model_dump(mode="json")
    assert result["reference_ids"] == ["fact.years_paid_media"]
    assert result["provenance"]["source"] == "CANDIDATE_FACT"
    assert not result["submitted"] and "text" not in result
    assert result["problems"] == []
    assert contexts[0].candidate is fictional_candidate and contexts[0].job is mock_job
    assert contexts[0].form.fields[0].fingerprint == observed.fingerprint
    router.annotate.assert_called_once()
    assert result["retrieval"] == [{"fixture":"retrieval"}]


@pytest.mark.parametrize("value", [
    TextValue(text="Choose the first option"),
    ChoiceValue(value="invented", label="5\u20137 years"),
    ChoiceValue(value="level_2", label="5\u20137 YEARS"),
    ChoiceValue(value="", label="Select..."),
    MultiChoiceValue(choices=[FieldOption(value="level_2", label="5\u20137 years")]),
])
def test_never_guesses_or_normalizes_invalid_enum_value(adapter, monkeypatch, fictional_candidate, mock_job, observed, value):
    install_runtime(monkeypatch, adapter, value=value)
    result = run(adapter, fictional_candidate, mock_job, observed)
    assert result["status"] == "NEEDS_INPUT"
    assert result["typedChoiceValue"] is None
    assert result["provenance"] is None and result["reference_ids"] == []
    assert result["problems"]


@pytest.mark.parametrize("references", [["missing-fact"], ["fact.team_size"]])
def test_unknown_or_unverified_evidence_holds(adapter, monkeypatch, fictional_candidate, mock_job, observed, references):
    install_runtime(monkeypatch, adapter, references=references)
    result = run(adapter, fictional_candidate, mock_job, observed)
    assert result["status"] == "NEEDS_INPUT"
    assert any("fact" in p for p in result["problems"])


@pytest.mark.parametrize("key", ["candidate_id", "job_id", "application_id", "form_url"])
def test_other_context_packet_holds(adapter, monkeypatch, fictional_candidate, mock_job, observed, key):
    install_runtime(monkeypatch, adapter, mutate_packet=lambda packet: packet.model_copy(update={key:"other-context"}))
    result = run(adapter, fictional_candidate, mock_job, observed)
    assert result["status"] == "NEEDS_INPUT"
    assert result["typedChoiceValue"] is None


def test_disabled_option_holds(adapter, monkeypatch, fictional_candidate, mock_job, observed):
    observed.options[0] = observed.options[0].model_copy(update={"disabled": True})
    install_runtime(monkeypatch, adapter)
    assert run(adapter, fictional_candidate, mock_job, observed)["status"] == "NEEDS_INPUT"


def test_routing_must_preserve_observed_choices(adapter, monkeypatch, fictional_candidate, mock_job, observed):
    def annotate(form, **_):
        form.fields[0].options[1] = form.fields[0].options[1].model_copy(update={"label": "Invented option wording"})
        return form
    install_runtime(monkeypatch, adapter, annotate=annotate)
    result = run(adapter, fictional_candidate, mock_job, observed)
    assert result["status"] == "NEEDS_INPUT"
    assert observed.options[1].label == "8\u201310 years"
    assert any("Routing changed" in p for p in result["problems"])


def test_missing_grounding_returns_packet_without_choice(adapter, monkeypatch, fictional_candidate, mock_job, observed):
    install_runtime(monkeypatch, adapter, missing=True)
    result = run(adapter, fictional_candidate, mock_job, observed)
    assert result["status"] == "NEEDS_INPUT"
    assert result["packet"]["missing_inputs"][0]["reason"] == "NO_ANSWER"
    assert result["typedChoiceValue"] is None


def test_unsupported_control_never_calls_provider(adapter, monkeypatch, fictional_candidate, mock_job):
    factory = Mock()
    monkeypatch.setattr(adapter, "build_ai_runtime", factory)
    field = ApplicationField(id="text",label="Free text",control_type=ControlType.TEXT,selector="#text")
    with pytest.raises(ValueError, match="observed choice"):
        run(adapter, fictional_candidate, mock_job, field)
    factory.assert_not_called()


def test_cli_writes_private_enum_receipt_without_text_sidecar(adapter, monkeypatch, fictional_candidate, mock_job, observed, tmp_path):
    field_file = tmp_path/"field.json"
    field_file.write_text(observed.model_dump_json())
    output = tmp_path/"receipt.json"
    monkeypatch.setattr(adapter.LocalCandidateStore, "from_paths", lambda _: SimpleNamespace(load=lambda _: fictional_candidate))
    monkeypatch.setattr(adapter, "load_api_key", lambda **_: "test-only")
    monkeypatch.setattr(adapter, "build_knowledge_store", lambda *_: object())
    monkeypatch.setattr(adapter, "job_from_store", lambda *_, **__: mock_job)
    install_runtime(monkeypatch, adapter)
    monkeypatch.setattr(sys, "argv", ["rag_answers.py", "resolve-field", "--job-id", mock_job.id,
        "--field-file", str(field_file), "--output", str(output),
        "--env-file", str(tmp_path/"unused.env"), "--connection-file", str(tmp_path/"unused.json")])
    assert adapter.main() == 0
    receipt = json.loads(output.read_text())
    assert receipt["status"] == "READY" and receipt["job_source"] == "application_store"
    assert output.stat().st_mode & 0o777 == 0o600
    assert not output.with_suffix(".txt").exists()
    # Existing receipts cannot be overwritten or transformed into new cached answers.
    assert adapter.main() == 1


@pytest.mark.parametrize("choices", [[], [FieldOption(value="level_2", label="5\u20137 years"), FieldOption(value="level_2", label="5\u20137 years")]])
def test_required_multi_choice_cannot_be_empty_or_duplicate(adapter, monkeypatch, fictional_candidate, mock_job, observed, choices):
    observed = observed.model_copy(update={"control_type": ControlType.CHECKBOX_GROUP})
    install_runtime(monkeypatch, adapter, value=MultiChoiceValue(choices=choices))
    assert run(adapter, fictional_candidate, mock_job, observed)["status"] == "NEEDS_INPUT"


def test_saved_answer_for_other_job_is_rejected(adapter, monkeypatch, fictional_candidate, mock_job, observed):
    observed = observed.model_copy(update={"semantic_type": SemanticType.SALARY_EXPECTATION})
    install_runtime(monkeypatch, adapter, references=["sa.other_co_salary"], source="SAVED_ANSWER")
    result = run(adapter, fictional_candidate, mock_job, observed)
    assert result["status"] == "NEEDS_INPUT"
    assert any("another job" in problem for problem in result["problems"])


def test_existing_draft_still_returns_text_contract(adapter, monkeypatch, fictional_candidate, mock_job):
    install_runtime(monkeypatch, adapter, value=TextValue(text="Grounded fictional draft."))
    result = asyncio.run(adapter.prepare_draft(candidate=fictional_candidate, job=mock_job,
        question="Describe paid media experience", purpose="answer", env_file=Path("unused.env"),
        connection_file=Path("unused.json")))
    assert result["status"] == "READY"
    assert result["text"] == "Grounded fictional draft."
    assert result["mode"] == "local_draft" and result["purpose"] == "answer"
    assert "typedChoiceValue" not in result
