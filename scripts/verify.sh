#!/usr/bin/env bash
# Interviewmaxxing local verification: the single command every worktree runs.
#
#   scripts/verify.sh            # full: lockfile-exact install, lint, types, all tests, CLI
#   scripts/verify.sh -k store   # extra arguments are passed to pytest
#
# Uses only this worktree: its own .venv and temporary IMX_HOME. Nothing is sent
# anywhere and no real profile or state is touched.
set -euo pipefail

cd "$(dirname "$0")/.."

export IMX_HOME
IMX_HOME="$(mktemp -d "${TMPDIR:-/tmp}/imx-verify.XXXXXX")"
trap 'rm -rf "$IMX_HOME"' EXIT

step() { printf '\n==> %s\n' "$*"; }

step "uv sync --locked --all-packages"
uv sync --locked --all-packages

step "ruff check"
uv run --no-sync ruff check .

step "mypy (strict, workspace sources)"
uv run --no-sync mypy

step "pytest"
uv run --no-sync pytest "$@"

step "CLI smoke"
uv run --no-sync interviewmaxxing --help >/dev/null
uv run --no-sync interviewmaxxing paths >/dev/null
set +e
uv run --no-sync interviewmaxxing apply "http://127.0.0.1:9/jobs/verify-smoke" >/dev/null 2>&1
code=$?
set -e
if [ "$code" -ne 3 ]; then
  echo "expected the apply skeleton to exit 3 (INCOMPLETE), got $code" >&2
  exit 1
fi

step "OK"
