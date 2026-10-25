from __future__ import annotations

import asyncio
import json
import shutil
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterator

import pytest

import chatgpt_web_adapter as adapter
import chatgpt_web_adapter.client as client_mod

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 40


def _live_like_stream_events(
    *,
    conversation_id: str = "conv-live",
    assistant_message_id: str = "assistant-final",
    title: str = "Live-like title",
    text_parts: tuple[str, ...] = ("scan-", "ok"),
    finish_reason: str = "stop",
) -> list[dict[str, Any]]:
    return [
        {"type": "resume_conversation_token", "kind": "topic", "token": "resume-token"},
        {
            "v": {
                "conversation_id": conversation_id,
                "message": {
                    "author": {"role": "user"},
                    "id": "user-1",
                    "recipient": "all",
                },
            }
        },
        {
            "v": {
                "message": {
                    "author": {"role": "assistant"},
                    "id": "system-ctx",
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": [""]},
                    "metadata": {
                        "is_user_system_message": True,
                        "model_slug": "gpt-5-3-mini",
                    },
                },
                "conversation_id": conversation_id,
            }
        },
        {
            "type": "input_message",
            "input_message": {
                "id": "user-1",
                "author": {"role": "user"},
            },
        },
        {
            "v": {
                "message": {
                    "author": {"role": "assistant"},
                    "id": assistant_message_id,
                    "recipient": "all",
                    "channel": "final",
                },
                "conversation_id": conversation_id,
            }
        },
        {"type": "title_generation", "title": title},
        {"type": "delta_encoding", "encoding": "v1"},
        {
            "type": "message_marker",
            "conversation_id": conversation_id,
            "message_id": assistant_message_id,
        },
        {
            "v": [
                *({"p": "/message/content/parts/0", "v": part} for part in text_parts),
                {
                    "p": "/message/metadata",
                    "v": {"finish_details": {"type": finish_reason}},
                },
            ]
        },
        {
            "type": "server_ste_metadata",
            "metadata": {"message_id": assistant_message_id},
        },
        {"type": "message_stream_complete", "conversation_id": conversation_id},
    ]


def _build_client() -> adapter.ChatGPTWebClient:
    client = object.__new__(adapter.ChatGPTWebClient)
    client.auth = adapter.AuthData(cookies={})
    client.timeout = 10
    client.base_headers = {"user-agent": "pytest-agent"}
    client.curl_bin = shutil.which("curl.exe") or shutil.which("curl")
    client.prefetched_requirements = None
    client.prefetched_proof_header = None
    client.prefetched_ts = 0.0
    client._file_cache = {}
    client.debug_trace_dir = None
    client.debug_trace_sanitize = True
    client._debug_trace_counter = 0
    if not client.curl_bin:
        raise RuntimeError("curl is required for tests")
    return client


@contextmanager
def _serve(handler_cls: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class CookieHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Set-Cookie", "session=internal; Path=/")
        self.end_headers()
        self.wfile.write(PNG_BYTES)

    def log_message(self, format: str, *args) -> None:
        return None


def _patch_chat_endpoints(monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    monkeypatch.setattr(
        client_mod,
        "CHAT_REQUIREMENTS_URL",
        f"{base_url}/backend-api/sentinel/chat-requirements",
    )
    monkeypatch.setattr(
        client_mod, "CHAT_BACKEND_URL", f"{base_url}/backend-api/f/conversation"
    )
    monkeypatch.setattr(
        client_mod,
        "CHAT_CONVERSATION_PREPARE_URL",
        f"{base_url}/backend-api/f/conversation/prepare",
    )
    monkeypatch.setattr(
        client_mod,
        "CHAT_CONVERSATION_URL",
        f"{base_url}/backend-api/conversation/{{conversation_id}}",
    )
    monkeypatch.setattr(
        client_mod, "CHAT_CONVERSATIONS_URL", f"{base_url}/backend-api/conversations"
    )
    monkeypatch.setattr(client_mod, "CHAT_FILES_URL", f"{base_url}/backend-api/files")
    monkeypatch.setattr(
        client_mod, "CELSIUS_WS_USER_URL", f"{base_url}/celsius/ws/user"
    )


def _make_chat_handler(
    state: dict[str, Any],
    *,
    backend_status: int = 200,
) -> type[BaseHTTPRequestHandler]:
    class ChatHandler(BaseHTTPRequestHandler):
        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0"))
            return self.rfile.read(length) if length else b""

        def _write_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path == "/backend-api/sentinel/chat-requirements":
                state["requirements_calls"] += 1
                self._write_json(
                    200,
                    {
                        "token": "req-token",
                        "turnstile": {"required": False},
                        "proofofwork": {"required": False},
                    },
                )
                return

            if self.path == "/backend-api/files":
                payload = json.loads(self._read_body().decode("utf-8"))
                state["file_create_payloads"].append(payload)
                base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
                self._write_json(
                    200,
                    {
                        "upload_url": f"{base_url}/upload/file-1",
                        "file_id": "file-1",
                    },
                )
                return

            if self.path == "/backend-api/files/file-1/uploaded":
                state["finalize_calls"] += 1
                self._write_json(200, {"download_url": "https://download.test/file-1"})
                return

            if self.path == "/backend-api/f/conversation":
                payload = json.loads(self._read_body().decode("utf-8"))
                state["conversation_payloads"].append(payload)
                state.setdefault("conversation_headers", []).append(
                    {key.lower(): value for key, value in self.headers.items()}
                )
                if payload.get("messages"):
                    state.setdefault("approval_stream_payloads", []).append(payload)
                if backend_status >= 400:
                    body = b"backend exploded"
                    self.send_response(backend_status)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                events = state.get("stream_events") or [
                    {
                        "v": {
                            "conversation_id": "conv-123",
                            "message": {
                                "author": {"role": "assistant"},
                                "id": "assistant-1",
                                "recipient": "all",
                            },
                        }
                    },
                    {"type": "title_generation", "title": "Generated title"},
                    {"v": "Hello "},
                    {
                        "v": [
                            {"p": "/message/content/parts/0", "v": "world"},
                            {
                                "p": "/message/metadata",
                                "v": {"finish_details": {"type": "stop"}},
                            },
                        ]
                    },
                ]
                for event in events:
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return

            if self.path == "/backend-api/f/conversation/prepare":
                payload = json.loads(self._read_body().decode("utf-8"))
                state.setdefault("prepare_payloads", []).append(payload)
                state.setdefault("prepare_headers", []).append(
                    {key.lower(): value for key, value in self.headers.items()}
                )
                self._write_json(
                    200, {"status": "ok", "conduit_token": "conduit-token"}
                )
                return

            self.send_response(404)
            self.end_headers()

        def do_GET(self) -> None:
            if self.path == "/celsius/ws/user":
                self._write_json(
                    200,
                    {
                        "websocket_url": "wss://pubsub.chatgpt.com/v1/socket?token=test-token",
                    },
                )
                return
            if self.path.startswith("/backend-api/conversations"):
                state["conversations_get_calls"] = (
                    state.get("conversations_get_calls", 0) + 1
                )
                self._write_json(
                    200,
                    {
                        "items": state.get(
                            "recent_conversations",
                            [{"id": "conv-123", "title": "Test conversation"}],
                        )
                    },
                )
                return
            if self.path == "/backend-api/conversation/conv-123":
                state["conversation_get_calls"] = (
                    state.get("conversation_get_calls", 0) + 1
                )
                conversation_statuses = state.get("conversation_get_statuses")
                if isinstance(conversation_statuses, list) and conversation_statuses:
                    status_entry = conversation_statuses.pop(0)
                    if isinstance(status_entry, dict):
                        self._write_json(
                            int(status_entry.get("status", 200)),
                            status_entry.get("payload") or {},
                        )
                        return
                conversation_payloads = state.get("conversation_get_payloads")
                if isinstance(conversation_payloads, list) and conversation_payloads:
                    conversation_payload = conversation_payloads.pop(0)
                    if isinstance(conversation_payload, dict):
                        self._write_json(200, conversation_payload)
                        return
                conversation_payload = state.get("conversation_get_payload")
                if isinstance(conversation_payload, dict):
                    self._write_json(200, conversation_payload)
                    return
                self._write_json(
                    200,
                    {
                        "conversation_id": "conv-123",
                        "mapping": {
                            "assistant-old": {
                                "message": {
                                    "id": "assistant-old",
                                    "author": {"role": "assistant"},
                                    "create_time": 1,
                                    "content": {
                                        "content_type": "text",
                                        "parts": ["approval required"],
                                    },
                                }
                            },
                            "assistant-new": {
                                "message": {
                                    "id": "assistant-new",
                                    "author": {"role": "assistant"},
                                    "create_time": 2,
                                    "content": {
                                        "content_type": "text",
                                        "parts": ["approved result"],
                                    },
                                    "metadata": {
                                        "finish_details": {"type": "stop"},
                                    },
                                }
                            },
                        },
                    },
                )
                return
            self.send_response(404)
            self.end_headers()

        def do_PUT(self) -> None:
            if self.path == "/upload/file-1":
                state["uploaded_payloads"].append(self._read_body())
                self.send_response(201)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args) -> None:
            return None

    return ChatHandler


def test_run_curl_persists_cookies_by_default() -> None:
    client = _build_client()

    with _serve(CookieHandler) as base_url:
        status, body, _ = client._run_curl(
            "GET",
            f"{base_url}/image.png",
            {"user-agent": client.base_headers["user-agent"]},
        )

    assert status == 200
    assert body == PNG_BYTES
    assert client.auth.cookies["session"] == "internal"


def test_run_curl_writes_sanitized_debug_trace(tmp_path) -> None:
    client = _build_client()
    client.debug_trace_dir = tmp_path

    with _serve(CookieHandler) as base_url:
        status, body, _ = client._run_curl(
            "GET",
            f"{base_url}/image.png",
            {
                "user-agent": client.base_headers["user-agent"],
                "authorization": "Bearer secret-token",
                "cookie": "session=secret",
                "openai-sentinel-proof-token": "proof-secret",
            },
        )

    trace_files = sorted(tmp_path.glob("*.json"))
    assert status == 200
    assert body == PNG_BYTES
    assert len(trace_files) == 1

    payload = json.loads(trace_files[0].read_text(encoding="utf-8"))
    assert payload["method"] == "GET"
    assert payload["response_status"] == 200
    assert payload["request_headers"]["authorization"] == "<redacted>"
    assert payload["request_headers"]["cookie"] == "<redacted>"
    assert payload["request_headers"]["openai-sentinel-proof-token"] == "<redacted>"
    assert "Set-Cookie: <redacted>" in payload["response_headers"]
    assert payload["response_body"]["kind"] == "base64"


def test_send_handles_richer_live_like_sse_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _build_client()
    client.auth.accessToken = "test-token"
    streamed_tokens: list[str] = []
    state = {
        "requirements_calls": 0,
        "file_create_payloads": [],
        "conversation_payloads": [],
        "uploaded_payloads": [],
        "finalize_calls": 0,
        "stream_events": _live_like_stream_events(),
    }

    with _serve(_make_chat_handler(state)) as base_url:
        _patch_chat_endpoints(monkeypatch, base_url)
        response = client.send(
            "Reply with exactly: scan-ok", on_token=streamed_tokens.append
        )

    assert streamed_tokens == ["scan-", "ok"]
    assert response.text == "scan-ok"
    assert response.title == "Live-like title"
    assert response.conversation.conversation_id == "conv-live"
    assert response.conversation.message_id == "assistant-final"
    assert response.conversation.finish_reason == "stop"
    assert response.request.sent_model == "gpt-5-3-mini"
    assert response.request.sent_reasoning_effort is None
    assert response.request.observed_model == "gpt-5-3-mini"


def test_parse_encoded_stream_item_extracts_last_sse_event() -> None:
    parsed = adapter.ChatGPTWebClient._parse_encoded_stream_item(
        "event: delta\n"
        'data: {"p":"/message/content/parts/0","v":"hello"}\n'
        "\n"
        "event: done\n"
        "data: [DONE]\n"
        "\n"
    )

    assert parsed == {"event": "done", "data": "[DONE]"}


def test_ws_topic_follow_continues_after_intermediate_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _build_client()
    events: list[dict[str, Any]] = []
    sent: list[str] = []
    topic_id = "conversation-turn-long-tool-loop"
    after_done_payload = {"marker": "after-done"}
    encoded_item = (
        "event: message\n"
        f"data: {json.dumps(after_done_payload, separators=(',', ':'))}\n\n"
    )
    frames = [
        json.dumps(
            [
                {
                    "id": 2,
                    "reply": {"catchups": [], "last_offset": "1-0", "recovered": True},
                }
            ]
        ),
        json.dumps(
            [
                {
                    "type": "message",
                    "topic_id": topic_id,
                    "payload": {
                        "type": "conversation-turn-stream",
                        "payload": {"type": "done"},
                    },
                }
            ]
        ),
        json.dumps(
            [
                {
                    "type": "message",
                    "topic_id": topic_id,
                    "payload": {
                        "type": "conversation-turn-stream",
                        "payload": {
                            "type": "stream-item",
                            "encoded_item": encoded_item,
                        },
                    },
                }
            ]
        ),
    ]

    class FakeWebSocket:
        async def send(self, raw: str) -> None:
            sent.append(raw)

        async def recv(self) -> str:
            if not frames:
                raise AssertionError("unexpected recv after post-done stream item")
            return frames.pop(0)

    class FakeConnect:
        async def __aenter__(self):
            return FakeWebSocket()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    import websockets

    monkeypatch.setattr(websockets, "connect", lambda *args, **kwargs: FakeConnect())
    monkeypatch.setattr(
        client,
        "_probe_celsius_ws_user",
        lambda: {"websocket_url": "wss://example.invalid/celsius"},
    )
    monkeypatch.setattr(client, "_build_headers", lambda extra=None: {})
    monkeypatch.setattr(
        client, "_capture_ws_url_diagnostics", lambda websocket_url, state: None
    )
    monkeypatch.setattr(client, "_parse_event", lambda payload, state: ([], None))

    def cancel_check() -> bool:
        return any(
            event.get("type") == "raw_ws_event"
            and event.get("parsed") == after_done_payload
            for event in events
        )

    asyncio.run(
        client._stream_handoff_via_ws_topic_async(
            topic_id,
            state={},
            on_event=events.append,
            on_token=None,
            cancel_check=cancel_check,
            stop_on_done=False,
        )
    )

    event_types = [event["type"] for event in events]
    assert "raw_ws_done" in event_types
    assert "raw_ws_event" in event_types
    assert event_types.index("raw_ws_done") < event_types.index("raw_ws_event")
    after_done_event = next(
        event for event in events if event["type"] == "raw_ws_event"
    )
    assert after_done_event["parsed"] == after_done_payload
    assert len(sent) == 1
