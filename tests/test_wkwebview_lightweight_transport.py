from __future__ import annotations

from pathlib import Path

from chatgpt_web_adapter.wkwebview_lightweight_transport import WKLightweightTransport


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict, float]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(self, url: str, *, headers: dict, timeout: float):
        self.calls.append((url, headers, timeout))
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
        return "topic-1", {"conversation_id": conversation_id, "message_id": "assistant-1"}

    def wk_transport_stream_topic(
        self,
        topic_id: str,
        *,
        websocket_url: str,
        state: dict,
        on_token,
    ) -> None:
        self.stream_calls.append((topic_id, websocket_url))
        state["message_id"] = "assistant-1"
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
        canonical_matches_write=lambda payload, **kwargs: payload.get("current_node") == "node-final",
        cache_final_payload=lambda conversation_id, payload: cached.append((conversation_id, payload)),
    )
    return transport, cached


def test_lightweight_transport_reads_canonical_through_explicit_contract(monkeypatch) -> None:
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


def test_lightweight_transport_uploads_attachments_through_explicit_contract(tmp_path) -> None:
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


def test_lightweight_transport_resumes_and_finalizes_through_explicit_contract(monkeypatch) -> None:
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
