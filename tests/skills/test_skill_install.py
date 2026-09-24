"""Exercise installation isolation, repeatability, and preservation boundaries."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills/find-jobs/scripts/install.py"
SPEC = importlib.util.spec_from_file_location("find_jobs_skill_install", SCRIPT)
install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install)


@pytest.fixture
def packages(tmp_path):
    root = tmp_path / "source"
    for name in install.NAMES:
        package = root / name
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text(f"---\nname: {name}\n---\nOriginal\n")
        (package / "references").mkdir()
        (package / "references/example.md").write_text("Original reference\n")
    return root


def invoke(packages, home, *extra):
    return install.main(["--source-root", str(packages), "--home", str(home), *extra])


def test_preview_creates_nothing_and_test_home_ignores_real_codex_home(packages, tmp_path, monkeypatch):
    home = tmp_path / "isolated"
    real = tmp_path / "real-codex"
    monkeypatch.setenv("CODEX_HOME", str(real))
    assert invoke(packages, home) == 0
    assert not home.exists()
    assert not real.exists()
    assert invoke(packages, home, "--install") == 0
    assert not real.exists()
    for name in install.NAMES:
        destination = home / ".agents/skills" / name
        for directory in (home / ".codex/skills", home / ".claude/skills"):
            assert (directory / name).is_symlink()
            assert (directory / name).resolve() == destination
            assert (directory / name / "SKILL.md").read_bytes() == (packages / name / "SKILL.md").read_bytes()


def test_rerun_and_managed_update_preserve_unrelated_skills(packages, tmp_path):
    home = tmp_path / "home"
    unrelated = home / ".claude/skills/existing/SKILL.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("Must stay untouched")
    assert invoke(packages, home, "--install") == 0
    installed = home / ".agents/skills/find-jobs/SKILL.md"
    first = installed.stat().st_mtime_ns
    assert invoke(packages, home, "--install") == 0
    assert installed.stat().st_mtime_ns == first
    (packages / "find-jobs/SKILL.md").write_text("Updated skill\n")
    (packages / "find-jobs/references/example.md").unlink()
    assert invoke(packages, home, "--install") == 0
    assert installed.read_text() == "Updated skill\n"
    assert not (installed.parent / "references/example.md").exists()
    assert unrelated.read_text() == "Must stay untouched"
    marker = json.loads((installed.parent / install.MANIFEST).read_text())
    assert marker["files"] == install.inventory(installed.parent)


@pytest.mark.parametrize("collision", ["canonical", "claude", "codex", "broken-link"])
def test_conflicts_preflight_all_skills_before_copy(packages, tmp_path, collision):
    home = tmp_path / "home"
    base = {"canonical": ".agents/skills", "claude": ".claude/skills",
            "codex": ".codex/skills", "broken-link": ".claude/skills"}[collision]
    conflict = home / base / "review-job-candidates"
    conflict.parent.mkdir(parents=True)
    if collision == "broken-link":
        conflict.symlink_to(tmp_path / "missing")
    else:
        conflict.mkdir()
        (conflict / "SKILL.md").write_text("Unmanaged")
    assert invoke(packages, home, "--install") == 2
    assert not (home / ".agents/skills/find-jobs").exists()
    if collision != "broken-link":
        assert (conflict / "SKILL.md").read_text() == "Unmanaged"


def test_local_edit_prevents_update_of_any_package(packages, tmp_path):
    home = tmp_path / "home"
    assert invoke(packages, home, "--install") == 0
    edited = home / ".agents/skills/review-job-candidates/SKILL.md"
    edited.write_text("Local customization")
    (packages / "find-jobs/SKILL.md").write_text("New source version")
    assert invoke(packages, home, "--install") == 2
    assert edited.read_text() == "Local customization"
    assert "Original" in (home / ".agents/skills/find-jobs/SKILL.md").read_text()


def test_symlink_in_source_is_rejected(packages, tmp_path):
    target = tmp_path / "private-file"
    target.write_text("Do not package")
    (packages / "find-jobs/secret").symlink_to(target)
    home = tmp_path / "home"
    assert invoke(packages, home, "--install") == 2
    assert not home.exists()
