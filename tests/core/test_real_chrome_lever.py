"""Offline tests: no driver imports, live profile, provider calls or browser launch."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit, urlunsplit

import pytest

DRIVER = Path(__file__).resolve().parents[2] / '.imx/dynamic-applications/real-chrome/lever.py'


def functions(**overrides):
    if not DRIVER.exists():
        pytest.skip('Local Lever driver not installed')
    tree = ast.parse(DRIVER.read_text())
    ns = dict(re=re, json=json, urlsplit=urlsplit, urlunsplit=urlunsplit,
              guarded_fill=lambda f: f, time=SimpleNamespace(sleep=Mock(), strftime=lambda _: 'now'))
    ns.update(overrides)
    nodes = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)]
    nodes += [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(DRIVER), 'exec'), ns)
    return ns


URL = 'https://jobs.lever.co/acme/job-id'


@pytest.mark.parametrize('url', ['http://jobs.lever.co/acme/job', 'https://evil.example/acme/job', URL+'/thanks'])
def test_rejects_wrong_navigation(url):
    with pytest.raises(ValueError):
        functions()['apply_url'](url)


def test_apply_url_preserves_query_not_duplicate_suffix():
    assert functions()['apply_url'](URL+'/apply?source=Saved') == URL+'/apply?source=Saved'


@pytest.mark.parametrize('change', [{'url': URL.replace('job-id','another')+'/thanks'}, {'form': True}, {'captcha': True}, {'text': 'Please submit your application'}])
def test_confirmation_rejects_other_job_and_false_success(change):
    receipt = {'url': URL+'/thanks', 'form': False, 'text': 'Application submitted!', 'errors': []}
    fn = functions()['confirmed']
    assert fn(receipt, URL)
    assert not fn({**receipt, **change}, URL)


def test_unknown_scoped_policy_never_falls_through_to_yes():
    ashby = Mock()
    ashby.policy_answer.return_value = (True, None)
    ns = functions(ashby=ashby)
    assert ns['choice_patterns']('Can you work in Canada?') == []
    assert ns['text_answer']({'label':'Years of quantum physics'}, {}, None, {}) is None
    ashby.rag.assert_not_called()
    ashby.opts_for.assert_not_called()


def test_ambiguous_choices_hold():
    options = [{'text': 'Yes, permanently', 'value': '1'}, {'text':'Yes, temporarily', 'value':'2'}]
    assert functions()['pick_choice'](options, ['^yes']) is None


def test_required_radio_group_needs_one_but_file_needs_attachment():
    f = {'key':'1','name':'q','label':'Question','type':'radio','required':True,'checked':False}
    ns = functions()
    assert ns['unresolved']({'form':True,'fields':[f, {**f,'key':'2','checked':True}]}) == []
    assert ns['unresolved']({'form':True,'fields':[{**f,'type':'file','files':0,'value':'fake.pdf'}]}) == ['Question']


def test_entrypoint_has_company_guard():
    blocked = {'result':'company_cooldown'}
    guard = Mock(return_value=Mock(return_value=blocked))
    fill = functions(guarded_fill=guard)['fill_job']
    assert fill(None, {}, None, False) == blocked
    guard.assert_called_once()


def test_no_submit_for_required_missing_or_dry():
    field = {'key':'1','name':'q','label':'Unknown required','type':'range','tag':'INPUT','required':True,'value':''}
    st = {'url':URL+'/apply','form':True,'fields':[field],'submit':True}
    chrome = Mock()
    chrome.js.return_value = st
    ns = functions(READ='snapshot')
    result = ns['fill_job'](chrome, dict(listing_id='id',company='Acme',title='Role',url=URL), None, False)
    assert result['result'] == 'needs_answers'
    chrome.cli.assert_not_called()
    chrome.js.return_value = {**st,'fields':[]}
    assert ns['fill_job'](chrome, dict(listing_id='id',company='Acme',title='Role',url=URL), None, True)['result'] == 'dry_filled'
    chrome.cli.assert_not_called()


def test_trusted_submit_and_same_job_receipt():
    chrome = Mock()
    form = {'url':URL+'/apply','form':True,'fields':[],'submit':True}
    chrome.js.side_effect = [form, form, {'form':False,'url':URL+'/thanks','text':'Application submitted!'}]
    ns = functions(READ='snapshot')
    result = ns['fill_job'](chrome, dict(listing_id='id',company='Acme',title='Role',url=URL), None, False)
    assert result['result'] == 'submitted'
    chrome.cli.assert_called_once_with('click','--role','button','--name','Submit application')


def test_max_length_in_rag_prompt():
    ashby = Mock(RULES=[], TEXT_DEFAULTS=[])
    ashby.policy_answer.return_value = (False, None)
    ashby.rag.return_value = ('Evidence-based answer.', 0.1)
    f = {'label':'Why Acme?', 'name':'cards[q][field0]', 'tag':'TEXTAREA','max':150}
    entry = {'cost':0.0}
    ns = functions(ashby=ashby)
    assert ns['text_answer'](f, {'company':'Acme'}, Path('listing.json'), entry)
    assert 'at most 150 characters' in ashby.rag.call_args.args[2]
    assert entry['cost'] == 0.1


@pytest.mark.parametrize('observed', [
    URL.replace('acme', 'other-company') + '/apply',
    URL.replace('job-id', 'other-job') + '/apply',
    URL.replace('jobs.lever.co', 'jobs.eu.lever.co') + '/apply',
    URL.replace('https:', 'http:') + '/apply',
    URL + '/thanks',
    '',
])
@pytest.mark.parametrize('stage', ['initial', 'final'])
def test_wrong_job_never_fills_or_submits(observed, stage):
    chrome = Mock()
    expected = {'url':URL+'/apply','form':True,'fields':[],'submit':True}
    wrong = {**expected, 'url':observed}
    if stage == 'initial':
        # Deliberately lacks field keys: touching the field before URL validation fails.
        wrong['fields'] = [{}]
        chrome.js.return_value = wrong
    else:
        chrome.js.side_effect = [expected, wrong]
    rc = Mock()
    ashby = Mock()
    result = functions(READ='snapshot', rc=rc, ashby=ashby)['fill_job'](
        chrome, dict(listing_id='id', company='Acme', title='Role', url=URL), None, False)
    assert result['result'] == 'needs_answers'
    assert any('Wrong job URL' in x for x in result['unanswered'])
    chrome.cli.assert_not_called()
    rc.set_text.assert_not_called()
    rc.attach.assert_not_called()
    ashby.rag.assert_not_called()


@pytest.mark.parametrize('suffix', ['', '/apply', '/apply/?source=Saved'])
def test_same_job_canonical_form_url(suffix):
    assert functions()['matching_form_url']({'url':URL+suffix}, URL)


@pytest.mark.parametrize('checked,valid', [(False, True), (False, False), (True, False)])
def test_required_checkbox_not_satisfied_by_checked_sibling(checked, valid):
    required = {'key':'1','name':'consents[]','label':'Required consent',
                'type':'checkbox','required':True,'checked':checked,'valid':valid}
    sibling = {**required,'key':'2','label':'Other consent','checked':True,'valid':True}
    assert functions()['unresolved']({'form':True,'fields':[required,sibling]}) == ['Required consent']


def test_radio_not_satisfied_by_same_name_checkbox_or_invalid_state():
    radio = {'key':'1','name':'q','label':'Radio group','type':'radio','required':True,'checked':False,'valid':True}
    checkbox = {**radio,'key':'2','type':'checkbox','checked':True}
    fn = functions()['unresolved']
    assert fn({'form':True,'fields':[radio,checkbox]}) == ['Radio group']
    assert fn({'form':True,'fields':[{**radio,'checked':True,'valid':False}]}) == ['Radio group']


@pytest.mark.parametrize('location', ['Austin, TX', 'United States (Remote)', 'Canada', 'Remote', None])
def test_text_answers_pass_job_jurisdiction(location):
    ashby = Mock(RULES=[], TEXT_DEFAULTS=[])
    ashby.policy_answer.return_value = (False, None)
    ashby.yesno_for.return_value = 'Yes'
    field = {'label':'Are you willing?', 'tag':'INPUT'}
    answer = functions(ashby=ashby)['text_answer'](field, {'location':location}, None, {'cost':0})
    assert answer == 'Yes'
    ashby.policy_answer.assert_called_once_with('Are you willing?', location)
    ashby.yesno_for.assert_called_once_with('are you willing?', location)


@pytest.mark.parametrize('control', ['SELECT', 'radio'])
def test_fill_passes_job_jurisdiction_to_choice_policy(control):
    ashby = Mock()
    ashby.policy_answer.return_value = (True, None)
    field = {'key':'1','name':'q','label':'Are you authorized to work?',
             'tag':control,'type':control.lower(),'required':True,'value':'',
             'checked':False,'option':'Yes','options':[{'text':'Yes','value':'yes'}]}
    form = {'url':URL+'/apply','form':True,'fields':[field],'submit':True}
    chrome = Mock()
    chrome.js.return_value = form
    job = dict(listing_id='id',company='Acme',title='Role',url=URL,location='Canada')
    result = functions(READ='snapshot', ashby=ashby)['fill_job'](chrome, job, None, False)
    assert result['result'] == 'needs_answers'
    ashby.policy_answer.assert_called_once_with(field['label'], 'Canada')
    ashby.opts_for.assert_not_called()
    chrome.cli.assert_not_called()


def test_choice_patterns_passes_location_to_option_policy():
    ashby = Mock()
    ashby.policy_answer.return_value = (False, None)
    ashby.opts_for.return_value = ['^yes']
    assert functions(ashby=ashby)['choice_patterns']('Are you willing?', 'Austin, TX') == ['^yes']
    ashby.opts_for.assert_called_once_with('are you willing?', 'Austin, TX')


def test_verified_jurisdiction_override_reaches_text_policies():
    ashby = Mock(RULES=[], TEXT_DEFAULTS=[])
    ashby.policy_answer.return_value = (False, None)
    ashby.yesno_for.return_value = 'No'
    job = {'location':'Remote', 'work_jurisdiction':'United States'}
    field = {'label':'Do you need sponsorship?', 'tag':'INPUT'}
    assert functions(ashby=ashby)['text_answer'](field, job, None, {'cost':0}) == 'No'
    ashby.policy_answer.assert_called_once_with(field['label'], 'United States')
    ashby.yesno_for.assert_called_once_with(field['label'].lower(), 'United States')


@pytest.mark.parametrize('control', ['SELECT', 'radio'])
def test_verified_jurisdiction_override_reaches_choice_policy(control):
    ashby = Mock()
    ashby.policy_answer.return_value = (True, None)
    field = {'key':'1','name':'q','label':'Do you need sponsorship?',
             'tag':control,'type':control.lower(),'required':True,'value':'',
             'checked':False,'option':'Yes','options':[{'text':'Yes','value':'yes'}]}
    chrome = Mock()
    chrome.js.return_value = {'url':URL+'/apply','form':True,'fields':[field],'submit':True}
    job = dict(listing_id='id',company='Acme',title='Role',url=URL,
               location='Remote',work_jurisdiction='United States')
    result = functions(READ='snapshot', ashby=ashby)['fill_job'](chrome, job, None, False)
    assert result['result'] == 'needs_answers'
    ashby.policy_answer.assert_called_once_with(field['label'], 'United States')
    chrome.cli.assert_not_called()
