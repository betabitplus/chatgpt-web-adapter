from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auth import CHATGPT_SESSION_COOKIE, _get_access_token_expiry
from .credential_store import (
    delete_keyring_payload,
    keyring_metadata,
    load_auth_payload,
    select_persist_backend,
    store_keyring_payload,
)
from .exceptions import AuthError
from .file_lock import InterProcessFileLock

_AUTH_LOCK_TIMEOUT_SECONDS = 30.0


def _auth_lock_path(path: Path) -> Path:
    return path.parent / f".{path.name}.lock"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: int | None = None
    if os.name == "nt":
        try:
            existing_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            pass

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary_path = Path(stream.name)

        if os.name == "nt":
            if existing_mode is not None:
                os.chmod(temporary_path, existing_mode)
        else:
            os.chmod(temporary_path, 0o600)

        os.replace(temporary_path, path)
        temporary_path = None

        # The replacement already carries owner-only permissions. Clamp the final
        # path as defense in depth if the platform applies replacement metadata in
        # an unexpected way.
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def persist_auth_data(
    auth: Any,
    auth_file: str | Path,
    *,
    session_token: str | None = None,
    session_expires_at: Any = None,
    credential_store: str | None = None,
) -> Path:
    """Atomically persist reusable auth state under a cross-process write lock."""

    path = Path(auth_file)
    lock = InterProcessFileLock(
        _auth_lock_path(path),
        timeout=_AUTH_LOCK_TIMEOUT_SECONDS,
        timeout_message=f"Authorization store is busy: {path}",
    )
    try:
        with lock:
            current, store_info = load_auth_payload(
                path,
                credential_store=credential_store,
            )
            current = dict(current or {})

            access_token = getattr(auth, "accessToken", None)
            cookies = dict(getattr(auth, "cookies", {}) or {})
            browser_cookies = [
                dict(item)
                for item in (getattr(auth, "browserCookies", []) or [])
                if isinstance(item, dict)
            ]
            headers = dict(getattr(auth, "headers", {}) or {})
            if isinstance(access_token, str) and access_token.strip():
                current["accessToken"] = access_token.strip()
                access_expires = _get_access_token_expiry(access_token)
                if access_expires is not None:
                    current["accessTokenExpiresAt"] = (
                        access_expires.isoformat().replace("+00:00", "Z")
                    )
            if session_token is None:
                cookie_token = cookies.get(CHATGPT_SESSION_COOKIE)
                if isinstance(cookie_token, str) and cookie_token.strip():
                    session_token = cookie_token
            if isinstance(session_token, str) and session_token.strip():
                current["sessionToken"] = session_token.strip()
            if session_expires_at is None:
                session_expires_at = getattr(auth, "expires", None)
            if session_expires_at is not None:
                current["expires"] = session_expires_at
                current["sessionExpiresAt"] = session_expires_at
            current["timestamp"] = (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            )
            current["cookies"] = cookies
            current["browserCookies"] = browser_cookies
            current["headers"] = headers
            current.pop("proof_token", None)
            current.pop("turnstile_token", None)

            backend = select_persist_backend(
                path,
                current=store_info,
                credential_store=credential_store,
            )
            if backend == "keyring":
                store_keyring_payload(store_info.account, current)
                _atomic_write_json(path, keyring_metadata(store_info.account, current))
            else:
                _atomic_write_json(path, current)
    except TimeoutError as error:
        raise AuthError(str(error)) from error
    return path


def migrate_auth_data(
    auth_file: str | Path,
    *,
    backend: str = "keyring",
) -> Path:
    """Explicitly migrate reusable auth material between keyring and file backends."""

    if backend not in {"keyring", "file"}:
        raise AuthError("Credential migration backend must be 'keyring' or 'file'")
    path = Path(auth_file)
    lock = InterProcessFileLock(
        _auth_lock_path(path),
        timeout=_AUTH_LOCK_TIMEOUT_SECONDS,
        timeout_message=f"Authorization store is busy: {path}",
    )
    try:
        with lock:
            payload, store_info = load_auth_payload(path)
            if payload is None:
                raise AuthError("No reusable authorization data is available to migrate")
            if backend == store_info.backend:
                if backend == "file":
                    _atomic_write_json(path, payload)
                else:
                    _atomic_write_json(
                        path,
                        keyring_metadata(store_info.account, payload),
                    )
                return path
            if backend == "keyring":
                if not store_info.keyring_available:
                    raise AuthError("OS credential store is unavailable")
                store_keyring_payload(store_info.account, payload)
                _atomic_write_json(path, keyring_metadata(store_info.account, payload))
                return path

            _atomic_write_json(path, payload)
            try:
                delete_keyring_payload(store_info.account)
            except AuthError as error:
                raise AuthError(
                    "Secure file fallback was written, but the OS credential-store copy "
                    "could not be removed"
                ) from error
            return path
    except TimeoutError as error:
        raise AuthError(str(error)) from error


def clear_auth_data(auth_file: str | Path) -> bool:
    """Remove reusable auth material from the active backend and metadata file."""

    path = Path(auth_file)
    lock = InterProcessFileLock(
        _auth_lock_path(path),
        timeout=_AUTH_LOCK_TIMEOUT_SECONDS,
        timeout_message=f"Authorization store is busy: {path}",
    )
    try:
        with lock:
            payload, store_info = load_auth_payload(path)
            existed = payload is not None or path.exists()
            if store_info.backend == "keyring" and payload is not None:
                delete_keyring_payload(store_info.account)
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                raise AuthError("Failed to remove authorization metadata file") from error
            return existed
    except TimeoutError as error:
        raise AuthError(str(error)) from error
