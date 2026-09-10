from __future__ import annotations

import base64
import fcntl
import json
import os
import subprocess
import threading
import time
from contextlib import contextmanager
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
from .wkwebview_helper_runtime import (
    WKHelperInvocation as _WKHelperInvocation,
)
from .wkwebview_helper_runtime import (
    WKWebViewHelperRuntime,
)
from .wkwebview_lightweight_transport import WKLightweightTransport

_EVENT_PREFIX = "WK_EVENT "


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
        self._helper_runtime = WKWebViewHelperRuntime(
            self.state_dir, build_timeout=self.build_timeout
        )
        self._profile_context = threading.local()
        self._authority_context = threading.local()
        self._canonical_context = threading.local()
        self._stopped_lock = threading.Lock()
        self._stopped_conversations: set[str] = set()
        self._final_payload_lock = threading.Lock()
        self._final_payload_cache: dict[str, dict[str, Any]] = {}
        self._stop_final_condition = threading.Condition()
        self._stopped_final_payloads: dict[str, dict[str, Any]] = {}
        self._lightweight_transport: WKLightweightTransport | None = None

    @property
    def helper_root(self) -> Path:
        return self._helper_runtime.helper_root

    @property
    def helper_app(self) -> Path:
        return self._helper_runtime.helper_app

    @property
    def helper_binary(self) -> Path:
        return self._helper_runtime.helper_binary

    def _source_paths(self) -> tuple[Path, Path, Path]:
        return self._helper_runtime.source_paths()

    @staticmethod
    def _source_digest(source: Path, plist: Path, minimal_shell: Path) -> str:
        return WKWebViewHelperRuntime.source_digest(source, plist, minimal_shell)

    def _ensure_helper(self) -> Path:
        return self._helper_runtime.ensure_helper()

    def _run_build(self, command: list[str]) -> None:
        self._helper_runtime.run_build(command)

    @staticmethod
    def _curl_ws_second_leg_enabled() -> bool:
        value = os.environ.get("CWA_WK_CURL_WS_SECOND_LEG", "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    @staticmethod
    def _minimal_security_shell_enabled() -> bool:
        value = os.environ.get("CWA_WK_MINIMAL_SECURITY_SHELL", "").strip().lower()
        return value in {"1", "true", "yes", "on"}

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

        self._lightweight_transport = WKLightweightTransport(
            source_client,
            canonical_matches_write=self._canonical_payload_matches_write,
            cache_final_payload=self._cache_final_payload,
        )
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
        transport = self._lightweight_transport
        if transport is None:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_SOURCE_CLIENT_MISSING",
                request_stage="wkwebview_curl_ws_second_leg",
            )
        return transport.resume_turn(
            conversation_id=conversation_id,
            resume_value=resume_value,
            timeout=timeout,
            relay_text_event=relay_text_event,
            text=text,
            baseline_current_node=baseline_current_node,
        )

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
        invocation = _WKHelperInvocation(
            command=[str(binary), "--timeout", f"{total_timeout:.3f}"],
            request={
                "observe_conversation": conversation_id,
                "poll_interval": 3.0,
                "timeout": total_timeout,
            },
        )
        command, request_input = self._helper_subprocess_input(invocation)
        env = os.environ.copy()
        env.setdefault("NSUnbufferedIO", "YES")
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
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
        assert process.stdin is not None
        try:
            process.stdin.write(request_input or "{}")
            process.stdin.close()
        except OSError:
            pass

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

    def _read_conversation_payload_via_curl(
        self,
        conversation_id: str,
        *,
        timeout: float,
    ) -> dict[str, Any] | None:
        transport = self._lightweight_transport
        if transport is None:
            return None
        return transport.read_canonical(conversation_id, timeout=timeout)

    def _read_conversation_payload_uncached(
        self,
        conversation_id: str,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        if self._curl_ws_second_leg_enabled():
            payload = self._read_conversation_payload_via_curl(
                conversation_id,
                timeout=timeout,
            )
            if isinstance(payload, dict):
                return payload

        binary = self._ensure_helper()
        payload = self._run_helper(
            _WKHelperInvocation(
                command=[str(binary), "--timeout", f"{timeout:.3f}"],
                request={
                    "canonical_conversation": conversation_id,
                    "timeout": float(timeout),
                },
            ),
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
        transport = self._lightweight_transport
        if transport is None:
            return None
        return transport.upload_attachments(attachment_paths)

    def _helper_command(
        self,
        *,
        conversation_id: str | None,
        text: str | None,
        timeout: float,
        attachment_paths: Sequence[str] = (),
        expected_current_node: str | None = None,
        stop_only: bool = False,
    ) -> _WKHelperInvocation:
        binary = self._ensure_helper()
        url = (
            f"https://chatgpt.com/c/{conversation_id}"
            if conversation_id
            else "https://chatgpt.com/"
        )
        request: dict[str, Any] = {
            "url": url,
            "timeout": float(timeout),
        }
        if text is not None:
            request["prompt"] = text
        if isinstance(expected_current_node, str) and expected_current_node.strip():
            request["expected_current_node"] = expected_current_node.strip()
        if attachment_paths:
            request["attachments"] = list(attachment_paths)
        profile = getattr(self._profile_context, "profile", None)
        if isinstance(profile, str):
            request["profile"] = PROFILE_TO_PRODUCT_MODE[profile]
        if stop_only:
            request["stop_only"] = True
        return _WKHelperInvocation(
            command=[str(binary), "--timeout", f"{timeout:.3f}"],
            request=request,
        )

    @staticmethod
    def _helper_subprocess_input(
        invocation: list[str] | _WKHelperInvocation,
    ) -> tuple[list[str], str | None]:
        return WKWebViewHelperRuntime.subprocess_input(invocation)

    def _run_helper(
        self,
        invocation: list[str] | _WKHelperInvocation,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        return self._helper_runtime.run(invocation, timeout=timeout)

    def _run_helper_streaming(
        self,
        invocation: list[str] | _WKHelperInvocation,
        *,
        timeout: float,
        on_text_event: Any,
        on_lifecycle_event: Any = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._helper_runtime.run_streaming(
            invocation,
            timeout=timeout,
            on_text_event=on_text_event,
            on_lifecycle_event=on_lifecycle_event,
            extra_env=extra_env,
        )

    @staticmethod
    def _terminate_observer_process(process: subprocess.Popen[str]) -> None:
        WKWebViewHelperRuntime.terminate_process(process)

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
        invocation = _WKHelperInvocation(
            command=[str(binary), "--timeout", f"{total_timeout:.3f}"],
            request={
                "observe_conversation": ref.conversation_id,
                "poll_interval": 1.0,
                "timeout": total_timeout,
            },
        )
        command, request_input = self._helper_subprocess_input(invocation)
        env = os.environ.copy()
        env.setdefault("NSUnbufferedIO", "YES")
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
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
        assert process.stdin is not None
        try:
            process.stdin.write(request_input or "{}")
            process.stdin.close()
        except OSError:
            pass

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

    def _resume_via_legacy_wk(
        self,
        *,
        conversation_id: str,
        resume_value: str,
        timeout: float,
        on_text_event: Any,
    ) -> dict[str, Any]:
        """Compatibility fallback for environments without the lightweight second leg."""

        invocation = _WKHelperInvocation(
            command=[
                str(self._ensure_helper()),
                "--observe-stream",
                "--timeout",
                f"{timeout:.3f}",
            ],
            request={
                "resume_conversation": conversation_id,
                "resume_offset": 0,
                "resume_value": resume_value,
                "timeout": timeout,
            },
        )
        return self._run_helper_streaming(
            invocation,
            timeout=timeout,
            on_text_event=on_text_event,
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
        invocation = self._helper_command(
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
            invocation.command += [
                "--observe-submit",
                "--observe-stream",
                "--stream-probe-until-resume-token",
            ]
            invocation.capture_resume = True
            if use_minimal_security_shell:
                invocation.command.append("--minimal-security-shell")
                if conversation_id is not None and minimal_parent_message_id is not None:
                    invocation.request["minimal_conversation_id"] = conversation_id
                    invocation.request["minimal_parent_message_id"] = minimal_parent_message_id
                    if minimal_model_slug is not None:
                        invocation.request["minimal_model_slug"] = minimal_model_slug
                    if minimal_thinking_effort is not None:
                        invocation.request["minimal_thinking_effort"] = minimal_thinking_effort
                if minimal_attachment_descriptors:
                    invocation.request["minimal_attachments"] = list(
                        minimal_attachment_descriptors
                    )
        started = time.monotonic()
        if on_text_event is not None:
            phase_timeout = total_timeout
            if self._curl_ws_second_leg_enabled():
                gate_wait = max(0.001, total_timeout - (time.monotonic() - started))
                with self._heavy_submit_gate(gate_wait):
                    phase_timeout = max(1.0, total_timeout - (time.monotonic() - started))
                    payload = self._run_helper_streaming(
                        invocation,
                        timeout=phase_timeout,
                        on_text_event=make_stream_relay(),
                        on_lifecycle_event=on_write_identity,
                    )
            else:
                payload = self._run_helper_streaming(
                    invocation,
                    timeout=phase_timeout,
                    on_text_event=make_stream_relay(),
                    on_lifecycle_event=on_write_identity,
                )
        else:
            payload = self._run_helper(invocation, timeout=total_timeout)
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
                        else:
                            resume_payload = self._resume_via_legacy_wk(
                                conversation_id=result_conversation_id.strip(),
                                resume_value=resume_value,
                                timeout=remaining,
                                on_text_event=make_stream_relay(),
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
