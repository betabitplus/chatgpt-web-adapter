from __future__ import annotations

import json

import chatgpt_web_adapter as adapter


def test_chunked_session_cookies_remove_conflicting_nonchunked_cookie(tmp_path) -> None:
    auth_file = tmp_path / "auth_data.json"
    auth_file.write_text(
        json.dumps(
            {
                "accessToken": "not.a.jwt",
                "sessionToken": "json-fallback",
                "cookies": {
                    "__Secure-next-auth.session-token": "conflicting-fallback",
                    "__Secure-next-auth.session-token.0": "chunk-zero",
                    "__Secure-next-auth.session-token.1": "chunk-one",
                },
            }
        ),
        encoding="utf-8",
    )

    auth = adapter.load_auth_data(auth_file)

    assert "__Secure-next-auth.session-token" not in auth.cookies
    assert auth.cookies["__Secure-next-auth.session-token.0"] == "chunk-zero"
    assert auth.cookies["__Secure-next-auth.session-token.1"] == "chunk-one"
