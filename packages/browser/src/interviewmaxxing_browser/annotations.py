"""Trusted boundary around optional, untrusted semantic annotations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, Protocol

from interviewmaxxing_core import ApplicationForm

from .normalize import PageModel


class FormAnnotator(Protocol):
    def annotate(self, form: ApplicationForm, *, document_id: str,
                 schema_hints: dict[str, Any] | None = None) -> ApplicationForm: ...


SchemaHintLoader = Callable[[str, str], dict[str, Any] | None]


def semantic_only(original: ApplicationForm, annotated: ApplicationForm) -> ApplicationForm:
    """Models may change field semantics; the browser owns everything else."""
    before = original.model_dump(mode="json", exclude={"inspected_at"})
    after = annotated.model_dump(mode="json", exclude={"inspected_at"})
    for payload in (before, after):
        for item in payload["fields"]:
            item.pop("semantic_type", None)
    if before != after:
        raise ValueError("semantic annotation changed browser-owned form properties")
    return original.model_copy(update={"fields": [
        field.model_copy(update={"semantic_type": replacement.semantic_type})
        for field, replacement in zip(original.fields, annotated.fields, strict=True)
    ]})


def observation_signature(model: PageModel, *, include_values: bool = False) -> str:
    """Includes constraints and bindings omitted by canonical question fingerprints.

    Value/validation updates caused by our own fill are excluded from structural
    comparisons; the provider-await guard includes them as well.
    """
    controls = []
    for control in model.snapshot.controls:
        item = control.model_dump(mode="json")
        if not include_values:
            for key in ("value", "checked", "files", "has_value", "invalid", "error_message",
                        "adjacent_errors"):
                item.pop(key, None)
            for key in ("described", "legend_described", "group_described"):
                item[key] = [part for part in item[key] if not part["error"]]
            for option in item["options"]:
                option.pop("selected", None)
            if item.get("aria"):
                for key in ("value", "expanded", "visible"):
                    item["aria"].pop(key, None)
                for option in item["aria"].get("options", []):
                    option.pop("selected", None)
        controls.append(item)
    identity = model.inspection.job_identity
    payload = {
        "url": model.snapshot.url,
        # Employer/job context can change in the same document while the form's
        # questions and selectors remain identical. It still invalidates routing.
        "title": model.snapshot.title,
        "headings": [heading.model_dump(mode="json") for heading in model.snapshot.headings],
        "meta": model.snapshot.meta.model_dump(mode="json"),
        "structured_data": model.snapshot.ld_json,
        "job_identity": identity.model_dump(mode="json", exclude={"observed_at"})
        if identity is not None else None,
        "kind": model.inspection.kind.value,
        "controls": controls,
        "forms": [form.model_dump(mode="json") for form in model.snapshot.forms],
        "buttons": [button.model_dump(mode="json") for button in model.snapshot.buttons],
        "step": model.snapshot.step.model_dump(mode="json") if model.snapshot.step else None,
        "form_index": model.form_index,
        "form_selector": model.form_selector,
        "actions": {"final": model.form.is_final_step,
                    "next": model.form.next_selector,
                    "submit": model.form.submit_selector} if model.form else None,
        "bindings": {key: {
            "selector": binding.selector, "control_type": binding.control_type.value,
            "option_selectors": dict(binding.option_selectors),
            "label_selectors": dict(binding.label_selectors),
        } for key, binding in model.bindings.items()},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
