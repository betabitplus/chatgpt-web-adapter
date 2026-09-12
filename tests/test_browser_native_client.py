from __future__ import annotations

import json
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
    _wait_for_new_final_assistant,
    await_browser_native_final,
    send_browser_native,
    submit_browser_native,
)
from chatgpt_web_adapter.browser_native_provider import BrowserNativeTurnResult
from chatgpt_web_adapter.exceptions import ConversationTimeoutError, RequestError
from chatgpt_web_adapter.types import ChatConversation


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
    assert response.metrics.backend_status == 200
    assert provider.normal_calls == [("hello", "existing-conversation", 2)]


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


def test_canonical_stream_identity_reconstructs_topic_from_latest_turn_exchange_id() -> None:
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
    assert duplicate_tool == []


def test_topic_stream_normalizer_does_not_rewind_seeded_answer_during_catchup() -> None:
    normalizer = CanonicalTopicStreamNormalizer(
        answer_message_id="assistant-1",
        answer_text="Hello world",
    )

    assert normalizer.feed_transport_event(
        {
            "type": "stream_handoff_ws_subscribed",
            "catchup_count": 2,
        }
    ) == []
    assert normalizer.feed_transport_event(
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
    ) == []
    assert normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {"p": "/message/content/parts/0", "v": " world"},
        }
    ) == []
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
                        "content": {"content_type": "text", "parts": ["Inspecting state"]},
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
    assert [event["message_kind"] for event in tool] == ["assistant_progress", "tool_call"]
    assert tool[0]["text"] == "Inspecting state"


def test_topic_stream_normalizer_emits_public_thought_summary_without_reasoning_title() -> None:
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

    assert normalizer.feed_transport_event(
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
    ) == []
    assert normalizer.feed_transport_event(
        {
            "type": "raw_ws_event",
            "parsed": {
                "p": "/message/content/thoughts/0/summary",
                "v": "ing state",
            },
        }
    ) == []
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
    assert sensitive_value not in events[1]["text"]
    assert "[REDACTED]" in events[1]["text"]
    assert events[2]["label"] == "README read"
    assert events[3]["label"] == "Checking context"
    assert events[3]["text"] == ""
    assert "private raw reasoning" not in repr(events)
    assert events[4]["text"] == "Worked for 12s"
    assert "m-final" not in emitted


def test_canonical_intermediate_events_emit_public_thought_summary_without_title() -> None:
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
