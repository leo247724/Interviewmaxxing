"""Fixtures for candidate-store tests: fictional profiles written under a temporary
``IMX_PROFILE_DIR`` (the shared autouse ``isolated_imx_home``)."""

from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_core import LocalPaths

REPO = Path(__file__).resolve().parents[2]
CORE_FIXTURES = REPO / "tests" / "fixtures" / "core"
FICTIONAL_RESUME = CORE_FIXTURES / "resume-avery-example.pdf"


def raw_core_profile() -> dict[str, Any]:
    """The canonical fictional profile JSON, unresolved (resume path is relative)."""
    data: dict[str, Any] = json.loads((CORE_FIXTURES / "candidate_profile.json").read_text())
    return data


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return path


@pytest.fixture
def paths(isolated_imx_home: LocalPaths) -> LocalPaths:
    return isolated_imx_home


@pytest.fixture
def candidate_store(paths: LocalPaths) -> LocalCandidateStore:
    return LocalCandidateStore.from_paths(paths)


@pytest.fixture
def write_candidate(paths: LocalPaths) -> Callable[..., Path]:
    """Write ``<profile_dir>/<id>/profile.json`` from the canonical fixture with the
    fictional resume copied beside it (``resume.path`` relative). ``edit`` may change
    the profile dict in place before it is written."""

    def write(
        candidate_id: str = "default",
        *,
        edit: Callable[[dict[str, Any]], None] | None = None,
        answers: list[dict[str, Any]] | None = None,
    ) -> Path:
        directory = paths.profile_dir / candidate_id
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FICTIONAL_RESUME, directory / FICTIONAL_RESUME.name)
        data = copy.deepcopy(raw_core_profile())
        data["id"] = candidate_id
        if edit is not None:
            edit(data)
        write_json(directory / "profile.json", data)
        if answers is not None:
            write_json(directory / "answers.json", answers)
        return directory

    return write
