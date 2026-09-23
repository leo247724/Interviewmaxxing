#!/usr/bin/env bash
# Interviewmaxxing local verification: the single command every worktree runs.
#
#   scripts/verify.sh            # full: locked install, lint, types, tests, CLI, browser E2E
#   scripts/verify.sh -k store   # extra arguments are passed to the unit/integration pytest
#   IMX_SKIP_E2E=1 scripts/verify.sh   # skip the real-Chromium end-to-end suite
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
# No profile in the temporary IMX_HOME: apply must stop (3) before opening a browser.
uv run --no-sync interviewmaxxing apply "http://127.0.0.1:9/jobs/verify-smoke" --headless >/dev/null 2>&1
code=$?
set -e
if [ "$code" -ne 3 ]; then
  echo "expected apply without a profile to exit 3 (not submitted), got $code" >&2
  exit 1
fi

if [ "${IMX_SKIP_E2E:-0}" != "1" ]; then
  step "Chromium for Playwright (no-op when already installed)"
  uv run --no-sync playwright install chromium

  step "end-to-end: installed CLI + real Chromium + localhost mock ATS"
  uv run --no-sync pytest e2e
fi

step "OK"
