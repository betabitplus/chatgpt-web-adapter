from __future__ import annotations

import errno
import os
import time
from pathlib import Path
from typing import BinaryIO

_LOCK_CONTENTION_ERRNOS = {
    errno.EACCES,
    errno.EAGAIN,
    errno.EDEADLK,
}


class InterProcessFileLock:
    """Kernel-backed exclusive lock anchored by a stable sidecar file."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 30.0,
        poll_interval: float = 0.05,
        timeout_message: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.path = Path(path)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self.timeout_message = timeout_message or f"Lock is busy: {self.path}"
        self._stream: BinaryIO | None = None

    def acquire(self) -> None:
        if self._stream is not None:
            raise RuntimeError(f"Lock is already held: {self.path}")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name != "nt":
                os.chmod(self.path, 0o600)

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()

            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    self._lock_stream(stream)
                    self._stream = stream
                    return
                except OSError as error:
                    if not self._is_contention(error):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(self.timeout_message) from error
                    time.sleep(self.poll_interval)
        except BaseException:
            stream.close()
            raise

    @staticmethod
    def _is_contention(error: OSError) -> bool:
        return isinstance(error, BlockingIOError) or error.errno in _LOCK_CONTENTION_ERRNOS

    @staticmethod
    def _lock_stream(stream: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_stream(stream: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            self._unlock_stream(stream)
        finally:
            stream.close()
            self._stream = None

    def __enter__(self) -> "InterProcessFileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
