from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .auth import (
    CHATGPT_SESSION_COOKIE,
    DEFAULT_AUTH_FILE,
    _get_access_token_expiry,
)
from .auth_browser import default_browser_profile_dir
from .auth_refresh import auth_needs_refresh
from .credential_store import load_auth_payload
from .types import AuthData


@dataclass(frozen=True)
class AuthStatus:
    auth_file: Path
    file_exists: bool
    access_token_present: bool
    access_token_expires_at: datetime | None
    access_token_needs_refresh: bool
    session_cookie_present: bool
    session_expires_at: Any = None
    cookies_present: bool = False
    browser_cookie_count: int = 0
    headers_present: bool = False
    proof_token_present: bool = False
    turnstile_token_present: bool = False
    browser_profile_dir: Path | None = None
    browser_profile_exists: bool = False
    credential_backend: str = "file"
    keyring_available: bool = False
    keyring_backend: str | None = None
    credential_metadata_present: bool = False
    captured_at: str | None = None


def get_auth_status(
    auth_file: str | Path = DEFAULT_AUTH_FILE,
    *,
    profile_dir: str | Path | None = None,
) -> AuthStatus:
    path = Path(auth_file)
    profile = (
        Path(profile_dir)
        if profile_dir is not None
        else default_browser_profile_dir()
    )
    payload, store_info = load_auth_payload(path)
    if payload is None:
        return AuthStatus(
            auth_file=path,
            file_exists=path.is_file(),
            access_token_present=False,
            access_token_expires_at=None,
            access_token_needs_refresh=True,
            session_cookie_present=False,
            session_expires_at=None,
            cookies_present=False,
            browser_cookie_count=0,
            headers_present=False,
            proof_token_present=False,
            turnstile_token_present=False,
            browser_profile_dir=profile,
            browser_profile_exists=profile.is_dir(),
            credential_backend=store_info.backend,
            keyring_available=store_info.keyring_available,
            keyring_backend=store_info.keyring_backend,
            credential_metadata_present=store_info.metadata_present,
            captured_at=None,
        )
    auth = AuthData.from_mapping(payload)
    has_session = any(
        name == CHATGPT_SESSION_COOKIE or name.startswith(f"{CHATGPT_SESSION_COOKIE}.")
        for name in auth.cookies
    )
    if not has_session:
        session_token = payload.get("sessionToken")
        has_session = isinstance(session_token, str) and bool(session_token.strip())
    expires_at = _get_access_token_expiry(auth.accessToken)
    return AuthStatus(
        auth_file=path,
        file_exists=path.is_file(),
        access_token_present=bool(auth.accessToken),
        access_token_expires_at=expires_at,
        access_token_needs_refresh=auth_needs_refresh(auth.accessToken),
        session_cookie_present=has_session,
        session_expires_at=auth.expires,
        cookies_present=bool(auth.cookies),
        browser_cookie_count=len(auth.browserCookies),
        headers_present=bool(auth.headers),
        proof_token_present=auth.proof_token is not None,
        turnstile_token_present=bool(auth.turnstile_token),
        browser_profile_dir=profile,
        browser_profile_exists=profile.is_dir(),
        credential_backend=store_info.backend,
        keyring_available=store_info.keyring_available,
        keyring_backend=store_info.keyring_backend,
        credential_metadata_present=store_info.metadata_present,
        captured_at=(
            str(payload.get("timestamp"))
            if isinstance(payload.get("timestamp"), str)
            else None
        ),
    )
