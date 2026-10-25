from __future__ import annotations

import json

from chatgpt_web_adapter.wkwebview_lightweight_transport import WKLightweightTransport


class _SourceClient:
    def wk_transport_headers(self, extra=None):
        return {"authorization": "Bearer test", **dict(extra or {})}


def _transport() -> WKLightweightTransport:
    return WKLightweightTransport(
        _SourceClient(),
        canonical_matches_write=lambda payload, **kwargs: True,
        cache_final_payload=lambda conversation_id, payload: None,
    )


def test_global_completion_subscription_matches_official_no_offset_shape() -> None:
    transport = _transport()

    commands = transport._conversation_completion_commands()

    assert commands == [
        {
            "id": 1,
            "command": {
                "type": "connect",
                "presence": {"type": "presence", "state": "foreground"},
            },
        },
        {
            "id": 2,
            "command": {
                "type": "subscribe",
                "topic_id": "conversations",
            },
        },
    ]
    assert "offset" not in commands[1]["command"]


def test_global_completion_parser_and_waiter_are_conversation_scoped() -> None:
    transport = _transport()
    raw = json.dumps(
        [
            {"type": "reply", "reply": {"ok": True}},
            {
                "type": "message",
                "topic_id": "conversations",
                "payload": {
                    "type": "conversation-turn-complete",
                    "payload": {"conversation_id": "conversation-1"},
                    "metadata": None,
                },
            },
            {
                "type": "message",
                "topic_id": "app_notifications",
                "payload": {
                    "type": "conversation-turn-complete",
                    "payload": {"conversation_id": "wrong-topic"},
                },
            },
        ]
    )

    assert transport._conversation_completion_ids(raw) == ("conversation-1",)

    baseline = transport._completion_sequence
    transport._record_conversation_completion("conversation-other")
    assert (
        transport.wait_for_conversation_completion(
            "conversation-1",
            after_sequence=baseline,
            timeout=0,
        )
        is False
    )
    transport._record_conversation_completion("conversation-1")
    assert (
        transport.wait_for_conversation_completion(
            "conversation-1",
            after_sequence=baseline,
            timeout=0,
        )
        is True
    )
