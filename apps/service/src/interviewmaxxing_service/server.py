"""Loopback-only HTTP transport (standard library ``http.server``).

Boundary rules, applied before any route runs:

* The server binds a loopback address only (``ServiceConfig``), and every request's
  ``Host`` must name that loopback address and port. This blocks DNS rebinding.
* Mutations (every ``POST``) need ``Origin`` equal to the configured frontend origin,
  byte for byte, and are refused when ``Sec-Fetch-Site`` says ``cross-site``. A ``GET``
  carrying a different ``Origin`` is refused too. No CORS headers are ever sent.
* ``POST`` bodies need an exact content type (``application/json``, or
  ``application/octet-stream`` for resume uploads) and a ``Content-Length`` within
  bounds. Query strings are refused everywhere: personal data travels in bodies only.
* Logs name the method, route template and status. Never bodies, ids, file paths or
  URLs.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from pydantic import BaseModel, TypeAdapter, ValidationError

from . import errors
from .config import ServiceConfig
from .discovery_models import (
    ImportInput,
    PipelineEntryInput,
    PipelineMoveInput,
    PipelineUpdateInput,
    SearchPreferencesInput,
)
from .models import (
    AnswerInput,
    EmptyBody,
    RecheckInput,
    ReconcileInput,
    StartApplicationInput,
    UserConfirmedNotReceivedInput,
    UserFoundConfirmationInput,
)
from .service import PresentationService

if TYPE_CHECKING:
    from .jobs_api import JobsApi
    from .pipeline_api import PipelineApi

log = logging.getLogger("interviewmaxxing.service.http")

FILENAME_HEADER = "X-Imx-Filename"
MAX_IMPORT_BODY_BYTES = 4 * 1024 * 1024
"""Pipeline import preview body: the frontend sends up to 2 MiB of file text, which
grows when JSON-escaped; P1 itself caps the parsed file at 5 MiB."""
_RECONCILE: TypeAdapter[
    RecheckInput | UserFoundConfirmationInput | UserConfirmedNotReceivedInput
] = TypeAdapter(ReconcileInput)
_APP = r"(?P<app>[^/]+)"
Route = tuple[str, re.Pattern[str], str]
ROUTES: list[Route] = [
    ("GET", re.compile(r"^/healthz$"), "health"),
    ("GET", re.compile(r"^/candidate$"), "candidate"),
    ("POST", re.compile(r"^/resumes$"), "upload"),
    ("POST", re.compile(r"^/applications$"), "start"),
    ("GET", re.compile(rf"^/applications/{_APP}$"), "status"),
    ("POST", re.compile(rf"^/applications/{_APP}/answers$"), "answers"),
    ("POST", re.compile(rf"^/applications/{_APP}/resume$"), "resume"),
    ("POST", re.compile(rf"^/applications/{_APP}/reconcile$"), "reconcile"),
    ("GET", re.compile(rf"^/applications/{_APP}/evidence/(?P<evidence>[^/]+)$"), "evidence"),
    ("GET", re.compile(r"^/pipeline$"), "board"),
    ("POST", re.compile(r"^/pipeline/entries$"), "entry_create"),
    ("POST", re.compile(r"^/pipeline/entries/(?P<entry>[^/]+)$"), "entry_update"),
    ("POST", re.compile(r"^/pipeline/entries/(?P<entry>[^/]+)/move$"), "entry_move"),
    ("POST", re.compile(r"^/pipeline/import/preview$"), "import_preview"),
    ("POST", re.compile(r"^/pipeline/import/(?P<preview>[^/]+)/commit$"), "import_commit"),
    ("GET", re.compile(r"^/selection/preferences$"), "prefs_get"),
    ("POST", re.compile(r"^/selection/preferences$"), "prefs_save"),
    ("POST", re.compile(r"^/selection/jobs/(?P<listing>[^/]+)$"), "decide"),
    ("POST", re.compile(r"^/jobs/search$"), "search_start"),
    ("GET", re.compile(r"^/jobs/search/(?P<run>[^/]+)$"), "search_status"),
    ("GET", re.compile(r"^/jobs$"), "listings"),
    ("GET", re.compile(r"^/jobs/(?P<listing>[^/]+)$"), "listing"),
    ("POST", re.compile(r"^/jobs/(?P<listing>[^/]+)/track$"), "track"),
]
_TEMPLATES = {
    "health": "/healthz",
    "candidate": "/candidate",
    "upload": "/resumes",
    "start": "/applications",
    "status": "/applications/{id}",
    "answers": "/applications/{id}/answers",
    "resume": "/applications/{id}/resume",
    "reconcile": "/applications/{id}/reconcile",
    "evidence": "/applications/{id}/evidence/{id}",
    "board": "/pipeline",
    "entry_create": "/pipeline/entries",
    "entry_update": "/pipeline/entries/{id}",
    "entry_move": "/pipeline/entries/{id}/move",
    "import_preview": "/pipeline/import/preview",
    "import_commit": "/pipeline/import/{id}/commit",
    "prefs_get": "/selection/preferences",
    "prefs_save": "/selection/preferences",
    "decide": "/selection/jobs/{id}",
    "search_start": "/jobs/search",
    "search_status": "/jobs/search/{id}",
    "listings": "/jobs",
    "listing": "/jobs/{id}",
    "track": "/jobs/{id}/track",
}


def _reject_constant(value: str) -> Any:
    raise ValueError(f"{value} is not valid JSON here")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _field_errors(exc: ValidationError) -> dict[str, str]:
    out: dict[str, str] = {}
    for err in exc.errors():
        loc = [str(p) for p in err["loc"] if not isinstance(p, int)]
        key = loc[-1] if loc else "body"
        if err["type"] == "missing":
            out.setdefault(key, "This is required.")
        elif err["type"] == "extra_forbidden":
            out.setdefault(key, "This field isn't accepted.")
        else:
            out.setdefault(key, "This value has the wrong type or format.")
    return out


class ServiceHandler(BaseHTTPRequestHandler):
    server_version = "interviewmaxxing-service"
    sys_version = ""
    timeout = 30

    service: PresentationService  # set on the subclass by make_server
    config: ServiceConfig
    pipeline: PipelineApi | None
    jobs: JobsApi | None

    # --- logging --------------------------------------------------------------------

    _route_name = "unmatched"

    def log_message(self, format: str, *args: Any) -> None:
        return  # replaced by log_request below

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        log.info("%s %s %s", self.command, _TEMPLATES.get(self._route_name, "?"), code)

    # --- entry points -----------------------------------------------------------------

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _unsupported(self) -> None:
        self._send_error(errors.ApiError(405, "invalid", "Method not allowed."))

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _unsupported

    def _handle(self, method: str) -> None:
        try:
            self._check_host()
            path = self._path()
            handler, params = self._route(method, path)
            self._check_origin(method)
            handler(**params)
        except errors.ApiError as exc:
            self._send_error(exc)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client left; any recorded work carries on
        except Exception as exc:
            log.error("unexpected error in %s: %s", self._route_name, type(exc).__name__)
            self._send_error(
                errors.ApiError(500, "unknown", "Something went wrong in the application service.")
            )

    # --- boundary checks ----------------------------------------------------------------

    def _check_host(self) -> None:
        host = self.headers.get("Host", "")
        allowed = {
            f"{name}:{self.config_port}"
            for name in ("127.0.0.1", "localhost", "[::1]")
        }
        if host.lower() not in allowed:
            raise errors.forbidden("This service only answers requests addressed to localhost.")

    @property
    def config_port(self) -> int:
        address: Any = self.server.server_address
        return int(address[1])

    def _path(self) -> str:
        raw = self.path
        if "?" in raw or "#" in raw:
            raise errors.invalid("Query strings are not accepted; send data in the request body.")
        if not raw.startswith("/"):
            raise errors.invalid("Bad request path.")
        return raw

    def _route(self, method: str, path: str) -> tuple[Callable[..., None], dict[str, str]]:
        allowed_methods = []
        for route_method, pattern, name in ROUTES:
            match = pattern.match(path)
            if match is None:
                continue
            if route_method != method:
                allowed_methods.append(route_method)
                continue
            self._route_name = name
            params = {k: unquote(v, errors="strict") for k, v in match.groupdict().items()}
            return getattr(self, f"_r_{name}"), params
        if allowed_methods:
            raise errors.ApiError(405, "invalid", "Method not allowed.")
        raise errors.not_found("No such route.")

    def _check_origin(self, method: str) -> None:
        origin = self.headers.get("Origin")
        if method != "POST":
            if origin is not None and origin != self.config.allowed_origin:
                raise errors.forbidden("Requests from that origin are not accepted.")
            return
        if origin != self.config.allowed_origin:
            raise errors.forbidden("Changes are only accepted from the Interviewmaxxing app.")
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            raise errors.forbidden("Cross-site requests are not accepted.")

    def _content_type(self) -> str:
        return self.headers.get("Content-Type", "").split(";")[0].strip().lower()

    def _read_body(self, limit: int) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise errors.ApiError(411, "invalid", "Send the body with a Content-Length.")
        raw = self.headers.get("Content-Length")
        if raw is None or not raw.isdigit():
            raise errors.ApiError(411, "invalid", "Send the body with a Content-Length.")
        length = int(raw)
        if length > limit:
            raise errors.ApiError(413, "invalid", "The request body is too large.")
        body = self.rfile.read(length)
        if len(body) != length:
            raise errors.invalid("The request body was cut short.")
        return body

    def _json(self, model: type[BaseModel] | TypeAdapter[Any], *, limit: int | None = None) -> Any:
        if self._content_type() != "application/json":
            raise errors.ApiError(415, "invalid", "Send JSON with Content-Type: application/json.")
        body = self._read_body(limit or self.config.max_json_bytes)
        try:
            data = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_no_duplicates,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise errors.invalid("The request body isn't valid JSON.") from exc
        if not isinstance(data, dict):
            raise errors.invalid("The request body must be a JSON object.")
        try:
            if isinstance(model, TypeAdapter):
                return model.validate_python(data)
            return model.model_validate(data)
        except ValidationError as exc:
            fields = _field_errors(exc)
            raise errors.ApiError(
                400, "invalid", "Some of the request is missing or malformed.", fields
            ) from exc

    # --- responses ------------------------------------------------------------------------

    def _common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._common_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, exc: errors.ApiError) -> None:
        try:
            self.close_connection = True
            self._send_json(exc.status, exc.body())
        except (BrokenPipeError, ConnectionResetError):
            pass

    # --- routes ---------------------------------------------------------------------------

    def _r_health(self) -> None:
        health = self.service.health()
        health["pipeline"] = "available" if self.pipeline is not None else "unavailable"
        health.update(self.jobs.status() if self.jobs is not None
                      else {"jobs": "unavailable", "selection": "unavailable"})
        self._send_json(200, health)

    def _r_candidate(self) -> None:
        self._send_json(200, self.service.get_candidate().dump())

    def _r_upload(self) -> None:
        if self._content_type() != "application/octet-stream":
            raise errors.ApiError(
                415, "invalid", "Send the file bytes with Content-Type: application/octet-stream."
            )
        encoded = self.headers.get(FILENAME_HEADER)
        if not encoded:
            raise errors.invalid(
                "Name the file in the X-Imx-Filename header.", {"resumeFile": "The file has no name."}
            )
        try:
            if not encoded.isascii():
                raise ValueError("header must be percent-encoded ASCII")
            filename = unquote(encoded, encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise errors.invalid(
                "The file name header isn't percent-encoded UTF-8.",
                {"resumeFile": "Rename the file and upload it again."},
            ) from exc
        content = self._read_body(self.config.max_upload_bytes)
        self._send_json(201, self.service.upload_resume(filename, content).dump())

    def _r_start(self) -> None:
        body = self._json(StartApplicationInput)
        view, created = self.service.start(body)
        self._send_json(201 if created else 200, view.dump())

    def _r_status(self, app: str) -> None:
        self._send_json(200, self.service.status(app).dump())

    def _r_answers(self, app: str) -> None:
        body = self._json(AnswerInput)
        self._send_json(200, self.service.answer(app, body).dump())

    def _r_resume(self, app: str) -> None:
        self._json(EmptyBody)
        self._send_json(200, self.service.resume(app).dump())

    def _r_reconcile(self, app: str) -> None:
        body = self._json(_RECONCILE)
        self._send_json(200, self.service.reconcile(app, body).dump())

    def _r_evidence(self, app: str, evidence: str) -> None:
        item = self.service.evidence(app, evidence)
        data = item.path.read_bytes()
        disposition = "attachment" if item.attachment else "inline"
        self.send_response(200)
        self.send_header("Content-Type", item.media_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'{disposition}; filename="{item.download_name}"')
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self._common_headers()
        self.end_headers()
        self.wfile.write(data)


    # --- pipeline, jobs and selection (S2) --------------------------------------------------

    def _pipeline(self) -> PipelineApi:
        if self.pipeline is None:
            raise errors.unavailable("The pipeline tracker isn't installed in this service.")
        return self.pipeline

    def _jobs(self) -> JobsApi:
        if self.jobs is None:
            raise errors.unavailable("Job search and selection aren't installed in this service.")
        return self.jobs

    def _r_board(self) -> None:
        self._send_json(200, self._pipeline().board().dump())

    def _r_entry_create(self) -> None:
        body = self._json(PipelineEntryInput)
        self._send_json(201, self._pipeline().create(body).dump())

    def _r_entry_update(self, entry: str) -> None:
        body = self._json(PipelineUpdateInput)
        self._send_json(200, self._pipeline().update(entry, body).dump())

    def _r_entry_move(self, entry: str) -> None:
        body = self._json(PipelineMoveInput)
        self._send_json(200, self._pipeline().move(entry, body).dump())

    def _r_import_preview(self) -> None:
        body = self._json(ImportInput, limit=MAX_IMPORT_BODY_BYTES)
        self._send_json(200, self._pipeline().preview_import(body).dump())

    def _r_import_commit(self, preview: str) -> None:
        self._json(EmptyBody)
        self._send_json(200, self._pipeline().commit_import(preview).dump())

    def _r_prefs_get(self) -> None:
        self._send_json(200, self._jobs().get_preferences().dump())

    def _r_prefs_save(self) -> None:
        body = self._json(SearchPreferencesInput)
        self._send_json(200, self._jobs().save_preferences(body).dump())

    def _r_decide(self, listing: str) -> None:
        self._json(EmptyBody)
        view, finished = self._jobs().decide(listing)
        self._send_json(200 if finished else 202, view.dump())

    def _r_search_start(self) -> None:
        body = self._json(SearchPreferencesInput)
        run, _created = self._jobs().start_search(body)
        self._send_json(202, run.dump())

    def _r_search_status(self, run: str) -> None:
        self._send_json(200, self._jobs().search_status(run).dump())

    def _r_listings(self) -> None:
        self._send_json(200, self._jobs().list_listings().dump())

    def _r_listing(self, listing: str) -> None:
        self._send_json(200, self._jobs().listing(listing).dump())

    def _r_track(self, listing: str) -> None:
        self._json(EmptyBody)
        view, created = self._jobs().track(listing)
        self._send_json(201 if created else 200, view.dump())


class LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: ServiceConfig, handler: type[ServiceHandler]) -> None:
        host = "127.0.0.1" if config.host == "localhost" else config.host.strip("[]")
        if ipaddress.ip_address(host).version == 6:
            self.address_family = socket.AF_INET6
        super().__init__((host, config.port), handler)


def make_server(
    service: PresentationService,
    *,
    pipeline: PipelineApi | None = None,
    jobs: JobsApi | None = None,
) -> LoopbackHTTPServer:
    handler = type(
        "BoundServiceHandler",
        (ServiceHandler,),
        {"service": service, "config": service.config, "pipeline": pipeline, "jobs": jobs},
    )
    return LoopbackHTTPServer(service.config, handler)
