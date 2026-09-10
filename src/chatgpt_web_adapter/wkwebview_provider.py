from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any, Iterator, Sequence

from .browser_native_provider import (
    BrowserNativeBridgeStatus,
    BrowserNativeRuntimeTabReleaseResult,
    BrowserNativeTurnResult,
)
from .exceptions import RequestError
from .product_model_profile_pr8_10 import (
    PROFILE_TO_PRODUCT_MODE,
    normalize_model_profile,
)
from .status import _status_from_payload
from .types import ChatConversation, ConversationRef
from .wkwebview_shared_broker import WKSystemResumeBrokerClient

_HELPER_BUNDLE_ID = "local.gptty.webkit-authority"
_HELPER_DIRNAME = "wkwebview-authority"
_RESULT_PREFIX = "WK_RESULT "
_EVENT_PREFIX = "WK_EVENT "


class _WKSharedResumeState:
    def __init__(self, on_text_event: Any) -> None:
        self.condition = threading.Condition()
        self.on_text_event = on_text_event
        self.final: dict[str, Any] | None = None
        self.error: str | None = None
        self.stream_started = False
        self.stream_ended = False
        self.stream_terminal = False
        self.canonical_polls = 0
        self.last_canonical_status: int | None = None


class _WKSharedResumeBroker:
    """Multiplex resume streams through one persistent WKWebView helper process."""

    def __init__(self, helper: Path, *, idle_timeout: float = 1.0) -> None:
        self.helper = Path(helper)
        self.idle_timeout = max(0.0, float(idle_timeout))
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._ready_event: threading.Event | None = None
        self._pending: dict[str, _WKSharedResumeState] = {}
        self._idle_timer: threading.Timer | None = None
        self._generation = 0
        self._stderr_tail: list[str] = []

    def _drain_stderr(self, process: subprocess.Popen[str]) -> None:
        stream = process.stderr
        if stream is None:
            return
        try:
            for line in stream:
                value = line.rstrip()
                if not value:
                    continue
                with self._lock:
                    self._stderr_tail.append(value)
                    if len(self._stderr_tail) > 20:
                        del self._stderr_tail[:-20]
        except Exception:
            pass

    def _reader_loop(self, process: subprocess.Popen[str], ready: threading.Event) -> None:
        stream = process.stdout
        if stream is None:
            self._fail_current_process(process, "WKWEBVIEW_SHARED_RESUME_STDOUT_MISSING")
            return
        try:
            for raw in stream:
                if not raw.startswith(_EVENT_PREFIX):
                    continue
                try:
                    event = json.loads(raw[len(_EVENT_PREFIX) :])
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                if event_type == "broker_ready":
                    ready.set()
                    continue
                request_id = event.get("request_id")
                if not isinstance(request_id, str) or not request_id:
                    continue
                with self._lock:
                    state = self._pending.get(request_id)
                if state is None:
                    continue
                if event_type in {
                    "assistant_text_snapshot",
                    "assistant_text_delta",
                    "assistant_text_revision",
                }:
                    callback_event = dict(event)
                    callback_event.pop("request_id", None)
                    try:
                        state.on_text_event(callback_event)
                    except Exception:
                        pass
                    continue
                terminal = False
                with state.condition:
                    if event_type in {"broker_resume_started", "broker_stream_started"}:
                        state.stream_started = True
                    elif event_type == "broker_stream_ended":
                        state.stream_ended = True
                    elif event_type == "broker_stream_terminal":
                        state.stream_terminal = True
                    elif event_type == "broker_canonical_probe":
                        state.canonical_polls += 1
                        raw_status = event.get("status")
                        if isinstance(raw_status, int) and not isinstance(raw_status, bool):
                            state.last_canonical_status = raw_status
                    elif event_type == "broker_final":
                        state.final = event
                        terminal = True
                    elif event_type == "broker_error":
                        state.error = str(event.get("error") or "WKWEBVIEW_SHARED_RESUME_FAILED")
                        terminal = True
                    elif event_type == "broker_cancelled":
                        state.error = "WKWEBVIEW_SHARED_RESUME_CANCELLED"
                        terminal = True
                    if terminal:
                        state.condition.notify_all()
                if terminal:
                    self._finish_pending(request_id, state)
        finally:
            self._fail_current_process(process, "WKWEBVIEW_SHARED_RESUME_PROCESS_ENDED")

    def _fail_current_process(self, process: subprocess.Popen[str], error: str) -> None:
        with self._lock:
            if self._process is not process:
                return
            states = list(self._pending.values())
            self._pending.clear()
            self._process = None
            self._ready_event = None
            self._generation += 1
        for state in states:
            with state.condition:
                if state.final is None and state.error is None:
                    state.error = error
                state.condition.notify_all()

    def _finish_pending(self, request_id: str, state: _WKSharedResumeState) -> None:
        with self._lock:
            if self._pending.get(request_id) is state:
                del self._pending[request_id]
            self._schedule_idle_locked()

    def _schedule_idle_locked(self) -> None:
        if self._pending or self._process is None:
            return
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        self._generation += 1
        generation = self._generation
        timer = threading.Timer(self.idle_timeout, self._shutdown_if_idle, args=(generation,))
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _shutdown_if_idle(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation or self._pending or self._process is None:
                return
            process = self._process
            self._process = None
            self._ready_event = None
            self._idle_timer = None
            self._generation += 1
        self._send_to_process(process, {"type": "shutdown"}, ignore_errors=True)
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()

    def _send_to_process(
        self,
        process: subprocess.Popen[str],
        command: dict[str, Any],
        *,
        ignore_errors: bool = False,
    ) -> None:
        try:
            payload = json.dumps(command, separators=(",", ":"), ensure_ascii=True) + "\n"
            with self._write_lock:
                if process.stdin is None:
                    raise BrokenPipeError("broker stdin is unavailable")
                process.stdin.write(payload)
                process.stdin.flush()
        except (OSError, BrokenPipeError, ValueError) as error:
            if not ignore_errors:
                raise RequestError(
                    f"WKWEBVIEW_SHARED_RESUME_WRITE_FAILED: {error}",
                    request_stage="wkwebview_shared_resume",
                ) from error

    def _ensure_started(self) -> subprocess.Popen[str]:
        with self._lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            self._generation += 1
            process = self._process
            if process is not None and process.poll() is None:
                ready = self._ready_event
            else:
                ready = threading.Event()
                try:
                    process = subprocess.Popen(
                        [str(self.helper), "--resume-broker"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                    )
                except OSError as error:
                    raise RequestError(
                        f"WKWEBVIEW_SHARED_RESUME_LAUNCH_FAILED: {error}",
                        request_stage="wkwebview_shared_resume",
                    ) from error
                self._process = process
                self._ready_event = ready
                threading.Thread(
                    target=self._reader_loop,
                    args=(process, ready),
                    daemon=True,
                    name="wk-shared-resume-reader",
                ).start()
                threading.Thread(
                    target=self._drain_stderr,
                    args=(process,),
                    daemon=True,
                    name="wk-shared-resume-stderr",
                ).start()
        if ready is None or not ready.wait(timeout=10.0):
            self._fail_current_process(process, "WKWEBVIEW_SHARED_RESUME_READY_TIMEOUT")
            if process.poll() is None:
                process.terminate()
            detail = ""
            with self._lock:
                if self._stderr_tail:
                    detail = ": " + self._stderr_tail[-1]
            raise RequestError(
                "WKWEBVIEW_SHARED_RESUME_READY_TIMEOUT" + detail,
                request_stage="wkwebview_shared_resume",
            )
        return process

    def resume(
        self,
        *,
        conversation_id: str,
        resume_token: str,
        offset: int,
        timeout: float,
        on_text_event: Any,
    ) -> dict[str, Any]:
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id is required")
        if not isinstance(resume_token, str) or not resume_token:
            raise ValueError("resume_token is required")
        if not callable(on_text_event):
            raise TypeError("on_text_event must be callable")
        total_timeout = float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")
        process = self._ensure_started()
        request_id = str(uuid.uuid4())
        state = _WKSharedResumeState(on_text_event)
        with self._lock:
            if self._process is not process or process.poll() is not None:
                raise RequestError(
                    "WKWEBVIEW_SHARED_RESUME_PROCESS_UNAVAILABLE",
                    request_stage="wkwebview_shared_resume",
                )
            self._pending[request_id] = state
        try:
            self._send_to_process(
                process,
                {
                    "type": "start_resume",
                    "request_id": request_id,
                    "conversation_id": conversation_id.strip(),
                    "resume_token": resume_token,
                    "offset": int(offset),
                },
            )
        except Exception:
            self._finish_pending(request_id, state)
            raise

        deadline = time.monotonic() + total_timeout
        with state.condition:
            while state.final is None and state.error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                state.condition.wait(timeout=min(remaining, 0.5))
        if state.final is None and state.error is None:
            with self._lock:
                if self._pending.get(request_id) is state:
                    del self._pending[request_id]
                self._schedule_idle_locked()
            self._send_to_process(
                process,
                {"type": "cancel", "request_id": request_id},
                ignore_errors=True,
            )
            raise RequestError(
                "WKWEBVIEW_SHARED_RESUME_TIMEOUT:"
                f"stream_started={int(state.stream_started)}:"
                f"canonical_polls={state.canonical_polls}:"
                f"last_status={state.last_canonical_status if state.last_canonical_status is not None else 0}",
                request_stage="wkwebview_shared_resume",
            )
        if state.error is not None:
            raise RequestError(
                f"{state.error}:stream_started={int(state.stream_started)}:"
                f"canonical_polls={state.canonical_polls}:"
                f"last_status={state.last_canonical_status if state.last_canonical_status is not None else 0}",
                request_stage="wkwebview_shared_resume",
            )
        assert state.final is not None
        status = state.final.get("status")
        if not isinstance(status, int) or isinstance(status, bool):
            status = 200
        encoded = state.final.get("canonical_body_base64")
        return {
            "ok": True,
            "status": status,
            "conversation_id": conversation_id.strip(),
            "offset": int(offset),
            "stream_started": state.stream_started,
            "stream_ended": state.stream_ended,
            "stream_terminal_observed": state.stream_terminal,
            "canonical_completed": True,
            "canonical_polls": state.canonical_polls,
            "last_canonical_status": state.last_canonical_status,
            "canonical_body_base64": encoded if isinstance(encoded, str) else "",
        }

    def close(self) -> None:
        with self._lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            process = self._process
            self._process = None
            self._ready_event = None
            states = list(self._pending.values())
            self._pending.clear()
            self._generation += 1
        for state in states:
            with state.condition:
                if state.final is None and state.error is None:
                    state.error = "WKWEBVIEW_SHARED_RESUME_CLOSED"
                state.condition.notify_all()
        if process is None:
            return
        self._send_to_process(process, {"type": "shutdown"}, ignore_errors=True)
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.terminate()


class WKWebViewTurnProvider:
    """macOS browser-owned turn provider backed by a minimal WKWebView app.

    The provider owns the protected product write while the existing CWA runtime
    retains canonical reconciliation, finality, and product semantics.
    """

    browser_authority_backend = "wkwebview"
    streaming_source = "WKWEBVIEW_PASSIVE_SSE"
    revision_safe_streaming_supported = True
    supports_attachment_paths = True
    supports_model_slug = False
    temporary_chat_supported = False
    _shared_heavy_submit_gate = threading.Lock()
    _shared_heavy_submit_lock_path = (
        Path.home()
        / "Library"
        / "Application Support"
        / "chatgpt-web-adapter"
        / "wk-heavy-submit.lock"
    )

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        turn_timeout: float = 150.0,
        build_timeout: float = 30.0,
    ) -> None:
        if turn_timeout <= 0:
            raise ValueError("turn_timeout must be positive")
        if build_timeout <= 0:
            raise ValueError("build_timeout must be positive")
        self.state_dir = (
            Path(state_dir).expanduser()
            if state_dir is not None
            else Path.home() / "Library" / "Application Support" / "chatgpt-web-adapter"
        )
        self.turn_timeout = float(turn_timeout)
        self.build_timeout = float(build_timeout)
        self._profile_context = threading.local()
        self._authority_context = threading.local()
        self._canonical_context = threading.local()
        self._build_lock = threading.Lock()
        self._stopped_lock = threading.Lock()
        self._stopped_conversations: set[str] = set()
        self._final_payload_lock = threading.Lock()
        self._final_payload_cache: dict[str, dict[str, Any]] = {}
        self._stop_final_condition = threading.Condition()
        self._stopped_final_payloads: dict[str, dict[str, Any]] = {}
        self._shared_resume_lock = threading.Lock()
        self._shared_resume_instance: WKSystemResumeBrokerClient | None = None
        self._source_client: Any | None = None

    @property
    def helper_root(self) -> Path:
        return self.state_dir / _HELPER_DIRNAME

    @property
    def helper_app(self) -> Path:
        return self.helper_root / "WKChatGPTAuthority.app"

    @property
    def helper_binary(self) -> Path:
        return self.helper_app / "Contents" / "MacOS" / "WKChatGPTAuthority"

    def _source_paths(self) -> tuple[Path, Path, Path]:
        package_root = resources.files("chatgpt_web_adapter")
        helper_root = package_root.joinpath("wkwebview_helper")
        source = Path(str(helper_root.joinpath("WKChatGPTAuthority.m")))
        plist = Path(str(helper_root.joinpath("Info.plist")))
        minimal_shell = Path(str(helper_root.joinpath("minimal_security_shell.js")))
        return source, plist, minimal_shell

    @staticmethod
    def _source_digest(source: Path, plist: Path, minimal_shell: Path) -> str:
        digest = hashlib.sha256()
        digest.update(source.read_bytes())
        digest.update(plist.read_bytes())
        digest.update(minimal_shell.read_bytes())
        return digest.hexdigest()

    def _ensure_helper(self) -> Path:
        if sys.platform != "darwin":
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_UNAVAILABLE: macOS is required",
                request_stage="wkwebview_authority_build",
            )
        source, plist, minimal_shell = self._source_paths()
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

        expected = self._source_digest(source, plist, minimal_shell)
        stamp = self.helper_root / "source.sha256"
        with self._build_lock:
            self.helper_root.mkdir(parents=True, exist_ok=True)
            lock_path = self.helper_root / ".build.lock"
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                # Another process may have completed the same build while this
                # process waited for the OS-wide lock, so re-check under the lock.
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
                command = [
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
                self._run_build(command)
                self._run_build(
                    ["codesign", "--force", "--deep", "--sign", "-", str(self.helper_app)]
                )
                stamp.write_text(expected + "\n", encoding="utf-8")
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        return self.helper_binary

    def _run_build(self, command: list[str]) -> None:
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
    def _shared_resume_enabled() -> bool:
        value = os.environ.get("CWA_WK_SHARED_RESUME_BROKER", "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    @staticmethod
    def _curl_ws_second_leg_enabled() -> bool:
        value = os.environ.get("CWA_WK_CURL_WS_SECOND_LEG", "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    @staticmethod
    def _minimal_security_shell_enabled() -> bool:
        value = os.environ.get("CWA_WK_MINIMAL_SECURITY_SHELL", "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def _get_shared_resume_broker(self) -> WKSystemResumeBrokerClient:
        with self._shared_resume_lock:
            broker = self._shared_resume_instance
            if broker is None:
                broker = WKSystemResumeBrokerClient(self._ensure_helper())
                self._shared_resume_instance = broker
            return broker

    def _close_shared_resume_broker(self) -> None:
        with self._shared_resume_lock:
            broker = self._shared_resume_instance
            self._shared_resume_instance = None
        if broker is not None:
            broker.close()

    @contextmanager
    def _heavy_submit_gate(self, timeout: float) -> Iterator[None]:
        wait_timeout = max(0.001, float(timeout))
        started = time.monotonic()
        acquired = self._shared_heavy_submit_gate.acquire(timeout=wait_timeout)
        if not acquired:
            raise RequestError(
                "WKWEBVIEW_HEAVY_SUBMIT_GATE_TIMEOUT",
                request_stage="wkwebview_authority_turn",
            )
        fd: int | None = None
        try:
            remaining = max(0.001, wait_timeout - (time.monotonic() - started))
            path = self._shared_heavy_submit_lock_path
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            deadline = time.monotonic() + remaining
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RequestError(
                            "WKWEBVIEW_HEAVY_SUBMIT_GATE_TIMEOUT",
                            request_stage="wkwebview_authority_turn",
                        )
                    time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
            self._shared_heavy_submit_gate.release()

    def status(self) -> BrowserNativeBridgeStatus:
        try:
            binary = self._ensure_helper()
        except Exception:
            return BrowserNativeBridgeStatus(False, False)
        return BrowserNativeBridgeStatus(
            available=binary.is_file(),
            extension_connected=binary.is_file(),
            host_pid=None,
            extension_id="wkwebview-authority",
            runtime_tab_id=None,
        )

    def build_canonical_client(self, source_client: Any) -> Any:
        from .wkwebview_canonical import WKWebViewCanonicalClient

        self._source_client = source_client
        return WKWebViewCanonicalClient(source_client, self)

    @staticmethod
    def _canonical_payload_is_final(payload: dict[str, Any]) -> bool:
        mapping = payload.get("mapping")
        current_node = payload.get("current_node")
        if not isinstance(mapping, dict) or not isinstance(current_node, str) or not current_node:
            return False
        node = mapping.get(current_node)
        message = node.get("message") if isinstance(node, dict) else None
        if not isinstance(message, dict):
            return False
        author = message.get("author")
        metadata = message.get("metadata")
        finish = metadata.get("finish_details") if isinstance(metadata, dict) else None
        role = author.get("role") if isinstance(author, dict) else None
        if role != "assistant" or message.get("recipient") not in {None, "all"}:
            return False
        active = {"running", "in_progress", "pending", "queued", "started", "streaming"}
        statuses = [
            payload.get("async_status"),
            payload.get("status"),
            node.get("async_status") if isinstance(node, dict) else None,
            node.get("status") if isinstance(node, dict) else None,
            metadata.get("async_status") if isinstance(metadata, dict) else None,
            metadata.get("status") if isinstance(metadata, dict) else None,
            message.get("status"),
        ]
        if any(str(value or "").lower() in active for value in statuses):
            return False
        completed = {"completed", "complete", "finished", "done", "success", "succeeded", "finished_successfully"}
        finish_present = (
            isinstance(finish, dict)
            and (
                isinstance(finish.get("type"), str)
                or isinstance(finish.get("reason"), str)
            )
        )
        return bool(
            finish_present
            or message.get("end_turn") is True
            or any(str(value or "").lower() in completed for value in statuses)
        )

    def _canonical_payload_matches_write(
        self,
        payload: dict[str, Any],
        *,
        text: str,
        baseline_current_node: str | None,
    ) -> bool:
        current_node = payload.get("current_node")
        if (
            baseline_current_node is not None
            and (
                not isinstance(current_node, str)
                or not current_node
                or current_node == baseline_current_node
            )
        ):
            return False
        return self._current_branch_contains_user_text(
            payload,
            text,
        ) and self._canonical_payload_is_final(payload)

    def _resume_via_curl_ws_second_leg(
        self,
        *,
        conversation_id: str,
        resume_value: str,
        timeout: float,
        relay_text_event: Any,
        text: str,
        baseline_current_node: str | None,
    ) -> dict[str, Any]:
        source_client = self._source_client
        if source_client is None:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_SOURCE_CLIENT_MISSING",
                request_stage="wkwebview_curl_ws_second_leg",
            )
        try:
            import copy

            from curl_cffi import requests as curl_requests
        except ImportError as error:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_DEPENDENCY_MISSING",
                request_stage="wkwebview_curl_ws_second_leg",
            ) from error

        ws_client = copy.copy(source_client)
        state: dict[str, Any] = {"conversation_id": conversation_id}
        capture = getattr(ws_client, "_capture_resume_token_diagnostics", None)
        if not callable(capture):
            raise RequestError(
                "WKWEBVIEW_CURL_WS_TOPIC_DECODER_MISSING",
                request_stage="wkwebview_curl_ws_second_leg",
            )
        capture(resume_value, state)
        topic_id = state.get("resume_turn_topic_id")
        if not isinstance(topic_id, str) or not topic_id:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_TOPIC_UNRESOLVED",
                request_stage="wkwebview_curl_ws_second_leg",
            )

        celsius_path = "/backend-api/celsius/ws/user"
        celsius_headers = ws_client._build_headers(
            {
                "accept": "*/*",
                "referer": "https://chatgpt.com/",
                "x-openai-target-path": celsius_path,
                "x-openai-target-route": celsius_path,
            }
        )
        with curl_requests.Session(impersonate="safari") as celsius_session:
            celsius = celsius_session.get(
                "https://chatgpt.com" + celsius_path,
                headers=celsius_headers,
                timeout=min(20.0, max(1.0, float(timeout))),
            )
            if celsius.status_code != 200:
                raise RequestError(
                    f"WKWEBVIEW_CURL_WS_CELSIUS_HTTP:{celsius.status_code}",
                    request_stage="wkwebview_curl_ws_second_leg",
                    status_code=celsius.status_code,
                )
            celsius_payload = celsius.json()
        websocket_url = (
            celsius_payload.get("websocket_url") if isinstance(celsius_payload, dict) else None
        )
        if not isinstance(websocket_url, str) or not websocket_url:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_URL_MISSING",
                request_stage="wkwebview_curl_ws_second_leg",
            )

        def probe_celsius() -> dict[str, Any]:
            return {"websocket_url": websocket_url}

        ws_client._probe_celsius_ws_user = probe_celsius
        raw_sequence = 0

        def on_token(value: str) -> None:
            nonlocal raw_sequence
            if not isinstance(value, str) or not value:
                return
            raw_sequence += 1
            event: dict[str, Any] = {
                "type": "assistant_text_delta",
                "sequence": raw_sequence,
                "delta": value,
            }
            message_id = state.get("message_id")
            if isinstance(message_id, str) and message_id:
                event["message_id"] = message_id
            relay_text_event(event)

        started = time.monotonic()
        ws_stream = getattr(ws_client, "_stream_handoff_via_ws_topic", None)
        if not callable(ws_stream):
            raise RequestError(
                "WKWEBVIEW_CURL_WS_STREAM_METHOD_MISSING",
                request_stage="wkwebview_curl_ws_second_leg",
            )
        ws_stream(
            topic_id,
            state=state,
            on_event=None,
            on_token=on_token,
        )

        deadline = started + max(1.0, float(timeout))
        canonical_url = f"https://chatgpt.com/backend-api/conversation/{conversation_id}"
        canonical_headers = ws_client._build_headers(
            {
                "accept": "application/json",
                "referer": f"https://chatgpt.com/c/{conversation_id}",
            }
        )
        last_status = 0
        with curl_requests.Session(impersonate="safari") as canonical_session:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RequestError(
                        f"WKWEBVIEW_CURL_WS_CANONICAL_TIMEOUT:last_status={last_status}",
                        request_stage="wkwebview_curl_ws_second_leg",
                    )
                response = canonical_session.get(
                    canonical_url,
                    headers=canonical_headers,
                    timeout=min(20.0, max(1.0, remaining)),
                )
                last_status = response.status_code
                if response.status_code in {401, 403}:
                    raise RequestError(
                        f"WKWEBVIEW_CURL_WS_CANONICAL_HTTP:{response.status_code}",
                        request_stage="wkwebview_curl_ws_second_leg",
                        status_code=response.status_code,
                    )
                if response.status_code == 200:
                    canonical_payload = response.json()
                    if isinstance(
                        canonical_payload, dict
                    ) and self._canonical_payload_matches_write(
                        canonical_payload,
                        text=text,
                        baseline_current_node=baseline_current_node,
                    ):
                        self._cache_final_payload(conversation_id, canonical_payload)
                        return {
                            "ok": True,
                            "status": 200,
                            "conversation_id": conversation_id,
                            "canonical_completed": True,
                            "stream_started": True,
                            "stream_ended": True,
                            "stream_terminal_observed": True,
                            "ws_token_events": raw_sequence,
                        }
                time.sleep(min(0.5, max(0.05, remaining)))

    @staticmethod
    def _decode_helper_json(
        payload: dict[str, Any],
        *,
        request_stage: str,
        error_prefix: str,
    ) -> dict[str, Any]:
        status = payload.get("status")
        if not isinstance(status, int) or status < 200 or status >= 300:
            raise RequestError(
                f"{error_prefix}_HTTP_STATUS:{status}",
                request_stage=request_stage,
                status_code=status if isinstance(status, int) else None,
            )
        encoded = payload.get("body_base64")
        if not isinstance(encoded, str):
            raise RequestError(
                f"{error_prefix}_BODY_MISSING",
                request_stage=request_stage,
            )
        try:
            raw = base64.b64decode(encoded, validate=True)
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RequestError(
                f"{error_prefix}_JSON_INVALID",
                request_stage=request_stage,
            ) from error
        if not isinstance(parsed, dict):
            raise RequestError(
                f"{error_prefix}_JSON_OBJECT_REQUIRED",
                request_stage=request_stage,
            )
        return parsed

    @staticmethod
    def _is_client_stopped_payload(payload: dict[str, Any]) -> bool:
        mapping = payload.get("mapping")
        current_node = payload.get("current_node")
        if not isinstance(mapping, dict) or not isinstance(current_node, str) or not current_node:
            return False
        node = mapping.get(current_node)
        message = node.get("message") if isinstance(node, dict) else None
        if not isinstance(message, dict):
            return False
        author = message.get("author")
        metadata = message.get("metadata")
        finish = metadata.get("finish_details") if isinstance(metadata, dict) else None
        return (
            isinstance(author, dict)
            and author.get("role") == "assistant"
            and message.get("recipient") in {None, "all"}
            and isinstance(finish, dict)
            and finish.get("type") == "interrupted"
            and finish.get("reason") == "client_stopped"
        )

    def _cache_final_payload(self, conversation_id: str, payload: dict[str, Any]) -> None:
        with self._final_payload_lock:
            self._final_payload_cache[conversation_id] = payload
        cache = getattr(self._canonical_context, "current_nodes", None)
        if not isinstance(cache, dict):
            cache = {}
            self._canonical_context.current_nodes = cache
        current_node = payload.get("current_node")
        cache[conversation_id] = (
            current_node if isinstance(current_node, str) and current_node else None
        )
        if self._is_client_stopped_payload(payload):
            with self._stop_final_condition:
                self._stopped_final_payloads[conversation_id] = payload
                self._stop_final_condition.notify_all()

    def _wait_for_stopped_final_payload(
        self,
        conversation_id: str,
        *,
        timeout: float,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._stop_final_condition:
            while True:
                payload = self._stopped_final_payloads.get(conversation_id)
                if isinstance(payload, dict):
                    return payload
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._stop_final_condition.wait(timeout=min(0.5, remaining))

    def _wait_for_canonical_stop_proof(
        self,
        conversation_id: str,
        *,
        timeout: float,
    ) -> dict[str, Any] | None:
        total_timeout = max(0.0, timeout)
        if total_timeout <= 0:
            return None
        binary = self._ensure_helper()
        command = [
            str(binary),
            "--observe-conversation",
            conversation_id,
            "--poll-interval",
            "3.000",
            "--timeout",
            f"{total_timeout:.3f}",
        ]
        env = os.environ.copy()
        env.setdefault("NSUnbufferedIO", "YES")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as error:
            raise RequestError(
                f"WKWEBVIEW_STOP_OBSERVER_LAUNCH_FAILED: {error}",
                request_stage="wkwebview_stop_generation",
            ) from error

        deadline = time.monotonic() + total_timeout
        try:
            assert process.stdout is not None
            while time.monotonic() < deadline:
                with self._stop_final_condition:
                    cached = self._stopped_final_payloads.get(conversation_id)
                if isinstance(cached, dict):
                    return cached
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                if not line.startswith(_EVENT_PREFIX):
                    continue
                try:
                    raw_event = json.loads(line[len(_EVENT_PREFIX) :])
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw_event, dict) or raw_event.get("type") != "canonical_payload":
                    continue
                try:
                    payload = self._decode_helper_json(
                        raw_event,
                        request_stage="wkwebview_stop_generation",
                        error_prefix="WKWEBVIEW_STOP_CANONICAL",
                    )
                except RequestError as error:
                    if error.status_code in {401, 403}:
                        raise
                    continue
                if self._is_client_stopped_payload(payload):
                    self._cache_final_payload(conversation_id, payload)
                    return payload
        finally:
            self._terminate_observer_process(process)
        return None

    def _read_conversation_payload_uncached(
        self,
        conversation_id: str,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        binary = self._ensure_helper()
        payload = self._run_helper(
            [
                str(binary),
                "--canonical-conversation",
                conversation_id,
                "--timeout",
                f"{timeout:.3f}",
            ],
            timeout=timeout,
        )
        return self._decode_helper_json(
            payload,
            request_stage="wkwebview_canonical_read",
            error_prefix="WKWEBVIEW_CANONICAL",
        )

    def read_conversation_payload(
        self,
        conversation_id: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        ref = ConversationRef(conversation_id)
        total_timeout = float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")
        with self._final_payload_lock:
            cached_final = self._final_payload_cache.pop(ref.conversation_id, None)
        if isinstance(cached_final, dict):
            cache = getattr(self._canonical_context, "current_nodes", None)
            if not isinstance(cache, dict):
                cache = {}
                self._canonical_context.current_nodes = cache
            current_node = cached_final.get("current_node")
            cache[ref.conversation_id] = (
                current_node if isinstance(current_node, str) and current_node else None
            )
            return cached_final
        parsed = self._read_conversation_payload_uncached(
            ref.conversation_id,
            timeout=total_timeout,
        )
        cache = getattr(self._canonical_context, "current_nodes", None)
        if not isinstance(cache, dict):
            cache = {}
            self._canonical_context.current_nodes = cache
        current_node = parsed.get("current_node")
        cache[ref.conversation_id] = (
            current_node if isinstance(current_node, str) and current_node else None
        )
        return parsed

    def read_catalog_payload(
        self,
        catalog: str,
        *,
        offset: int = 0,
        limit: int = 100,
        is_archived: bool = False,
        is_starred: bool = False,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        normalized = catalog.strip().lower() if isinstance(catalog, str) else ""
        if normalized not in {"conversations", "models"}:
            raise ValueError("catalog must be conversations or models")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative int")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an int between 1 and 100")
        total_timeout = float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")
        binary = self._ensure_helper()
        command = [
            str(binary),
            "--catalog",
            normalized,
            "--offset",
            str(offset),
            "--limit",
            str(limit),
            "--timeout",
            f"{total_timeout:.3f}",
        ]
        if is_archived:
            command.append("--archived")
        if is_starred:
            command.append("--starred")
        payload = self._run_helper(command, timeout=total_timeout)
        return self._decode_helper_json(
            payload,
            request_stage="wkwebview_catalog_read",
            error_prefix="WKWEBVIEW_CATALOG",
        )

    def _cached_current_node(self, conversation_id: str | None) -> str | None:
        if not conversation_id:
            return None
        cache = getattr(self._canonical_context, "current_nodes", None)
        if not isinstance(cache, dict):
            return None
        value = cache.get(conversation_id)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _current_branch_contains_user_text(payload: dict[str, Any], text: str) -> bool:
        mapping = payload.get("mapping")
        current = payload.get("current_node")
        if not isinstance(mapping, dict) or not isinstance(current, str):
            return False
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            node = mapping.get(current)
            if not isinstance(node, dict):
                return False
            message = node.get("message")
            if isinstance(message, dict):
                author = message.get("author")
                content = message.get("content")
                parts = content.get("parts") if isinstance(content, dict) else None
                if (
                    isinstance(author, dict)
                    and author.get("role") == "user"
                    and isinstance(parts, list)
                ):
                    rendered = "\n".join(part for part in parts if isinstance(part, str))
                    if rendered.strip() == text.strip():
                        return True
            parent = node.get("parent")
            current = parent if isinstance(parent, str) else ""
        return False

    def _wait_for_canonical_write_commit(
        self,
        *,
        conversation_id: str,
        text: str,
        baseline_current_node: str | None,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(2.0, min(float(timeout), 30.0))
        while time.monotonic() < deadline:
            try:
                payload = self.read_conversation_payload(
                    conversation_id,
                    timeout=min(10.0, max(1.0, deadline - time.monotonic())),
                )
            except Exception:
                time.sleep(0.25)
                continue
            current_node = payload.get("current_node")
            node_changed = (
                baseline_current_node is None
                or (
                    isinstance(current_node, str)
                    and current_node
                    and current_node != baseline_current_node
                )
            )
            if node_changed and self._current_branch_contains_user_text(payload, text):
                return payload
            time.sleep(0.25)
        raise RequestError(
            "WKWEBVIEW_WRITE_CANONICAL_COMMIT_NOT_PROVEN",
            request_stage="wkwebview_authority_postwrite",
        )

    @contextmanager
    def require_profile(self, profile: str) -> Iterator[str]:
        normalized = normalize_model_profile(profile)
        if getattr(self._profile_context, "profile", None) is not None:
            raise RuntimeError("nested model-profile requirements are not supported")
        self._profile_context.profile = normalized
        try:
            yield normalized
        finally:
            if hasattr(self._profile_context, "profile"):
                del self._profile_context.profile

    def set_browser_authority_lease(self, lease_id: str) -> None:
        if not isinstance(lease_id, str) or not lease_id.strip():
            raise ValueError("browser authority lease_id is required")
        self._authority_context.lease_id = lease_id.strip()

    def clear_browser_authority_lease(self) -> None:
        if hasattr(self._authority_context, "lease_id"):
            del self._authority_context.lease_id

    def _current_browser_authority_lease_id(self) -> str | None:
        value = getattr(self._authority_context, "lease_id", None)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _normalize_attachment_paths(
        attachment_paths: Sequence[str | Path] | None,
    ) -> tuple[str, ...]:
        if attachment_paths is None:
            return ()
        if isinstance(attachment_paths, (str, bytes, bytearray, Path)):
            raise TypeError("attachment_paths must be a sequence of local paths")
        normalized: list[str] = []
        for index, raw_path in enumerate(attachment_paths):
            if not isinstance(raw_path, (str, Path)):
                raise TypeError(f"attachment_paths[{index}] must be str or Path")
            try:
                path = Path(raw_path).expanduser().resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ValueError(f"attachment_paths[{index}] is unavailable") from error
            if not path.is_file():
                raise ValueError(f"attachment_paths[{index}] must reference a regular file")
            normalized.append(str(path))
        return tuple(normalized)

    def _upload_minimal_security_attachments(
        self,
        attachment_paths: Sequence[str],
    ) -> tuple[dict[str, Any], ...] | None:
        if not attachment_paths:
            return ()
        source_client = self._source_client
        uploader = getattr(source_client, "_upload_media_files", None) if source_client is not None else None
        if not callable(uploader):
            return None
        try:
            uploaded = uploader(
                [(Path(path), Path(path).name) for path in attachment_paths]
            )
        except Exception:
            return None
        if not isinstance(uploaded, list) or len(uploaded) != len(attachment_paths):
            return None
        descriptors: list[dict[str, Any]] = []
        for item in uploaded:
            if not isinstance(item, dict):
                return None
            file_id = item.get("file_id")
            if not isinstance(file_id, str) or not file_id.strip():
                return None
            descriptor = {
                "file_id": file_id.strip(),
                "file_name": item.get("file_name") if isinstance(item.get("file_name"), str) else "attachment",
                "file_size": item.get("file_size") if isinstance(item.get("file_size"), int) else None,
                "mime_type": item.get("mime_type") if isinstance(item.get("mime_type"), str) else None,
                "width": item.get("width") if isinstance(item.get("width"), int) else None,
                "height": item.get("height") if isinstance(item.get("height"), int) else None,
            }
            descriptors.append(descriptor)
        return tuple(descriptors)

    def _helper_command(
        self,
        *,
        conversation_id: str | None,
        text: str | None,
        timeout: float,
        attachment_paths: Sequence[str] = (),
        expected_current_node: str | None = None,
        stop_only: bool = False,
    ) -> list[str]:
        binary = self._ensure_helper()
        url = (
            f"https://chatgpt.com/c/{conversation_id}"
            if conversation_id
            else "https://chatgpt.com/"
        )
        command = [str(binary), "--url", url, "--timeout", f"{timeout:.3f}"]
        if text is not None:
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            command += ["--prompt-base64", encoded]
        if isinstance(expected_current_node, str) and expected_current_node.strip():
            command += ["--expected-current-node", expected_current_node.strip()]
        for path in attachment_paths:
            command += ["--attach", path]
        profile = getattr(self._profile_context, "profile", None)
        if isinstance(profile, str):
            command += ["--profile", PROFILE_TO_PRODUCT_MODE[profile]]
        if stop_only:
            command.append("--stop-only")
        return command

    def _run_helper(self, command: list[str], *, timeout: float) -> dict[str, Any]:
        env = os.environ.copy()
        env.setdefault("NSUnbufferedIO", "YES")
        try:
            completed = subprocess.run(
                command,
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
            if line.startswith(_RESULT_PREFIX):
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

    def _run_helper_streaming(
        self,
        command: list[str],
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
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as error:
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_LAUNCH_FAILED: {error}",
                request_stage="wkwebview_authority_turn",
            ) from error

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
                self._terminate_observer_process(process)

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
    def _terminate_observer_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)

    def observe_turn(
        self,
        *,
        conversation_id: str,
        turn_exchange_id: str | None,
        browser_authority_lease_id: str,
        timeout: float,
        on_event: Any = None,
    ) -> dict[str, Any]:
        """Observe one committed turn through one lightweight WebKit context.

        The helper stays on ``robots.txt`` and emits authenticated canonical
        snapshots. It owns no product write authority; shared CWA code remains
        responsible for interpreting snapshots and reconciling canonical finality.
        """

        ref = ConversationRef(conversation_id)
        if not isinstance(browser_authority_lease_id, str) or not browser_authority_lease_id.strip():
            raise ValueError("browser_authority_lease_id is required")
        total_timeout = float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")

        binary = self._ensure_helper()
        command = [
            str(binary),
            "--observe-conversation",
            ref.conversation_id,
            "--poll-interval",
            "1.000",
            "--timeout",
            f"{total_timeout:.3f}",
        ]
        env = os.environ.copy()
        env.setdefault("NSUnbufferedIO", "YES")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as error:
            raise RequestError(
                f"WKWEBVIEW_CANONICAL_OBSERVER_LAUNCH_FAILED: {error}",
                request_stage="browser_native_observe_turn",
            ) from error

        deadline = time.monotonic() + total_timeout
        last_message_id: str | None = None
        last_finish_reason: str | None = None
        try:
            assert process.stdout is not None
            while time.monotonic() < deadline:
                if self.stop_requested_for(ref.conversation_id):
                    return {
                        "ok": True,
                        "conversationId": ref.conversation_id,
                        "turnExchangeId": turn_exchange_id,
                        "messageId": last_message_id,
                        "finishReason": "stopped",
                    }

                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                if not line.startswith(_EVENT_PREFIX):
                    continue
                try:
                    raw_event = json.loads(line[len(_EVENT_PREFIX) :])
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw_event, dict) or raw_event.get("type") != "canonical_payload":
                    continue
                try:
                    payload = self._decode_helper_json(
                        raw_event,
                        request_stage="browser_native_observe_turn",
                        error_prefix="WKWEBVIEW_CANONICAL_OBSERVER",
                    )
                except RequestError as error:
                    if error.status_code in {401, 403}:
                        raise
                    continue

                current_node = payload.get("current_node")
                cache = getattr(self._canonical_context, "current_nodes", None)
                if not isinstance(cache, dict):
                    cache = {}
                    self._canonical_context.current_nodes = cache
                cache[ref.conversation_id] = (
                    current_node if isinstance(current_node, str) and current_node else None
                )

                if on_event is not None:
                    try:
                        on_event({"type": "canonical_payload_snapshot", "payload": payload})
                    except Exception:
                        pass

                status = _status_from_payload(payload)
                last_message_id = status.message_id or last_message_id
                last_finish_reason = status.finish_reason or last_finish_reason
                if status.status == "completed":
                    return {
                        "ok": True,
                        "conversationId": ref.conversation_id,
                        "turnExchangeId": turn_exchange_id,
                        "messageId": last_message_id,
                        "finishReason": last_finish_reason or "stop",
                    }
        finally:
            self._terminate_observer_process(process)

        raise RequestError(
            "PASSIVE_OBSERVER_STREAM_ENDED_WITHOUT_TERMINAL",
            request_stage="browser_native_observe_turn",
        )

    def _send_text_impl(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str | None = None,
        timeout: float | None = None,
        attachment_paths: Sequence[str | Path] | None = None,
        model_slug: str | None = None,
        on_text_event: Any = None,
        on_write_identity: Any = None,
    ) -> BrowserNativeTurnResult:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text is required")
        if on_write_identity is not None and not callable(on_write_identity):
            raise TypeError("on_write_identity must be callable")
        total_timeout = self.turn_timeout if timeout is None else float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")
        conversation_id = None
        baseline_current_node = None
        prewrite_payload: dict[str, Any] | None = None
        if conversation is not None:
            conversation_id = ConversationRef.from_any(conversation).conversation_id
            self.clear_stop_requested_for(conversation_id)
            baseline_current_node = self._cached_current_node(conversation_id)
            if baseline_current_node is None:
                prewrite_payload = self.read_conversation_payload(
                    conversation_id,
                    timeout=min(15.0, total_timeout),
                )
                value = prewrite_payload.get("current_node")
                baseline_current_node = value if isinstance(value, str) and value else None
        attachments = self._normalize_attachment_paths(attachment_paths)
        selected_profile = getattr(self._profile_context, "profile", None)
        use_minimal_security_shell = (
            on_text_event is not None
            and self._minimal_security_shell_enabled()
            and self._curl_ws_second_leg_enabled()
            and model_slug is None
        )
        minimal_parent_message_id = None
        minimal_model_slug = None
        minimal_thinking_effort = None
        minimal_attachment_descriptors: tuple[dict[str, Any], ...] = ()
        if use_minimal_security_shell and conversation_id is not None:
            if not isinstance(prewrite_payload, dict):
                prewrite_payload = self.read_conversation_payload(
                    conversation_id,
                    timeout=min(15.0, total_timeout),
                )
            current_value = prewrite_payload.get("current_node")
            baseline_current_node = (
                current_value if isinstance(current_value, str) and current_value else None
            )
            mapping = (
                prewrite_payload.get("mapping")
                if isinstance(prewrite_payload.get("mapping"), dict)
                else {}
            )
            node = mapping.get(baseline_current_node) if baseline_current_node else None
            message = node.get("message") if isinstance(node, dict) else None
            author = message.get("author") if isinstance(message, dict) else None
            role = author.get("role") if isinstance(author, dict) else None
            message_id = message.get("id") if isinstance(message, dict) else None
            if role == "assistant" and isinstance(message_id, str) and message_id.strip():
                minimal_parent_message_id = message_id.strip()
            else:
                use_minimal_security_shell = False
            if use_minimal_security_shell and not isinstance(selected_profile, str):
                selected_model = prewrite_payload.get("default_model_slug")
                metadata = message.get("metadata") if isinstance(message, dict) else None
                selected_effort = metadata.get("thinking_effort") if isinstance(metadata, dict) else None
                if isinstance(selected_model, str) and selected_model.strip():
                    minimal_model_slug = selected_model.strip()
                    if isinstance(selected_effort, str) and selected_effort.strip():
                        minimal_thinking_effort = selected_effort.strip()
                    elif "thinking" in minimal_model_slug.lower():
                        use_minimal_security_shell = False
                else:
                    use_minimal_security_shell = False
        if use_minimal_security_shell and attachments:
            uploaded_descriptors = self._upload_minimal_security_attachments(attachments)
            if uploaded_descriptors is None:
                use_minimal_security_shell = False
            else:
                minimal_attachment_descriptors = uploaded_descriptors
        command = self._helper_command(
            conversation_id=conversation_id,
            text=text,
            timeout=total_timeout,
            attachment_paths=() if use_minimal_security_shell else attachments,
            expected_current_node=baseline_current_node,
        )

        stream_sequence = 0

        def make_stream_relay() -> Any:
            phase_last_sequence = 0

            def relay(event: dict[str, Any]) -> None:
                nonlocal stream_sequence, phase_last_sequence
                if not isinstance(event, dict):
                    return
                raw_sequence = event.get("sequence")
                if isinstance(raw_sequence, int) and not isinstance(raw_sequence, bool) and raw_sequence > 0:
                    if phase_last_sequence and raw_sequence <= phase_last_sequence:
                        return
                    increment = (
                        raw_sequence - phase_last_sequence if phase_last_sequence else 1
                    )
                    phase_last_sequence = raw_sequence
                else:
                    increment = 1
                stream_sequence += max(1, increment)
                normalized = dict(event)
                normalized["sequence"] = stream_sequence
                on_text_event(normalized)

            return relay

        if on_text_event is not None:
            command += [
                "--observe-submit",
                "--observe-stream",
                "--stream-probe-until-resume-token",
            ]
            if use_minimal_security_shell:
                command.append("--minimal-security-shell")
                if conversation_id is not None and minimal_parent_message_id is not None:
                    command += [
                        "--minimal-conversation-id",
                        conversation_id,
                        "--minimal-parent-message-id",
                        minimal_parent_message_id,
                    ]
                    if minimal_model_slug is not None:
                        command += ["--minimal-model-slug", minimal_model_slug]
                    if minimal_thinking_effort is not None:
                        command += ["--minimal-thinking-effort", minimal_thinking_effort]
                if minimal_attachment_descriptors:
                    encoded_attachments = base64.b64encode(
                        json.dumps(
                            list(minimal_attachment_descriptors),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).decode("ascii")
                    command += [
                        "--minimal-attachments-base64",
                        encoded_attachments,
                        "--minimal-attachment-count",
                        str(len(minimal_attachment_descriptors)),
                    ]
        started = time.monotonic()
        resume_handoff_path: Path | None = None
        try:
            if on_text_event is not None:
                fd, raw_handoff_path = tempfile.mkstemp(prefix="cwa-wk-resume-")
                os.close(fd)
                resume_handoff_path = Path(raw_handoff_path)
                phase_timeout = total_timeout
                if self._shared_resume_enabled() or self._curl_ws_second_leg_enabled():
                    gate_wait = max(0.001, total_timeout - (time.monotonic() - started))
                    with self._heavy_submit_gate(gate_wait):
                        phase_timeout = max(1.0, total_timeout - (time.monotonic() - started))
                        payload = self._run_helper_streaming(
                            command,
                            timeout=phase_timeout,
                            on_text_event=make_stream_relay(),
                            on_lifecycle_event=on_write_identity,
                            extra_env={"CWA_WK_RESUME_HANDOFF_FILE": str(resume_handoff_path)},
                        )
                else:
                    payload = self._run_helper_streaming(
                        command,
                        timeout=phase_timeout,
                        on_text_event=make_stream_relay(),
                        on_lifecycle_event=on_write_identity,
                        extra_env={"CWA_WK_RESUME_HANDOFF_FILE": str(resume_handoff_path)},
                    )
                try:
                    resume_value = resume_handoff_path.read_text(encoding="utf-8").strip()
                except OSError:
                    resume_value = ""
                if resume_value:
                    payload["stream_resume_value"] = resume_value
            else:
                payload = self._run_helper(command, timeout=total_timeout)
        finally:
            if resume_handoff_path is not None:
                resume_handoff_path.unlink(missing_ok=True)
        result_conversation_id = payload.get("conversation_id")
        if not isinstance(result_conversation_id, str) or not result_conversation_id.strip():
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_CONVERSATION_ID_UNRESOLVED",
                request_stage="wkwebview_authority_turn",
            )
        response_status = payload.get("response_status", 200)
        if not isinstance(response_status, int) or not (200 <= response_status < 300):
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_HTTP_STATUS:{response_status}",
                request_stage="wkwebview_authority_turn",
                status_code=response_status if isinstance(response_status, int) else None,
            )
        attachment_count = payload.get("attachment_count", 0)
        if not isinstance(attachment_count, int) or isinstance(attachment_count, bool):
            attachment_count = 0
        if attachments and attachment_count != len(attachments):
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_ATTACHMENT_COUNT_MISMATCH",
                request_stage="wkwebview_authority_turn",
            )
        helper_canonical_committed = payload.get("canonical_committed") is True
        helper_write_commit_proven = (
            payload.get("write_commit_proven") is True or helper_canonical_committed
        )
        committed_current_node = payload.get("committed_current_node")
        if helper_canonical_committed:
            cache = getattr(self._canonical_context, "current_nodes", None)
            if not isinstance(cache, dict):
                cache = {}
                self._canonical_context.current_nodes = cache
            cache[result_conversation_id.strip()] = (
                committed_current_node
                if isinstance(committed_current_node, str) and committed_current_node
                else None
            )
        elif not helper_write_commit_proven:
            self._wait_for_canonical_write_commit(
                conversation_id=result_conversation_id.strip(),
                text=text,
                baseline_current_node=baseline_current_node,
                timeout=min(30.0, total_timeout),
            )

        phase_one_final_cached = False
        encoded_phase_one_final = payload.get("canonical_body_base64")
        if isinstance(encoded_phase_one_final, str) and encoded_phase_one_final:
            final_payload = self._decode_helper_json(
                {
                    "status": payload.get("response_status", 200),
                    "body_base64": encoded_phase_one_final,
                },
                request_stage="wkwebview_stream_finality",
                error_prefix="WKWEBVIEW_STREAM_CANONICAL",
            )
            if self._canonical_payload_matches_write(
                final_payload,
                text=text,
                baseline_current_node=baseline_current_node,
            ):
                self._cache_final_payload(result_conversation_id.strip(), final_payload)
                phase_one_final_cached = True

        passive_observer_armed = on_text_event is None
        if on_text_event is not None:
            resume_value = payload.pop("stream_resume_value", None)
            phase_one_completed = phase_one_final_cached or bool(payload.get("stream_terminal_observed"))
            if not phase_one_completed:
                if isinstance(resume_value, str) and resume_value:
                    remaining = max(1.0, total_timeout - (time.monotonic() - started))
                    try:
                        if self._curl_ws_second_leg_enabled():
                            resume_payload = self._resume_via_curl_ws_second_leg(
                                conversation_id=result_conversation_id.strip(),
                                resume_value=resume_value,
                                timeout=remaining,
                                relay_text_event=make_stream_relay(),
                                text=text,
                                baseline_current_node=baseline_current_node,
                            )
                        elif self._shared_resume_enabled():
                            resume_payload = self._get_shared_resume_broker().resume(
                                conversation_id=result_conversation_id.strip(),
                                resume_token=resume_value,
                                offset=0,
                                timeout=remaining,
                                on_text_event=make_stream_relay(),
                            )
                        else:
                            resume_command = [
                                str(self._ensure_helper()),
                                "--resume-conversation",
                                result_conversation_id.strip(),
                                "--resume-offset",
                                "0",
                                "--observe-stream",
                                "--timeout",
                                f"{remaining:.3f}",
                            ]
                            resume_payload = self._run_helper_streaming(
                                resume_command,
                                timeout=remaining,
                                on_text_event=make_stream_relay(),
                                extra_env={"CWA_WK_RESUME_VALUE": resume_value},
                            )
                        encoded_final = resume_payload.get("canonical_body_base64")
                        if isinstance(encoded_final, str) and encoded_final:
                            final_payload = self._decode_helper_json(
                                {
                                    "status": resume_payload.get("status", 200),
                                    "body_base64": encoded_final,
                                },
                                request_stage="wkwebview_resume_finality",
                                error_prefix="WKWEBVIEW_RESUME_CANONICAL",
                            )
                            if self._canonical_payload_matches_write(
                                final_payload,
                                text=text,
                                baseline_current_node=baseline_current_node,
                            ):
                                self._cache_final_payload(result_conversation_id.strip(), final_payload)
                            else:
                                passive_observer_armed = True
                    except RequestError:
                        passive_observer_armed = True
                else:
                    passive_observer_armed = True

        elapsed_ms = payload.get("elapsed_ms")
        if not isinstance(elapsed_ms, int):
            elapsed_ms = int((time.monotonic() - started) * 1000)
        return BrowserNativeTurnResult(
            conversation_id=result_conversation_id.strip(),
            turn_exchange_id=(
                payload.get("turn_exchange_id")
                if isinstance(payload.get("turn_exchange_id"), str)
                else None
            ),
            response_status=response_status,
            response_mime_type="text/event-stream",
            final_url=payload.get("final_url") if isinstance(payload.get("final_url"), str) else None,
            tab_id=None,
            tab_was_active=False,
            elapsed_ms=elapsed_ms,
            runtime_reloaded=True,
            runtime_reload_ms=payload.get("load_elapsed_ms")
            if isinstance(payload.get("load_elapsed_ms"), int)
            else None,
            runtime_tab_preexisting=False,
            runtime_tab_created_for_turn=True,
            tab_active_after=False,
            tab_activated_during_turn=False,
            foreground_activation_observed=False,
            browser_authority_lease_id=self._current_browser_authority_lease_id(),
            attachment_count=attachment_count,
            passive_observer_armed=passive_observer_armed,
        )

    def send_text(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str | None = None,
        timeout: float | None = None,
        attachment_paths: Sequence[str | Path] | None = None,
        model_slug: str | None = None,
    ) -> BrowserNativeTurnResult:
        return self._send_text_impl(
            text,
            conversation=conversation,
            timeout=timeout,
            attachment_paths=attachment_paths,
            model_slug=model_slug,
        )

    def send_text_streaming(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str | None = None,
        timeout: float | None = None,
        attachment_paths: Sequence[str | Path] | None = None,
        model_slug: str | None = None,
        on_text_event: Any,
        on_write_identity: Any = None,
    ) -> BrowserNativeTurnResult:
        return self._send_text_impl(
            text,
            conversation=conversation,
            timeout=timeout,
            attachment_paths=attachment_paths,
            model_slug=model_slug,
            on_text_event=on_text_event,
            on_write_identity=on_write_identity,
        )

    def send_text_with_stale_ui_recovery(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str,
        timeout: float | None = None,
        canonical_completed_at_ms: int,
        attachment_paths: Sequence[str | Path] | None = None,
        model_slug: str | None = None,
    ) -> BrowserNativeTurnResult:
        # Every WKWebView turn starts from a fresh route, so there is no retained
        # stale composer state to repair. The canonical completion proof is still
        # owned by the caller and this remains a single write attempt.
        if isinstance(canonical_completed_at_ms, bool) or canonical_completed_at_ms <= 0:
            raise ValueError("canonical_completed_at_ms must be a positive integer")
        return self.send_text(
            text,
            conversation=conversation,
            timeout=timeout,
            attachment_paths=attachment_paths,
            model_slug=model_slug,
        )

    def send_text_with_stale_ui_recovery_streaming(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str,
        timeout: float | None = None,
        canonical_completed_at_ms: int,
        attachment_paths: Sequence[str | Path] | None = None,
        model_slug: str | None = None,
        on_text_event: Any,
        on_write_identity: Any = None,
    ) -> BrowserNativeTurnResult:
        if isinstance(canonical_completed_at_ms, bool) or canonical_completed_at_ms <= 0:
            raise ValueError("canonical_completed_at_ms must be a positive integer")
        return self.send_text_streaming(
            text,
            conversation=conversation,
            timeout=timeout,
            attachment_paths=attachment_paths,
            model_slug=model_slug,
            on_text_event=on_text_event,
            on_write_identity=on_write_identity,
        )

    def stop_generation(
        self,
        conversation_id: str | None = None,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if conversation_id is None:
            raise ValueError("WKWebView stop_generation requires a conversation id")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be a non-empty string")
        total_timeout = float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")
        conversation_id = conversation_id.strip()
        started = time.monotonic()
        command = self._helper_command(
            conversation_id=conversation_id,
            text=None,
            timeout=total_timeout,
            stop_only=True,
        )
        payload = self._run_helper(command, timeout=total_timeout)
        if payload.get("stop_requested") is not True:
            raise RequestError(
                "WKWEBVIEW_STOP_REQUEST_NOT_PROVEN",
                request_stage="wkwebview_stop_generation",
            )

        remaining = max(0.0, total_timeout - (time.monotonic() - started))
        final_payload = self._wait_for_stopped_final_payload(
            conversation_id,
            timeout=min(8.0, remaining),
        )
        if final_payload is None:
            remaining = max(0.0, total_timeout - (time.monotonic() - started))
            final_payload = self._wait_for_canonical_stop_proof(
                conversation_id,
                timeout=remaining,
            )
        if final_payload is None or not self._is_client_stopped_payload(final_payload):
            raise RequestError(
                "WKWEBVIEW_STOP_CANONICAL_NOT_PROVEN",
                request_stage="wkwebview_stop_generation",
            )

        self._mark_conversation_stopped(conversation_id)
        return {
            "ok": True,
            "stopped": True,
            "conversationId": conversation_id,
            "provider": "wkwebview",
        }

    def release_runtime_tab(
        self,
        *,
        expected_runtime_tab_id: int | None,
        browser_authority_lease_id: str,
        timeout: float = 10.0,
    ) -> BrowserNativeRuntimeTabReleaseResult:
        if not isinstance(browser_authority_lease_id, str) or not browser_authority_lease_id.strip():
            raise ValueError("browser_authority_lease_id is required")
        # One-shot helper processes are already gone when the write result is
        # returned. Report an already-absent authority to the existing lease model.
        return BrowserNativeRuntimeTabReleaseResult(
            released=False,
            already_absent=True,
            runtime_tab_id=None,
            browser_authority_lease_id=browser_authority_lease_id.strip(),
        )

    def _mark_conversation_stopped(self, conversation_id: str) -> None:
        with self._stopped_lock:
            self._stopped_conversations.add(conversation_id)

    def stop_requested_for(self, conversation_id: str) -> bool:
        with self._stopped_lock:
            return conversation_id in self._stopped_conversations

    def clear_stop_requested_for(self, conversation_id: str) -> None:
        with self._stopped_lock:
            self._stopped_conversations.discard(conversation_id)
        with self._stop_final_condition:
            self._stopped_final_payloads.pop(conversation_id, None)


__all__ = ["WKWebViewTurnProvider"]
