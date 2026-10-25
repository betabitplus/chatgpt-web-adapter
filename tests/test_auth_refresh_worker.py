from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from chatgpt_web_adapter import auth_refresh_worker
from chatgpt_web_adapter.auth import CHATGPT_SESSION_COOKIE


def test_worker_does_not_forward_credentials_across_redirect(monkeypatch) -> None:
    requests = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(
                {
                    "path": self.path,
                    "cookie": self.headers.get("cookie"),
                    "authorization": self.headers.get("authorization"),
                }
            )
            if self.path == "/source":
                self.send_response(302)
                self.send_header(
                    "location",
                    f"http://127.0.0.1:{self.server.server_port}/destination",
                )
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    server_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )
    server_thread.start()
    monkeypatch.setattr(
        auth_refresh_worker,
        "SESSION_URL",
        f"http://127.0.0.1:{server.server_port}/source",
    )
    try:
        result = auth_refresh_worker.fetch_session(
            {
                "cookie": f"{CHATGPT_SESSION_COOKIE}=fake-private-session",
                "authorization": "Bearer fake-private-access",
            },
            2.0,
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2.0)

    assert result == {"status": 302, "data": None, "set_cookie_headers": []}
    assert requests == [
        {
            "path": "/source",
            "cookie": f"{CHATGPT_SESSION_COOKIE}=fake-private-session",
            "authorization": None,
        }
    ]
