"""Load the OpenRouter API key without exposing it.

The key is read from exactly one place, in this order:

1. an explicit ``env_file`` argument;
2. the file named by ``IMX_OPENROUTER_ENV_FILE``;
3. the process variable ``OPENROUTER_API_KEY``.

Only the ``OPENROUTER_API_KEY`` line of an env file is parsed; other values in the file
are never kept. The key is wrapped in :class:`ApiKey`, whose ``repr``/``str`` are
redacted. Error messages name the source and the problem, never the value.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

OPENROUTER_KEY_NAME = "OPENROUTER_API_KEY"
ENV_FILE_VARIABLE = "IMX_OPENROUTER_ENV_FILE"
REDACTED = "<redacted>"

_MAX_ENV_FILE_BYTES = 64 * 1024
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_KEY_SHAPE = re.compile(r"^[\x21-\x7e]{8,512}$")
"""Printable ASCII without whitespace: safe as an HTTP header value."""


class CredentialError(Exception):
    """The key is missing or unusable. The message never contains the key."""


class ApiKey:
    """An API key whose value is only available through :meth:`reveal`."""

    __slots__ = ("_value", "source")

    def __init__(self, value: str, *, source: str) -> None:
        if not _KEY_SHAPE.fullmatch(value):
            raise CredentialError(
                f"{OPENROUTER_KEY_NAME} from {source} is not a single printable token"
            )
        self._value = value
        self.source = source

    def reveal(self) -> str:
        return self._value

    def redact(self, text: str) -> str:
        """``text`` with every occurrence of the key replaced."""
        return text.replace(self._value, REDACTED)

    def __repr__(self) -> str:
        return f"ApiKey({REDACTED}, source={self.source!r})"

    __str__ = __repr__

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("ApiKey is not serializable")


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    # Unquoted values may carry a trailing ``# comment``.
    return raw.split(" #", 1)[0].strip()


def read_key_from_env_file(path: Path) -> ApiKey:
    """Read ``OPENROUTER_API_KEY`` from a dotenv-style file."""
    source = f"env file {path}"
    try:
        if not path.is_file():
            raise CredentialError(f"{source} does not exist or is not a file")
        if path.stat().st_size > _MAX_ENV_FILE_BYTES:
            raise CredentialError(f"{source} is unexpectedly large")
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CredentialError(f"{source} could not be read ({type(exc).__name__})") from None
    value: str | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _LINE.match(line)
        if match and match.group(1) == OPENROUTER_KEY_NAME:
            value = _unquote(match.group(2))
    if not value:
        raise CredentialError(f"{OPENROUTER_KEY_NAME} is not set in {source}")
    return ApiKey(value, source=source)


def load_api_key(
    env_file: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> ApiKey:
    """Resolve the OpenRouter key (see module docstring for the order)."""
    env = os.environ if environ is None else environ
    if env_file is not None:
        return read_key_from_env_file(env_file.expanduser())
    configured = env.get(ENV_FILE_VARIABLE)
    if configured:
        return read_key_from_env_file(Path(configured).expanduser())
    value = env.get(OPENROUTER_KEY_NAME)
    if value:
        return ApiKey(value.strip(), source="process environment")
    raise CredentialError(
        f"{OPENROUTER_KEY_NAME} is not configured; set {ENV_FILE_VARIABLE} to an ignored "
        f"env file containing it, or export {OPENROUTER_KEY_NAME}"
    )
