from __future__ import annotations

import threading

import chatgpt_web_adapter.browser_native_host as subject
from chatgpt_web_adapter.browser_native_host import BrowserNativeBroker


def test_broker_rejects_wrong_local_token_and_answers_ping(tmp_path) -> None:
    broker = BrowserNativeBroker(state_dir=tmp_path)
    broker.start()
    try:
        denied = broker.handle_local_request(
            {"protocol": 1, "type": "ping", "request_id": "x", "token": "wrong"}
        )
        assert denied["ok"] is False
        assert denied["error"] == "BROWSER_NATIVE_UNAUTHORIZED"

        allowed = broker.handle_local_request(
            {
                "protocol": 1,
                "type": "ping",
                "request_id": "y",
                "token": broker.token,
            }
        )
        assert allowed["ok"] is True
        assert allowed["extensionConnected"] is False
    finally:
        broker.close()


def test_stop_generation_bypasses_active_turn_lock(tmp_path, monkeypatch) -> None:
    broker = BrowserNativeBroker(state_dir=tmp_path)
    broker.start()
    broker.extension_connected = True
    captured: dict[str, object] = {}

    def fake_write(_stream, message) -> None:
        captured.update(message)
        broker.route_native_message(
            {
                "protocol": 1,
                "type": "stop_generation_result",
                "request_id": message["request_id"],
                "ok": True,
                "stopped": True,
                "conversationId": "conversation-1",
                "tabId": 42,
            }
        )

    monkeypatch.setattr(subject, "write_native_message", fake_write)
    broker.turn_lock.acquire()
    try:
        result = broker.handle_local_request(
            {
                "protocol": 1,
                "type": "stop_generation",
                "request_id": "stop-1",
                "token": broker.token,
                "conversationId": "conversation-1",
                "timeoutMs": 1000,
            }
        )
        assert result["ok"] is True
        assert result["stopped"] is True
        assert captured["type"] == "stop_generation"
    finally:
        broker.turn_lock.release()
        broker.close()


def test_stop_generation_wakes_matching_pending_observe_turn(
    tmp_path, monkeypatch
) -> None:
    broker = BrowserNativeBroker(state_dir=tmp_path)
    broker.start()
    broker.extension_connected = True
    observe_forwarded = threading.Event()
    observe_result: dict[str, object] = {}

    def fake_write(_stream, message) -> None:
        if message["type"] == "observe_turn":
            observe_forwarded.set()
            return
        if message["type"] == "stop_generation":
            broker.route_native_message(
                {
                    "protocol": 1,
                    "type": "stop_generation_result",
                    "request_id": message["request_id"],
                    "ok": True,
                    "stopped": True,
                    "conversationId": "conversation-1",
                    "tabId": 42,
                }
            )

    monkeypatch.setattr(subject, "write_native_message", fake_write)
    broker.turn_lock.acquire()
    broker._reserve_authority_for_readback("lease-1")

    def observe() -> None:
        observe_result.update(
            broker.handle_local_request(
                {
                    "protocol": 1,
                    "type": "observe_turn",
                    "request_id": "observe-1",
                    "token": broker.token,
                    "browserAuthorityLeaseId": "lease-1",
                    "conversationId": "conversation-1",
                    "turnExchangeId": "turn-1",
                    "timeoutMs": 30_000,
                }
            )
        )

    thread = threading.Thread(target=observe)
    thread.start()
    try:
        assert observe_forwarded.wait(timeout=1.0)
        stopped = broker.handle_local_request(
            {
                "protocol": 1,
                "type": "stop_generation",
                "request_id": "stop-1",
                "token": broker.token,
                "conversationId": "conversation-1",
                "timeoutMs": 1000,
            }
        )
        assert stopped["ok"] is True
        assert stopped["stopped"] is True
        thread.join(timeout=1.0)
        assert not thread.is_alive()
        assert observe_result["ok"] is True
        assert observe_result["conversationId"] == "conversation-1"
        assert observe_result["turnExchangeId"] == "turn-1"
        assert observe_result["finishReason"] == "stopped"
        assert observe_result["source"] == "explicit_stop_broker"
    finally:
        broker.close()
