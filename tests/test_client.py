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

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 40


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
