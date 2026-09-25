"""``scripts/rag_answers.py`` import-facts and remove-facts against a fictional profile in a
temporary IMX_HOME (round 5, addendum item 8): a confirmed fact whose own evidence dates or
places it elsewhere is listed and not imported, a row the story index marked superseded is
listed and never imported, and remove-facts removes facts by id."""
from __future__ import annotations

import json
import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_core import LocalPaths

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rag_answers.py"


def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict[str, Any]]:
    module = runpy.run_path(str(SCRIPT), run_name="rag_answers_under_test")
    monkeypatch.setattr(sys, "argv", ["rag_answers.py", *args])
    code = module["main"]()
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1]) if out else {}


def test_import_lists_self_contradicting_and_superseded_facts_and_remove_removes(
        paths: LocalPaths, write_candidate: Callable[..., Path], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def add_role(profile: dict[str, Any]) -> None:
        profile["experience"].append({"id": "exp.harbor", "company": "Harbor Growth Solutions", "title": "Marketing Manager",
                                      "start": "2023-10", "end": "2024-02", "current": False, "summary": None, "fact_ids": []})

    write_candidate("default", edit=add_role)
    good = {"id": "sf_0123456789abcdef_000000000001", "key": "achievement",
            "value": "I grew free athlete sign-ups by 40% in one season (youth sports recruiting network, 2019-08 to 2020-05)",
            "evidence": ["Story 05: Growth Marketer, Tidewater Recruiting (Aug 2019 - May 2020)", "period_source: story"]}
    stale = {"id": "sf_0123456789abcdef_000000000002", "key": "achievement",
             "value": "I wrote the weekly report for the founders (resume: Harbor Growth Solutions, 2023-10 to 2024-02)",
             "evidence": ["Story 05: Growth Marketer, Tidewater Recruiting (Aug 2019 - May 2020)",
                          "period_source: resume_role", "resume_role_id: exp.harbor"]}
    superseded = {"id": "sf_0123456789abcdef_000000000003", "superseded": True, "reason": "story_link_changed"}
    confirm = tmp_path / "facts.confirm.json"
    confirm.write_text(json.dumps([good, stale, superseded]), encoding="utf-8")
    code, result = run(monkeypatch, capsys, "import-facts", "--file", str(confirm))
    assert code == 0
    assert result == {"imported_facts": 1, "rejected": [{"id": stale["id"], "reasons": ["evidence_period_contradicts_fact_period"]}],
                      "superseded_not_imported": [superseded["id"]], "index_refresh_required": True}
    store = LocalCandidateStore.from_paths(paths)
    profile = store.load("default")
    imported = profile.find_fact(good["id"])
    assert imported is not None and imported.is_verified and imported.source == "user:confirmed fact import"
    assert profile.find_fact(stale["id"]) is None and profile.find_fact(superseded["id"]) is None
    assert "Harbor Growth Solutions" not in json.dumps(result)  # reason codes only, never values
    # A superseded row may carry nothing else.
    confirm.write_text(json.dumps([{**superseded, "value": "smuggled"}]), encoding="utf-8")
    assert run(monkeypatch, capsys, "import-facts", "--file", str(confirm))[0] == 1
    # remove-facts removes by id and names ids it did not know.
    code, result = run(monkeypatch, capsys, "remove-facts", "--ids", f"{good['id']},sf_unknown")
    assert code == 0 and result == {"removed_facts": [good["id"]], "unknown_ids": ["sf_unknown"],
                                    "index_refresh_required": True}
    assert store.load("default").find_fact(good["id"]) is None
    assert run(monkeypatch, capsys, "remove-facts", "--ids", " , ")[0] == 1
