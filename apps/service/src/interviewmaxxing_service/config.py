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
``IMX_ALLOW_SUBMISSION``       (unset)                      ``1`` lets the dashboard submit
                                                            approved applications, each after
                                                            the person confirms it
=============================  ===========================  ================================

Local data paths and the candidate id come from ``LocalPaths.from_env()``
(``IMX_HOME``, ``IMX_CANDIDATE_ID``, ...).
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
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
    allow_submission: bool = False
    """``IMX_ALLOW_SUBMISSION=1`` exactly, read once at start (the CLI's ``submit`` rule).
    Without it the service builds no submission-capable runner and refuses every submit.
    With it, an application is submitted only when the person approved its preparation
    and confirms the submission in the dashboard, and TEST_ONLY still limits the service
    to local test sites."""
    browser: str = "playwright"
    opencli_profile: str | None = None
    ai_routing: bool = False
    ai_env_file: Path | None = None
    writer_model: str | None = None
    rag_connection_file: Path | None = None

    def __post_init__(self) -> None:
        if self.rag_connection_file is not None and (
            not self.ai_routing or not self.rag_connection_file.is_absolute()
        ):
            raise ConfigError("IMX_SERVICE_RAG_CONNECTION_FILE requires AI routing and an absolute path")
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
        if self.browser not in ("playwright", "opencli"):
            raise ConfigError("IMX_SERVICE_BROWSER must be playwright or opencli")
        if (self.opencli_profile is not None
                and (not self.opencli_profile.strip() or self.browser != "opencli")):
            raise ConfigError("IMX_SERVICE_OPENCLI_PROFILE requires browser opencli and a nonempty alias")
        if self.ai_routing:
            if self.ai_env_file is None or not self.ai_env_file.is_absolute():
                raise ConfigError("IMX_SERVICE_AI_ROUTING requires an absolute IMX_SERVICE_AI_ENV_FILE")
            if self.writer_model != "anthropic/claude-opus-5.5":
                raise ConfigError("IMX_SERVICE_AI_ROUTING requires IMX_SERVICE_WRITER_MODEL=anthropic/claude-opus-5.5")
        elif self.ai_env_file is not None or self.writer_model is not None:
            raise ConfigError("AI env file and writer model require IMX_SERVICE_AI_ROUTING=1")

    @property
    def dynamic_runtime(self) -> bool:
        return self.browser == "opencli" or self.ai_routing

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
        ai_flag = env.get("IMX_SERVICE_AI_ROUTING", "0").strip().lower()
        if ai_flag not in ("0", "false", "no", "off", "1", "true", "yes", "on"):
            raise ConfigError("IMX_SERVICE_AI_ROUTING must be an explicit boolean (0 or 1)")
        ai_file = env.get("IMX_SERVICE_AI_ENV_FILE", "").strip()
        return cls(
            paths=LocalPaths.from_env(env),
            allowed_origin=origin,
            host=env.get("IMX_SERVICE_HOST", "127.0.0.1"),
            port=port,
            public_base=env.get("IMX_SERVICE_PUBLIC_BASE", "/api/imx"),
            headless=env.get("IMX_SERVICE_HEADLESS", "0") in ("1", "true", "yes"),
            max_upload_bytes=max_upload,
            application_mode=env.get("IMX_SERVICE_APPLICATION_MODE", "TEST_ONLY"),
            allow_submission=env.get("IMX_ALLOW_SUBMISSION") == "1",
            browser=env.get("IMX_SERVICE_BROWSER", "playwright").strip().lower(),
            opencli_profile=env.get("IMX_SERVICE_OPENCLI_PROFILE", "").strip() or None,
            ai_routing=ai_flag in ("1", "true", "yes", "on"),
            ai_env_file=Path(ai_file).expanduser() if ai_file else None,
            writer_model=env.get("IMX_SERVICE_WRITER_MODEL", "").strip() or None,
            rag_connection_file=(Path(env["IMX_SERVICE_RAG_CONNECTION_FILE"]).expanduser()
                                 if env.get("IMX_SERVICE_RAG_CONNECTION_FILE", "").strip() else None),
        )
