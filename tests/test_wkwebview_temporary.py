from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from chatgpt_web_adapter.exceptions import RequestError
from chatgpt_web_adapter.wkwebview_helper_runtime import WKHelperInvocation
from chatgpt_web_adapter.wkwebview_temporary import WKTemporaryTurnRuntime


class _TemporaryStreamTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.index = 0

    def stream_temporary_turn(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        self.index += 1
        message_id = f"assistant-{self.index}"
        kwargs["relay_text_event"](
            {
                "type": "assistant_text_delta",
                "sequence": 1,
                "message_id": message_id,
                "delta": f"temporary-{self.index}",
                "finish_reason": "stop",
            }
        )
        return {
            "ok": True,
            "conversation_id": kwargs["conversation_id"],
            "message_id": message_id,
            "parent_message_id": message_id,
            "finish_reason": "stop",
            "turn_exchange_id": f"turn-{self.index}",
            "ws_token_events": 1,
        }


class _EarlyHandoffTemporaryTransport(_TemporaryStreamTransport):
    def __init__(self) -> None:
        super().__init__()
        self.arm_calls: list[str] = []
        self.release_calls: list[str] = []
        self.follow_calls: list[dict[str, Any]] = []

    def arm_conversation_completion(
        self,
        conversation_id: str,
        *,
        timeout: float,
    ) -> int:
        assert timeout > 0
        self.arm_calls.append(f"completion:{conversation_id}")
        return 0

    def arm_early_handoff(self, conversation_id: str) -> str:
        self.arm_calls.append(conversation_id)
        return "attempt-temporary-1"

    def early_handoff_control(self, attempt_id: str) -> dict[str, str]:
        assert attempt_id == "attempt-temporary-1"
        return {
            "conversation_id": "temporary-1",
            "topic_id": "conversation-turn-turn-2",
            "turn_exchange_id": "turn-2",
        }

    def release_early_handoff(self, attempt_id: str) -> None:
        self.release_calls.append(attempt_id)

    def follow_topic(self, **kwargs: Any) -> dict[str, Any]:
        self.follow_calls.append(dict(kwargs))
        kwargs["on_event"](
            {
                "type": "assistant_text_delta",
                "sequence": 1,
                "message_id": "assistant-2",
                "delta": "temporary-2",
                "finish_reason": "stop",
            }
        )
        return {
            "ok": True,
            "conversation_id": kwargs["conversation_id"],
            "topic_id": kwargs["topic_id"],
            "message_id": "assistant-2",
            "turn_exchange_id": "turn-2",
            "finish_reason": "stop",
            "stream_finality_proven": True,
            "completed": True,
        }


class _LifecycleLease:
    def __init__(self, lifecycle_id: str) -> None:
        self.lifecycle_id = lifecycle_id
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _HelperRuntime:
    def __init__(self) -> None:
        self.leases: list[_LifecycleLease] = []

    def acquire_temporary_lifecycle(
        self,
        lifecycle_id: str,
        *,
        timeout: float,
    ) -> _LifecycleLease:
        assert timeout > 0
        lease = _LifecycleLease(lifecycle_id)
        self.leases.append(lease)
        return lease


class _Provider:
    def __init__(self) -> None:
        self._lightweight_transport = _TemporaryStreamTransport()
        self._helper_runtime = _HelperRuntime()
        self.invocations: list[WKHelperInvocation] = []
        self.current_lease = "lease-1"
        self.temporary_mode_observed = True

    def _current_browser_authority_lease_id(self) -> str:
        return self.current_lease

    def _helper_command(
        self,
        *,
        conversation_id: str | None,
        text: str,
        timeout: float,
    ) -> WKHelperInvocation:
        return WKHelperInvocation(
            command=["wk-helper", "--timeout", str(timeout)],
            request={
                "conversation": conversation_id,
                "prompt": text,
                "timeout": timeout,
            },
        )

    @contextmanager
    def _heavy_submit_gate(self, timeout: float) -> Iterator[int]:
        del timeout
        yield 0

    def _run_helper_streaming(
        self,
        invocation: WKHelperInvocation,
        *,
        timeout: float,
        on_text_event: Any,
        external_completion_check: Any = None,
    ) -> dict[str, Any]:
        del timeout, on_text_event
        self.invocations.append(invocation)
        conversation_id = (
            invocation.request.get("minimal_conversation_id") or "temporary-1"
        )
        if callable(external_completion_check):
            signal = external_completion_check()
            if isinstance(signal, dict) and signal.get("kind") == "early_handoff":
                return {
                    "ok": True,
                    "conversation_id": conversation_id,
                    "response_status": 200,
                    "submit_temporary_mode_observed": self.temporary_mode_observed,
                    "stream_topic_id": signal["topic_id"],
                    "turn_exchange_id": signal["turn_exchange_id"],
                    "_cwa_early_handoff_observed": True,
                }
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "response_status": 200,
            "submit_temporary_mode_observed": self.temporary_mode_observed,
            "stream_resume_value": "opaque-resume",
            "turn_exchange_id": "phase-a-turn",
        }


def test_wk_temporary_new_continuation_and_end_are_process_bound() -> None:
    provider = _Provider()
    runtime = WKTemporaryTurnRuntime(provider)
    events: list[dict[str, Any]] = []

    first = runtime.send(
        "first",
        conversation_id=None,
        lifecycle_id="lifecycle-1",
        timeout=30,
        on_event=events.append,
        browser_authority_lease_id="lease-1",
    )
    second = runtime.send(
        "second",
        conversation_id="temporary-1",
        lifecycle_id="lifecycle-1",
        timeout=30,
        on_event=events.append,
        browser_authority_lease_id="lease-1",
    )

    assert first["conversationId"] == "temporary-1"
    assert first["temporaryModeProven"] is True
    assert first["temporaryContinuationIdentityProven"] is False
    assert second["conversationId"] == "temporary-1"
    assert second["temporaryContinuationIdentityProven"] is True
    assert provider.invocations[0].request["minimal_temporary"] is True
    assert provider.invocations[0].request["minimal_temporary_lifecycle_id"] == "lifecycle-1"
    assert provider.invocations[0].request["url"] == "https://chatgpt.com/?temporary-chat=true"
    assert "minimal_conversation_id" not in provider.invocations[0].request
    assert provider.invocations[1].request["minimal_conversation_id"] == "temporary-1"
    assert provider.invocations[1].request["minimal_parent_message_id"] == "assistant-1"
    assert provider.invocations[1].request["minimal_temporary_lifecycle_id"] == "lifecycle-1"
    assert [event["delta"] for event in events] == ["temporary-1", "temporary-2"]
    assert len(provider._helper_runtime.leases) == 1
    lifecycle_lease = provider._helper_runtime.leases[0]
    assert lifecycle_lease.closed is False

    assert runtime.end(
        lifecycle_id="lifecycle-1",
        conversation_id="temporary-1",
    ) == {"ok": True, "temporaryLifecycleState": "ENDED"}
    assert lifecycle_lease.closed is True
    with pytest.raises(RequestError, match="WKWEBVIEW_TEMPORARY_LIFECYCLE_NOT_LIVE"):
        runtime.send(
            "third",
            conversation_id="temporary-1",
            lifecycle_id="lifecycle-1",
            timeout=30,
            on_event=events.append,
            browser_authority_lease_id="lease-1",
        )

def test_wk_temporary_does_not_arm_global_completion_or_early_handoff() -> None:
    provider = _Provider()
    transport = _EarlyHandoffTemporaryTransport()
    provider._lightweight_transport = transport
    runtime = WKTemporaryTurnRuntime(provider)
    events: list[dict[str, Any]] = []

    first = runtime.send(
        "first",
        conversation_id=None,
        lifecycle_id="lifecycle-no-global-observer",
        timeout=30,
        on_event=events.append,
        browser_authority_lease_id="lease-1",
    )
    second = runtime.send(
        "second",
        conversation_id=first["conversationId"],
        lifecycle_id="lifecycle-no-global-observer",
        timeout=30,
        on_event=events.append,
        browser_authority_lease_id="lease-1",
    )

    assert second["conversationId"] == "temporary-1"
    assert second["turnExchangeId"] == "turn-2"
    assert "minimal_handoff_attempt_id" not in provider.invocations[0].request
    assert "minimal_handoff_attempt_id" not in provider.invocations[1].request
    assert transport.arm_calls == []
    assert transport.release_calls == []
    assert transport.follow_calls == []
    assert len(transport.calls) == 2
    assert [event["delta"] for event in events] == ["temporary-1", "temporary-2"]


def test_wk_temporary_broker_terminal_uses_browser_sse_without_resume_leg() -> None:
    provider = _Provider()
    runtime = WKTemporaryTurnRuntime(provider)
    events: list[dict[str, Any]] = []

    def terminal_run(
        invocation: WKHelperInvocation,
        *,
        timeout: float,
        on_text_event: Any,
    ) -> dict[str, Any]:
        assert timeout > 0
        provider.invocations.append(invocation)
        on_text_event(
            {
                "type": "assistant_text_snapshot",
                "sequence": 1,
                "message_id": "assistant-direct",
                "text": "temporary-direct",
            }
        )
        return {
            "ok": True,
            "conversation_id": "temporary-direct-1",
            "response_status": 200,
            "submit_temporary_mode_observed": True,
            "stream_terminal_observed": True,
            "assistant_message_id": "assistant-direct",
            "turn_exchange_id": "turn-direct",
        }

    provider._run_helper_streaming = terminal_run  # type: ignore[method-assign]
    result = runtime.send(
        "first",
        conversation_id=None,
        lifecycle_id="lifecycle-direct",
        timeout=30,
        on_event=events.append,
        browser_authority_lease_id="lease-1",
    )

    assert result["conversationId"] == "temporary-direct-1"
    assert result["turnExchangeId"] == "turn-direct"
    assert provider._lightweight_transport.calls == []
    assert events == [
        {
            "type": "assistant_text_snapshot",
            "sequence": 1,
            "message_id": "assistant-direct",
            "text": "temporary-direct",
        }
    ]
    assert runtime._bindings["lifecycle-direct"] == (
        "temporary-direct-1",
        "assistant-direct",
    )
    assert runtime.end(
        lifecycle_id="lifecycle-direct",
        conversation_id="temporary-direct-1",
    ) == {"ok": True, "temporaryLifecycleState": "ENDED"}


def test_wk_temporary_requires_browser_observed_mode_proof() -> None:
    provider = _Provider()
    provider.temporary_mode_observed = False
    runtime = WKTemporaryTurnRuntime(provider)

    with pytest.raises(RequestError, match="WKWEBVIEW_TEMPORARY_MODE_NOT_OBSERVED"):
        runtime.send(
            "first",
            conversation_id=None,
            lifecycle_id="lifecycle-1",
            timeout=30,
            on_event=lambda _event: None,
            browser_authority_lease_id="lease-1",
        )


def test_packaged_wk_helper_proves_temporary_mode_from_observed_submit_body() -> None:
    root = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "chatgpt_web_adapter"
        / "wkwebview_helper"
    )
    shell = (root / "minimal_security_shell.js").read_text(encoding="utf-8")
    helper = (root / "WKChatGPTAuthority.m").read_text(encoding="utf-8")

    assert "history_and_training_disabled: true" in shell
    assert "history_and_training_disabled=${temporary" in shell
    assert "temporary_mode:temporary" in helper
    assert "submit_temporary_mode_observed" in helper


def test_packaged_wk_helper_uses_native_trusted_send_click_without_double_flipping_y() -> None:
    root = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "chatgpt_web_adapter"
        / "wkwebview_helper"
    )
    helper = (root / "WKChatGPTAuthority.m").read_text(encoding="utf-8")

    assert "static NSDictionary *NativeClickSendButton(WKWebView *webView)" in helper
    assert "NSEventTypeLeftMouseDown" in helper
    assert "NSEventTypeLeftMouseUp" in helper
    assert "cssY * scaleY" in helper
    assert "[webView convertPoint:localPoint toView:nil]" in helper
    assert "bounds.size.height - (cssY * scaleY)" not in helper
    assert "static NSString *SendScript(void)" not in helper
    assert '@"strategy": @"native_send_button_click"' in helper
