#!/usr/bin/env python3
"""Run an isolated local Interview Helper stack; never stop pre-existing processes."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import TextIO
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".imx" / "interview-helper"
WEB = ROOT / "apps" / "web"
PORTS = {"dashboard": 4327, "service": 8775, "voice worker": 8767}


def private_write(path: Path, content: str) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlink at {path.name}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)


def available(port: int, *, udp: bool = False) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def is_local() -> bool:
    parsed = urlparse(os.getenv("LIVEKIT_URL") or "ws://127.0.0.1:7880")
    return parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port in (None, 7880)


def load_environment() -> None:
    from dotenv import load_dotenv

    from interviewmaxxing_service.interview_providers import load_interview_env

    os.environ.setdefault("IMX_INTERVIEW_ENV_FILE", str(ROOT / "env.local"))
    load_interview_env()
    # Local generated credentials persist outside version control. Explicit process
    # or env.local values always take precedence.
    if is_local() and (STATE / "local.env").is_file():
        load_dotenv(STATE / "local.env", override=False)
    os.environ.update({
        "IMX_HOME": str(STATE / "runtime"),
        "IMX_PROFILE_DIR": str(Path.home() / ".interviewmaxxing" / "profile"),
        # The dashboard's pipeline and the interview job picker show the canonical board, not an
        # empty sandbox copy (service lock, applications and interviews stay in the runtime dir).
        "IMX_PIPELINE_DB": str(Path.home() / ".interviewmaxxing" / "state" / "pipeline.sqlite3"),
        "IMX_SERVICE_HOST": "127.0.0.1", "IMX_SERVICE_PORT": "8775",
        "IMX_SERVICE_ORIGIN": "http://127.0.0.1:4327",
        "IMX_BACKEND_URL": "http://127.0.0.1:8775", "IMX_WEB_ORIGIN": "http://127.0.0.1:4327",
        "IMX_VOICE_WORKER_PORT": "8767", "IMX_SERVICE_APPLICATION_MODE": "TEST_ONLY",
        "IMX_ALLOW_SUBMISSION": "0",
    })


def configure_local() -> Path:
    for key, fallback in (("LIVEKIT_URL", "ws://127.0.0.1:7880"),
                          ("LIVEKIT_API_KEY", "imx_" + secrets.token_hex(8)),
                          ("LIVEKIT_API_SECRET", secrets.token_urlsafe(36))):
        if not os.getenv(key):
            os.environ[key] = fallback
    private_write(STATE / "local.env", "".join(f"{key}={json.dumps(os.environ[key])}\n" for key in (
        "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")))
    key, secret = os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]
    config = ("port: 7880\nbind_addresses: [127.0.0.1]\nrtc:\n  tcp_port: 7881\n"
              "  port_range_start: 50100\n  port_range_end: 50200\n"
              "  use_external_ip: false\n  node_ip: 127.0.0.1\n"
              "  enable_loopback_candidate: true\nkeys:\n"
              f"  {json.dumps(key)}: {json.dumps(secret)}\n")
    path = STATE / "livekit.yaml"
    private_write(path, config)
    return path


class OwnedProcesses:
    def __init__(self) -> None:
        self.children: list[subprocess.Popen[str]] = []
        self.named: dict[str, subprocess.Popen[str]] = {}
        self.readers: list[threading.Thread] = []
        self.logs: list[TextIO] = []
        self.secrets = [value for key, value in os.environ.items() if value and len(value) >= 6 and
                        any(marker in key.upper() for marker in ("KEY", "SECRET", "TOKEN", "PASSWORD"))]

    def start(self, name: str, command: list[str], cwd: Path = ROOT) -> subprocess.Popen[str]:
        log_path = STATE / "logs" / f"{name}.log"
        if log_path.is_symlink():
            raise RuntimeError("Refusing a symlink log file")
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        log = os.fdopen(fd, "w")
        os.fchmod(log.fileno(), 0o600)
        self.logs.append(log)
        process = subprocess.Popen(command, cwd=cwd, env=os.environ.copy(), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   errors="replace", start_new_session=True)
        self.children.append(process)
        self.named[name] = process
        def drain() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                for value in self.secrets:
                    line = line.replace(value, "[REDACTED]")
                line = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED_TOKEN]", line)
                log.write(line)
                log.flush()
            process.stdout.close()
        reader = threading.Thread(target=drain, name=f"{name}-log", daemon=True)
        reader.start()
        self.readers.append(reader)
        print(f"Started {name}; private log: {log_path.relative_to(ROOT)}", flush=True)
        return process

    def wait_port(self, process: subprocess.Popen[str], port: int, timeout: float = 45) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"A child exited while starting port {port}; inspect its private log.")
            with socket.socket() as probe:
                probe.settimeout(.25)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    return
            time.sleep(.2)
        raise RuntimeError(f"Timed out waiting for port {port}; inspect its private log.")

    def _signal(self, processes: list[subprocess.Popen[str]], sig: signal.Signals) -> None:
        # Only process groups created and retained by this launcher are eligible.
        for process in processes:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, sig)

    def _stop_group(self, names: tuple[str, ...], grace: float) -> None:
        processes = [self.named[name] for name in names if name in self.named]
        self._signal(processes, signal.SIGTERM)
        deadline = time.monotonic() + grace
        for process in processes:
            try:
                process.wait(timeout=max(.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                self._signal([process], signal.SIGKILL)
                process.wait(timeout=5)

    def close(self) -> None:
        try:
            # Stop incoming browser requests first. Keep media transport alive while
            # both judges finish and the worker executes its own 240-second drain.
            self._stop_group(("web", "web-build"), 10)
            if any(self.named[name].poll() is None for name in ("service", "voice") if name in self.named):
                print("Draining service and voice judgments (up to 250 seconds); a second Ctrl-C forces owned processes to stop.", flush=True)
            self._stop_group(("service", "voice"), 250)
            self._stop_group(("livekit",), 10)
        except KeyboardInterrupt:
            print("Forced stop requested; pending judgments may need retry after restart.", flush=True)
            self._signal(self.children, signal.SIGKILL)
            for process in self.children:
                process.wait(timeout=5)
        finally:
            for reader in self.readers:
                reader.join(timeout=2)
            for log in self.logs:
                log.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true", help="Use the existing apps/web/.next production build.")
    parser.add_argument("--check", action="store_true", help="Read-only local configuration/port check; never start or stop processes.")
    args = parser.parse_args(argv)
    load_environment()
    local = is_local()
    ports = {**PORTS, **({"LiveKit": 7880, "LiveKit RTC TCP": 7881} if local else {})}
    conflicts = [f"{name} ({port})" for name, port in ports.items() if not available(port)]
    udp_conflicts = [port for port in range(50100, 50201) if not available(port, udp=True)] if local else []
    missing = [key for key in ("OPENROUTER_API_KEY", "ELEVEN_API_KEY") if not os.getenv(key)]
    if not local:
        missing.extend(key for key in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET") if not os.getenv(key))
    if args.check:
        print("Transport:", "local self-hosted LiveKit" if local else "configured remote LiveKit")
        print("Provider configuration:", "missing " + ", ".join(missing) if missing else "present; connectivity not tested")
        print("Ports already in use:", ", ".join(conflicts) or "none")
        print("RTC UDP ports in use:", ", ".join(map(str, udp_conflicts)) or "none")
        print("Runtime:", STATE / "runtime")
        print("Canonical candidate profile:", os.environ["IMX_PROFILE_DIR"])
        print("Canonical pipeline:", os.environ["IMX_PIPELINE_DB"])
        return 0
    if conflicts or udp_conflicts:
        print("Refusing to start: required ports are already occupied. No existing processes were stopped.", file=sys.stderr)
        print(", ".join(conflicts + [f"UDP {port}" for port in udp_conflicts]), file=sys.stderr)
        return 2
    if missing:
        print("Configure these keys in ignored env.local: " + ", ".join(missing), file=sys.stderr)
        return 2
    node = shutil.which("node")
    livekit = shutil.which("livekit-server") if local else None
    next_cli = WEB / "node_modules" / "next" / "dist" / "bin" / "next"
    if not node or not next_cli.is_file() or (local and not livekit):
        print("Install web dependencies (npm ci in apps/web), Node and local livekit-server first.", file=sys.stderr)
        return 2
    os.umask(0o077)
    (STATE / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
    config = configure_local() if local else None
    children = OwnedProcesses()
    def interrupted(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    build = None
    try:
        if args.skip_build:
            if not (WEB / ".next" / "BUILD_ID").is_file():
                raise RuntimeError("No production build exists. Run without --skip-build.")
        else:
            build = children.start("web-build", [node, str(next_cli), "build"], WEB)
            try:
                exit_code = build.wait(timeout=300)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Web build exceeded five minutes; inspect web-build.log.") from exc
            if exit_code:
                raise RuntimeError("Web build failed; inspect web-build.log.")
        if config:
            assert livekit is not None
            transport = children.start("livekit", [livekit, "--config", str(config)])
            children.wait_port(transport, 7880)
        service = children.start("service", [sys.executable, "-m", "interviewmaxxing_service"])
        children.wait_port(service, 8775)
        voice = children.start("voice", [sys.executable, "-m", "interviewmaxxing_service.interview_voice", "start"])
        children.wait_port(voice, 8767)
        web = children.start("web", [node, str(next_cli), "start", "--hostname", "127.0.0.1", "--port", "4327"], WEB)
        children.wait_port(web, 4327)
        print("Interview Helper: http://127.0.0.1:4327/interviews\nCtrl-C stops only this launcher's children.", flush=True)
        active = [child for child in children.children if child is not build]
        while all(child.poll() is None for child in active):
            time.sleep(.5)
        raise RuntimeError("An owned component exited. Inspect its private log before restarting.")
    except KeyboardInterrupt:
        print("Stopping owned Interview Helper processes.")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"Interview Helper: {exc}", file=sys.stderr)
        return 1
    finally:
        children.close()


if __name__ == "__main__":
    raise SystemExit(main())
