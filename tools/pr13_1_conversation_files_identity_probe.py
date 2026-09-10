from __future__ import annotations

import argparse
import json
import re
import subprocess
from typing import Any
from urllib.parse import quote

from chatgpt_web_adapter import ChatGPTWebClient

CHATGPT_ORIGIN = "https://chatgpt.com"
PROBE_SCHEMA = "CWA_PR13_1_CONVERSATION_FILES_IDENTITY_PROBE_V1"
_IDENTITY_KEYS = ("file_id", "artifact_id", "asset_id", "id")
_FILENAME_KEYS = ("filename", "file_name", "name")
_MEDIA_TYPE_KEYS = ("media_type", "mime_type", "content_type")
_SIZE_KEYS = ("size_bytes", "size")
_COLLECTION_KEYS = ("files", "items", "data")
_SENSITIVE_LOCATOR_KEYS = frozenset(
    {
        "url",
        "href",
        "download_url",
        "signed_url",
        "download_uri",
        "upload_url",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "secret",
        "token",
    }
)
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,192}$")
_MEDIA_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _conversation_id(value: Any) -> str:
    conversation_id = _optional_text(value)
    if (
        conversation_id is None
        or len(conversation_id) > 256
        or any(marker in conversation_id for marker in ("/", "?", "#"))
    ):
        raise ValueError("CONVERSATION_ID_REQUIRED")
    return conversation_id


def _safe_identity(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None or not _OPAQUE_ID_RE.fullmatch(text):
        return None
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in ("token", "secret", "credential", "authorization", "cookie")
    ):
        return None
    return text


def _safe_filename(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None or len(text) > 255:
        return None
    if text in {".", ".."} or "/" in text or "\\" in text:
        return None
    if any(ord(char) < 32 for char in text):
        return None
    return text


def _safe_media_type(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    normalized = text.lower()
    if len(normalized) > 128 or not _MEDIA_TYPE_RE.fullmatch(normalized):
        return None
    return normalized


def _safe_size(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _first_safe(record: dict[str, Any], keys: tuple[str, ...], sanitizer):
    for key in keys:
        if key not in record:
            continue
        value = sanitizer(record.get(key))
        if value is not None:
            return key, value
    return None, None


def _collection(payload: Any) -> tuple[str, list[Any]] | None:
    if isinstance(payload, list):
        return "top_level_list", payload
    if not isinstance(payload, dict):
        return None
    for key in _COLLECTION_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return key, value
    return None


def summarize_files_payload(payload: Any) -> dict[str, Any]:
    """Return locator-free identity evidence from an unknown files payload shape."""

    collection = _collection(payload)
    if collection is None:
        return {
            "response_shape": type(payload).__name__,
            "collection_key": None,
            "record_count": 0,
            "records": [],
            "sensitive_locator_fields_present": False,
            "explicit_identity_candidate_count": 0,
            "unique_identity_candidate_count": 0,
            "all_records_have_explicit_identity_candidate": False,
            "identity_candidates_unique": False,
            "characterization": "UNRECOGNIZED_FILES_RESPONSE_SHAPE",
            "stable_product_identity_proven": False,
            "download_authority_granted": False,
        }

    collection_key, raw_records = collection
    records: list[dict[str, Any]] = []
    sensitive_locator_fields_present = False
    identities: list[str] = []
    object_record_count = 0

    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            records.append({"record_shape": type(raw_record).__name__})
            continue
        object_record_count += 1
        sensitive_locator_fields_present = sensitive_locator_fields_present or any(
            key in raw_record and raw_record.get(key) is not None
            for key in _SENSITIVE_LOCATOR_KEYS
        )
        identity_key, identity = _first_safe(
            raw_record,
            _IDENTITY_KEYS,
            _safe_identity,
        )
        filename_key, filename = _first_safe(
            raw_record,
            _FILENAME_KEYS,
            _safe_filename,
        )
        media_type_key, media_type = _first_safe(
            raw_record,
            _MEDIA_TYPE_KEYS,
            _safe_media_type,
        )
        size_key, size_bytes = _first_safe(raw_record, _SIZE_KEYS, _safe_size)

        safe_record: dict[str, Any] = {
            "explicit_identity_key": identity_key,
            "explicit_identity": identity,
            "filename_key": filename_key,
            "filename": filename,
            "media_type_key": media_type_key,
            "media_type": media_type,
            "size_key": size_key,
            "size_bytes": size_bytes,
            "conversation_id_field_present": isinstance(
                raw_record.get("conversation_id"), str
            ),
            "message_id_field_present": isinstance(raw_record.get("message_id"), str),
            "locator_field_present": any(
                key in raw_record and raw_record.get(key) is not None
                for key in _SENSITIVE_LOCATOR_KEYS
            ),
        }
        records.append(safe_record)
        if identity is not None:
            identities.append(identity)

    unique_identity_count = len(set(identities))
    all_records_have_identity = bool(raw_records) and len(identities) == len(raw_records)
    identities_unique = bool(identities) and unique_identity_count == len(identities)

    if not raw_records:
        characterization = "EMPTY_FILE_COLLECTION_OBSERVED"
    elif object_record_count != len(raw_records):
        characterization = "NON_OBJECT_FILE_RECORD_OBSERVED"
    elif not all_records_have_identity:
        characterization = "FILE_RECORDS_WITHOUT_EXPLICIT_IDENTITY"
    elif not identities_unique:
        characterization = "DUPLICATE_EXPLICIT_IDENTITY_CANDIDATE_OBSERVED"
    else:
        characterization = "EXPLICIT_PRODUCT_IDENTITY_CANDIDATES_OBSERVED"

    return {
        "response_shape": type(payload).__name__,
        "collection_key": collection_key,
        "record_count": len(raw_records),
        "records": records,
        "sensitive_locator_fields_present": sensitive_locator_fields_present,
        "explicit_identity_candidate_count": len(identities),
        "unique_identity_candidate_count": unique_identity_count,
        "all_records_have_explicit_identity_candidate": all_records_have_identity,
        "identity_candidates_unique": identities_unique,
        "characterization": characterization,
        # A single endpoint observation can prove explicit product fields exist,
        # but cannot prove longitudinal stability across independent observations.
        "stable_product_identity_proven": False,
        # Observation is never download/filesystem authority.
        "download_authority_granted": False,
    }


def probe_conversation_files(
    client: Any,
    conversation_id: str,
) -> dict[str, Any]:
    """Perform exactly one authenticated, read-only conversation-files request."""

    conversation_id = _conversation_id(conversation_id)
    endpoint = (
        f"{CHATGPT_ORIGIN}/backend-api/conversations/"
        f"{quote(conversation_id, safe='')}/files"
    )
    headers = client._build_headers(
        {
            "accept": "application/json",
            "referer": f"{CHATGPT_ORIGIN}/c/{conversation_id}",
        }
    )

    status, payload = client._json_request("GET", endpoint, None, headers)
    report: dict[str, Any] = {
        "schema": PROBE_SCHEMA,
        "request_count": 1,
        "method": "GET",
        "conversation_id_present": True,
        "status_code": status,
        "response_body_exported": False,
        "locator_values_exported": False,
        "download_attempted": False,
        "write_attempted": False,
        "stable_product_identity_proven": False,
        "download_authority_granted": False,
    }

    if status == 401:
        report["characterization"] = "AUTHENTICATION_REQUIRED"
        return report
    if status == 403:
        report["characterization"] = "ACCESS_CHALLENGED"
        return report
    if status == 404:
        report["characterization"] = "ENDPOINT_ABSENT_OR_NOT_VISIBLE"
        return report
    if status >= 400:
        report["characterization"] = "FILES_ENDPOINT_HTTP_ERROR"
        return report

    report.update(summarize_files_payload(payload))
    return report


def _git_output(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def run_probe(
    *,
    conversation_id: str,
    expected_head: str,
    timeout: float,
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    head = _git_output("rev-parse", "HEAD")
    tracked_clean = _git_output("status", "--porcelain", "--untracked-files=no") == ""
    report: dict[str, Any] = {
        "schema": PROBE_SCHEMA,
        "head": head,
        "expected_head": expected_head,
        "head_matches": head == expected_head,
        "tracked_clean": tracked_clean,
        "request_count": 0,
        "download_attempted": False,
        "write_attempted": False,
        "stable_product_identity_proven": False,
        "download_authority_granted": False,
    }
    if head != expected_head or not tracked_clean:
        report["characterization"] = "EXACT_HEAD_OR_TRACKED_CLEAN_GATE_FAILED"
        return report

    client = ChatGPTWebClient(
        auto_login=False,
        auto_sentinel=False,
        timeout=timeout,
    )
    try:
        probe = probe_conversation_files(client, conversation_id)
    except Exception as exc:
        report.update(
            {
                "request_count": 1,
                "characterization": "PROBE_REQUEST_FAILED",
                "error_type": type(exc).__name__,
            }
        )
        return report

    report.update(probe)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one read-only authenticated GET against the ChatGPT "
            "conversation-files surface and emit locator-free identity evidence."
        )
    )
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    report = run_probe(
        conversation_id=args.conversation_id,
        expected_head=args.expected_head,
        timeout=args.timeout,
    )
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))

    completed_characterizations = {
        "AUTHENTICATION_REQUIRED",
        "ACCESS_CHALLENGED",
        "ENDPOINT_ABSENT_OR_NOT_VISIBLE",
        "FILES_ENDPOINT_HTTP_ERROR",
        "UNRECOGNIZED_FILES_RESPONSE_SHAPE",
        "EMPTY_FILE_COLLECTION_OBSERVED",
        "NON_OBJECT_FILE_RECORD_OBSERVED",
        "FILE_RECORDS_WITHOUT_EXPLICIT_IDENTITY",
        "DUPLICATE_EXPLICIT_IDENTITY_CANDIDATE_OBSERVED",
        "EXPLICIT_PRODUCT_IDENTITY_CANDIDATES_OBSERVED",
    }
    return 0 if report.get("characterization") in completed_characterizations else 1


if __name__ == "__main__":
    raise SystemExit(main())
