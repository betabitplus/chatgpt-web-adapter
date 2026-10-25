from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import chatgpt_web_adapter.doctor as doctor


def test_auth_checks_never_export_token_or_cookie_values(
    monkeypatch, tmp_path: Path
) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(
        doctor,
        "get_auth_status",
        lambda *args, **kwargs: SimpleNamespace(
            auth_file=auth_file,
            file_exists=True,
            access_token_present=True,
            access_token_expires_at=None,
            access_token_needs_refresh=False,
            session_cookie_present=True,
            session_expires_at=None,
            browser_cookie_count=3,
            browser_profile_dir=profile,
            browser_profile_exists=True,
        ),
    )

    payload = json.dumps(
        [
            check.to_dict()
            for check in doctor._auth_checks(auth_file, profile_dir=profile)
        ]
    )

    assert "access_token" not in payload.lower() or "access_token_present" in payload
    assert "session_cookie_present" in payload
    assert "auth.file_permissions" in payload
    assert "secret-token-value" not in payload
    assert "cookie-value" not in payload
