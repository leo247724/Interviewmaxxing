"""Shared helpers for the I1 end-to-end tests.

Everything is local and fictional: the mock ATS (``scripts/mock_ats.py``) runs as a
separate process on a loopback port, the candidate is the mock's fictional "Avery
Quill", and every CLI call is a fresh ``interviewmaxxing`` process (so each step is a
process restart) with ``IMX_HOME`` in a temporary directory.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MOCK_ATS = ROOT / "scripts" / "mock_ats.py"
FIXTURES = ROOT / "tests" / "fixtures" / "browser"
RESUME = FIXTURES / "resume_avery_quill.pdf"
CLI = Path(sys.executable).with_name("interviewmaxxing")
VERIFIED_AT = "2026-09-01T12:00:00Z"

# Exact question wording (label, help text, placeholder) as the mock ATS shows it.
Q_WORK_AUTH = "Are you legally authorized to work in the United States?"
Q_SPONSORSHIP = "Will you now or in the future require visa sponsorship?"
Q_YEARS = "Years of professional experience"
Q_SKILLS = "Primary skills\nHold Ctrl or Command to select more than one."
Q_WHY = "Why do you want to work at Brambleway Analytics?"


class MockServer:
    """``scripts/mock_ats.py`` as a separate process (never the in-process class)."""

    def __init__(self, state_dir: Path) -> None:
        ready = state_dir.parent / "mock-ready.json"
        self.proc = subprocess.Popen(
            [sys.executable, str(MOCK_ATS), "--state-dir", str(state_dir),
             "--ready-file", str(ready)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 20
        while not ready.exists():
            if self.proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("mock ATS did not start: " + (self.proc.stderr.read() if self.proc.stderr else ""))
            time.sleep(0.05)
        self.origin: str = json.loads(ready.read_text())["origin"]

    def url(self, job: str) -> str:
        """The application form URL."""
        return f"{self.origin}/jobs/{job}/apply"

    def posting(self, job: str) -> str:
        """The job posting URL (links to the form and to the public status page)."""
        return f"{self.origin}/jobs/{job}"

    def get(self, path: str) -> Any:
        with urllib.request.urlopen(self.origin + path, timeout=10) as response:
            return json.loads(response.read())

    def post(self, path: str) -> Any:
        request = urllib.request.Request(self.origin + path, method="POST", data=b"")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def submissions(self, job: str) -> dict[str, Any]:
        return self.get(f"/__test__/submissions?job_id={job}")  # type: ignore[no-any-return]

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _saved(answer_id: str, question: str, value: Any, semantic_type: str | None = None,
           **scope: Any) -> dict[str, Any]:
    return {
        "id": answer_id, "scope": scope.pop("scope", "GLOBAL"), "semantic_type": semantic_type,
        "question": question, "value": value, "confirmed_at": VERIFIED_AT, **scope,
    }


def write_profile(profile_dir: Path, *, candidate_id: str = "default",
                  why_job_url: str | None = None) -> Path:
    """The fictional candidate, with saved answers for the standard job's explicit and
    custom questions (the user saved these earlier). Nothing covers the questions of
    the missing-required, attestation or validation jobs."""
    directory = profile_dir / candidate_id
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RESUME, directory / "resume.pdf")

    def fact(fid: str, key: str, value: Any) -> dict[str, Any]:
        return {"id": fid, "key": key, "value": value, "source": "user",
                "verification": {"status": "VERIFIED", "method": "USER_STATED",
                                 "verified_at": VERIFIED_AT}}

    saved = [
        _saved("sa.work_auth", Q_WORK_AUTH, "Yes, I am authorized to work in the US",
               "WORK_AUTHORIZATION"),
        _saved("sa.sponsorship", Q_SPONSORSHIP, "No, I will not require sponsorship", "SPONSORSHIP"),
        _saved("sa.years", Q_YEARS, "6 to 9 years"),
        _saved("sa.skills", Q_SKILLS, ["Python", "SQL", "Apache Spark", "dbt"]),
    ]
    if why_job_url:
        saved.append(_saved("sa.why", Q_WHY, "I have built data platforms for seven years and "
                            "want to work on logistics forecasting.", scope="JOB",
                            job_url=why_job_url, employer="Brambleway Analytics"))
    profile = {
        "id": candidate_id,
        "identity": {
            "first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
            "phone": "+1 (303) 555-0142",
            "linkedin_url": "https://www.linkedin.example.test/in/avery-quill",
            "address": {"city": "Denver", "region": "CO", "country": "United States"},
            "verified_at": VERIFIED_AT,
        },
        "resume": {"id": "resume_supplied", "path": "resume.pdf"},
        "facts": [fact("fact.years", "years_professional_experience", 7)],
        "saved_answers": saved,
    }
    path = directory / "profile.json"
    path.write_text(json.dumps(profile, indent=2))
    return path


def switch_resume(profile_path: Path, content: bytes, *, name: str, resume_id: str) -> Path:
    """Point the profile at a different resume file (the user changed resumes)."""
    resume = profile_path.parent / name
    resume.write_bytes(content)
    profile = json.loads(profile_path.read_text())
    profile["resume"] = {"id": resume_id, "path": name}
    profile_path.write_text(json.dumps(profile, indent=2))
    return resume


@dataclass
class CliResult:
    code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        return json.loads(self.stdout)


class Cli:
    """Runs the installed ``interviewmaxxing`` entry point as a new process each time."""

    def __init__(self, home: Path, artifacts: Path) -> None:
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("IMX_")}
        self.env["IMX_HOME"] = str(home)
        self.artifacts = artifacts

    def __call__(self, *args: str, timeout: float = 180) -> CliResult:
        proc = subprocess.run([str(CLI), *args], capture_output=True, text=True,
                              env=self.env, timeout=timeout)
        result = CliResult(proc.returncode, proc.stdout, proc.stderr)
        log = self.artifacts / "cli.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as fh:
            fh.write(f"$ interviewmaxxing {' '.join(args)}\n[exit {proc.returncode}]\n"
                     f"{proc.stdout}\n{proc.stderr}\n")
        return result
