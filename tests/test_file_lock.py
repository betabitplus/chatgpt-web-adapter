from __future__ import annotations

import os
import stat
import subprocess
import sys
import time

import pytest

from chatgpt_web_adapter.file_lock import InterProcessFileLock


def test_interprocess_file_lock_rejects_reentrant_acquire(tmp_path) -> None:
    lock = InterProcessFileLock(tmp_path / "state.lock", timeout=0.1)
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="already held"):
            lock.acquire()
    finally:
        lock.release()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable to Windows")
def test_interprocess_file_lock_file_is_owner_only(tmp_path) -> None:
    path = tmp_path / "state.lock"
    path.write_bytes(b"0")
    path.chmod(0o666)

    with InterProcessFileLock(path, timeout=0.1):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_interprocess_file_lock_is_released_when_owner_process_dies(tmp_path) -> None:
    path = tmp_path / "state.lock"
    ready = tmp_path / "ready"
    code = (
        "import time;"
        "from pathlib import Path;"
        "from chatgpt_web_adapter.file_lock import InterProcessFileLock;"
        f"lock=InterProcessFileLock({str(path)!r}, timeout=1);"
        "lock.acquire();"
        f"Path({str(ready)!r}).write_text('ready');"
        "time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "child process did not acquire the lock"
        process.kill()
        process.wait(timeout=5)

        with InterProcessFileLock(path, timeout=1):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
