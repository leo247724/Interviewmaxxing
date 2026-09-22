"""Local file conventions: profile, state database, artifacts and browser profile.

All personal data lives under ``IMX_HOME`` (default ``~/.interviewmaxxing``), outside
source control. Each path can be overridden individually:

=====================  ==============================  ==============================
Variable               Default                          Contents
=====================  ==============================  ==============================
``IMX_HOME``           ``~/.interviewmaxxing``          Root of everything below
``IMX_PROFILE_DIR``    ``$IMX_HOME/profile``            Candidate profile and resume
``IMX_STATE_DB``       ``$IMX_HOME/state/imx.sqlite3``  Requests, applications, events
``IMX_ARTIFACTS_DIR``  ``$IMX_HOME/artifacts``          Evidence, one dir per app id
``IMX_BROWSER_DIR``    ``$IMX_HOME/browser``            Persistent browser profile
``IMX_CANDIDATE_ID``   ``default``                      Candidate used by ``apply``
=====================  ==============================  ==============================

For development inside a worktree use ``IMX_HOME=$PWD/.imx`` (git-ignored).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOME = Path("~/.interviewmaxxing")
DEFAULT_CANDIDATE_ID = "default"


@dataclass(frozen=True, slots=True)
class LocalPaths:
    home: Path
    profile_dir: Path
    state_db: Path
    artifacts_dir: Path
    browser_dir: Path
    candidate_id: str = DEFAULT_CANDIDATE_ID

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, home: Path | None = None
    ) -> LocalPaths:
        """Resolve paths from the environment; ``home`` overrides ``IMX_HOME``."""
        env = os.environ if env is None else env

        def pick(name: str, default: Path) -> Path:
            value = env.get(name)
            return Path(value).expanduser() if value else default

        root = (home or pick("IMX_HOME", DEFAULT_HOME)).expanduser()
        return cls(
            home=root,
            profile_dir=pick("IMX_PROFILE_DIR", root / "profile"),
            state_db=pick("IMX_STATE_DB", root / "state" / "imx.sqlite3"),
            artifacts_dir=pick("IMX_ARTIFACTS_DIR", root / "artifacts"),
            browser_dir=pick("IMX_BROWSER_DIR", root / "browser"),
            candidate_id=env.get("IMX_CANDIDATE_ID") or DEFAULT_CANDIDATE_ID,
        )

    def application_artifacts(self, application_id: str) -> Path:
        """Directory for one application's evidence. ``EvidenceRef.path`` values are
        relative to ``artifacts_dir`` and therefore start with ``<application_id>/``."""
        return self.artifacts_dir / application_id

    def ensure(self) -> None:
        """Create the home, state, artifacts and browser directories (owner-only access)."""
        for directory in (self.home, self.state_db.parent, self.artifacts_dir, self.browser_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
