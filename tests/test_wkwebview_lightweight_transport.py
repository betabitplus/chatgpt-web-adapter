from __future__ import annotations

from pathlib import Path

from chatgpt_web_adapter.client import ChatGPTWebClient
from chatgpt_web_adapter.wkwebview_lightweight_transport import WKLightweightTransport


class _Response:
    def __init__(
        self, status_code: int, payload: dict, *, content: bytes = b""
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self) -> dict:
        return self._payload


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(self, url: str, *, headers: dict, timeout: float):
        self.calls.append(("GET", url, {"headers": headers, "timeout": timeout}))
        return self._responses.pop(0)

    def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, dict(kwargs)))
        return self._responses.pop(0)

    def put(self, url: str, **kwargs):
        self.calls.append(("PUT", url, dict(kwargs)))
        return self._responses.pop(0)


class _CurlRequests:
    def __init__(self, response_groups: list[list[_Response]]) -> None:
        self._response_groups = response_groups
        self.sessions: list[_Session] = []

    def Session(self, *, impersonate: str):
        assert impersonate == "safari"
        session = _Session(self._response_groups.pop(0))
        self.sessions.append(session)
        return session


class _SourceClient:
    def __init__(self) -> None:
        self.header_calls: list[dict[str, str | None]] = []
        self.resume_calls: list[tuple[str, str]] = []
        self.stream_calls: list[tuple[str, str]] = []
        self.upload_calls: list[list[tuple[Path, str | None]]] = []

    def wk_transport_headers(self, extra=None):
        self.header_calls.append(dict(extra or {}))
        return {"authorization": "Bearer test", **dict(extra or {})}

    def wk_transport_resume_state(self, resume_token: str, *, conversation_id: str):
        self.resume_calls.append((resume_token, conversation_id))
        return "topic-1", {
            "conversation_id": conversation_id,
            "message_id": "assistant-1",
        }

    def wk_transport_stream_topic(
        self,
        topic_id: str,
        *,
        websocket_url: str,
        state: dict,
        on_event=None,
        on_token=None,
        should_stop=None,
        stop_on_done=True,
    ) -> None:
        self.stream_calls.append((topic_id, websocket_url))
        self.stop_on_done = stop_on_done
        state["message_id"] = "assistant-1"
        if on_event is not None:
            on_event(
                {
                    "type": "raw_ws_event",
                    "parsed": {
                        "v": {
                            "message": {
                                "id": "assistant-1",
                                "author": {"role": "assistant"},
                                "recipient": "all",
                                "content": {"content_type": "text", "parts": ["hello"]},
                                "metadata": {"turn_exchange_id": "turn-1"},
                            }
                        }
                    },
                }
            )
            on_event({"type": "raw_ws_done", "topic_id": topic_id})
        if on_token is not None:
            on_token("hello")

    def wk_transport_upload_media_files(self, media):
        items = list(media)
        self.upload_calls.append(items)
        path, filename = items[0]
        return [
            {
                "file_id": "file-1",
                "file_name": filename,
                "file_size": path.stat().st_size,
                "mime_type": "image/png",
            }
        ]


def _transport(source: _SourceClient):
    cached: list[tuple[str, dict]] = []
    transport = WKLightweightTransport(
        source,
        canonical_matches_write=lambda payload, **kwargs: (
            payload.get("current_node") == "node-final"
        ),
        cache_final_payload=lambda conversation_id, payload: cached.append(
            (conversation_id, payload)
        ),
    )
    return transport, cached


def test_ws_parser_captures_terminal_evidence_without_canonical_reconcile() -> None:
    full_message_state: dict = {}
    ChatGPTWebClient._parse_event(
        {
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
        full_message_state,
    )

    assert full_message_state["message_id"] == "assistant-final"
    assert full_message_state["finish_reason"] == "end_turn"
    assert full_message_state["stream_terminal_observed"] is True

    patch_state: dict = {}
    ChatGPTWebClient._parse_event(
        {
            "v": {
                "message": {
                    "id": "assistant-final",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["partial"]},
                    "status": "in_progress",
                    "metadata": {},
                }
            }
        },
        patch_state,
    )
    ChatGPTWebClient._parse_event(
        {"p": "/message/status", "v": "completed"},
        patch_state,
    )

    assert patch_state["finish_reason"] == "completed"
    assert patch_state["stream_terminal_observed"] is True

    non_answer_state: dict = {}
    ChatGPTWebClient._parse_event(
        {
            "v": {
                "message": {
                    "id": "context-1",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {
                        "content_type": "model_editable_context",
                        "parts": [],
                    },
                    "status": "finished_successfully",
                    "metadata": {},
                }
            }
        },
        non_answer_state,
    )

    assert non_answer_state["current_message_is_final_text"] is False
    assert "finish_reason" not in non_answer_state
    assert non_answer_state.get("stream_terminal_observed") is not True


def test_lightweight_resume_raw_observer_crosses_segment_done_until_turn_stop(
    monkeypatch,
) -> None:
    class RawSource(_SourceClient):
        def wk_transport_resume_state(self, resume_token: str, *, conversation_id: str):
            self.resume_calls.append((resume_token, conversation_id))
            return "topic-1", {"conversation_id": conversation_id}

        def wk_transport_stream_topic(
            self,
            topic_id: str,
            *,
            websocket_url: str,
            state: dict,
            on_event=None,
            on_token=None,
            should_stop=None,
            stop_on_done=True,
        ) -> None:
            self.stream_calls.append((topic_id, websocket_url))
            self.stop_on_done = stop_on_done
            assert on_event is not None
            assert should_stop is not None
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
                                "content": {"content_type": "text", "parts": ["done"]},
                                "end_turn": True,
                                "metadata": {"turn_exchange_id": "turn-1"},
                            }
                        }
                    },
                }
            )
            on_event(
                {
                    "type": "raw_ws_event",
                    "parsed": {
                        "message": None,
                        # Exact historical live shape: the Celsius terminal frame
                        # supplied only error text and no error_code.
                        "error": (
                            "You've reached the maximum length for this conversation, "
                            "but you can keep talking by starting a new chat."
                        ),
                    },
                }
            )
            assert should_stop() is True
            if on_token is not None:
                on_token("done")

    source = RawSource()
    transport, cached = _transport(source)
    final_payload = {"current_node": "node-final", "mapping": {"node-final": {}}}
    curl = _CurlRequests(
        [
            [_Response(200, {"websocket_url": "wss://example.invalid/ws"})],
            [_Response(200, final_payload)],
        ]
    )
    monkeypatch.setattr(transport, "_curl_requests", lambda: curl)
    raw_events: list[dict] = []
    turn_completed = False

    def on_transport_event(event: dict) -> None:
        nonlocal turn_completed
        raw_events.append(event)
        parsed = event.get("parsed")
        message = (
            parsed.get("v", {}).get("message") if isinstance(parsed, dict) else None
        )
        if isinstance(message, dict) and message.get("end_turn") is True:
            turn_completed = True

    result = transport.resume_turn(
        conversation_id="conversation-1",
        resume_value="resume-secret",
        timeout=10,
        relay_text_event=lambda _event: (_ for _ in ()).throw(
            AssertionError("raw observer mode must not duplicate token-only events")
        ),
        on_transport_event=on_transport_event,
        stream_should_stop=lambda: turn_completed,
        text="prompt",
        baseline_current_node="node-before",
    )

    assert source.stop_on_done is False
    assert [event["type"] for event in raw_events] == [
        "raw_ws_done",
        "raw_ws_event",
        "raw_ws_event",
    ]
    assert result["canonical_completed"] is False
    assert result["stream_finality_proven"] is True
    assert result["terminal_error_code"] is None
    assert result["terminal_error"] == (
        "You've reached the maximum length for this conversation, "
        "but you can keep talking by starting a new chat."
    )
    assert result["message_id"] is None
    assert result["finish_reason"] == "stream_terminal"
    assert result["ws_token_events"] == 1
    assert len(curl.sessions) == 1
    assert cached == []
