from __future__ import annotations

import json
from pathlib import Path

from interviewmaxxing_browser.schema_catalog import SchemaCatalog


def _map(path: Path, *, backend: str = "ashby", question: str = "First name") -> None:
    path.write_text(json.dumps({"backend": backend, "schema_version": "1",
        "observed": {"field_patterns": [{"label": question}]},
        "inferred": {"arbitrary_script": "never import this"},
        "observed_samples": [{"evidence_private": "/private/not-a-model-input"}]}))


def test_catalog_refreshes_atomically_replaced_maps_and_protects_cached_copy(tmp_path):
    path = tmp_path / "ashby.json"
    _map(path)
    catalog = SchemaCatalog(tmp_path)
    first = catalog.hint("ashby", "https://jobs.ashbyhq.com/example")
    assert first and first["field_patterns"] == [{"label": "First name"}]
    assert "inferred" not in first and "observed_samples" not in first
    first["field_patterns"].clear()
    assert catalog.hint("ashby", "https://jobs.ashbyhq.com/example")["field_patterns"]
    updated = tmp_path / "replace.json"
    _map(updated, question="Why do you want this role?")
    updated.replace(path)
    second = catalog.hint("ashby", "https://jobs.ashbyhq.com/example")
    assert second and second["map_sha256"] != first["map_sha256"]
    assert second["field_patterns"] == [{"label": "Why do you want this role?"}]


def test_invalid_missing_mismatched_and_oversized_priors_are_ignored(tmp_path):
    catalog = SchemaCatalog(tmp_path)
    assert catalog.hint("../secret", "https://example.test") is None
    assert catalog.hint("ashby", "file:///private/secret") is None
    assert catalog.hint("absent", "https://example.test") is None
    path = tmp_path / "ashby.json"
    _map(path, backend="greenhouse")
    assert catalog.hint("ashby", "https://example.test") is None
    path.write_text("{broken")
    assert catalog.hint("ashby", "https://example.test") is None
    _map(path, question="x" * 520_000)
    assert catalog.hint("ashby", "https://example.test") is None


def test_large_hints_keep_only_version_identity(tmp_path):
    _map(tmp_path / "ashby.json", question="x" * 30_000)
    hint = SchemaCatalog(tmp_path).hint("ashby", "https://example.test")
    assert hint and hint["authority"] == "untrusted_prior_only"
    assert "detail_omitted" in hint and "field_patterns" not in hint


def test_exact_url_index_supplies_prior_for_employer_domain(tmp_path):
    _map(tmp_path / "ashby.json")
    (tmp_path / "_url_index.json").write_text(json.dumps({
        "https://example.test/job/17": "ashby"}))
    catalog = SchemaCatalog(tmp_path)
    hint = catalog.hint("generic", "https://example.test/job/17?utm_source=linkedin")
    assert hint and hint["backend"] == "ashby"
    assert catalog.hint("generic", "https://different.test/job/17") is None
