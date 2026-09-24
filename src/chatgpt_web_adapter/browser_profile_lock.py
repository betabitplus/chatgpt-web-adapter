from __future__ import annotations

from pathlib import Path

from .file_lock import InterProcessFileLock


class BrowserProfileLock:
    """Cross-process exclusive lock for one Chromium user-data directory."""

    def __init__(self, profile_dir: str | Path, *, timeout: float = 30.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        profile = Path(profile_dir)
        self.path = profile.parent / f".{profile.name}.lock"
        self.timeout = float(timeout)
        self._lock = InterProcessFileLock(
            self.path,
            timeout=self.timeout,
            poll_interval=0.1,
            timeout_message=f"Browser profile is busy: {self.path.parent}",
        )

    def acquire(self) -> None:
        self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> "BrowserProfileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
