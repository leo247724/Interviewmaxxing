"""Service configuration: loopback binding, the approved frontend origin and bounds.

=============================  ===========================  ================================
Variable                       Default                      Meaning
=============================  ===========================  ================================
``IMX_SERVICE_HOST``           ``127.0.0.1``                Bind address; must be loopback
``IMX_SERVICE_PORT``           ``8765``                     Bind port (``0`` = ephemeral)
``IMX_SERVICE_ORIGIN``         (required)                   Exact frontend origin allowed to
                                                            send mutations, e.g.
                                                            ``http://127.0.0.1:4317``
``IMX_SERVICE_PUBLIC_BASE``    ``/api/imx``                 Path prefix the browser uses to
                                                            reach this service (evidence links)
``IMX_SERVICE_HEADLESS``       ``0``                        ``1`` runs the browser headless
``IMX_SERVICE_MAX_UPLOAD``     ``10485760``                 Resume upload limit in bytes
``IMX_SERVICE_APPLICATION_MODE`` ``TEST_ONLY``              ``TEST_ONLY`` or ``LIVE`` (see below)
=============================  ===========================  ================================

Local data paths and the candidate id come from ``LocalPaths.from_env()``
(``IMX_HOME``, ``IMX_CANDIDATE_ID``, ...).
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from interviewmaxxing_core import LocalPaths

DEFAULT_PORT = 8765
DEFAULT_MAX_UPLOAD = 10 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024


class ConfigError(ValueError):
    pass


def is_loopback_host(host: str) -> bool:
    name = host.strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def normalize_origin(origin: str) -> str:
    """``scheme://host[:port]`` exactly, for a loopback http(s) origin."""
    parts = urlsplit(origin.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("IMX_SERVICE_ORIGIN must look like http://127.0.0.1:4317")
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise ConfigError("IMX_SERVICE_ORIGIN must be an origin, without a path or credentials")
    if not is_loopback_host(parts.hostname):
        raise ConfigError("IMX_SERVICE_ORIGIN must be a loopback origin (the local frontend)")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    paths: LocalPaths
    allowed_origin: str
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    public_base: str = "/api/imx"
    headless: bool = False
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD
    max_json_bytes: int = MAX_JSON_BYTES
    reconcile_wait_s: float = 25.0
    """How long ``reconcile`` (recheck) waits for the browser check before answering."""
    application_mode: str = "TEST_ONLY"
    """``TEST_ONLY`` (default): application runs, resumes and site rechecks may only
    target loopback test sites; job discovery and selection are unaffected. ``LIVE``
    must be chosen explicitly (``IMX_SERVICE_APPLICATION_MODE=LIVE``)."""

    def __post_init__(self) -> None:
        if not is_loopback_host(self.host):
            raise ConfigError(f"refusing to bind a non-loopback address {self.host!r}")
        if not 0 <= self.port <= 65535:
            raise ConfigError("port out of range")
        object.__setattr__(self, "allowed_origin", normalize_origin(self.allowed_origin))
        if not self.public_base.startswith("/") or "//" in self.public_base:
            raise ConfigError("IMX_SERVICE_PUBLIC_BASE must be an absolute path such as /api/imx")
        object.__setattr__(self, "public_base", self.public_base.rstrip("/"))
        if self.max_upload_bytes < 1:
            raise ConfigError("upload limit must be positive")
        mode = self.application_mode.strip().upper()
        if mode not in ("TEST_ONLY", "LIVE"):
            raise ConfigError("IMX_SERVICE_APPLICATION_MODE must be TEST_ONLY or LIVE")
        object.__setattr__(self, "application_mode", mode)

    @property
    def candidate_id(self) -> str:
        return self.paths.candidate_id

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServiceConfig:
        env = os.environ if env is None else env
        origin = env.get("IMX_SERVICE_ORIGIN", "").strip()
        if not origin:
            raise ConfigError(
                "set IMX_SERVICE_ORIGIN to the frontend's exact origin, e.g. http://127.0.0.1:4317"
            )
        try:
            port = int(env.get("IMX_SERVICE_PORT", str(DEFAULT_PORT)))
            max_upload = int(env.get("IMX_SERVICE_MAX_UPLOAD", str(DEFAULT_MAX_UPLOAD)))
        except ValueError as exc:
            raise ConfigError("IMX_SERVICE_PORT and IMX_SERVICE_MAX_UPLOAD must be integers") from exc
        return cls(
            paths=LocalPaths.from_env(env),
            allowed_origin=origin,
            host=env.get("IMX_SERVICE_HOST", "127.0.0.1"),
            port=port,
            public_base=env.get("IMX_SERVICE_PUBLIC_BASE", "/api/imx"),
            headless=env.get("IMX_SERVICE_HEADLESS", "0") in ("1", "true", "yes"),
            max_upload_bytes=max_upload,
            application_mode=env.get("IMX_SERVICE_APPLICATION_MODE", "TEST_ONLY"),
        )
