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
    assert calls == [{'env_file': Path('/tmp/synthetic.env'), 'writer_model': 'anthropic/claude-opus-5.5',
                      'writer_effort': None}]
    runtime_components(DynamicOptions(ai_routing=True, env_file=Path('/tmp/synthetic.env'),
        writer_model='anthropic/claude-opus-5.5', writer_effort='medium'))
    assert calls[-1]['writer_effort'] == 'medium'


def test_writer_effort_is_a_narrative_flag_of_the_ai_runtime():
    parser = build_parser()
    for command in (['apply', 'https://example.test/job'], ['resume', 'app-example'],
                    ['prepare-batch', '--inventory', '/tmp/inventory.json']):
        args = parser.parse_args([*command, '--ai-routing', '--env-file', '/tmp/synthetic.env',
                                  '--writer-model', 'anthropic/claude-opus-5.5', '--writer-effort', 'medium'])
        assert args.writer_effort == 'medium'
        assert parser.parse_args(command).writer_effort is None
    with pytest.raises(SystemExit):
        parser.parse_args(['apply', 'https://example.test/job', '--writer-effort', 'xhigh'])
    with pytest.raises(ValueError, match='--writer-effort requires --ai-routing'):
        DynamicOptions(writer_effort='high').validate()
    DynamicOptions(ai_routing=True, env_file=Path('/tmp/synthetic.env'),
                   writer_model='anthropic/claude-opus-5.5', writer_effort='low').validate()


def test_a_batch_forwards_and_records_the_writer_effort(isolated_imx_home):
    from interviewmaxxing_cli.batch import BatchOptions, BatchRunOptions

    options = BatchOptions(paths=isolated_imx_home, batch_id='b-effort', candidate_id='default', ai_routing=True,
                           env_file=Path('/private/x.env'), writer_model='anthropic/claude-opus-5.5',
                           writer_effort='medium')
    argv = options.argv('https://example.test/job')
    assert argv[argv.index('--writer-effort') + 1] == 'medium'
    assert options.run_options().writer_effort == 'medium'
    assert BatchRunOptions.model_validate(options.run_options().model_dump(mode='json')).writer_effort == 'medium'
    plain = BatchOptions(paths=isolated_imx_home, batch_id='b-plain', candidate_id='default')
    assert '--writer-effort' not in plain.argv('https://example.test/job')
    assert plain.run_options().writer_effort is None
    with pytest.raises(ValueError, match='--writer-effort requires --ai-routing'):
        BatchOptions(paths=isolated_imx_home, batch_id='b-bad', candidate_id='default', writer_effort='high')
