"""Offline tests for Gmail tool result transport and browser challenge binding."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location('gh_handoff', Path(__file__).resolve().parents[2] / 'scripts/greenhouse_code_handoff.py')
assert spec and spec.loader
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
NOW = 1790622000.0


def email(**changes):
    m = {'id': 'mail-123', 'internal_date': str(int(NOW * 1000)), 'payload': {
        'headers': [{'name': 'From', 'value': 'Greenhouse <no-reply@us.greenhouse-mail.io>'},
                    {'name': 'To', 'value': h.EMAIL},
                    {'name': 'Subject', 'value': 'Security code for your application to Example'}],
        'mime_type': 'multipart/related', 'parts': [{'mime_type': 'text/html', 'body': {
            'content': '<p>Copy and paste this code into the security code field on your application:</p><h1>aB34cD78</h1><p>After you enter the code, resubmit your application.</p>'}}]}}
    m.update(changes)
    return m


@pytest.fixture
def pending(tmp_path):
    p = tmp_path / 'pending.json'
    h.write_private(p, {'company': 'Example', 'email': h.EMAIL, 'code_requested_at': NOW - 10,
        'challenge_id': 'challenge-123', 'browser_tab': 'original-tab',
        'browser_session': 'existing-session', 'challenge_url': 'https://job-boards.greenhouse.io/example/jobs/123',
        'handoff_state': 'waiting_for_email', 'company_reservation': 'reserved'})
    return p


def test_full_html_message_normalized():
    m = h.normalize_gmail(email())
    assert m['subject'].endswith('Example')
    assert 'aB34cD78' in m['body']
    assert m['received_at'] == NOW


def test_snippet_cannot_supply_code():
    with pytest.raises(h.HandoffError):
        h.normalize_gmail({'id': 'm1', 'snippet': 'aB34cD78'})


def test_fill_uses_original_tab_and_never_submits(pending, monkeypatch):
    browser = Mock(return_value={'ok': True})
    monkeypatch.setattr(h, 'browser', browser)
    result = h.fill_from_gmail(pending, {'challenge_id': 'challenge-123', 'messages': [email()]}, now=NOW)
    assert result['submitted'] is False
    assert browser.call_count == 2
    for call in browser.call_args_list:
        assert call.args[:2] == ('existing-session', 'eval')
        assert call.args[-2:] == ('--tab', 'original-tab')
        assert '.submit(' not in call.args[2]
    saved = json.loads(pending.read_text())
    assert saved['handoff_state'] == 'code_filled'
    assert saved['company_reservation'] == 'reserved'
    assert 'aB34cD78' not in pending.read_text()
    assert 'aB34cD78' not in json.dumps(result)
    assert pending.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('mode', ['wrong_challenge', 'stale', 'ambiguous'])
def test_invalid_email_handoff_never_touches_browser(pending, monkeypatch, mode):
    browser = Mock()
    monkeypatch.setattr(h, 'browser', browser)
    envelope = {'challenge_id': 'challenge-123', 'messages': [email()]}
    if mode == 'wrong_challenge':
        envelope['challenge_id'] = 'other'
    elif mode == 'stale':
        envelope['messages'] = [email(internal_date=str(int((NOW - 100) * 1000)))]
    else:
        envelope['messages'].append(email(id='mail-456'))
    with pytest.raises(ValueError):
        h.fill_from_gmail(pending, envelope, now=NOW)
    browser.assert_not_called()
    assert json.loads(pending.read_text())['handoff_state'] == 'waiting_for_email'


def test_navigation_or_changed_field_holds(pending, monkeypatch):
    browser = Mock(return_value={'ok': False, 'reason': 'challenge_or_url_changed'})
    monkeypatch.setattr(h, 'browser', browser)
    with pytest.raises(h.HandoffError):
        h.fill_from_gmail(pending, {'challenge_id': 'challenge-123', 'messages': [email()]}, now=NOW)
    assert browser.call_count == 1
    assert json.loads(pending.read_text())['handoff_state'] == 'waiting_for_email'


def test_readback_failure_retains_pending(pending, monkeypatch):
    browser = Mock(side_effect=[{'ok': True}, {'ok': False}])
    monkeypatch.setattr(h, 'browser', browser)
    with pytest.raises(h.HandoffError):
        h.fill_from_gmail(pending, {'challenge_id': 'challenge-123', 'messages': [email()]}, now=NOW)
    assert json.loads(pending.read_text())['handoff_state'] == 'waiting_for_email'


def test_create_binds_exact_tab(tmp_path, monkeypatch):
    url = 'https://job-boards.greenhouse.io/example/jobs/123'
    browser = Mock(side_effect=[[{'page': 'original', 'url': url}, {'page': 'other', 'url': url + '4'}], {'ok': True, 'url': url}])
    monkeypatch.setattr(h, 'browser', browser)
    p = tmp_path / 'pending.json'
    req = h.create_handoff({'company': 'Example', 'url': url, 'code_requested_at': NOW}, 's', p)
    assert browser.call_args.args[-2:] == ('--tab', 'original')
    assert req['email'] == h.EMAIL
    assert req['read_format'] == 'full'
    assert req['search_arguments']['query'].endswith(f'after:{int(NOW)-2}')


def test_duplicate_url_tabs_do_not_bind(tmp_path, monkeypatch):
    url = 'https://job-boards.greenhouse.io/example/jobs/123'
    browser = Mock(return_value=[{'page': 'a', 'url': url}, {'page': 'b', 'url': url}])
    monkeypatch.setattr(h, 'browser', browser)
    with pytest.raises(h.HandoffError):
        h.create_handoff({'company': 'Example', 'url': url, 'code_requested_at': NOW}, 's', tmp_path / 'p.json')
    assert browser.call_count == 1


def test_rebind_preserves_original_attempt_and_reservation(pending, monkeypatch):
    original = json.loads(pending.read_text())
    original['handoff_state'] = 'binding_required'
    h.write_private(pending, original)
    browser = Mock(side_effect=[[{'page': 'original-tab', 'url': original['challenge_url']}],
                               {'ok': True, 'url': original['challenge_url']}])
    monkeypatch.setattr(h, 'browser', browser)
    h.create_handoff(original, original['browser_session'], pending,
                     expected_url=original['challenge_url'], rebind=True)
    new = json.loads(pending.read_text())
    assert new['code_requested_at'] == original['code_requested_at']
    assert new['company_reservation'] == 'reserved'
    assert new['browser_tab'] == 'original-tab'
    assert new['challenge_id'] != original['challenge_id']
    assert new['handoff_state'] == 'waiting_for_email'


def test_rebind_never_replaces_closed_original_tab(pending, monkeypatch):
    original = json.loads(pending.read_text())
    monkeypatch.setattr(h, 'browser', Mock(return_value=[{'page': 'replacement-tab', 'url': original['challenge_url']}]))
    with pytest.raises(h.HandoffError):
        h.create_handoff(original, original['browser_session'], pending,
                         expected_url=original['challenge_url'], rebind=True)
    assert json.loads(pending.read_text()) == original


def test_challenge_cannot_bind_different_embed_job(tmp_path, monkeypatch):
    browser = Mock()
    monkeypatch.setattr(h, "browser", browser)
    with pytest.raises(h.HandoffError, match="different Greenhouse job"):
        h.create_handoff(
            {"company": "Example", "url": "https://job-boards.greenhouse.io/example/jobs/123", "code_requested_at": NOW},
            "s", tmp_path / "pending.json",
            expected_url="https://job-boards.greenhouse.io/embed/job_app?for=example&token=456",
        )
    browser.assert_not_called()
