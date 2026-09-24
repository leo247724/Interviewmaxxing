#!/usr/bin/env python3
"""Install the local job-search skills for Codex and Claude.

Preview by default. Managed copies live outside any worktree, so deleting a
checkout does not break either agent. Refuse to replace unrelated/local edits.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

NAMES = (
    "find-jobs", "find-jobs-source", "review-job-candidates", "find-jobs-fast-batches",
    "coordinate-worker-pool", "worker-resource-budget", "find-jobs-save-batches",
)
MANIFEST = ".interviewmaxxing-skill.json"
OWNER = "interviewmaxxing.find-jobs-skills.v1"


class InstallError(ValueError):
    pass


def inventory(root: Path) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink():
        raise InstallError(f"Expected an ordinary skill directory: {root}")
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.name in {MANIFEST, ".DS_Store"}:
            continue
        if path.is_symlink():
            raise InstallError(f"Unexpected symlink inside skill package: {path}")
        if path.is_file():
            result[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir():
            raise InstallError(f"Unsupported file type: {path}")
    if "SKILL.md" not in result:
        raise InstallError(f"Missing SKILL.md: {root}")
    return result


def exists(path: Path) -> bool:
    return os.path.lexists(path)


def make_plan(source: Path, home: Path, codex_home: Path) -> list[dict]:
    shared = home / ".agents" / "skills"
    plan = []
    for name in NAMES:
        package = source / name
        files = inventory(package)
        destination = shared / name
        action = "install"
        if exists(destination):
            current = inventory(destination)
            try:
                marker = json.loads((destination / MANIFEST).read_text())
            except (OSError, ValueError) as exc:
                raise InstallError(f"Unmanaged existing skill; will not overwrite: {destination}") from exc
            if (marker.get("owner") != OWNER or marker.get("name") != name
                    or marker.get("files") != current):
                raise InstallError(f"Installed skill has local changes; will not overwrite: {destination}")
            action = "unchanged" if current == files else "update"
        links = [codex_home / "skills" / name, home / ".claude" / "skills" / name]
        for link in links:
            if link == destination:
                continue
            if exists(link) and (not link.is_symlink() or link.resolve() != destination.resolve()):
                raise InstallError(f"Conflicting skill path; will not replace: {link}")
        plan.append({"name": name, "source": str(package), "destination": str(destination),
                     "action": action, "files": files, "links": [str(p) for p in links]})
    return plan


def install_package(item: dict) -> None:
    source = Path(item["source"])
    destination = Path(item["destination"])
    if item["action"] != "unchanged":
        staging = Path(tempfile.mkdtemp(prefix=f".{item['name']}-", dir=destination.parent))
        backup = staging.with_name(staging.name + "-previous")
        replaced = False
        try:
            for relative in item["files"]:
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / relative, target)
            if inventory(staging) != item["files"]:
                raise InstallError(f"Source changed during installation: {source}")
            (staging / MANIFEST).write_text(json.dumps({
                "owner": OWNER, "name": item["name"], "files": item["files"],
            }, indent=2) + "\n")
            if exists(destination):
                destination.rename(backup)
            staging.rename(destination)
            replaced = True
        finally:
            if not replaced and exists(backup) and not exists(destination):
                backup.rename(destination)
            if staging.exists():
                shutil.rmtree(staging)
            if replaced and backup.exists():
                shutil.rmtree(backup)
    for raw in item["links"]:
        link = Path(raw)
        if link == destination:
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        if not exists(link):
            link.symlink_to(destination, target_is_directory=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--home", type=Path, help="Alternate home for an isolated installation test")
    parser.add_argument("--codex-home", type=Path, help="Optional explicit Codex compatibility directory")
    parser.add_argument("--install", action="store_true", help="Apply the validated plan; otherwise preview")
    args = parser.parse_args(argv)
    home = (args.home or Path.home()).expanduser().resolve()
    if args.codex_home:
        codex_home = args.codex_home.expanduser().resolve()
    elif args.home:
        codex_home = home / ".codex"  # A test home must never inherit the real CODEX_HOME.
    else:
        codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser().resolve()
    try:
        source = args.source_root.expanduser().resolve()
        plan = make_plan(source, home, codex_home)
        if args.install:
            shared = home / ".agents" / "skills"
            shared.mkdir(parents=True, exist_ok=True)
            with (shared / ".interviewmaxxing-install.lock").open("a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                plan = make_plan(source, home, codex_home)
                for item in plan:
                    install_package(item)
        print(json.dumps({"installed": args.install, "skills": [
            {k: v for k, v in item.items() if k != "files"} for item in plan
        ]}, indent=2))
        return 0
    except (InstallError, OSError) as exc:
        print(f"install-find-jobs-skills: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
