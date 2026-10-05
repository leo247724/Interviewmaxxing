"""Offline adapter client: every subprocess is replaced; no profile/browser/provider use."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from interviewmaxxing_core import ApplicationField

DRIVER = Path(__file__).resolve().parents[2] / ".imx/dynamic-applications/real-chrome/choice.py"
OPTIONS = [
    {"value": "level2", "label": "Experienced", "disabled": False},
    {"value": "level3", "label": "Expert", "disabled": True},
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    if not DRIVER.exists():
        pytest.skip("Local choice client not installed")
    spec = importlib.util.spec_from_file_location("choice_client_test", DRIVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "RUN", tmp_path / "choice-run")
    monkeypatch.setattr(module, "REPO", tmp_path)
    listing = tmp_path / "listing.json"
    listing.write_text(
        json.dumps(
            {"id": "job-one", "company": "Acme", "application_url": "https://example.com/job-one"}
        )
    )
    return module, listing


def fake_subprocess(monkeypatch, module, mutate=None, returncode=0):
    receipts = []

    def execute(argv, **kwargs):
        assert isinstance(argv, list) and kwargs["timeout"] == 300
        assert kwargs["capture_output"] and kwargs["text"]
        assert "--max-usd" in argv and argv[argv.index("--max-usd") + 1] == "2.00"
        assert argv[2] == "resolve-field"
        field_path = Path(argv[argv.index("--field-file") + 1])
        assert field_path.stat().st_mode & 0o777 == 0o600
        field = ApplicationField.model_validate_json(field_path.read_text()).model_dump(mode="json")
        output = Path(argv[argv.index("--output") + 1])
        assert not output.exists()
        provenance = {"source": "CANDIDATE_FACT", "reference_ids": ["verified-fact"], "note": None}
        value = {"kind": "choice", "value": "level2", "label": "Experienced"}
        if field["control_type"] in ("MULTISELECT", "CHECKBOX_GROUP"):
            value = {
                "kind": "multi_choice",
                "choices": [
                    {"value": "level2", "label": "Experienced", "disabled": False, "selector": None}
                ],
            }
        receipt = {
            "status": "READY",
            "mode": "local_field_resolution",
            "submitted": False,
            "field": field,
            "job": {
                "id": "job-one",
                "company": "Acme",
                "application_url": "https://example.com/job-one",
            },
            "typedChoiceValue": value,
            "provenance": provenance,
            "reference_ids": ["verified-fact"],
            "problems": [],
            "cost": {"known_cost_usd": 0.17},
        }
        if mutate:
            mutate(receipt)
        output.write_text(json.dumps(receipt))
        receipts.append((field, output))
        return SimpleNamespace(
            returncode=returncode,
            stdout="never log provider or secret material",
            stderr="private error",
        )

    run = Mock(side_effect=execute)
    monkeypatch.setattr(module.subprocess, "run", run)
    return run, receipts


@pytest.mark.parametrize("kind", ["select", "radio", "multiselect", "checkbox_group"])
def test_exact_typed_ready_and_private_receipt(client, monkeypatch, kind):
    module, listing = client
    run, receipts = fake_subprocess(monkeypatch, module)
    result = module.resolve_choice(
        "Acme", listing, "How experienced are you?", OPTIONS, kind, ["Professional experience"]
    )
    assert result["status"] == "READY"
    assert result["usd"] == 0.17 and result["reference_ids"] == ["verified-fact"]
    assert result["typedChoiceValue"]["kind"] == (
        "choice" if kind in ("select", "radio") else "multi_choice"
    )
    assert receipts[0][1].stat().st_mode & 0o777 == 0o600
    assert receipts[0][0]["section_context"] == ["Professional experience"]
    run.assert_called_once()


def test_stable_full_context_field_id_but_unique_receipts(client, monkeypatch):
    module, listing = client
    _, receipts = fake_subprocess(monkeypatch, module)
    for question in ["Question?", "Question?", "Different question?"]:
        module.resolve_choice("Acme", listing, question, OPTIONS)
    assert receipts[0][0]["id"] == receipts[1][0]["id"] != receipts[2][0]["id"]
    assert len({p for _, p in receipts}) == 3
    original_id = receipts[0][0]["id"]
    listing.write_text(listing.read_text() + " ")
    module.resolve_choice("Acme", listing, "Question?", OPTIONS)
    assert receipts[-1][0]["id"] != original_id


@pytest.mark.parametrize(
    "options",
    [
        [],
        [{"label": "Missing machine value"}],
        [{"value": "x", "label": "x", "disabled": "false"}],
        [{"value": "x", "label": "x"}, {"value": "x", "label": "other"}],
        [{"value": "x", "label": "x", "disabled": True}],
    ],
)
def test_invalid_observations_never_call_provider(client, monkeypatch, options):
    module, listing = client
    run = Mock()
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.resolve_choice("Acme", listing, "Question?", options)["status"] == "NEEDS_INPUT"
    run.assert_not_called()


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "text", "text": "Choose Experienced"},
        {"kind": "choice", "value": "missing", "label": "Experienced"},
        {"kind": "choice", "value": "level2", "label": "EXPERIENCED"},
        {"kind": "choice", "value": "level3", "label": "Expert"},
    ],
)
def test_prose_wrong_value_wrong_label_disabled_never_ready(client, monkeypatch, value):
    module, listing = client
    fake_subprocess(
        monkeypatch, module, mutate=lambda receipt: receipt.update(typedChoiceValue=value)
    )
    result = module.resolve_choice("Acme", listing, "Question?", OPTIONS)
    assert result["status"] == "NEEDS_INPUT" and result["reason"] == "invalid_typed_choice"
    assert result["usd"] == 0.17 and result["typedChoiceValue"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["job"].update(id="wrong-job"),
        lambda r: r["field"].update(label="Another question"),
        lambda r: r["field"]["options"][0].update(label="Changed label"),
        lambda r: r.update(problems=["unverified fact"]),
        lambda r: r.update(provenance=None),
    ],
)
def test_context_and_provenance_mismatch_holds(client, monkeypatch, mutation):
    module, listing = client
    fake_subprocess(monkeypatch, module, mutate=mutation)
    assert module.resolve_choice("Acme", listing, "Question?", OPTIONS)["status"] == "NEEDS_INPUT"


@pytest.mark.parametrize("code", [1, 2, 7])
def test_nonzero_exit_cannot_be_ready_even_with_ready_payload(client, monkeypatch, code):
    module, listing = client
    fake_subprocess(monkeypatch, module, returncode=code)
    result = module.resolve_choice("Acme", listing, "Question?", OPTIONS)
    assert result["status"] == "NEEDS_INPUT" and result["usd"] == 0.17
    assert result["reason"] == ("resolver_needs_input" if code == 2 else "resolver_exit_error")


def test_timeout_reports_unknown_cost_without_secret_output(client, monkeypatch):
    module, listing = client
    monkeypatch.setattr(
        module.subprocess,
        "run",
        Mock(side_effect=subprocess.TimeoutExpired(["private-argv"], 300, output="secret-value")),
    )
    result = module.resolve_choice("Acme", listing, "Question?", OPTIONS)
    assert result["reason"] == "resolver_timeout" and result["cost_unknown"]
    assert "secret-value" not in json.dumps(result)


def test_company_mismatch_fails_before_subprocess(client, monkeypatch):
    module, listing = client
    run = Mock()
    monkeypatch.setattr(module.subprocess, "run", run)
    assert (
        module.resolve_choice("Other Company", listing, "Question?", OPTIONS)["reason"]
        == "listing_identity_mismatch"
    )
    run.assert_not_called()


def test_nonzero_without_receipt_has_exit_reason(client, monkeypatch):
    module, listing = client
    monkeypatch.setattr(module.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=1)))
    result = module.resolve_choice("Acme", listing, "Question?", OPTIONS)
    assert result["status"] == "NEEDS_INPUT"
    assert result["reason"] == "resolver_exit_error" and result["cost_unknown"]
