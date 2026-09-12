from __future__ import annotations

import time
from typing import Any

from .exceptions import RequestError
from .status import _status_from_payload
from .types import ConversationRef
from .wkwebview_helper_runtime import WKHelperInvocation

_CANONICAL_OBSERVER_POLL_INTERVAL_SECONDS = 15.0


class WKTurnObserver:
    """Own passive observation and Stop lifecycle for the WK turn provider."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def wait_for_canonical_stop_proof(
        self,
        conversation_id: str,
        *,
        timeout: float,
    ) -> dict[str, Any] | None:
        provider = self.provider
        total_timeout = max(0.0, timeout)
        if total_timeout <= 0:
            return None
        invocation = WKHelperInvocation(
            command=[
                str(provider._ensure_helper()),
                "--timeout",
                f"{total_timeout:.3f}",
            ],
            request={
                "observe_conversation": conversation_id,
                "poll_interval": 3.0,
                "timeout": total_timeout,
            },
        )

        def cached_stop() -> dict[str, Any] | None:
            return provider._canonical_state.stopped_final_payload(conversation_id)

        def handle_event(raw_event: dict[str, Any]) -> dict[str, Any] | None:
            if raw_event.get("type") != "canonical_payload":
                return None
            try:
                payload = provider._decode_helper_json(
                    raw_event,
                    request_stage="wkwebview_stop_generation",
                    error_prefix="WKWEBVIEW_STOP_CANONICAL",
                )
            except RequestError as error:
                if error.status_code is None or error.status_code in {401, 403}:
                    raise
                return None
            if not provider._is_client_stopped_payload(payload):
                return None
            provider._cache_final_payload(conversation_id, payload)
            return payload

        return provider._helper_runtime.run_event_observer(
            invocation,
            timeout=total_timeout,
            on_event=handle_event,
            on_tick=cached_stop,
            request_stage="wkwebview_stop_generation",
            launch_error_prefix="WKWEBVIEW_STOP_OBSERVER_LAUNCH_FAILED",
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
        provider = self.provider
        ref = ConversationRef(conversation_id)
        if (
            not isinstance(browser_authority_lease_id, str)
            or not browser_authority_lease_id.strip()
        ):
            raise ValueError("browser_authority_lease_id is required")
        total_timeout = float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")

        invocation = WKHelperInvocation(
            command=[
                str(provider._ensure_helper()),
                "--timeout",
                f"{total_timeout:.3f}",
            ],
            request={
                "observe_conversation": ref.conversation_id,
                "poll_interval": _CANONICAL_OBSERVER_POLL_INTERVAL_SECONDS,
                "timeout": total_timeout,
            },
        )
        last_message_id: str | None = None
        last_finish_reason: str | None = None

        def stop_result() -> dict[str, Any] | None:
            if not provider.stop_requested_for(ref.conversation_id):
                return None
            return {
                "ok": True,
                "conversationId": ref.conversation_id,
                "turnExchangeId": turn_exchange_id,
                "messageId": last_message_id,
                "finishReason": "stopped",
            }

        def handle_event(raw_event: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal last_message_id, last_finish_reason
            if raw_event.get("type") != "canonical_payload":
                return None
            try:
                payload = provider._decode_helper_json(
                    raw_event,
                    request_stage="browser_native_observe_turn",
                    error_prefix="WKWEBVIEW_CANONICAL_OBSERVER",
                )
            except RequestError as error:
                if error.status_code is None or error.status_code in {401, 403}:
                    raise
                return None

            provider._canonical_state.set_current_node(
                ref.conversation_id, payload.get("current_node")
            )

            if on_event is not None:
                try:
                    on_event({"type": "canonical_payload_snapshot", "payload": payload})
                except Exception:
                    # Observer callbacks are best-effort telemetry; consumer failures
                    # must not suppress canonical finality or Stop reconciliation.
                    pass

            status = _status_from_payload(payload)
            last_message_id = status.message_id or last_message_id
            last_finish_reason = status.finish_reason or last_finish_reason
            if status.status != "completed":
                return None
            return {
                "ok": True,
                "conversationId": ref.conversation_id,
                "turnExchangeId": turn_exchange_id,
                "messageId": last_message_id,
                "finishReason": last_finish_reason or "stop",
            }

        result = provider._helper_runtime.run_event_observer(
            invocation,
            timeout=total_timeout,
            on_event=handle_event,
            on_tick=stop_result,
            request_stage="browser_native_observe_turn",
            launch_error_prefix="WKWEBVIEW_CANONICAL_OBSERVER_LAUNCH_FAILED",
        )
        if result is not None:
            return result
        raise RequestError(
            "PASSIVE_OBSERVER_STREAM_ENDED_WITHOUT_TERMINAL",
            request_stage="browser_native_observe_turn",
        )

    def stop_generation(
        self,
        conversation_id: str,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        provider = self.provider
        started = time.monotonic()
        stop_context = provider._wait_for_stop_context(
            conversation_id,
            timeout=min(8.0, timeout),
        )
        remaining = max(0.1, timeout - (time.monotonic() - started))
        command = provider._helper_command(
            conversation_id=conversation_id,
            text=None,
            timeout=remaining,
            stop_only=True,
            stop_context=stop_context,
        )
        payload = provider._run_helper(command, timeout=remaining)
        if payload.get("stop_requested") is not True:
            raise RequestError(
                "WKWEBVIEW_STOP_REQUEST_NOT_PROVEN",
                request_stage="wkwebview_stop_generation",
            )

        final_payload = provider._wait_for_stopped_final_payload(
            conversation_id,
            timeout=0.0,
        )
        proof_kind = None
        stream_status = None
        if final_payload is not None and provider._is_client_stopped_payload(
            final_payload
        ):
            proof_kind = "canonical_client_stopped"

        if proof_kind is None and stop_context is not None and any(stop_context):
            remaining = max(0.0, timeout - (time.monotonic() - started))
            stream_status = provider._wait_for_stream_stop_proof(
                conversation_id,
                turn_trace_id=stop_context[1] or None,
                timeout=min(8.0, remaining),
            )
            if stream_status in {"IS_STOP_REQUESTED", "COMPLETE"}:
                proof_kind = "stream_status"

        if proof_kind is None:
            remaining = max(0.0, timeout - (time.monotonic() - started))
            final_payload = provider._wait_for_canonical_stop_proof(
                conversation_id,
                timeout=remaining,
            )
            if final_payload is not None and provider._is_client_stopped_payload(
                final_payload
            ):
                proof_kind = "canonical_client_stopped"

        if proof_kind is None:
            raise RequestError(
                "WKWEBVIEW_STOP_CANONICAL_NOT_PROVEN",
                request_stage="wkwebview_stop_generation",
            )

        provider._mark_conversation_stopped(conversation_id)
        provider._clear_stop_context(conversation_id)
        return {
            "ok": True,
            "stopped": True,
            "conversationId": conversation_id,
            "provider": "wkwebview",
            "proof": proof_kind,
            "streamStatus": stream_status,
        }
