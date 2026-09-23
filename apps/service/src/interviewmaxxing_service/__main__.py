"""``interviewmaxxing-service``: run the local presentation service.

    IMX_SERVICE_ORIGIN=http://127.0.0.1:4317 interviewmaxxing-service

Prints ``interviewmaxxing-service listening on http://127.0.0.1:<port>`` once ready.
Stop it with Ctrl-C (SIGINT) or SIGTERM.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import signal
import sys
import threading
from types import FrameType
from typing import Any

from .app import build_app
from .config import ConfigError, ServiceConfig
from .executor import Dispatcher
from .integration import (
    LocalCandidateGateway,
    LocalJobsBackend,
    LocalSelectionBackend,
    runner_factory,
    runner_problem,
)
from .ownership import ServiceAlreadyRunning


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="interviewmaxxing-service",
        description="Loopback-only HTTP service for the Interviewmaxxing frontend. "
        "Configuration comes from IMX_* environment variables (see README).",
    )
    parser.add_argument("--port", type=int, help="override IMX_SERVICE_PORT (0 = ephemeral)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        config = ServiceConfig.from_env()
        if args.port is not None:
            config = dataclasses.replace(config, port=args.port)
    except ConfigError as exc:
        print(f"interviewmaxxing-service: {exc}", file=sys.stderr)
        return 2
    candidates = LocalCandidateGateway(config)
    backends: dict[str, Any] = {}
    unavailable: dict[str, str] = {}
    try:
        jobs_backend = LocalJobsBackend()
        backends.update(listings=jobs_backend, search=jobs_backend)
    except ImportError:
        unavailable["jobs"] = "Job search (interviewmaxxing-jobs) isn't installed in this service."
    try:
        backends["decisions"] = LocalSelectionBackend(config.paths)
    except ImportError:
        unavailable["selection"] = (
            "Jev selection (interviewmaxxing-selection) isn't installed in this service."
        )
    try:
        app = build_app(
            config,
            candidates=candidates,
            dispatcher=Dispatcher(runner_factory(config)),
            profile_loader=lambda: candidates.profile(config.candidate_id),
            runner_problem=runner_problem,
            unavailable=unavailable,
            **backends,
        )
        try:
            server = app.server()
        except BaseException:
            app.close()
            raise
    except (ServiceAlreadyRunning, OSError) as exc:
        print(f"interviewmaxxing-service: {exc}", file=sys.stderr)
        return 1

    def _stop(signum: int, frame: FrameType | None) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    address: tuple[str, int] = server.server_address[:2]  # type: ignore[assignment]
    host, port = address
    shown = f"[{host}]" if ":" in str(host) else host
    print(f"interviewmaxxing-service listening on http://{shown}:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
