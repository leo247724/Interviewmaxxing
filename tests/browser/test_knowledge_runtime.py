import json
from pathlib import Path

import pytest

from interviewmaxxing_browser.ai.knowledge_runtime import build_knowledge_store, connection_settings
from interviewmaxxing_browser.ai.providers import AIHold, CallBudget
from interviewmaxxing_cli.dynamic import DynamicOptions
from interviewmaxxing_core import LocalPaths
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_service.config import ConfigError, ServiceConfig


def test_private_configuration_errors_never_echo_connection_data(tmp_path):
    file = tmp_path / "connection.json"
    file.write_text(json.dumps({"host": "db.example.test", "dbname": "postgres", "user": "private",
                               "password": "secret-do-not-log", "sslmode": "disable"}))
    with pytest.raises(ValueError) as caught:
        connection_settings(file)
    assert "secret-do-not-log" not in str(caught.value)
    settings = json.loads(file.read_text())
    settings["sslmode"] = "require"
    file.write_text(json.dumps(settings))
    assert connection_settings(file)["connect_timeout"] == 10


def test_retrieval_requires_explicit_ai_and_absolute_path(tmp_path):
    with pytest.raises(ValueError):
        DynamicOptions(rag_connection_file=tmp_path / "db.json").validate()
    with pytest.raises(ValueError):
        DynamicOptions(ai_routing=True, env_file=tmp_path / "env.local",
            writer_model="anthropic/claude-opus-5.5", rag_connection_file=Path("relative")).validate()
    with pytest.raises(ConfigError):
        ServiceConfig(paths=LocalPaths.from_env(home=tmp_path),
            allowed_origin="http://127.0.0.1:4317", rag_connection_file=tmp_path / "db.json")


def test_exhausted_runtime_budget_stops_embedding_before_http(tmp_path, monkeypatch):
    file = tmp_path / "db.json"
    file.write_text(json.dumps({"host": "db.example.test", "dbname": "postgres",
                               "user": "example", "password": "fictional-secret"}))
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kw: pytest.fail("HTTP attempted"))
    budget = CallBudget(max_calls=1)
    budget.reserve(b"already used for classification", .001)
    store = build_knowledge_store(ApiKey("fictional-key", source="test"), file, budget=budget)
    with pytest.raises(AIHold, match="budget exhausted"):
        store._embedder.embed(["A query"])
    assert budget.calls == 1


def test_failed_embedding_is_charged_to_runtime_budget(tmp_path, monkeypatch):
    import urllib.error

    file = tmp_path / "db.json"
    file.write_text(json.dumps({"host": "db.example.test", "dbname": "postgres",
                               "user": "example", "password": "fictional-secret"}))
    def unavailable(*args, **kwargs):
        raise urllib.error.URLError("transport failed")
    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    budget = CallBudget()
    store = build_knowledge_store(ApiKey("fictional-key", source="test"), file, budget=budget)
    with pytest.raises(RuntimeError):
        store._embedder.embed(["A query"])
    assert budget.calls == 1 and len(budget.metadata()) == 1
    assert budget.metadata()[0]["purpose"] == "knowledge_embedding"
    assert budget.metadata()[0]["status"] == "ERROR"
    assert budget.metadata()[0]["cost_usd"] is None
