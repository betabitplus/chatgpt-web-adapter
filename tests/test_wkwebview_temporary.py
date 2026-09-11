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


class _Provider:
    def __init__(self) -> None:
        self._lightweight_transport = _TemporaryStreamTransport()
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
    ) -> dict[str, Any]:
        del timeout, on_text_event
        self.invocations.append(invocation)
        conversation_id = (
            invocation.request.get("minimal_conversation_id") or "temporary-1"
        )
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
    assert "minimal_conversation_id" not in provider.invocations[0].request
    assert provider.invocations[1].request["minimal_conversation_id"] == "temporary-1"
    assert provider.invocations[1].request["minimal_parent_message_id"] == "assistant-1"
    assert [event["delta"] for event in events] == ["temporary-1", "temporary-2"]

    assert runtime.end(
        lifecycle_id="lifecycle-1",
        conversation_id="temporary-1",
    ) == {"ok": True, "temporaryLifecycleState": "ENDED"}
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
