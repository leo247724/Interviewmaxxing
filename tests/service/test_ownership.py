"""A live service exclusively owns recovery and workers for its shared task DB."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest

from interviewmaxxing_core import LocalPaths, WorkArrangement
from interviewmaxxing_service import jobs_api
from interviewmaxxing_service.ownership import ServiceAlreadyRunning, ServiceOwnership

from .conftest import FakeCandidates, FictionalSite, serve
from .test_jobs_routes import FakeDecisions, FakeListings, FakeSearch, listing


@pytest.mark.parametrize("other_candidate", ["candidate-a", "candidate-b"])
def test_duplicate_start_cannot_interrupt_or_duplicate_live_work(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other_candidate: str,
) -> None:
    monkeypatch.setattr(jobs_api, "DECISION_WAIT_S", 0.03)
    paths = dataclasses.replace(isolated_imx_home, candidate_id="candidate-a")
    repo, decisions = FakeListings(), FakeDecisions()
    items = [listing(f"queue-{i}", f"Fictional Analyst {i}", location="Austin, TX",
                     arrangement=WorkArrangement.HYBRID) for i in range(2)]
    repo.items.update({item.id: item for item in items})
    decisions.gate.clear()
    with serve(paths, fictional_site, FakeCandidates(tmp_path / "a"),
               listings=repo, search=FakeSearch(repo), decisions=decisions) as a:
        try:
            initial = [a.client.post(f"/selection/jobs/{x.id}", {}).json["decisionTask"]
                       for x in items]
            assert [task["state"] for task in initial] == ["RUNNING", "QUEUED"]
            with pytest.raises(ServiceAlreadyRunning), serve(
                dataclasses.replace(paths, candidate_id=other_candidate), fictional_site,
                FakeCandidates(tmp_path / "b"), listings=repo, search=FakeSearch(repo),
                decisions=FakeDecisions(),
            ):
                pytest.fail("a second service acquired the shared task database")
            after = [a.client.get(f"/jobs/{x.id}").json["decisionTask"] for x in items]
            assert [(task["id"], task["state"]) for task in after] == [
                (task["id"], task["state"]) for task in initial
            ]
            retried = a.client.post(f"/selection/jobs/{items[0].id}", {}).json["decisionTask"]
            assert retried["id"] == initial[0]["id"]
        finally:
            decisions.gate.set()
            a.app.jobs._decision_pool.submit(lambda: None).result(10)
        assert decisions.calls == 2
    # A real restart can acquire ownership after the first service has stopped.
    with serve(paths, fictional_site, FakeCandidates(tmp_path / "a")):
        pass


def test_duplicate_cli_start_refuses_before_recovery(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, tmp_path: Path,
) -> None:
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "a")) as a:
        task, _ = a.app.state.create_or_join(
            candidate_id="default", kind="decision", dedupe_key="live", request={},
            prefix="dec_", subject="fictional-job",
        )
        a.app.state.update(task.id, state="RUNNING")
        env = {key: value for key, value in os.environ.items() if not key.startswith("IMX_")}
        env.update(IMX_HOME=str(isolated_imx_home.state_db.parent.parent),
                   IMX_SERVICE_ORIGIN="http://127.0.0.1:4317",
                   IMX_SERVICE_APPLICATION_MODE="TEST_ONLY")
        result = subprocess.run(
            [sys.executable, "-m", "interviewmaxxing_service", "--port",
             str(a.client.port)], env=env, capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 1, result.stderr
        assert "already running" in result.stderr
        assert a.app.state.get(task.id).state == "RUNNING"


def test_process_exit_releases_lock_without_removing_its_file(tmp_path: Path) -> None:
    path = tmp_path / "service.lock"
    script = """import os, sys
from pathlib import Path
from interviewmaxxing_service.ownership import ServiceOwnership
owner = ServiceOwnership(Path(sys.argv[1]))
print('owned', flush=True)
sys.stdin.readline()
os._exit(0)
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "owned"
        with pytest.raises(ServiceAlreadyRunning):
            ServiceOwnership(path)
        child.communicate("exit\n", timeout=10)
        assert child.returncode == 0
        assert path.exists() and path.stat().st_mode & 0o777 == 0o600
        owner = ServiceOwnership(path)
        owner.close()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
