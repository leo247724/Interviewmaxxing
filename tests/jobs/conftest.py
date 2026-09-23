"""Offline helpers for job-ingestion tests: fictional recorded payloads replayed by a
fake BrowserTransport. Nothing here launches a browser or touches the network."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from interviewmaxxing_jobs.opencli import check_session_name, extraction_script

FIXTURES = Path(__file__).parent.parent / "fixtures" / "jobs"
SCRIPTS = ("linkedin_search", "linkedin_detail", "builtin_search", "builtin_detail",
           "indeed_search", "indeed_detail", "google_search", "google_detail")
NOW = datetime(2026, 9, 22, 18, 0, tzinfo=UTC)


def load_fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))
    return data


def script_name(script: str) -> str:
    for name in SCRIPTS:
        if script == extraction_script(name):
            return name
    raise AssertionError("evaluate() was given a script that is not a shipped extractor")


class FakeTransport:
    """Replays fixture payloads. ``routes(session, url, script, clicked)`` returns the
    payload for the current page; every call is recorded for assertions."""

    def __init__(self, routes: Callable[[str, str, str, int | None], dict[str, Any]]):
        self.routes = routes
        self.calls: list[tuple[str, ...]] = []
        self.url: dict[str, str] = {}
        self.clicked: dict[str, int | None] = {}

    def open(self, session: str, url: str) -> None:
        check_session_name(session)
        self.calls.append(("open", session, url))
        self.url[session] = url
        self.clicked[session] = None

    def wait_for(self, session: str, selector: str, timeout_ms: int) -> bool:
        self.calls.append(("wait_for", session, selector))
        return True

    def pause(self, session: str, seconds: float) -> None:
        self.calls.append(("pause", session))

    def evaluate(self, session: str, script: str) -> Any:
        name = script_name(script)
        self.calls.append(("evaluate", session, name))
        return copy.deepcopy(self.routes(session, self.url.get(session, ""), name,
                                         self.clicked.get(session)))

    def click(self, session: str, selector: str, nth: int | None = None) -> None:
        self.calls.append(("click", session, selector, str(nth)))
        self.clicked[session] = nth

    def close(self, session: str) -> None:
        self.calls.append(("close", session))


def fixture_routes(overrides: dict[str, str] | None = None) -> Callable[..., dict[str, Any]]:
    """Routes for all four fictional sources. ``overrides`` maps a source to the
    fixture key returned for every page of that source (e.g. ``{"linkedin": "login_wall"}``)."""
    data = {s: load_fixture(s) for s in ("linkedin", "builtin", "indeed", "google")}
    overrides = overrides or {}

    def route(session: str, url: str, script: str, clicked: int | None) -> dict[str, Any]:
        source = session.removeprefix("imx-jobs-")
        fx = data[source]
        if source in overrides:
            return dict(fx[overrides[source]])
        parts = urlsplit(url)
        if script.endswith("_search"):
            if "start=" in url or "page=2" in url:
                return {**fx["search"], "cards": [], "items": []}
            return dict(fx["search"])
        if source == "linkedin":
            return dict(fx["details"][parts.path.rstrip("/").split("/")[-1]])
        if source == "builtin":
            return dict(fx["details"][parts.path.rstrip("/").split("/")[-1]])
        if source == "indeed":
            return dict(fx["details"][parse_qs(parts.query)["jk"][0]])
        return dict(fx["details"][str(clicked)])

    return route


@pytest.fixture
def clock() -> Callable[[], datetime]:
    return lambda: NOW
