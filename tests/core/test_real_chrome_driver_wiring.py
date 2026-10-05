"""Offline checks of locally installed, ignored personal-Chrome drivers.

Only selected function ASTs execute, with fake browser/provider/database globals.
A checkout without these local operational drivers explicitly skips these checks.
"""

from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
DRIVERS = {
    "rc": ROOT / ".imx/dynamic-applications/real-chrome/rc.py",
    "ashby": ROOT / ".imx/dynamic-applications/real-chrome/ashby.py",
    "gh": ROOT / ".imx/dynamic-applications/real-chrome/gh.py",
    "wf": ROOT / ".imx/dynamic-applications/wellfound-run/wf.py",
}


def _function(driver: str, name: str, namespace: dict[str, Any]) -> Any:
    path = DRIVERS[driver]
    if not path.exists():
        pytest.skip(f"Local ignored driver is not installed: {path.relative_to(ROOT)}")
    tree = ast.parse(path.read_text())
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    # Do not import the driver's top-level modules or run its filesystem side effects.
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            function,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("driver", ["ashby", "gh"])
def test_fill_entrypoint_is_guarded_before_any_driver_body(driver: str) -> None:
    blocked = {"result": "company_cooldown"}
    guard = Mock(return_value=Mock(return_value=blocked))
    fill = _function(driver, "fill_job", {"guarded_fill": guard})
    assert guard.call_count == 1
    # No Chrome, filesystem or provider globals are supplied: executing the underlying
    # driver body would fail. Its entrypoint must return the decorator's denial.
    assert fill(object(), {"company": "Example"}, None, False) == blocked
    guard.return_value.assert_called_once()


def _gh_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fill: Mock) -> tuple[Any, Mock, Mock]:
    jobs = [{"listing_id": f"fictional-{i}", "company": f"Company {i}", "pipeline_id": f"pipe-{i}"} for i in range(2)]
    (tmp_path / "jobs.json").write_text(json.dumps(jobs))
    monkeypatch.setattr("sys.argv", ["gh.py"])
    chrome = Mock(session="fictional-personal-chrome-session", page_id="original-tab")
    connection = Mock()
    connection.execute.return_value.fetchall.return_value = []
    connection.execute.return_value.fetchone.return_value = None
    sqlite = Mock()
    sqlite.connect.return_value = connection
    def create_handoff(entry, session, path, **kwargs):
        path.write_text(json.dumps(entry))
        path.chmod(0o600)
        return {"action": "read_matching_gmail_then_fill"}

    main = _function(
        "gh",
        "main",
        {
            "RUN": tmp_path,
            "json": json,
            "os": os,
            "sqlite3": sqlite,
            "rc": SimpleNamespace(Chrome=Mock(return_value=chrome)),
            "fill_job": fill,
            "create_handoff": create_handoff,
            "write_private": lambda path, entry: path.write_text(json.dumps(entry)),
            "time": time,
            "ashby": SimpleNamespace(mark_card=Mock()),
        },
    )
    return main, chrome, sqlite


def test_greenhouse_code_challenge_preserves_session_and_stops_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = {
        "listing_id": "fictional-0",
        "company": "Company 0",
        "result": "needs_code",
        "company_reservation": "fictional-reservation-token",
        "code_requested_at": 1234567890.5,
    }
    fill = Mock(return_value=pending)
    main, chrome, _ = _gh_main(tmp_path, monkeypatch, fill)
    main()
    fill.assert_called_once()
    assert fill.call_args.args[1]["listing_id"] == "fictional-0"
    marker = json.loads((tmp_path / "pending-code.json").read_text())
    assert marker["pipeline_id"] == "pipe-0"
    assert marker["browser_session"] == chrome.session
    assert marker["company_reservation"] == "fictional-reservation-token"
    assert marker["code_requested_at"] == 1234567890.5
    assert (tmp_path / "pending-code.json").stat().st_mode & 0o777 == 0o600
    chrome.close.assert_not_called()
    assert len((tmp_path / "ledger.jsonl").read_text().splitlines()) == 1


def test_greenhouse_existing_code_challenge_blocks_before_database_or_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pending-code.json").write_text('{"browser_session":"existing"}')
    fill = Mock()
    main, chrome, sqlite = _gh_main(tmp_path, monkeypatch, fill)
    with pytest.raises(SystemExit, match="code challenge is pending"):
        main()
    fill.assert_not_called()
    sqlite.connect.assert_not_called()
    chrome.close.assert_not_called()
    assert not (tmp_path / "ledger.jsonl").exists()


def test_wellfound_company_denial_cannot_reach_send(tmp_path: Path) -> None:
    job = {
        "listing_id": "fictional",
        "company": "Example",
        "title": "Example role",
        "url": "https://example.invalid/fictional",
    }
    (tmp_path / "jobs.json").write_text(json.dumps([job]))
    (tmp_path / "fields.json").write_text(
        json.dumps(
            [
                {"listing_id": "fictional", "status": "fields_read", "fields": []},
            ]
        )
    )
    guard = Mock()
    guard.reserve.return_value = SimpleNamespace(allowed=False, reason="company_cooldown")
    js = Mock()
    cli = Mock()
    phase_submit = _function(
        "wf",
        "phase_submit",
        {
            "RUN": tmp_path,
            "json": json,
            "time": time,
            "policy": lambda: guard,
            "profile_inputs": lambda: {},
            "open_and_click": lambda url: ("opened", {"modal": True}),
            "js": js,
            "cli": cli,
            "CANCEL": "fictional-cancel",
            "SEND_JS": "fictional-send",
        },
    )
    phase_submit()
    guard.reserve.assert_called_once_with("Example")
    assert js.call_args_list == [(("fictional-cancel",),)]
    guard.commit.assert_not_called()
    row = json.loads((tmp_path / "ledger.jsonl").read_text())
    assert row["result"] == "company_cooldown"
    cli.assert_called_once_with("close")


def test_greenhouse_binding_failure_preserves_recoverable_original_tab(tmp_path, monkeypatch):
    pending = {'listing_id': 'fictional-0', 'company': 'Company 0', 'result': 'needs_code',
               'company_reservation': 'reservation', 'code_requested_at': 1234567890.5,
               'challenge_url': 'https://job-boards.greenhouse.io/example/jobs/123'}
    main, chrome, _ = _gh_main(tmp_path, monkeypatch, Mock(return_value=pending))
    main.__globals__['create_handoff'] = Mock(side_effect=TimeoutError('bridge temporarily unavailable'))
    main()
    saved = json.loads((tmp_path / 'pending-code.json').read_text())
    assert saved['handoff_state'] == 'binding_required'
    assert saved['browser_tab'] == 'original-tab'
    assert saved['code_requested_at'] == 1234567890.5
    assert saved['company_reservation'] == 'reservation'
    assert saved['pipeline_id'] == 'pipe-0'
    chrome.close.assert_not_called()

@pytest.mark.parametrize('control_type', ['radio', 'checkbox', 'file'])
def test_greenhouse_unhandled_required_control_cannot_submit(control_type):
    field = {'id': 'unhandled', 'name': 'unhandled', 'label': 'Unusual requirement',
             'type': control_type, 'tag': 'INPUT', 'role': '', 'required': True,
             'value': '', 'selected': '', 'checked': False}
    chrome = Mock()
    chrome.js.return_value = {'fields': [field], 'submit': True, 'url': 'https://example.invalid'}
    fill = _function('gh', 'fill_job', {
        'guarded_fill': lambda fn: fn, 'time': time, 're': __import__('re'),
        'json': json, 'READ': 'form snapshot', 'YESNO': [],
        'ashby': SimpleNamespace(yesno_for=lambda *args: None),
    })
    result = fill(chrome, {'listing_id': 'example', 'company': 'Example',
                          'title': 'Example role', 'url': 'https://example.invalid'}, None, False)
    assert result['result'] == 'needs_answers'
    chrome.cli.assert_not_called()


def test_greenhouse_selection_drift_after_fill_blocks_submit():
    field = {'id': 'question-1', 'name': 'question-1', 'label': 'Are you comfortable with hybrid work?',
             'type': 'text', 'tag': 'INPUT', 'role': 'combobox', 'required': True,
             'value': '', 'selected': '', 'checked': False}
    chrome = Mock()
    chrome.js.side_effect = [{'fields': [field], 'submit': True, 'url': 'https://example.invalid'}, {'fields': [{**field, 'selected': 'No'}], 'url': 'https://example.invalid'}]
    fill = _function('gh', 'fill_job', {
        'guarded_fill': lambda fn: fn, 'time': time, 're': __import__('re'), 'json': json,
        'READ': 'form snapshot', 'EEO': [], 'YESNO': [],
        'ashby': SimpleNamespace(policy_answer=lambda *args: (False, None), yesno_for=lambda *args: 'Yes'),
        'react_select': lambda *args: {'ok': True, 'shown': 'Yes'},
    })
    result = fill(chrome, {'listing_id': 'example', 'company': 'Example',
                          'title': 'Example role', 'url': 'https://example.invalid'}, None, False)
    assert result['result'] == 'needs_answers'
    assert any('Selection changed' in x for x in result['unanswered'])
    chrome.cli.assert_not_called()


def test_greenhouse_redirected_job_cannot_fill_or_submit():
    chrome = Mock()
    chrome.js.return_value = {'fields': [{'id': 'email'}], 'submit': True,
                              'url': 'https://other-company.invalid/jobs/different'}
    fill = _function('gh', 'fill_job', {'guarded_fill': lambda fn: fn, 'time': time, 'READ': 'snapshot'})
    result = fill(chrome, {'listing_id': 'example', 'company': 'Example',
                          'title': 'Example role', 'url': 'https://example.invalid/jobs/123'}, None, False)
    assert result['result'] == 'no_form'
    assert chrome.js.call_count == 1
    chrome.cli.assert_not_called()


@pytest.mark.parametrize("payload,expected", [
    ('[{"value":"0","text":"No"},{"value":"1","text":"Yes"}]', [{"value":"0","text":"No"},{"value":"1","text":"Yes"}]),
    (json.dumps({'result': json.dumps([{'value': '0'}])}), [{'value':'0'}]),
    ('{"ok":true}', {"ok": True}),
    ('1234567', '1234567'),
])
def test_opencli_parser_preserves_option_arrays_and_scalar_strings(payload, expected):
    js = _function('rc', 'js', {'json': json})
    assert js(SimpleNamespace(cli=lambda *args, **kwargs: payload), 'snapshot') == expected


def test_greenhouse_tries_postal_code_when_full_state_name_opens_no_menu():
    import re

    class StateCodeOnly:
        probe = ''
        shown = ''
        typed = []

        def cli(self, *args):
            if args[0] == 'type':
                self.probe = args[2]
                self.typed.append(args[2])

        def js(self, code):
            if 'expanded: inp?' in code:
                return {'expanded': 'true' if self.probe == 'TX' else 'false', 'shown': self.shown}
            if 'Toggle flyout' in code:
                return 'state-opener'
            if 'const opts=' in code:
                return {'ok': True, 'option_id': 'react-select-state-option-0', 'picked': 'TX'}
            if "getAttribute('role')!=='option'" in code:
                self.shown = 'TX'
            return True

    chrome = StateCodeOnly()
    select = _function('gh', 'react_select', {
        'json': json, 're': re, 'time': SimpleNamespace(sleep=lambda _: None),
    })
    assert select(chrome, 'state', ['Texas', 'TX'], r'^(?:Texas|TX)$')['ok'] is True
    assert chrome.typed == ['Texas', 'TX']


def test_greenhouse_stops_after_required_text_has_no_grounded_answer():
    import re

    question = {'id': 'first_question', 'name': 'q1', 'label': 'Describe your industry experience?',
                'tag': 'TEXTAREA', 'type': 'textarea', 'role': '', 'required': True}
    later = {**question, 'id': 'second_question', 'name': 'q2', 'label': 'Why this company?'}
    url = 'https://job-boards.greenhouse.io/example/jobs/12345'
    chrome = Mock()
    chrome.js.return_value = {'url': url, 'submit': True, 'fields': [question, later]}
    answer = Mock(return_value=None)
    fill = _function('gh', 'fill_job', {
        'guarded_fill': lambda fn: fn, 're': re, 'time': time, 'json': json,
        'READ': 'snapshot', 'EEO': [], 'value_for': answer, 'rc': Mock(),
    })
    result = fill(chrome, {'listing_id': 'fixture', 'company': 'Example', 'title': 'Growth', 'url': url}, None, False)
    assert result['result'] == 'needs_answers'
    assert result['unanswered'] == [question['label']]
    assert answer.call_count == 1
    chrome.cli.assert_not_called()
