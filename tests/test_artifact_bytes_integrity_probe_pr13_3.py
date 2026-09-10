from __future__ import annotations

import hashlib
from typing import Any

import tools.pr13_3_artifact_bytes_integrity_probe as probe_mod
from tools.pr13_3_artifact_bytes_integrity_probe import (
    _extract_resolution_locator,
    _locator_policy,
    characterize_integrity,
    probe_locator_bytes,
    run_integrity_gate,
)

PAYLOAD = b"CWA_PR13_1_CONVERSATION_FILES_IDENTITY_PROBE\n"
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()


class _Client:
    def __init__(
        self,
        *,
        json_responses: list[tuple[int, Any]] | None = None,
        raw_response: tuple[int, bytes, str] = (200, PAYLOAD, ""),
    ) -> None:
        self.json_responses = list(json_responses or [])
        self.raw_response = raw_response
        self.base_headers = {
            "user-agent": "cwa-test",
            "authorization": "Bearer base-secret",
            "cookie": "session=base-secret",
        }
        self.json_calls: list[tuple[str, str, Any, dict[str, str]]] = []
        self.raw_calls: list[dict[str, Any]] = []

    def _build_headers(self, additions: dict[str, str]) -> dict[str, str]:
        headers = dict(self.base_headers)
        headers.update(additions)
        return headers

    def _json_request(self, method, url, payload, headers):
        self.json_calls.append((method, url, payload, headers))
        return self.json_responses.pop(0)

    def _run_curl(
        self,
        method,
        url,
        headers,
        body=None,
        *,
        persist_cookies=True,
        follow_redirects=False,
    ):
        self.raw_calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "persist_cookies": persist_cookies,
                "follow_redirects": follow_redirects,
            }
        )
        return self.raw_response


def _patch_clean_head(monkeypatch, head: str = "head-1") -> None:
    monkeypatch.setattr(
        probe_mod,
        "_git_output",
        lambda *args: head if args == ("rev-parse", "HEAD") else "",
    )


def test_locator_policy_allows_only_known_https_origins() -> None:
    assert _locator_policy("https://chatgpt.com/backend-api/estuary/content?id=x") == {
        "locator_allowed": True,
        "locator_origin_class": "CHATGPT_SAME_ORIGIN",
        "attach_chatgpt_auth": True,
    }
    assert _locator_policy("https://files.oaiusercontent.com/signed") == {
        "locator_allowed": True,
        "locator_origin_class": "OAIUSERCONTENT",
        "attach_chatgpt_auth": False,
    }
    assert _locator_policy("http://chatgpt.com/file")["locator_allowed"] is False
    assert _locator_policy("https://example.com/file")["locator_allowed"] is False
    assert (
        _locator_policy("https://user:pass@chatgpt.com/file")["locator_allowed"]
        is False
    )
    assert _locator_policy("https://chatgpt.com:444/file")["locator_allowed"] is False


def test_extract_resolution_locator_keeps_value_internal() -> None:
    key, locator = _extract_resolution_locator(
        {"download_url": "https://chatgpt.com/backend-api/estuary/content?sig=secret"}
    )

    assert key == "download_url"
    assert locator is not None
    assert "secret" in locator


def test_same_origin_locator_uses_auth_but_exports_no_bytes_or_locator() -> None:
    client = _Client()

    report = probe_locator_bytes(
        client,
        conversation_id="conversation-1",
        locator="https://chatgpt.com/backend-api/estuary/content?id=x&sig=secret",
    )

    assert report["characterization"] == "LOCATOR_BYTES_OBSERVED"
    assert report["request_count"] == 1
    assert report["bytes_observed"] is True
    assert report["byte_count"] == len(PAYLOAD)
    assert report["sha256"] == PAYLOAD_SHA256
    assert report["bytes_exported"] is False
    assert report["locator_value_exported"] is False
    assert report["artifact_disk_write_attempted"] is False

    call = client.raw_calls[0]
    assert call["headers"]["authorization"] == "Bearer base-secret"
    assert call["headers"]["cookie"] == "session=base-secret"
    assert call["persist_cookies"] is False
    assert call["follow_redirects"] is False
    assert "secret" not in str(report)
    assert PAYLOAD.decode().strip() not in str(report)


def test_oaiusercontent_locator_never_receives_chatgpt_credentials() -> None:
    client = _Client()

    report = probe_locator_bytes(
        client,
        conversation_id="conversation-1",
        locator="https://files.oaiusercontent.com/signed?sig=secret",
    )

    assert report["characterization"] == "LOCATOR_BYTES_OBSERVED"
    call = client.raw_calls[0]
    assert "authorization" not in call["headers"]
    assert "cookie" not in call["headers"]
    assert call["headers"]["user-agent"] == "cwa-test"
    assert "secret" not in str(report)


def test_redirect_is_not_followed() -> None:
    client = _Client(raw_response=(302, b"", "location: https://example.invalid"))

    report = probe_locator_bytes(
        client,
        conversation_id="conversation-1",
        locator="https://chatgpt.com/backend-api/estuary/content?id=x",
    )

    assert report["characterization"] == "LOCATOR_REDIRECT_NOT_FOLLOWED"
    assert report["bytes_observed"] is False
    assert client.raw_calls[0]["follow_redirects"] is False


def test_integrity_requires_exact_size_and_sha256() -> None:
    byte_report = {
        "bytes_observed": True,
        "byte_count": len(PAYLOAD),
        "sha256": PAYLOAD_SHA256,
        "request_count": 1,
        "status_code": 200,
        "locator_origin_class": "CHATGPT_SAME_ORIGIN",
    }

    report = characterize_integrity(
        byte_report,
        expected_size=len(PAYLOAD),
        expected_sha256=PAYLOAD_SHA256,
    )

    assert (
        report["characterization"] == "IDENTITY_BOUND_ARTIFACT_BYTES_INTEGRITY_OBSERVED"
    )
    assert report["size_matches"] is True
    assert report["sha256_matches"] is True
    assert report["artifact_bytes_proven"] is True
    assert report["artifact_integrity_proven"] is True
    assert report["identity_bound_byte_retrieval_proven"] is True
    assert report["stable_product_identity_proven"] is False
    assert report["download_authority_granted"] is False


def test_integrity_mismatch_fails_closed() -> None:
    byte_report = {
        "bytes_observed": True,
        "byte_count": len(PAYLOAD),
        "sha256": PAYLOAD_SHA256,
        "request_count": 1,
        "status_code": 200,
        "locator_origin_class": "CHATGPT_SAME_ORIGIN",
    }

    report = characterize_integrity(
        byte_report,
        expected_size=len(PAYLOAD),
        expected_sha256="0" * 64,
    )

    assert report["characterization"] == "ARTIFACT_BYTES_OR_INTEGRITY_NOT_PROVEN"
    assert report["artifact_bytes_proven"] is False
    assert report["artifact_integrity_proven"] is False
    assert report["identity_bound_byte_retrieval_proven"] is False


def test_gate_proves_identity_bound_bytes_without_disk_write(monkeypatch) -> None:
    _patch_clean_head(monkeypatch)
    locator = "https://chatgpt.com/backend-api/estuary/content?id=x&sig=secret"
    client = _Client(
        json_responses=[
            (
                200,
                {
                    "items": [
                        {
                            "file_id": "file_abc123",
                            "file_name": "result.txt",
                            "mime_type": "text/plain",
                        }
                    ]
                },
            ),
            (
                200,
                {
                    "download_url": locator,
                    "file_name": "result.txt",
                    "file_size_bytes": len(PAYLOAD),
                },
            ),
        ]
    )

    report = run_integrity_gate(
        conversation_id="conversation-1",
        expected_filename="result.txt",
        expected_size=len(PAYLOAD),
        expected_sha256=PAYLOAD_SHA256,
        expected_head="head-1",
        timeout=1.0,
        client_factory=lambda _timeout: client,
    )

    assert (
        report["characterization"] == "IDENTITY_BOUND_ARTIFACT_BYTES_INTEGRITY_OBSERVED"
    )
    assert report["request_count"] == 3
    assert report["identity_discovery_request_count"] == 1
    assert report["resolution_request_count"] == 1
    assert report["locator_fetch_request_count"] == 1
    assert report["identity_key"] == "file_id"
    assert report["resolution_locator_field_present"] is True
    assert report["artifact_bytes_proven"] is True
    assert report["artifact_integrity_proven"] is True
    assert report["download_attempted"] is True
    assert report["artifact_disk_write_attempted"] is False
    assert report["materialization_attempted"] is False
    assert report["download_authority_granted"] is False
    assert report["identity_values_exported"] is False
    assert report["locator_values_exported"] is False
    assert report["artifact_bytes_exported"] is False
    assert "file_abc123" not in str(report)
    assert "secret" not in str(report)
    assert PAYLOAD.decode().strip() not in str(report)

    assert len(client.json_calls) == 2
    assert len(client.raw_calls) == 1
    assert "file_abc123" in client.json_calls[1][1]


def test_gate_rejects_unrecognized_locator_before_byte_fetch(monkeypatch) -> None:
    _patch_clean_head(monkeypatch)
    client = _Client(
        json_responses=[
            (
                200,
                {"items": [{"file_id": "file_abc123", "file_name": "result.txt"}]},
            ),
            (
                200,
                {"download_url": "https://example.com/signed?sig=secret"},
            ),
        ]
    )

    report = run_integrity_gate(
        conversation_id="conversation-1",
        expected_filename="result.txt",
        expected_size=len(PAYLOAD),
        expected_sha256=PAYLOAD_SHA256,
        expected_head="head-1",
        timeout=1.0,
        client_factory=lambda _timeout: client,
    )

    assert report["characterization"] == "LOCATOR_ORIGIN_REJECTED"
    assert report["request_count"] == 2
    assert report["locator_fetch_request_count"] == 0
    assert report["artifact_bytes_proven"] is False
    assert client.raw_calls == []
    assert "example.com" not in str(report)
    assert "secret" not in str(report)


def test_gate_stops_when_resolver_has_no_locator(monkeypatch) -> None:
    _patch_clean_head(monkeypatch)
    client = _Client(
        json_responses=[
            (
                200,
                {"items": [{"file_id": "file_abc123", "file_name": "result.txt"}]},
            ),
            (200, {"status": "success"}),
        ]
    )

    report = run_integrity_gate(
        conversation_id="conversation-1",
        expected_filename="result.txt",
        expected_size=len(PAYLOAD),
        expected_sha256=PAYLOAD_SHA256,
        expected_head="head-1",
        timeout=1.0,
        client_factory=lambda _timeout: client,
    )

    assert report["characterization"] == "RESOLUTION_LOCATOR_NOT_PROVEN"
    assert report["request_count"] == 2
    assert report["locator_fetch_request_count"] == 0
    assert client.raw_calls == []
