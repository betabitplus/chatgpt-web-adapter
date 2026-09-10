from __future__ import annotations

import chatgpt_web_adapter.client as client_mod


def _payload() -> dict:
    return {
        "conversation_id": "conversation-1",
        "current_node": "assistant-1",
        "mapping": {
            "assistant-1": {
                "id": "assistant-1",
                "parent": None,
                "children": [],
                "message": {
                    "id": "assistant-1",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["done"]},
                },
            }
        },
    }


def test_explicit_endpoint_override_preserves_legacy_conversation_reader(
    monkeypatch,
) -> None:
    expected = _payload()
    calls: list[str] = []

    def legacy_reader(_self, conversation_id: str):
        calls.append(conversation_id)
        return expected

    monkeypatch.setattr(
        client_mod._core.ChatGPTWebClient,
        "_get_conversation_payload",
        legacy_reader,
    )
    monkeypatch.setattr(
        client_mod,
        "CHAT_CONVERSATION_URL",
        "http://fixture.test/backend-api/conversation/{conversation_id}",
    )

    client = object.__new__(client_mod.ChatGPTWebClient)
    payload = client._get_conversation_payload("conversation-1")

    assert payload is expected
    assert calls == ["conversation-1"]


def test_instance_injected_reader_is_not_bypassed_by_full_history_reader() -> None:
    client = object.__new__(client_mod.ChatGPTWebClient)
    client._get_conversation_payload = lambda _conversation_id: _payload()
    client._get_full_conversation_payload = lambda _conversation_id: (
        _ for _ in ()
    ).throw(AssertionError("full reader must not bypass explicit instance reader"))

    messages = client.get_messages("conversation-1")

    assert [(message.message_id, message.text) for message in messages] == [
        ("assistant-1", "done")
    ]
