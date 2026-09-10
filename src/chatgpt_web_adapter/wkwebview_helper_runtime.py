from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from .exceptions import RequestError

_HELPER_DIRNAME = "wkwebview-authority"
_RESULT_PREFIX = "WK_RESULT "
_EVENT_PREFIX = "WK_EVENT "


@dataclass
class WKHelperInvocation:
    """Private request envelope for one native WK helper invocation."""

    command: list[str]
    request: dict[str, Any]
    capture_resume: bool = False


class WKWebViewHelperRuntime:
    """Build and execute the short-lived native WKWebView authority helper."""

    def __init__(
        self,
        state_dir: str | Path,
        *,
        build_timeout: float,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.build_timeout = float(build_timeout)
        self._build_lock = threading.Lock()

    @property
    def helper_root(self) -> Path:
        return self.state_dir / _HELPER_DIRNAME

    @property
    def helper_app(self) -> Path:
        return self.helper_root / "WKChatGPTAuthority.app"

    @property
    def helper_binary(self) -> Path:
        return self.helper_app / "Contents" / "MacOS" / "WKChatGPTAuthority"

    @staticmethod
    def source_paths() -> tuple[Path, Path, Path]:
        package_root = resources.files("chatgpt_web_adapter")
        helper_root = package_root.joinpath("wkwebview_helper")
        source = Path(str(helper_root.joinpath("WKChatGPTAuthority.m")))
        plist = Path(str(helper_root.joinpath("Info.plist")))
        minimal_shell = Path(str(helper_root.joinpath("minimal_security_shell.js")))
        return source, plist, minimal_shell

    @staticmethod
    def source_digest(source: Path, plist: Path, minimal_shell: Path) -> str:
        digest = hashlib.sha256()
        digest.update(source.read_bytes())
        digest.update(plist.read_bytes())
        digest.update(minimal_shell.read_bytes())
        return digest.hexdigest()

    def ensure_helper(self) -> Path:
        if sys.platform != "darwin":
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_UNAVAILABLE: macOS is required",
                request_stage="wkwebview_authority_build",
            )
        source, plist, minimal_shell = self.source_paths()
        if not source.is_file() or not plist.is_file() or not minimal_shell.is_file():
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_SOURCE_MISSING",
                request_stage="wkwebview_authority_build",
            )
        if shutil.which("clang") is None or shutil.which("codesign") is None:
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_TOOLCHAIN_MISSING: clang/codesign required",
                request_stage="wkwebview_authority_build",
            )

        expected = self.source_digest(source, plist, minimal_shell)
        stamp = self.helper_root / "source.sha256"
        with self._build_lock:
            self.helper_root.mkdir(parents=True, exist_ok=True)
            lock_path = self.helper_root / ".build.lock"
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                if self.helper_binary.is_file() and stamp.is_file():
                    try:
                        if stamp.read_text(encoding="utf-8").strip() == expected:
                            return self.helper_binary
                    except OSError:
                        pass

                macos_dir = self.helper_app / "Contents" / "MacOS"
                resources_dir = self.helper_app / "Contents" / "Resources"
                macos_dir.mkdir(parents=True, exist_ok=True)
                resources_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(plist, self.helper_app / "Contents" / "Info.plist")
                shutil.copy2(minimal_shell, resources_dir / "minimal_security_shell.js")
                self.run_build(
                    [
                        "clang",
                        "-fobjc-arc",
                        "-framework",
                        "Cocoa",
                        "-framework",
                        "WebKit",
                        str(source),
                        "-o",
                        str(self.helper_binary),
                    ]
                )
                self.run_build(
                    [
                        "codesign",
                        "--force",
                        "--deep",
                        "--sign",
                        "-",
                        str(self.helper_app),
                    ]
                )
                stamp.write_text(expected + "\n", encoding="utf-8")
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        return self.helper_binary

    def run_build(self, command: list[str]) -> None:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.build_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_BUILD_FAILED: {error}",
                request_stage="wkwebview_authority_build",
            ) from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "build failed").strip()
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_BUILD_FAILED: {detail[-1500:]}",
                request_stage="wkwebview_authority_build",
            )

    @staticmethod
    def subprocess_input(
        invocation: list[str] | WKHelperInvocation,
    ) -> tuple[list[str], str | None]:
        if isinstance(invocation, WKHelperInvocation):
            command = [*invocation.command, "--request-stdin"]
            request_input = json.dumps(
                invocation.request,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return command, request_input
        return list(invocation), None

    def run(
        self,
        invocation: list[str] | WKHelperInvocation,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        env = os.environ.copy()
        env.setdefault("NSUnbufferedIO", "YES")
        command, request_input = self.subprocess_input(invocation)
        try:
            completed = subprocess.run(
                command,
                input=request_input,
                capture_output=True,
                text=True,
                timeout=max(1.0, timeout + 5.0),
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_TIMEOUT",
                request_stage="wkwebview_authority_turn",
            ) from error
        except OSError as error:
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_LAUNCH_FAILED: {error}",
                request_stage="wkwebview_authority_turn",
            ) from error

        payload = None
        for line in reversed(completed.stdout.splitlines()):
            if not line.startswith(_RESULT_PREFIX):
                continue
            try:
                candidate = json.loads(line[len(_RESULT_PREFIX) :])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
        if payload is None:
            detail = (completed.stderr or completed.stdout or "no helper result").strip()
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_NO_RESULT: {detail[-2000:]}",
                request_stage="wkwebview_authority_turn",
            )
        if payload.get("ok") is not True:
            raise RequestError(
                str(payload.get("error") or "WKWEBVIEW_AUTHORITY_TURN_FAILED"),
                request_stage="wkwebview_authority_turn",
            )
        return payload

    def run_streaming(
        self,
        invocation: list[str] | WKHelperInvocation,
        *,
        timeout: float,
        on_text_event: Any,
        on_lifecycle_event: Any = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not callable(on_text_event):
            raise TypeError("on_text_event must be callable")
        if on_lifecycle_event is not None and not callable(on_lifecycle_event):
            raise TypeError("on_lifecycle_event must be callable")
        env = os.environ.copy()
        env.setdefault("NSUnbufferedIO", "YES")
        if extra_env:
            env.update(extra_env)
        command, request_input = self.subprocess_input(invocation)
        resume_read_fd: int | None = None
        resume_write_fd: int | None = None
        pass_fds: tuple[int, ...] = ()
        if isinstance(invocation, WKHelperInvocation) and invocation.capture_resume:
            resume_read_fd, resume_write_fd = os.pipe()
            os.set_inheritable(resume_write_fd, True)
            command += ["--resume-handoff-fd", str(resume_write_fd)]
            pass_fds = (resume_write_fd,)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if request_input is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
                pass_fds=pass_fds,
            )
        except OSError as error:
            if resume_read_fd is not None:
                os.close(resume_read_fd)
            if resume_write_fd is not None:
                os.close(resume_write_fd)
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_LAUNCH_FAILED: {error}",
                request_stage="wkwebview_authority_turn",
            ) from error
        if resume_write_fd is not None:
            os.close(resume_write_fd)
        if request_input is not None:
            assert process.stdin is not None
            try:
                process.stdin.write(request_input)
                process.stdin.close()
            except OSError:
                pass

        deadline = time.monotonic() + max(1.0, timeout + 5.0)
        payload: dict[str, Any] | None = None
        try:
            assert process.stdout is not None
            while time.monotonic() < deadline:
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    time.sleep(0.02)
                    continue
                if line.startswith(_EVENT_PREFIX):
                    try:
                        event = json.loads(line[len(_EVENT_PREFIX) :])
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    event_type = event.get("type")
                    if event_type in {
                        "assistant_text_snapshot",
                        "assistant_text_delta",
                        "assistant_text_revision",
                    }:
                        try:
                            on_text_event(event)
                        except Exception:
                            pass
                        continue
                    if event_type == "write_identity_resolved" and on_lifecycle_event is not None:
                        try:
                            on_lifecycle_event(event)
                        except Exception:
                            pass
                    continue
                if not line.startswith(_RESULT_PREFIX):
                    continue
                try:
                    candidate = json.loads(line[len(_RESULT_PREFIX) :])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    payload = candidate
                    break
        finally:
            if process.poll() is None:
                self.terminate_process(process)

        resume_value = ""
        if resume_read_fd is not None:
            try:
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(resume_read_fd, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                resume_value = b"".join(chunks).decode("utf-8").strip()
            except (OSError, UnicodeDecodeError):
                resume_value = ""
            finally:
                os.close(resume_read_fd)
        if payload is not None and resume_value:
            payload["stream_resume_value"] = resume_value

        if payload is None:
            detail = "no helper result"
            if process.stderr is not None:
                try:
                    stderr = process.stderr.read().strip()
                except Exception:
                    stderr = ""
                if stderr:
                    detail = stderr
            if time.monotonic() >= deadline:
                detail = "streaming helper timed out"
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_NO_RESULT: {detail[-2000:]}",
                request_stage="wkwebview_authority_turn",
            )
        if payload.get("ok") is not True:
            raise RequestError(
                str(payload.get("error") or "WKWEBVIEW_AUTHORITY_TURN_FAILED"),
                request_stage="wkwebview_authority_turn",
            )
        return payload

    @staticmethod
    def terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)
