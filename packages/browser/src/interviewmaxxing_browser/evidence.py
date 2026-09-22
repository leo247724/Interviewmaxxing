"""Evidence files under the artifacts directory, referenced by ``EvidenceRef``.

Files are written to ``BrowserOptions.artifacts_dir`` (``<artifacts_root>/<app id>``)
and referenced by POSIX paths relative to ``artifacts_root``, as CONTRACTS.md
section 5 requires. They may contain personal data and live only in ignored local
storage.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath

from interviewmaxxing_core import EvidenceKind, EvidenceRef

from .driver import PageDriver


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:48] or "evidence"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EvidenceRecorder:
    def __init__(self, artifacts_dir: Path, artifacts_root: Path) -> None:
        root = artifacts_root.expanduser().resolve()
        directory = artifacts_dir.expanduser().resolve()
        try:
            relative = directory.relative_to(root)
        except ValueError:
            raise ValueError(
                f"artifacts_dir {directory} must be inside artifacts_root {root}"
            ) from None
        self.directory = directory
        self._relative = PurePosixPath(*relative.parts)
        self._seq = 0

    def _path(self, label: str, suffix: str) -> tuple[Path, str]:
        name = f"{self._seq:03d}-{_slug(label)}{suffix}"
        return self.directory / name, str(self._relative / name)

    def _ref(self, kind: EvidenceKind, path: Path, rel: str, description: str) -> EvidenceRef:
        return EvidenceRef(kind=kind, path=rel, sha256=_digest(path), description=description)

    async def capture(
        self,
        driver: PageDriver,
        label: str,
        *,
        description: str,
        html: bool = False,
        text: str | None = None,
    ) -> list[EvidenceRef]:
        """Screenshot (and optionally HTML and visible text) of the current page."""
        self._seq += 1
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        refs: list[EvidenceRef] = []
        shot, rel = self._path(label, ".png")
        try:
            await driver.screenshot(shot)
            refs.append(self._ref(EvidenceKind.SCREENSHOT, shot, rel, f"{description} (screenshot)"))
        except Exception:  # evidence must never break the observed action
            pass
        if html:
            page, rel = self._path(label, ".html")
            try:
                page.write_text(await driver.html(), encoding="utf-8")
                refs.append(self._ref(EvidenceKind.HTML_SNAPSHOT, page, rel, f"{description} (HTML)"))
            except Exception:
                pass
        if text is not None:
            txt, rel = self._path(label, ".txt")
            txt.write_text(text, encoding="utf-8")
            refs.append(self._ref(EvidenceKind.PAGE_TEXT, txt, rel, f"{description} (visible text)"))
        return refs
