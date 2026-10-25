"""The WKWebView helper's own ChatGPT sign-in as the one source of reusable auth.

The helper's WebKit store is a real, self-renewing ChatGPT web session: the
WKWebView backend builds every protected write in it. Copying that session into
the auth file before use keeps the token and cookies the HTTP parts of a turn
use (stream reads, final reads, uploads) on the same account, and makes signing
in or switching accounts a single step: `sign_in_to_browser` shows chatgpt.com
in the helper's window, the person signs in (or out and into another account)
and closes it.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .auth import CHAT_URL
from .auth_store import persist_auth_data
from .browser_cookies import flatten_browser_cookies, serialize_browser_cookies
from .exceptions import AuthError
from .types import AuthData

_RESULT_PREFIX = "WK_RESULT "
_synced: set[str] = set()
_synced_lock = threading.Lock()


def _helper_binary(state_dir: str | Path | None) -> Path:
    from .wkwebview_provider import WKWebViewTurnProvider

    return WKWebViewTurnProvider(state_dir=state_dir)._ensure_helper()


def _run_helper(args: list[str], timeout: float, state_dir: str | Path | None) -> dict[str, Any]:
    helper = _helper_binary(state_dir)
    try:
        completed = subprocess.run(
            [str(helper), *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise AuthError("The ChatGPT browser session did not answer in time") from error
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(_RESULT_PREFIX):
            return json.loads(line[len(_RESULT_PREFIX):])
    raise AuthError(f"The ChatGPT browser session gave no result: {(completed.stderr or '').strip()[-500:]}")


def _account(session: dict[str, Any]) -> str | None:
    user = session.get("user")
    return user.get("email") if isinstance(user, dict) else None


def _persist(result: dict[str, Any], auth_file: str | Path, credential_store: str | None) -> str:
    if not result.get("ok"):
        if result.get("error") == "WKWEBVIEW_AUTHORITY_LOGIN_REQUIRED":
            raise AuthError("The ChatGPT browser is not signed in: run `gptty auth sign-in`")
        raise AuthError(f"Reading the ChatGPT browser session failed: {result.get('error')}")
    session = result["session"]
    records = serialize_browser_cookies(
        [
            SimpleNamespace(
                name=c["name"], value=c["value"], domain=c["domain"], path=c.get("path") or "/",
                secure=bool(c.get("secure")), http_only=bool(c.get("httpOnly")), expires=c.get("expires"),
            )
            for c in result.get("cookies") or []
        ]
    )
    cookies = flatten_browser_cookies(records)
    if any(name.startswith("__Secure-next-auth.session-token.") for name in cookies):
        # A split session cookie wins over a stale whole one, as in the browser.
        cookies.pop("__Secure-next-auth.session-token", None)
    headers = {"accept": "*/*", "accept-language": "en-US,en;q=0.8", "referer": CHAT_URL}
    if result.get("user_agent"):
        headers["user-agent"] = result["user_agent"]
    auth = AuthData(
        accessToken=session["accessToken"].strip(),
        accessTokenSource="wkwebview-session:accessToken",
        cookies=cookies,
        browserCookies=records,
        headers=headers,
        expires=session.get("expires"),
    )
    persist_auth_data(
        auth,
        auth_file,
        session_token=(session.get("sessionToken") or "").strip() or None,
        session_expires_at=session.get("expires"),
        credential_store=credential_store,
    )
    return _account(session) or "unknown account"


def sync_auth_from_browser(
    auth_file: str | Path,
    *,
    credential_store: str | None = None,
    state_dir: str | Path | None = None,
    once_per_process: bool = False,
) -> str | None:
    """Copy the browser's current ChatGPT session into the auth file; returns its account (None: already done)."""

    key = str(Path(auth_file).expanduser().resolve())
    if once_per_process:
        with _synced_lock:
            if key in _synced:
                return None
    account = _persist(_run_helper(["--session-export"], 90.0, state_dir), auth_file, credential_store)
    with _synced_lock:
        _synced.add(key)
    return account


def sign_in_to_browser(
    auth_file: str | Path,
    *,
    credential_store: str | None = None,
    state_dir: str | Path | None = None,
    timeout: float = 900.0,
) -> str:
    """Show chatgpt.com in the browser's window; once it is closed, copy its session into the auth file.

    Returns the signed-in account."""

    result = _run_helper(["--sign-in", "--timeout", str(int(timeout))], timeout + 60.0, state_dir)
    return _persist(result, auth_file, credential_store)


__all__ = ["sign_in_to_browser", "sync_auth_from_browser"]
