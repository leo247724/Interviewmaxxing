"""Read bounded, versioned backend priors; the live form remains authoritative."""
from __future__ import annotations

import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from interviewmaxxing_core.urls import normalize_application_url

BACKEND_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
MAX_MAP_BYTES = 512_000
MAX_HINT_BYTES = 24_000


def _default_directory() -> Path | None:
    configured = os.environ.get("IMX_SCHEMA_CATALOG_DIR")
    if configured:
        return Path(configured).expanduser()
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "application-schemas"
        if candidate.is_dir():
            return candidate
    return None


class SchemaCatalog:
    """Read-only local cache exported from the Supabase schema-map registry.

    Priors contain observed field families and known gaps, never executable actions.
    No database credentials or network work are required on a per-field hot path.
    Invalid, absent or oversized maps return no hint instead of blocking observation.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else _default_directory()
        self._cache: dict[str, tuple[tuple[int, int, int], dict[str, Any]]] = {}
        self._url_index: tuple[tuple[int, int, int], dict[str, str]] | None = None

    def backend_for(self, url: str, fallback: str = "generic") -> str:
        """Exact Saved URL classification when exported alongside backend priors."""
        if self.path is None:
            return fallback
        index = self.path / "_url_index.json"
        try:
            stat = index.stat()
            if stat.st_size > 2_000_000 or index.is_symlink():
                return fallback
            signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            if self._url_index is None or self._url_index[0] != signature:
                raw = json.loads(index.read_text())
                if not isinstance(raw, dict) or any(
                    not isinstance(k, str) or not isinstance(v, str)
                    or not BACKEND_NAME.fullmatch(v) for k, v in raw.items()
                ):
                    return fallback
                self._url_index = (signature, raw)
            return self._url_index[1].get(normalize_application_url(url), fallback)
        except (OSError, ValueError, TypeError):
            return fallback

    def _read(self, backend: str) -> dict[str, Any] | None:
        if self.path is None or not BACKEND_NAME.fullmatch(backend):
            return None
        filename = self.path / f"{backend}.json"
        try:
            if filename.is_symlink():
                return None
            stat = filename.stat()
            if stat.st_size > MAX_MAP_BYTES:
                return None
            signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            cached = self._cache.get(backend)
            if cached is not None and cached[0] == signature:
                return cached[1]
            body = filename.read_bytes()
            if len(body) > MAX_MAP_BYTES:
                return None
            record = json.loads(body)
            if not isinstance(record, dict) or record.get("backend") != backend:
                return None
            observed = record.get("observed")
            if not isinstance(observed, dict):
                observed = {}
            hint = {
                "backend": backend,
                "schema_version": record.get("schema_version"),
                "map_sha256": hashlib.sha256(json.dumps(
                    record, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                "observed_at": record.get("generated_at_utc"),
                "coverage": record.get("coverage", {}),
                "field_patterns": observed.get("field_patterns", []),
                "control_families": observed.get("control_families", {}),
                "step_patterns": observed.get("step_patterns", []),
                "conditional_patterns": observed.get("conditional_patterns", []),
                "blockers": observed.get("blockers", []),
                "provider_hint": record.get("provider_hint", {}),
                "authority": "untrusted_prior_only",
            }
            if len(json.dumps(hint).encode()) > MAX_HINT_BYTES:
                # A large catalogue cannot crowd out the actual question. Preserve
                # version metadata; the live field is sufficient to classify.
                hint = {k: hint[k] for k in ("backend", "schema_version", "map_sha256",
                    "observed_at", "authority")}
                hint["detail_omitted"] = "map exceeds bounded hint context"
            if len(self._cache) >= 128 and backend not in self._cache:
                self._cache.pop(next(iter(self._cache)))
            self._cache[backend] = (signature, hint)
            return hint
        except (OSError, ValueError, TypeError):
            return None

    def hint(self, backend: str, url: str) -> dict[str, Any] | None:
        # URL only selects context. A prior cannot navigate or change the target.
        try:
            scheme = urlsplit(url).scheme
        except ValueError:
            return None
        if scheme not in {"http", "https"}:
            return None
        hint = self._read(self.backend_for(url, backend))
        if hint is None:
            return None
        copy: dict[str, Any] = json.loads(json.dumps(hint))
        return copy  # callers cannot mutate the cached prior


@lru_cache(maxsize=8)
def _catalog_at(path: Path | None) -> SchemaCatalog:
    return SchemaCatalog(path)


def load_schema_hint(backend: str, url: str) -> dict[str, Any] | None:
    return _catalog_at(_default_directory()).hint(backend, url)
