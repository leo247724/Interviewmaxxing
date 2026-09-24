"""worker-resource-budget: read-only snapshot parsing, identity classification and output bounds."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills/worker-resource-budget/scripts/resource_snapshot.py"
SPEC = importlib.util.spec_from_file_location("resource_snapshot", SCRIPT)
snap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snap)

START = "Wed Sep 23 04:05:27 2026"
EPOCH = time.mktime(time.strptime(START, "%a %b %d %H:%M:%S %Y"))
PS = f"""\
  100     1  204800   12.5 {START}
  101   100  102400    3.0 {START}
  102   101   51200    1.5 {START}
  200     1  409600   40.0 Wed Sep 23 03:00:00 2026
  300   100   10240    0.0 {START}
 garbage line
  400     1     abc    1.0 {START}
"""
VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               1000.
Pages active:                             5000.
Pages inactive:                           2000.
Pages speculative:                         500.
Pages wired down:                         3000.
Pages occupied by compressor:            40000.
"""
SWAP = "total = 24576.00M  used = 19558.25M  free = 5017.75M  (encrypted)"


def fake_runner(ps=PS, vm=VM_STAT, swap=SWAP, memsize="25769803776"):
    outputs = {"ps": ps, "vm_stat": vm, "hw.memsize": memsize, "vm.swapusage": swap}

    def runner(cmd):
        calls.append(cmd)
        key = cmd[0] if cmd[0] in outputs else cmd[-1]
        return outputs.get(key)
    calls = []
    runner.calls = calls
    return runner


def test_parse_ps_reads_only_numeric_columns_and_skips_bad_rows():
    procs = snap.parse_ps(PS)
    assert set(procs) == {100, 101, 102, 200, 300}
    assert procs[100] == {"pid": 100, "ppid": 1, "rss_bytes": 204800 * 1024, "cpu_pct": 12.5, "start_epoch": EPOCH}
    assert not {"comm", "args", "command", "ucomm", "env"} & set(snap.PS_COLUMNS)


def test_identity_statuses_duplicates_and_deduplicated_aggregate():
    procs = snap.parse_ps(PS)
    workers = [
        {"worker_id": "lead", "pid": 100, "expected_started_at": EPOCH},          # tree 100,101,102,300
        {"worker_id": "child", "pid": 101, "expected_started_at": EPOCH},         # inside lead's tree
        {"worker_id": "reused", "pid": 200, "expected_started_at": EPOCH},        # start differs
        {"worker_id": "gone", "pid": 999, "expected_started_at": EPOCH},
        {"worker_id": "noproof", "pid": 300},
        {"worker_id": "a", "pid": 102, "expected_started_at": EPOCH},
        {"worker_id": "b", "pid": 102, "expected_started_at": EPOCH},
        {"worker_id": "bad", "pid": "100"},
        {"worker_id": "badtime", "pid": 300, "expected_started_at": "yesterday"},
    ]
    rows, aggregate = snap.classify(workers, procs)
    status = {r["worker_id"]: r["status"] for r in rows}
    assert status == {"lead": "matched", "child": "matched", "reused": "start_mismatch", "gone": "missing",
                      "noproof": "duplicate_claim", "a": "duplicate_claim", "b": "duplicate_claim",
                      "bad": "invalid_entry", "badtime": "duplicate_claim"}
    # Overlapping matched trees are counted once; mismatched/unproven processes are excluded.
    assert aggregate["unique_pids"] == 4 and aggregate["matched_workers"] == 2
    assert aggregate["rss_footprint_bytes"] == (204800 + 102400 + 51200 + 10240) * 1024


def test_unproven_identity_when_no_start_proof():
    rows, aggregate = snap.classify([{"worker_id": "x", "pid": 300}], snap.parse_ps(PS))
    assert rows[0]["status"] == "unproven_identity" and rows[0]["tree_rss_bytes"] == 10240 * 1024
    assert aggregate["unique_pids"] == 0 and aggregate["rss_footprint_bytes"] is None and aggregate["cpu_pct"] is None


def test_expected_start_accepts_iso_with_offset_and_rejects_naive():
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(EPOCH)) + "Z"
    assert snap.parse_expected(iso) == EPOCH
    with pytest.raises(snap.SnapshotError, match="timezone"):
        snap.parse_expected("2026-09-23T04:05:27")


def test_missing_metrics_are_unknown_not_zero(monkeypatch):
    monkeypatch.setattr(snap.sys, "platform", "darwin")
    host = snap.host_metrics(fake_runner(vm=None, swap="garbage", memsize=None))
    assert host["available_estimate_bytes"] is None and host["swap_used_bytes"] is None
    assert host["mem_total_bytes"] is None and host["compressed_bytes"] is None
    assert snap.parse_vm_stat("Pages free: 10.\n") is None  # no page size => unknown
    partial = snap.parse_vm_stat("Mach (page size of 4096 bytes)\nPages free: 10.\n")
    assert partial["available_estimate_bytes"] is None
    linux = snap.parse_meminfo("MemTotal: 1000 kB\nSwapTotal: 50 kB\n")
    assert linux["available_estimate_bytes"] is None and linux["swap_used_bytes"] is None


def test_macos_memory_and_swap_parsing(monkeypatch):
    monkeypatch.setattr(snap.sys, "platform", "darwin")
    host = snap.host_metrics(fake_runner())
    assert host["mem_total_bytes"] == 25769803776
    assert host["available_estimate_bytes"] == (1000 + 2000 + 500) * 16384
    assert host["swap_used_bytes"] == int(19558.25 * snap.MB)


def test_cli_stdout_is_bounded_and_detail_file_is_private(tmp_path, capsys):
    inventory = tmp_path / "workers.json"
    inventory.write_text(json.dumps({"workers": [{"worker_id": f"w{i:02d}", "pid": 100000 + i} for i in range(40)]
                                    + [{"worker_id": "lead", "pid": 100, "expected_started_at": EPOCH}]}))
    detail = tmp_path / "snap.json"
    runner = fake_runner()
    assert snap.main(["--inventory", str(inventory), "--output", str(detail), "--limit", "5"], runner=runner) == 0
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert len(summary["workers"]) == 5 and summary["workers_truncated"] == 36
    assert summary["workers"][0]["worker_id"] == "lead" and summary["inventory"] == {"missing": 40, "matched": 1}
    assert len(out) < 4000 and out.count("\n") == 1
    assert stat.S_IMODE(detail.stat().st_mode) == 0o600
    assert len(json.loads(detail.read_text())["workers"]) == 41
    assert sum(1 for call in runner.calls if call[0] == "ps") == 1
    assert all(col in ("pid=", "ppid=", "rss=", "%cpu=", "lstart=") for col in runner.calls[0][3::2])


def test_cli_errors_are_actionable(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert snap.main(["--inventory", str(bad)], runner=fake_runner()) == 2
    assert "not valid JSON" in capsys.readouterr().err
    assert snap.main(["--inventory", str(tmp_path / "none.json")], runner=fake_runner()) == 2
    assert "cannot read inventory" in capsys.readouterr().err
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps([{"worker_id": "x", "pid": 1}]))
    assert snap.main(["--inventory", str(ok)], runner=fake_runner(ps=None)) == 2
    assert "ps is unavailable" in capsys.readouterr().err


def test_help_runs_without_side_effects():
    result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and "--inventory" in result.stdout and "never" in result.stdout


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_expected_start_never_matches(bad):
    with pytest.raises(snap.SnapshotError, match="finite"):
        snap.parse_expected(bad)
    rows, aggregate = snap.classify([{"worker_id": "x", "pid": 100, "expected_started_at": bad}], snap.parse_ps(PS))
    assert rows[0]["status"] == "unproven_identity" and "finite" in rows[0]["note"]
    assert aggregate["matched_workers"] == 0 and aggregate["rss_footprint_bytes"] is None


@pytest.mark.parametrize("expected, status", [
    (EPOCH, "matched"), (EPOCH + 0.7, "matched"),                    # same whole second
    (EPOCH + 1, "start_mismatch"), (EPOCH - 1, "start_mismatch"),    # PID reused one second apart
    (EPOCH - 0.3, "start_mismatch"),                                 # fraction of the previous second
])
def test_start_time_compares_the_same_whole_second(expected, status):
    rows, _ = snap.classify([{"worker_id": "x", "pid": 300, "expected_started_at": expected}], snap.parse_ps(PS))
    assert rows[0]["status"] == status


def test_output_errors_are_actionable(tmp_path, capsys):
    missing_parent = tmp_path / "nope" / "snap.json"
    assert snap.main(["--output", str(missing_parent)], runner=fake_runner()) == 2
    assert "--output directory does not exist" in capsys.readouterr().err
    assert not missing_parent.parent.exists()
    target_is_dir = tmp_path / "adir"
    target_is_dir.mkdir()
    assert snap.main(["--output", str(target_is_dir)], runner=fake_runner()) == 2
    assert "cannot write --output" in capsys.readouterr().err
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".snapshot-")]


def test_linux_memory_read_failure_stays_unknown(monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(snap.sys, "platform", "linux")
    monkeypatch.setattr(snap.os.path, "exists", lambda path: path == "/proc/meminfo")
    monkeypatch.setattr(snap, "open", denied, raising=False)
    host = snap.host_metrics(fake_runner())
    assert host["source"] == "/proc/meminfo"
    assert all(host[k] is None for k in ("mem_total_bytes", "available_estimate_bytes",
                                         "swap_total_bytes", "swap_used_bytes"))
