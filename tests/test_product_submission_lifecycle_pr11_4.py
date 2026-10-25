from __future__ import annotations

from types import SimpleNamespace

import pytest

import chatgpt_web_adapter.browser_owned_submission_lifecycle as lifecycle_subject
from chatgpt_web_adapter.browser_native_client import BrowserNativeSubmission
from chatgpt_web_adapter.browser_native_provider import BrowserNativeBridgeStatus
from chatgpt_web_adapter.browser_owned_product_transport import (
    BrowserOwnedProductTransport,
)
from chatgpt_web_adapter.browser_owned_submission_lifecycle import (
    SUBMISSION_FINALITY_PENDING,
)
from chatgpt_web_adapter.browser_owned_write_runtime import (
    BrowserOwnedWriteRuntimeError,
)
from chatgpt_web_adapter.product_runtime import ChatGPTProductRuntime
from chatgpt_web_adapter.revision_safe_streaming_pr8_9 import (
    RevisionSafeTextAccumulator,
)
from chatgpt_web_adapter.types import ChatResponse, ConversationStatus


class FakeClient:
    def __init__(self) -> None:
        self._browser_native_turn_provider = None
        self.readback_ack_count = 0
        self.status_value = "completed"

    def get_status(self, conversation):
        if isinstance(self.status_value, BaseException):
            raise self.status_value
        return ConversationStatus(status=self.status_value, message_id="baseline")

    def get_messages(self, conversation, **kwargs):
        return []

    def attach_conversation(self, conversation):
        raise AssertionError("canonical attach is replaced by the PR11.4 await stub")

    def complete_canonical_readback(self):
        self.readback_ack_count += 1
        return True


class FakeProvider:
    def __init__(self) -> None:
        self.bound = None
        self.bind_history: list[str | None] = []
        self.status_count = 0
        self.release_calls = []

    def send_text(self, *args, **kwargs):
        raise AssertionError(
            "low-level provider call is replaced by submit_browser_native"
        )

    def status(self):
        self.status_count += 1
        return BrowserNativeBridgeStatus(
            available=True,
            extension_connected=True,
            runtime_tab_id=41,
        )

    def set_browser_authority_lease(self, lease_id):
        self.bound = lease_id
        self.bind_history.append(lease_id)

    def clear_browser_authority_lease(self):
        self.bound = None
        self.bind_history.append(None)

    def release_runtime_tab(
        self,
        *,
        expected_runtime_tab_id,
        browser_authority_lease_id,
        timeout,
    ):
        self.release_calls.append(
            (expected_runtime_tab_id, browser_authority_lease_id, timeout)
        )
        return SimpleNamespace(
            released=True,
            already_absent=False,
            runtime_tab_id=expected_runtime_tab_id,
        )


def _install_split_stubs(monkeypatch, provider: FakeProvider):
    calls = {"submit": 0, "await": 0, "bound_during_await": None}

    def fake_submit(client, text, **kwargs):
        calls["submit"] += 1
        callback = kwargs["on_event"]
        callback(
            {
                "type": "browser_native_write_completed",
                "submission_id": "submission-1",
                "accepted_at_ms": 123456,
                "conversation_id": "conversation-1",
                "turn_exchange_id": "exchange-1",
                "runtime_tab_id": 41,
                "runtime_tab_preexisting": True,
            }
        )
        turn = SimpleNamespace(
            conversation_id="conversation-1",
            turn_exchange_id="exchange-1",
            response_status=200,
            tab_id=41,
        )
        return BrowserNativeSubmission(
            submission_id="submission-1",
            turn=turn,
            baseline_assistant_ids=frozenset(),
            baseline_message_ids=frozenset(),
            timeout=float(kwargs["timeout"]),
            poll_interval=float(kwargs["poll_interval"]),
            started_monotonic=1.0,
            accepted_at_ms=123456,
            is_continuation=False,
            attachment_count=0,
            stream_state=RevisionSafeTextAccumulator(),
            on_token=kwargs.get("on_token"),
            on_event=callback,
        )

    def fake_await(client, submission):
        calls["await"] += 1
        calls["bound_during_await"] = provider.bound
        submission.on_event(
            {
                "type": "browser_native_readback_completed",
                "submission_id": submission.submission_id,
                "conversation_id": "conversation-1",
                "message_id": "assistant-1",
                "canonical_finality_proven": True,
            }
        )
        return ChatResponse(text="done")

    monkeypatch.setattr(lifecycle_subject, "submit_browser_native", fake_submit)
    monkeypatch.setattr(lifecycle_subject, "await_browser_native_final", fake_await)
    return calls


def _runtime(monkeypatch):
    client = FakeClient()
    provider = FakeProvider()
    calls = _install_split_stubs(monkeypatch, provider)
    transport = BrowserOwnedProductTransport(client, provider=provider)
    runtime = ChatGPTProductRuntime(
        client,
        transport="browser-owned",
        write_transport=transport,
    )
    return runtime, transport, client, provider, calls


def test_pending_submission_blocks_every_second_write_before_delegation(monkeypatch):
    runtime, transport, _client, _provider, calls = _runtime(monkeypatch)
    ack = runtime.submit("first")

    with pytest.raises(BrowserOwnedWriteRuntimeError) as caught:
        runtime.submit("second")
    assert caught.value.failure_kind == SUBMISSION_FINALITY_PENDING
    assert caught.value.write_may_have_been_submitted is False

    with pytest.raises(BrowserOwnedWriteRuntimeError) as caught_send:
        runtime.send("compatibility send")
    assert caught_send.value.failure_kind == SUBMISSION_FINALITY_PENDING

    with pytest.raises(BrowserOwnedWriteRuntimeError) as caught_temporary:
        transport.send_text("temporary", conversation_mode="temporary")
    assert caught_temporary.value.failure_kind == SUBMISSION_FINALITY_PENDING

    assert calls["submit"] == 1
    assert runtime.submission_lifecycle_snapshot()["submission_id"] == ack.submission_id
