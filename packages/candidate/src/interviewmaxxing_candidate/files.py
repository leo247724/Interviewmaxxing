"""Strict JSON reading, readable validation errors and private atomic writes."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from interviewmaxxing_core import CandidateProfileInvalid

_MAX_REPORTED_ERRORS = 10


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def read_json(path: Path) -> Any:
    """Parse ``path`` as strict JSON: no duplicate keys, no NaN/Infinity.

    Raises ``CandidateProfileInvalid`` naming the file (and position) on failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise CandidateProfileInvalid(f"{path}: not UTF-8 text") from None
    except OSError as exc:
        raise CandidateProfileInvalid(f"{path}: cannot read ({exc.strerror})") from None
    try:
        return json.loads(
            text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
        )
    except json.JSONDecodeError as exc:
        raise CandidateProfileInvalid(
            f"{path}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from None
    except ValueError as exc:
        raise CandidateProfileInvalid(f"{path}: invalid JSON: {exc}") from None


def _location(prefix: str, loc: tuple[int | str, ...]) -> str:
    out = prefix
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else part
    return out or "(root)"


def describe_validation_error(path: Path, exc: ValidationError, *, prefix: str = "") -> str:
    """One line per problem, e.g. ``profile.json: facts[0].verification: Field required``."""
    errors = exc.errors(include_url=False)
    lines = [f"{path}: {len(errors)} problem(s):"]
    for err in errors[:_MAX_REPORTED_ERRORS]:
        lines.append(f"  {_location(prefix, tuple(err['loc']))}: {err['msg']}")
    if len(errors) > _MAX_REPORTED_ERRORS:
        lines.append(f"  ... and {len(errors) - _MAX_REPORTED_ERRORS} more")
    return "\n".join(lines)


def write_json_private(path: Path, data: Any) -> None:
    """Atomically replace ``path`` with ``data`` as JSON, readable only by the owner."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.chmod(0o600)
        tmp.replace(path)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
