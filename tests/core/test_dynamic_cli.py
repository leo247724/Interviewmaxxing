"""The dynamic CLI is opt-in and classification has no application-state side effects."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from interviewmaxxing_cli.dynamic import DynamicOptions, classify_url, runtime_components
from interviewmaxxing_cli.main import build_parser, main
from interviewmaxxing_cli.runner import NoninteractiveInteraction, create_runner


def test_runtime_options_are_explicit_and_no_submit_flag_exists():
    parser = build_parser()
    defaults = parser.parse_args(['apply', 'https://example.test/job'])
    assert not defaults.ai_routing and defaults.browser == 'playwright'
    args = parser.parse_args(['resume', 'app-example', '--browser', 'opencli',
        '--opencli-profile', 'synthetic', '--ai-routing', '--env-file', '/tmp/synthetic.env',
        '--writer-model', 'anthropic/claude-opus-5.5'])
    assert args.ai_routing and args.browser == 'opencli'
    with pytest.raises(SystemExit):
        parser.parse_args(['apply', 'https://example.test/job', '--submit'])


@pytest.mark.parametrize('flags', [
    ['--ai-routing'], ['--opencli-profile', 'test'],
    ['--env-file', '/tmp/test'], ['--writer-model', 'unconfigured'],
])
def test_invalid_configuration_fails_before_state_creation(flags, tmp_path):
    home = tmp_path / 'never-created'
    with pytest.raises(SystemExit) as exc:
        main(['--home', str(home), 'classify', 'https://example.test/job', *flags])
    assert exc.value.code == 2
    assert not home.exists()


def test_opencli_factory_preparation_and_profile(isolated_imx_home):
    runner = create_runner(isolated_imx_home, headless=True,
        interaction=NoninteractiveInteraction(),
        dynamic_options=DynamicOptions(browser='opencli', opencli_profile='synthetic'))
    assert runner.prepare_only
    assert runner.browser_factory.config.profile == 'synthetic'
    assert 'imx-assessment-opencli' in runner.browser_factory.config.protected_sessions


def test_classify_only_observes_and_closes(monkeypatch, tmp_path):
    calls = []

    class Browser:
        annotator = None

        async def observe(self, url):
            calls.append(('observe', url))
            return SimpleNamespace(model_dump=lambda **_: {'kind': 'APPLICATION_FORM'})

        async def close(self):
            calls.append(('close',))

    class Factory:
        async def start(self, options):
            assert not options.allow_submission
            calls.append(('start',))
            return Browser()

    monkeypatch.setattr('interviewmaxxing_cli.dynamic.runtime_components', lambda _: (Factory(), None))
    result = asyncio.run(classify_url('https://example.test/job', options=DynamicOptions(),
        artifacts_dir=tmp_path / 'artifacts'))
    assert result['mode'] == 'observation_only'
    assert not result['submitted']
    assert calls == [('start',), ('observe', 'https://example.test/job'), ('close',)]
    assert not tmp_path.joinpath('state.sqlite3').exists()


def test_provider_build_is_only_called_when_opted_in(monkeypatch):
    import interviewmaxxing_browser.ai as ai

    calls = []
    router, resolver = object(), object()
    monkeypatch.setattr(ai, 'build_ai_runtime', lambda **kwargs: (calls.append(kwargs) or router, resolver))
    factory, _ = runtime_components(DynamicOptions())
    assert factory.annotator is None and not calls
    factory, selected = runtime_components(DynamicOptions(ai_routing=True,
        env_file=Path('/tmp/synthetic.env'), writer_model='anthropic/claude-opus-5.5'))
    assert factory.annotator is router and selected is resolver
    assert calls == [{'env_file': Path('/tmp/synthetic.env'), 'writer_model': 'anthropic/claude-opus-5.5'}]
