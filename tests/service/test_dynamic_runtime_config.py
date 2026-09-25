"""Service runtime configuration is explicit, local-only in preflight and preparation-only."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from interviewmaxxing_service import ConfigError, ServiceConfig
from interviewmaxxing_service.integration import runner_factory, runner_problem

from .conftest import FakeCandidates, serve

ORIGIN = 'http://127.0.0.1:4317'


def configured(tmp_path, **changes):
    key_file = tmp_path / 'synthetic.env'
    key_file.write_text('OPENROUTER_API_KEY=synthetic-service-test-key\n')
    values = {'IMX_HOME': str(tmp_path / 'home'), 'IMX_SERVICE_ORIGIN': ORIGIN,
        'IMX_SERVICE_BROWSER': 'opencli', 'IMX_SERVICE_OPENCLI_PROFILE': 'synthetic',
        'IMX_SERVICE_AI_ROUTING': '1', 'IMX_SERVICE_AI_ENV_FILE': str(key_file),
        'IMX_SERVICE_WRITER_MODEL': 'anthropic/claude-opus-5.5'}
    values.update(changes)
    return values


def test_default_service_factory_is_deterministic_and_preparation_only(tmp_path):
    config = ServiceConfig.from_env({'IMX_HOME': str(tmp_path / 'home'), 'IMX_SERVICE_ORIGIN': ORIGIN})
    assert config.application_mode == 'TEST_ONLY'
    assert not config.dynamic_runtime and not config.ai_routing
    assert config.browser == 'playwright'
    runner = runner_factory(config)(SimpleNamespace())
    assert runner.prepare_only
    assert type(runner.browser_factory).__name__ == 'PlaywrightSessionFactory'
    assert runner.browser_factory.annotator is None
    assert type(runner.resolver).__name__ == 'FactualPacketResolver'
    assert not config.paths.state_db.exists()


def test_configured_service_builds_dynamic_factory_without_browser_start(tmp_path, monkeypatch):
    import interviewmaxxing_browser.ai as ai
    from interviewmaxxing_browser.opencli import OpenCliSessionFactory
    from interviewmaxxing_candidate import LocalCandidateStore

    calls = []
    annotator, resolver = object(), object()
    monkeypatch.setattr(ai, 'build_ai_runtime', lambda **kwargs:
        (calls.append(kwargs) or annotator, resolver))
    monkeypatch.setattr(OpenCliSessionFactory, 'start', lambda *_: pytest.fail('browser started'))
    monkeypatch.setattr(LocalCandidateStore, 'load', lambda *_: pytest.fail('candidate loaded'))
    config = ServiceConfig.from_env(configured(tmp_path))
    assert calls == []
    assert runner_problem(config) is None
    assert calls == []
    runner = runner_factory(config)(SimpleNamespace())
    assert runner.prepare_only and config.application_mode == 'TEST_ONLY'
    assert runner.browser_factory.config.profile == 'synthetic'
    assert runner.browser_factory.annotator is annotator and runner.resolver is resolver
    # No writer effort is configured; the runtime builder passes it through (WP12 d83e148).
    assert calls == [{'env_file': config.ai_env_file, 'writer_model': 'anthropic/claude-opus-5.5',
                      'writer_effort': None}]
    assert not config.paths.state_db.exists()


@pytest.mark.parametrize('change', [
    {'IMX_SERVICE_BROWSER': 'unknown'},
    {'IMX_SERVICE_AI_ROUTING': 'maybe'},
    {'IMX_SERVICE_AI_ROUTING': '0'},
    {'IMX_SERVICE_AI_ENV_FILE': ''},
    {'IMX_SERVICE_AI_ENV_FILE': 'relative.env'},
    {'IMX_SERVICE_WRITER_MODEL': ''},
    {'IMX_SERVICE_WRITER_MODEL': 'automatic'},
    {'IMX_SERVICE_BROWSER': 'playwright'},
])
def test_invalid_config_fails_without_state_or_provider(tmp_path, monkeypatch, change):
    import interviewmaxxing_browser.ai as ai

    monkeypatch.setattr(ai, 'build_ai_runtime', lambda **_: pytest.fail('provider constructed'))
    env = configured(tmp_path, **change)
    with pytest.raises(ConfigError):
        ServiceConfig.from_env(env)
    assert not tmp_path.joinpath('home').exists()


def test_missing_credentials_stop_before_request_recording(
    tmp_path, isolated_imx_home, fictional_site, monkeypatch
):
    import interviewmaxxing_browser.ai as ai

    monkeypatch.setattr(ai, 'build_ai_runtime', lambda **_: pytest.fail('provider constructed'))
    env = configured(tmp_path, IMX_SERVICE_AI_ENV_FILE=str(tmp_path / 'missing.env'))
    config = ServiceConfig.from_env(env)
    problem = runner_problem(config)
    assert 'credentials' in problem
    assert 'synthetic-service-test-key' not in problem
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / 'candidate'),
               runner_problem=lambda: runner_problem(config)) as harness:
        health = harness.client.get('/healthz')
        assert health.json['runner'] == 'unavailable'
        assert harness.start().status == 503
        with harness.store() as store:
            assert store.list_applications() == []
        assert harness.runs == []


def test_dynamic_config_rejects_old_runner_before_factory(tmp_path, monkeypatch):
    config = ServiceConfig.from_env(configured(tmp_path))
    monkeypatch.setattr('interviewmaxxing_service.integration._runner_module',
        lambda: SimpleNamespace(create_runner=lambda paths, headless, interaction: None))
    assert 'does not support' in runner_problem(config)
    with pytest.raises(RuntimeError, match='does not support'):
        runner_factory(config)(SimpleNamespace())
    assert not config.paths.state_db.exists()


def test_service_startup_rejects_invalid_runtime_before_candidate_gateway(tmp_path, monkeypatch, capsys):
    import interviewmaxxing_service.__main__ as entrypoint

    for name, value in configured(tmp_path, IMX_SERVICE_WRITER_MODEL='unsupported').items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(entrypoint, 'LocalCandidateGateway',
        lambda *_: pytest.fail('candidate gateway built before config was accepted'))
    assert entrypoint.main([]) == 2
    assert 'IMX_SERVICE_WRITER_MODEL' in capsys.readouterr().err
    assert not tmp_path.joinpath('home').exists()


def test_each_run_gets_a_fresh_resolver_with_no_traces_or_suggestion_decisions(tmp_path):
    """Round 7 (review 3): the per-run resolver invariant. Every run the service starts
    builds its own dynamic resolver, empty at run start, so no trace or lookup decision of
    one application reaches another's routing.trace event."""
    config = ServiceConfig.from_env(configured(tmp_path))
    make = runner_factory(config)
    first, second = make(SimpleNamespace()), make(SimpleNamespace())
    assert first.resolver is not second.resolver
    for runner in (first, second):
        assert type(runner.resolver).__name__ == 'DynamicPacketResolver'
        assert runner.resolver.narrative_traces == []
        assert runner.resolver._suggestion_decisions == {}
    first.resolver._trace({'stage': 'fictional', 'status': 'HELD'})
    assert second.resolver.narrative_traces == []
    assert not config.paths.state_db.exists()
