"""Fixtures for pipeline tests. Fictional data only."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from interviewmaxxing_pipeline import ImportDocument, PipelineStore, load_import

PIPELINE_FIXTURES = Path(__file__).parents[1] / "fixtures" / "pipeline"


@pytest.fixture
def pipeline_fixtures() -> Path:
    return PIPELINE_FIXTURES


@pytest.fixture
def pipeline_db(tmp_path: Path) -> Path:
    return tmp_path / "state" / "pipeline.sqlite3"


@pytest.fixture
def pipeline(pipeline_db: Path, clock) -> Iterator[PipelineStore]:
    store = PipelineStore.open(pipeline_db, clock=clock)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def export_doc() -> ImportDocument:
    return load_import(PIPELINE_FIXTURES / "pipeline_export.json")


@pytest.fixture
def csv_doc() -> ImportDocument:
    return load_import(PIPELINE_FIXTURES / "pipeline.csv")
