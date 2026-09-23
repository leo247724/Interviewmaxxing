from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from interviewmaxxing_core import LocationPriority
from interviewmaxxing_jobs.cli import build_query, main, parser

EXAMPLE = Path(__file__).parent.parent.parent / "examples" / "job-search.example.json"


def test_example_query_is_the_users_default_search() -> None:
    query = build_query(parser().parse_args(["search", "--query-file", str(EXAMPLE)]))
    assert query.title_phrases == ["marketing manager", "marketing director"]
    assert [t.location for t in query.onsite] == ["Austin, TX"]
    assert query.remote is not None and query.remote.eligible_region == "United States"
    assert query.location_priority is LocationPriority.STRONGLY_PREFER_ONSITE_HYBRID
    assert query.minimum_compensation is not None
    assert (query.minimum_compensation.amount, query.minimum_compensation.currency) == (100_000, "USD")
    assert query.max_results_per_source == 50


def test_cli_edits_keywords_location_and_priority() -> None:
    query = build_query(parser().parse_args([
        "smoke", "--title", "brand manager", "--keyword", "B2B", "--onsite", "Round Rock, TX",
        "--no-remote", "--sources", "indeed,builtin", "--location-priority", "BALANCED"]))
    assert query.title_phrases == ["brand manager"] and query.keywords == ["B2B"]
    assert query.remote is None and query.sources == ["indeed", "builtin"]
    assert query.max_results_per_source == 2
    assert query.location_priority is LocationPriority.BALANCED


def test_listings_command_reads_an_empty_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["listings", "--db", str(tmp_path / "j.sqlite3")]) == 0
    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.skipif(os.environ.get("IMX_JOBS_LIVE") != "1",
                    reason="live OpenCLI smoke is opt-in: IMX_JOBS_LIVE=1")
def test_live_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    """Opt-in: a tiny real search through the user's connected browser profile.
    Prints only public job metadata; stores into a temporary database."""
    sources = os.environ.get("IMX_JOBS_LIVE_SOURCES", "linkedin,builtin,indeed,google")
    main(["smoke", "--sources", sources, "--limit", "2", "--details", "1"])
    report = json.loads(capsys.readouterr().out)
    states = {r["source"]: r["state"] for r in report["sources"]}
    assert set(states) == set(sources.split(","))
    for r in report["sources"]:
        assert r["state"] in {"OK", "PARTIAL", "NEEDS_USER", "BLOCKED", "ERROR"}
        if r["state"] != "OK":
            assert r["message"]
