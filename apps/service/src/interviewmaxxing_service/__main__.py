"""``interviewmaxxing-service``: run the local presentation service.

    IMX_SERVICE_ORIGIN=http://127.0.0.1:4317 interviewmaxxing-service

Prints ``interviewmaxxing-service listening on http://127.0.0.1:<port>`` once ready.
Stop it with Ctrl-C (SIGINT) or SIGTERM.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from types import FrameType

from .config import ConfigError, ServiceConfig
from .executor import Dispatcher
from .integration import LocalCandidateGateway, runner_factory
from .server import make_server
from .service import PresentationService


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
            config = ServiceConfig(
                paths=config.paths, allowed_origin=config.allowed_origin, host=config.host,
                port=args.port, public_base=config.public_base, headless=config.headless,
                max_upload_bytes=config.max_upload_bytes,
            )
    except ConfigError as exc:
        print(f"interviewmaxxing-service: {exc}", file=sys.stderr)
        return 2
    config.paths.ensure()
    dispatcher = Dispatcher(runner_factory(config))
    service = PresentationService(
        config, candidates=LocalCandidateGateway(config), dispatcher=dispatcher
    )
    service.recover()
    server = make_server(service)

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
        dispatcher.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
