"""Secure OpenRouter key loading: explicit file, env-file variable, process env; no leaks."""

from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from interviewmaxxing_selection import (
    ENV_FILE_VARIABLE,
    ApiKey,
    CredentialError,
    load_api_key,
    read_key_from_env_file,
)

SECRET = "sk-or-v1-fictional-secret-abcdef123456"


def write_env(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "env.local"
    path.write_text(text)
    return path


def test_reads_only_the_openrouter_line(tmp_path: Path) -> None:
    path = write_env(
        tmp_path,
        '# comment\nOTHER_SECRET=nope\nexport OPENROUTER_API_KEY="' + SECRET + '"\nX=1\n',
    )
    key = read_key_from_env_file(path)
    assert key.reveal() == SECRET
    assert not hasattr(key, "__dict__")  # nothing else retained


@pytest.mark.parametrize(
    "line",
    [
        f"OPENROUTER_API_KEY={SECRET}",
        f"OPENROUTER_API_KEY='{SECRET}'",
        f"OPENROUTER_API_KEY = {SECRET}  # trailing comment",
    ],
)
def test_dotenv_forms(tmp_path: Path, line: str) -> None:
    assert read_key_from_env_file(write_env(tmp_path, line + "\n")).reveal() == SECRET


def test_repr_str_and_errors_never_contain_the_key(tmp_path: Path) -> None:
    key = ApiKey(SECRET, source="test")
    assert SECRET not in repr(key)
    assert SECRET not in str(key)
    assert SECRET not in f"{key}"
    assert key.redact(f"bearer {SECRET} failed") == "bearer <redacted> failed"
    with pytest.raises(TypeError):
        pickle.dumps(key)
    with pytest.raises(CredentialError) as excinfo:
        ApiKey("has whitespace " + SECRET, source="test")
    assert SECRET not in str(excinfo.value)


def test_missing_and_empty(tmp_path: Path) -> None:
    with pytest.raises(CredentialError, match="does not exist"):
        read_key_from_env_file(tmp_path / "absent.env")
    with pytest.raises(CredentialError, match="is not set"):
        read_key_from_env_file(write_env(tmp_path, "OPENROUTER_API_KEY=\n"))
    with pytest.raises(CredentialError, match="not configured"):
        load_api_key(environ={})


def test_resolution_order(tmp_path: Path) -> None:
    explicit = write_env(tmp_path, f"OPENROUTER_API_KEY={SECRET}\n")
    other = tmp_path / "other.env"
    other.write_text("OPENROUTER_API_KEY=sk-or-v1-from-variable-file\n")
    env = {ENV_FILE_VARIABLE: str(other), "OPENROUTER_API_KEY": "sk-or-v1-from-process"}
    assert load_api_key(explicit, environ=env).reveal() == SECRET
    assert load_api_key(environ=env).reveal() == "sk-or-v1-from-variable-file"
    assert load_api_key(environ={"OPENROUTER_API_KEY": "sk-or-v1-from-process"}).source == (
        "process environment"
    )
