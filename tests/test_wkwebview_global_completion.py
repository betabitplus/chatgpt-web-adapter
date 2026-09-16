from __future__ import annotations

import json

from chatgpt_web_adapter.wkwebview_helper_runtime import (
    WKHelperInvocation,
    WKWebViewHelperRuntime,
)
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


def test_early_handoff_control_is_attempt_and_conversation_scoped() -> None:
    transport = _transport()
    attempt_id = transport.arm_early_handoff("conversation-1")
    assert isinstance(attempt_id, str) and attempt_id

    raw = json.dumps(
        {
            "type": "conversation-turn-handoff-control",
            "payload": {
                "handoff_attempt_id": attempt_id,
                "conversation_id": "conversation-1",
                "turn_exchange_id": "turn-exchange-1",
                "topic_id": "conversation-topic-1",
                "server_request_id": "request-1",
            },
        }
    )
    control = transport._early_handoff_control(raw)
    assert control == {
        "attempt_id": attempt_id,
        "conversation_id": "conversation-1",
        "turn_exchange_id": "turn-exchange-1",
        "topic_id": "conversation-topic-1",
        "server_request_id": "request-1",
    }

    transport._record_early_handoff_control(control)
    assert transport.early_handoff_control(attempt_id) == control

    wrong_conversation = dict(control)
    wrong_conversation["conversation_id"] = "conversation-other"
    transport._early_handoff_controls.pop(attempt_id, None)
    transport._record_early_handoff_control(wrong_conversation)
    assert transport.early_handoff_control(attempt_id) is None

    transport.release_early_handoff(attempt_id)
    assert attempt_id not in transport._early_handoff_expected


def test_early_handoff_can_bind_server_conversation_to_exact_unbound_attempt() -> None:
    transport = _transport()
    attempt_id = transport.arm_early_handoff()
    assert isinstance(attempt_id, str) and attempt_id

    control = {
        "attempt_id": attempt_id,
        "conversation_id": "temporary-new-1",
        "turn_exchange_id": "turn-exchange-new-1",
        "topic_id": "conversation-topic-new-1",
        "server_request_id": None,
    }
    transport._record_early_handoff_control(control)
    assert transport.early_handoff_control(attempt_id) == control

    wrong_attempt = dict(control)
    wrong_attempt["attempt_id"] = "not-armed"
    transport._record_early_handoff_control(wrong_attempt)
    assert transport.early_handoff_control("not-armed") is None


def test_fresh_temporary_early_handoff_requires_browser_observed_mode() -> None:
    invocation = WKHelperInvocation(
        command=["wk-helper", "--minimal-security-shell"],
        request={"minimal_temporary": True},
    )
    signal = {
        "kind": "early_handoff",
        "conversation_id": "temporary-new-1",
        "topic_id": "conversation-topic-new-1",
        "turn_exchange_id": "turn-exchange-new-1",
    }

    assert (
        WKWebViewHelperRuntime._external_completion_payload(invocation, signal)
        is None
    )

    payload = WKWebViewHelperRuntime._external_completion_payload(
        invocation,
        signal,
        submit_temporary_mode_observed=True,
    )
    assert payload is not None
    assert payload["conversation_id"] == "temporary-new-1"
    assert payload["stream_conversation_id"] == "temporary-new-1"
    assert payload["stream_topic_id"] == "conversation-topic-new-1"
    assert payload["submit_temporary_mode_observed"] is True

    ordinary_invocation = WKHelperInvocation(
        command=["wk-helper", "--minimal-security-shell"],
        request={},
    )
    assert (
        WKWebViewHelperRuntime._external_completion_payload(
            ordinary_invocation,
            signal,
            submit_temporary_mode_observed=True,
        )
        is None
    )


def test_early_handoff_parser_rejects_topic_frames_and_invalid_topics() -> None:
    transport = _transport()
    assert (
        transport._early_handoff_control(
            json.dumps(
                [
                    {
                        "type": "message",
                        "topic_id": "conversations",
                        "payload": {"type": "conversation-turn-handoff-control"},
                    }
                ]
            )
        )
        is None
    )
    assert (
        transport._early_handoff_control(
            json.dumps(
                {
                    "type": "conversation-turn-handoff-control",
                    "payload": {
                        "handoff_attempt_id": "attempt-1",
                        "conversation_id": "conversation-1",
                        "turn_exchange_id": "turn-exchange-1",
                        "topic_id": "not-a-conversation-topic",
                    },
                }
            )
        )
        is None
    )
