"""Process lifetime ownership of recovery and workers sharing a service database."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class ServiceAlreadyRunning(RuntimeError):
    """Another service owns this home's task database."""


class ServiceOwnership:
    """An OS lock, released on close or process death; the lock file stays put."""

    def __init__(self, path: Path) -> None:
        self._fd: int | None = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(self._fd, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.close()
            raise ServiceAlreadyRunning(
                "Another service is already running for this IMX_HOME. Stop it before restarting."
            ) from exc
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
