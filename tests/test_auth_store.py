from __future__ import annotations

import json
import os
import stat

import pytest

import chatgpt_web_adapter.auth_store as auth_store
from chatgpt_web_adapter.auth_store import persist_auth_data
from chatgpt_web_adapter.types import AuthData


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX mode bits are not portable to Windows"
)
def test_persist_auth_data_repairs_unsafe_existing_mode_and_preserves_unknown_fields(
    tmp_path,
) -> None:
    path = tmp_path / "auth.json"
    path.write_text('{"customMarker":"keep-me"}\n', encoding="utf-8")
    path.chmod(0o644)

    persist_auth_data(AuthData(cookies={"session.0": "chunk"}), path)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["customMarker"] == "keep-me"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(auth_store._auth_lock_path(path).stat().st_mode) == 0o600
