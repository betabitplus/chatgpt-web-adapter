from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chatgpt_web_adapter.exceptions import RequestError
from chatgpt_web_adapter.wkwebview_canonical import (
    WKCanonicalState,
)
from chatgpt_web_adapter.wkwebview_helper_runtime import (
    WKWebViewHelperRuntime,
)
from chatgpt_web_adapter.wkwebview_provider import WKWebViewTurnProvider


def _invocation_argv(invocation) -> list[str]:
    command = getattr(invocation, "command", invocation)
    return list(command)


def _invocation_request(invocation) -> dict:
    request = getattr(invocation, "request", {})
    return dict(request) if isinstance(request, dict) else {}


def _final_canonical_for_prompt(prompt: str, *, assistant_text: str = "done") -> dict:
    return {
        "current_node": "node-final",
        "mapping": {
            "user-final": {
                "parent": None,
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": [prompt]},
                },
            },
            "node-final": {
                "parent": "user-final",
                "message": {
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "status": "finished_successfully",
                    "end_turn": True,
                    "content": {"parts": [assistant_text]},
                },
            },
        },
    }


def test_wkwebview_continuation_command_fences_canonical_parent(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))

    command = provider._helper_command(
        conversation_id="conversation-1",
        text="hello",
        timeout=12,
        expected_current_node="node-7",
    )

    assert _invocation_request(command)["expected_current_node"] == "node-7"
    assert "hello" not in _invocation_argv(command)
    assert "conversation-1" not in _invocation_argv(command)


@pytest.mark.skipif(
    os.name != "posix", reason="anonymous helper FD handoff is POSIX-only"
)
def test_wkwebview_helper_runner_uses_stdin_and_anonymous_resume_fd(
    monkeypatch, tmp_path
) -> None:
    helper = tmp_path / "fake-wk-helper"
    helper.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

request = json.load(sys.stdin)
fd_index = sys.argv.index("--resume-handoff-fd") + 1
fd = int(sys.argv[fd_index])
os.write(
    fd,
    json.dumps({
        "v": 2,
        "r": "resume-test",
        "p": "conversation-turn-direct",
        "x": "turn-direct",
        "i": "conversation-private",
        "c": "conduit-test",
        "t": "trace-test",
    }).encode(),
)
os.close(fd)
print("WK_RESULT " + json.dumps({
    "ok": True,
    "request_prompt": request.get("prompt"),
    "request_url": request.get("url"),
    "argv_contains_prompt": request.get("prompt") in sys.argv,
    "argv_contains_url": request.get("url") in sys.argv,
}))
""",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: helper)
    invocation = provider._helper_command(
        conversation_id="conversation-private",
        text="prompt-private",
        timeout=5,
    )
    invocation.capture_resume = True

    payload = provider._run_helper_streaming(
        invocation,
        timeout=5,
        on_text_event=lambda event: None,
    )

    assert payload["request_prompt"] == "prompt-private"
    assert payload["request_url"].endswith("/c/conversation-private")
    assert payload["argv_contains_prompt"] is False
    assert payload["argv_contains_url"] is False
    assert payload["stream_resume_value"] == "resume-test"
    assert payload["stream_topic_id"] == "conversation-turn-direct"
    assert payload["turn_exchange_id"] == "turn-direct"
    assert payload["stream_conversation_id"] == "conversation-private"
    assert payload["_cwa_stop_conduit_token"] == "conduit-test"
    assert payload["_cwa_stop_turn_trace_id"] == "trace-test"


@pytest.mark.skipif(os.name != "posix", reason="helper process control is POSIX-only")
def test_wkwebview_helper_can_finish_from_browser_stream_completion(
    monkeypatch, tmp_path
) -> None:
    helper = tmp_path / "fake-wk-helper"
    helper.write_text(
        """#!/usr/bin/env python3
import json
import sys
import time

json.load(sys.stdin)
print("WK_EVENT " + json.dumps({
    "type": "submit_request_observed",
    "temporary_mode": True,
}), flush=True)
print("WK_EVENT " + json.dumps({
    "type": "assistant_text_delta",
    "message_id": "assistant-stream-complete",
    "sequence": 1,
    "delta": "done",
}), flush=True)
print("WK_EVENT " + json.dumps({
    "type": "raw_ws_event",
    "parsed": {
        "type": "message_stream_complete",
        "conversation_id": "conversation-stream-complete",
    },
}), flush=True)
time.sleep(10)
print("WK_RESULT " + json.dumps({
    "ok": False,
    "error": "SHOULD_NOT_REACH_RESULT",
}), flush=True)
""",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: helper)
    invocation = provider._helper_command(
        conversation_id=None,
        text="prompt-private",
        timeout=10,
    )

    state: dict[str, Any] = {
        "temporary_mode": False,
        "assistant_message_id": None,
        "conversation_id": None,
    }

    def on_text(event: dict[str, Any]) -> None:
        message_id = event.get("message_id")
        if isinstance(message_id, str) and message_id:
            state["assistant_message_id"] = message_id

    def on_transport(event: dict[str, Any]) -> None:
        if (
            event.get("type") == "submit_request_observed"
            and event.get("temporary_mode") is True
        ):
            state["temporary_mode"] = True
        parsed = event.get("parsed")
        if (
            event.get("type") == "raw_ws_event"
            and isinstance(parsed, dict)
            and parsed.get("type") == "message_stream_complete"
        ):
            state["conversation_id"] = parsed.get("conversation_id")

    def completion_check() -> bool | dict[str, Any]:
        if (
            state["temporary_mode"] is not True
            or not state["assistant_message_id"]
            or not state["conversation_id"]
        ):
            return False
        return {
            "kind": "browser_stream_complete",
            "conversation_id": state["conversation_id"],
            "assistant_message_id": state["assistant_message_id"],
        }

    started = time.monotonic()
    payload = provider._run_helper_streaming(
        invocation,
        timeout=10,
        on_text_event=on_text,
        on_transport_event=on_transport,
        external_completion_check=completion_check,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 3
    assert payload["conversation_id"] == "conversation-stream-complete"
    assert payload["assistant_message_id"] == "assistant-stream-complete"
    assert payload["stream_terminal_observed"] is True
    assert payload["write_commit_proven"] is True
    assert payload["write_commit_proof"] == "BROWSER_STREAM_COMPLETE"
    assert payload["_cwa_browser_stream_complete_observed"] is True


def test_wkwebview_curl_ws_finality_rejects_stale_previous_turn() -> None:
    provider = WKWebViewTurnProvider()
    previous = {
        "current_node": "assistant-old",
        "mapping": {
            "user-old": {
                "parent": None,
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["same prompt"]},
                },
            },
            "assistant-old": {
                "parent": "user-old",
                "message": {
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "status": "finished_successfully",
                    "end_turn": True,
                    "content": {"parts": ["old answer"]},
                },
            },
        },
    }
    current = {
        "current_node": "assistant-new",
        "mapping": {
            **previous["mapping"],
            "user-new": {
                "parent": "assistant-old",
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["same prompt"]},
                },
            },
            "assistant-new": {
                "parent": "user-new",
                "message": {
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "status": "finished_successfully",
                    "end_turn": True,
                    "content": {"parts": ["new answer"]},
                },
            },
        },
    }

    assert WKCanonicalState.payload_is_final(previous) is True
    assert (
        provider._canonical_payload_matches_write(
            previous,
            text="same prompt",
            baseline_current_node="assistant-old",
        )
        is False
    )
    assert (
        provider._canonical_payload_matches_write(
            current,
            text="same prompt",
            baseline_current_node="assistant-old",
        )
        is True
    )


def test_wkwebview_continuation_retries_once_after_pre_submit_timeout(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    monkeypatch.setattr(provider, "_lightweight_transport", None)
    monkeypatch.setattr(
        "chatgpt_web_adapter.wkwebview_provider.time.sleep",
        lambda _seconds: None,
    )
    prewrite = {
        "current_node": "assistant-old",
        "mapping": {
            "assistant-old": {
                "message": {
                    "id": "assistant-old",
                    "author": {"role": "assistant"},
                    "metadata": {},
                }
            }
        },
    }
    monkeypatch.setattr(provider, "peek_prewrite_payload", lambda _cid: prewrite)
    guard_reads: list[str] = []

    def guard_read(
        conversation_id: str,
        *,
        timeout: float,
        coordinated: bool = True,
    ):
        guard_reads.append(conversation_id)
        assert coordinated is True
        return prewrite

    monkeypatch.setattr(
        provider,
        "_read_conversation_payload_uncached",
        guard_read,
    )
    calls = 0

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        on_transport_event=None,
        **kwargs,
    ):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RequestError(
                "WKWEBVIEW_PRE_SUBMIT_TIMEOUT",
                request_stage="wkwebview_authority_turn",
            )
        if callable(on_lifecycle_event):
            on_lifecycle_event(
                {
                    "type": "write_identity_resolved",
                    "conversation_id": "conversation-1",
                }
            )
        return {
            "ok": True,
            "conversation_id": "conversation-1",
            "response_status": 200,
            "attachment_count": 0,
            "write_commit_proven": True,
            "write_commit_proof": "STREAM_TERMINAL",
            "canonical_committed": False,
            "committed_current_node": "",
            "stream_terminal_observed": True,
            "stream_resume_present": False,
            "stream_resume_handoff_written": False,
        }

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    transport_events: list[dict[str, Any]] = []

    result = provider.send_text_streaming(
        "continue",
        conversation="conversation-1",
        model_slug="model-1",
        on_text_event=lambda _event: None,
        on_transport_event=transport_events.append,
    )

    assert calls == 2
    assert guard_reads == ["conversation-1"]
    assert result.conversation_id == "conversation-1"
    assert transport_events == [
        {
            "type": "pre_submit_retry",
            "conversation_id": "conversation-1",
        }
    ]


def test_wkwebview_continuation_does_not_retry_when_commit_is_ambiguous(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    monkeypatch.setattr(provider, "_lightweight_transport", None)
    monkeypatch.setattr(
        "chatgpt_web_adapter.wkwebview_provider.time.sleep",
        lambda _seconds: None,
    )
    prewrite = {
        "current_node": "assistant-old",
        "mapping": {
            "assistant-old": {
                "message": {
                    "id": "assistant-old",
                    "author": {"role": "assistant"},
                    "metadata": {},
                }
            }
        },
    }
    changed = {
        "current_node": "user-new",
        "mapping": {
            "user-new": {
                "message": {
                    "id": "user-new",
                    "author": {"role": "user"},
                    "content": {"parts": ["continue"]},
                }
            }
        },
    }
    monkeypatch.setattr(provider, "peek_prewrite_payload", lambda _cid: prewrite)
    monkeypatch.setattr(
        provider,
        "_read_conversation_payload_uncached",
        lambda _cid, *, timeout, coordinated=True: changed,
    )
    calls = 0

    def fake_stream(command, **kwargs):
        nonlocal calls
        calls += 1
        raise RequestError(
            "WKWEBVIEW_PRE_SUBMIT_TIMEOUT",
            request_stage="wkwebview_authority_turn",
        )

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)

    with pytest.raises(
        RequestError,
        match="WKWEBVIEW_PRE_SUBMIT_COMMIT_AMBIGUOUS",
    ):
        provider.send_text_streaming(
            "continue",
            conversation="conversation-1",
            model_slug="model-1",
            on_text_event=lambda _event: None,
        )

    assert calls == 1


def test_wkwebview_new_chat_recovers_identity_by_client_message_id(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    helper_calls = 0
    identity_events: list[dict] = []

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        nonlocal helper_calls
        helper_calls += 1
        return {
            "ok": True,
            "identity_recovery_required": True,
            "client_message_id": "client-message-1",
            "conversation_id": "",
            "response_status": 200,
            "attachment_count": 0,
            "write_commit_proven": False,
            "canonical_committed": False,
            "stream_terminal_observed": False,
            "stream_resume_present": False,
            "stream_resume_handoff_written": False,
        }

    canonical = _final_canonical_for_prompt("recover me", assistant_text="recovered")
    canonical["mapping"]["user-final"]["message"]["id"] = "client-message-1"
    provider._lightweight_transport = SimpleNamespace(
        source_client=SimpleNamespace(
            auth=SimpleNamespace(cookies={"session": "test-cookie"})
        ),
        read_catalog=lambda *args, **kwargs: {
            "items": [{"id": "conversation-recovered"}],
            "total": 1,
        },
    )
    monkeypatch.setattr(
        provider,
        "_read_conversation_payload_via_coordinated_curl",
        lambda *args, **kwargs: (canonical, None),
    )

    def fail_generic_recovery(*args, **kwargs):
        raise AssertionError(
            "identity recovery must not use generic/WK canonical fallback"
        )

    monkeypatch.setattr(provider, "read_catalog_payload", fail_generic_recovery)
    monkeypatch.setattr(
        provider, "_read_conversation_payload_uncached", fail_generic_recovery
    )

    def fail_resume(**kwargs):
        raise AssertionError("canonical identity recovery must not start WS resume")

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    monkeypatch.setattr(provider, "_resume_via_curl_ws_second_leg", fail_resume)

    with provider.require_profile("FAST"):
        result = provider.send_text_streaming(
            "recover me",
            on_text_event=lambda event: None,
            on_write_identity=identity_events.append,
        )

    assert helper_calls == 1
    assert result.conversation_id == "conversation-recovered"
    assert result.phase_a_transport == "wkwebview_minimal_security_shell"
    assert result.phase_b_transport == "canonical_message_id_recovery"
    assert result.phase_b_fallback_reason is None
    assert identity_events[-1]["conversation_id"] == "conversation-recovered"
    cached = provider._canonical_state.take_final_payload("conversation-recovered")
    assert cached == canonical


def test_turn_broker_connection_end_preserves_positive_request_without_inference(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeConnection:
        def __init__(self, *, emit_submit: bool) -> None:
            self.emit_submit = emit_submit
            self.closed = False
            self.calls = 0

        def recv_envelope(self, timeout: float):
            self.calls += 1
            if self.emit_submit and self.calls == 1:
                return {
                    "type": "event",
                    "event": {
                        "type": "submit_request_observed",
                        "temporary_mode": False,
                    },
                }
            raise RequestError(
                "WKWEBVIEW_TURN_BROKER_CONNECTION_ENDED",
                request_stage="wkwebview_authority_turn",
            )

        def close(self) -> None:
            self.closed = True

    connections: list[FakeConnection] = []

    class FakeBrokerClient:
        def __init__(self, helper_binary: Path) -> None:
            self.helper_binary = helper_binary

        def start_turn(self, request: dict[str, Any], *, timeout: float):
            connection = FakeConnection(emit_submit=not connections)
            connections.append(connection)
            return connection

    monkeypatch.setattr(
        "chatgpt_web_adapter.wkwebview_helper_runtime.WKSystemTurnBrokerClient",
        FakeBrokerClient,
    )
    runtime = WKWebViewHelperRuntime(tmp_path, build_timeout=1)
    invocation = SimpleNamespace(request={"prompt": "hello"})

    with pytest.raises(RequestError) as submitted:
        runtime._run_streaming_via_turn_broker(
            invocation,
            timeout=1.0,
            on_text_event=lambda _event: None,
            on_lifecycle_event=None,
            on_transport_event=None,
            on_submit_started=None,
            external_completion_check=None,
        )
    assert submitted.value.write_dispatched is True
    assert submitted.value.submit_request_observed is True
    assert submitted.value.submit_response_observed is None

    with pytest.raises(RequestError) as unobserved:
        runtime._run_streaming_via_turn_broker(
            invocation,
            timeout=1.0,
            on_text_event=lambda _event: None,
            on_lifecycle_event=None,
            on_transport_event=None,
            on_submit_started=None,
            external_completion_check=None,
        )
    assert unobserved.value.write_dispatched is None
    assert unobserved.value.submit_request_observed is None
    assert unobserved.value.submit_response_observed is None
    assert all(connection.closed for connection in connections)
