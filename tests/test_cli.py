from __future__ import annotations

import json
from types import SimpleNamespace

from chatgpt_web_adapter import cli
from chatgpt_web_adapter.auth import CHATGPT_SESSION_COOKIE


def test_auth_status_cli_reports_profile_and_structured_cookies(
    tmp_path, capsys
) -> None:
    auth_file = tmp_path / "auth.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    auth_file.write_text(
        json.dumps(
            {
                "accessToken": "not.a.jwt",
                "cookies": {CHATGPT_SESSION_COOKIE: "session"},
                "browserCookies": [
                    {
                        "name": CHATGPT_SESSION_COOKIE,
                        "value": "session",
                        "domain": ".chatgpt.com",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cli.main(
        [
            "auth",
            "status",
            "--auth-file",
            str(auth_file),
            "--profile-dir",
            str(profile),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["browser_cookie_count"] == 1
    assert payload["browser_profile_exists"] is True


def test_auth_login_force_disables_saved_auth_reuse(tmp_path, monkeypatch, capsys) -> None:
    captured = {}

    def login(path, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(auth_file=path, profile_dir=tmp_path / "profile")

    monkeypatch.setattr(cli, "browser_login", login)

    result = cli.main(
        [
            "auth",
            "login",
            "--auth-file",
            str(tmp_path / "auth.json"),
            "--force",
        ]
    )

    assert result == 0
    assert captured["reuse_existing_auth"] is False
    assert "Authorization saved" in capsys.readouterr().out


def test_auth_login_forwards_explicit_credential_store(tmp_path, monkeypatch) -> None:
    captured = {}

    def login(path, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(auth_file=path, profile_dir=tmp_path / "profile")

    monkeypatch.setattr(cli, "browser_login", login)

    result = cli.main([
        "auth",
        "login",
        "--auth-file",
        str(tmp_path / "auth.json"),
        "--credential-store",
        "file",
    ])

    assert result == 0
    assert captured["credential_store"] == "file"


def test_auth_migrate_cli_reports_backend(tmp_path, monkeypatch, capsys) -> None:
    auth_file = tmp_path / "auth.json"
    calls = {}

    def migrate(path, *, backend):
        calls["path"] = path
        calls["backend"] = backend

    monkeypatch.setattr(cli, "migrate_auth_data", migrate)
    monkeypatch.setattr(
        cli,
        "get_auth_status",
        lambda _path: SimpleNamespace(
            credential_backend="keyring",
            keyring_available=True,
        ),
    )

    result = cli.main([
        "auth",
        "migrate",
        "--auth-file",
        str(auth_file),
        "--backend",
        "keyring",
    ])

    assert result == 0
    assert calls == {"path": auth_file, "backend": "keyring"}
    payload = json.loads(capsys.readouterr().out)
    assert payload["credential_backend"] == "keyring"


def test_auth_logout_cli_delegates_to_store(tmp_path, monkeypatch, capsys) -> None:
    auth_file = tmp_path / "auth.json"
    calls = []
    monkeypatch.setattr(
        cli,
        "clear_auth_data",
        lambda path: calls.append(path) or True,
    )

    result = cli.main(["auth", "logout", "--auth-file", str(auth_file)])

    assert result == 0
    assert calls == [auth_file]
    assert json.loads(capsys.readouterr().out) == {"removed": True}
