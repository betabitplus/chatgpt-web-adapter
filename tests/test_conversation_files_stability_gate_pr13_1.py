from __future__ import annotations

from tools.pr13_1_conversation_files_stability_gate import (
    characterize_independent_reads,
)


def _report(identity: str, *, filename: str = "probe.txt", key: str = "file_id"):
    return {
        "characterization": "EXPLICIT_PRODUCT_IDENTITY_CANDIDATES_OBSERVED",
        "records": [
            {
                "explicit_identity_key": key,
                "explicit_identity": identity,
                "filename": filename,
            }
        ],
    }


def test_same_identity_across_independent_reads_is_proven_short_term_only() -> None:
    report = characterize_independent_reads(
        _report("file_same"),
        _report("file_same"),
        expected_filename="probe.txt",
    )

    assert report["characterization"] == (
        "SAME_EXPLICIT_IDENTITY_ACROSS_INDEPENDENT_READS"
    )
    assert report["request_count"] == 2
    assert report["fresh_client_count"] == 2
    assert report["same_explicit_identity"] is True
    assert report["same_identity_key"] is True
    assert report["short_term_identity_stability_proven"] is True
    assert report["stable_product_identity_proven"] is False
    assert report["resolution_surface_proven"] is False
    assert report["download_authority_granted"] is False
    assert report["identity_values_exported"] is False
    assert report["first_identity_fingerprint"] == report["second_identity_fingerprint"]
    assert "file_same" not in str(report)


def test_changed_identity_fails_stability_gate() -> None:
    report = characterize_independent_reads(
        _report("file_first"),
        _report("file_second"),
        expected_filename="probe.txt",
    )

    assert report["characterization"] == (
        "EXPLICIT_IDENTITY_CHANGED_ACROSS_INDEPENDENT_READS"
    )
    assert report["same_explicit_identity"] is False
    assert report["short_term_identity_stability_proven"] is False
    assert report["first_identity_fingerprint"] != report["second_identity_fingerprint"]
    assert "file_first" not in str(report)
    assert "file_second" not in str(report)


def test_identity_key_change_is_not_treated_as_stable() -> None:
    report = characterize_independent_reads(
        _report("same", key="file_id"),
        _report("same", key="artifact_id"),
        expected_filename="probe.txt",
    )

    assert report["same_explicit_identity"] is True
    assert report["same_identity_key"] is False
    assert report["short_term_identity_stability_proven"] is False
    assert report["characterization"] == (
        "EXPLICIT_IDENTITY_CHANGED_ACROSS_INDEPENDENT_READS"
    )


def test_filename_is_anchor_only_and_missing_target_fails_closed() -> None:
    report = characterize_independent_reads(
        _report("file_one", filename="other.txt"),
        _report("file_one", filename="other.txt"),
        expected_filename="probe.txt",
    )

    assert report["first_target_record_present"] is False
    assert report["characterization"] == "FIRST_READ_TARGET_IDENTITY_NOT_PROVEN"
    assert report["short_term_identity_stability_proven"] is False


def test_ambiguous_duplicate_filename_target_fails_closed() -> None:
    first = {
        "characterization": "EXPLICIT_PRODUCT_IDENTITY_CANDIDATES_OBSERVED",
        "records": [
            {
                "explicit_identity_key": "file_id",
                "explicit_identity": "one",
                "filename": "probe.txt",
            },
            {
                "explicit_identity_key": "file_id",
                "explicit_identity": "two",
                "filename": "probe.txt",
            },
        ],
    }

    report = characterize_independent_reads(
        first,
        _report("one"),
        expected_filename="probe.txt",
    )

    assert report["characterization"] == "FIRST_READ_TARGET_IDENTITY_NOT_PROVEN"
    assert report["short_term_identity_stability_proven"] is False
