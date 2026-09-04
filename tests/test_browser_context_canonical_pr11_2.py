from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from chatgpt_web_adapter.browser_context_canonical import (
    BROWSER_CONTEXT_CANONICAL_READ_PLANE,
    BrowserContextCanonicalClient,
    BrowserContextCanonicalReadError,
    _CanonicalReadChunkCollector,
)
from chatgpt_web_adapter.browser_native_provider import BrowserNativeTurnProvider
from chatgpt_web_adapter.browser_owned_product_transport import (
    BrowserOwnedProductTransport,
)

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "src" / "chatgpt_web_adapter" / "browser_native_extension"


def _payload() -> dict:
    return {
        "conversation_id": "conversation-1",
        "title": "Browser canonical",
        "current_node": "assistant-node",
        "mapping": {
            "assistant-node": {
                "id": "assistant-node",
                "parent": None,
                "children": [],
                "message": {
                    "id": "assistant-1",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["done"]},
                    "metadata": {
                        "finish_details": {"type": "stop"},
                        "message_status": "finished_successfully",
                    },
                    "end_turn": True,
                },
            }
        },
    }


def test_chunk_collector_reassembles_exact_sha256_sealed_bytes() -> None:
    body = json.dumps(_payload(), separators=(",", ":")).encode()
    digest = hashlib.sha256(body).hexdigest()
    split = len(body) // 2
    parts = (body[:split], body[split:])
    collector = _CanonicalReadChunkCollector(request_id="request-1")

    for index, part in enumerate(parts):
        collector.add(
            {
                "request_id": "request-1",
                "chunkIndex": index,
                "chunkCount": 2,
                "totalBytes": len(body),
                "sha256": digest,
                "data": base64.b64encode(part).decode(),
            }
        )

    assert collector.finish(
        {
            "chunkCount": 2,
            "totalBytes": len(body),
            "sha256": digest,
        }
    ) == body


def test_chunk_collector_rejects_integrity_mismatch() -> None:
    body = b'{"ok":true}'
    digest = hashlib.sha256(body).hexdigest()
    collector = _CanonicalReadChunkCollector(request_id="request-1")
    collector.add(
        {
            "request_id": "request-1",
            "chunkIndex": 0,
            "chunkCount": 1,
            "totalBytes": len(body),
            "sha256": digest,
            "data": base64.b64encode(body).decode(),
        }
    )

    with pytest.raises(ValueError, match="CANONICAL_READ_FINAL_MANIFEST_MISMATCH"):
        collector.finish(
            {
                "chunkCount": 1,
                "totalBytes": len(body),
                "sha256": "0" * 64,
            }
        )


def test_canonical_error_exports_only_sanitized_metadata() -> None:
    error = BrowserContextCanonicalReadError(
        "<html>secret challenge body</html>",
        conversation_id="conversation-1",
        status_code=403,
        content_type="text/html; charset=utf-8",
    )

    payload = error.to_dict()
    assert error.reason_code == "CANONICAL_READ_FAILED"
    assert payload["status_code"] == 403
    assert payload["content_type"] == "text/html; charset=utf-8"
    assert "secret challenge" not in str(error)
    assert "secret challenge" not in json.dumps(payload)
    assert payload["body_preview"] is None


def test_browser_context_client_owns_terminal_ack_contract(tmp_path) -> None:
    provider = BrowserNativeTurnProvider(state_dir=tmp_path)
    client = BrowserContextCanonicalClient(object(), provider)

    assert callable(client.complete_canonical_readback)
    assert callable(getattr(provider, "set_browser_authority_lease", None))
    assert callable(getattr(provider, "clear_browser_authority_lease", None))
    assert not callable(getattr(provider, "read_conversation", None))
    assert not callable(getattr(provider, "complete_canonical_readback", None))


def test_browser_context_client_keeps_python_status_interpreter(tmp_path, monkeypatch) -> None:
    provider = BrowserNativeTurnProvider(state_dir=tmp_path)
    client = BrowserContextCanonicalClient(object(), provider)
    monkeypatch.setattr(client.transport, "read_conversation", lambda _conversation: _payload())

    status = client.get_status("conversation-1")

    assert status.status == "completed"
    assert status.message_id == "assistant-1"


def test_browser_context_client_lists_real_catalog_payloads(tmp_path, monkeypatch) -> None:
    provider = BrowserNativeTurnProvider(state_dir=tmp_path)
    client = BrowserContextCanonicalClient(object(), provider)
    calls = []

    def fake_catalog(kind, **kwargs):
        calls.append((kind, kwargs))
        if kind == "models":
            return {"models": [{"slug": "gpt-real", "title": "Real"}, {"slug": ""}]}
        archived = kwargs["is_archived"]
        starred = kwargs["is_starred"]
        if not archived and not starred:
            return {
                "items": [
                    {"id": "new", "title": "New", "update_time": "2026-09-04T20:00:00Z"},
                    {"id": "dup", "title": "Old duplicate", "update_time": "2026-09-01T20:00:00Z"},
                ],
                "total": 2,
            }
        if archived and starred:
            return {
                "items": [
                    {"id": "dup", "title": "Latest duplicate", "update_time": "2026-09-05T20:00:00Z"}
                ],
                "total": 1,
            }
        return {"items": [], "total": 0}

    monkeypatch.setattr(client.transport, "read_catalog", fake_catalog)

    conversations = client.list_conversations()
    models = client.list_models()

    assert [item["id"] for item in conversations] == ["dup", "new"]
    assert conversations[0]["title"] == "Latest duplicate"
    assert models == [{"slug": "gpt-real", "title": "Real"}]
    assert sum(kind == "conversations" for kind, _kwargs in calls) == 4
    assert sum(kind == "models" for kind, _kwargs in calls) == 1


def test_conversation_snapshot_reuses_one_canonical_payload(tmp_path, monkeypatch) -> None:
    provider = BrowserNativeTurnProvider(state_dir=tmp_path)
    client = BrowserContextCanonicalClient(object(), provider)
    reads = []

    def fake_read(conversation_id):
        reads.append(conversation_id)
        return _payload()

    monkeypatch.setattr(client, "_get_conversation_payload", fake_read)

    snapshot = client.conversation_snapshot("conversation-1")

    assert reads == ["conversation-1"]
    assert snapshot["status"].status == "completed"
    assert [message.text for message in snapshot["messages"]] == ["done"]


class _Canonical:
    def get_status(self, conversation):
        raise AssertionError("not used during construction")

    def get_messages(self, conversation, **kwargs):
        raise AssertionError("not used during construction")

    def attach_conversation(self, conversation):
        raise AssertionError("not used during construction")


def test_default_browser_owned_transport_assembles_browser_context_read_plane() -> None:
    transport = BrowserOwnedProductTransport(_Canonical())

    assert isinstance(transport.canonical_client, BrowserContextCanonicalClient)
    assert transport.governance()["read_plane"] == BROWSER_CONTEXT_CANONICAL_READ_PLANE
    assert (
        transport.governance()["browser_authority_release_event"]
        == "browser_native_readback_completed"
    )


def test_custom_provider_preserves_legacy_canonical_client_contract() -> None:
    class _CustomProvider:
        def status(self):
            raise AssertionError("not used during construction")

        def send_text(self, *args, **kwargs):
            raise AssertionError("not used during construction")

    canonical = _Canonical()
    transport = BrowserOwnedProductTransport(canonical, provider=_CustomProvider())

    assert transport.canonical_client is canonical
    assert transport.governance()["read_plane"] == "BROWSERLESS_CANONICAL_HTTP"
    assert (
        transport.governance()["browser_authority_release_event"]
        == "browser_native_write_completed"
    )


def test_extension_layers_canonical_read_without_replacing_frozen_boundaries() -> None:
    source = (EXTENSION / "service_worker_canonical_read.js").read_text(encoding="utf-8")
    read = (EXTENSION / "service_worker_runtime_read.js").read_text(encoding="utf-8")
    runtime = (EXTENSION / "service_worker_runtime.js").read_text(encoding="utf-8")
    bootstrap = (
        EXTENSION / "service_worker_browser_runtime_v2.js"
    ).read_text(encoding="utf-8")
    connector = (EXTENSION / "service_worker_connector_support_pr10_0.js").read_text(
        encoding="utf-8"
    )
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["version"] == "0.1.13"
    assert (
        manifest["background"]["service_worker"]
        == "service_worker_browser_runtime_v2.js"
    )
    assert 'importScripts("service_worker_runtime.js");' in bootstrap
    assert bootstrap.rstrip().endswith(
        'importScripts("service_worker_passive_stream_observer.js");'
    )
    assert connector.rstrip().endswith("};")
    citations = 'importScripts("service_worker_product_source_citations_pr9_3.js");'
    canonical = 'importScripts("service_worker_canonical_read.js");'
    assert read.index(citations) < read.index(canonical)
    assert runtime.index('importScripts("service_worker_runtime_write.js");') < runtime.index(
        'importScripts("service_worker_runtime_read.js");'
    )
    assert 'importScripts("service_worker_browser_runtime_v2.js")' not in source
    assert '"catalog_read"' in source
    assert '"models"' in source
    assert '"conversations"' in source

    assert 'credentials: "include"' in source
    assert 'fetch("/api/auth/session"' in source
    assert 'authorization: "Bearer " + token' in source
    assert 'const cacheKey = "__cwaCanonicalAccessTokenV1"' in source
    assert "const cacheTtlMs = 60_000" in source
    assert "globalThis[cacheKey] = { token, cachedAtMs: Date.now() }" in source
    assert "delete globalThis[cacheKey]" in source
    assert "response.arrayBuffer()" in source
    assert 'crypto.subtle.digest("SHA-256", bytes)' in source
    assert "CWA_CANONICAL_CHUNK_BASE64_CHARS = 600_000" in source
    assert 'response.status === 404' in source
    assert 'response.status === 429' in source
    assert '"CANONICAL_READ_RATE_LIMITED"' in source
    assert 'response.status === 404 || response.status === 429' in source
    assert '"CANONICAL_READ_AUTHENTICATION_REQUIRED"' in source
    assert '"CANONICAL_READ_ACCESS_CHALLENGED"' in source
    assert "document.cookie" not in source
    assert "cf_clearance" not in source
    assert "_puid" not in source
    assert "set-cookie" not in source.lower()


def test_host_serializes_write_read_and_close_on_one_authority_lane() -> None:
    source = (
        ROOT / "src" / "chatgpt_web_adapter" / "browser_native_host.py"
    ).read_text(encoding="utf-8")

    assert '"canonical_read"' in source
    assert '"catalog_read"' in source
    assert '"canonical_read_complete"' in source
    assert '"release_runtime_tab"' in source
    assert "_authority_reserved_lease_id" in source
    assert "_reserve_authority_for_readback" in source
    assert "_complete_authority_reservation" in source
    assert 'message.get("type") in {"turn_event", "canonical_read_chunk"}' in source
