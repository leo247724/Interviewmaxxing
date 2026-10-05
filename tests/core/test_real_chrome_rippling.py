"""Offline-only Rippling checks, with no profile imports or browser/provider calls."""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import pytest

DRIVER = Path(__file__).resolve().parents[2] / ".imx/dynamic-applications/real-chrome/rippling.py"
URL = "https://ats.rippling.com/acme/jobs/11111111-2222-3333-4444-555555555555"
JOB = dict(listing_id="fake-id", company="Acme", title="Role", url=URL, location="Austin, TX")
FORM = {"url": URL + "/apply/", "form": True, "fields": [], "submit": True}


def functions(**overrides):
    if not DRIVER.exists():
        pytest.skip("Local Rippling driver not installed")
    tree = ast.parse(DRIVER.read_text())
    ns = dict(
        re=re,
        json=json,
        os=os,
        Path=Path,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
        parse_qs=parse_qs,
        urlencode=urlencode,
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


def test_identity_query_binding_and_apply_path():
    ns = functions()
    direct = URL + "/apply?jobBoardSlug=acme&jobId=11111111-2222-3333-4444-555555555555&step=application"
    assert ns["apply_url"](URL) == direct
    assert ns["matching_form_url"]({**FORM,"url":direct}, URL)
    for query in ("jobBoardSlug=evil", "jobId=other", "jobId=11111111-2222-3333-4444-555555555555&jobId=11111111-2222-3333-4444-555555555555"):
        assert ns["job_identity"](URL + "?" + query) is None


@pytest.mark.parametrize("phase", ["initial", "final"])
@pytest.mark.parametrize(
    "wrong",
    [
        URL.replace("11111111", "99999999"),
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


def test_view_url_hold_never_opens_browser():
    c = Mock()
    result = functions()["fill_job"](
        c, {**JOB, "url": "https://jobs.workable.com/view/opaque"}, None, False
    )
    assert result["result"] == "needs_answers"
    c.open.assert_not_called()


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
    c.cli.assert_called_once_with("click", "--role", "button", "--name", "Submit application")


@pytest.mark.parametrize(
    "change",
    [
        {"url": URL.replace("11111111", "99999999") + "/apply/"},
        {"url": URL.replace("acme", "other") + "/apply/"},
        {"form": True},
        {"text": "Submit application"},
        {"captcha": True},
    ],
)
def test_rejects_false_confirmation(change):
    receipt = {
        "url": URL + "/apply/",
        "form": False,
        "text": "Your application has been submitted successfully.",
    }
    assert not functions()["confirmed"]({**receipt, **change}, URL)



def test_required_combobox_and_radio_group_cannot_be_blank():
    ns=functions()
    fields=[{"key":"1","name":"","label":"Question","type":"combobox","required":True,"value":"","valid":True},
            {"key":"2","name":"","label":"Radio question","type":"radiogroup","required":True,"value":"","valid":True}]
    assert ns["unresolved"]({"form":True,"fields":fields}) == ["Question","Radio question"]


def test_combo_rejects_unrelated_menu_without_clicking_option():
    c=Mock()
    c.js.side_effect=[True,{"url":URL.replace("acme","other"),"options":[{"value":"0","text":"Yes"}]}]
    assert functions()["select_combo"](c,{"key":"1"},["^Yes$"],URL)=={"ok":False}
    assert c.js.call_count==2


def test_combo_requires_independent_selected_readback():
    c=Mock()
    c.js.side_effect=[True,{"url":URL,"options":[{"value":"0","text":"Yes"}]},{"ok":True},{**FORM,"fields":[{"key":"1","value":"No"}]}]
    assert functions()["select_combo"](c,{"key":"1"},["^Yes$"],URL).get("ok") is False


def test_dispatch_timeout_is_uncertain_and_stops_queue():
    c=Mock(session="test",page_id="tab")
    c.js.return_value=FORM
    result=functions()["fill_job"](c,JOB,None,False)
    assert result["result"]=="send_failed"
    c.cli.assert_called_once_with("click","--role","button","--name","Submit application")


@pytest.mark.parametrize("kind", ["combobox", "select-one", "radiogroup"])
def test_later_choice_drift_prevents_submit(kind):
    f=dict(key="1",name="q",label="Authorized?",type=kind,tag="SELECT" if kind=="select-one" else "DIV",required=True,value="",valid=True,options=[dict(value="yes",text="Yes"),dict(value="no",text="No")])
    initial={**FORM,"fields":[f]}
    final={**FORM,"fields":[{**f,"value":"No" if kind=="combobox" else "no"}]}
    c=Mock()
    c.js.side_effect=[initial, {"ok":True}, final] if kind=="radiogroup" else [initial,final]
    ns=functions(rc=SimpleNamespace(set_text=Mock(return_value={"ok":True})))
    ns["choice_patterns"]=lambda *args:["^Yes$"]
    ns["select_combo"]=lambda *args:{"ok":True,"value":"Yes"}
    result=ns["fill_job"](c,JOB,None,False)
    assert result["result"]=="needs_answers"
    assert any("drifted" in s for s in result["unanswered"])
    c.cli.assert_not_called()


@pytest.mark.parametrize("kind", ["radio", "checkbox"])
def test_unmapped_native_choice_holds_without_option_key_crash(kind):
    f=dict(key="1",name="q",label="Unknown question",type=kind,tag="INPUT",required=True,value="yes",checked=False,valid=True)
    c=Mock()
    c.js.return_value={**FORM,"fields":[f]}
    assert functions()["fill_job"](c,JOB,None,False)["result"]=="needs_answers"
    c.cli.assert_not_called()


def test_phone_country_default_requires_known_us_profile_and_final_readback():
    f=dict(key="1",name="q",label="Phone country code",type="combobox",tag="INPUT",required=True,value="+1 US",valid=True)
    c=Mock()
    c.js.side_effect=[{**FORM,"fields":[f]},{**FORM,"fields":[{**f,"value":"+1 CA"}]}]
    assert functions(ashby=SimpleNamespace(SA={"country":"United States"}))["fill_job"](c,JOB,None,False)["result"]=="needs_answers"
    c.cli.assert_not_called()


def test_resume_dom_upload_receipt_required_even_after_attach_success():
    f=dict(key="1",name="",label="Resume",type="file",tag="INPUT",required=True,value="",files=0,valid=True)
    c=Mock()
    c.js.side_effect=[{**FORM,"fields":[f]},{**FORM,"fields":[{**f,"files":1,"uploaded_names":["wrong.pdf"]}]}]
    ns=functions(rc=SimpleNamespace(attach=Mock(return_value={"ok":True,"name":"resume.pdf"})))
    result=ns["fill_job"](c,JOB,None,False)
    assert result["result"]=="needs_answers"
    assert 'Resume upload not independently confirmed' in result['unanswered']
    c.cli.assert_not_called()


def test_only_verified_same_reservation_receipt_reconciles_uncertain_ledger():
    ns=functions()
    failed={"result":"send_failed","company_reservation":"token"}
    assert ns["has_unreconciled_dispatch"]([failed])
    assert ns["has_unreconciled_dispatch"]([failed,{"result":"submitted","company_reservation":"other","verified":URL}])
    assert ns["has_unreconciled_dispatch"]([failed,{"result":"submitted","company_reservation":"token"}])
    assert not ns["has_unreconciled_dispatch"]([failed,{"result":"submitted","company_reservation":"token","verified":URL}])


def test_observed_rippling_success_requires_confirmation_step():
    receipt={"url":URL+"/apply?step=confirmation","form":False,"text":"Confirmation\nYou have successfully applied to Senior Performance Marketing Manager"}
    assert functions()["confirmed"](receipt,URL)
    assert not functions()["confirmed"]({**receipt,"url":URL+"/apply?step=application"},URL)
    assert not functions()["confirmed"]({**receipt,"url":URL+"/apply?step=confirmation&step=application"},URL)


def test_known_unsupported_attachment_stops_before_provider_spend():
    essay=dict(key="1",name="q",label="Tell us a story",type="textarea",tag="TEXTAREA",required=True,value="",valid=True)
    attachment=dict(key="2",name="",label="Cover letter",type="file",tag="INPUT",required=True,value="",files=0,valid=True)
    c=Mock()
    c.js.return_value={**FORM,"fields":[essay,attachment]}
    ns=functions()
    ns["text_answer"]=Mock(side_effect=AssertionError("Provider must not run"))
    assert ns["fill_job"](c,JOB,None,False)["result"]=="needs_answers"
    ns["text_answer"].assert_not_called()


def test_first_required_needs_input_stops_repeated_provider_requests():
    f=dict(key="1",name="q",label="Unknown required story",type="textarea",tag="TEXTAREA",required=True,value="",valid=True)
    c=Mock()
    c.js.return_value={**FORM,"fields":[f,{**f,"key":"2","label":"Another story"}]}
    ns=functions()
    ns["text_answer"]=Mock(return_value=None)
    assert ns["fill_job"](c,JOB,None,False)["result"]=="needs_answers"
    assert ns["text_answer"].call_count==1
    c.cli.assert_not_called()


def test_unknown_optional_essay_never_calls_provider():
    ashby=SimpleNamespace(SA={},RULES=[],TEXT_DEFAULTS=[],policy_answer=lambda *args:(False,None),rag=Mock(side_effect=AssertionError("Optional essay should stay blank")))
    f=dict(label="Optional fun fact",tag="TEXTAREA",type="textarea",id="",name="",required=False)
    assert functions(ashby=ashby)["text_answer"](f,JOB,None,{"cost":0}) is None
    ashby.rag.assert_not_called()
