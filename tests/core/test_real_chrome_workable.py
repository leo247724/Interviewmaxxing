"""Offline-only Workable checks, with no profile imports or browser/provider calls."""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit, urlunsplit

import pytest

DRIVER = Path(__file__).resolve().parents[2] / ".imx/dynamic-applications/real-chrome/workable.py"
URL = "https://apply.workable.com/acme/j/ABC123"
JOB = dict(listing_id="fake-id", company="Acme", title="Role", url=URL, location="Austin, TX")
FORM = {"url": URL + "/apply/", "form": True, "fields": [], "submit": True}


def functions(**overrides):
    if not DRIVER.exists():
        pytest.skip("Local Workable driver not installed")
    tree = ast.parse(DRIVER.read_text())
    ns = dict(
        re=re,
        json=json,
        os=os,
        Path=Path,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
        guarded_fill=lambda f: f,
        time=SimpleNamespace(sleep=Mock(), strftime=lambda _: "now"),
        READ="snapshot",
        OPEN_VIEW="open-view %s",
    )
    ns.update(overrides)
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    nodes += [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(DRIVER), "exec"
        ),
        ns,
    )
    return ns


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.workable.com/view/opaque",
        "http://apply.workable.com/acme/j/ABC123",
        "https://evil.example/acme/j/ABC123",
        URL + "/unexpected",
        "https://user@apply.workable.com/acme/j/ABC123",
        "",
        None,
    ],
)
def test_invalid_identity(url):
    assert functions()["job_identity"](url) is None


def test_short_redirect_and_resolved_organization_binding():
    ns = functions()
    assert (
        ns["apply_url"]("https://apply.workable.com/j/ABC123?source=Saved")
        == "https://apply.workable.com/j/ABC123/apply/?source=Saved"
    )
    assert ns["matching_form_url"](FORM, "https://apply.workable.com/j/ABC123")
    assert not ns["matching_form_url"](
        {**FORM, "url": URL.replace("acme", "different") + "/apply/"}, URL
    )


@pytest.mark.parametrize("phase", ["initial", "final"])
@pytest.mark.parametrize(
    "wrong",
    [
        URL.replace("ABC123", "OTHER"),
        URL.replace("acme", "other"),
        "https://evil.example/acme/j/ABC123/apply/",
    ],
)
def test_wrong_identity_prevents_fill_and_dispatch(phase, wrong):
    c = Mock()
    bad = {**FORM, "url": wrong}
    c.js.side_effect = [bad] if phase == "initial" else [FORM, bad]
    ns = functions()
    result = ns["fill_job"](c, JOB, None, False)
    assert result["result"] == "needs_answers"
    c.cli.assert_not_called()


def test_view_url_with_unverified_identity_never_fills_or_submits():
    c = Mock()
    result = functions()["fill_job"](
        c, {**JOB, "url": "https://jobs.workable.com/view/opaque"}, None, False
    )
    assert result["result"] == "needs_answers"
    c.open.assert_called_once()
    c.cli.assert_not_called()


def test_required_unknown_and_checkbox_hold():
    f = {
        "key": "1",
        "name": "q",
        "label": "Consent",
        "tag": "INPUT",
        "type": "checkbox",
        "required": True,
        "checked": False,
        "valid": False,
    }
    sibling = {**f, "key": "2", "checked": True, "valid": True}
    assert functions()["unresolved"]({"form": True, "fields": [f, sibling]}) == ["Consent"]
    ashby = Mock()
    ashby.policy_answer.return_value = (True, None)
    assert functions(ashby=ashby)["choice_patterns"]("Can you work in Canada?", "Canada") == []
    ashby.opts_for.assert_not_called()


def test_binding_salary_ceiling_is_not_generic_consent():
    ashby = Mock()
    result = functions(ashby=ashby)["choice_patterns"](
        "Do you acknowledge that the salary range of $90,000 to $110,000 will not change?"
    )
    assert result == []
    ashby.opts_for.assert_not_called()


def test_profile_identity_uses_explicit_field():
    ashby = SimpleNamespace(
        SA={
            "first_name": "Test",
            "last_name": "Candidate",
            "city": "Austin",
            "state": "TX",
            "country": "United States",
        }
    )
    ns = functions(ashby=ashby)
    assert (
        ns["text_answer"](
            {"id": "firstname", "name": "firstname", "label": "First name"}, JOB, None, {}
        )
        == "Test"
    )
    assert (
        ns["text_answer"]({"id": "address", "name": "address", "label": "Address"}, JOB, None, {})
        == "Austin, TX, United States"
    )


def test_dry_never_dispatches():
    c = Mock()
    c.js.return_value = FORM
    assert functions()["fill_job"](c, JOB, None, True)["result"] == "dry_filled"
    c.cli.assert_not_called()


def test_verified_receipt_and_trusted_click():
    c = Mock()
    c.js.side_effect = [
        FORM,
        FORM,
        {
            "url": URL + "/apply/",
            "form": False,
            "text": "Your application has been submitted successfully.",
        },
    ]
    result = functions()["fill_job"](c, JOB, None, False)
    assert result["result"] == "submitted"
    c.cli.assert_called_once_with("click", "--role", "button", "--name", "Submit application",
                                   "--testid", "imx-workable-submit")


@pytest.mark.parametrize(
    "change",
    [
        {"url": URL.replace("ABC123", "OTHER") + "/apply/"},
        {"url": URL.replace("acme", "other") + "/apply/"},
        {"form": True},
        {"text": "Submit application"},
        {"captcha": True},
        {"cookie": True},
    ],
)
def test_rejects_false_confirmation(change):
    receipt = {
        "url": URL + "/apply/",
        "form": False,
        "text": "Your application has been submitted successfully.",
    }
    assert not functions()["confirmed"]({**receipt, **change}, URL)


def test_cookie_requires_fresh_snapshot_before_fill():
    c = Mock()
    c.js.side_effect = [
        {**FORM, "cookie": True, "cookie_reject": "Reject all"},
        {**FORM, "url": URL.replace("ABC123", "OTHER")},
    ]
    result = functions()["fill_job"](c, JOB, None, False)
    assert result["result"] == "needs_answers"
    c.cli.assert_called_once_with("click", "--role", "button", "--name", "Reject all")


def test_challenge_keeps_unknown_dispatch_receipt():
    c = Mock(session="owned-session", page_id="owned-tab")
    c.js.side_effect = [FORM, FORM, {"url": URL + "/apply/", "form": True, "captcha": True}]
    result = functions()["fill_job"](c, JOB, None, False)
    assert result["result"] == "send_failed"
    assert result["browser_session"] == "owned-session"
    assert result["browser_tab"] == "owned-tab"


def test_guard_runs_before_browser():
    guard = Mock(return_value=Mock(return_value={"result": "company_cooldown"}))
    assert (
        functions(guarded_fill=guard)["fill_job"](None, {}, None, False)["result"]
        == "company_cooldown"
    )


@pytest.mark.parametrize("result", ["send_failed", "error"])
def test_main_preserves_uncertain_tab_and_does_not_move_card(tmp_path, monkeypatch, result):
    (tmp_path / "jobs.json").write_text(json.dumps([JOB]))
    monkeypatch.setattr("sys.argv", ["workable.py"])
    monkeypatch.delenv("WF_KEEP", raising=False)
    c = Mock(session="owned", page_id="tab")
    db = Mock()
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.fetchone.return_value = None
    entry = {**JOB, "result": result, "company_reservation": "pending-token"}
    mark = Mock()
    ns = functions(
        RUN=tmp_path,
        rc=SimpleNamespace(Chrome=Mock(return_value=c)),
        sqlite3=SimpleNamespace(connect=Mock(return_value=db)),
        ashby=SimpleNamespace(mark_card=mark),
    )
    ns["fill_job"] = Mock(return_value=entry)
    ns["main"]()
    c.close.assert_not_called()
    mark.assert_not_called()
    with pytest.raises(SystemExit, match="uncertain Workable submission"):
        ns["main"]()
    assert ns["rc"].Chrome.call_count == 1


@pytest.mark.parametrize("ready_answer", ["Verified candidate summary", None])
def test_exact_summary_gets_candidate_and_role_context_without_bypassing_review(ready_answer):
    rag = Mock(return_value=(ready_answer, 0.125))
    ashby = SimpleNamespace(SA={}, RULES=[], TEXT_DEFAULTS=[],
                            policy_answer=Mock(return_value=(False, None)), rag=rag)
    field = dict(id="summary", name="summary", label="Summary", tag="TEXTAREA",
                 required=True, max=500, help_text="Include relevant paid search background.")
    entry = {"cost": 0.0}
    result = functions(ashby=ashby)["text_answer"](field, JOB, Path("listing.json"), entry)
    assert result == ready_answer
    args = rag.call_args.args
    assert args[0] == JOB["company"]
    assert args[1] == Path("listing.json")
    assert "my verified work experience" in args[2]
    assert "Role role at Acme" in args[2]
    assert field["help_text"] in args[2]
    assert "at most 500 characters" in args[2]
    assert args[3] == "answer"
    assert entry["cost"] == 0.125


def test_other_narrative_prompt_is_preserved():
    rag = Mock(return_value=(None, 0.0))
    ashby = SimpleNamespace(SA={}, RULES=[], TEXT_DEFAULTS=[],
                            policy_answer=Mock(return_value=(False, None)), rag=rag)
    field = dict(id="question", name="question", label="Summary of your proposed campaign?",
                 tag="TEXTAREA", required=True, max=-1)
    functions(ashby=ashby)["text_answer"](field, JOB, None, {"cost": 0.0})
    assert rag.call_args.args[2] == field["label"]


def test_observed_decline_all_cookie_choice_requires_new_snapshot():
    c = Mock()
    c.js.side_effect = [{**FORM, "cookie": True, "cookie_reject": "Decline all"}, FORM, FORM]
    assert functions()["fill_job"](c, JOB, None, True)["result"] == "dry_filled"
    c.cli.assert_called_once_with("click", "--role", "button", "--name", "Decline all")
    assert c.js.call_count == 3


@pytest.mark.parametrize("label,name", [("Summary", "summary"), ("Cover letter", "comments")])
def test_optional_narrative_first_pass_skips_provider(label, name):
    rag = Mock()
    ashby = SimpleNamespace(SA={}, RULES=[], TEXT_DEFAULTS=[],
                            policy_answer=Mock(return_value=(False, None)), rag=rag)
    field = dict(id=name, name=name, label=label, tag="TEXTAREA", required=False, max=500)
    assert functions(ashby=ashby)["text_answer"](field, JOB, None, {"cost": 0.0}) is None
    rag.assert_not_called()


@pytest.mark.parametrize("mode,expected", [
    ("react-replaced-selected", True),
    ("unselected", False),
    ("already-selected", True),
    ("ambiguous-replacement", False),
    ("native-selected", True),
])
def test_toggle_reads_fresh_native_or_aria_state_without_browser(mode, expected):
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the isolated DOM fixture")
    tree = ast.parse(DRIVER.read_text())
    toggle = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "TOGGLE" for t in n.targets))
    fixture = r"""
const mode=MODE;
let clicks=0;
const wrapper={getAttribute:()=>mode==='already-selected'?'true':'false'};
const old={type:'radio',name:'verified-question',value:'true',checked:false,disabled:false,
  closest:()=>wrapper, click:()=>{clicks++; if(mode==='react-replaced-selected') current=[replacement];
    if(mode==='ambiguous-replacement') current=[replacement,replacement];
    if(mode==='native-selected') old.checked=true;}};
const replacement={...old,closest:()=>({getAttribute:()=> 'true'})};
let current=[old];
const document={querySelector:()=>old,querySelectorAll:()=>current};
const result=SCRIPT;
console.log(JSON.stringify({result,clicks}));
""".replace("MODE", json.dumps(mode)).replace("SCRIPT", toggle % json.dumps("6"))
    completed = subprocess.run([node, "-e", fixture], check=True, text=True, capture_output=True)
    receipt = json.loads(completed.stdout)
    assert receipt["result"]["ok"] is expected
    assert receipt["clicks"] == (0 if mode == "already-selected" else 1)


VIEW_URL = "https://jobs.workable.com/view/Abc123/remote-role-at-acme"
VIEW_JOB = {**JOB, "url": VIEW_URL}
VIEW_FORM = {**FORM, "url": VIEW_URL, "job_title": "Role", "job_company": "Acme", "dialog_count": 1}


@pytest.mark.parametrize("change", [
    {"url": VIEW_URL.replace("Abc123", "Different")},
    {"url": VIEW_URL + "?different=1"},
    {"url": VIEW_URL.replace("jobs.workable.com", "evil.example")},
    {"job_title": "Another role"},
    {"job_company": "Another company"},
])
def test_view_binding_requires_exact_url_heading_company(change):
    assert functions()["matching_view_job"](VIEW_FORM, VIEW_JOB)
    assert not functions()["matching_view_job"]({**VIEW_FORM, **change}, VIEW_JOB)


def test_view_modal_opens_then_fills_with_fresh_binding_and_unique_submit():
    c = Mock()
    c.js.side_effect = [
        {**VIEW_FORM, "form": False, "dialog_count": 0},
        {"ok": True}, VIEW_FORM, VIEW_FORM,
        {**VIEW_FORM, "form": False, "dialog_text": "Your application has been submitted successfully."},
    ]
    result = functions()["fill_job"](c, VIEW_JOB, None, False)
    assert result["result"] == "submitted"
    assert result["verified"] == VIEW_URL
    c.cli.assert_called_once_with("click", "--role", "button", "--name", "Submit application",
                                   "--testid", "imx-workable-submit")


def test_view_unloaded_dialog_does_not_submit():
    c = Mock()
    unloaded = {**VIEW_FORM, "form": False}
    c.js.side_effect = [unloaded, {"ok": True}] + [unloaded] * 15
    result = functions()["fill_job"](c, VIEW_JOB, None, False)
    assert result["result"] == "needs_answers"
    assert "did not load" in result["unanswered"][0]
    c.cli.assert_not_called()


def test_view_heading_drift_before_submit_holds():
    c = Mock()
    c.js.side_effect = [VIEW_FORM, {"ok": True}, VIEW_FORM, {**VIEW_FORM, "job_title": "Other"}]
    assert functions()["fill_job"](c, VIEW_JOB, None, False)["result"] == "needs_answers"
    c.cli.assert_not_called()


@pytest.mark.parametrize("change", [
    {"dialog_count": 0}, {"dialog_count": 2}, {"form": True},
    {"job_company": "Wrong"}, {"url": VIEW_URL + "?changed=1"},
    {"dialog_text": "", "text": "Your application has been submitted successfully."},
    {"dialog_text": "Application error. Please retry."},
])
def test_view_confirmation_requires_unique_job_bound_modal_without_live_form(change):
    receipt = {**VIEW_FORM, "form": False,
               "dialog_text": "Your application has been submitted successfully."}
    assert functions()["confirmed"](receipt, VIEW_URL, VIEW_JOB)
    assert not functions()["confirmed"]({**receipt, **change}, VIEW_URL, VIEW_JOB)


def radio_fields(selected):
    return [dict(key=str(i), id=f"r{i}", name="work_authorization", tag="INPUT", type="radio",
                 label="Are you authorized to work in the US?", option=option, value=value,
                 required=True, valid=True, checked=value == selected)
            for i, (option, value) in enumerate([("YES", "true"), ("NO", "false")])]


@pytest.mark.parametrize("selected,expected_result", [("false", "needs_answers"), ("true", "dry_filled")])
def test_later_radio_flip_cannot_pass_final_verification(selected, expected_result):
    c = Mock()
    c.js.side_effect = [{**FORM, "fields": radio_fields(None)},
                        {**FORM, "fields": radio_fields(selected)}]
    ns = functions()
    ns["choice_patterns"] = Mock(return_value=["^YES$"])
    ns["select_toggle"] = Mock(return_value={"ok": True})
    result = ns["fill_job"](c, JOB, None, True)
    assert result["result"] == expected_result
    if selected == "false":
        assert any("filled answer changed" in item for item in result["unanswered"])
    c.cli.assert_not_called()


def test_nonempty_wrong_text_after_rerender_blocks_actual_dispatch():
    field = dict(key="0", id="firstname", name="firstname", tag="INPUT", type="text",
                 label="First name", required=True, valid=True, value="", max=-1)
    c = Mock()
    c.js.side_effect = [{**VIEW_FORM, "fields": [field]}, {"ok": True},
                        {**VIEW_FORM, "fields": [field]},
                        {**VIEW_FORM, "fields": [{**field, "value": "Wrong name"}]}]
    ns = functions(rc=SimpleNamespace(set_text=Mock(return_value={"ok": True})))
    ns["text_answer"] = Mock(return_value="Verified name")
    result = ns["fill_job"](c, VIEW_JOB, None, False)
    assert result["result"] == "needs_answers"
    assert "First name: filled answer changed" in result["unanswered"]
    c.cli.assert_not_called()


def test_select_value_label_and_uploaded_filename_are_independently_verified():
    ns = functions()
    select = dict(key="1", id="country", name="country", label="Country", tag="SELECT",
                  type="select-one", value="us", options=[dict(value="us", text="United States")])
    file = dict(key="2", id="upload-old", name="", label="Resume", tag="INPUT", type="file",
                files=1, file_names=["verified-resume.pdf"])
    expected = [ns["expected_field"](select, "select", "us", "United States"),
                ns["expected_field"](file, "file", "verified-resume.pdf")]
    assert ns["changed_expected"]({**FORM, "fields": [select, file]}, expected) == []
    changed = [{**select, "options": [dict(value="us", text="Canada")]},
               {**file, "file_names": ["other-resume.pdf"]}]
    assert len(ns["changed_expected"]({**FORM, "fields": changed}, expected)) == 2
