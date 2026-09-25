from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .exceptions import AuthError

CREDENTIAL_STORE_ENV = "CWA_CREDENTIAL_STORE"
CREDENTIAL_STORE_SERVICE = "chatgpt-web-adapter"
CREDENTIAL_STORE_SCHEMA = 1
CREDENTIAL_STORE_MARKER = "credentialStore"
SUPPORTED_CREDENTIAL_STORES = ("auto", "keyring", "file")


@dataclass(frozen=True)
class CredentialStoreInfo:
    backend: str
    account: str
    keyring_available: bool
    keyring_backend: str | None = None
    metadata_present: bool = False


class _KeyringProvider:
    def _module(self):
        try:
            import keyring
        except ModuleNotFoundError:
            return None
        return keyring

    def status(self) -> tuple[bool, str | None]:
        module = self._module()
        if module is None:
            return False, None
        try:
            backend = module.get_keyring()
            priority = float(getattr(backend, "priority", 0) or 0)
        except Exception:
            return False, None
        name = f"{type(backend).__module__}.{type(backend).__name__}"
        return priority > 0 and _keyring_backend_is_secure(backend), name

    def get(self, service: str, account: str) -> str | None:
        module = self._module()
        if module is None:
            raise AuthError("OS credential store support is not installed")
        try:
            return module.get_password(service, account)
        except Exception as error:
            raise AuthError("Failed to read from the OS credential store") from error

    def set(self, service: str, account: str, value: str) -> None:
        module = self._module()
        if module is None:
            raise AuthError("OS credential store support is not installed")
        try:
            module.set_password(service, account, value)
        except Exception as error:
            raise AuthError("Failed to write to the OS credential store") from error

    def delete(self, service: str, account: str) -> bool:
        module = self._module()
        if module is None:
            raise AuthError("OS credential store support is not installed")
        try:
            module.delete_password(service, account)
            return True
        except Exception as error:
            errors = getattr(module, "errors", None)
            missing = getattr(errors, "PasswordDeleteError", None)
            if missing is not None and isinstance(error, missing):
                return False
            raise AuthError("Failed to delete from the OS credential store") from error


_KEYRING_PROVIDER = _KeyringProvider()

_SECURE_KEYRING_MODULE_PREFIXES = (
    "keyring.backends.macOS",
    "keyring.backends.Windows",
    "keyring.backends.SecretService",
    "keyring.backends.kwallet",
    "keyring.backends.libsecret",
)


def _keyring_backend_is_secure(backend: Any) -> bool:
    """Accept only OS-backed keyring providers, never plaintext/null fallbacks."""

    module = str(type(backend).__module__)
    name = str(type(backend).__name__)
    lowered = f"{module}.{name}".lower()
    if (
        module.startswith("keyrings.alt")
        or "plaintext" in lowered
        or ".fail." in lowered
        or ".null." in lowered
    ):
        return False
    if module.startswith(_SECURE_KEYRING_MODULE_PREFIXES):
        return True
    if module.startswith("keyring.backends.chainer"):
        try:
            children = list(getattr(backend, "backends", ()) or ())
        except Exception:
            return False
        return bool(children) and _keyring_backend_is_secure(children[0])
    return False


def normalize_credential_store(value: str | None = None) -> str:
    raw = value if value is not None else os.getenv(CREDENTIAL_STORE_ENV, "auto")
    normalized = str(raw or "auto").strip().lower()
    if normalized not in SUPPORTED_CREDENTIAL_STORES:
        supported = ", ".join(SUPPORTED_CREDENTIAL_STORES)
        raise AuthError(
            f"Unsupported credential store {normalized!r}; expected one of: {supported}"
        )
    return normalized


def credential_account(auth_file: str | Path) -> str:
    path = Path(auth_file).expanduser()
    try:
        identity = str(path.resolve())
    except OSError:
        identity = str(path.absolute())
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"auth:{digest}"


def keyring_status() -> tuple[bool, str | None]:
    return _KEYRING_PROVIDER.status()


def _read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise AuthError(f"Failed to read auth data from {path}") from error
    except ValueError as error:
        raise AuthError(f"Failed to parse auth data from {path}") from error
    if not isinstance(payload, dict):
        raise AuthError(f"Auth data in {path} must contain a JSON object")
    return payload


def _metadata(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    marker = payload.get(CREDENTIAL_STORE_MARKER)
    if not isinstance(marker, dict):
        return None
    if marker.get("backend") != "keyring":
        return None
    return marker


def _parse_keyring_secret(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise AuthError("OS credential store contains invalid authorization data") from error
    if not isinstance(payload, dict):
        raise AuthError("OS credential store contains invalid authorization data")
    return payload


def _keyring_payload(account: str) -> dict[str, Any] | None:
    available, _ = keyring_status()
    if not available:
        raise AuthError("OS credential store is unavailable")
    return _parse_keyring_secret(
        _KEYRING_PROVIDER.get(CREDENTIAL_STORE_SERVICE, account)
    )


def load_auth_payload(
    auth_file: str | Path,
    *,
    credential_store: str | None = None,
) -> tuple[dict[str, Any] | None, CredentialStoreInfo]:
    path = Path(auth_file).expanduser()
    policy = normalize_credential_store(credential_store)
    account = credential_account(path)
    available, backend_name = keyring_status()
    file_payload = _read_json_object(path)
    marker = _metadata(file_payload)

    if marker is not None:
        marker_account = marker.get("account")
        if isinstance(marker_account, str) and marker_account.strip():
            account = marker_account.strip()
        if not available:
            raise AuthError(
                "Authorization is OS-credential-store-backed, but the OS credential "
                "store is unavailable"
            )
        payload = _keyring_payload(account)
        if payload is None:
            raise AuthError(
                "Authorization metadata exists, but the OS credential store entry is missing"
            )
        return payload, CredentialStoreInfo(
            backend="keyring",
            account=account,
            keyring_available=True,
            keyring_backend=backend_name,
            metadata_present=True,
        )

    if file_payload is not None:
        return file_payload, CredentialStoreInfo(
            backend="file",
            account=account,
            keyring_available=available,
            keyring_backend=backend_name,
            metadata_present=False,
        )

    if policy != "file" and available:
        payload = _keyring_payload(account)
        if payload is not None:
            return payload, CredentialStoreInfo(
                backend="keyring",
                account=account,
                keyring_available=True,
                keyring_backend=backend_name,
                metadata_present=False,
            )

    return None, CredentialStoreInfo(
        backend="file",
        account=account,
        keyring_available=available,
        keyring_backend=backend_name,
        metadata_present=False,
    )


def select_persist_backend(
    auth_file: str | Path,
    *,
    current: CredentialStoreInfo,
    credential_store: str | None = None,
) -> str:
    policy = normalize_credential_store(credential_store)
    if current.backend == "keyring":
        return "keyring"
    if policy == "file":
        return "file"
    if policy == "keyring":
        if not current.keyring_available:
            raise AuthError(
                "OS credential store was explicitly requested but is unavailable"
            )
        return "keyring"
    return "keyring" if current.keyring_available else "file"


def store_keyring_payload(account: str, payload: dict[str, Any]) -> None:
    available, _ = keyring_status()
    if not available:
        raise AuthError("OS credential store is unavailable")
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _KEYRING_PROVIDER.set(CREDENTIAL_STORE_SERVICE, account, serialized)
    verified = _KEYRING_PROVIDER.get(CREDENTIAL_STORE_SERVICE, account)
    if verified != serialized:
        raise AuthError("OS credential store verification failed after write")


def delete_keyring_payload(account: str) -> bool:
    available, _ = keyring_status()
    if not available:
        raise AuthError("OS credential store is unavailable")
    return _KEYRING_PROVIDER.delete(CREDENTIAL_STORE_SERVICE, account)


def keyring_metadata(
    account: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": CREDENTIAL_STORE_SCHEMA,
        CREDENTIAL_STORE_MARKER: {
            "backend": "keyring",
            "service": CREDENTIAL_STORE_SERVICE,
            "account": account,
        },
    }
    for key in (
        "timestamp",
        "accessTokenExpiresAt",
        "expires",
        "sessionExpiresAt",
    ):
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if value is not None:
                result[key] = value
    return result
