"""Offline BambooHR identity, answer, drift and receipt guards; no live browser."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

DRIVER = Path(__file__).resolve().parents[2] / ".imx/dynamic-applications/real-chrome/bamboo.py"
URL = "https://example.bamboohr.com/careers/42"
JOB = dict(listing_id="fixture", company="Example", title="Role", url=URL, location="Remote")
FORM = dict(url=URL, form=True, fields=[], submit=True, errors=[], captcha=False)


def functions(**overrides):
    tree = ast.parse(DRIVER.read_text())
    ns = dict(
        re=re,
        json=json,
        os=os,
        Path=Path,
        urlsplit=urlsplit,
        guarded_fill=lambda f: f,
        time=SimpleNamespace(sleep=Mock(), strftime=lambda _: "now"),
        READ="snapshot",
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


def field(**kw):
    return dict(
        key="1",
        name="question",
        label="Question",
        tag="INPUT",
        type="text",
        required=True,
        value="",
        valid=True,
        **kw,
    )


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "http://example.bamboohr.com/careers/42",
        "https://example.bamboohr.com.evil/careers/42",
        "https://user@example.bamboohr.com/careers/42",
        "https://example.bamboohr.com:443/careers/42",
        URL + "/apply",
        URL + "/confirmation",
        URL.replace("/42", "/0"),
        URL + "\n",
        "https://bamboohr.com/careers/42",
    ],
)
def test_identity_rejects_wrong_or_unobserved_routes(url):
    assert functions()["job_identity"](url) is None


def test_identity_accepts_source_query_but_binds_exact_form_url():
    ns = functions()
    assert ns["job_identity"](URL + "?source=LinkedIn") == ("example.bamboohr.com", "42")
    assert ns["matching_form_url"](FORM, URL + "?source=LinkedIn")
    assert not ns["bound_form"](FORM, URL + "?source=LinkedIn")


@pytest.mark.parametrize(
    "after",
    [
        {**FORM, "form": False, "text": "Thank you for applying"},
    ],
)
def test_explicit_same_job_receipt(after):
    assert functions()["confirmed"](after, URL)


@pytest.mark.parametrize(
    "change",
    [
        dict(form=True),
        dict(errors=["Invalid"]),
        dict(captcha=True),
        dict(url=URL.replace("42", "43")),
        dict(url=URL.replace("example", "other")),
        dict(url=URL + "?source=changed"),
        dict(text="Application form"),
        dict(url=URL + "/thanks"),
    ],
)
def test_confirmation_fails_closed(change):
    after = {**FORM, "form": False, "text": "Your application has been received", **change}
    assert not functions()["confirmed"](after, URL)


def test_wrong_initial_identity_never_fills_or_dispatches():
    c = Mock()
    c.js.return_value = {**FORM, "url": URL.replace("42", "43")}
    assert functions()["fill_job"](c, JOB, None, False)["result"] == "needs_answers"
    c.cli.assert_not_called()


def test_dry_does_not_submit():
    c = Mock()
    c.js.return_value = FORM
    assert functions()["fill_job"](c, JOB, None, True)["result"] == "dry_filled"
    c.cli.assert_not_called()


def test_confirmed_submission_dispatches_once():
    c = Mock()
    c.js.side_effect = [FORM, FORM, {**FORM, "form": False, "text": "Thank you for applying"}]
    result = functions()["fill_job"](c, JOB, None, False)
    assert result["result"] == "submitted"
    c.cli.assert_called_once_with("click", "--role", "button", "--name", "Submit application")


def test_uncertain_submission_preserves_browser_identity():
    c = Mock(session="lease", page_id="tab")
    c.js.return_value = FORM
    result = functions()["fill_job"](c, JOB, None, False)
    assert result["result"] == "send_failed" and result["browser_tab"] == "tab"
    c.close.assert_not_called()


@pytest.mark.parametrize("change", [dict(errors=["Required"]), dict(url=URL + "?source=changed")])
def test_final_validation_or_url_drift_prevents_submit(change):
    c = Mock()
    c.js.side_effect = [FORM, {**FORM, **change}]
    assert functions()["fill_job"](c, JOB, None, False)["result"] == "needs_answers"
    c.cli.assert_not_called()


def test_final_captcha_is_left_for_the_person_and_never_dispatched():
    # goal 3 (2026-10-01): a CAPTCHA-guarded form is filled and its tab kept for the person; the driver never clicks Submit
    c = Mock(session="imx-g3-univ-1")
    c.js.side_effect = [FORM, {**FORM, "captcha": True}]
    result = functions()["fill_job"](c, JOB, None, False)
    assert result["result"] == "captcha_challenge" and result["tab_kept_for_person"]
    c.cli.assert_not_called()


def test_unknown_required_does_not_get_generic_answer():
    ashby = Mock()
    ashby.policy_answer.return_value = (True, None)
    assert functions(ashby=ashby)["text_answer"](field(), JOB, None, {"cost": 0}) is None
    ashby.rag.assert_not_called()


def test_work_jurisdiction_reaches_policy():
    ashby = Mock()
    ashby.policy_answer.return_value = (True, "Yes")
    functions(ashby=ashby)["text_answer"](
        field(), {**JOB, "work_jurisdiction": "United States"}, None, {}
    )
    ashby.policy_answer.assert_called_once_with("Question", "United States")


def test_state_country_use_grounded_aliases_only():
    ashby = SimpleNamespace(SA={"state": "TX", "country": "United States"})
    ns = functions(ashby=ashby)
    assert ns["pick_choice"]([{"text": "Texas", "value": "TX"}], ns["choice_patterns"]("State *"))
    assert ns["pick_choice"](
        [{"text": "United States", "value": "1"}], ns["choice_patterns"]("Country")
    )
    assert not ns["pick_choice"](
        [{"text": "Canada", "value": "2"}], ns["choice_patterns"]("Country")
    )
    assert not ns["pick_choice"](
        [{"text": "Texas", "value": "TX"}, {"text": "TX", "value": "48"}],
        ns["choice_patterns"]("State"),
    )


def test_final_selected_text_drift_prevents_dispatch():
    f = dict(
        key="1",
        name="state",
        label="State",
        tag="BUTTON",
        type="combobox",
        required=True,
        value="Texas",
        selected_text="Texas",
        valid=True,
    )
    first = {**FORM, "fields": [f]}
    changed = {**FORM, "fields": [{**f, "value": "California", "selected_text": "California"}]}
    c = Mock()
    c.js.side_effect = [first, first, changed]
    ns = functions(ashby=SimpleNamespace(SA={"state": "TX"}))
    result = ns["fill_job"](c, JOB, None, False)
    assert result["result"] == "needs_answers" and any("drifted" in x for x in result["unanswered"])
    c.cli.assert_not_called()


def test_unobserved_menu_holds_without_option_click():
    # the menu is polled a few times (its items render late in a background tab); none appears, so nothing is clicked
    c = Mock()
    c.js.side_effect = [{"expanded": False}] + [{"url": URL, "options": []}] * 5
    result = functions()["select_combo"](c, {"key": "state"}, ["^Texas$"], URL)
    assert result == {"ok": False}
    assert c.js.call_count == 6
    assert c.cli.call_args_list[-1].args == ("keys", "Escape")
    assert not any("imx-bamboo-pick" in str(call.args) for call in c.js.call_args_list)


def test_actual_read_contract_excludes_honeypot_and_preserves_option_text():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for offline DOM contract fixture")
    tree = ast.parse(DRIVER.read_text())
    read = next(
        ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "READ" for t in n.targets)
    )
    fixture = r"""
const el=(o)=>({disabled:false,type:'text',tagName:'INPUT',name:'',id:'',value:'',dataset:{},
 getClientRects:()=>[1],getAttribute:()=>null,closest:()=>null,labels:[],validity:{valid:true},...o});
const trap=el({name:'nickname_hpcsaf',labels:[{innerText:'Please leave this field blank'}]});
const input=el({name:'customQuestionAnswers.yes_no_1',type:'radio',value:'Yes',checked:true,
 labels:[{innerText:'Yes'}],closest:s=>s==='fieldset'?({getAttribute:()=> 'true',querySelector:()=>({innerText:'Authorized to work? *'})}):({getAttribute:()=>null,querySelector:()=>null})});
const select=el({tagName:'SELECT',name:'state.value',value:'TX',options:[{text:'Texas',value:'TX',disabled:false}],
 selectedOptions:[{text:'Texas'}],labels:[{innerText:'State *'}]});
const button=el({tagName:'BUTTON',innerText:'Submit Application',setAttribute:()=>{}});
const hidden=el({tagName:'SELECT',name:'countryId.value',getAttribute:k=>k==='aria-hidden'?'true':null});
const form={querySelectorAll:()=>[trap,input,select,hidden]};
global.document={body:{innerText:'Apply now'},querySelector:s=>s==='form#job-application-form'?form:null,
 querySelectorAll:s=>s==='button[type=submit]'?[button]:[],getElementById:()=>null};
global.location={href:'https://example.bamboohr.com/careers/42'};
"""
    result = subprocess.run(
        [node, "-e", fixture + "\nconsole.log(" + read + ")"],
        text=True,
        capture_output=True,
        check=True,
    )
    snapshot = json.loads(result.stdout)
    assert len(snapshot["fields"]) == 2
    radio, select = snapshot["fields"]
    assert (
        radio["label"] == "Authorized to work? *" and radio["option"] == "Yes" and radio["checked"]
    )
    assert select["selected_text"] == "Texas" and select["options"][0]["text"] == "Texas"
    assert snapshot["submit"]


def test_date_available_uses_explicit_profile_start_date():
    ashby = SimpleNamespace(SA={"earliest_start_date": "2026-10-01"})
    f = {**field(), "label": "Date Available *"}
    assert functions(ashby=ashby)["text_answer"](f, JOB, None, {}) == "10/01/2026"
    ashby.SA.clear()
    assert functions(ashby=ashby)["text_answer"](f, JOB, None, {}) is None


def test_captcha_form_is_filled_from_rules_for_the_person_but_never_dispatched():
    # goal 3 (2026-10-01): fill what the standing rules answer, then stop at the CAPTCHA with the tab kept; no Submit click
    empty = {**FORM, "captcha": True, "fields": [field()]}
    filled = {**FORM, "captcha": True, "fields": [{**field(), "value": "Yes"}]}
    c = Mock(session="imx-g3-univ-1")
    c.js.side_effect = [empty, empty, filled]
    rc = Mock()
    rc.set_text.return_value = {"ok": True}
    ashby = Mock()
    ashby.policy_answer.return_value = (True, "Yes")
    result = functions(rc=rc, ashby=ashby)["fill_job"](c, JOB, None, False)
    assert result["result"] == "captcha_challenge" and result["tab_kept_for_person"]
    ashby.rag.assert_not_called()
    rc.set_text.assert_called_once()
    c.cli.assert_not_called()
