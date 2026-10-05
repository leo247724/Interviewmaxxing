"""Offline GH enum-selection checks; no Chrome or provider process is started."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

DRIVER = Path(__file__).resolve().parents[2] / '.imx/dynamic-applications/real-chrome/gh_choice.py'
URL = 'https://job-boards.greenhouse.io/example/jobs/123'
OPTIONS = [{'value': 'react-select-question_123-option-0', 'label': 'Option A', 'disabled': False},
           {'value': 'react-select-question_123-option-1', 'label': 'Option B', 'disabled': False}]


def snapshot(**changes):
    return {'url': URL, 'supported': True, 'expanded': True,
            'options': copy.deepcopy(OPTIONS), 'selected': [], **changes}


def resolved(**changes):
    return {'status': 'READY', 'typedChoiceValue': {'kind': 'choice',
            'value': OPTIONS[1]['value'], 'label': OPTIONS[1]['label']},
            'provenance': {'source': 'RAG_PROFILE', 'reference_ids': ['fact-1']},
            'reference_ids': ['fact-1'], 'usd': 0.03, 'receipt': '/private/receipt.json',
            'cost_unknown': False, **changes}


class Browser:
    def __init__(self, states, clicked=None):
        self.states = iter(states)
        self.clicks = []
        self.clicked = {'ok': True} if clicked is None else clicked

    def js(self, script):
        if 'imx-grounded-observe' in script:
            return next(self.states)
        assert 'imx-grounded-click' in script
        payload = script.split(' const a=', 1)[1].split(';', 1)[0]
        self.clicks.append(json.loads(payload))
        return self.clicked


@pytest.fixture
def helper(monkeypatch):
    if not DRIVER.exists():
        pytest.skip('Local operational driver is not installed')
    provider = SimpleNamespace(resolve_choice=Mock(return_value=resolved()))
    monkeypatch.setitem(sys.modules, 'choice', provider)
    spec = importlib.util.spec_from_file_location('gh_choice_test_local', DRIVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    return module, provider


def run(helper, states, *, result=None, clicked=None):
    module, provider = helper
    if result is not None:
        provider.resolve_choice.return_value = result
    browser = Browser(states, clicked)
    entry = {'cost': 1.0}
    opener = Mock()
    answer = module.fill_grounded_select(browser, 'question_123', 'Choose a skill',
                                        {'company': 'Example'}, '/private/listing.json', entry, opener)
    return answer, browser, entry, opener


def test_exact_observed_choice_clicked_and_independently_verified(helper):
    answer, browser, entry, opener = run(helper, [snapshot(), snapshot(), snapshot(),
                                                snapshot(selected=['Option B'])])
    assert answer == {'ok': True, 'shown': 'Option B', 'why': None}
    assert browser.clicks == [{'url': URL, 'input': 'question_123',
                               'option': OPTIONS[1]['value'], 'label': 'Option B'}]
    opener.assert_called_once_with(browser, 'question_123', [], r'(?!)')
    helper[1].resolve_choice.assert_called_once_with('Example', '/private/listing.json',
                                                     'Choose a skill', OPTIONS)
    assert entry['cost'] == 1.03
    receipt = entry['choice_receipts'][0]
    assert receipt['provenance'] == resolved()['provenance']
    assert receipt['reference_ids'] == ['fact-1']
    assert receipt['receipt'] == '/private/receipt.json'
    assert receipt['selection_status'] == 'VERIFIED'


@pytest.mark.parametrize('first', [snapshot(supported=False), {}, snapshot(url='')])
def test_unsupported_initial_control_never_opens_or_calls_provider(helper, first):
    answer, browser, _, opener = run(helper, [first])
    assert not answer['ok']
    assert not browser.clicks
    opener.assert_not_called()
    helper[1].resolve_choice.assert_not_called()


@pytest.mark.parametrize('observed', [snapshot(options=[]), snapshot(url=URL + '/different'),
    snapshot(options=[{**OPTIONS[0], 'disabled': True}]),
    snapshot(options=[{**OPTIONS[0], 'value': 'unrelated-option'}])])
def test_invalid_observation_never_calls_provider(helper, observed):
    answer, browser, _, _ = run(helper, [snapshot(), observed])
    assert not answer['ok']
    assert not browser.clicks
    helper[1].resolve_choice.assert_not_called()


@pytest.mark.parametrize('result', [
    resolved(status='NEEDS_INPUT'),
    resolved(typedChoiceValue='Option B'),
    resolved(typedChoiceValue={'kind': 'multi_choice', 'choices': [OPTIONS[1]]}),
    resolved(typedChoiceValue={'kind': 'choice', 'value': OPTIONS[1]['value'], 'label': 'Option'}),
    resolved(typedChoiceValue={'kind': 'choice', 'value': 'invented', 'label': 'Option B'}),
    resolved(provenance=None),
    resolved(reference_ids=[]),
])
def test_unready_prose_multichoice_or_ungrounded_result_never_clicks(helper, result):
    answer, browser, entry, _ = run(helper, [snapshot(), snapshot()], result=result)
    assert not answer['ok']
    assert not browser.clicks
    assert entry['cost'] == 1.03
    assert entry['choice_receipts'][0]['selection_status'] == 'HELD'


@pytest.mark.parametrize('fresh', [snapshot(url=URL + '/other'), snapshot(supported=False),
    snapshot(options=[{**OPTIONS[1], 'label': 'Changed label'}]),
    snapshot(options=[{**OPTIONS[1], 'disabled': True}]), snapshot(options=[])])
def test_page_control_or_option_drift_prevents_click(helper, fresh):
    answer, browser, _, _ = run(helper, [snapshot(), snapshot(), fresh])
    assert not answer['ok']
    assert not browser.clicks


def test_closed_menu_is_reopened_and_revalidated_before_click(helper):
    answer, browser, _, opener = run(helper, [snapshot(), snapshot(),
        snapshot(expanded=False, options=[]), snapshot(), snapshot(selected=['Option B'])])
    assert answer['ok']
    assert len(browser.clicks) == 1
    assert opener.call_count == 2


def test_reopen_navigation_is_held(helper):
    answer, browser, _, _ = run(helper, [snapshot(), snapshot(),
        snapshot(expanded=False, options=[]), snapshot(url=URL + '/changed')])
    assert answer['why'] == 'changed_page'
    assert not browser.clicks


@pytest.mark.parametrize('after', [snapshot(selected=['Option A']),
    snapshot(selected=['Option B', 'Option A']), snapshot(selected=[]),
    snapshot(url=URL + '/other', selected=['Option B'])])
def test_independent_readback_must_show_exact_single_choice_on_same_page(helper, after):
    answer, browser, _, _ = run(helper, [snapshot(), snapshot(), snapshot(), after, after, after])
    assert not answer['ok']
    assert len(browser.clicks) == 1


def test_dom_race_rejection_is_not_reported_as_success(helper):
    answer, _, _, _ = run(helper, [snapshot(), snapshot(), snapshot()], clicked={'ok': False})
    assert answer['why'] == 'exact_click_failed'


def test_provider_exception_is_private_hold(helper):
    helper[1].resolve_choice.side_effect = RuntimeError('private secret payload')
    answer, browser, entry, _ = run(helper, [snapshot(), snapshot()])
    assert not answer['ok']
    assert not browser.clicks
    assert 'private secret' not in json.dumps([answer, entry])


@pytest.mark.parametrize('cost', [True, -1, float('nan'), float('inf'), '0.03'])
def test_invalid_cost_cannot_reach_click(helper, cost):
    answer, browser, entry, _ = run(helper, [snapshot(), snapshot()], result=resolved(usd=cost))
    assert answer['why'] == 'invalid_resolver_cost'
    assert not browser.clicks
    assert entry['cost'] == 1.0


def test_identity_provenance_may_have_no_reference_ids(helper):
    result = resolved(provenance={'source': 'PROFILE_IDENTITY', 'reference_ids': []}, reference_ids=[])
    answer, _, _, _ = run(helper, [snapshot(), snapshot(), snapshot(),
                                     snapshot(selected=['Option B'])], result=result)
    assert answer['ok']


def test_ready_without_receipt_is_held(helper):
    answer, browser, _, _ = run(helper, [snapshot(), snapshot()], result=resolved(receipt=None))
    assert answer['why'] == 'missing_resolver_receipt'
    assert not browser.clicks
