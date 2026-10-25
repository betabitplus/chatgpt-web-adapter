from __future__ import annotations

import threading

import pytest

import chatgpt_web_adapter.credential_store as credential_store
from chatgpt_web_adapter import (
    AuthData,
    AuthError,
    load_auth_data,
    persist_auth_data,
)


class FakeKeyring:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.values: dict[tuple[str, str], str] = {}
        self.fail_set = False
        self.fail_get = False
        self.fail_delete = False
        self.verify_mismatch = False
        self.lock = threading.Lock()

    def status(self) -> tuple[bool, str | None]:
        return self.available, "tests.FakeKeyring" if self.available else None

    def get(self, service: str, account: str) -> str | None:
        if self.fail_get:
            raise AuthError("fake keyring read failed")
        with self.lock:
            value = self.values.get((service, account))
        if value is not None and self.verify_mismatch:
            return value + "-mismatch"
        return value

    def set(self, service: str, account: str, value: str) -> None:
        if self.fail_set:
            raise AuthError("fake keyring write failed")
        with self.lock:
            self.values[(service, account)] = value

    def delete(self, service: str, account: str) -> bool:
        if self.fail_delete:
            raise AuthError("fake keyring delete failed")
        with self.lock:
            return self.values.pop((service, account), None) is not None


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeKeyring) -> None:
    monkeypatch.setattr(credential_store, "_KEYRING_PROVIDER", fake)


def _token(marker: str) -> str:
    return "token-" + marker


def test_keyring_backed_auth_fails_closed_when_os_store_is_unavailable(
    monkeypatch,
    tmp_path,
) -> None:
    fake = FakeKeyring()
    _install_fake(monkeypatch, fake)
    path = tmp_path / "auth.json"
    sensitive = _token("closed")
    persist_auth_data(AuthData(accessToken=sensitive), path)
    fake.available = False

    with pytest.raises(AuthError, match="credential store is unavailable"):
        load_auth_data(path)

    assert sensitive not in path.read_text(encoding="utf-8")
