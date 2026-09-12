from __future__ import annotations

from pathlib import Path

import pytest

from chatgpt_web_adapter.client import ChatGPTWebClient
from chatgpt_web_adapter.exceptions import MediaError, RequestError
from chatgpt_web_adapter.wkwebview_lightweight_transport import WKLightweightTransport


def test_production_client_exposes_wk_lightweight_source_contract() -> None:
    for method_name in (
        "wk_transport_headers",
        "wk_transport_resume_state",
        "wk_transport_stream_topic",
        "wk_transport_upload_media_files",
    ):
        assert callable(getattr(ChatGPTWebClient, method_name, None))


class _Response:
    def __init__(self, status_code: int, payload: dict, *, content: bytes = b"") -> None:
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


def test_lightweight_transport_reads_canonical_through_explicit_contract(
    monkeypatch,
) -> None:
    source = _SourceClient()
    transport, _ = _transport(source)
    curl = _CurlRequests([[_Response(200, {"current_node": "node-1", "mapping": {}})]])
    monkeypatch.setattr(transport, "_curl_requests", lambda: curl)

    payload = transport.read_canonical("conversation-1", timeout=7)

    assert payload == {"current_node": "node-1", "mapping": {}}
    assert source.header_calls == [
        {
            "accept": "application/json",
            "referer": "https://chatgpt.com/c/conversation-1",
        }
    ]
    assert len(curl.sessions) == 1


def test_lightweight_transport_downloads_attachment_with_authenticated_content_fetch(
    monkeypatch,
) -> None:
    source = _SourceClient()
    transport, _ = _transport(source)
    curl = _CurlRequests(
        [
            [
                _Response(
                    200,
                    {
                        "download_url": "https://files.example.invalid/content",
                        "file_name": "instructions.md",
                        "mime_type": "text/markdown",
                    },
                ),
                _Response(200, {}, content=b"# Instructions\nFull text."),
            ]
        ]
    )
    monkeypatch.setattr(transport, "_curl_requests", lambda: curl)

    content, metadata = transport.download_attachment("file-1", timeout=9)

    assert content == b"# Instructions\nFull text."
    assert metadata["file_name"] == "instructions.md"
    assert source.header_calls == [
        {
            "accept": "application/json",
            "referer": "https://chatgpt.com/",
        },
        {
            "accept": "*/*",
            "referer": "https://chatgpt.com/",
        },
    ]
    assert [call[0] for call in curl.sessions[0].calls] == ["GET", "GET"]
    assert curl.sessions[0].calls[1][2]["headers"]["authorization"] == "Bearer test"


def test_lightweight_transport_reads_catalog_through_explicit_contract(
    monkeypatch,
) -> None:
    source = _SourceClient()
    transport, _ = _transport(source)
    curl = _CurlRequests(
        [[_Response(200, {"items": [{"id": "conversation-1"}], "total": 1})]]
    )
    monkeypatch.setattr(transport, "_curl_requests", lambda: curl)

    payload = transport.read_catalog(
        "conversations",
        offset=0,
        limit=20,
        timeout=7,
    )

    assert payload == {"items": [{"id": "conversation-1"}], "total": 1}
    assert source.header_calls == [
        {
            "accept": "application/json",
            "referer": "https://chatgpt.com/",
        }
    ]
    assert len(curl.sessions) == 1
    method, url, _kwargs = curl.sessions[0].calls[0]
    assert method == "GET"
    assert url == (
        "https://chatgpt.com/backend-api/conversations"
        "?offset=0&limit=20&order=updated&is_archived=false&is_starred=false"
    )


def test_lightweight_follow_topic_uses_one_celsius_bootstrap_and_forwards_events(
    monkeypatch,
) -> None:
    source = _SourceClient()
    transport, _ = _transport(source)
    curl = _CurlRequests(
        [[_Response(200, {"websocket_url": "wss://example.invalid/celsius"})]]
    )
    monkeypatch.setattr(transport, "_curl_requests", lambda: curl)
    events = []

    result = transport.follow_topic(
        conversation_id="conversation-1",
        topic_id="conversation-turn-turn-1",
        timeout=30,
        on_event=events.append,
    )

    assert result["completed"] is False
    assert result["segment_done_count"] == 1
    assert result["topic_id"] == "conversation-turn-turn-1"
    assert source.stop_on_done is False
    assert source.stream_calls == [
        ("conversation-turn-turn-1", "wss://example.invalid/celsius")
    ]
    assert [event["type"] for event in events] == ["raw_ws_event", "raw_ws_done"]
    assert len(curl.sessions) == 1
    assert len(curl.sessions[0].calls) == 1
    method, url, _kwargs = curl.sessions[0].calls[0]
    assert method == "GET"
    assert url == "https://chatgpt.com/backend-api/celsius/ws/user"


def test_lightweight_transport_uploads_attachments_through_explicit_contract(
    tmp_path,
) -> None:
    source = _SourceClient()
    transport, _ = _transport(source)
    attachment = tmp_path / "red.png"
    attachment.write_bytes(b"png")

    descriptors = transport.upload_attachments([str(attachment)])

    assert descriptors == (
        {
            "file_id": "file-1",
            "file_name": "red.png",
            "file_size": 3,
            "mime_type": "image/png",
            "width": None,
            "height": None,
        },
    )
    assert source.upload_calls == [[(attachment, "red.png")]]


def test_lightweight_transport_uploads_generic_file_through_standard_file_flow(
    tmp_path,
    monkeypatch,
) -> None:
    class Source(_SourceClient):
        def wk_transport_upload_media_files(self, media):
            raise AssertionError("generic files must not use the image uploader")

    source = Source()
    transport, _ = _transport(source)
    attachment = tmp_path / "notes.txt"
    attachment.write_text("payload", encoding="utf-8")
    curl = _CurlRequests(
        [
            [
                _Response(
                    200,
                    {
                        "file_id": "file-text-1",
                        "upload_url": "https://upload.invalid/blob",
                    },
                ),
                _Response(201, {}),
                _Response(200, {"download_url": "https://download.invalid/file"}),
            ]
        ]
    )
    monkeypatch.setattr(transport, "_curl_requests", lambda: curl)

    descriptors = transport.upload_attachments([str(attachment)])

    assert descriptors == (
        {
            "file_id": "file-text-1",
            "file_name": "notes.txt",
            "file_size": 7,
            "mime_type": "text/plain",
            "width": None,
            "height": None,
        },
    )
    calls = curl.sessions[0].calls
    assert [call[0] for call in calls] == ["POST", "PUT", "POST"]
    assert calls[0][2]["json"] == {
        "file_name": "notes.txt",
        "file_size": 7,
        "use_case": "multimodal",
    }
    assert calls[1][2]["headers"]["content-type"] == "text/plain"


def test_lightweight_transport_streams_temporary_without_canonical_get(
    monkeypatch,
) -> None:
    source = _SourceClient()
    transport, cached = _transport(source)
    curl = _CurlRequests(
        [[_Response(200, {"websocket_url": "wss://example.invalid/ws"})]]
    )
    monkeypatch.setattr(transport, "_curl_requests", lambda: curl)
    events: list[dict] = []

    result = transport.stream_temporary_turn(
        conversation_id="temporary-conversation",
        resume_value="resume-secret",
        timeout=10,
        relay_text_event=events.append,
    )

    assert result["conversation_id"] == "temporary-conversation"
    assert result["message_id"] == "assistant-1"
    assert result["ws_token_events"] == 1
    assert events == [
        {
            "type": "assistant_text_delta",
            "sequence": 1,
            "delta": "hello",
            "message_id": "assistant-1",
        }
    ]
    assert len(curl.sessions) == 1
    assert cached == []


def test_lightweight_transport_resumes_and_finalizes_through_explicit_contract(
    monkeypatch,
) -> None:
    source = _SourceClient()
    transport, cached = _transport(source)
    final_payload = {"current_node": "node-final", "mapping": {"node-final": {}}}
    curl = _CurlRequests(
        [
            [_Response(200, {"websocket_url": "wss://example.invalid/ws"})],
            [_Response(200, final_payload)],
        ]
    )
    monkeypatch.setattr(transport, "_curl_requests", lambda: curl)
    events: list[dict] = []

    result = transport.resume_turn(
        conversation_id="conversation-1",
        resume_value="resume-secret",
        timeout=10,
        relay_text_event=events.append,
        text="prompt",
        baseline_current_node="node-before",
    )

    assert source.resume_calls == [("resume-secret", "conversation-1")]
    assert source.stream_calls == [("topic-1", "wss://example.invalid/ws")]
    assert events == [
        {
            "type": "assistant_text_delta",
            "sequence": 1,
            "delta": "hello",
            "message_id": "assistant-1",
        }
    ]
    assert cached == [("conversation-1", final_payload)]
    assert result["canonical_completed"] is True
    assert result["ws_token_events"] == 1


def test_lightweight_resume_raw_observer_crosses_segment_done_until_turn_stop(monkeypatch) -> None:
    class RawSource(_SourceClient):
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
            assert should_stop() is True
            state["message_id"] = "assistant-final"
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
        message = parsed.get("v", {}).get("message") if isinstance(parsed, dict) else None
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
    assert [event["type"] for event in raw_events] == ["raw_ws_done", "raw_ws_event"]
    assert result["canonical_completed"] is True
    assert result["ws_token_events"] == 1
    assert cached == [("conversation-1", final_payload)]


def test_lightweight_resume_canonical_reconcile_uses_bounded_backoff(monkeypatch) -> None:
    source = _SourceClient()
    transport, cached = _transport(source)
    final_payload = {"current_node": "node-final", "mapping": {"node-final": {}}}
    curl = _CurlRequests(
        [
            [_Response(200, {"websocket_url": "wss://example.invalid/ws"})],
            [
                _Response(200, {"current_node": "not-final", "mapping": {}}),
                _Response(200, final_payload),
            ],
        ]
    )
    monkeypatch.setattr(transport, "_curl_requests", lambda: curl)
    sleeps: list[float] = []
    monkeypatch.setattr(
        "chatgpt_web_adapter.wkwebview_lightweight_transport.time.sleep",
        sleeps.append,
    )

    result = transport.resume_turn(
        conversation_id="conversation-1",
        resume_value="resume-secret",
        timeout=30,
        relay_text_event=lambda _event: None,
        text="prompt",
        baseline_current_node="node-before",
    )

    assert result["canonical_completed"] is True
    assert sleeps == [1.0]
    assert len(curl.sessions[1].calls) == 2
    assert cached == [("conversation-1", final_payload)]


def test_lightweight_resume_429_uses_long_backoff(monkeypatch) -> None:
    source = _SourceClient()
    transport, _ = _transport(source)
    final_payload = {"current_node": "node-final", "mapping": {"node-final": {}}}
    curl = _CurlRequests(
        [
            [_Response(200, {"websocket_url": "wss://example.invalid/ws"})],
            [_Response(429, {}), _Response(200, final_payload)],
        ]
    )
    monkeypatch.setattr(transport, "_curl_requests", lambda: curl)
    sleeps: list[float] = []
    monkeypatch.setattr(
        "chatgpt_web_adapter.wkwebview_lightweight_transport.time.sleep",
        sleeps.append,
    )

    result = transport.resume_turn(
        conversation_id="conversation-1",
        resume_value="resume-secret",
        timeout=120,
        relay_text_event=lambda _event: None,
        text="prompt",
        baseline_current_node="node-before",
    )

    assert result["canonical_completed"] is True
    assert sleeps == [60.0]
    assert len(curl.sessions[1].calls) == 2


def test_lightweight_resume_stop_skips_canonical_polling(monkeypatch) -> None:
    source = _SourceClient()
    cached: list[tuple[str, dict]] = []
    transport = WKLightweightTransport(
        source,
        canonical_matches_write=lambda payload, **kwargs: False,
        cache_final_payload=lambda conversation_id, payload: cached.append(
            (conversation_id, payload)
        ),
        stop_requested=lambda conversation_id: conversation_id == "conversation-1",
    )

    def stopped_stream(
        topic_id: str,
        *,
        websocket_url: str,
        state: dict,
        on_token,
        should_stop=None,
    ) -> None:
        source.stream_calls.append((topic_id, websocket_url))
        assert should_stop is not None
        assert should_stop() is True

    monkeypatch.setattr(source, "wk_transport_stream_topic", stopped_stream)
    curl = _CurlRequests(
        [[_Response(200, {"websocket_url": "wss://example.invalid/ws"})]]
    )
    monkeypatch.setattr(transport, "_curl_requests", lambda: curl)

    result = transport.resume_turn(
        conversation_id="conversation-1",
        resume_value="resume-secret",
        timeout=10,
        relay_text_event=lambda _event: None,
        text="prompt",
        baseline_current_node="node-before",
    )

    assert result["stop_requested"] is True
    assert result["canonical_completed"] is False
    assert result["stream_terminal_observed"] is True
    assert result["ws_token_events"] == 0
    assert source.stream_calls == [("topic-1", "wss://example.invalid/ws")]
    assert len(curl.sessions) == 1
    assert cached == []


def test_lightweight_canonical_auth_failure_does_not_fallback(monkeypatch) -> None:
    source = _SourceClient()
    transport, _ = _transport(source)
    curl = _CurlRequests([[_Response(401, {})]])
    monkeypatch.setattr(transport, "_curl_requests", lambda: curl)

    with pytest.raises(
        RequestError, match="WKWEBVIEW_CURL_CANONICAL_HTTP:401"
    ) as caught:
        transport.read_canonical("conversation-1", timeout=7)

    assert caught.value.status_code == 401
    assert caught.value.request_stage == "wkwebview_canonical_read"


def test_lightweight_canonical_invalid_schema_does_not_fallback(monkeypatch) -> None:
    source = _SourceClient()
    transport, _ = _transport(source)
    curl = _CurlRequests([[_Response(200, [])]])
    monkeypatch.setattr(transport, "_curl_requests", lambda: curl)

    with pytest.raises(RequestError, match="WKWEBVIEW_CURL_CANONICAL_INVALID_SCHEMA"):
        transport.read_canonical("conversation-1", timeout=7)


def test_lightweight_canonical_transport_failure_allows_wk_fallback(
    monkeypatch,
) -> None:
    class TransportFailure(Exception):
        pass

    class FailingSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def get(self, url: str, *, headers: dict, timeout: float):
            raise TransportFailure("network down")

    class FailingCurl:
        RequestsError = TransportFailure

        @staticmethod
        def Session(*, impersonate: str):
            assert impersonate == "safari"
            return FailingSession()

    source = _SourceClient()
    transport, _ = _transport(source)
    monkeypatch.setattr(transport, "_curl_requests", lambda: FailingCurl)

    assert transport.read_canonical("conversation-1", timeout=7) is None
    assert transport.take_canonical_fallback_reason() == (
        "WKWEBVIEW_CURL_CANONICAL_TRANSPORT"
    )


def test_lightweight_canonical_missing_source_contract_is_actionable() -> None:
    transport, _ = _transport(object())

    with pytest.raises(
        RequestError,
        match="WKWEBVIEW_CURL_CANONICAL_SOURCE_CONTRACT_MISSING",
    ):
        transport.read_canonical("conversation-1", timeout=7)


def test_lightweight_attachment_request_error_does_not_fallback(tmp_path) -> None:
    class FailingSource(_SourceClient):
        def wk_transport_upload_media_files(self, media):
            raise RequestError("upload auth failed", status_code=401)

    transport, _ = _transport(FailingSource())
    attachment = tmp_path / "red.png"
    attachment.write_bytes(b"png")

    with pytest.raises(RequestError, match="upload auth failed") as caught:
        transport.upload_attachments([str(attachment)])

    assert caught.value.status_code == 401


def test_lightweight_attachment_media_error_does_not_fallback(tmp_path) -> None:
    class FailingSource(_SourceClient):
        def wk_transport_upload_media_files(self, media):
            raise MediaError("invalid attachment")

    transport, _ = _transport(FailingSource())
    attachment = tmp_path / "red.png"
    attachment.write_bytes(b"png")

    with pytest.raises(MediaError, match="invalid attachment"):
        transport.upload_attachments([str(attachment)])


def test_lightweight_attachment_programming_error_does_not_fallback(tmp_path) -> None:
    class FailingSource(_SourceClient):
        def wk_transport_upload_media_files(self, media):
            raise RuntimeError("unexpected bug")

    transport, _ = _transport(FailingSource())
    attachment = tmp_path / "red.png"
    attachment.write_bytes(b"png")

    with pytest.raises(RuntimeError, match="unexpected bug"):
        transport.upload_attachments([str(attachment)])


def test_lightweight_attachment_invalid_schema_is_actionable(tmp_path) -> None:
    class FailingSource(_SourceClient):
        def wk_transport_upload_media_files(self, media):
            return [{"file_name": "red.png"}]

    transport, _ = _transport(FailingSource())
    attachment = tmp_path / "red.png"
    attachment.write_bytes(b"png")

    with pytest.raises(
        RequestError, match="WKWEBVIEW_ATTACHMENT_UPLOAD_FILE_ID_MISSING"
    ):
        transport.upload_attachments([str(attachment)])
