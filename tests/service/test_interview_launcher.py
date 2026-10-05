"""Launcher shutdown ordering without starting processes or sending real signals."""
import importlib.util
import signal
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_interview_helper.py"
spec = importlib.util.spec_from_file_location("interview_launcher", SCRIPT)
assert spec and spec.loader
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class FakeProcess:
    def __init__(self, pid, events, timeout_once=False):
        self.pid = pid
        self.events = events
        self.status = None
        self.timeout_once = timeout_once

    def poll(self):
        return self.status

    def wait(self, timeout):
        self.events.append(("wait", self.pid, timeout))
        if self.timeout_once:
            self.timeout_once = False
            raise subprocess.TimeoutExpired("fixture", timeout)
        self.status = 0
        return 0


def test_shutdown_drains_judges_before_stopping_transport(monkeypatch):
    events = []
    owned = launcher.OwnedProcesses()
    owned.named = {name: FakeProcess(i, events) for i, name in enumerate(("web", "service", "voice", "livekit"), 1)}
    owned.children = list(owned.named.values())
    monkeypatch.setattr(launcher.os, "killpg", lambda pid, sig: events.append(("signal", pid, sig)))
    owned.close()
    service_wait = next(event for event in events if event[:2] == ("wait", 2))
    voice_wait = next(event for event in events if event[:2] == ("wait", 3))
    assert service_wait[2] >= 240 and voice_wait[2] >= 240
    transport_stop = events.index(("signal", 4, signal.SIGTERM))
    assert events.index(service_wait) < transport_stop and events.index(voice_wait) < transport_stop
    assert events[0] == ("signal", 1, signal.SIGTERM)
    assert not any(event[0] == "signal" and event[2] == signal.SIGKILL for event in events)


def test_shutdown_timeout_only_forces_owned_stalled_process(monkeypatch):
    events = []
    owned = launcher.OwnedProcesses()
    owned.named = {"voice": FakeProcess(20, events, timeout_once=True), "livekit": FakeProcess(30, events)}
    owned.children = list(owned.named.values())
    monkeypatch.setattr(launcher.os, "killpg", lambda pid, sig: events.append(("signal", pid, sig)))
    owned.close()
    first_wait = next(event for event in events if event[:2] == ("wait", 20))
    assert first_wait[2] >= 240
    assert events.index(("signal", 20, signal.SIGKILL)) > events.index(first_wait)
    assert ("signal", 30, signal.SIGKILL) not in events
