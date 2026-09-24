from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import chatgpt_web_adapter.auth_store as auth_store
from chatgpt_web_adapter.auth_store import persist_auth_data
from chatgpt_web_adapter.exceptions import AuthError
from chatgpt_web_adapter.file_lock import InterProcessFileLock
from chatgpt_web_adapter.types import AuthData


def test_persist_auth_data_keeps_structured_browser_cookies(tmp_path) -> None:
    path = tmp_path / "auth.json"
    auth = AuthData(
        accessToken="not.a.jwt",
        cookies={"session.0": "chunk"},
        browserCookies=[
            {
                "name": "session.0",
                "value": "chunk",
                "domain": ".chatgpt.com",
                "path": "/",
                "secure": True,
            }
        ],
    )

    persist_auth_data(auth, path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    loaded = AuthData.from_json(path)

    assert saved["browserCookies"][0]["domain"] == ".chatgpt.com"
    assert loaded.browserCookies == saved["browserCookies"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable to Windows")
def test_persist_auth_data_repairs_unsafe_existing_mode_and_preserves_unknown_fields(
    tmp_path,
) -> None:
    path = tmp_path / "auth.json"
    path.write_text('{"customMarker":"keep-me"}\n', encoding="utf-8")
    path.chmod(0o644)

    persist_auth_data(AuthData(cookies={"session.0": "chunk"}), path)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["customMarker"] == "keep-me"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(auth_store._auth_lock_path(path).stat().st_mode) == 0o600


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    root = Path(__file__).resolve().parents[1]
    src = root / "src"
    previous = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(src) if not previous else str(src) + os.pathsep + previous
    )
    return env


def test_persist_auth_data_waits_for_cross_process_lock(tmp_path) -> None:
    path = tmp_path / "auth.json"
    ready = tmp_path / "child-ready"
    lock = InterProcessFileLock(auth_store._auth_lock_path(path), timeout=1)
    code = (
        "from pathlib import Path;"
        "from chatgpt_web_adapter.auth_store import persist_auth_data;"
        "from chatgpt_web_adapter.types import AuthData;"
        f"Path({str(ready)!r}).write_text('ready');"
        f"persist_auth_data(AuthData(accessToken='not.a.jwt'), {str(path)!r})"
    )

    lock.acquire()
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        env=_subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "child process did not reach persist_auth_data"
        time.sleep(0.1)
        assert process.poll() is None
        assert not path.exists()
    finally:
        lock.release()

    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, (stdout, stderr)
    assert json.loads(path.read_text(encoding="utf-8"))["accessToken"] == "not.a.jwt"


def test_persist_auth_data_reports_lock_timeout_as_auth_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    path = tmp_path / "auth.json"
    lock = InterProcessFileLock(auth_store._auth_lock_path(path), timeout=1)
    monkeypatch.setattr(auth_store, "_AUTH_LOCK_TIMEOUT_SECONDS", 0.05)

    lock.acquire()
    try:
        with pytest.raises(AuthError, match="Authorization store is busy"):
            persist_auth_data(AuthData(accessToken="not.a.jwt"), path)
    finally:
        lock.release()


def test_concurrent_auth_writers_preserve_independent_updates(tmp_path) -> None:
    path = tmp_path / "auth.json"
    start = tmp_path / "start"
    code_token = f"""
import time
from pathlib import Path
from chatgpt_web_adapter.auth_store import persist_auth_data
from chatgpt_web_adapter.types import AuthData
start = Path({str(start)!r})
while not start.exists():
    time.sleep(0.01)
persist_auth_data(AuthData(accessToken="not.a.jwt"), {str(path)!r})
"""
    code_session = f"""
import time
from pathlib import Path
from chatgpt_web_adapter.auth_store import persist_auth_data
from chatgpt_web_adapter.types import AuthData
start = Path({str(start)!r})
while not start.exists():
    time.sleep(0.01)
persist_auth_data(AuthData(), {str(path)!r}, session_token="session-marker")
"""

    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code],
            env=_subprocess_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for code in (code_token, code_session)
    ]
    start.write_text("go", encoding="utf-8")

    results = [process.communicate(timeout=10) for process in processes]
    assert [process.returncode for process in processes] == [0, 0], results

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["accessToken"] == "not.a.jwt"
    assert saved["sessionToken"] == "session-marker"

def test_same_process_auth_writers_serialize_per_file(tmp_path) -> None:
    path = tmp_path / "auth.json"
    start = threading.Barrier(3)
    errors: list[BaseException] = []

    def write_token() -> None:
        try:
            start.wait(timeout=2)
            persist_auth_data(AuthData(accessToken="not.a.jwt"), path)
        except BaseException as error:
            errors.append(error)

    def write_session() -> None:
        try:
            start.wait(timeout=2)
            persist_auth_data(AuthData(), path, session_token="session-marker")
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=write_token),
        threading.Thread(target=write_session),
    ]
    for thread in threads:
        thread.start()
    start.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert not errors
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["accessToken"] == "not.a.jwt"
    assert saved["sessionToken"] == "session-marker"


def test_different_auth_files_are_not_process_globally_serialized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    paths = [tmp_path / "one.json", tmp_path / "two.json"]
    rendezvous = threading.Barrier(2)
    errors: list[BaseException] = []
    original = auth_store._atomic_write_json

    def synchronized_write(path: Path, payload: dict) -> None:
        rendezvous.wait(timeout=2)
        original(path, payload)

    monkeypatch.setattr(auth_store, "_atomic_write_json", synchronized_write)

    def writer(path: Path) -> None:
        try:
            persist_auth_data(AuthData(accessToken="not.a.jwt"), path)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(path,)) for path in paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert not errors
    assert all(path.is_file() for path in paths)
