"""Fixtures for the I1 CLI + real Chromium end-to-end tests.

Run with ``.venv/bin/python -m pytest e2e`` (not part of the default ``tests`` run).
On failure, the test's CLI log, screenshots, HTML and page text are copied to the
ignored ``e2e/.artifacts/<test name>/`` directory.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # importlib mode: make support importable

from support import Cli, MockServer, write_profile

ARTIFACTS = Path(__file__).resolve().parent / ".artifacts"


@pytest.fixture
def ats(tmp_path: Path) -> Iterator[MockServer]:
    server = MockServer(tmp_path / "mock-state")
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "imx-home"


@pytest.fixture
def cli(home: Path, tmp_path: Path) -> Cli:
    return Cli(home, tmp_path / "logs")


@pytest.fixture
def profile(home: Path, ats: MockServer) -> Path:
    return write_profile(home / "profile", why_job_url=ats.url("standard"))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    tmp = item.funcargs.get("tmp_path")
    if not isinstance(tmp, Path):
        return
    target = ARTIFACTS / item.name
    shutil.rmtree(target, ignore_errors=True)
    for name in ("logs", "imx-home/artifacts"):
        source = tmp / name
        if source.exists():
            shutil.copytree(source, target / name.replace("/", "-"))
