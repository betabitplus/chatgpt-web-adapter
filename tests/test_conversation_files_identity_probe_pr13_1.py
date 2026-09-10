from __future__ import annotations

from typing import Any

from tools.pr13_1_conversation_files_identity_probe import (
    probe_conversation_files,
    summarize_files_payload,
)


class _Client:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self.payload = payload
        self.calls: list[tuple[str, str, Any, dict[str, str]]] = []

    def _build_headers(self, additions: dict[str, str]) -> dict[str, str]:
        return dict(additions)

    def _json_request(self, method, url, payload, headers):
        self.calls.append((method, url, payload, headers))
        return self.status, self.payload


def test_probe_performs_exactly_one_get_and_exports_no_locator_values() -> None:
    client = _Client(
        200,
        {
            "files": [
                {
                    "file_id": "file_abc123",
                    "filename": "result.txt",
                    "mime_type": "text/plain",
                    "size_bytes": 17,
                    "download_url": "https://signed.example/secret?token=very-secret",
                    "conversation_id": "conversation-1",
                    "message_id": "message-1",
                }
            ]
        },
    )

    report = probe_conversation_files(client, "conversation-1")

    assert len(client.calls) == 1
    method, url, payload, headers = client.calls[0]
    assert method == "GET"
    assert url == ("https://chatgpt.com/backend-api/conversations/conversation-1/files")
    assert payload is None
    assert headers["accept"] == "application/json"
    assert report["request_count"] == 1
    assert report["write_attempted"] is False
    assert report["download_attempted"] is False
    assert report["locator_values_exported"] is False
    assert report["sensitive_locator_fields_present"] is True
    assert report["characterization"] == (
        "EXPLICIT_PRODUCT_IDENTITY_CANDIDATES_OBSERVED"
    )
    assert report["stable_product_identity_proven"] is False
    assert report["download_authority_granted"] is False
    assert report["records"] == [
        {
            "explicit_identity_key": "file_id",
            "explicit_identity": "file_abc123",
            "filename_key": "filename",
            "filename": "result.txt",
            "media_type_key": "mime_type",
            "media_type": "text/plain",
            "size_key": "size_bytes",
            "size_bytes": 17,
            "conversation_id_field_present": True,
            "message_id_field_present": True,
            "locator_field_present": True,
        }
    ]
    assert "signed.example" not in str(report)
    assert "very-secret" not in str(report)


def test_duplicate_ids_are_not_promoted_to_identity_evidence() -> None:
    report = summarize_files_payload(
        [
            {"id": "same-id", "name": "one.txt"},
            {"id": "same-id", "name": "two.txt"},
        ]
    )

    assert report["explicit_identity_candidate_count"] == 2
    assert report["unique_identity_candidate_count"] == 1
    assert report["identity_candidates_unique"] is False
    assert report["characterization"] == (
        "DUPLICATE_EXPLICIT_IDENTITY_CANDIDATE_OBSERVED"
    )
    assert report["stable_product_identity_proven"] is False


def test_missing_identity_fails_closed_without_guessing_from_filename() -> None:
    report = summarize_files_payload(
        {"items": [{"filename": "looks-unique.txt", "size": 12}]}
    )

    assert report["explicit_identity_candidate_count"] == 0
    assert report["all_records_have_explicit_identity_candidate"] is False
    assert report["characterization"] == "FILE_RECORDS_WITHOUT_EXPLICIT_IDENTITY"


def test_status_classification_never_retries_or_falls_back() -> None:
    expected = {
        401: "AUTHENTICATION_REQUIRED",
        403: "ACCESS_CHALLENGED",
        404: "ENDPOINT_ABSENT_OR_NOT_VISIBLE",
        500: "FILES_ENDPOINT_HTTP_ERROR",
    }

    for status, characterization in expected.items():
        client = _Client(status, {"detail": "not exported"})
        report = probe_conversation_files(client, "conversation-1")

        assert len(client.calls) == 1
        assert report["characterization"] == characterization
        assert report["response_body_exported"] is False
        assert report["download_attempted"] is False
        assert report["write_attempted"] is False


def test_invalid_conversation_identity_is_rejected_before_any_request() -> None:
    client = _Client(200, [])

    for invalid in ("", "a/b", "a?b", "a#b"):
        try:
            probe_conversation_files(client, invalid)
        except ValueError as exc:
            assert str(exc) == "CONVERSATION_ID_REQUIRED"
        else:
            raise AssertionError("invalid conversation id was accepted")

    assert client.calls == []
