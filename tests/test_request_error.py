from __future__ import annotations

from chatgpt_web_adapter import RequestError


def test_request_error_preserves_exact_write_dispatch_evidence() -> None:
    error = RequestError(
        "WKWEBVIEW_MINIMAL_SECURITY_WRITE_FAILED",
        request_stage="wkwebview_authority_turn",
        write_dispatched=False,
        submit_request_observed=False,
        submit_response_observed=False,
        submit_response_status=0,
    )

    assert error.write_dispatched is False
    assert error.submit_request_observed is False
    assert error.submit_response_observed is False
    assert error.submit_response_status is None
    assert error.to_dict()["write_dispatched"] is False
