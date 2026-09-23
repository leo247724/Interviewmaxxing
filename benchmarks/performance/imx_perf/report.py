"""Result writers: JSON files plus small Markdown tables."""

from __future__ import annotations

import json
import hashlib
import sys
import platform
import subprocess
import time
from pathlib import Path
from typing import Any


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", "utf-8")


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    def cell(v: Any) -> str:
        if isinstance(v, float):
            return f"{v:,.3f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.0f}"
        if isinstance(v, int):
            return f"{v:,}"
        return str(v)

    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(cell(v) for v in row) + " |")
    return "\n".join(out)


def git_head(path: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=False, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def run_metadata(repo: Path) -> dict[str, Any]:
    load = None
    try:
        import os
        load = os.getloadavg()
    except (AttributeError, OSError):
        pass
    return {
        "argv": sys.argv,
        "harness_sha256": hashlib.sha256(b"".join(
            p.name.encode() + p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py"))
        )).hexdigest(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "repo_head": git_head(repo),
        "load_average_1_5_15": load,
    }
