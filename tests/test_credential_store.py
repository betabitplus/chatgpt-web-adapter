from __future__ import annotations

import json
import threading

import pytest

import chatgpt_web_adapter.credential_store as credential_store
from chatgpt_web_adapter import (
    AuthData,
    AuthError,
    clear_auth_data,
    load_auth_data,
    migrate_auth_data,
    persist_auth_data,
)
from chatgpt_web_adapter.auth_status import get_auth_status


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


def test_auto_uses_secure_file_when_keyring_is_unavailable(monkeypatch, tmp_path) -> None:
    fake = FakeKeyring(available=False)
    _install_fake(monkeypatch, fake)
    path = tmp_path / "auth.json"

    persist_auth_data(AuthData(accessToken=_token("file")), path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["access" + "Token"] == _token("file")
    assert credential_store.CREDENTIAL_STORE_MARKER not in payload
    assert get_auth_status(path).credential_backend == "file"


def test_auto_migrates_existing_file_to_keyring_without_leaving_secrets(
    monkeypatch,
    tmp_path,
) -> None:
    fake = FakeKeyring()
    _install_fake(monkeypatch, fake)
    path = tmp_path / "auth.json"
    sensitive = _token("migrate")
    path.write_text(
        json.dumps({"access" + "Token": sensitive, "customMarker": "preserve"}),
        encoding="utf-8",
    )

    persist_auth_data(AuthData(cookies={"session": "cookie-value"}), path)

    metadata = json.loads(path.read_text(encoding="utf-8"))
    assert metadata[credential_store.CREDENTIAL_STORE_MARKER]["backend"] == "keyring"
    assert sensitive not in path.read_text(encoding="utf-8")
    assert "cookie-value" not in path.read_text(encoding="utf-8")
    loaded = load_auth_data(path)
    assert loaded.accessToken == sensitive
    assert loaded.cookies["session"] == "cookie-value"
    account = credential_store.credential_account(path)
    raw = fake.values[(credential_store.CREDENTIAL_STORE_SERVICE, account)]
    assert json.loads(raw)["customMarker"] == "preserve"
    assert get_auth_status(path).credential_backend == "keyring"


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


def test_failed_keyring_migration_preserves_last_usable_plaintext_file(
    monkeypatch,
    tmp_path,
) -> None:
    fake = FakeKeyring()
    fake.fail_set = True
    _install_fake(monkeypatch, fake)
    path = tmp_path / "auth.json"
    sensitive = _token("survive")
    original = json.dumps({"access" + "Token": sensitive}) + "\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(AuthError, match="fake keyring write failed"):
        migrate_auth_data(path, backend="keyring")

    assert path.read_text(encoding="utf-8") == original
    assert load_auth_data(path).accessToken == sensitive


def test_failed_keyring_verification_preserves_plaintext_file(monkeypatch, tmp_path) -> None:
    fake = FakeKeyring()
    fake.verify_mismatch = True
    _install_fake(monkeypatch, fake)
    path = tmp_path / "auth.json"
    sensitive = _token("verify")
    original = json.dumps({"access" + "Token": sensitive}) + "\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(AuthError, match="verification failed"):
        migrate_auth_data(path, backend="keyring")

    assert path.read_text(encoding="utf-8") == original


def test_explicit_file_migration_writes_file_before_removing_keyring(
    monkeypatch,
    tmp_path,
) -> None:
    fake = FakeKeyring()
    _install_fake(monkeypatch, fake)
    path = tmp_path / "auth.json"
    sensitive = _token("portable")
    persist_auth_data(AuthData(accessToken=sensitive), path)
    account = credential_store.credential_account(path)
    assert (credential_store.CREDENTIAL_STORE_SERVICE, account) in fake.values

    migrate_auth_data(path, backend="file")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["access" + "Token"] == sensitive
    assert (credential_store.CREDENTIAL_STORE_SERVICE, account) not in fake.values
    assert get_auth_status(path).credential_backend == "file"


def test_logout_removes_keyring_entry_and_metadata(monkeypatch, tmp_path) -> None:
    fake = FakeKeyring()
    _install_fake(monkeypatch, fake)
    path = tmp_path / "auth.json"
    persist_auth_data(AuthData(accessToken=_token("logout")), path)
    account = credential_store.credential_account(path)

    assert clear_auth_data(path) is True

    assert not path.exists()
    assert (credential_store.CREDENTIAL_STORE_SERVICE, account) not in fake.values
    with pytest.raises(AuthError, match="No access token found"):
        load_auth_data(path)


def test_concurrent_keyring_updates_preserve_independent_fields(monkeypatch, tmp_path) -> None:
    fake = FakeKeyring()
    _install_fake(monkeypatch, fake)
    path = tmp_path / "auth.json"
    persist_auth_data(AuthData(accessToken=_token("seed")), path)
    barrier = threading.Barrier(3)
    errors: list[BaseException] = []

    def write_token() -> None:
        try:
            barrier.wait(timeout=2)
            persist_auth_data(AuthData(accessToken=_token("new")), path)
        except BaseException as error:
            errors.append(error)

    def write_session() -> None:
        try:
            barrier.wait(timeout=2)
            persist_auth_data(
                AuthData(),
                path,
                session_token="session-marker",
            )
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=write_token), threading.Thread(target=write_session)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert not errors
    loaded = load_auth_data(path)
    assert loaded.accessToken == _token("new")
    assert loaded.cookies["__Secure-next-auth.session-token"] == "session-marker"


def test_explicit_keyring_backend_unavailable_does_not_create_auth_file(
    monkeypatch,
    tmp_path,
) -> None:
    fake = FakeKeyring(available=False)
    _install_fake(monkeypatch, fake)
    path = tmp_path / "auth.json"

    with pytest.raises(AuthError, match="explicitly requested.*unavailable"):
        persist_auth_data(
            AuthData(accessToken=_token("required")),
            path,
            credential_store="keyring",
        )

    assert not path.exists()


def test_logout_delete_failure_keeps_keyring_metadata_for_safe_retry(
    monkeypatch,
    tmp_path,
) -> None:
    fake = FakeKeyring()
    _install_fake(monkeypatch, fake)
    path = tmp_path / "auth.json"
    sensitive = _token("logout-retry")
    persist_auth_data(AuthData(accessToken=sensitive), path)
    before = path.read_text(encoding="utf-8")
    fake.fail_delete = True

    with pytest.raises(AuthError, match="fake keyring delete failed"):
        clear_auth_data(path)

    assert path.read_text(encoding="utf-8") == before
    assert sensitive not in before
    fake.fail_delete = False
    assert load_auth_data(path).accessToken == sensitive


def test_file_migration_delete_failure_keeps_both_usable_copies(
    monkeypatch,
    tmp_path,
) -> None:
    fake = FakeKeyring()
    _install_fake(monkeypatch, fake)
    path = tmp_path / "auth.json"
    sensitive = _token("duplicate-safe")
    persist_auth_data(AuthData(accessToken=sensitive), path)
    account = credential_store.credential_account(path)
    fake.fail_delete = True

    with pytest.raises(AuthError, match="could not be removed"):
        migrate_auth_data(path, backend="file")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["access" + "Token"] == sensitive
    assert (credential_store.CREDENTIAL_STORE_SERVICE, account) in fake.values
    fake.fail_delete = False
    assert load_auth_data(path).accessToken == sensitive


def _backend(module: str, name: str = "Keyring", *, children=None):
    cls = type(name, (), {"priority": 1})
    cls.__module__ = module
    backend = cls()
    if children is not None:
        backend.backends = list(children)
    return backend


def test_secure_backend_policy_accepts_os_stores_and_rejects_plaintext() -> None:
    secure = _backend("keyring.backends.macOS")
    insecure = _backend("keyrings.alt.file", "PlaintextKeyring")
    unknown = _backend("thirdparty.keyring")
    secure_chain = _backend(
        "keyring.backends.chainer",
        "ChainerBackend",
        children=[secure, insecure],
    )
    insecure_chain = _backend(
        "keyring.backends.chainer",
        "ChainerBackend",
        children=[insecure, secure],
    )

    assert credential_store._keyring_backend_is_secure(secure) is True
    assert credential_store._keyring_backend_is_secure(insecure) is False
    assert credential_store._keyring_backend_is_secure(unknown) is False
    assert credential_store._keyring_backend_is_secure(secure_chain) is True
    assert credential_store._keyring_backend_is_secure(insecure_chain) is False


def test_keyring_only_recovery_state_is_usable_and_migration_repairs_metadata(
    monkeypatch,
    tmp_path,
) -> None:
    fake = FakeKeyring()
    _install_fake(monkeypatch, fake)
    path = tmp_path / "auth.json"
    account = credential_store.credential_account(path)
    sensitive = _token("orphan-recovery")
    fake.values[(credential_store.CREDENTIAL_STORE_SERVICE, account)] = json.dumps(
        {
            "access" + "Token": sensitive,
            "cookies": {},
            "browserCookies": [],
            "headers": {},
        }
    )

    status = get_auth_status(path)

    assert status.file_exists is False
    assert status.credential_backend == "keyring"
    assert status.credential_metadata_present is False
    assert status.access_token_present is True
    assert load_auth_data(path).accessToken == sensitive

    migrate_auth_data(path, backend="keyring")

    metadata_text = path.read_text(encoding="utf-8")
    assert sensitive not in metadata_text
    assert json.loads(metadata_text)[credential_store.CREDENTIAL_STORE_MARKER][
        "backend"
    ] == "keyring"
    repaired = get_auth_status(path)
    assert repaired.file_exists is True
    assert repaired.credential_metadata_present is True


def test_metadata_write_failure_keeps_keyring_copy_recoverable(
    monkeypatch,
    tmp_path,
) -> None:
    import chatgpt_web_adapter.auth_store as auth_store

    fake = FakeKeyring()
    _install_fake(monkeypatch, fake)
    path = tmp_path / "auth.json"
    sensitive = _token("metadata-failure")
    original_write = auth_store._atomic_write_json

    def fail_metadata(*_args, **_kwargs):
        raise OSError("metadata write failed")

    monkeypatch.setattr(auth_store, "_atomic_write_json", fail_metadata)
    with pytest.raises(OSError, match="metadata write failed"):
        persist_auth_data(AuthData(accessToken=sensitive), path)
    assert not path.exists()
    assert load_auth_data(path).accessToken == sensitive

    monkeypatch.setattr(auth_store, "_atomic_write_json", original_write)
    migrate_auth_data(path, backend="keyring")
    assert path.is_file()
    assert sensitive not in path.read_text(encoding="utf-8")
