from __future__ import annotations

import base64
import fcntl
import json
import os
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
from .types import ChatConversation, ConversationRef
from .wkwebview_canonical import WKCanonicalState
from .wkwebview_helper_runtime import (
    WKHelperInvocation as _WKHelperInvocation,
)
from .wkwebview_helper_runtime import (
    WKWebViewHelperRuntime,
)
from .wkwebview_lightweight_transport import WKLightweightTransport
from .wkwebview_temporary import WKTemporaryTurnRuntime
from .wkwebview_turn_observer import WKTurnObserver
from .wkwebview_turn_orchestrator import WKTurnOrchestrator


class WKWebViewTurnProvider:
    """macOS browser-owned turn provider backed by a minimal WKWebView app.

    The provider owns the protected product write while the existing CWA runtime
    retains canonical reconciliation, finality, and product semantics.
    """

    browser_authority_backend = "wkwebview"
    streaming_source = "WKWEBVIEW_RESUME_FENCED_PRODUCT_STREAM"
    revision_safe_streaming_supported = True
    supports_attachment_paths = True
    supports_files = True
    supports_multimodal_continuation = True
    supports_model_slug = True
    temporary_chat_supported = True
    temporary_prewrite_proof = WKTemporaryTurnRuntime.PREWRITE_PROOF
    temporary_mode_proof_detail = WKTemporaryTurnRuntime.MODE_PROOF_DETAIL
    temporary_lifecycle_proof_detail = WKTemporaryTurnRuntime.LIFECYCLE_PROOF_DETAIL
    temporary_finality_detail = WKTemporaryTurnRuntime.FINALITY_DETAIL
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
        self._canonical_read_context = threading.local()
        self._canonical_state = WKCanonicalState()
        self._stopped_lock = threading.Lock()
        self._stopped_conversations: set[str] = set()
        self._stop_context_condition = threading.Condition()
        self._pending_stop_contexts: set[str] = set()
        self._stop_contexts: dict[str, tuple[str, str]] = {}
        self._lightweight_transport: WKLightweightTransport | None = None
        self._turn_observer = WKTurnObserver(self)
        self._turn_orchestrator = WKTurnOrchestrator(self)
        self._temporary_runtime = WKTemporaryTurnRuntime(self)

    def _ensure_helper(self) -> Path:
        return self._helper_runtime.ensure_helper()

    @staticmethod
    def _lightweight_path_enabled() -> bool:
        value = os.environ.get("CWA_WK_FORCE_LEGACY", "").strip().lower()
        return value not in {"1", "true", "yes", "on"}

    def build_temporary_chat_provider(self) -> WKWebViewTurnProvider:
        return self

    @contextmanager
    def _heavy_submit_gate(self, timeout: float) -> Iterator[int]:
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
            yield int((time.monotonic() - started) * 1000)
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
        except (OSError, RequestError):
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
            stop_requested=self.stop_requested_for,
        )
        return WKWebViewCanonicalClient(source_client, self)

    def _canonical_payload_matches_write(
        self,
        payload: dict[str, Any],
        *,
        text: str,
        baseline_current_node: str | None,
    ) -> bool:
        return self._canonical_state.payload_matches_write(
            payload,
            text=text,
            baseline_current_node=baseline_current_node,
        )

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
        return WKCanonicalState.is_client_stopped_payload(payload)

    def _cache_final_payload(
        self, conversation_id: str, payload: dict[str, Any]
    ) -> None:
        self._canonical_state.cache_final_payload(conversation_id, payload)

    def _wait_for_stopped_final_payload(
        self,
        conversation_id: str,
        *,
        timeout: float,
    ) -> dict[str, Any] | None:
        return self._canonical_state.wait_for_stopped_final_payload(
            conversation_id, timeout=timeout
        )

    def _wait_for_canonical_stop_proof(
        self,
        conversation_id: str,
        *,
        timeout: float,
    ) -> dict[str, Any] | None:
        return self._turn_observer.wait_for_canonical_stop_proof(
            conversation_id,
            timeout=timeout,
        )

    def _wait_for_stream_stop_proof(
        self,
        conversation_id: str,
        *,
        turn_trace_id: str | None,
        timeout: float,
    ) -> str | None:
        transport = self._lightweight_transport
        if transport is None:
            return None
        return transport.wait_for_stop_status(
            conversation_id,
            turn_trace_id=turn_trace_id,
            timeout=timeout,
        )

    def _record_canonical_read_observation(
        self,
        transport: str | None,
        fallback_reason: str | None = None,
    ) -> None:
        self._canonical_read_context.transport = transport
        self._canonical_read_context.fallback_reason = fallback_reason

    def _canonical_read_observation(self) -> tuple[str | None, str | None]:
        transport = getattr(self._canonical_read_context, "transport", None)
        fallback_reason = getattr(self._canonical_read_context, "fallback_reason", None)
        return (
            transport if isinstance(transport, str) and transport else None,
            fallback_reason
            if isinstance(fallback_reason, str) and fallback_reason
            else None,
        )

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
        fallback_reason = None
        if self._lightweight_path_enabled():
            payload = self._read_conversation_payload_via_curl(
                conversation_id,
                timeout=timeout,
            )
            if isinstance(payload, dict):
                self._record_canonical_read_observation("curl_cffi")
                return payload
            transport = self._lightweight_transport
            if transport is not None:
                fallback_reason = transport.take_canonical_fallback_reason()

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
        parsed = self._decode_helper_json(
            payload,
            request_stage="wkwebview_canonical_read",
            error_prefix="WKWEBVIEW_CANONICAL",
        )
        self._record_canonical_read_observation("wkwebview", fallback_reason)
        return parsed

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
        cached_final = self._canonical_state.take_final_payload(ref.conversation_id)
        if cached_final is not None:
            self._record_canonical_read_observation("cache")
            return cached_final
        parsed = self._read_conversation_payload_uncached(
            ref.conversation_id,
            timeout=total_timeout,
        )
        self._canonical_state.set_current_node(
            ref.conversation_id, parsed.get("current_node")
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
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("limit must be an int between 1 and 100")
        total_timeout = float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")
        if self._lightweight_path_enabled():
            transport = self._lightweight_transport
            if transport is not None:
                lightweight_payload = transport.read_catalog(
                    normalized,
                    offset=offset,
                    limit=limit,
                    is_archived=is_archived,
                    is_starred=is_starred,
                    timeout=total_timeout,
                )
                if isinstance(lightweight_payload, dict):
                    self._record_canonical_read_observation("curl_cffi")
                    return lightweight_payload
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
        return self._canonical_state.cached_current_node(conversation_id)

    @staticmethod
    def _current_branch_contains_user_text(payload: dict[str, Any], text: str) -> bool:
        return WKCanonicalState.current_branch_contains_user_text(payload, text)

    def _wait_for_canonical_write_commit(
        self,
        *,
        conversation_id: str,
        text: str,
        baseline_current_node: str | None,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(2.0, min(float(timeout), 30.0))
        retry_delay = 1.0
        while time.monotonic() < deadline:
            try:
                payload = self.read_conversation_payload(
                    conversation_id,
                    timeout=min(10.0, max(1.0, deadline - time.monotonic())),
                )
            except RequestError as error:
                if error.status_code in {401, 403}:
                    raise
                sleep_delay = 60.0 if error.status_code == 429 else retry_delay
            else:
                current_node = payload.get("current_node")
                node_changed = baseline_current_node is None or (
                    isinstance(current_node, str)
                    and current_node
                    and current_node != baseline_current_node
                )
                if node_changed and self._current_branch_contains_user_text(payload, text):
                    return payload
                sleep_delay = retry_delay

            remaining = max(0.0, deadline - time.monotonic())
            if remaining > 0:
                time.sleep(min(sleep_delay, remaining))
            if sleep_delay < 60.0:
                retry_delay = min(retry_delay * 2.0, 8.0)
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
                raise ValueError(
                    f"attachment_paths[{index}] must reference a regular file"
                )
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
        stop_context: tuple[str, str] | None = None,
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
            if stop_context is not None:
                conduit_token, turn_trace_id = stop_context
                if conduit_token:
                    request["stop_context_conduit"] = conduit_token
                if turn_trace_id:
                    request["stop_context_trace"] = turn_trace_id
        return _WKHelperInvocation(
            command=[str(binary), "--timeout", f"{timeout:.3f}"],
            request=request,
        )

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

    def send_temporary_text(
        self,
        text: str,
        *,
        conversation_id: str | None,
        lifecycle_id: str,
        timeout: float,
        on_event: Any,
        browser_authority_lease_id: str,
    ) -> dict[str, Any]:
        return self._temporary_runtime.send(
            text,
            conversation_id=conversation_id,
            lifecycle_id=lifecycle_id,
            timeout=timeout,
            on_event=on_event,
            browser_authority_lease_id=browser_authority_lease_id,
        )

    def end_temporary_lifecycle(
        self,
        *,
        lifecycle_id: str,
        conversation_id: str | None,
    ) -> dict[str, Any]:
        return self._temporary_runtime.end(
            lifecycle_id=lifecycle_id,
            conversation_id=conversation_id,
        )

    def observe_turn(
        self,
        *,
        conversation_id: str,
        turn_exchange_id: str | None,
        browser_authority_lease_id: str,
        timeout: float,
        on_event: Any = None,
    ) -> dict[str, Any]:
        return self._turn_observer.observe_turn(
            conversation_id=conversation_id,
            turn_exchange_id=turn_exchange_id,
            browser_authority_lease_id=browser_authority_lease_id,
            timeout=timeout,
            on_event=on_event,
        )

    def _resume_via_direct_wk(
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
        conversation: ConversationRef
        | ChatConversation
        | dict[str, Any]
        | str
        | None = None,
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
        streaming = on_text_event is not None
        prepared = self._turn_orchestrator.prepare_turn(
            conversation=conversation,
            total_timeout=total_timeout,
            attachment_paths=attachment_paths,
            model_slug=model_slug,
            streaming=streaming,
        )
        invocation = self._turn_orchestrator.build_turn_invocation(
            text=text,
            total_timeout=total_timeout,
            prepared=prepared,
            streaming=streaming,
        )
        make_stream_relay = (
            self._turn_orchestrator.stream_relay_factory(on_text_event)
            if streaming
            else None
        )
        started = time.monotonic()
        identity_conversation_id: str | None = None

        def handle_write_identity(event: dict[str, Any]) -> None:
            nonlocal identity_conversation_id
            conversation_id = (
                event.get("conversation_id") if isinstance(event, dict) else None
            )
            if isinstance(conversation_id, str) and conversation_id.strip():
                identity_conversation_id = conversation_id.strip()
                self._mark_stop_context_pending(identity_conversation_id)
            if on_write_identity is not None:
                on_write_identity(event)

        try:
            payload = self._turn_orchestrator.run_protected_phase_one(
                invocation,
                total_timeout=total_timeout,
                started=started,
                streaming=streaming,
                on_text_event=make_stream_relay()
                if make_stream_relay is not None
                else None,
                on_write_identity=handle_write_identity,
            )
            payload = self._turn_orchestrator.recover_phase_one_identity(
                payload,
                prepared=prepared,
                text=text,
                total_timeout=total_timeout,
                started=started,
            )
            recovered_conversation_id = payload.get("conversation_id")
            if (
                payload.get("_cwa_identity_recovered") is True
                and isinstance(recovered_conversation_id, str)
                and recovered_conversation_id.strip()
                and identity_conversation_id != recovered_conversation_id.strip()
            ):
                handle_write_identity(
                    {
                        "type": "write_identity_resolved",
                        "conversation_id": recovered_conversation_id.strip(),
                        "submit_response_observed": True,
                        "submit_response_status": payload.get("response_status"),
                    }
                )
            phase_one = self._turn_orchestrator.validate_phase_one(
                payload,
                prepared=prepared,
                text=text,
                total_timeout=total_timeout,
            )
        except Exception:
            if identity_conversation_id is not None:
                self._clear_stop_context(identity_conversation_id)
            raise

        try:
            passive_observer_armed = self._turn_orchestrator.resume_after_phase_one(
                payload,
                result_conversation_id=phase_one.conversation_id,
                phase_one_final_cached=phase_one.phase_one_final_cached,
                prepared=prepared,
                text=text,
                total_timeout=total_timeout,
                started=started,
                streaming=streaming,
                make_stream_relay=(
                    make_stream_relay
                    if make_stream_relay is not None
                    else lambda: lambda _event: None
                ),
            )
            return self._turn_orchestrator.build_turn_result(
                payload,
                phase_one=phase_one,
                prepared=prepared,
                started=started,
                passive_observer_armed=passive_observer_armed,
            )
        finally:
            self._clear_stop_context(phase_one.conversation_id)

    def send_text(
        self,
        text: str,
        *,
        conversation: ConversationRef
        | ChatConversation
        | dict[str, Any]
        | str
        | None = None,
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
        conversation: ConversationRef
        | ChatConversation
        | dict[str, Any]
        | str
        | None = None,
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
        if (
            isinstance(canonical_completed_at_ms, bool)
            or canonical_completed_at_ms <= 0
        ):
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
        if (
            isinstance(canonical_completed_at_ms, bool)
            or canonical_completed_at_ms <= 0
        ):
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

    def _mark_stop_context_pending(self, conversation_id: str) -> None:
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            return
        normalized = conversation_id.strip()
        with self._stop_context_condition:
            self._pending_stop_contexts.add(normalized)
            self._stop_context_condition.notify_all()

    def _store_stop_context(
        self,
        conversation_id: str,
        conduit_token: str | None,
        turn_trace_id: str | None,
    ) -> None:
        normalized = conversation_id.strip()
        conduit = conduit_token.strip() if isinstance(conduit_token, str) else ""
        trace = turn_trace_id.strip() if isinstance(turn_trace_id, str) else ""
        with self._stop_context_condition:
            self._pending_stop_contexts.discard(normalized)
            if conduit or trace:
                self._stop_contexts[normalized] = (conduit, trace)
            else:
                self._stop_contexts.pop(normalized, None)
            self._stop_context_condition.notify_all()

    def _wait_for_stop_context(
        self,
        conversation_id: str,
        *,
        timeout: float,
    ) -> tuple[str, str] | None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._stop_context_condition:
            while conversation_id in self._pending_stop_contexts:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._stop_context_condition.wait(timeout=min(0.1, remaining))
            context = self._stop_contexts.get(conversation_id)
            return tuple(context) if context is not None else None

    def _clear_stop_context(self, conversation_id: str) -> None:
        with self._stop_context_condition:
            self._pending_stop_contexts.discard(conversation_id)
            self._stop_contexts.pop(conversation_id, None)
            self._stop_context_condition.notify_all()

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
        normalized_conversation_id = conversation_id.strip()
        total_timeout = float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")
        return self._turn_observer.stop_generation(
            normalized_conversation_id,
            timeout=total_timeout,
        )

    def release_runtime_tab(
        self,
        *,
        expected_runtime_tab_id: int | None,
        browser_authority_lease_id: str,
        timeout: float = 10.0,
    ) -> BrowserNativeRuntimeTabReleaseResult:
        if (
            not isinstance(browser_authority_lease_id, str)
            or not browser_authority_lease_id.strip()
        ):
            raise ValueError("browser_authority_lease_id is required")
        lease_id = browser_authority_lease_id.strip()
        # One-shot helper processes are already gone when the write result is
        # returned. Report an already-absent authority to the existing lease model.
        return BrowserNativeRuntimeTabReleaseResult(
            released=False,
            already_absent=True,
            runtime_tab_id=None,
            browser_authority_lease_id=lease_id,
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
        self._canonical_state.clear_stopped_final_payload(conversation_id)


__all__ = ["WKWebViewTurnProvider"]
