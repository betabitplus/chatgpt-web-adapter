"""The sign-in step that decides which ChatGPT account lands in the auth file.

Only the WKChatGPTAuthority process is faked, with the WK_RESULT line it prints.
The auth file is real; the OS credential store is in memory, never the Keychain.
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
from typing import Any

import pytest

import chatgpt_web_adapter.credential_store as credential_store
from chatgpt_web_adapter import AuthError, load_auth_data, wkwebview_session
from chatgpt_web_adapter.credential_store import load_auth_payload
from chatgpt_web_adapter.wkwebview_session import (
    sign_in_to_browser,
    sync_auth_from_browser,
)

SESSION_COOKIE = "__Secure-next-auth.session-token"
WEBKIT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko)"
)
LOGIN_REQUIRED = {"ok": False, "error": "WKWEBVIEW_AUTHORITY_LOGIN_REQUIRED"}


class MemoryKeyring:
    def __init__(self) -> None:
        self.available = False
        self.values: dict[tuple[str, str], str] = {}

    def status(self) -> tuple[bool, str | None]:
        return self.available, "tests.MemoryKeyring" if self.available else None

    def get(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete(self, service: str, account: str) -> bool:
        return self.values.pop((service, account), None) is not None


@pytest.fixture(autouse=True)
def keyring(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    """Every test gets the in-memory store; unavailable unless a test turns it on."""

    store = MemoryKeyring()
    monkeypatch.setattr(credential_store, "_KEYRING_PROVIDER", store)
    monkeypatch.delenv(credential_store.CREDENTIAL_STORE_ENV, raising=False)
    return store


def _helper_prints(monkeypatch, tmp_path, *results: dict[str, Any]) -> list[list[str]]:
    """Each helper run prints the next result; returns the arguments of every run."""

    binary = tmp_path / "WKChatGPTAuthority"
    pending = list(results)
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        assert argv[0] == str(binary)
        calls.append(list(argv[1:]))
        result = pending.pop(0)
        return subprocess.CompletedProcess(
            argv,
            0 if result["ok"] else 60,
            stdout=f"WK_RESULT {json.dumps(result)}\n",
            stderr="",
        )

    monkeypatch.setattr(wkwebview_session, "_helper_binary", lambda state_dir: binary)
    monkeypatch.setattr(wkwebview_session.subprocess, "run", run)
    return calls


def _access_token(person: str) -> str:
    def part(value: dict[str, Any]) -> str:
        raw = json.dumps(value).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    claims = {
        "https://api.openai.com/auth": {"user_id": f"user-{person}"},
        "exp": int(time.time()) + 10 * 24 * 3600,
    }
    return f"{part({'alg': 'RS256', 'typ': 'JWT'})}.{part(claims)}.signature"


def _cookie(name: str, value: str, *, domain: str = ".chatgpt.com") -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "secure": True,
        "httpOnly": name != "oai-did",
        "expires": 1800000000.0,
    }


def _signed_in(person: str, *, split_session_cookie: bool) -> dict[str, Any]:
    """What `WKChatGPTAuthority --session-export` reports for a signed-in account."""

    if split_session_cookie:
        chunks = [f"{person}-session-chunk-0", f"{person}-session-chunk-1"]
        session_token = "".join(chunks)
        session_cookies = [
            _cookie(f"{SESSION_COOKIE}.0", chunks[0]),
            _cookie(f"{SESSION_COOKIE}.1", chunks[1]),
            # left over from before the session outgrew one cookie
            _cookie(SESSION_COOKIE, f"{person}-stale-whole-session"),
        ]
    else:
        session_token = f"{person}-whole-session"
        session_cookies = [_cookie(SESSION_COOKIE, session_token)]
    return {
        "ok": True,
        "session": {
            "user": {
                "id": f"user-{person}",
                "name": person.title(),
                "email": f"{person}@example.com",
                "idp": "auth0",
                "mfa": False,
            },
            "expires": "2027-01-23T17:00:00.000Z",
            "account": {
                "id": f"account-{person}",
                "planType": "plus",
                "structure": "personal",
            },
            "accessToken": _access_token(person),
            "authProvider": "openai",
            "sessionToken": session_token,
        },
        "cookies": [
            *session_cookies,
            _cookie("oai-did", f"{person}-device"),
            _cookie("__cf_bm", f"{person}-cloudflare"),
            _cookie("auth-session", f"{person}-auth", domain="auth.openai.com"),
        ],
        "user_agent": WEBKIT_USER_AGENT,
        "bundle_id": "local.gptty.webkit-authority",
    }


def test_sync_copies_the_signed_in_browser_session_into_the_auth_file(
    monkeypatch, tmp_path
) -> None:
    auth_file = tmp_path / "auth_data.json"
    alice = _signed_in("alice", split_session_cookie=True)
    calls = _helper_prints(monkeypatch, tmp_path, alice)

    assert sync_auth_from_browser(auth_file) == "alice@example.com"

    assert calls == [["--session-export"]]
    stored = json.loads(auth_file.read_text(encoding="utf-8"))
    assert stored["accessToken"] == alice["session"]["accessToken"]
    assert stored["sessionToken"] == "alice-session-chunk-0alice-session-chunk-1"
    # The split session cookie wins over the stale whole one, as in the browser.
    assert stored["cookies"] == {
        f"{SESSION_COOKIE}.0": "alice-session-chunk-0",
        f"{SESSION_COOKIE}.1": "alice-session-chunk-1",
        "oai-did": "alice-device",
        "__cf_bm": "alice-cloudflare",
    }
    assert stored["headers"]["user-agent"] == WEBKIT_USER_AGENT
    assert load_auth_data(auth_file).accessToken == alice["session"]["accessToken"]


def test_switching_account_leaves_nothing_of_the_previous_one(
    monkeypatch, tmp_path, keyring
) -> None:
    keyring.available = (
        True  # as with gptty on a Mac: the session lives in the OS store
    )
    auth_file = tmp_path / "auth_data.json"
    alice = _signed_in("alice", split_session_cookie=True)
    bob = _signed_in("bob", split_session_cookie=False)
    calls = _helper_prints(monkeypatch, tmp_path, alice, bob)

    assert sync_auth_from_browser(auth_file) == "alice@example.com"
    # In the browser's window the person signs out and into another account.
    assert sign_in_to_browser(auth_file, timeout=300) == "bob@example.com"

    assert calls == [["--session-export"], ["--sign-in", "--timeout", "300"]]
    stored, _ = load_auth_payload(auth_file)
    assert stored["accessToken"] == bob["session"]["accessToken"]
    assert stored["sessionToken"] == "bob-whole-session"
    # A split cookie left from Alice would outrank Bob's whole one and act as Alice.
    assert stored["cookies"] == {
        SESSION_COOKIE: "bob-whole-session",
        "oai-did": "bob-device",
        "__cf_bm": "bob-cloudflare",
    }
    everything = auth_file.read_text(encoding="utf-8") + "".join(
        keyring.values.values()
    )
    assert "alice" not in everything
    assert alice["session"]["accessToken"] not in everything
    auth = load_auth_data(auth_file)
    assert auth.accessToken == bob["session"]["accessToken"]
    assert auth.cookies[SESSION_COOKIE] == "bob-whole-session"


def test_signed_out_browser_asks_to_sign_in_and_keeps_the_stored_sign_in(
    monkeypatch, tmp_path
) -> None:
    auth_file = tmp_path / "auth_data.json"
    alice = _signed_in("alice", split_session_cookie=True)
    _helper_prints(monkeypatch, tmp_path, alice, LOGIN_REQUIRED)
    sync_auth_from_browser(auth_file)
    before = auth_file.read_bytes()

    with pytest.raises(AuthError, match="gptty auth sign-in"):
        sync_auth_from_browser(auth_file)

    assert auth_file.read_bytes() == before
