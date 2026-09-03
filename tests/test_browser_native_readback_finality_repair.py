from __future__ import annotations

from types import SimpleNamespace

import pytest

from chatgpt_web_adapter.browser_native_client import (
    _status_finalizes_message,
    _wait_for_new_final_assistant,
)
from chatgpt_web_adapter.exceptions import ConversationTimeoutError
from chatgpt_web_adapter.messages import _message_finish_reason


def test_message_finish_reason_prefers_finish_details_type() -> None:
    message = {
        "metadata": {
            "finish_details": {"type": "stop"},
            "finish_reason": "metadata-fallback",
        },
        "finish_reason": "top-level-fallback",
    }
    assert _message_finish_reason(message) == "stop"


def test_message_finish_reason_falls_back_to_metadata_finish_reason() -> None:
    message = {"metadata": {"finish_reason": "stop"}}
    assert _message_finish_reason(message) == "stop"


def test_message_finish_reason_falls_back_to_top_level_finish_reason() -> None:
    message = {"metadata": {}, "finish_reason": "stop"}
    assert _message_finish_reason(message) == "stop"


def test_status_finality_requires_matching_message_id() -> None:
    status = SimpleNamespace(status="completed", message_id="assistant-new")
    assert _status_finalizes_message(status, "assistant-new") is True
    assert _status_finalizes_message(status, "assistant-old") is False


class _ReadbackClient:
    def __init__(self, *, status, messages) -> None:
        self.status = status
        self.messages = messages

    def get_status(self, conversation):
        return self.status

    def get_messages(self, conversation, **kwargs):
        return self.messages


def _assistant(*, message_id: str, text: str, finish_reason=None):
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        finish_reason=finish_reason,
        model="gpt-test",
    )


def test_matching_completed_status_finalizes_nonempty_assistant_without_finish_reason() -> None:
    message = _assistant(message_id="assistant-new", text="done", finish_reason=None)
    client = _ReadbackClient(
        status=SimpleNamespace(status="completed", message_id="assistant-new"),
        messages=[message],
    )
    assert _wait_for_new_final_assistant(
        client,
        "conversation-1",
        baseline_assistant_ids=set(),
        timeout=0.01,
        interval=0.001,
    ) is message


def test_stale_completed_status_cannot_finalize_new_partial_assistant() -> None:
    message = _assistant(message_id="assistant-new", text="partial", finish_reason=None)
    client = _ReadbackClient(
        status=SimpleNamespace(status="completed", message_id="assistant-old"),
        messages=[message],
    )
    with pytest.raises(ConversationTimeoutError):
        _wait_for_new_final_assistant(
            client,
            "conversation-1",
            baseline_assistant_ids=set(),
            timeout=0.001,
            interval=0.001,
        )


def test_running_status_cannot_finalize_same_message_without_finish_reason() -> None:
    message = _assistant(message_id="assistant-new", text="partial", finish_reason=None)
    client = _ReadbackClient(
        status=SimpleNamespace(status="running", message_id="assistant-new"),
        messages=[message],
    )
    with pytest.raises(ConversationTimeoutError):
        _wait_for_new_final_assistant(
            client,
            "conversation-1",
            baseline_assistant_ids=set(),
            timeout=0.001,
            interval=0.001,
        )


def test_finish_reason_remains_fast_path_without_status_message_id() -> None:
    message = _assistant(message_id="assistant-new", text="done", finish_reason="stop")
    client = _ReadbackClient(
        status=SimpleNamespace(status="completed", message_id=None),
        messages=[message],
    )
    assert _wait_for_new_final_assistant(
        client,
        "conversation-1",
        baseline_assistant_ids=set(),
        timeout=0.01,
        interval=0.001,
    ) is message


def _canonical_node(
    *,
    message_id: str,
    parent: str | None,
    role: str,
    text: str,
    turn_exchange_id: str | None,
    recipient: str = "all",
    finish_reason: str | None = None,
):
    metadata = {"model_slug": "gpt-test"}
    if turn_exchange_id is not None:
        metadata["turn_exchange_id"] = turn_exchange_id
        metadata["working_turn_id"] = turn_exchange_id
    if finish_reason is not None:
        metadata["finish_details"] = {"type": finish_reason}
    return {
        "id": message_id,
        "parent": parent,
        "children": [],
        "message": {
            "id": message_id,
            "author": {"role": role},
            "content": {"content_type": "text", "parts": [text]},
            "recipient": recipient,
            "metadata": metadata,
        },
    }


class _CanonicalPayloadClient:
    def __init__(self, payload):
        self.payload = payload

    def _get_conversation_payload(self, conversation):
        return self.payload


def test_exact_turn_exchange_id_ignores_stale_and_tool_directed_assistants() -> None:
    old = _canonical_node(
        message_id="assistant-old",
        parent=None,
        role="assistant",
        text="GPTTY_OK",
        turn_exchange_id="turn-old",
        finish_reason="stop",
    )
    user = _canonical_node(
        message_id="user-new",
        parent="assistant-old",
        role="user",
        text="use the tool",
        turn_exchange_id="turn-new",
    )
    tool_directed = _canonical_node(
        message_id="assistant-tool-call",
        parent="user-new",
        role="assistant",
        text='{"tool":"open_workspace"}',
        turn_exchange_id="turn-new",
        recipient="api_tool.call_tool",
        finish_reason="stop",
    )
    tool = _canonical_node(
        message_id="tool-result",
        parent="assistant-tool-call",
        role="tool",
        text='{"ok":true}',
        turn_exchange_id="turn-new",
    )
    final = _canonical_node(
        message_id="assistant-final",
        parent="tool-result",
        role="assistant",
        text="CODEXPRO_OK",
        turn_exchange_id="turn-new",
        finish_reason="stop",
    )
    payload = {
        "current_node": "assistant-final",
        "mapping": {
            "assistant-old": old,
            "user-new": user,
            "assistant-tool-call": tool_directed,
            "tool-result": tool,
            "assistant-final": final,
        },
    }
    client = _CanonicalPayloadClient(payload)

    message, returned_payload, read_count = _wait_for_new_final_assistant(
        client,
        "conversation-1",
        baseline_assistant_ids=set(),
        timeout=0.01,
        interval=0.001,
        include_readback=True,
        turn_exchange_id="turn-new",
    )

    assert message.message_id == "assistant-final"
    assert message.text == "CODEXPRO_OK"
    assert returned_payload is payload
    assert read_count == 1


def test_turn_filter_falls_back_to_baseline_when_payload_has_no_turn_metadata() -> None:
    old = _canonical_node(
        message_id="assistant-old",
        parent=None,
        role="assistant",
        text="OLD",
        turn_exchange_id=None,
        finish_reason="stop",
    )
    final = _canonical_node(
        message_id="assistant-new",
        parent="assistant-old",
        role="assistant",
        text="NEW",
        turn_exchange_id=None,
        finish_reason="stop",
    )
    payload = {
        "current_node": "assistant-new",
        "mapping": {"assistant-old": old, "assistant-new": final},
    }
    client = _CanonicalPayloadClient(payload)

    message = _wait_for_new_final_assistant(
        client,
        "conversation-1",
        baseline_assistant_ids={"assistant-old"},
        timeout=0.01,
        interval=0.001,
        turn_exchange_id="turn-new",
    )

    assert message.message_id == "assistant-new"
    assert message.text == "NEW"
