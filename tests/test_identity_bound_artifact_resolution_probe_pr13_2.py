from __future__ import annotations

from typing import Any

import tools.pr13_2_identity_bound_artifact_resolution_probe as probe_mod
from tools.pr13_2_identity_bound_artifact_resolution_probe import (
    characterize_resolution_pair,
    probe_resolution_request,
    run_resolution_gate,
    summarize_resolution_payload,
)


class _Client:
    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Any, dict[str, str]]] = []

    def _build_headers(self, additions: dict[str, str]) -> dict[str, str]:
        return dict(additions)

    def _json_request(self, method, url, payload, headers):
        self.calls.append((method, url, payload, headers))
        return self.responses.pop(0)


def test_resolution_payload_exports_presence_not_locator_value() -> None:
    report = summarize_resolution_payload(
        {
            "download_url": "https://example.invalid/signed?token=secret",
            "file_name": "result.txt",
            "file_size_bytes": 42,
        }
    )

    assert report == {
        "response_shape": "dict",
        "locator_field_present": True,
        "locator_value_exported": False,
        "filename_key": "file_name",
        "filename": "result.txt",
        "size_key": "file_size_bytes",
        "size_bytes": 42,
        "resolution_payload_recognized": True,
    }
    assert "example.invalid" not in str(report)
    assert "secret" not in str(report)


def test_resolution_request_uses_exact_generated_file_identity_surface() -> None:
    client = _Client(
        [(200, {"download_url": "https://example.invalid/signed", "file_name": "x.txt"})]
    )

    report = probe_resolution_request(
        client,
        conversation_id="conversation-1",
        file_id="file_abc123",
    )

    assert len(client.calls) == 1
    method, url, payload, headers = client.calls[0]
    assert method == "GET"
    assert url == (
        "https://chatgpt.com/backend-api/files/download/file_abc123"
        "?conversation_id=conversation-1&inline=false"
    )
    assert payload is None
    assert headers["accept"] == "application/json"
    assert report["status_code"] == 200
    assert report["locator_field_present"] is True
    assert report["locator_value_exported"] is False


def test_pair_requires_real_resolution_and_rejected_negative_control() -> None:
    real = {
        "status_code": 200,
        "resolution_payload_recognized": True,
        "locator_field_present": True,
    }
    control = {
        "status_code": 404,
        "resolution_payload_recognized": False,
        "locator_field_present": False,
    }

    report = characterize_resolution_pair(real, control)

    assert report["characterization"] == "IDENTITY_BOUND_RESOLUTION_SURFACE_OBSERVED"
    assert report["identity_bound_resolution_surface_proven"] is True
    assert report["stable_product_identity_proven"] is False
    assert report["artifact_bytes_proven"] is False
    assert report["download_authority_granted"] is False


def test_pair_fails_closed_when_control_also_resolves() -> None:
    real = {
        "status_code": 200,
        "resolution_payload_recognized": True,
        "locator_field_present": True,
    }
    control = {
        "status_code": 200,
        "resolution_payload_recognized": True,
        "locator_field_present": True,
    }

    report = characterize_resolution_pair(real, control)

    assert report["characterization"] == "NEGATIVE_CONTROL_UNEXPECTEDLY_RESOLVED"
    assert report["identity_bound_resolution_surface_proven"] is False


def test_pair_treats_server_error_control_as_inconclusive() -> None:
    real = {
        "status_code": 200,
        "resolution_payload_recognized": True,
        "locator_field_present": True,
    }
    control = {
        "status_code": 500,
        "resolution_payload_recognized": False,
        "locator_field_present": False,
    }

    report = characterize_resolution_pair(real, control)

    assert report["characterization"] == "NEGATIVE_CONTROL_INCONCLUSIVE"
    assert report["identity_bound_resolution_surface_proven"] is False


def test_gate_derives_file_id_then_resolves_real_and_negative_control(monkeypatch) -> None:
    monkeypatch.setattr(
        probe_mod,
        "_git_output",
        lambda *args: "head-1" if args == ("rev-parse", "HEAD") else "",
    )
    client = _Client(
        [
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
                    "download_url": "https://example.invalid/signed?token=secret",
                    "file_name": "result.txt",
                    "file_size_bytes": 42,
                },
            ),
            (404, {"detail": "not found"}),
        ]
    )

    report = run_resolution_gate(
        conversation_id="conversation-1",
        expected_filename="result.txt",
        expected_head="head-1",
        timeout=1.0,
        client_factory=lambda _timeout: client,
    )

    assert report["characterization"] == "IDENTITY_BOUND_RESOLUTION_SURFACE_OBSERVED"
    assert report["request_count"] == 3
    assert report["identity_discovery_request_count"] == 1
    assert report["resolution_request_count"] == 1
    assert report["negative_control_request_count"] == 1
    assert report["identity_key"] == "file_id"
    assert report["identity_values_exported"] is False
    assert report["locator_values_exported"] is False
    assert report["response_body_exported"] is False
    assert report["identity_bound_resolution_surface_proven"] is True
    assert report["artifact_bytes_proven"] is False
    assert report["download_attempted"] is False
    assert report["write_attempted"] is False
    assert "file_abc123" not in str(report)
    assert "example.invalid" not in str(report)
    assert "secret" not in str(report)

    assert len(client.calls) == 3
    real_url = client.calls[1][1]
    control_url = client.calls[2][1]
    assert "file_abc123" in real_url
    assert "file_abc120" in control_url
    assert real_url != control_url


def test_gate_does_not_run_control_when_real_id_does_not_resolve(monkeypatch) -> None:
    monkeypatch.setattr(
        probe_mod,
        "_git_output",
        lambda *args: "head-1" if args == ("rev-parse", "HEAD") else "",
    )
    client = _Client(
        [
            (
                200,
                {"items": [{"file_id": "file_abc123", "file_name": "result.txt"}]},
            ),
            (404, {"detail": "not found"}),
        ]
    )

    report = run_resolution_gate(
        conversation_id="conversation-1",
        expected_filename="result.txt",
        expected_head="head-1",
        timeout=1.0,
        client_factory=lambda _timeout: client,
    )

    assert report["characterization"] == "REAL_ID_RESOLUTION_NOT_PROVEN"
    assert report["request_count"] == 2
    assert report["negative_control_request_count"] == 0
    assert report["identity_bound_resolution_surface_proven"] is False
    assert len(client.calls) == 2
