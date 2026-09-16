from __future__ import annotations

import os
import threading
import time
from typing import Any

from .exceptions import RequestError
from .types import ConversationRef


class WKTemporaryTurnRuntime:
    """Self-contained Temporary Chat transport over minimal WK + lightweight WS.

    The public Temporary lifecycle state machine stays in TemporaryProductWriteRuntime.
    This component owns only WK-specific process-local continuation binding: one opaque
    lifecycle id maps to the Temporary conversation and latest assistant parent.
    """

    PREWRITE_PROOF = "WK_MINIMAL_SUBMIT_HISTORY_AND_TRAINING_DISABLED_TRUE"
    MODE_PROOF_DETAIL = (
        "WKWebView observed the protected conversation POST body with "
        "history_and_training_disabled=true before browser-owned dispatch"
    )
    LIFECYCLE_PROOF_DETAIL = (
        "process-local lifecycle identity is bound to the Temporary conversation and "
        "latest assistant parent; conversation id alone cannot continue it"
    )
    FINALITY_DETAIL = (
        "browser-owned Temporary write resumed through the product WebSocket stream; "
        "ordinary canonical conversation GET is intentionally not used"
    )

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self._lock = threading.Lock()
        self._bindings: dict[str, tuple[str, str]] = {}
        self._leases: dict[str, Any] = {}

    def _release_lifecycle_lease(self, lifecycle_id: str) -> None:
        with self._lock:
            lease = self._leases.pop(lifecycle_id, None)
        close = getattr(lease, "close", None)
        if callable(close):
            close()

    def send(
        self,
        text: str,
        *,
        conversation_id: str | None,
        lifecycle_id: str,
        timeout: float,
        on_event: Any,
        browser_authority_lease_id: str,
    ) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text is required")
        lifecycle_id = lifecycle_id.strip() if isinstance(lifecycle_id, str) else ""
        if not lifecycle_id:
            raise ValueError("temporary lifecycle id is required")
        if not callable(on_event):
            raise TypeError("on_event must be callable")
        total_timeout = float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")
        if (
            self.provider._current_browser_authority_lease_id()
            != browser_authority_lease_id
        ):
            raise RequestError(
                "WKWEBVIEW_TEMPORARY_AUTHORITY_LEASE_MISMATCH",
                request_stage="wkwebview_temporary_preflight",
            )
        transport = self.provider._lightweight_transport
        if transport is None:
            raise RequestError(
                "WKWEBVIEW_TEMPORARY_LIGHTWEIGHT_TRANSPORT_UNAVAILABLE",
                request_stage="wkwebview_temporary_preflight",
            )

        parent_message_id: str | None = None
        fresh_lifecycle = conversation_id is None

        def invalidate_fresh_lifecycle() -> None:
            if fresh_lifecycle:
                self._release_lifecycle_lease(lifecycle_id)

        if conversation_id is not None:
            conversation_id = ConversationRef(conversation_id).conversation_id
            with self._lock:
                binding = self._bindings.get(lifecycle_id)
                lease_present = lifecycle_id in self._leases
            if binding is None or binding[0] != conversation_id or not lease_present:
                raise RequestError(
                    "WKWEBVIEW_TEMPORARY_LIFECYCLE_NOT_LIVE",
                    request_stage="wkwebview_temporary_preflight",
                )
            parent_message_id = binding[1]
        else:
            with self._lock:
                if lifecycle_id in self._bindings or lifecycle_id in self._leases:
                    raise RequestError(
                        "WKWEBVIEW_TEMPORARY_LIFECYCLE_ALREADY_BOUND",
                        request_stage="wkwebview_temporary_preflight",
                    )
            acquire_lease = getattr(
                self.provider._helper_runtime,
                "acquire_temporary_lifecycle",
                None,
            )
            if not callable(acquire_lease):
                raise RequestError(
                    "WKWEBVIEW_TEMPORARY_LIFECYCLE_LEASE_UNAVAILABLE",
                    request_stage="wkwebview_temporary_preflight",
                )
            lease = acquire_lease(
                lifecycle_id,
                timeout=min(10.0, total_timeout),
            )
            with self._lock:
                self._leases[lifecycle_id] = lease

        invocation = self.provider._helper_command(
            conversation_id=conversation_id,
            text=text,
            timeout=total_timeout,
        )
        invocation.command += [
            "--observe-submit",
            "--observe-stream",
            "--observe-stream-until-resume-token",
            "--minimal-security-shell",
        ]
        invocation.capture_resume = True
        invocation.request["url"] = "https://chatgpt.com/?temporary-chat=true"
        invocation.request["minimal_temporary"] = True
        invocation.request["minimal_temporary_lifecycle_id"] = lifecycle_id
        proxy_enabled = os.environ.get("CWA_WK_PROXY_PROTECTED_WRITE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if proxy_enabled:
            source_client = getattr(transport, "source_client", None)
            auth = getattr(source_client, "auth", None)
            cookies = getattr(auth, "cookies", None)
            if not isinstance(cookies, dict) or not cookies:
                invalidate_fresh_lifecycle()
                raise RequestError(
                    "WKWEBVIEW_TEMPORARY_PROXY_COOKIES_MISSING",
                    request_stage="wkwebview_temporary_preflight",
                )
            cookie_parts = [
                f"{key}={value}"
                for key, value in cookies.items()
                if isinstance(key, str)
                and key
                and isinstance(value, str)
                and value
                and "\r" not in key
                and "\n" not in key
                and "\r" not in value
                and "\n" not in value
            ]
            if not cookie_parts:
                invalidate_fresh_lifecycle()
                raise RequestError(
                    "WKWEBVIEW_TEMPORARY_PROXY_COOKIES_MISSING",
                    request_stage="wkwebview_temporary_preflight",
                )
            invocation.request["proxy_protected_write"] = True
            invocation.request["proxy_cookie_header"] = "; ".join(cookie_parts)
        if conversation_id is not None and parent_message_id is not None:
            invocation.request["minimal_conversation_id"] = conversation_id
            invocation.request["minimal_parent_message_id"] = parent_message_id
        started = time.monotonic()
        try:
            with self.provider._heavy_submit_gate(total_timeout):
                phase_timeout = max(1.0, total_timeout - (time.monotonic() - started))
                payload = self.provider._run_helper_streaming(
                    invocation,
                    timeout=phase_timeout,
                    on_text_event=on_event,
                )
        except Exception:
            invalidate_fresh_lifecycle()
            raise

        result_conversation_id = payload.get("conversation_id")
        if (
            not isinstance(result_conversation_id, str)
            or not result_conversation_id.strip()
        ):
            invalidate_fresh_lifecycle()
            raise RequestError(
                "WKWEBVIEW_TEMPORARY_CONVERSATION_ID_MISSING",
                request_stage="wkwebview_temporary_write",
            )
        result_conversation_id = result_conversation_id.strip()
        if conversation_id is not None and result_conversation_id != conversation_id:
            invalidate_fresh_lifecycle()
            raise RequestError(
                "WKWEBVIEW_TEMPORARY_CONVERSATION_MISMATCH",
                request_stage="wkwebview_temporary_write",
            )
        if payload.get("submit_temporary_mode_observed") is not True:
            invalidate_fresh_lifecycle()
            raise RequestError(
                "WKWEBVIEW_TEMPORARY_MODE_NOT_OBSERVED",
                request_stage="wkwebview_temporary_write",
            )
        terminal_observed = payload.get("stream_terminal_observed") is True
        if terminal_observed:
            assistant_message_id = payload.get("assistant_message_id")
            if not isinstance(assistant_message_id, str) or not assistant_message_id.strip():
                invalidate_fresh_lifecycle()
                raise RequestError(
                    "WKWEBVIEW_TEMPORARY_PARENT_MESSAGE_MISSING",
                    request_stage="wkwebview_temporary_stream",
                )
            streamed = {
                "message_id": assistant_message_id.strip(),
                "turn_exchange_id": payload.get("turn_exchange_id"),
                "finish_reason": "stop",
            }
        else:
            try:
                topic_id = payload.get("stream_topic_id")
                if isinstance(topic_id, str) and topic_id.strip():
                    streamed = transport.follow_topic(
                        conversation_id=result_conversation_id,
                        topic_id=topic_id.strip(),
                        timeout=max(1.0, total_timeout - (time.monotonic() - started)),
                        on_event=on_event,
                    )
                    if streamed.get("stream_finality_proven") is not True:
                        raise RequestError(
                            "WKWEBVIEW_TEMPORARY_STREAM_FINALITY_MISSING",
                            request_stage="wkwebview_temporary_stream",
                        )
                else:
                    resume_value = payload.get("stream_resume_value")
                    if not isinstance(resume_value, str) or not resume_value.strip():
                        raise RequestError(
                            "WKWEBVIEW_TEMPORARY_RESUME_FENCE_MISSING",
                            request_stage="wkwebview_temporary_write",
                        )
                    streamed = transport.stream_temporary_turn(
                        conversation_id=result_conversation_id,
                        resume_value=resume_value.strip(),
                        timeout=max(1.0, total_timeout - (time.monotonic() - started)),
                        relay_text_event=on_event,
                    )
            except Exception:
                invalidate_fresh_lifecycle()
                raise
        message_id = streamed.get("message_id")
        if not isinstance(message_id, str) or not message_id.strip():
            invalidate_fresh_lifecycle()
            raise RequestError(
                "WKWEBVIEW_TEMPORARY_PARENT_MESSAGE_MISSING",
                request_stage="wkwebview_temporary_stream",
            )
        with self._lock:
            self._bindings[lifecycle_id] = (
                result_conversation_id,
                message_id.strip(),
            )

        response_status = payload.get("response_status")
        if not isinstance(response_status, int) or not 200 <= response_status < 300:
            response_status = 200
        return {
            "ok": True,
            "conversationId": result_conversation_id,
            "responseStatus": response_status,
            "conversationMode": "temporary",
            "temporaryModeProven": True,
            "temporaryPrewriteProof": self.PREWRITE_PROOF,
            "temporaryLifecycleToken": lifecycle_id,
            "temporaryLifecycleState": "LIVE",
            "temporaryLiveWriteAuthorityProven": True,
            "temporaryContinuationIdentityProven": conversation_id is not None,
            "temporaryPausedConversationWriteCount": 0,
            "turnExchangeId": streamed.get("turn_exchange_id")
            or payload.get("turn_exchange_id"),
            "tabId": None,
        }

    def end(self, *, lifecycle_id: str, conversation_id: str | None) -> dict[str, Any]:
        lifecycle_id = lifecycle_id.strip() if isinstance(lifecycle_id, str) else ""
        if not lifecycle_id:
            raise ValueError("temporary lifecycle id is required")
        with self._lock:
            binding = self._bindings.get(lifecycle_id)
            if binding is not None and conversation_id is not None:
                if binding[0] != ConversationRef(conversation_id).conversation_id:
                    raise RequestError(
                        "WKWEBVIEW_TEMPORARY_LIFECYCLE_CONVERSATION_MISMATCH",
                        request_stage="temporary_lifecycle_close",
                    )
            self._bindings.pop(lifecycle_id, None)
        self._release_lifecycle_lease(lifecycle_id)
        return {"ok": True, "temporaryLifecycleState": "ENDED"}
