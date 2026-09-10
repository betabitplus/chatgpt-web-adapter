from __future__ import annotations

import hashlib
import json

import pytest

import tools.pr13_4_longitudinal_artifact_identity_promotion_gate as gate_mod
from tools.pr13_4_longitudinal_artifact_identity_promotion_gate import (
    BASELINE_EVIDENCE_COMMIT,
    EXPECTED_ARTIFACT_SHA256,
    EXPECTED_FILENAME,
    EXPECTED_SIZE_BYTES,
    MIN_LONGITUDINAL_AGE_SECONDS,
    _normalize_sha256,
    run_promotion_gate,
)

HEAD = "head-1"
FILE_ID = "file_longitudinal_identity_1"
BASELINE_FINGERPRINT = hashlib.sha256(FILE_ID.encode("utf-8")).hexdigest()
NOW = 2_000_000_000.0
BASELINE_COMMIT_EPOCH = int(NOW) - MIN_LONGITUDINAL_AGE_SECONDS - 600


class _Client:
    pass


def _patch_git(
    monkeypatch,
    *,
    head: str = HEAD,
    clean: bool = True,
    baseline_epoch: int = BASELINE_COMMIT_EPOCH,
    merge_base: str = BASELINE_EVIDENCE_COMMIT,
) -> None:
    def fake_git_output(*args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return head
        if args == ("status", "--porcelain", "--untracked-files=no"):
            return "" if clean else " M tracked.py"
        if args == ("merge-base", BASELINE_EVIDENCE_COMMIT, head):
            return merge_base
        if args == ("show", "-s", "--format=%ct", BASELINE_EVIDENCE_COMMIT):
            return str(baseline_epoch)
        raise AssertionError(args)

    monkeypatch.setattr(gate_mod, "_git_output", fake_git_output)


def _discovery(*, identity: str = FILE_ID, identity_key: str = "file_id") -> dict:
    return {
        "characterization": "EXPLICIT_PRODUCT_IDENTITY_CANDIDATES_OBSERVED",
        "records": [
            {
                "filename": EXPECTED_FILENAME,
                "explicit_identity": identity,
                "explicit_identity_key": identity_key,
            }
        ],
    }


def _patch_positive_network(monkeypatch, *, body_sha256: str = EXPECTED_ARTIFACT_SHA256):
    calls: list[str] = []

    def fake_discovery(client, conversation_id):
        calls.append("discover")
        return _discovery()

    def fake_resolver(client, *, conversation_id, file_id):
        calls.append("resolve")
        assert file_id == FILE_ID
        return 200, {"download_url": "https://chatgpt.com/backend-api/estuary/content?sig=secret"}

    def fake_locator(client, *, conversation_id, locator):
        calls.append("bytes")
        assert "sig=secret" in locator
        return {
            "request_count": 1,
            "status_code": 200,
            "byte_count": EXPECTED_SIZE_BYTES,
            "sha256": body_sha256,
            "bytes_observed": True,
            "locator_origin_class": "CHATGPT_SAME_ORIGIN",
        }

    monkeypatch.setattr(gate_mod, "probe_conversation_files", fake_discovery)
    monkeypatch.setattr(gate_mod, "_resolver_payload", fake_resolver)
    monkeypatch.setattr(gate_mod, "probe_locator_bytes", fake_locator)
    return calls


def test_normalize_sha256_accepts_only_exact_digest() -> None:
    assert _normalize_sha256("A" * 64) == "a" * 64
    for value in ("", "a" * 63, "a" * 65, "z" * 64):
        with pytest.raises(ValueError, match="BASELINE_IDENTITY_FINGERPRINT_REQUIRED"):
            _normalize_sha256(value)


def test_positive_gate_promotes_bounded_stable_identity(monkeypatch) -> None:
    _patch_git(monkeypatch)
    calls = _patch_positive_network(monkeypatch)

    report = run_promotion_gate(
        conversation_id="conversation-1",
        baseline_identity_fingerprint=BASELINE_FINGERPRINT,
        expected_head=HEAD,
        timeout=20.0,
        client_factory=lambda timeout: _Client(),
        now_epoch=NOW,
    )

    assert calls == ["discover", "resolve", "bytes"]
    assert report["characterization"] == "LONGITUDINAL_ARTIFACT_IDENTITY_PROMOTION_PROVEN"
    assert report["baseline_age_requirement_met"] is True
    assert report["baseline_age_seconds"] >= MIN_LONGITUDINAL_AGE_SECONDS
    assert report["identity_fingerprint_matches"] is True
    assert report["longitudinal_identity_stability_proven"] is True
    assert report["stable_product_identity_proven"] is True
    assert report["indefinite_identity_stability_proven"] is False
    assert report["artifact_bytes_proven"] is True
    assert report["artifact_integrity_proven"] is True
    assert report["identity_bound_byte_retrieval_proven"] is True
    assert report["request_count"] == 3
    assert report["fresh_client_count"] == 1
    assert report["download_authority_granted"] is False
    assert report["production_handoff_promoted"] is False
    assert report["artifact_disk_write_attempted"] is False
    assert report["materialization_attempted"] is False
    assert report["write_attempted"] is False

    rendered = json.dumps(report, sort_keys=True)
    assert BASELINE_FINGERPRINT not in rendered
    assert FILE_ID not in rendered
    assert "sig=secret" not in rendered


def test_too_young_baseline_performs_no_network(monkeypatch) -> None:
    _patch_git(
        monkeypatch,
        baseline_epoch=int(NOW) - MIN_LONGITUDINAL_AGE_SECONDS + 1,
    )
    created = []

    report = run_promotion_gate(
        conversation_id="conversation-1",
        baseline_identity_fingerprint=BASELINE_FINGERPRINT,
        expected_head=HEAD,
        timeout=20.0,
        client_factory=lambda timeout: created.append(timeout) or _Client(),
        now_epoch=NOW,
    )

    assert report["characterization"] == "LONGITUDINAL_INTERVAL_NOT_YET_PROVEN"
    assert report["baseline_age_requirement_met"] is False
    assert report["request_count"] == 0
    assert created == []


def test_exact_head_or_clean_failure_performs_no_network(monkeypatch) -> None:
    _patch_git(monkeypatch, clean=False)
    created = []

    report = run_promotion_gate(
        conversation_id="conversation-1",
        baseline_identity_fingerprint=BASELINE_FINGERPRINT,
        expected_head=HEAD,
        timeout=20.0,
        client_factory=lambda timeout: created.append(timeout) or _Client(),
        now_epoch=NOW,
    )

    assert report["characterization"] == "EXACT_HEAD_OR_TRACKED_CLEAN_GATE_FAILED"
    assert report["request_count"] == 0
    assert created == []


def test_baseline_commit_must_be_ancestor(monkeypatch) -> None:
    _patch_git(monkeypatch, merge_base="different-commit")

    report = run_promotion_gate(
        conversation_id="conversation-1",
        baseline_identity_fingerprint=BASELINE_FINGERPRINT,
        expected_head=HEAD,
        timeout=20.0,
        client_factory=lambda timeout: _Client(),
        now_epoch=NOW,
    )

    assert report["characterization"] == "BASELINE_EVIDENCE_TIME_GATE_FAILED"
    assert report["error_type"] == "RuntimeError"
    assert report["request_count"] == 0


def test_fingerprint_mismatch_stops_before_resolution(monkeypatch) -> None:
    _patch_git(monkeypatch)
    calls: list[str] = []

    def fake_discovery(client, conversation_id):
        calls.append("discover")
        return _discovery(identity="file_changed_identity")

    monkeypatch.setattr(gate_mod, "probe_conversation_files", fake_discovery)
    monkeypatch.setattr(
        gate_mod,
        "_resolver_payload",
        lambda *args, **kwargs: pytest.fail("resolver must not run after identity mismatch"),
    )

    report = run_promotion_gate(
        conversation_id="conversation-1",
        baseline_identity_fingerprint=BASELINE_FINGERPRINT,
        expected_head=HEAD,
        timeout=20.0,
        client_factory=lambda timeout: _Client(),
        now_epoch=NOW,
    )

    assert calls == ["discover"]
    assert report["characterization"] == "LONGITUDINAL_IDENTITY_FINGERPRINT_MISMATCH"
    assert report["identity_fingerprint_matches"] is False
    assert report["stable_product_identity_proven"] is False
    assert report["request_count"] == 1


def test_identity_key_change_fails_closed(monkeypatch) -> None:
    _patch_git(monkeypatch)
    monkeypatch.setattr(
        gate_mod,
        "probe_conversation_files",
        lambda client, conversation_id: _discovery(identity_key="artifact_id"),
    )

    report = run_promotion_gate(
        conversation_id="conversation-1",
        baseline_identity_fingerprint=BASELINE_FINGERPRINT,
        expected_head=HEAD,
        timeout=20.0,
        client_factory=lambda timeout: _Client(),
        now_epoch=NOW,
    )

    assert report["characterization"] == "PRODUCT_IDENTITY_KEY_CHANGED"
    assert report["identity_key"] == "artifact_id"
    assert report["stable_product_identity_proven"] is False
    assert report["request_count"] == 1


def test_integrity_mismatch_blocks_stability_promotion(monkeypatch) -> None:
    _patch_git(monkeypatch)
    calls = _patch_positive_network(monkeypatch, body_sha256="0" * 64)

    report = run_promotion_gate(
        conversation_id="conversation-1",
        baseline_identity_fingerprint=BASELINE_FINGERPRINT,
        expected_head=HEAD,
        timeout=20.0,
        client_factory=lambda timeout: _Client(),
        now_epoch=NOW,
    )

    assert calls == ["discover", "resolve", "bytes"]
    assert report["characterization"] == "LONGITUDINAL_ARTIFACT_INTEGRITY_NOT_PROVEN"
    assert report["identity_fingerprint_matches"] is True
    assert report["artifact_integrity_proven"] is False
    assert report["stable_product_identity_proven"] is False
    assert report["request_count"] == 3
