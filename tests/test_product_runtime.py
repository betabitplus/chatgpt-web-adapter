from __future__ import annotations

from types import SimpleNamespace

import pytest

import chatgpt_web_adapter.product_runtime as product_runtime
from chatgpt_web_adapter.browser_native_provider import BrowserNativeBridgeStatus
from chatgpt_web_adapter.product_runtime import (
    BROWSER_OWNED_PRODUCT_TRANSPORT,
    DEFAULT_PRODUCT_TRANSPORT,
    SUPPORTED_PRODUCT_TRANSPORTS,
    ChatGPTProductRuntime,
    ProductRuntimeExecution,
    assemble_product_runtime,
    normalize_product_transport,
)


class _Provider:
    def __init__(self, *, tab_id: int | None = 41, connected: bool = True) -> None:
        self.tab_id = tab_id
        self.connected = connected
        self.stop_calls: list[tuple[str | None, float]] = []

    def status(self) -> BrowserNativeBridgeStatus:
        return BrowserNativeBridgeStatus(
            available=True,
            extension_connected=self.connected,
            runtime_tab_id=self.tab_id,
        )

    def send_text(self, *args, **kwargs):
        raise AssertionError("test provider write should not be called")

    def stop_generation(self, conversation_id=None, *, timeout=10.0):
        self.stop_calls.append((conversation_id, timeout))
        return {"ok": True, "stopped": True, "conversationId": conversation_id}


class _Client:
    def __init__(self, status: str = "completed") -> None:
        self.status_value = status

    def get_status(self, conversation):
        return SimpleNamespace(status=self.status_value)

    def get_messages(self, conversation, **kwargs):
        return []

    def attach_conversation(self, conversation):
        return SimpleNamespace(conversation_id=conversation)


def test_transport_selection_is_closed_and_browser_owned_by_default() -> None:
    assert DEFAULT_PRODUCT_TRANSPORT == BROWSER_OWNED_PRODUCT_TRANSPORT
    assert SUPPORTED_PRODUCT_TRANSPORTS == ("browser-owned", "browserless-request")
    assert normalize_product_transport(" browser-owned ") == "browser-owned"
    assert normalize_product_transport(" browserless-request ") == "browserless-request"
    with pytest.raises(ValueError, match="unsupported product transport"):
        normalize_product_transport("legacy-direct")


def test_new_chat_readiness_does_not_require_preexisting_runtime_tab() -> None:
    runtime = ChatGPTProductRuntime(_Client(), provider=_Provider(tab_id=None))

    health = runtime.health()

    assert health.transport == "browser-owned"
    assert health.ready is True
    assert health.runtime_tab_id is None
    assert health.runtime_tab_preexisting is False
    assert health.canonical_read_checked is False
    assert health.fallback_transport is None


def test_continuation_requires_canonical_completed_status() -> None:
    client = _Client(status="running")
    runtime = ChatGPTProductRuntime(client, provider=_Provider())

    health = runtime.health("conversation-1")

    assert health.ready is False
    assert health.canonical_status == "running"
    assert health.canonical_read_checked is True


def test_reassembled_runtime_observes_same_external_runtime_tab() -> None:
    provider = _Provider(tab_id=77)
    first = ChatGPTProductRuntime(_Client(), provider=provider)
    second = ChatGPTProductRuntime(_Client(), provider=provider)

    first_health = first.health("conversation-1")
    second_health = second.health("conversation-1")

    assert first_health.runtime_tab_id == 77
    assert second_health.runtime_tab_id == 77
    assert first_health.runtime_tab_preexisting is True
    assert second_health.runtime_tab_preexisting is True


def test_stop_generation_delegates_out_of_band_to_browser_provider() -> None:
    provider = _Provider()
    runtime = ChatGPTProductRuntime(_Client(), provider=provider)

    result = runtime.stop_generation("conversation-1", timeout=3.5)

    assert result["stopped"] is True
    assert provider.stop_calls == [("conversation-1", 3.5)]


def test_runtime_exposes_canonical_conversation_catalog(monkeypatch) -> None:
    runtime = ChatGPTProductRuntime(_Client(), provider=_Provider())
    catalog = [{"id": "conversation-1", "title": "One"}]
    monkeypatch.setattr(
        runtime.canonical,
        "list_conversations",
        lambda: catalog,
        raising=False,
    )

    result = runtime.list_conversations()

    assert result == catalog
    assert result is not catalog
    assert result[0] is not catalog[0]


def test_runtime_exposes_bounded_recent_conversation_catalog(monkeypatch) -> None:
    runtime = ChatGPTProductRuntime(_Client(), provider=_Provider())
    catalog = [{"id": "conversation-1", "title": "One"}]
    calls = []

    def fake_recent(*, limit):
        calls.append(limit)
        return catalog

    monkeypatch.setattr(
        runtime.canonical,
        "list_recent_conversations",
        fake_recent,
        raising=False,
    )

    result = runtime.list_recent_conversations(limit=50)

    assert calls == [50]
    assert result == catalog
    assert result is not catalog
    assert result[0] is not catalog[0]


def test_runtime_exposes_canonical_model_catalog(monkeypatch) -> None:
    runtime = ChatGPTProductRuntime(_Client(), provider=_Provider())
    models = [{"slug": "gpt-5-6-thinking", "title": "GPT-5.6"}]
    monkeypatch.setattr(
        runtime.canonical,
        "list_models",
        lambda: models,
        raising=False,
    )

    result = runtime.list_models()

    assert result == models
    assert result is not models
    assert result[0] is not models[0]


def test_runtime_exposes_canonical_conversation_snapshot(monkeypatch) -> None:
    runtime = ChatGPTProductRuntime(_Client(), provider=_Provider())
    snapshot = {"status": object(), "messages": [object()]}
    calls = []

    def fake_snapshot(conversation, **kwargs):
        calls.append((conversation, kwargs))
        return snapshot

    monkeypatch.setattr(
        runtime.canonical,
        "conversation_snapshot",
        fake_snapshot,
        raising=False,
    )

    result = runtime.conversation_snapshot("conversation-1", limit=25)

    assert result == snapshot
    assert result is not snapshot
    assert calls == [("conversation-1", {"limit": 25})]


def test_runtime_follow_snapshot_reuses_one_canonical_payload(monkeypatch) -> None:
    runtime = ChatGPTProductRuntime(_Client(), provider=_Provider())
    payload = {"current_node": "node-2", "mapping": {}}
    reads = []
    status = SimpleNamespace(status="tool_running")
    messages = [SimpleNamespace(message_id="m2", role="assistant", text="thinking")]

    def fake_payload(_conversation):
        reads.append("payload")
        return payload

    def fake_status(reader, ref):
        assert reader._get_conversation_payload(ref.conversation_id) is not payload
        assert reader._get_conversation_payload(ref.conversation_id) == payload
        return status

    def fake_messages(reader, ref, *, limit=None):
        assert reader._get_conversation_payload(ref.conversation_id) == payload
        assert limit == 32
        return messages

    def fake_events(
        event_payload,
        *,
        baseline_message_ids,
        emitted_message_ids,
        submission_id,
    ):
        assert event_payload == payload
        assert baseline_message_ids == frozenset()
        assert emitted_message_ids == {"m1"}
        assert submission_id is None
        emitted_message_ids.add("m2")
        return [{"type": "canonical_intermediate_message", "message_id": "m2"}]

    monkeypatch.setattr(
        runtime.canonical,
        "get_conversation_payload",
        fake_payload,
        raising=False,
    )
    monkeypatch.setattr(product_runtime, "get_status", fake_status)
    monkeypatch.setattr(product_runtime, "get_messages", fake_messages)
    monkeypatch.setattr(product_runtime, "_canonical_intermediate_events", fake_events)

    result = runtime.conversation_follow_snapshot(
        "conversation-1",
        emitted_message_ids=["m1"],
        limit=32,
    )

    assert reads == ["payload"]
    assert result["status"] is status
    assert result["messages"] is messages
    assert result["events"] == [
        {"type": "canonical_intermediate_message", "message_id": "m2"}
    ]
    assert result["emitted_message_ids"] == ["m1", "m2"]


def test_runtime_topic_follow_streams_events_then_reconciles_once(monkeypatch) -> None:
    provider = _Provider()
    runtime = ChatGPTProductRuntime(_Client(), provider=provider)
    provider_calls = []
    final_calls = []

    def follow_stream_topic(
        *,
        conversation_id,
        topic_id,
        timeout,
        on_event,
        should_stop,
    ):
        provider_calls.append((conversation_id, topic_id, timeout))
        on_event(
            {
                "type": "raw_ws_event",
                "parsed": {
                    "v": {
                        "message": {
                            "id": "tool-1",
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
        on_event({"type": "raw_ws_done", "topic_id": topic_id})
        assert should_stop() is False
        on_event(
            {
                "type": "raw_ws_event",
                "parsed": {
                    "v": {
                        "message": {
                            "id": "assistant-final",
                            "author": {"role": "assistant"},
                            "recipient": "all",
                            "status": "finished_successfully",
                            "end_turn": True,
                            "content": {"content_type": "text", "parts": ["done"]},
                            "metadata": {"turn_exchange_id": "turn-1"},
                        }
                    }
                },
            }
        )
        assert should_stop() is True
        return {"completed": False, "segment_done_count": 1}

    provider.follow_stream_topic = follow_stream_topic
    monkeypatch.setattr(
        runtime,
        "conversation_follow_snapshot",
        lambda conversation, *, emitted_message_ids, limit: (
            final_calls.append((conversation, tuple(emitted_message_ids), limit))
            or {
                "status": SimpleNamespace(status="completed"),
                "messages": [],
                "events": [],
                "emitted_message_ids": list(emitted_message_ids),
            }
        ),
    )
    events = []

    result = runtime.conversation_follow_stream(
        "conversation-1",
        topic_id="conversation-turn-turn-1",
        emitted_message_ids=(),
        timeout=90,
        limit=64,
        on_event=events.append,
    )

    assert provider_calls == [
        ("conversation-1", "conversation-turn-turn-1", 90)
    ]
    assert len(events) == 2
    assert events[0]["message_kind"] == "tool_call"
    assert events[0]["message_id"] == "tool-1"
    assert events[1]["type"] == "assistant_text_delta"
    assert events[1]["message_id"] == "assistant-final"
    assert events[1]["delta"] == "done"
    assert len(final_calls) == 1
    final_ref, final_ids, final_limit = final_calls[0]
    assert getattr(final_ref, "conversation_id", None) == "conversation-1"
    assert final_ids == ("tool-1",)
    assert final_limit == 64
    assert result["stream_completed"] is True
    assert result["stream_topic_id"] == "conversation-turn-turn-1"


def test_runtime_topic_follow_cancelled_skips_final_canonical_read(monkeypatch) -> None:
    provider = _Provider()
    runtime = ChatGPTProductRuntime(_Client(), provider=provider)
    canonical_reads = []

    provider.follow_stream_topic = lambda **_kwargs: {"completed": False}
    monkeypatch.setattr(
        runtime,
        "conversation_follow_snapshot",
        lambda *args, **kwargs: canonical_reads.append((args, kwargs)),
    )
    def stop():
        return True

    result = runtime.conversation_follow_stream(
        "conversation-1",
        topic_id="conversation-turn-turn-1",
        should_stop=stop,
    )

    assert canonical_reads == []
    assert result["stream_completed"] is False
    assert result["stream_cancelled"] is True


def test_runtime_exposes_complete_gptty_read_surface() -> None:
    runtime = ChatGPTProductRuntime(_Client(), provider=_Provider())

    for name in (
        "attach_conversation",
        "get_messages",
        "get_status",
        "list_conversations",
        "list_recent_conversations",
        "list_models",
        "conversation_snapshot",
        "conversation_follow_snapshot",
        "conversation_follow_stream",
        "get_conversation_payload",
    ):
        assert callable(getattr(runtime, name, None)), name


def test_runtime_exposes_raw_canonical_conversation_payload(monkeypatch) -> None:
    runtime = ChatGPTProductRuntime(_Client(), provider=_Provider())
    payload = {"title": "Graph", "mapping": {"node": {}}}
    monkeypatch.setattr(
        runtime.canonical,
        "get_conversation_payload",
        lambda _conversation: payload,
        raising=False,
    )

    result = runtime.get_conversation_payload("conversation-1")

    assert result == payload
    assert result is not payload


def test_send_text_delegates_exactly_once_without_fallback() -> None:
    runtime = ChatGPTProductRuntime(_Client(), provider=_Provider())
    calls = []
    expected = object()

    def fake_send_text(text, **kwargs):
        calls.append((text, kwargs))
        return expected

    runtime._writer.send_text = fake_send_text

    result = runtime.send_text(
        "hello",
        conversation="conversation-1",
        timeout=12.0,
        poll_interval=0.25,
    )

    assert result is expected
    assert calls == [
        (
            "hello",
            {
                "conversation": "conversation-1",
                "timeout": 12.0,
                "poll_interval": 0.25,
                "on_token": None,
                "on_event": None,
            },
        )
    ]
    assert runtime.governance()["fallback_transport"] is None
    assert runtime.governance()["legacy_direct_write_fallback"] is False


def test_send_text_forwards_real_model_slug_without_profile_mapping() -> None:
    runtime = ChatGPTProductRuntime(_Client(), provider=_Provider())
    calls = []
    expected = object()

    def fake_send_text(text, **kwargs):
        calls.append((text, kwargs))
        return expected

    runtime._writer.send_text = fake_send_text

    result = runtime.send_text("hello", model="gpt-5-6")

    assert result is expected
    assert calls == [
        (
            "hello",
            {
                "conversation": None,
                "timeout": 150.0,
                "poll_interval": 0.5,
                "on_token": None,
                "on_event": None,
                "model_slug": "gpt-5-6",
            },
        )
    ]


def test_observed_send_preserves_transport_and_writer_observation() -> None:
    runtime = ChatGPTProductRuntime(_Client(), provider=_Provider())
    response = object()
    observation = SimpleNamespace(to_dict=lambda: {"runtime_tab_id": 77})
    runtime._writer.send_text_observed = lambda *args, **kwargs: SimpleNamespace(
        response=response,
        observation=observation,
    )

    execution = runtime.send_text_observed("hello")

    assert isinstance(execution, ProductRuntimeExecution)
    assert execution.transport == "browser-owned"
    assert execution.response is response
    assert execution.observation is observation


def test_assembly_disables_interactive_login_and_sentinel(monkeypatch) -> None:
    captured = {}

    class FakeClient(_Client):
        def __init__(self, **kwargs):
            super().__init__()
            captured.update(kwargs)

    monkeypatch.setattr(product_runtime, "ChatGPTWebClient", FakeClient)
    provider = _Provider()

    runtime = assemble_product_runtime(
        transport="browser-owned",
        provider=provider,
        auth_file="saved-auth.json",
        client_timeout=33,
    )

    assert isinstance(runtime, ChatGPTProductRuntime)
    assert captured["auth_file"] == "saved-auth.json"
    assert captured["timeout"] == 33
    assert captured["auto_refresh_auth"] is True
    assert captured["auto_login"] is False
    assert captured["auto_sentinel"] is False
