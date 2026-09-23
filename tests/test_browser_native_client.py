from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from chatgpt_web_adapter.browser_context_canonical import (
    BrowserContextCanonicalReadError,
)
from chatgpt_web_adapter.browser_native_client import (
    CanonicalTopicStreamNormalizer,
    _canonical_intermediate_events,
    _canonical_stream_answer_seed,
    _canonical_stream_identity,
    _is_stream_health_event,
    _make_passive_terminal_stop_check,
    _wait_for_new_final_assistant,
    await_browser_native_final,
    send_browser_native,
    submit_browser_native,
)
from chatgpt_web_adapter.browser_native_provider import BrowserNativeTurnResult
from chatgpt_web_adapter.exceptions import ConversationTimeoutError, RequestError
from chatgpt_web_adapter.types import ChatConversation


def test_stream_subscription_is_forwarded_as_health_diagnostic() -> None:
    assert _is_stream_health_event(
        {
            "type": "stream_handoff_ws_subscribed",
            "catchup_count": 0,
            "last_offset": "1000-0",
        }
    )
    assert _is_stream_health_event(
        {
            "type": "stream_handoff_terminal_status",
            "stream_status": "COMPLETE",
            "last_offset": "1000-0",
        }
    )


def test_passive_terminal_stop_check_waits_then_stops_and_cancel_is_immediate() -> None:
    state = {"completed": False, "cancelled": False, "settled": False}
    times = iter([10.0, 11.0, 11.5])
    should_stop = _make_passive_terminal_stop_check(
        lambda: state["completed"],
        cancelled=lambda: state["cancelled"],
        settled=lambda: state["settled"],
        settle_seconds=1.5,
        monotonic=lambda: next(times),
    )

    assert should_stop() is False
    state["completed"] = True
    assert should_stop() is False
    assert should_stop() is False
    assert should_stop() is True

    state["completed"] = False
    assert should_stop() is False
    state["completed"] = True
    state["settled"] = True
    assert should_stop() is True

    state["completed"] = False
    state["settled"] = False
    assert should_stop() is False
    state["cancelled"] = True
    assert should_stop() is True


class FakeProvider:
    def __init__(self) -> None:
        self.normal_calls = []

    def send_text(self, text, *, conversation=None, timeout=None):
        self.normal_calls.append((text, conversation, timeout))
        return BrowserNativeTurnResult(
            conversation_id="conversation-1",
            turn_exchange_id="turn-1",
            response_status=200,
            response_mime_type="text/event-stream",
            final_url="https://chatgpt.com/c/conversation-1",
            tab_id=17,
            tab_was_active=False,
            elapsed_ms=500,
        )


class RecoveryFakeProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.recovery_calls = []

    def send_text_with_stale_ui_recovery(
        self,
        text,
        *,
        conversation,
        timeout=None,
        canonical_completed_at_ms,
    ):
        self.recovery_calls.append(
            (text, conversation, timeout, canonical_completed_at_ms)
        )
        return BrowserNativeTurnResult(
            conversation_id="conversation-1",
            turn_exchange_id="turn-1",
            response_status=200,
            response_mime_type="text/event-stream",
            final_url="https://chatgpt.com/c/conversation-1",
            tab_id=17,
            tab_was_active=False,
            elapsed_ms=500,
            runtime_reloaded=True,
            runtime_reload_ms=321,
        )


class LeaseAwareProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.lease_id = "lease-new"
        self.write_leases: list[str | None] = []
        self.write_started = False

    def _current_browser_authority_lease_id(self):
        return self.lease_id

    def clear_browser_authority_lease(self) -> None:
        self.lease_id = None

    def set_browser_authority_lease(self, lease_id: str) -> None:
        self.lease_id = lease_id

    def send_text(self, text, *, conversation=None, timeout=None):
        self.write_started = True
        self.write_leases.append(self.lease_id)
        return super().send_text(text, conversation=conversation, timeout=timeout)


def _client(provider, *, status_value="completed"):
    old = SimpleNamespace(
        message_id="old-assistant",
        finish_reason="stop",
        text="old",
        model="gpt-old",
    )
    final = SimpleNamespace(
        message_id="new-assistant",
        finish_reason="stop",
        text="CANONICAL_READBACK",
        model="gpt-new",
    )

    class Client:
        _browser_native_turn_provider = provider

        def __init__(self) -> None:
            self.events = []
            self.status_values = (
                list(status_value) if isinstance(status_value, (list, tuple)) else None
            )

        def _emit_event(self, callback, event_type, **payload):
            self.events.append((event_type, payload))

        def get_status(self, conversation):
            if self.status_values is not None:
                value = self.status_values.pop(0) if self.status_values else "running"
            else:
                value = status_value
            return SimpleNamespace(status=value)

        def get_messages(self, conversation, **kwargs):
            if conversation == "existing-conversation":
                return [old]
            return [old, final]

        def attach_conversation(self, conversation):
            return SimpleNamespace(
                conversation=ChatConversation(conversation_id="conversation-1"),
                title="Browser native test",
            )

    return Client()


def test_client_returns_canonical_readback_not_native_body() -> None:
    provider = FakeProvider()
    client = _client(provider)
    response = send_browser_native(
        client,
        "hello",
        conversation="existing-conversation",
        timeout=2,
        poll_interval=0.01,
    )

    assert response.text == "CANONICAL_READBACK"
    assert response.conversation.conversation_id == "conversation-1"
    assert response.conversation.message_id == "new-assistant"
    assert response.request.turn_exchange_id == "turn-1"
    assert response.request.observed_model == "gpt-new"
    assert response.request.terminal_observed is True
    assert response.request.terminal_source == "canonical_readback"
    assert response.metrics.backend_status == 200
    assert provider.normal_calls == [("hello", "existing-conversation", 2)]


def test_stream_terminal_finality_skips_canonical_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider(FakeProvider):
        revision_safe_streaming_supported = True

        def send_text_streaming(
            self,
            text,
            *,
            conversation=None,
            timeout=None,
            on_text_event,
            on_transport_event,
            stream_should_stop,
        ):
            on_text_event(
                {
                    "type": "assistant_text_snapshot",
                    "sequence": 1,
                    "message_id": "assistant-stream",
                    "text": "stream final",
                }
            )
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id="turn-stream",
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=None,
                tab_was_active=False,
                elapsed_ms=100,
                stream_finality_proven=True,
                stream_message_id="assistant-stream",
                stream_finish_reason="stop",
                stream_model_slug="gpt-5-6-thinking",
                phase_one_tail_id="tail-1",
            )

        def consume_submitted_turn_tail(self, turn, *, timeout):
            assert turn.phase_one_tail_id == "tail-1"
            assert timeout <= 2.0
            return {
                "terminal_error_code": "conversation_too_large",
                "terminal_error": "You've reached the maximum length for this conversation.",
            }

    provider = Provider()
    client = _client(provider)

    def fail_canonical(*_args, **_kwargs):
        raise AssertionError("terminal WS finality must not poll canonical readback")

    monkeypatch.setattr(
        "chatgpt_web_adapter.browser_native_client._wait_for_new_final_assistant",
        fail_canonical,
    )

    def fail_attach(_conversation):
        raise AssertionError("terminal WS finality must not attach/read conversation")

    client.attach_conversation = fail_attach

    delivered: list[dict] = []
    submission = submit_browser_native(
        client,
        "hello",
        conversation="existing-conversation",
        timeout=2,
        poll_interval=0.01,
        on_event=delivered.append,
    )
    response = await_browser_native_final(client, submission)

    assert response.text == "stream final"
    assert response.conversation.conversation_id == "conversation-1"
    assert response.conversation.message_id == "assistant-stream"
    assert response.conversation.parent_message_id == "assistant-stream"
    assert response.conversation.finish_reason == "stop"
    assert response.request.observed_model == "gpt-5-6-thinking"
    assert response.request.turn_exchange_id == "turn-stream"
    assert response.request.terminal_observed is True
    assert response.request.terminal_source == "stream"
    assert response.request.terminal_error_code == "conversation_too_large"
    assert (
        response.request.terminal_error
        == "You've reached the maximum length for this conversation."
    )


def test_split_submit_defers_exact_topic_and_await_final_never_polls_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider(FakeProvider):
        revision_safe_streaming_supported = True

        def __init__(self) -> None:
            super().__init__()
            self.submit_calls = 0
            self.follow_calls = 0
            self.observe_calls = 0

        def submit_text_streaming(
            self,
            text,
            *,
            conversation=None,
            timeout=None,
            on_text_event,
            on_write_identity=None,
            on_transport_event=None,
            stream_should_stop=None,
        ):
            self.submit_calls += 1
            if on_write_identity is not None:
                on_write_identity({"conversation_id": "conversation-1"})
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id="turn-deferred",
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=None,
                tab_was_active=False,
                elapsed_ms=120,
                passive_observer_armed=True,
                stream_topic_id="conversation-turn-deferred",
            )

        def follow_submitted_turn(
            self,
            turn,
            *,
            timeout,
            on_transport_event=None,
            stream_should_stop=None,
        ):
            self.follow_calls += 1
            assert turn.stream_topic_id == "conversation-turn-deferred"
            assert on_transport_event is not None
            on_transport_event(
                {
                    "type": "stream_handoff_server_stalled",
                    "topic_id": turn.stream_topic_id,
                    "server_idle_seconds": 305.0,
                }
            )
            on_transport_event(
                {
                    "type": "raw_ws_event",
                    "parsed": {
                        "message": {
                            "id": "assistant-deferred",
                            "author": {"role": "assistant"},
                            "content": {"content_type": "text", "parts": ["deferred final"]},
                            "status": "finished_successfully",
                            "end_turn": True,
                            "metadata": {"model_slug": "gpt-5-6-thinking"},
                        }
                    },
                }
            )
            on_transport_event({"type": "raw_ws_done"})
            return {
                "stream_finality_proven": True,
                "message_id": "assistant-deferred",
                "finish_reason": "stop",
                "observed_model": "gpt-5-6-thinking",
                "segment_done_count": 1,
                "terminal_error_code": "conversation_too_large",
                "terminal_error": "You've reached the maximum length for this conversation.",
            }

        def observe_turn(self, **kwargs):
            self.observe_calls += 1
            raise AssertionError("exact deferred topic must not use canonical observer")

    provider = Provider()
    client = _client(provider)
    delivered: list[dict] = []

    def emit(callback, event_type, **payload):
        client.events.append((event_type, payload))
        if callback is not None:
            callback({"type": event_type, **payload})

    client._emit_event = emit
    monkeypatch.setattr(
        "chatgpt_web_adapter.browser_native_client._wait_for_new_final_assistant",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("exact deferred topic must not canonical-read")
        ),
    )

    submission = submit_browser_native(
        client,
        "hello",
        conversation="existing-conversation",
        timeout=5,
        poll_interval=0.01,
        on_event=delivered.append,
    )

    assert provider.submit_calls == 1
    assert provider.follow_calls == 0
    assert submission.turn.stream_topic_id == "conversation-turn-deferred"
    assert any(event.get("type") == "browser_native_write_completed" for event in delivered)

    response = await_browser_native_final(client, submission)

    assert provider.follow_calls == 1
    assert provider.observe_calls == 0
    assert any(
        event.get("type") == "stream_handoff_server_stalled"
        and event.get("server_idle_seconds") == 305.0
        for event in delivered
    )
    assert response.text == "deferred final"
    assert response.conversation.message_id == "assistant-deferred"
    assert response.request.observed_model == "gpt-5-6-thinking"
    assert response.request.terminal_error_code == "conversation_too_large"
    assert (
        response.request.terminal_error
        == "You've reached the maximum length for this conversation."
    )


def test_split_submit_external_completion_reconciles_full_canonical_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider(FakeProvider):
        revision_safe_streaming_supported = True

        def __init__(self) -> None:
            super().__init__()
            self.follow_calls = 0
            self.observe_calls = 0

        def submit_text_streaming(
            self,
            text,
            *,
            conversation=None,
            timeout=None,
            on_text_event,
            on_write_identity=None,
            on_transport_event=None,
            stream_should_stop=None,
        ):
            if on_write_identity is not None:
                on_write_identity({"conversation_id": "conversation-1"})
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id="turn-delivery-loss",
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=None,
                tab_was_active=False,
                elapsed_ms=100,
                passive_observer_armed=True,
                stream_topic_id="conversation-turn-delivery-loss",
            )

        def follow_submitted_turn(
            self,
            turn,
            *,
            timeout,
            on_transport_event=None,
            stream_should_stop=None,
        ):
            self.follow_calls += 1
            assert on_transport_event is not None
            on_transport_event(
                {
                    "type": "raw_ws_event",
                    "parsed": {
                        "message": {
                            "id": "assistant-partial",
                            "author": {"role": "assistant"},
                            "recipient": "all",
                            "content": {
                                "content_type": "text",
                                "parts": ["partial before delivery loss"],
                            },
                            "status": "in_progress",
                            "end_turn": False,
                            "metadata": {
                                "turn_exchange_id": "turn-delivery-loss",
                            },
                        }
                    },
                }
            )
            return {
                "external_completion_observed": True,
                "stream_finality_proven": False,
                "message_id": "assistant-partial",
                "finish_reason": "conversation_turn_complete",
                "segment_done_count": 0,
            }

        def observe_turn(self, **kwargs):
            self.observe_calls += 1
            raise AssertionError(
                "independent completion must go directly to canonical final reconcile"
            )

    provider = Provider()
    client = _client(provider)
    delivered: list[dict] = []
    canonical_calls: list[dict] = []

    def canonical_final(*_args, **kwargs):
        canonical_calls.append(dict(kwargs))
        return (
            SimpleNamespace(
                message_id="assistant-canonical-final",
                finish_reason="stop",
                text="CANONICAL_RECOVERED_FINAL",
                model="gpt-5-6-thinking",
            ),
            None,
            1,
        )

    monkeypatch.setattr(
        "chatgpt_web_adapter.browser_native_client._wait_for_new_final_assistant",
        canonical_final,
    )

    submission = submit_browser_native(
        client,
        "hello",
        conversation="existing-conversation",
        timeout=5,
        poll_interval=0.01,
        on_event=delivered.append,
    )
    response = await_browser_native_final(client, submission)

    assert provider.follow_calls == 1
    assert provider.observe_calls == 0
    assert len(canonical_calls) == 1
    assert canonical_calls[0]["include_readback"] is True
    assert response.text == "CANONICAL_RECOVERED_FINAL"
    assert response.text != "partial before delivery loss"
    assert response.conversation.message_id == "assistant-canonical-final"
    assert response.conversation.finish_reason == "stop"


def test_split_submit_accepts_normalizer_terminal_proof_after_revision_without_canonical_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider(FakeProvider):
        revision_safe_streaming_supported = True

        def __init__(self) -> None:
            super().__init__()
            self.observe_calls = 0

        def submit_text_streaming(
            self,
            text,
            *,
            conversation=None,
            timeout=None,
            on_text_event,
            on_write_identity=None,
            on_transport_event=None,
            stream_should_stop=None,
        ):
            if on_write_identity is not None:
                on_write_identity({"conversation_id": "conversation-1"})
            on_text_event(
                {
                    "type": "assistant_text_snapshot",
                    "sequence": 1,
                    "message_id": "assistant-revised",
                    "text": "LOAD_WRONG",
                }
            )
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id="turn-revised",
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=None,
                tab_was_active=False,
                elapsed_ms=100,
                passive_observer_armed=True,
                stream_topic_id="conversation-turn-revised",
            )

        def follow_submitted_turn(
            self,
            turn,
            *,
            timeout,
            on_transport_event=None,
            stream_should_stop=None,
        ):
            assert on_transport_event is not None
            on_transport_event(
                {
                    "type": "raw_ws_event",
                    "parsed": {
                        "message": {
                            "id": "assistant-revised",
                            "author": {"role": "assistant"},
                            "content": {
                                "content_type": "text",
                                "parts": ["LOAD_PTY_1_0_OK_20260917"],
                            },
                            "status": "in_progress",
                            "end_turn": False,
                        }
                    },
                }
            )
            on_transport_event({"type": "raw_ws_done"})
            on_transport_event(
                {
                    "type": "raw_ws_event",
                    "parsed": {
                        "message": {
                            "id": "assistant-revised",
                            "author": {"role": "assistant"},
                            "recipient": "all",
                            "channel": "final",
                            "content": {
                                "content_type": "text",
                                "parts": ["LOAD_PTY_1_0_OK_20260917"],
                            },
                            "status": "finished_successfully",
                            "end_turn": True,
                        }
                    },
                }
            )
            on_transport_event({"type": "raw_ws_done"})
            return {
                "stream_finality_proven": False,
                "message_id": None,
                "finish_reason": None,
                "segment_done_count": 1,
            }

        def observe_turn(self, **kwargs):
            self.observe_calls += 1
            raise AssertionError("normalizer terminal proof must not canonical-observe")

    provider = Provider()
    client = _client(provider)
    delivered: list[dict] = []

    def emit(callback, event_type, **payload):
        client.events.append((event_type, payload))
        if callback is not None:
            callback({"type": event_type, **payload})

    client._emit_event = emit
    monkeypatch.setattr(
        "chatgpt_web_adapter.browser_native_client._wait_for_new_final_assistant",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("normalizer terminal proof must not canonical-read")
        ),
    )

    submission = submit_browser_native(
        client,
        "hello",
        conversation="existing-conversation",
        timeout=5,
        poll_interval=0.01,
        on_event=delivered.append,
    )
    response = await_browser_native_final(client, submission)

    assert response.text == "LOAD_PTY_1_0_OK_20260917"
    assert response.conversation.message_id == "assistant-revised"
    assert response.conversation.finish_reason == "stop"
    assert provider.observe_calls == 0
    assert any(event.get("type") == "assistant_text_revision" for event in delivered)


def test_split_submit_stop_cancels_topic_without_canonical_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider(FakeProvider):
        revision_safe_streaming_supported = True

        def __init__(self) -> None:
            super().__init__()
            self.stopped = False
            self.follow_calls = 0

        def submit_text_streaming(
            self,
            text,
            *,
            conversation=None,
            timeout=None,
            on_text_event,
            on_write_identity=None,
            on_transport_event=None,
            stream_should_stop=None,
        ):
            if on_write_identity is not None:
                on_write_identity({"conversation_id": "conversation-1"})
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id="turn-stop",
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=None,
                tab_was_active=False,
                elapsed_ms=100,
                passive_observer_armed=True,
                stream_topic_id="conversation-turn-stop",
            )

        def follow_submitted_turn(
            self,
            turn,
            *,
            timeout,
            on_transport_event=None,
            stream_should_stop=None,
        ):
            self.follow_calls += 1
            assert on_transport_event is not None
            on_transport_event(
                {
                    "type": "raw_ws_event",
                    "parsed": {
                        "message": {
                            "id": "assistant-partial",
                            "author": {"role": "assistant"},
                            "content": {"content_type": "text", "parts": ["partial answer"]},
                            "status": "in_progress",
                            "end_turn": False,
                        }
                    },
                }
            )
            self.stopped = True
            assert stream_should_stop is not None and stream_should_stop() is True
            return {
                "stream_finality_proven": False,
                "message_id": "assistant-partial",
                "finish_reason": "stream_terminal",
                "segment_done_count": 0,
            }

        def stop_requested_for(self, conversation_id):
            return self.stopped

        def clear_stop_requested_for(self, conversation_id):
            self.stopped = False

    provider = Provider()
    client = _client(provider)
    delivered: list[dict] = []

    def emit(callback, event_type, **payload):
        client.events.append((event_type, payload))
        if callback is not None:
            callback({"type": event_type, **payload})

    client._emit_event = emit
    monkeypatch.setattr(
        "chatgpt_web_adapter.browser_native_client._wait_for_new_final_assistant",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("confirmed Stop must not canonical-read")
        ),
    )

    submission = submit_browser_native(
        client,
        "hello",
        conversation="existing-conversation",
        timeout=5,
        poll_interval=0.01,
        on_event=delivered.append,
    )
    response = await_browser_native_final(client, submission)

    assert provider.follow_calls == 1
    assert response.text == "partial answer"
    assert response.conversation.finish_reason == "stopped"
    assert provider.stopped is False
    readback = [event for event in delivered if event.get("type") == "browser_native_readback_completed"]
    assert readback[-1]["canonical_payload_read_count"] == 0
    assert readback[-1]["stopped_by_user"] is True


def test_active_streaming_send_waits_for_whole_turn_terminal() -> None:
    class Provider(FakeProvider):
        revision_safe_streaming_supported = True

        def send_text_streaming(
            self,
            text,
            *,
            conversation=None,
            timeout=None,
            on_text_event,
            on_transport_event,
            stream_should_stop,
        ):
            assert stream_should_stop() is False
            on_transport_event(
                {
                    "type": "raw_ws_event",
                    "parsed": {
                        "v": {
                            "message": {
                                "id": "reasoning-1",
                                "author": {"role": "assistant"},
                                "recipient": "all",
                                "status": "finished_successfully",
                                "content": {
                                    "content_type": "thoughts",
                                    "parts": ["internal-only-parts"],
                                    "thoughts": [
                                        {
                                            "summary": "Checking state",
                                            "content": "internal-only-content",
                                            "finished": True,
                                        }
                                    ],
                                },
                                "metadata": {"turn_exchange_id": "turn-1"},
                            }
                        }
                    },
                }
            )
            assert stream_should_stop() is False
            on_transport_event(
                {"type": "raw_ws_done", "topic_id": "conversation-turn-turn-1"}
            )
            assert stream_should_stop() is False
            on_transport_event(
                {
                    "type": "raw_ws_event",
                    "parsed": {
                        "v": {
                            "message": {
                                "id": "assistant-1",
                                "author": {"role": "assistant"},
                                "recipient": "all",
                                "content": {
                                    "content_type": "text",
                                    "parts": ["done"],
                                },
                                "metadata": {"turn_exchange_id": "turn-1"},
                                "end_turn": True,
                            }
                        }
                    },
                }
            )
            assert stream_should_stop() is False
            on_transport_event(
                {
                    "type": "raw_ws_event",
                    "parsed": {"type": "message_stream_complete"},
                }
            )
            assert stream_should_stop() is True
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id="turn-1",
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=None,
                tab_was_active=False,
                elapsed_ms=100,
            )

    provider = Provider()
    client = _client(provider)
    delivered: list[dict] = []

    def emit(callback, event_type, **payload):
        client.events.append((event_type, payload))
        if callback is not None:
            callback({"type": event_type, **payload})

    client._emit_event = emit

    submit_browser_native(
        client,
        "hello",
        conversation="existing-conversation",
        timeout=2,
        poll_interval=0.01,
        on_event=delivered.append,
    )

    reasoning = [
        event
        for event in delivered
        if event.get("type") == "canonical_intermediate_message"
        and event.get("message_id") == "reasoning-1"
    ]
    assert len(reasoning) == 1
    assert reasoning[0]["message_kind"] == "reasoning"
    assert reasoning[0]["text"] == "Checking state"
    assert "internal-only" not in repr(reasoning)
    answer_events = [
        event
        for event in delivered
        if event.get("type")
        in {
            "assistant_text_snapshot",
            "assistant_text_delta",
            "assistant_text_revision",
        }
        and event.get("message_id") == "assistant-1"
    ]
    assert answer_events
    assert (
        answer_events[-1].get("delta") == "done"
        or answer_events[-1].get("text") == "done"
    )


def test_active_streaming_send_forwards_first_leg_raw_intermediate_without_polling() -> (
    None
):
    reasoning_observed = threading.Event()
    provider_returned = threading.Event()
    payload_reads = 0
    provider_entry_reads: list[int] = []
    provider_return_reads: list[int] = []

    baseline_payload = {
        "conversation_id": "conversation-1",
        "current_node": "old",
        "mapping": {
            "old": {
                "id": "old",
                "parent": None,
                "children": [],
                "message": {
                    "id": "old-assistant",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "status": "finished_successfully",
                    "content": {"content_type": "text", "parts": ["old"]},
                    "metadata": {},
                    "end_turn": True,
                },
            }
        },
    }
    reasoning_message = {
        "id": "reasoning-live",
        "author": {"role": "assistant"},
        "recipient": "all",
        "status": "finished_successfully",
        "content": {
            "content_type": "thoughts",
            "thoughts": [
                {
                    "summary": "Visible first-leg reasoning",
                    "content": "internal-only-content",
                    "finished": True,
                }
            ],
        },
        "metadata": {"turn_exchange_id": "turn-1"},
        "end_turn": False,
    }

    class Provider(FakeProvider):
        revision_safe_streaming_supported = True

        def send_text_streaming(
            self,
            text,
            *,
            conversation=None,
            timeout=None,
            on_text_event,
            on_write_identity,
            on_transport_event,
        ):
            provider_entry_reads.append(payload_reads)
            on_write_identity(
                {
                    "type": "write_identity_resolved",
                    "conversation_id": "conversation-1",
                    "submit_response_observed": True,
                    "submit_response_status": 200,
                }
            )
            on_transport_event(
                {
                    "type": "raw_ws_event",
                    "parsed": {"v": {"message": reasoning_message}},
                }
            )
            assert reasoning_observed.wait(2.0)
            provider_return_reads.append(payload_reads)
            provider_returned.set()
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id="turn-1",
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=None,
                tab_was_active=False,
                elapsed_ms=500,
            )

    provider = Provider()
    client = _client(provider)

    def read_payload(_conversation_id: str):
        nonlocal payload_reads
        payload_reads += 1
        return baseline_payload

    client._get_conversation_payload = read_payload
    delivered: list[dict] = []

    def emit(callback, event_type, **payload):
        event = {"type": event_type, **payload}
        client.events.append((event_type, payload))
        if callback is not None:
            callback(event)

    client._emit_event = emit

    def on_event(event: dict) -> None:
        delivered.append(event)
        if event.get("message_id") == "reasoning-live":
            assert provider_returned.is_set() is False
            reasoning_observed.set()

    submit_browser_native(
        client,
        "hello",
        conversation="existing-conversation",
        timeout=2,
        poll_interval=0.01,
        on_event=on_event,
    )

    assert reasoning_observed.is_set()
    assert provider_returned.is_set()
    assert provider_entry_reads
    assert provider_return_reads == provider_entry_reads
    reasoning = [
        event
        for event in delivered
        if event.get("type") == "canonical_intermediate_message"
        and event.get("message_id") == "reasoning-live"
    ]
    assert reasoning == [
        {
            "type": "canonical_intermediate_message",
            "message_id": "reasoning-live",
            "message_kind": "reasoning",
            "text": "Visible first-leg reasoning",
            "label": None,
            "tool_name": None,
            "submission_id": reasoning[0]["submission_id"],
        }
    ]
    assert "internal-only" not in repr(reasoning)


def test_streaming_write_identity_is_emitted_before_write_completed() -> None:
    class IdentityProvider(FakeProvider):
        revision_safe_streaming_supported = True

        def send_text_streaming(
            self,
            text,
            *,
            conversation=None,
            timeout=None,
            on_text_event,
            on_write_identity,
        ):
            on_write_identity(
                {
                    "type": "write_identity_resolved",
                    "conversation_id": "conversation-early",
                    "submit_response_observed": True,
                    "submit_response_status": 200,
                }
            )
            on_text_event(
                {
                    "type": "assistant_text_delta",
                    "sequence": 1,
                    "message_id": "assistant-early",
                    "delta": "partial",
                }
            )
            return BrowserNativeTurnResult(
                conversation_id="conversation-early",
                turn_exchange_id="turn-early",
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-early",
                tab_id=None,
                tab_was_active=False,
                elapsed_ms=100,
                canonical_read_transport="curl_cffi",
                phase_a_transport="wkwebview_minimal_security_shell",
                phase_a_gate_wait_ms=12,
                phase_a_elapsed_ms=88,
                phase_b_transport="curl_cffi_websocket",
                phase_b_elapsed_ms=144,
            )

    provider = IdentityProvider()
    client = _client(provider)

    submission = submit_browser_native(
        client,
        "hello",
        timeout=2,
        poll_interval=0.01,
        on_event=lambda _event: None,
    )

    assert submission.turn.conversation_id == "conversation-early"
    event_types = [event_type for event_type, _payload in client.events]
    identity_index = event_types.index("browser_native_write_identity_resolved")
    completed_index = event_types.index("browser_native_write_completed")
    assert identity_index < completed_index
    identity_payload = client.events[identity_index][1]
    assert identity_payload["conversation_id"] == "conversation-early"
    assert identity_payload["status_code"] == 200
    completed_payload = client.events[completed_index][1]
    assert completed_payload["canonical_read_transport"] == "curl_cffi"
    assert completed_payload["phase_a_transport"] == "wkwebview_minimal_security_shell"
    assert completed_payload["phase_a_gate_wait_ms"] == 12
    assert completed_payload["phase_a_elapsed_ms"] == 88
    assert completed_payload["phase_b_transport"] == "curl_cffi_websocket"
    assert completed_payload["phase_b_elapsed_ms"] == 144


def test_completed_continuation_authorizes_bounded_stale_ui_recovery() -> None:
    provider = RecoveryFakeProvider()
    client = _client(provider, status_value="completed")

    response = send_browser_native(
        client,
        "hello",
        conversation="existing-conversation",
        timeout=2,
        poll_interval=0.01,
    )

    assert response.text == "CANONICAL_READBACK"
    assert provider.normal_calls == []
    assert len(provider.recovery_calls) == 1
    text, conversation, timeout, completed_at_ms = provider.recovery_calls[0]
    assert (text, conversation, timeout) == ("hello", "existing-conversation", 2)
    assert isinstance(completed_at_ms, int) and completed_at_ms > 0
    write_events = [
        payload
        for event_type, payload in client.events
        if event_type == "browser_native_write_completed"
    ]
    assert write_events[0]["runtime_reloaded"] is True
    assert write_events[0]["runtime_reload_ms"] == 321


def test_continuation_prewrite_reuses_one_canonical_payload_for_baseline_and_status() -> (
    None
):
    provider = RecoveryFakeProvider()

    class Client:
        _browser_native_turn_provider = provider

        def __init__(self) -> None:
            self.reads = 0

        def _get_conversation_payload(self, conversation_id):
            assert conversation_id == "existing-conversation"
            self.reads += 1
            return _completed_canonical_payload()

        def get_status(self, conversation):
            assert conversation == "existing-conversation"
            self.reads += 1
            return SimpleNamespace(status="completed")

        def _emit_event(self, callback, event_type, **payload):
            return None

    client = Client()
    submission = submit_browser_native(
        client,
        "hello",
        conversation="existing-conversation",
        timeout=2,
        poll_interval=0.01,
    )

    assert submission.baseline_message_ids == frozenset({"assistant-1"})
    assert submission.baseline_assistant_ids == frozenset({"assistant-1"})
    assert client.reads == 2
    assert len(provider.recovery_calls) == 1


def test_supplied_commit_payload_avoids_duplicate_baseline_read() -> None:
    provider = FakeProvider()

    class Client:
        _browser_native_turn_provider = provider

        def _get_conversation_payload(self, _conversation_id):
            raise AssertionError("commit payload should be reused instead of reread")

        def _emit_event(self, callback, event_type, **payload):
            return None

    client = Client()
    submission = submit_browser_native(
        client,
        "hello",
        conversation="existing-conversation",
        timeout=2,
        poll_interval=0.01,
        _prewrite_canonical_payload=_completed_canonical_payload(),
    )

    assert submission.baseline_message_ids == frozenset({"assistant-1"})
    assert submission.baseline_assistant_ids == frozenset({"assistant-1"})
    assert provider.normal_calls == [("hello", "existing-conversation", 2)]


def test_running_continuation_never_authorizes_stale_ui_recovery() -> None:
    provider = RecoveryFakeProvider()
    client = _client(provider, status_value="running")

    send_browser_native(
        client,
        "hello",
        conversation="existing-conversation",
        timeout=2,
        poll_interval=0.01,
    )

    assert provider.normal_calls == [("hello", "existing-conversation", 2)]
    assert provider.recovery_calls == []


def test_completion_evidence_must_still_be_completed_on_immediate_recheck() -> None:
    provider = RecoveryFakeProvider()
    client = _client(provider, status_value=["completed", "running", "completed"])

    send_browser_native(
        client,
        "hello",
        conversation="existing-conversation",
        timeout=2,
        poll_interval=0.01,
    )

    assert provider.normal_calls == [("hello", "existing-conversation", 2)]
    assert provider.recovery_calls == []


def test_continuation_preflight_temporarily_suspends_new_write_lease() -> None:
    provider = LeaseAwareProvider()
    client = _client(provider, status_value="completed")
    observed_reads: list[tuple[str, str | None]] = []
    original_get_messages = client.get_messages
    original_get_status = client.get_status

    def get_messages(conversation, **kwargs):
        phase = "postwrite" if provider.write_started else "prewrite"
        observed_reads.append((phase, provider._current_browser_authority_lease_id()))
        return original_get_messages(conversation, **kwargs)

    def get_status(conversation):
        phase = "postwrite" if provider.write_started else "prewrite"
        observed_reads.append((phase, provider._current_browser_authority_lease_id()))
        return original_get_status(conversation)

    client.get_messages = get_messages
    client.get_status = get_status

    response = send_browser_native(
        client,
        "hello",
        conversation="existing-conversation",
        timeout=2,
        poll_interval=0.01,
    )

    assert response.text == "CANONICAL_READBACK"
    prewrite = [lease for phase, lease in observed_reads if phase == "prewrite"]
    postwrite = [lease for phase, lease in observed_reads if phase == "postwrite"]
    assert prewrite and all(lease is None for lease in prewrite)
    assert provider.write_leases == ["lease-new"]
    assert postwrite and all(lease == "lease-new" for lease in postwrite)
    assert provider._current_browser_authority_lease_id() == "lease-new"


def test_new_chat_never_authorizes_stale_ui_recovery() -> None:
    provider = RecoveryFakeProvider()
    client = _client(provider, status_value="completed")

    send_browser_native(
        client,
        "hello",
        timeout=2,
        poll_interval=0.01,
    )

    assert provider.normal_calls == [("hello", None, 2)]
    assert provider.recovery_calls == []


def _completed_canonical_payload() -> dict:
    return {
        "conversation_id": "conversation-1",
        "title": "Committed new chat",
        "current_node": "assistant-node",
        "mapping": {
            "assistant-node": {
                "id": "assistant-node",
                "parent": None,
                "children": [],
                "message": {
                    "id": "assistant-1",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["done"]},
                    "metadata": {
                        "finish_details": {"type": "stop"},
                        "message_status": "finished_successfully",
                    },
                    "end_turn": True,
                },
            }
        },
    }


def test_fresh_internal_commit_evidence_skips_duplicate_recovery_status_read() -> None:
    provider = RecoveryFakeProvider()
    checked_at_ms = int(time.time() * 1000)

    class Client:
        _browser_native_turn_provider = provider

        def _get_conversation_payload(self, _conversation_id):
            raise AssertionError("fresh supplied commit payload must be reused")

        def get_status(self, _conversation):
            raise AssertionError(
                "fresh internal commit evidence must skip status recheck"
            )

        def _emit_event(self, callback, event_type, **payload):
            return None

    submission = submit_browser_native(
        Client(),
        "hello",
        conversation="existing-conversation",
        timeout=2,
        poll_interval=0.01,
        _prewrite_canonical_payload=_completed_canonical_payload(),
        _prewrite_canonical_completed_at_ms=checked_at_ms,
    )

    assert submission.baseline_message_ids == frozenset({"assistant-1"})
    assert len(provider.recovery_calls) == 1
    assert provider.recovery_calls[0][3] == checked_at_ms


def test_progress_message_cannot_finalize_long_turn(monkeypatch) -> None:
    progress_payload = {
        "conversation_id": "conversation-1",
        "title": "Long turn",
        "current_node": "progress-node",
        "mapping": {
            "progress-node": {
                "id": "progress-node",
                "parent": None,
                "children": [],
                "message": {
                    "id": "progress-1",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {
                        "content_type": "text",
                        "parts": ["Core foundation уже не теория..."],
                    },
                    "metadata": {
                        "is_thinking_preamble_message": True,
                        "message_status": "finished_successfully",
                    },
                    "end_turn": False,
                },
            }
        },
    }

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def _get_conversation_payload(self, conversation_id):
            self.calls += 1
            return (
                progress_payload if self.calls == 1 else _completed_canonical_payload()
            )

    monkeypatch.setattr(
        "chatgpt_web_adapter.browser_native_client.time.sleep",
        lambda seconds: None,
    )
    client = Client()
    message, payload, reads = _wait_for_new_final_assistant(
        client,
        "conversation-1",
        baseline_assistant_ids=frozenset(),
        timeout=30.0,
        interval=0.01,
        include_readback=True,
    )

    assert client.calls == 2
    assert reads == 2
    assert payload is not None
    assert message.message_id == "assistant-1"
    assert message.text == "done"


def test_committed_new_chat_treats_initial_canonical_400_as_transient() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def _get_conversation_payload(self, conversation_id):
            self.calls += 1
            if self.calls == 1:
                raise RequestError("canonical status=400", status_code=400)
            return _completed_canonical_payload()

    client = Client()
    message, payload, reads = _wait_for_new_final_assistant(
        client,
        "conversation-1",
        baseline_assistant_ids=frozenset(),
        timeout=0.5,
        interval=0.01,
        include_readback=True,
        retry_400_until_timeout=True,
    )

    assert client.calls == 2
    assert reads == 2
    assert payload is not None
    assert message.text == "done"


def test_canonical_400_without_committed_new_chat_grace_fails_fast() -> None:
    class Client:
        def _get_conversation_payload(self, conversation_id):
            raise RequestError("canonical status=400", status_code=400)

    with pytest.raises(RequestError) as raised:
        _wait_for_new_final_assistant(
            Client(),
            "conversation-1",
            baseline_assistant_ids=frozenset(),
            timeout=0.5,
            interval=0.01,
            retry_400_until_timeout=False,
        )

    assert raised.value.status_code == 400


def test_retryable_canonical_429_backs_off_and_recovers(monkeypatch) -> None:
    class RateLimited(RequestError):
        retryable = True

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def _get_conversation_payload(self, conversation_id):
            self.calls += 1
            if self.calls == 1:
                raise RateLimited("canonical status=429", status_code=429)
            return _completed_canonical_payload()

    sleeps = []
    monkeypatch.setattr(
        "chatgpt_web_adapter.browser_native_client.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )
    client = Client()
    message, payload, reads = _wait_for_new_final_assistant(
        client,
        "conversation-1",
        baseline_assistant_ids=frozenset(),
        timeout=30.0,
        interval=0.01,
        include_readback=True,
    )

    assert client.calls == 2
    assert sleeps == [15.0]
    assert reads == 2
    assert payload is not None
    assert message.text == "done"


def test_retryable_browser_context_timeout_recovers_without_replaying_write(
    monkeypatch,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def _get_conversation_payload(self, conversation_id):
            self.calls += 1
            if self.calls == 1:
                raise BrowserContextCanonicalReadError(
                    "CANONICAL_READ_TIMEOUT",
                    conversation_id=conversation_id,
                    retryable=False,
                )
            return _completed_canonical_payload()

    sleeps = []
    monkeypatch.setattr(
        "chatgpt_web_adapter.browser_native_client.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )
    client = Client()
    message, payload, reads = _wait_for_new_final_assistant(
        client,
        "conversation-1",
        baseline_assistant_ids=frozenset(),
        timeout=5.0,
        interval=0.01,
        include_readback=True,
        minimum_poll_interval=0.01,
    )

    assert client.calls == 2
    assert sleeps == [0.01]
    assert reads == 2
    assert payload is not None
    assert message.text == "done"


def test_successful_canonical_polling_has_fifteen_second_floor(monkeypatch) -> None:
    pending = {
        "conversation_id": "conversation-1",
        "current_node": "user-node",
        "mapping": {
            "user-node": {
                "id": "user-node",
                "parent": None,
                "children": [],
                "message": {
                    "id": "user-1",
                    "author": {"role": "user"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["hello"]},
                },
            }
        },
    }

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def _get_conversation_payload(self, conversation_id):
            self.calls += 1
            return pending if self.calls == 1 else _completed_canonical_payload()

    sleeps = []
    monkeypatch.setattr(
        "chatgpt_web_adapter.browser_native_client.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )
    client = Client()
    message, _, _ = _wait_for_new_final_assistant(
        client,
        "conversation-1",
        baseline_assistant_ids=frozenset(),
        timeout=30.0,
        interval=0.01,
        include_readback=True,
    )

    assert message.text == "done"
    assert client.calls == 2
    assert sleeps == [15.0]


def test_canonical_stream_identity_reconstructs_topic_from_latest_turn_exchange_id() -> (
    None
):
    payload = {
        "current_node": "assistant-new",
        "mapping": {
            "user-old": {
                "parent": None,
                "children": ["assistant-old"],
                "message": {
                    "id": "user-old",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["old"]},
                    "metadata": {},
                },
            },
            "assistant-old": {
                "parent": "user-old",
                "children": ["user-new"],
                "message": {
                    "id": "assistant-old",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["old answer"]},
                    "metadata": {"turn_exchange_id": "turn-old"},
                },
            },
            "user-new": {
                "parent": "assistant-old",
                "children": ["assistant-new"],
                "message": {
                    "id": "user-new",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["new"]},
                    "metadata": {},
                },
            },
            "assistant-new": {
                "parent": "user-new",
                "children": [],
                "message": {
                    "id": "assistant-new",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["partial"]},
                    "metadata": {
                        "turn_exchange_id": "turn-new",
                        "working_turn_id": "turn-new",
                    },
                },
            },
        },
    }

    topic_id, turn_exchange_id = _canonical_stream_identity(payload)
    answer_message_id, answer_text = _canonical_stream_answer_seed(
        payload,
        turn_exchange_id=turn_exchange_id,
    )

    assert topic_id == "conversation-turn-turn-new"
    assert turn_exchange_id == "turn-new"
    assert answer_message_id == "assistant-new"
    assert answer_text == "partial"


def test_canonical_stream_identity_prefers_explicit_stream_topic_id() -> None:
    payload = {
        "current_node": "assistant-1",
        "mapping": {
            "assistant-1": {
                "parent": None,
                "children": [],
                "message": {
                    "id": "assistant-1",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["partial"]},
                    "metadata": {
                        "stream_topic_id": "custom-topic",
                        "turn_exchange_id": "turn-1",
                    },
                },
            }
        },
    }

    assert _canonical_stream_identity(payload) == ("custom-topic", "turn-1")


def test_topic_stream_normalizer_waits_for_whole_turn_terminal_after_final() -> None:
    normalizer = CanonicalTopicStreamNormalizer()
    final_event = {
        "type": "raw_ws_event",
        "parsed": {
            "v": {
                "message": {
                    "id": "assistant-final",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["done"]},
                    "end_turn": True,
                    "metadata": {"turn_exchange_id": "turn-1"},
                }
            }
        },
    }

    normalizer.feed_transport_event(final_event)
    assert normalizer.turn_completed is True
    assert normalizer.whole_turn_terminal_seen is False

    normalizer.feed_transport_event({"type": "raw_ws_done"})
    assert normalizer.segment_kind is None
    assert normalizer.whole_turn_terminal_seen is False

    normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {"type": "message_stream_complete"},
        }
    )
    assert normalizer.whole_turn_terminal_seen is True

    error_normalizer = CanonicalTopicStreamNormalizer()
    error_normalizer.feed_transport_event(final_event)
    error_normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "message": None,
                "error_code": "conversation_too_large",
                "error": "You've reached the maximum length for this conversation.",
            },
        }
    )
    assert error_normalizer.whole_turn_terminal_seen is True


def test_post_final_stop_waits_for_whole_turn_marker_or_bounded_fallback() -> None:
    normalizer = CanonicalTopicStreamNormalizer()
    now = [10.0]
    should_stop = _make_passive_terminal_stop_check(
        lambda: normalizer.turn_completed,
        settled=lambda: normalizer.whole_turn_terminal_seen,
        settle_seconds=5.0,
        monotonic=lambda: now[0],
    )
    normalizer.turn_completed = True

    assert should_stop() is False
    normalizer.segment_kind = None
    now[0] = 12.0
    assert should_stop() is False

    normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {"type": "message_stream_complete"},
        }
    )
    assert should_stop() is True

    fallback = CanonicalTopicStreamNormalizer()
    fallback.turn_completed = True
    now[0] = 20.0
    fallback_stop = _make_passive_terminal_stop_check(
        lambda: fallback.turn_completed,
        settled=lambda: fallback.whole_turn_terminal_seen,
        settle_seconds=5.0,
        monotonic=lambda: now[0],
    )
    assert fallback_stop() is False
    now[0] = 25.0
    assert fallback_stop() is True


def test_topic_stream_normalizer_replays_only_new_answer_delta_and_live_tool() -> None:
    normalizer = CanonicalTopicStreamNormalizer(
        emitted_message_ids=("tool-old",),
        answer_message_id="assistant-1",
        answer_text="Hello",
    )

    initial = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "assistant-1",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "content": {"content_type": "text", "parts": ["Hello"]},
                        "metadata": {"turn_exchange_id": "turn-1"},
                    }
                }
            },
        }
    )
    delta = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {"p": "/message/content/parts/0", "v": " world"},
        }
    )
    tool = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "offset": "1789764066954-0",
            "parsed": {
                "v": {
                    "message": {
                        "id": "tool-new",
                        "author": {"role": "assistant"},
                        "recipient": "api_tool.call_tool",
                        "content": {
                            "content_type": "code",
                            "parts": ['{"path":"search","args":{"query":"needle"}}'],
                        },
                        "metadata": {"turn_exchange_id": "turn-1"},
                    }
                }
            },
        }
    )
    duplicate_tool = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "tool-new",
                        "author": {"role": "assistant"},
                        "recipient": "api_tool.call_tool",
                        "status": "finished_successfully",
                        "content": {
                            "content_type": "code",
                            "parts": ['{"path":"search","args":{"query":"needle"}}'],
                        },
                        "metadata": {"turn_exchange_id": "turn-1"},
                    }
                }
            },
        }
    )

    assert initial == []
    assert delta == [
        {
            "type": "assistant_text_delta",
            "message_id": "assistant-1",
            "sequence": 1,
            "delta": " world",
        }
    ]
    assert len(tool) == 1
    assert tool[0]["type"] == "canonical_intermediate_message"
    assert tool[0]["message_kind"] == "tool_call"
    assert tool[0]["message_id"] == "tool-new"
    assert tool[0]["tool_name"] == "api_tool.call_tool"
    assert tool[0]["label"] == "Searching needle..."
    assert tool[0]["source_offset"] == "1789764066954-0"
    assert duplicate_tool == []


def test_topic_stream_normalizer_does_not_rewind_seeded_answer_during_catchup() -> None:
    normalizer = CanonicalTopicStreamNormalizer(
        answer_message_id="assistant-1",
        answer_text="Hello world",
    )

    assert (
        normalizer.feed_transport_event(
            {
                "type": "stream_handoff_ws_subscribed",
                "catchup_count": 2,
            }
        )
        == []
    )
    assert (
        normalizer.feed_transport_event(
            {
                "type": "raw_ws_event",
                "parsed": {
                    "v": {
                        "message": {
                            "id": "assistant-1",
                            "author": {"role": "assistant"},
                            "recipient": "all",
                            "content": {"content_type": "text", "parts": ["Hello"]},
                            "metadata": {"turn_exchange_id": "turn-1"},
                        }
                    }
                },
            }
        )
        == []
    )
    assert (
        normalizer.feed_transport_event(
            {
                "type": "raw_ws_event",
                "parsed": {"p": "/message/content/parts/0", "v": " world"},
            }
        )
        == []
    )
    assert normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {"p": "/message/content/parts/0", "v": "!"},
        }
    ) == [
        {
            "type": "assistant_text_delta",
            "message_id": "assistant-1",
            "sequence": 1,
            "delta": "!",
        }
    ]


def test_topic_stream_normalizer_flushes_completed_thinking_before_tool() -> None:
    normalizer = CanonicalTopicStreamNormalizer()

    thinking = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "thinking-1",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "content": {
                            "content_type": "text",
                            "parts": ["Inspecting state"],
                        },
                        "metadata": {
                            "is_thinking_preamble_message": True,
                            "turn_exchange_id": "turn-1",
                        },
                    }
                }
            },
        }
    )
    tool = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "tool-1",
                        "author": {"role": "assistant"},
                        "recipient": "functions.exec",
                        "status": "finished_successfully",
                        "content": {"content_type": "text", "parts": ["{}"]},
                        "metadata": {"turn_exchange_id": "turn-1"},
                    }
                }
            },
        }
    )

    assert thinking == []
    assert [event["message_kind"] for event in tool] == [
        "assistant_progress",
        "tool_call",
    ]
    assert tool[0]["text"] == "Inspecting state"


def test_topic_stream_normalizer_preserves_commentary_between_tools_and_final() -> None:
    normalizer = CanonicalTopicStreamNormalizer()

    commentary = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "commentary-1",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "channel": "commentary",
                        "status": "finished_successfully",
                        "content": {
                            "content_type": "text",
                            "parts": ["Visible progress before the tool."],
                        },
                        "metadata": {
                            "is_thinking_preamble_message": True,
                            "is_visually_hidden_from_conversation": True,
                            "turn_exchange_id": "turn-1",
                        },
                        "end_turn": False,
                    }
                }
            },
        }
    )
    tool = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "tool-1",
                        "author": {"role": "assistant"},
                        "recipient": "functions.exec",
                        "status": "finished_successfully",
                        "content": {"content_type": "text", "parts": ["{}"]},
                        "metadata": {"turn_exchange_id": "turn-1"},
                        "end_turn": False,
                    }
                }
            },
        }
    )
    commentary_after_tool = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "commentary-2",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "status": "finished_successfully",
                        "content": {
                            "content_type": "text",
                            "parts": ["Visible progress after the tool."],
                        },
                        "metadata": {
                            "output_channel": "commentary",
                            "turn_exchange_id": "turn-1",
                        },
                        "end_turn": False,
                    }
                }
            },
        }
    )
    final = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "answer-1",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "channel": "final",
                        "status": "in_progress",
                        "content": {"content_type": "text", "parts": ["Final answer"]},
                        "metadata": {"turn_exchange_id": "turn-1"},
                        "end_turn": False,
                    }
                }
            },
        }
    )

    assert commentary == [
        {
            "type": "canonical_intermediate_message",
            "message_id": "commentary-1",
            "message_kind": "commentary",
            "text": "Visible progress before the tool.",
            "label": None,
            "tool_name": None,
        }
    ]
    assert tool[0]["message_kind"] == "tool_call"
    assert commentary_after_tool == [
        {
            "type": "canonical_intermediate_message",
            "message_id": "commentary-2",
            "message_kind": "commentary",
            "text": "Visible progress after the tool.",
            "label": None,
            "tool_name": None,
        }
    ]
    assert final == [
        {
            "type": "assistant_text_delta",
            "message_id": "answer-1",
            "sequence": 1,
            "delta": "Final answer",
        }
    ]
    assert normalizer.answer_message_id == "answer-1"
    assert normalizer.turn_completed is False
    assert normalizer.feed_transport_event({"type": "raw_ws_done"}) == []
    assert normalizer.turn_completed is False
    assert normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {"p": "/message/end_turn", "v": True},
        }
    ) == []
    assert normalizer.turn_completed is True


def test_topic_stream_normalizer_emits_hidden_commentary_when_patch_completes() -> None:
    normalizer = CanonicalTopicStreamNormalizer()

    started = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "commentary-patched-1",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "channel": "commentary",
                        "status": "in_progress",
                        "content": {
                            "content_type": "text",
                            "parts": ["Qualification mismatch исчез"],
                        },
                        "metadata": {
                            "is_thinking_preamble_message": True,
                            "is_visually_hidden_from_conversation": True,
                            "turn_exchange_id": "turn-1",
                        },
                        "end_turn": False,
                    }
                }
            },
        }
    )
    completed = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "p": "/message/status",
                "v": "finished_successfully",
            },
        }
    )

    assert started == []
    assert completed == [
        {
            "type": "canonical_intermediate_message",
            "message_id": "commentary-patched-1",
            "message_kind": "commentary",
            "text": "Qualification mismatch исчез",
            "label": None,
            "tool_name": None,
        }
    ]


def test_topic_stream_normalizer_preserves_hidden_tool_error_result() -> None:
    normalizer = CanonicalTopicStreamNormalizer()

    output = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "tool-error-1",
                        "author": {
                            "role": "tool",
                            "name": "api_tool.call_tool",
                            "metadata": {},
                        },
                        "recipient": "all",
                        "channel": "commentary",
                        "status": "finished_successfully",
                        "content": {
                            "content_type": "text",
                            "parts": [
                                '{"codexpro_tool":"apply_patch",'
                                '"error":"CodexProError: error: corrupt patch at line 13",'
                                '"is_error":true}'
                            ],
                        },
                        "metadata": {
                            "is_visually_hidden_from_conversation": True,
                            "turn_exchange_id": "turn-1",
                        },
                    }
                }
            },
        }
    )

    assert len(output) == 1
    event = output[0]
    assert event["type"] == "canonical_intermediate_message"
    assert event["message_id"] == "tool-error-1"
    assert event["message_kind"] == "tool_result"
    assert event["label"] is None
    assert event["tool_name"] == "api_tool.call_tool"
    assert json.loads(event["text"]) == {
        "codexpro_tool": "apply_patch",
        "error": "CodexProError: error: corrupt patch at line 13",
        "is_error": True,
    }


def test_topic_stream_normalizer_still_filters_hidden_non_commentary() -> None:
    normalizer = CanonicalTopicStreamNormalizer()

    hidden = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "hidden-internal-1",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "channel": "final",
                        "status": "finished_successfully",
                        "content": {
                            "content_type": "text",
                            "parts": ["Internal hidden text."],
                        },
                        "metadata": {
                            "is_visually_hidden_from_conversation": True,
                            "turn_exchange_id": "turn-1",
                        },
                        "end_turn": False,
                    }
                }
            },
        }
    )

    assert hidden == []
    assert "hidden-internal-1" not in normalizer.emitted_message_ids
    assert normalizer.answer_message_id is None


def test_canonical_commentary_is_intermediate_and_never_answer_seed() -> None:
    payload = {
        "current_node": "answer-1",
        "mapping": {
            "commentary-1": {
                "parent": None,
                "children": ["answer-1"],
                "message": {
                    "id": "commentary-1",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "status": "finished_successfully",
                    "content": {"content_type": "text", "parts": ["Visible progress"]},
                    "metadata": {
                        "message_channel": "commentary",
                        "turn_exchange_id": "turn-1",
                    },
                    "end_turn": False,
                },
            },
            "answer-1": {
                "parent": "commentary-1",
                "children": [],
                "message": {
                    "id": "answer-1",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "channel": "final",
                    "status": "finished_successfully",
                    "content": {"content_type": "text", "parts": ["Final answer"]},
                    "metadata": {"turn_exchange_id": "turn-1"},
                    "end_turn": True,
                },
            },
        },
    }
    emitted: set[str] = set()

    events = _canonical_intermediate_events(
        payload,
        baseline_message_ids=frozenset(),
        emitted_message_ids=emitted,
        submission_id="submission-1",
    )

    assert events == [
        {
            "type": "canonical_intermediate_message",
            "message_id": "commentary-1",
            "message_kind": "commentary",
            "turn_exchange_id": "turn-1",
            "text": "Visible progress",
            "label": None,
            "tool_name": None,
            "submission_id": "submission-1",
        }
    ]
    assert _canonical_stream_answer_seed(payload, turn_exchange_id="turn-1") == (
        "answer-1",
        "Final answer",
    )


def test_topic_stream_normalizer_emits_public_thought_summary_without_reasoning_title() -> (
    None
):
    normalizer = CanonicalTopicStreamNormalizer()

    events = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "thought-1",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "status": "finished_successfully",
                        "content": {
                            "content_type": "thoughts",
                            "parts": ["private raw reasoning"],
                            "thoughts": [
                                {
                                    "summary": "Visible reasoning update",
                                    "content": "private hidden reasoning",
                                    "finished": True,
                                }
                            ],
                        },
                        "metadata": {"turn_exchange_id": "turn-1"},
                    }
                }
            },
        }
    )

    assert events == [
        {
            "type": "canonical_intermediate_message",
            "message_id": "thought-1",
            "message_kind": "reasoning",
            "text": "Visible reasoning update",
            "label": None,
            "tool_name": None,
        }
    ]
    assert "private" not in repr(events)


def test_topic_stream_normalizer_reconstructs_public_thought_summary_patches() -> None:
    normalizer = CanonicalTopicStreamNormalizer()

    assert (
        normalizer.feed_transport_event(
            {
                "type": "raw_ws_event",
                "parsed": {
                    "v": {
                        "message": {
                            "id": "thought-1",
                            "author": {"role": "assistant"},
                            "recipient": "all",
                            "status": "in_progress",
                            "content": {
                                "content_type": "thoughts",
                                "thoughts": [
                                    {
                                        "summary": "Inspect",
                                        "content": "private hidden reasoning",
                                        "finished": False,
                                    }
                                ],
                            },
                            "metadata": {"turn_exchange_id": "turn-1"},
                        }
                    }
                },
            }
        )
        == []
    )
    assert (
        normalizer.feed_transport_event(
            {
                "type": "raw_ws_event",
                "parsed": {
                    "p": "/message/content/thoughts/0/summary",
                    "v": "ing state",
                },
            }
        )
        == []
    )
    events = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {"p": "/message/status", "v": "finished_successfully"},
        }
    )

    assert events == [
        {
            "type": "canonical_intermediate_message",
            "message_id": "thought-1",
            "message_kind": "reasoning",
            "text": "Inspecting state",
            "label": None,
            "tool_name": None,
        }
    ]
    assert "private" not in repr(events)


def test_topic_stream_normalizer_does_not_complete_on_segment_done_before_tool_chain() -> None:
    normalizer = CanonicalTopicStreamNormalizer()

    events = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "answer-early",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "channel": "final",
                        "status": "finished_successfully",
                        "content": {"content_type": "text", "parts": ["Early text"]},
                        "metadata": {"turn_exchange_id": "turn-1"},
                        "end_turn": False,
                    }
                }
            },
        }
    )
    assert events == [
        {
            "type": "assistant_text_delta",
            "message_id": "answer-early",
            "sequence": 1,
            "delta": "Early text",
        }
    ]
    assert normalizer.feed_transport_event({"type": "raw_ws_done"}) == []
    assert normalizer.turn_completed is False

    tool_events = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "tool-1",
                        "author": {"role": "assistant"},
                        "recipient": "api_tool.call_tool",
                        "status": "finished_successfully",
                        "content": {"content_type": "text", "parts": ["{}"]},
                        "metadata": {"turn_exchange_id": "turn-1"},
                        "end_turn": False,
                    }
                }
            },
        }
    )
    assert tool_events[0]["message_kind"] == "tool_call"
    assert normalizer.turn_completed is False

    commentary = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "commentary-1",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "channel": "commentary",
                        "status": "finished_successfully",
                        "content": {"content_type": "text", "parts": ["Still working"]},
                        "metadata": {"turn_exchange_id": "turn-1"},
                        "end_turn": False,
                    }
                }
            },
        }
    )
    assert commentary[0]["message_kind"] == "commentary"
    assert normalizer.feed_transport_event({"type": "raw_ws_done"}) == []
    assert normalizer.turn_completed is False

    final_events = normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "v": {
                    "message": {
                        "id": "answer-final",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "channel": "final",
                        "status": "finished_successfully",
                        "content": {"content_type": "text", "parts": ["Complete final answer"]},
                        "metadata": {"turn_exchange_id": "turn-1"},
                        "end_turn": True,
                    }
                }
            },
        }
    )
    assert final_events[-1]["type"] in {"assistant_text_delta", "assistant_text_revision"}
    assert normalizer.answer_message_id == "answer-final"
    assert normalizer.answer_text == "Complete final answer"
    assert normalizer.turn_completed is True


def test_canonical_intermediate_events_emit_completed_blocks_and_redact_sensitive_fields() -> (
    None
):
    sensitive_key = "access_" + "token"
    sensitive_value = "hide-" + "this-value"

    def node(node_id, parent, message):
        return {"id": node_id, "parent": parent, "children": [], "message": message}

    mapping = {
        "user": node(
            "user",
            None,
            {
                "id": "m-user",
                "author": {"role": "user"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": ["work"]},
            },
        ),
        "preamble": node(
            "preamble",
            "user",
            {
                "id": "m-preamble",
                "author": {"role": "assistant"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": ["Reading files…"]},
                "metadata": {"is_thinking_preamble_message": True},
                "end_turn": False,
            },
        ),
        "call": node(
            "call",
            "preamble",
            {
                "id": "m-call",
                "author": {"role": "assistant"},
                "recipient": "api_tool.call_tool",
                "create_time": 1789764263.550104,
                "content": {
                    "content_type": "code",
                    "text": json.dumps(
                        {
                            "args": {
                                "path": "/tmp/readme",
                                sensitive_key: sensitive_value,
                            }
                        }
                    ),
                },
                "metadata": {"tool_invoking_message": "Reading README…"},
                "end_turn": False,
            },
        ),
        "result": node(
            "result",
            "call",
            {
                "id": "m-result",
                "author": {"role": "tool", "name": "api_tool.call_tool"},
                "recipient": "all",
                "content": {"content_type": "code", "text": json.dumps({"ok": True})},
                "metadata": {"tool_invoked_message": "README read"},
            },
        ),
        "thoughts": node(
            "thoughts",
            "result",
            {
                "id": "m-thoughts",
                "author": {"role": "assistant"},
                "recipient": "all",
                "content": {
                    "content_type": "thoughts",
                    "parts": ["private raw reasoning"],
                },
                "metadata": {"reasoning_title": "Checking context"},
                "end_turn": False,
            },
        ),
        "recap": node(
            "recap",
            "thoughts",
            {
                "id": "m-recap",
                "author": {"role": "assistant"},
                "recipient": "all",
                "content": {
                    "content_type": "reasoning_recap",
                    "parts": ["Worked for 12s"],
                },
                "end_turn": False,
            },
        ),
        "final": node(
            "final",
            "recap",
            {
                "id": "m-final",
                "author": {"role": "assistant"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": ["done"]},
                "end_turn": True,
            },
        ),
    }
    payload = {
        "conversation_id": "conversation-1",
        "current_node": "final",
        "mapping": mapping,
    }
    emitted = set()

    events = _canonical_intermediate_events(
        payload,
        baseline_message_ids=frozenset(),
        emitted_message_ids=emitted,
        submission_id="submission-1",
    )

    assert [event["message_kind"] for event in events] == [
        "assistant_progress",
        "tool_call",
        "tool_result",
        "reasoning",
        "reasoning",
    ]
    assert events[0]["text"] == "Reading files…"
    assert events[1]["label"] == "Reading README…"
    assert events[1]["source_time_ms"] == 1789764263550
    assert sensitive_value not in events[1]["text"]
    assert "[REDACTED]" in events[1]["text"]
    assert events[2]["label"] == "README read"
    assert events[3]["label"] == "Checking context"
    assert events[3]["text"] == ""
    assert "private raw reasoning" not in repr(events)
    assert events[4]["text"] == "Worked for 12s"
    assert "m-final" not in emitted


def test_canonical_intermediate_events_emit_public_thought_summary_without_title() -> (
    None
):
    payload = {
        "conversation_id": "conversation-1",
        "current_node": "final",
        "mapping": {
            "thoughts": {
                "id": "thoughts",
                "parent": None,
                "children": ["final"],
                "message": {
                    "id": "m-thoughts-public",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "status": "finished_successfully",
                    "content": {
                        "content_type": "thoughts",
                        "parts": ["private raw reasoning"],
                        "thoughts": [
                            {
                                "summary": "Visible reasoning update",
                                "content": "private hidden reasoning",
                                "finished": True,
                            }
                        ],
                    },
                    "metadata": {},
                    "end_turn": False,
                },
            },
            "final": {
                "id": "final",
                "parent": "thoughts",
                "children": [],
                "message": {
                    "id": "m-final",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["done"]},
                    "metadata": {},
                    "end_turn": True,
                },
            },
        },
    }
    emitted: set[str] = set()

    events = _canonical_intermediate_events(
        payload,
        baseline_message_ids=frozenset(),
        emitted_message_ids=emitted,
        submission_id="submission-1",
    )

    assert events == [
        {
            "type": "canonical_intermediate_message",
            "message_id": "m-thoughts-public",
            "message_kind": "reasoning",
            "turn_exchange_id": None,
            "text": "Visible reasoning update",
            "label": None,
            "tool_name": None,
            "submission_id": "submission-1",
        }
    ]
    assert "private" not in repr(events)


def test_unlabeled_tool_calls_get_concise_context_and_plain_thoughts_are_suppressed() -> (
    None
):
    payload = {
        "conversation_id": "conversation-1",
        "current_node": "thoughts",
        "mapping": {
            "call": {
                "id": "call",
                "parent": None,
                "children": ["thoughts"],
                "message": {
                    "id": "m-call",
                    "author": {"role": "assistant"},
                    "recipient": "api_tool.call_tool",
                    "content": {
                        "content_type": "code",
                        "text": json.dumps(
                            {
                                "path": "/CodexTool/link_x/read",
                                "args": {"workspace_id": "ws-1", "path": "README.md"},
                            }
                        ),
                    },
                    "metadata": {},
                    "end_turn": False,
                },
            },
            "thoughts": {
                "id": "thoughts",
                "parent": "call",
                "children": [],
                "message": {
                    "id": "m-thoughts-plain",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {
                        "content_type": "thoughts",
                        "parts": ["private raw reasoning"],
                    },
                    "metadata": {},
                    "end_turn": False,
                },
            },
        },
    }
    emitted: set[str] = set()

    events = _canonical_intermediate_events(
        payload,
        baseline_message_ids=frozenset(),
        emitted_message_ids=emitted,
        submission_id="submission-1",
    )

    assert [event["message_kind"] for event in events] == ["tool_call"]
    assert events[0]["label"] == "Reading README.md..."
    assert "private raw reasoning" not in repr(events)


def test_current_canonical_progress_waits_for_revision_completion() -> None:
    payload = {
        "conversation_id": "conversation-1",
        "current_node": "progress",
        "mapping": {
            "user": {
                "id": "user",
                "parent": None,
                "children": ["progress"],
                "message": {
                    "id": "m-user",
                    "author": {"role": "user"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["work"]},
                },
            },
            "progress": {
                "id": "progress",
                "parent": "user",
                "children": [],
                "message": {
                    "id": "m-progress",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["Первый"]},
                    "metadata": {"is_thinking_preamble_message": True},
                    "end_turn": False,
                },
            },
        },
    }
    emitted: set[str] = set()

    first = _canonical_intermediate_events(
        payload,
        baseline_message_ids=frozenset({"m-user"}),
        emitted_message_ids=emitted,
        submission_id="submission-1",
    )
    assert first == []

    payload["mapping"]["progress"]["message"]["content"]["parts"] = [
        "Первый нюанс уже появился на уровне инструмента: читаю файл диапазонами."
    ]
    second = _canonical_intermediate_events(
        payload,
        baseline_message_ids=frozenset({"m-user"}),
        emitted_message_ids=emitted,
        submission_id="submission-1",
    )
    assert second == []
    assert "m-progress" not in emitted

    payload["mapping"]["progress"]["children"] = ["call"]
    payload["mapping"]["call"] = {
        "id": "call",
        "parent": "progress",
        "children": [],
        "message": {
            "id": "m-call",
            "author": {"role": "assistant"},
            "recipient": "api_tool.call_tool",
            "content": {"content_type": "code", "text": "{}"},
            "metadata": {},
            "end_turn": False,
        },
    }
    payload["current_node"] = "call"

    third = _canonical_intermediate_events(
        payload,
        baseline_message_ids=frozenset({"m-user"}),
        emitted_message_ids=emitted,
        submission_id="submission-1",
    )
    assert third[0]["message_kind"] == "assistant_progress"
    assert third[0]["text"] == (
        "Первый нюанс уже появился на уровне инструмента: читаю файл диапазонами."
    )
    assert "m-progress" in emitted


def test_completed_current_reasoning_summary_is_safe_to_emit() -> None:
    payload = {
        "conversation_id": "conversation-1",
        "current_node": "thoughts",
        "mapping": {
            "thoughts": {
                "id": "thoughts",
                "parent": None,
                "children": [],
                "message": {
                    "id": "m-thoughts",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "status": "finished_successfully",
                    "content": {
                        "content_type": "thoughts",
                        "thoughts": [
                            {
                                "summary": "Visible completed summary",
                                "content": "internal-only-content",
                                "finished": True,
                            }
                        ],
                    },
                    "metadata": {},
                    "end_turn": False,
                },
            }
        },
    }
    emitted: set[str] = set()

    events = _canonical_intermediate_events(
        payload,
        baseline_message_ids=frozenset(),
        emitted_message_ids=emitted,
        submission_id="submission-1",
    )

    assert len(events) == 1
    assert events[0]["message_id"] == "m-thoughts"
    assert events[0]["message_kind"] == "reasoning"
    assert events[0]["text"] == "Visible completed summary"


def test_passive_observer_waits_without_canonical_polling_then_reconciles_once() -> (
    None
):
    class Provider(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.observe_calls = []

        def send_text(self, text, *, conversation=None, timeout=None):
            self.normal_calls.append((text, conversation, timeout))
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id=None,
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=17,
                tab_was_active=False,
                elapsed_ms=50,
                browser_authority_lease_id="lease-1",
                passive_observer_armed=True,
            )

        def observe_turn(
            self,
            *,
            conversation_id,
            turn_exchange_id,
            browser_authority_lease_id,
            timeout,
            on_event=None,
        ):
            self.observe_calls.append(
                (conversation_id, turn_exchange_id, browser_authority_lease_id, timeout)
            )
            if on_event is not None:
                on_event(
                    {
                        "type": "canonical_intermediate_message",
                        "message_kind": "assistant_progress",
                        "text": "Working passively",
                        "source": "passive_page_stream",
                    }
                )
            return {
                "ok": True,
                "conversationId": conversation_id,
                "turnExchangeId": "turn-passive",
                "messageId": "assistant-1",
                "finishReason": "stop",
            }

    provider = Provider()

    class Client:
        _browser_native_turn_provider = provider

        def __init__(self) -> None:
            self.reads = 0
            self.events = []

        def _emit_event(self, callback, event_type, **payload):
            self.events.append((event_type, payload))
            if callback is not None:
                callback({"type": event_type, **payload})

        def _get_conversation_payload(self, conversation_id):
            assert provider.observe_calls, (
                "canonical read happened before passive terminal"
            )
            self.reads += 1
            return _completed_canonical_payload()

    client = Client()
    delivered = []
    response = send_browser_native(
        client,
        "hello",
        timeout=2,
        poll_interval=0.01,
        on_event=delivered.append,
    )

    assert response.text == "done"
    assert response.request.turn_exchange_id == "turn-passive"
    assert len(provider.observe_calls) == 1
    assert provider.observe_calls[0][1] is None
    assert client.reads == 1
    assert any(
        event.get("type") == "canonical_intermediate_message"
        and event.get("text") == "Working passively"
        for event in delivered
    )


def test_passive_canonical_snapshots_feed_revision_safe_text_stream() -> None:
    partial = _completed_canonical_payload()
    partial["mapping"]["assistant-node"]["message"]["content"]["parts"] = ["do"]
    partial["mapping"]["assistant-node"]["message"]["metadata"] = {}
    partial["mapping"]["assistant-node"]["message"]["end_turn"] = False
    final = _completed_canonical_payload()

    class Provider(FakeProvider):
        def send_text(self, text, *, conversation=None, timeout=None):
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id=None,
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=None,
                tab_was_active=False,
                elapsed_ms=50,
                browser_authority_lease_id="lease-1",
                passive_observer_armed=True,
            )

        def observe_turn(self, **kwargs):
            callback = kwargs.get("on_event")
            assert callback is not None
            callback({"type": "canonical_payload_snapshot", "payload": partial})
            callback({"type": "canonical_payload_snapshot", "payload": final})
            return {
                "ok": True,
                "conversationId": kwargs["conversation_id"],
                "turnExchangeId": None,
                "messageId": "assistant-1",
                "finishReason": "stop",
            }

    provider = Provider()

    class Client:
        _browser_native_turn_provider = provider

        def _emit_event(self, callback, event_type, **payload):
            if callback is not None:
                callback({"type": event_type, **payload})

        def _get_conversation_payload(self, _conversation_id):
            return final

    delivered = []
    response = send_browser_native(
        Client(),
        "hello",
        timeout=2,
        poll_interval=0.01,
        on_event=delivered.append,
    )

    assert response.text == "done"
    snapshots = [
        event for event in delivered if event.get("type") == "assistant_text_snapshot"
    ]
    deltas = [
        event for event in delivered if event.get("type") == "assistant_text_delta"
    ]
    assert snapshots and snapshots[0]["text"] == "do"
    assert deltas and deltas[0]["delta"] == "ne"
    finalized = [
        event for event in delivered if event.get("type") == "canonical_text_finalized"
    ][-1]
    assert finalized["streamed_text_length"] == 4
    assert finalized["reconciliation"] == "EXACT_MATCH"


def test_streaming_write_can_handoff_to_passive_canonical_text_without_restart() -> (
    None
):
    partial = _completed_canonical_payload()
    partial["mapping"]["assistant-node"]["message"]["content"]["parts"] = ["done"]
    partial["mapping"]["assistant-node"]["message"]["metadata"] = {}
    partial["mapping"]["assistant-node"]["message"]["end_turn"] = False
    final = _completed_canonical_payload()

    class Provider(FakeProvider):
        revision_safe_streaming_supported = True

        def send_text_streaming(
            self,
            text,
            *,
            conversation=None,
            timeout=None,
            on_text_event,
        ):
            on_text_event(
                {
                    "type": "assistant_text_snapshot",
                    "sequence": 1,
                    "message_id": "assistant-1",
                    "text": "do",
                }
            )
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id=None,
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=None,
                tab_was_active=False,
                elapsed_ms=50,
                browser_authority_lease_id="lease-1",
                passive_observer_armed=True,
            )

        def observe_turn(self, **kwargs):
            callback = kwargs.get("on_event")
            assert callback is not None
            callback({"type": "canonical_payload_snapshot", "payload": partial})
            callback({"type": "canonical_payload_snapshot", "payload": final})
            return {
                "ok": True,
                "conversationId": kwargs["conversation_id"],
                "turnExchangeId": None,
                "messageId": "assistant-1",
                "finishReason": "stop",
            }

    provider = Provider()

    class Client:
        _browser_native_turn_provider = provider

        def _emit_event(self, callback, event_type, **payload):
            if callback is not None:
                callback({"type": event_type, **payload})

        def _get_conversation_payload(self, _conversation_id):
            return final

    delivered = []
    response = send_browser_native(
        Client(),
        "hello",
        timeout=2,
        poll_interval=0.01,
        on_event=delivered.append,
    )

    assert response.text == "done"
    text_events = [
        event
        for event in delivered
        if event.get("type")
        in {
            "assistant_text_snapshot",
            "assistant_text_delta",
            "assistant_text_revision",
        }
    ]
    assert [event["type"] for event in text_events] == [
        "assistant_text_snapshot",
        "assistant_text_delta",
    ]
    assert text_events[0]["sequence"] == 1
    assert text_events[0]["text"] == "do"
    assert text_events[1]["sequence"] == 2
    assert text_events[1]["delta"] == "ne"
    finalized = [
        event for event in delivered if event.get("type") == "canonical_text_finalized"
    ][-1]
    assert finalized["streamed_text_length"] == 4
    assert finalized["stream_delivery_incomplete"] is False
    assert finalized["reconciliation"] == "EXACT_MATCH"


def test_explicit_stop_signal_short_circuits_non_passive_canonical_wait() -> None:
    class Provider:
        def __init__(self) -> None:
            self.stopped = True

        def send_text(self, text, *, conversation=None, timeout=None):
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id="turn-1",
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=17,
                tab_was_active=False,
                elapsed_ms=50,
                passive_observer_armed=False,
            )

        def stop_requested_for(self, conversation_id):
            assert conversation_id == "conversation-1"
            return self.stopped

        def clear_stop_requested_for(self, conversation_id):
            assert conversation_id == "conversation-1"
            self.stopped = False

    provider = Provider()

    class Client:
        _browser_native_turn_provider = provider

        def _emit_event(self, callback, event_type, **payload):
            if callback is not None:
                callback({"type": event_type, **payload})

        def _get_conversation_payload(self, _conversation_id):
            raise AssertionError("explicit Stop must skip canonical polling")

    response = send_browser_native(Client(), "hello", timeout=30, poll_interval=5)

    assert response.text == ""
    assert response.conversation.finish_reason == "stopped"
    assert provider.stopped is False


def test_stopped_passive_observer_accepts_unfinished_canonical_partial(
    monkeypatch,
) -> None:
    class Provider(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.stopped = True

        def send_text(self, text, *, conversation=None, timeout=None):
            self.normal_calls.append((text, conversation, timeout))
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id=None,
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=17,
                tab_was_active=False,
                elapsed_ms=50,
                browser_authority_lease_id="lease-1",
                passive_observer_armed=True,
            )

        def observe_turn(self, **kwargs):
            return {
                "ok": True,
                "conversationId": kwargs["conversation_id"],
                "turnExchangeId": "turn-stopped",
                "messageId": "assistant-partial",
                "finishReason": "stopped",
            }

        def stop_requested_for(self, conversation_id):
            assert conversation_id == "conversation-1"
            return self.stopped

        def clear_stop_requested_for(self, conversation_id):
            assert conversation_id == "conversation-1"
            self.stopped = False

    partial_payload = {
        "conversation_id": "conversation-1",
        "title": "Stopped chat",
        "current_node": "assistant-node",
        "mapping": {
            "assistant-node": {
                "id": "assistant-node",
                "parent": None,
                "children": [],
                "message": {
                    "id": "assistant-partial",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["saved partial"]},
                    "metadata": {},
                    "end_turn": False,
                },
            }
        },
    }
    provider = Provider()

    class Client:
        _browser_native_turn_provider = provider

        def __init__(self) -> None:
            self.reads = 0
            self.events = []

        def _emit_event(self, callback, event_type, **payload):
            event = {"type": event_type, **payload}
            self.events.append(event)
            if callback is not None:
                callback(event)

        def _get_conversation_payload(self, conversation_id):
            self.reads += 1
            return partial_payload

    monkeypatch.setattr(
        "chatgpt_web_adapter.browser_native_client.time.sleep",
        lambda _seconds: None,
    )
    client = Client()
    response = send_browser_native(client, "hello", timeout=2, poll_interval=0.01)

    assert response.text == "saved partial"
    assert response.title == "Stopped chat"
    assert response.conversation.finish_reason == "stopped"
    assert response.request.turn_exchange_id == "turn-stopped"
    assert client.reads == 1
    assert provider.stopped is False
    readback = [
        event
        for event in client.events
        if event["type"] == "browser_native_readback_completed"
    ][-1]
    assert readback["stopped_by_user"] is True
    assert readback["canonical_finality_proven"] is False


def test_stopped_passive_observer_returns_empty_when_partial_is_not_materialized(
    monkeypatch,
) -> None:
    class Provider(FakeProvider):
        def send_text(self, text, *, conversation=None, timeout=None):
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id=None,
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=17,
                tab_was_active=False,
                elapsed_ms=50,
                browser_authority_lease_id="lease-1",
                passive_observer_armed=True,
            )

        def observe_turn(self, **kwargs):
            return {
                "ok": True,
                "conversationId": kwargs["conversation_id"],
                "turnExchangeId": "turn-stopped",
                "messageId": None,
                "finishReason": "stopped",
            }

    provider = Provider()

    class Client:
        _browser_native_turn_provider = provider

        def _emit_event(self, callback, event_type, **payload):
            if callback is not None:
                callback({"type": event_type, **payload})

    submission = submit_browser_native(Client(), "hello", timeout=2, poll_interval=0.01)

    def timeout_wait(*_args, **_kwargs):
        raise ConversationTimeoutError("no stopped partial yet", timeout=1.0)

    monkeypatch.setattr(
        "chatgpt_web_adapter.browser_native_client._wait_for_new_final_assistant",
        timeout_wait,
    )
    monkeypatch.setattr(
        "chatgpt_web_adapter.browser_native_client.time.sleep",
        lambda _seconds: None,
    )
    response = await_browser_native_final(Client(), submission)

    assert response.text == ""
    assert response.conversation.conversation_id == "conversation-1"
    assert response.conversation.finish_reason == "stopped"


def test_passive_observer_stream_end_without_terminal_returns_incomplete_after_bounded_reconcile(
    monkeypatch,
) -> None:
    class Provider(FakeProvider):
        def send_text(self, text, *, conversation=None, timeout=None):
            self.normal_calls.append((text, conversation, timeout))
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id="turn-1",
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=17,
                tab_was_active=False,
                elapsed_ms=50,
                browser_authority_lease_id="lease-1",
                passive_observer_armed=True,
            )

        def observe_turn(self, **_kwargs):
            raise RequestError(
                "PASSIVE_OBSERVER_STREAM_ENDED_WITHOUT_TERMINAL",
                request_stage="browser_native_observe_turn",
            )

    provider = Provider()

    class Client:
        _browser_native_turn_provider = provider

        def __init__(self) -> None:
            self.events = []

        def _emit_event(self, callback, event_type, **payload):
            event = {"type": event_type, **payload}
            self.events.append(event)
            if callback is not None:
                callback(event)

    submission = submit_browser_native(Client(), "hello", timeout=120, poll_interval=15)
    observed_timeouts = []

    def timeout_wait(*_args, **kwargs):
        observed_timeouts.append(kwargs["timeout"])
        raise ConversationTimeoutError("no final assistant", timeout=kwargs["timeout"])

    monkeypatch.setattr(
        "chatgpt_web_adapter.browser_native_client._wait_for_new_final_assistant",
        timeout_wait,
    )
    client = Client()
    response = await_browser_native_final(client, submission)

    assert response.text == ""
    assert response.conversation.conversation_id == "conversation-1"
    assert response.conversation.finish_reason == "incomplete"
    assert observed_timeouts and observed_timeouts[0] <= 5.0
    readback = [
        event
        for event in client.events
        if event["type"] == "browser_native_readback_completed"
    ][-1]
    assert readback["canonical_finality_proven"] is False
    assert readback["incomplete_without_terminal"] is True


def test_passive_stream_end_without_terminal_accepts_late_canonical_final() -> None:
    class Provider(FakeProvider):
        def send_text(self, text, *, conversation=None, timeout=None):
            self.normal_calls.append((text, conversation, timeout))
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id="turn-1",
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=17,
                tab_was_active=False,
                elapsed_ms=50,
                browser_authority_lease_id="lease-1",
                passive_observer_armed=True,
            )

        def observe_turn(self, **_kwargs):
            raise RequestError(
                "PASSIVE_OBSERVER_STREAM_ENDED_WITHOUT_TERMINAL",
                request_stage="browser_native_observe_turn",
            )

    provider = Provider()

    class Client:
        _browser_native_turn_provider = provider

        def __init__(self) -> None:
            self.events = []

        def _emit_event(self, callback, event_type, **payload):
            event = {"type": event_type, **payload}
            self.events.append(event)
            if callback is not None:
                callback(event)

        def _get_conversation_payload(self, _conversation_id):
            return _completed_canonical_payload()

    client = Client()
    response = send_browser_native(client, "hello", timeout=120, poll_interval=15)

    assert response.text == "done"
    assert response.conversation.finish_reason != "incomplete"
    readback = [
        event
        for event in client.events
        if event["type"] == "browser_native_readback_completed"
    ][-1]
    assert readback["canonical_finality_proven"] is True
    assert readback["incomplete_without_terminal"] is False


def test_passive_observer_stream_start_failure_falls_back_to_bounded_canonical_read() -> (
    None
):
    class Provider(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.observe_calls = 0

        def send_text(self, text, *, conversation=None, timeout=None):
            self.normal_calls.append((text, conversation, timeout))
            return BrowserNativeTurnResult(
                conversation_id="conversation-1",
                turn_exchange_id="turn-1",
                response_status=200,
                response_mime_type="text/event-stream",
                final_url="https://chatgpt.com/c/conversation-1",
                tab_id=17,
                tab_was_active=False,
                elapsed_ms=50,
                browser_authority_lease_id="lease-1",
                passive_observer_armed=True,
            )

        def observe_turn(self, **_kwargs):
            self.observe_calls += 1
            raise RequestError(
                "PASSIVE_OBSERVER_STREAM_NOT_OBSERVED",
                request_stage="browser_native_observe_turn",
            )

    provider = Provider()

    class Client:
        _browser_native_turn_provider = provider

        def __init__(self) -> None:
            self.reads = 0
            self.events = []

        def _emit_event(self, callback, event_type, **payload):
            self.events.append((event_type, payload))
            if callback is not None:
                callback({"type": event_type, **payload})

        def _get_conversation_payload(self, conversation_id):
            self.reads += 1
            return _completed_canonical_payload()

    client = Client()
    delivered = []
    response = send_browser_native(
        client,
        "hello",
        timeout=2,
        poll_interval=0.01,
        on_event=delivered.append,
    )

    assert response.text == "done"
    assert provider.observe_calls == 1
    assert client.reads == 1
    assert any(
        event.get("type") == "browser_native_passive_observer_fallback"
        and "PASSIVE_OBSERVER_STREAM_NOT_OBSERVED" in event.get("reason", "")
        for event in delivered
    )
