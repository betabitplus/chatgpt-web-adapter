from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Callable, TypeVar

from .exceptions import RequestError
from .wkwebview_turn_broker import WKSystemTurnBrokerClient

_HELPER_DIRNAME = "wkwebview-authority"
_RESULT_PREFIX = "WK_RESULT "
_EVENT_PREFIX = "WK_EVENT "

_ObserverResult = TypeVar("_ObserverResult")
_MINIMUM_MACOS = (12, 0)


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

    @staticmethod
    def macos_version() -> tuple[int, int] | None:
        raw = platform.mac_ver()[0]
        try:
            parts = tuple(int(part) for part in raw.split(".")[:2])
        except ValueError:
            return None
        if len(parts) < 2:
            return None
        return parts[0], parts[1]

    def ensure_helper(self) -> Path:
        if sys.platform != "darwin":
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_UNAVAILABLE: macOS is required",
                request_stage="wkwebview_authority_build",
            )
        macos_version = self.macos_version()
        if macos_version is None or macos_version < _MINIMUM_MACOS:
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_UNAVAILABLE: macOS 12+ is required",
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
            detail = (
                completed.stderr or completed.stdout or "no helper result"
            ).strip()
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_NO_RESULT: {detail[-2000:]}",
                request_stage="wkwebview_authority_turn",
            )
        if payload.get("ok") is not True:
            error_message = str(
                payload.get("error") or "WKWEBVIEW_AUTHORITY_TURN_FAILED"
            )
            detail = payload.get("detail")
            stage = payload.get("stage")
            if isinstance(stage, str) and stage.strip():
                error_message = f"{error_message} stage={stage.strip()}"
            raise RequestError(
                error_message,
                status_code=payload.get("status"),
                body_preview=detail,
                request_stage="wkwebview_authority_turn",
            )
        return payload

    def acquire_temporary_lifecycle(
        self,
        lifecycle_id: str,
        *,
        timeout: float = 10.0,
    ) -> Any:
        value = os.environ.get("CWA_WK_DISABLE_TURN_BROKER", "").strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return None
        helper = self.ensure_helper()
        client = WKSystemTurnBrokerClient(helper)
        return client.acquire_temporary_lifecycle(lifecycle_id, timeout=timeout)

    @staticmethod
    def _minimal_turn_broker_enabled(
        invocation: list[str] | WKHelperInvocation,
    ) -> bool:
        if not isinstance(invocation, WKHelperInvocation):
            return False
        if "--minimal-security-shell" not in invocation.command:
            return False
        value = os.environ.get("CWA_WK_DISABLE_TURN_BROKER", "").strip().lower()
        return value not in {"1", "true", "yes", "on"}

    @staticmethod
    def _external_completion_payload(
        invocation: WKHelperInvocation,
        completion_signal: bool | dict[str, Any],
        *,
        submit_temporary_mode_observed: bool = False,
    ) -> dict[str, Any] | None:
        conversation_id = invocation.request.get("minimal_conversation_id")
        normalized_conversation_id = (
            conversation_id.strip()
            if isinstance(conversation_id, str) and conversation_id.strip()
            else None
        )
        if (
            isinstance(completion_signal, dict)
            and completion_signal.get("kind") == "early_handoff"
        ):
            signal_conversation_id = completion_signal.get("conversation_id")
            topic_id = completion_signal.get("topic_id")
            turn_exchange_id = completion_signal.get("turn_exchange_id")
            signal_conversation_id = (
                signal_conversation_id.strip()
                if isinstance(signal_conversation_id, str)
                and signal_conversation_id.strip()
                else None
            )
            temporary_fresh_handoff = (
                normalized_conversation_id is None
                and invocation.request.get("minimal_temporary") is True
                and submit_temporary_mode_observed
            )
            conversation_matches = (
                normalized_conversation_id is not None
                and signal_conversation_id == normalized_conversation_id
            )
            if (
                signal_conversation_id is not None
                and (conversation_matches or temporary_fresh_handoff)
                and isinstance(topic_id, str)
                and topic_id.strip()
                and isinstance(turn_exchange_id, str)
                and turn_exchange_id.strip()
            ):
                resolved_conversation_id = (
                    normalized_conversation_id or signal_conversation_id
                )
                minimal_attachments = invocation.request.get("minimal_attachments")
                attachment_count = (
                    len(minimal_attachments)
                    if isinstance(minimal_attachments, list)
                    else invocation.request.get("minimal_attachment_count", 0)
                )
                if not isinstance(attachment_count, int) or isinstance(
                    attachment_count, bool
                ):
                    attachment_count = 0
                payload: dict[str, Any] = {
                    "ok": True,
                    "identity_recovery_required": False,
                    "client_message_id": "",
                    "conversation_id": resolved_conversation_id,
                    "response_status": 200,
                    "submit_response_status": 200,
                    "submit_temporary_mode_observed": submit_temporary_mode_observed,
                    "stream_response_status": 0,
                    "write_commit_proven": True,
                    "canonical_committed": False,
                    "stream_terminal_observed": False,
                    "stream_resume_present": False,
                    "stream_resume_handoff_written": False,
                    "stream_handoff_observed": True,
                    "stream_topic_id": topic_id.strip(),
                    "turn_exchange_id": turn_exchange_id.strip(),
                    "stream_conversation_id": resolved_conversation_id,
                    "attachment_count": attachment_count,
                    "minimal_security_shell": True,
                    "_cwa_early_handoff_observed": True,
                }
                server_request_id = completion_signal.get("server_request_id")
                if isinstance(server_request_id, str) and server_request_id.strip():
                    payload["_cwa_server_request_id"] = server_request_id.strip()
                return payload
        elif (
            bool(completion_signal)
            and isinstance(conversation_id, str)
            and conversation_id.strip()
        ):
            return {
                "ok": True,
                "identity_recovery_required": True,
                "client_message_id": "",
                "conversation_id": conversation_id.strip(),
                "response_status": 0,
                "submit_response_status": 0,
                "stream_response_status": 0,
                "write_commit_proven": False,
                "canonical_committed": False,
                "stream_terminal_observed": False,
                "stream_resume_present": False,
                "stream_resume_handoff_written": False,
                "stream_handoff_observed": False,
                "stream_topic_id": "",
                "turn_exchange_id": "",
                "minimal_security_shell": True,
                "_cwa_external_completion_observed": True,
            }
        return None

    def _run_streaming_via_turn_broker(
        self,
        invocation: WKHelperInvocation,
        *,
        timeout: float,
        on_text_event: Any,
        on_lifecycle_event: Any,
        on_transport_event: Any,
        on_submit_started: Callable[[], None] | None,
        external_completion_check: Callable[[], bool | dict[str, Any]] | None,
    ) -> dict[str, Any]:
        client = WKSystemTurnBrokerClient(self.helper_binary)
        connection = client.start_turn(dict(invocation.request), timeout=timeout)
        deadline = time.monotonic() + max(1.0, timeout + 5.0)
        submit_request_seen = False
        submit_temporary_mode_observed = False
        payload: dict[str, Any] | None = None
        try:
            while time.monotonic() < deadline:
                envelope = connection.recv_envelope(0.05)
                if isinstance(envelope, dict):
                    kind = envelope.get("type")
                    if kind == "error":
                        raise RequestError(
                            str(
                                envelope.get("error")
                                or "WKWEBVIEW_TURN_BROKER_FAILED"
                            ),
                            request_stage="wkwebview_authority_turn",
                        )
                    if kind == "result":
                        candidate = envelope.get("result")
                        if isinstance(candidate, dict):
                            payload = candidate
                            break
                    elif kind == "event":
                        event = envelope.get("event")
                        if not isinstance(event, dict):
                            continue
                        event_type = event.get("type")
                        if (
                            event_type == "submit_request_observed"
                            and on_transport_event is not None
                        ):
                            try:
                                on_transport_event(event)
                            except Exception:
                                pass
                        if event_type == "submit_request_observed":
                            submit_request_seen = True
                            submit_temporary_mode_observed = (
                                event.get("temporary_mode") is True
                            )
                            if on_submit_started is not None:
                                try:
                                    on_submit_started()
                                except Exception:
                                    pass
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
                        if (
                            event_type in {"raw_ws_event", "proxy_fetch_state"}
                            and on_transport_event is not None
                        ):
                            try:
                                on_transport_event(event)
                            except Exception:
                                pass
                            continue
                        if (
                            event_type == "write_identity_resolved"
                            and on_lifecycle_event is not None
                        ):
                            try:
                                on_lifecycle_event(event)
                            except Exception:
                                pass

                if submit_request_seen and external_completion_check is not None:
                    completion_signal: bool | dict[str, Any] = False
                    try:
                        completion_signal = external_completion_check()
                    except Exception:
                        completion_signal = False
                    candidate = self._external_completion_payload(
                        invocation,
                        completion_signal,
                        submit_temporary_mode_observed=(
                            submit_temporary_mode_observed
                        ),
                    )
                    if candidate is not None:
                        payload = candidate
                        break
        finally:
            connection.close()

        if payload is None:
            raise RequestError(
                "WKWEBVIEW_TURN_BROKER_NO_RESULT",
                request_stage="wkwebview_authority_turn",
            )
        if payload.get("ok") is not True:
            error_message = str(
                payload.get("error") or "WKWEBVIEW_AUTHORITY_TURN_FAILED"
            )
            detail = payload.get("detail")
            stage = payload.get("stage")
            if isinstance(stage, str) and stage.strip():
                error_message = f"{error_message} stage={stage.strip()}"
            raise RequestError(
                error_message,
                status_code=payload.get("status"),
                body_preview=detail,
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
        on_transport_event: Any = None,
        on_submit_started: Callable[[], None] | None = None,
        external_completion_check: Callable[[], bool | dict[str, Any]] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not callable(on_text_event):
            raise TypeError("on_text_event must be callable")
        if on_lifecycle_event is not None and not callable(on_lifecycle_event):
            raise TypeError("on_lifecycle_event must be callable")
        if on_transport_event is not None and not callable(on_transport_event):
            raise TypeError("on_transport_event must be callable")
        if on_submit_started is not None and not callable(on_submit_started):
            raise TypeError("on_submit_started must be callable")
        if external_completion_check is not None and not callable(
            external_completion_check
        ):
            raise TypeError("external_completion_check must be callable")
        if self._minimal_turn_broker_enabled(invocation):
            assert isinstance(invocation, WKHelperInvocation)
            return self._run_streaming_via_turn_broker(
                invocation,
                timeout=timeout,
                on_text_event=on_text_event,
                on_lifecycle_event=on_lifecycle_event,
                on_transport_event=on_transport_event,
                on_submit_started=on_submit_started,
                external_completion_check=external_completion_check,
            )
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
        submit_request_seen = False
        stdout_lines: queue.Queue[str | None] = queue.Queue()

        def read_stdout_lines() -> None:
            assert process.stdout is not None
            try:
                for stdout_line in process.stdout:
                    stdout_lines.put(stdout_line)
            finally:
                stdout_lines.put(None)

        stdout_reader = threading.Thread(
            target=read_stdout_lines,
            name="cwa-wk-helper-stdout",
            daemon=True,
        )
        stdout_reader.start()
        try:
            while time.monotonic() < deadline:
                try:
                    line = stdout_lines.get(timeout=0.05)
                except queue.Empty:
                    line = None
                    if process.poll() is not None:
                        break
                if line is not None:
                    if line.startswith(_EVENT_PREFIX):
                        try:
                            event = json.loads(line[len(_EVENT_PREFIX) :])
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        event_type = event.get("type")
                        if (
                            event_type == "submit_request_observed"
                            and on_transport_event is not None
                        ):
                            try:
                                on_transport_event(event)
                            except Exception:
                                pass
                        if event_type == "submit_request_observed":
                            submit_request_seen = True
                            if on_submit_started is not None:
                                try:
                                    on_submit_started()
                                except Exception:
                                    pass
                        if event_type in {
                            "assistant_text_snapshot",
                            "assistant_text_delta",
                            "assistant_text_revision",
                        }:
                            try:
                                on_text_event(event)
                            except Exception:
                                # Consumer callbacks are observational; they must not
                                # abort or corrupt the browser-owned transport lifecycle.
                                pass
                            continue
                        if (
                            event_type in {"raw_ws_event", "proxy_fetch_state"}
                            and on_transport_event is not None
                        ):
                            try:
                                on_transport_event(event)
                            except Exception:
                                # Raw transport callbacks are observational and must not
                                # interfere with the browser-owned write lifecycle.
                                pass
                            continue
                        if (
                            event_type == "write_identity_resolved"
                            and on_lifecycle_event is not None
                        ):
                            try:
                                on_lifecycle_event(event)
                            except Exception:
                                # Lifecycle callbacks are also observational; transport
                                # completion/finality must not depend on consumer code.
                                pass
                        continue
                    if line.startswith(_RESULT_PREFIX):
                        try:
                            candidate = json.loads(line[len(_RESULT_PREFIX) :])
                        except json.JSONDecodeError:
                            candidate = None
                        if isinstance(candidate, dict):
                            payload = candidate
                            break

                if (
                    submit_request_seen
                    and external_completion_check is not None
                    and isinstance(invocation, WKHelperInvocation)
                ):
                    completion_signal: bool | dict[str, Any] = False
                    try:
                        completion_signal = external_completion_check()
                    except Exception:
                        completion_signal = False
                    conversation_id = invocation.request.get(
                        "minimal_conversation_id"
                    )
                    if (
                        isinstance(completion_signal, dict)
                        and completion_signal.get("kind") == "browser_stream_complete"
                    ):
                        signal_conversation_id = completion_signal.get(
                            "conversation_id"
                        )
                        assistant_message_id = completion_signal.get(
                            "assistant_message_id"
                        )
                        if (
                            "--minimal-security-shell" not in invocation.command
                            and isinstance(signal_conversation_id, str)
                            and signal_conversation_id.strip()
                            and isinstance(assistant_message_id, str)
                            and assistant_message_id.strip()
                            and (
                                not isinstance(conversation_id, str)
                                or not conversation_id.strip()
                                or signal_conversation_id == conversation_id.strip()
                            )
                        ):
                            payload = {
                                "ok": True,
                                "identity_recovery_required": False,
                                "client_message_id": "",
                                "assistant_message_id": assistant_message_id.strip(),
                                "conversation_id": signal_conversation_id.strip(),
                                "response_status": 200,
                                "submit_response_status": 0,
                                "stream_response_status": 200,
                                "write_commit_proven": True,
                                "write_commit_proof": "BROWSER_STREAM_COMPLETE",
                                "canonical_committed": False,
                                "stream_terminal_observed": True,
                                "stream_resume_present": False,
                                "stream_resume_handoff_written": False,
                                "stream_handoff_observed": False,
                                "stream_topic_id": "",
                                "turn_exchange_id": "",
                                "stream_conversation_id": signal_conversation_id.strip(),
                                "minimal_security_shell": False,
                                "_cwa_browser_stream_complete_observed": True,
                            }
                            break
                    elif (
                        isinstance(completion_signal, dict)
                        and completion_signal.get("kind") == "early_handoff"
                    ):
                        signal_conversation_id = completion_signal.get(
                            "conversation_id"
                        )
                        topic_id = completion_signal.get("topic_id")
                        turn_exchange_id = completion_signal.get(
                            "turn_exchange_id"
                        )
                        if (
                            isinstance(conversation_id, str)
                            and conversation_id.strip()
                            and signal_conversation_id == conversation_id.strip()
                            and isinstance(topic_id, str)
                            and topic_id.strip()
                            and isinstance(turn_exchange_id, str)
                            and turn_exchange_id.strip()
                        ):
                            minimal_attachments = invocation.request.get(
                                "minimal_attachments"
                            )
                            attachment_count = (
                                len(minimal_attachments)
                                if isinstance(minimal_attachments, list)
                                else invocation.request.get(
                                    "minimal_attachment_count", 0
                                )
                            )
                            if not isinstance(attachment_count, int) or isinstance(
                                attachment_count, bool
                            ):
                                attachment_count = 0
                            payload = {
                                "ok": True,
                                "identity_recovery_required": False,
                                "client_message_id": "",
                                "conversation_id": conversation_id.strip(),
                                "response_status": 200,
                                "submit_response_status": 200,
                                "stream_response_status": 0,
                                "write_commit_proven": True,
                                "canonical_committed": False,
                                "stream_terminal_observed": False,
                                "stream_resume_present": False,
                                "stream_resume_handoff_written": False,
                                "stream_handoff_observed": True,
                                "stream_topic_id": topic_id.strip(),
                                "turn_exchange_id": turn_exchange_id.strip(),
                                "stream_conversation_id": conversation_id.strip(),
                                "attachment_count": attachment_count,
                                "minimal_security_shell": True,
                                "_cwa_early_handoff_observed": True,
                            }
                            server_request_id = completion_signal.get(
                                "server_request_id"
                            )
                            if (
                                isinstance(server_request_id, str)
                                and server_request_id.strip()
                            ):
                                payload["_cwa_server_request_id"] = (
                                    server_request_id.strip()
                                )
                            break
                    elif (
                        bool(completion_signal)
                        and isinstance(conversation_id, str)
                        and conversation_id.strip()
                    ):
                        payload = {
                            "ok": True,
                            "identity_recovery_required": True,
                            "client_message_id": "",
                            "conversation_id": conversation_id.strip(),
                            "response_status": 0,
                            "submit_response_status": 0,
                            "stream_response_status": 0,
                            "write_commit_proven": False,
                            "canonical_committed": False,
                            "stream_terminal_observed": False,
                            "stream_resume_present": False,
                            "stream_resume_handoff_written": False,
                            "stream_handoff_observed": False,
                            "stream_topic_id": "",
                            "turn_exchange_id": "",
                            "minimal_security_shell": True,
                            "_cwa_external_completion_observed": True,
                        }
                        break
        finally:
            if process.poll() is None:
                self.terminate_process(process)
            stdout_reader.join(timeout=0.5)

        private_handoff = ""
        if resume_read_fd is not None:
            try:
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(resume_read_fd, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                private_handoff = b"".join(chunks).decode("utf-8").strip()
            except (OSError, UnicodeDecodeError):
                private_handoff = ""
            finally:
                os.close(resume_read_fd)
        if payload is not None and private_handoff:
            try:
                handoff_payload = json.loads(private_handoff)
            except json.JSONDecodeError:
                handoff_payload = None
            if isinstance(handoff_payload, dict):
                resume_value = handoff_payload.get("r")
                stream_topic_id = handoff_payload.get("p")
                turn_exchange_id = handoff_payload.get("x")
                stream_conversation_id = handoff_payload.get("i")
                stop_conduit_token = handoff_payload.get("c")
                turn_trace_id = handoff_payload.get("t")
                if isinstance(resume_value, str) and resume_value:
                    payload["stream_resume_value"] = resume_value
                if isinstance(stream_topic_id, str) and stream_topic_id:
                    payload["stream_topic_id"] = stream_topic_id
                if isinstance(turn_exchange_id, str) and turn_exchange_id:
                    payload["turn_exchange_id"] = turn_exchange_id
                if isinstance(stream_conversation_id, str) and stream_conversation_id:
                    payload["stream_conversation_id"] = stream_conversation_id
                if isinstance(stop_conduit_token, str) and stop_conduit_token:
                    payload["_cwa_stop_conduit_token"] = stop_conduit_token
                if isinstance(turn_trace_id, str) and turn_trace_id:
                    payload["_cwa_stop_turn_trace_id"] = turn_trace_id
            else:
                # Backward-compatible private handoff for an already-built helper.
                payload["stream_resume_value"] = private_handoff

        if payload is None:
            detail = "no helper result"
            if process.stderr is not None:
                try:
                    stderr = process.stderr.read().strip()
                except (OSError, ValueError):
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
            error_message = str(
                payload.get("error") or "WKWEBVIEW_AUTHORITY_TURN_FAILED"
            )
            detail = payload.get("detail")
            stage = payload.get("stage")
            if isinstance(stage, str) and stage.strip():
                error_message = f"{error_message} stage={stage.strip()}"
            raise RequestError(
                error_message,
                status_code=payload.get("status"),
                body_preview=detail,
                request_stage="wkwebview_authority_turn",
            )
        return payload

    def run_event_observer(
        self,
        invocation: list[str] | WKHelperInvocation,
        *,
        timeout: float,
        on_event: Callable[[dict[str, Any]], _ObserverResult | None],
        on_tick: Callable[[], _ObserverResult | None] | None = None,
        request_stage: str,
        launch_error_prefix: str,
    ) -> _ObserverResult | None:
        """Run a helper that emits ``WK_EVENT`` envelopes until a callback resolves it."""

        if not callable(on_event):
            raise TypeError("on_event must be callable")
        if on_tick is not None and not callable(on_tick):
            raise TypeError("on_tick must be callable")

        env = os.environ.copy()
        env.setdefault("NSUnbufferedIO", "YES")
        command, request_input = self.subprocess_input(invocation)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if request_input is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as error:
            raise RequestError(
                f"{launch_error_prefix}: {error}",
                request_stage=request_stage,
            ) from error

        if request_input is not None:
            assert process.stdin is not None
            try:
                process.stdin.write(request_input)
                process.stdin.close()
            except OSError:
                pass

        deadline = time.monotonic() + max(0.0, float(timeout))
        try:
            assert process.stdout is not None
            while time.monotonic() < deadline:
                if on_tick is not None:
                    resolved = on_tick()
                    if resolved is not None:
                        return resolved

                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                if not line.startswith(_EVENT_PREFIX):
                    continue
                try:
                    event = json.loads(line[len(_EVENT_PREFIX) :])
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                resolved = on_event(event)
                if resolved is not None:
                    return resolved
        finally:
            self.terminate_process(process)
        return None

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
