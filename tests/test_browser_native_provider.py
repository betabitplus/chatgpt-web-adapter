from __future__ import annotations

import json
import socket
import threading

from chatgpt_web_adapter.browser_native_protocol import (
    recv_local_message,
    send_local_message,
)
from chatgpt_web_adapter.browser_native_provider import BrowserNativeTurnProvider


def _round_trip(tmp_path, invoke):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    token = "t" * 32
    (tmp_path / "bridge.json").write_text(
        json.dumps(
            {
                "protocol": 1,
                "host": "127.0.0.1",
                "port": listener.getsockname()[1],
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            request = recv_local_message(connection)
            captured.update(request)
            send_local_message(
                connection,
                {
                    "protocol": 1,
                    "type": "turn_result",
                    "request_id": request["request_id"],
                    "ok": True,
                    "conversationId": "conversation-1",
                    "turnExchangeId": "turn-1",
                    "responseStatus": 200,
                    "responseMimeType": "text/event-stream",
                    "finalUrl": "https://chatgpt.com/c/conversation-1",
                    "tabId": 42,
                    "tabWasActive": False,
                    "elapsedMs": 1234,
                    "runtimeReloaded": True,
                    "runtimeReloadMs": 321,
                },
            )
        listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    result = invoke(BrowserNativeTurnProvider(state_dir=tmp_path))
    thread.join(timeout=2)
    return token, captured, result


def test_provider_round_trip_uses_loopback_token_and_safe_result(tmp_path) -> None:
    token, captured, result = _round_trip(
        tmp_path,
        lambda provider: provider.send_text("hello", timeout=2),
    )

    assert captured["token"] == token
    assert captured["conversationId"] is None
    assert captured["canonicalCompleted"] is False
    assert captured["canonicalCompletedAtMs"] is None
    assert result.conversation_id == "conversation-1"
    assert result.turn_exchange_id == "turn-1"
    assert result.response_status == 200
    assert result.tab_was_active is False
    assert result.runtime_reloaded is True
    assert result.runtime_reload_ms == 321


def test_provider_serializes_fresh_canonical_completion_recovery_evidence(tmp_path) -> None:
    _, captured, result = _round_trip(
        tmp_path,
        lambda provider: provider.send_text_with_stale_ui_recovery(
            "hello",
            conversation="conversation-1",
            timeout=2,
            canonical_completed_at_ms=123456,
        ),
    )

    assert captured["conversationId"] == "conversation-1"
    assert captured["canonicalCompleted"] is True
    assert captured["canonicalCompletedAtMs"] == 123456
    assert result.runtime_reloaded is True
    assert result.runtime_reload_ms == 321


def test_provider_requests_passive_observer_for_turn(tmp_path) -> None:
    _, captured, result = _round_trip(
        tmp_path,
        lambda provider: provider.send_text("hello", timeout=2),
    )

    assert captured["passiveObserve"] is True
    assert captured["streamTextObservations"] is False
    assert captured["modelSlug"] is None
    assert result.passive_observer_armed is False


def test_provider_forwards_real_model_slug_without_mapping(tmp_path) -> None:
    _, captured, _result = _round_trip(
        tmp_path,
        lambda provider: provider.send_text("hello", timeout=2, model_slug="gpt-5-6"),
    )

    assert captured["modelSlug"] == "gpt-5-6"


def test_provider_observe_turn_streams_events_then_returns_terminal(tmp_path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    token = "t" * 32
    (tmp_path / "bridge.json").write_text(
        json.dumps(
            {
                "protocol": 1,
                "host": "127.0.0.1",
                "port": listener.getsockname()[1],
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            request = recv_local_message(connection)
            captured.update(request)
            send_local_message(
                connection,
                {
                    "protocol": 1,
                    "type": "turn_event",
                    "request_id": request["request_id"],
                    "event": {
                        "type": "canonical_intermediate_message",
                        "message_kind": "assistant_progress",
                        "text": "Working",
                    },
                },
            )
            send_local_message(
                connection,
                {
                    "protocol": 1,
                    "type": "observe_turn_result",
                    "request_id": request["request_id"],
                    "ok": True,
                    "conversationId": "conversation-1",
                    "turnExchangeId": "turn-1",
                    "messageId": "assistant-1",
                    "finishReason": "stop",
                },
            )
        listener.close()

    events = []
    thread = threading.Thread(target=serve)
    thread.start()
    provider = BrowserNativeTurnProvider(state_dir=tmp_path)
    result = provider.observe_turn(
        conversation_id="conversation-1",
        turn_exchange_id="turn-1",
        browser_authority_lease_id="lease-1",
        timeout=2,
        on_event=events.append,
    )
    thread.join(timeout=2)

    assert captured["type"] == "observe_turn"
    assert captured["conversationId"] == "conversation-1"
    assert captured["turnExchangeId"] == "turn-1"
    assert captured["browserAuthorityLeaseId"] == "lease-1"
    assert events == [
        {
            "type": "canonical_intermediate_message",
            "message_kind": "assistant_progress",
            "text": "Working",
        }
    ]
    assert result["messageId"] == "assistant-1"


def test_provider_stop_generation_uses_out_of_band_rpc(tmp_path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    token = "t" * 32
    (tmp_path / "bridge.json").write_text(
        json.dumps(
            {
                "protocol": 1,
                "host": "127.0.0.1",
                "port": listener.getsockname()[1],
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            request = recv_local_message(connection)
            captured.update(request)
            send_local_message(
                connection,
                {
                    "protocol": 1,
                    "type": "stop_generation_result",
                    "request_id": request["request_id"],
                    "ok": True,
                    "stopped": True,
                    "conversationId": "conversation-1",
                    "tabId": 42,
                },
            )
        listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    provider = BrowserNativeTurnProvider(state_dir=tmp_path)
    result = provider.stop_generation("conversation-1", timeout=2)
    thread.join(timeout=2)

    assert captured["type"] == "stop_generation"
    assert captured["conversationId"] == "conversation-1"
    assert captured["timeoutMs"] == 2000
    assert result["stopped"] is True
    assert result["conversationId"] == "conversation-1"
    assert provider.stop_requested_for("conversation-1") is True
    provider.clear_stop_requested_for("conversation-1")
    assert provider.stop_requested_for("conversation-1") is False
