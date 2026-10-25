from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from chatgpt_web_adapter.exceptions import RequestError
from chatgpt_web_adapter.wkwebview_helper_runtime import WKHelperInvocation
from chatgpt_web_adapter.wkwebview_provider import WKWebViewTurnProvider
from chatgpt_web_adapter.wkwebview_temporary import WKTemporaryTurnRuntime


class _TemporaryStreamTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.index = 0
        self.source_client = SimpleNamespace(
            auth=SimpleNamespace(cookies={"session": "test-cookie"})
        )

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
            "stream_finality_proven": True,
            "ws_token_events": 1,
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

    def _configure_protected_write_proxy(self, request, **kwargs):
        return WKWebViewTurnProvider._configure_protected_write_proxy(
            self, request, **kwargs
        )

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
        on_transport_event: Any = None,
        external_completion_check: Any = None,
    ) -> dict[str, Any]:
        del timeout, on_text_event
        self.invocations.append(invocation)
        if callable(on_transport_event):
            on_transport_event({"type": "submit_response_observed", "status": 200})
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


def test_wk_temporary_new_continuation_and_end_are_process_bound(monkeypatch) -> None:
    monkeypatch.delenv("CWA_WK_PROXY_PROTECTED_WRITE", raising=False)
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
    assert first["terminalObserved"] is True
    assert first["terminalSource"] == "wkwebview_temporary_stream"
    assert first["messageId"] == "assistant-1"
    assert first["temporaryModeProven"] is True
    assert first["temporaryContinuationIdentityProven"] is False
    assert second["conversationId"] == "temporary-1"
    assert second["terminalObserved"] is True
    assert second["messageId"] == "assistant-2"
    assert second["temporaryContinuationIdentityProven"] is True
    assert provider.invocations[0].request["minimal_temporary"] is True
    assert (
        provider.invocations[0].request["minimal_temporary_lifecycle_id"]
        == "lifecycle-1"
    )
    assert (
        provider.invocations[0].request["url"]
        == "https://chatgpt.com/?temporary-chat=true"
    )
    assert provider.invocations[0].request["proxy_protected_write"] is True
    assert (
        provider.invocations[0].request["proxy_cookie_header"] == "session=test-cookie"
    )
    assert "minimal_conversation_id" not in provider.invocations[0].request
    assert provider.invocations[1].request["minimal_conversation_id"] == "temporary-1"
    assert provider.invocations[1].request["minimal_parent_message_id"] == "assistant-1"
    assert (
        provider.invocations[1].request["minimal_temporary_lifecycle_id"]
        == "lifecycle-1"
    )
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
