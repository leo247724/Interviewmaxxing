"""Shared test configuration for every package.

* Every test runs with ``IMX_*`` pointed at a temporary directory, so no test can
  read or write the real profile, state database, artifacts or browser profile.
* Core fixtures (a fictional candidate and a mock form covering every control type)
  are available to all packages' tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_core import (
    ApplicationForm,
    ApplicationPacket,
    ApplicationStore,
    CandidateProfile,
    JobIdentityObservation,
    LocalPaths,
    SubmissionObservation,
)

CORE_FIXTURES = Path(__file__).parent / "fixtures" / "core"
FICTIONAL_RESUME = CORE_FIXTURES / "resume-avery-example.pdf"


@pytest.fixture(autouse=True)
def isolated_imx_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalPaths:
    """Point all local-data paths at a per-test temporary IMX_HOME."""
    for name in (
        "IMX_PROFILE_DIR",
        "IMX_STATE_DB",
        "IMX_ARTIFACTS_DIR",
        "IMX_BROWSER_DIR",
        "IMX_CANDIDATE_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "imx-home"
    monkeypatch.setenv("IMX_HOME", str(home))
    return LocalPaths.from_env()


def _resolve_fixture_paths(data: Any) -> Any:
    """Make fixture artifact paths (relative to the fixtures dir) absolute."""
    if isinstance(data, dict):
        out = {k: _resolve_fixture_paths(v) for k, v in data.items()}
        if "sha256" in out and "filename" in out and isinstance(out.get("path"), str):
            out["path"] = str(CORE_FIXTURES / out["path"])
        return out
    if isinstance(data, list):
        return [_resolve_fixture_paths(v) for v in data]
    return data


def load_core_fixture(name: str) -> Any:
    """Parsed JSON of ``tests/fixtures/core/<name>`` with artifact paths resolved."""
    return _resolve_fixture_paths(json.loads((CORE_FIXTURES / name).read_text()))


@pytest.fixture
def core_fixture() -> Callable[[str], Any]:
    return load_core_fixture


@pytest.fixture
def fictional_candidate() -> CandidateProfile:
    return CandidateProfile.model_validate(load_core_fixture("candidate_profile.json"))


@pytest.fixture
def mock_form() -> ApplicationForm:
    return ApplicationForm.model_validate(load_core_fixture("application_form.json"))


@pytest.fixture
def mock_packet() -> ApplicationPacket:
    return ApplicationPacket.model_validate(load_core_fixture("application_packet.json"))


@pytest.fixture
def mock_identity() -> JobIdentityObservation:
    return JobIdentityObservation.model_validate(load_core_fixture("job_identity_observation.json"))


@pytest.fixture
def accepted_observation() -> SubmissionObservation:
    return SubmissionObservation.model_validate(
        load_core_fixture("submission_observation_accepted.json")
    )


class FakeClock:
    """Deterministic, manually advanced UTC clock for store tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 22, 20, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "imx.sqlite3"


@pytest.fixture
def store(store_path: Path, clock: FakeClock) -> Iterator[ApplicationStore]:
    s = ApplicationStore.open(store_path, clock=clock)
    try:
        yield s
    finally:
        s.close()
