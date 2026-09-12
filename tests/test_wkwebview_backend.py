from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import chatgpt_web_adapter.browser_authority_backend as browser_backend
from chatgpt_web_adapter.browser_authority_backend import (
    CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND,
    WKWEBVIEW_BROWSER_AUTHORITY_BACKEND,
    normalize_browser_authority_backend,
)
from chatgpt_web_adapter.exceptions import RequestError
from chatgpt_web_adapter.product_capabilities import (
    IMAGES,
    TEMPORARY_CHAT,
    CapabilityState,
)
from chatgpt_web_adapter.product_runtime import assemble_product_runtime
from chatgpt_web_adapter.wkwebview_canonical import (
    WKWEBVIEW_CANONICAL_READ_PLANE,
    WKCanonicalState,
    WKWebViewCanonicalClient,
)
from chatgpt_web_adapter.wkwebview_helper_runtime import WKWebViewHelperRuntime
from chatgpt_web_adapter.wkwebview_provider import WKWebViewTurnProvider


def _invocation_argv(invocation) -> list[str]:
    command = getattr(invocation, "command", invocation)
    return list(command)


def _invocation_request(invocation) -> dict:
    request = getattr(invocation, "request", {})
    return dict(request) if isinstance(request, dict) else {}


class _Client:
    def get_status(self, conversation):
        return SimpleNamespace(status="completed")

    def get_messages(self, conversation, **kwargs):
        return []

    def attach_conversation(self, conversation):
        return SimpleNamespace(conversation_id=conversation)


def _helper_payload(value: dict) -> dict:
    encoded = base64.b64encode(json.dumps(value).encode("utf-8")).decode("ascii")
    return {
        "ok": True,
        "status": 200,
        "content_type": "application/json",
        "body_base64": encoded,
    }


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


def test_browser_authority_backend_selection_is_closed() -> None:
    assert normalize_browser_authority_backend(" chrome-native ") == (
        CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND
    )
    assert normalize_browser_authority_backend(" WKWEBVIEW ") == (
        WKWEBVIEW_BROWSER_AUTHORITY_BACKEND
    )
    with pytest.raises(ValueError, match="unsupported browser authority backend"):
        normalize_browser_authority_backend("webkit-ish")


@pytest.mark.parametrize(
    ("platform_name", "macos_version", "expected"),
    [
        ("darwin", (12, 0), WKWEBVIEW_BROWSER_AUTHORITY_BACKEND),
        ("darwin", (15, 6), WKWEBVIEW_BROWSER_AUTHORITY_BACKEND),
        ("darwin", (11, 7), CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND),
        ("darwin", None, CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND),
        ("linux", None, CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND),
        ("win32", None, CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND),
    ],
)
def test_platform_default_browser_authority_backend_is_safe(
    platform_name: str,
    macos_version: tuple[int, int] | None,
    expected: str,
) -> None:
    assert (
        browser_backend._select_default_browser_authority_backend(
            platform_name=platform_name,
            macos_version=macos_version,
        )
        == expected
    )


def test_runtime_uses_resolved_default_browser_authority_backend(monkeypatch) -> None:
    monkeypatch.setattr(
        browser_backend,
        "DEFAULT_BROWSER_AUTHORITY_BACKEND",
        WKWEBVIEW_BROWSER_AUTHORITY_BACKEND,
    )

    runtime = assemble_product_runtime(client=_Client())

    assert isinstance(runtime.write_transport.provider, WKWebViewTurnProvider)
    assert runtime.governance()["browser_authority_backend"] == "wkwebview"
    assert (
        runtime.governance()["browser_authority_effective_runtime_default_policy"]
        == "TURN_SCOPED"
    )


def test_explicit_chrome_native_overrides_promoted_default(monkeypatch) -> None:
    monkeypatch.setattr(
        browser_backend,
        "DEFAULT_BROWSER_AUTHORITY_BACKEND",
        WKWEBVIEW_BROWSER_AUTHORITY_BACKEND,
    )

    runtime = assemble_product_runtime(
        client=_Client(),
        browser_authority_backend="chrome-native",
    )

    assert not isinstance(runtime.write_transport.provider, WKWebViewTurnProvider)
    assert runtime.governance()["browser_authority_backend"] == "chrome-native"


def test_runtime_assembles_wkwebview_behind_browser_owned_boundary() -> None:
    runtime = assemble_product_runtime(
        client=_Client(),
        browser_authority_backend="wkwebview",
        browser_authority_policy="TURN_SCOPED",
    )

    assert isinstance(runtime.write_transport.provider, WKWebViewTurnProvider)
    assert isinstance(runtime.canonical, WKWebViewCanonicalClient)
    assert runtime.canonical.canonical_read_plane == WKWEBVIEW_CANONICAL_READ_PLANE
    governance = runtime.governance()
    assert governance["browser_authority_backend"] == "wkwebview"
    assert (
        governance["browser_authority_effective_runtime_default_policy"]
        == "TURN_SCOPED"
    )
    assert governance["model_slug_product_runtime_selection_supported"] is True
    assert governance["media_product_runtime_supported"] is True
    assert governance["media_semantic_default_model_profile_supported"] is True
    assert governance["temporary_chat_product_runtime_selection_supported"] is True
    assert runtime.capabilities().state(IMAGES) is CapabilityState.AVAILABLE
    assert runtime.capabilities().state(TEMPORARY_CHAT) is CapabilityState.AVAILABLE
    assert governance["streaming_source"] == "WKWEBVIEW_RESUME_FENCED_PRODUCT_STREAM"
    assert governance["streaming_canonical_finality"] == (
        WKWEBVIEW_CANONICAL_READ_PLANE
    )


def test_runtime_backend_selection_rejects_conflicting_low_level_injection() -> None:
    provider = WKWebViewTurnProvider()
    with pytest.raises(ValueError, match="provider and browser_authority_backend"):
        assemble_product_runtime(
            client=_Client(),
            provider=provider,
            browser_authority_backend="wkwebview",
        )
    with pytest.raises(ValueError, match="requires transport='browser-owned'"):
        assemble_product_runtime(
            client=_Client(),
            transport="browserless-request",
            browser_authority_backend="wkwebview",
        )


def test_wkwebview_provider_text_attachment_download_is_cached() -> None:
    provider = WKWebViewTurnProvider()

    class _AttachmentTransport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, float]] = []

        def download_attachment(self, file_id: str, *, timeout: float):
            self.calls.append((file_id, timeout))
            return b"# Instructions\nFull text.", {"mime_type": "text/markdown"}

    transport = _AttachmentTransport()
    provider._lightweight_transport = transport
    attachment = {
        "id": "file-1",
        "name": "instructions.md",
        "mime_type": "text/markdown",
        "size": 25,
    }

    assert provider.read_text_attachment(attachment, timeout=7) == (
        "# Instructions\nFull text."
    )
    assert provider.read_text_attachment(attachment, timeout=9) == (
        "# Instructions\nFull text."
    )
    assert transport.calls == [("file-1", 7)]

    assert (
        provider.read_text_attachment(
            {
                "id": "file-2",
                "name": "diagram.pdf",
                "mime_type": "application/pdf",
            }
        )
        is None
    )
    assert transport.calls == [("file-1", 7)]


def test_wkwebview_catalog_read_uses_helper_and_decodes_json(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    commands: list[list[str]] = []
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))

    def fake_run(command, *, timeout):
        commands.append(_invocation_argv(command))
        return _helper_payload({"items": [{"id": "conversation-1"}], "total": 1})

    monkeypatch.setattr(provider, "_run_helper", fake_run)

    payload = provider.read_catalog_payload(
        "conversations",
        offset=3,
        limit=7,
        is_archived=True,
        is_starred=True,
        timeout=9,
    )

    assert payload["items"] == [{"id": "conversation-1"}]
    command = commands[0]
    assert command[:3] == ["/tmp/wk-helper", "--catalog", "conversations"]
    assert command[command.index("--offset") + 1] == "3"
    assert command[command.index("--limit") + 1] == "7"
    assert "--archived" in command
    assert "--starred" in command


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


def test_wkwebview_stop_context_stays_in_private_request_envelope(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))

    invocation = provider._helper_command(
        conversation_id="conversation-private",
        text=None,
        timeout=12,
        stop_only=True,
        stop_context=("conduit-test", "trace-test"),
    )

    request = _invocation_request(invocation)
    argv = _invocation_argv(invocation)
    assert request["stop_context_conduit"] == "conduit-test"
    assert request["stop_context_trace"] == "trace-test"
    assert "conduit-test" not in argv
    assert "trace-test" not in argv
    assert "conversation-private" not in argv


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
    json.dumps({"v": 1, "r": "resume-test", "c": "conduit-test", "t": "trace-test"}).encode(),
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
    assert payload["_cwa_stop_conduit_token"] == "conduit-test"
    assert payload["_cwa_stop_turn_trace_id"] == "trace-test"


def test_wkwebview_canonical_read_caches_current_node(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    monkeypatch.setattr(
        provider,
        "_run_helper",
        lambda command, *, timeout: _helper_payload(
            {"current_node": "node-2", "mapping": {"node-2": {}}}
        ),
    )

    payload = provider.read_conversation_payload("conversation-1", timeout=5)

    assert payload["current_node"] == "node-2"
    assert provider._cached_current_node("conversation-1") == "node-2"


def test_wkwebview_canonical_read_prefers_curl_second_leg(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    calls: list[tuple[str, float]] = []

    def fake_curl_read(conversation_id: str, *, timeout: float):
        calls.append((conversation_id, timeout))
        return {"current_node": "node-curl", "mapping": {"node-curl": {}}}

    monkeypatch.setattr(provider, "_read_conversation_payload_via_curl", fake_curl_read)

    def fail_if_wk_helper_runs():
        raise AssertionError(
            "canonical pre-read should not launch WK when curl succeeds"
        )

    monkeypatch.setattr(provider, "_ensure_helper", fail_if_wk_helper_runs)

    payload = provider.read_conversation_payload("conversation-curl", timeout=7)

    assert payload["current_node"] == "node-curl"
    assert calls == [("conversation-curl", 7.0)]
    assert provider._cached_current_node("conversation-curl") == "node-curl"
    assert provider._canonical_read_observation() == ("curl_cffi", None)


def test_wkwebview_canonical_read_falls_back_to_helper_when_curl_unavailable(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(
        provider,
        "_read_conversation_payload_via_curl",
        lambda conversation_id, *, timeout: None,
    )
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    commands: list[list[str]] = []
    requests: list[dict] = []

    def fake_run(command, *, timeout):
        commands.append(_invocation_argv(command))
        requests.append(_invocation_request(command))
        return _helper_payload({"current_node": "node-wk", "mapping": {"node-wk": {}}})

    monkeypatch.setattr(provider, "_run_helper", fake_run)

    payload = provider.read_conversation_payload("conversation-fallback", timeout=5)

    assert payload["current_node"] == "node-wk"
    assert requests[0]["canonical_conversation"] == "conversation-fallback"
    assert "conversation-fallback" not in commands[0]
    assert provider._canonical_read_observation() == ("wkwebview", None)


def test_wkwebview_canonical_fallback_records_reason(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)

    class FailingLightweightTransport:
        def read_canonical(self, conversation_id: str, *, timeout: float):
            return None

        def take_canonical_fallback_reason(self):
            return "WKWEBVIEW_CURL_CANONICAL_HTTP:503"

    provider._lightweight_transport = FailingLightweightTransport()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    monkeypatch.setattr(
        provider,
        "_run_helper",
        lambda command, *, timeout: _helper_payload(
            {"current_node": "node-wk", "mapping": {"node-wk": {}}}
        ),
    )

    provider.read_conversation_payload("conversation-fallback", timeout=5)

    assert provider._canonical_read_observation() == (
        "wkwebview",
        "WKWEBVIEW_CURL_CANONICAL_HTTP:503",
    )


def test_wkwebview_lightweight_path_is_default_with_explicit_legacy_escape_hatch(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    assert provider._lightweight_path_enabled() is True

    for value in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("CWA_WK_FORCE_LEGACY", value)
        assert provider._lightweight_path_enabled() is False

    monkeypatch.setenv("CWA_WK_FORCE_LEGACY", "0")
    assert provider._lightweight_path_enabled() is True


def test_wkwebview_declares_revision_safe_streaming_capability() -> None:
    provider = WKWebViewTurnProvider()

    assert provider.revision_safe_streaming_supported is True
    assert callable(provider.send_text_streaming)
    assert callable(provider.send_text_with_stale_ui_recovery_streaming)


def test_wkwebview_force_legacy_streaming_uses_direct_wk_resume(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setenv("CWA_WK_FORCE_LEGACY", "1")
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))

    def fail_if_duplicate_commit_check_runs(**kwargs):
        raise AssertionError(
            "helper canonical commit proof must avoid duplicate provider polling"
        )

    monkeypatch.setattr(
        provider,
        "_wait_for_canonical_write_commit",
        fail_if_duplicate_commit_check_runs,
    )

    calls: list[tuple[list[str], dict]] = []
    final_canonical = _final_canonical_for_prompt("hello", assistant_text="hello world")

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        calls.append((_invocation_argv(command), _invocation_request(command)))
        if "resume_conversation" not in _invocation_request(command):
            assert callable(on_lifecycle_event)
            on_lifecycle_event(
                {
                    "type": "write_identity_resolved",
                    "conversation_id": "conversation-1",
                }
            )
            on_text_event(
                {
                    "type": "assistant_text_delta",
                    "sequence": 1,
                    "message_id": "assistant-1",
                    "delta": "hello ",
                }
            )
            return {
                "ok": True,
                "conversation_id": "conversation-1",
                "response_status": 200,
                "final_url": "https://chatgpt.com/c/conversation-1",
                "attachment_count": 0,
                "elapsed_ms": 100,
                "load_elapsed_ms": 50,
                "write_commit_proven": True,
                "write_commit_proof": "RESUME_FENCE",
                "canonical_committed": False,
                "committed_current_node": "",
                "stream_ended": False,
                "stream_terminal_observed": False,
                "stream_resume_present": True,
                "stream_resume_handoff_written": True,
                "stream_resume_value": "resume-secret",
            }

        assert _invocation_request(command)["resume_value"] == "resume-secret"
        assert "resume-secret" not in _invocation_argv(command)
        on_text_event(
            {
                "type": "assistant_text_delta",
                "sequence": 1,
                "message_id": "assistant-1",
                "delta": "world",
            }
        )
        return {
            "ok": True,
            "status": 200,
            "conversation_id": "conversation-1",
            "canonical_completed": True,
            "canonical_body_base64": base64.b64encode(
                json.dumps(final_canonical).encode("utf-8")
            ).decode("ascii"),
        }

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    events: list[dict] = []
    identity_events: list[dict] = []

    result = provider.send_text_streaming(
        "hello",
        on_text_event=events.append,
        on_write_identity=identity_events.append,
    )

    assert identity_events == [
        {"type": "write_identity_resolved", "conversation_id": "conversation-1"}
    ]
    assert [event["sequence"] for event in events] == [1, 2]
    assert [event["delta"] for event in events] == ["hello ", "world"]
    assert result.conversation_id == "conversation-1"
    assert result.passive_observer_armed is False
    assert result.phase_a_transport == "wkwebview_full_page"
    assert result.phase_b_transport == "wkwebview_direct_resume"
    first_command, first_request = calls[0]
    assert first_request["prompt"] == "hello"
    assert "hello" not in first_command
    assert "--observe-stream-until-resume-token" in first_command
    assert "--observe-stream-until-end" not in first_command
    second_command, second_request = calls[1]
    assert second_request["resume_value"] == "resume-secret"
    assert second_request["resume_offset"] == 0
    assert "--resume-value" not in second_command

    def fail_if_helper_runs(*args, **kwargs):
        raise AssertionError(
            "cached final canonical payload must avoid another helper read"
        )

    monkeypatch.setattr(provider, "_run_helper", fail_if_helper_runs)
    assert (
        provider.read_conversation_payload("conversation-1", timeout=5)
        == final_canonical
    )
    assert provider._cached_current_node("conversation-1") == "node-final"


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


def test_wkwebview_streaming_without_resume_keeps_heavy_page_until_final_canonical(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))

    def fail_if_duplicate_commit_check_runs(**kwargs):
        raise AssertionError(
            "final canonical helper proof must avoid duplicate polling"
        )

    monkeypatch.setattr(
        provider,
        "_wait_for_canonical_write_commit",
        fail_if_duplicate_commit_check_runs,
    )
    final_canonical = {
        "current_node": "node-final",
        "mapping": {
            "user-1": {
                "parent": None,
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["hello"]},
                },
            },
            "node-final": {
                "parent": "user-1",
                "message": {
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "status": "finished_successfully",
                    "end_turn": True,
                    "content": {"parts": ["done"]},
                },
            },
        },
    }
    calls: list[list[str]] = []

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        calls.append(_invocation_argv(command))
        assert "--resume-conversation" not in _invocation_argv(command)
        on_text_event(
            {
                "type": "assistant_text_snapshot",
                "sequence": 1,
                "message_id": "assistant-1",
                "text": "done",
            }
        )
        return {
            "ok": True,
            "conversation_id": "conversation-1",
            "response_status": 200,
            "final_url": "https://chatgpt.com/c/conversation-1",
            "attachment_count": 0,
            "elapsed_ms": 100,
            "load_elapsed_ms": 50,
            "write_commit_proven": True,
            "write_commit_proof": "FINAL_CANONICAL",
            "canonical_committed": False,
            "canonical_final_completed": True,
            "canonical_body_base64": base64.b64encode(
                json.dumps(final_canonical).encode("utf-8")
            ).decode("ascii"),
            "committed_current_node": "node-final",
            "stream_ended": False,
            "stream_terminal_observed": False,
            "stream_resume_present": False,
            "stream_resume_handoff_written": False,
        }

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    events: list[dict] = []

    result = provider.send_text_streaming("hello", on_text_event=events.append)

    assert len(calls) == 1
    assert result.passive_observer_armed is False
    assert events[0]["text"] == "done"

    def fail_if_helper_runs(*args, **kwargs):
        raise AssertionError(
            "cached final canonical payload must avoid another helper read"
        )

    monkeypatch.setattr(provider, "_run_helper", fail_if_helper_runs)
    assert (
        provider.read_conversation_payload("conversation-1", timeout=5)
        == final_canonical
    )


def test_wkwebview_stop_requires_canonical_client_stopped_proof(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    monkeypatch.setattr(
        provider,
        "_run_helper",
        lambda command, *, timeout: {
            "ok": True,
            "stop_requested": True,
            "status": 200,
            "conversation_id": "conversation-1",
        },
    )
    monkeypatch.setattr(
        provider,
        "_wait_for_stopped_final_payload",
        lambda conversation_id, *, timeout: None,
    )
    monkeypatch.setattr(
        provider,
        "_wait_for_canonical_stop_proof",
        lambda conversation_id, *, timeout: None,
    )

    with pytest.raises(RequestError, match="WKWEBVIEW_STOP_CANONICAL_NOT_PROVEN"):
        provider.stop_generation("conversation-1", timeout=5)

    assert provider.stop_requested_for("conversation-1") is False


def test_wkwebview_stop_uses_worker_stopped_final_without_second_canonical_reader(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    stopped_canonical = {
        "current_node": "node-stopped",
        "mapping": {
            "node-stopped": {
                "id": "node-stopped",
                "message": {
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "status": "finished_successfully",
                    "end_turn": True,
                    "content": {"content_type": "text", "parts": ["partial"]},
                    "metadata": {
                        "finish_details": {
                            "type": "interrupted",
                            "reason": "client_stopped",
                        }
                    },
                },
            }
        },
    }
    monkeypatch.setattr(
        provider,
        "_run_helper",
        lambda command, *, timeout: {
            "ok": True,
            "stop_requested": True,
            "status": 200,
            "conversation_id": "conversation-1",
        },
    )
    monkeypatch.setattr(
        provider,
        "_wait_for_stopped_final_payload",
        lambda conversation_id, *, timeout: stopped_canonical,
    )

    def fail_if_fallback_reads(*args, **kwargs):
        raise AssertionError(
            "worker stopped-final proof must avoid fallback canonical reads"
        )

    monkeypatch.setattr(
        provider, "_wait_for_canonical_stop_proof", fail_if_fallback_reads
    )

    result = provider.stop_generation("conversation-1", timeout=5)

    assert result["stopped"] is True
    assert result["conversationId"] == "conversation-1"
    assert provider.stop_requested_for("conversation-1") is True


def test_wkwebview_minimal_security_shell_gate_is_narrow(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    commands: list[list[str]] = []
    requests: list[dict[str, Any]] = []

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        commands.append(_invocation_argv(command))
        requests.append(_invocation_request(command))
        return {
            "ok": True,
            "conversation_id": "conversation-1",
            "response_status": 200,
            "attachment_count": 0,
            "write_commit_proven": True,
            "write_commit_proof": "RESUME_FENCE",
            "canonical_committed": False,
            "committed_current_node": "",
            "stream_ended": True,
            "stream_terminal_observed": False,
            "stream_resume_present": True,
            "stream_resume_handoff_written": True,
            "stream_resume_value": "resume-secret",
        }

    def fake_resume(**kwargs):
        canonical = _final_canonical_for_prompt(kwargs["text"], assistant_text="ok")
        return {
            "ok": True,
            "status": 200,
            "conversation_id": "conversation-1",
            "canonical_body_base64": base64.b64encode(
                json.dumps(canonical).encode("utf-8")
            ).decode("ascii"),
        }

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    monkeypatch.setattr(provider, "_resume_via_curl_ws_second_leg", fake_resume)

    provider.send_text_streaming("hello", on_text_event=lambda event: None)
    provider.send_text_streaming(
        "hello-model",
        model_slug="gpt-5-6-thinking",
        on_text_event=lambda event: None,
    )

    assert "--minimal-security-shell" in commands[0]
    assert "--minimal-security-shell" in commands[1]
    assert "minimal_model_slug" not in requests[0]
    assert requests[1]["minimal_model_slug"] == "gpt-5-6-thinking"


def test_wkwebview_minimal_security_shell_continuation_uses_canonical_parent(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    prewrite = {
        "current_node": "node-before",
        "mapping": {
            "node-before": {
                "parent": "user-before",
                "message": {
                    "id": "assistant-message-before",
                    "author": {"role": "assistant"},
                    "content": {"parts": ["previous answer"]},
                },
            }
        },
    }
    monkeypatch.setattr(
        provider, "read_conversation_payload", lambda *args, **kwargs: prewrite
    )
    commands: list[list[str]] = []
    requests: list[dict] = []

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        commands.append(_invocation_argv(command))
        requests.append(_invocation_request(command))
        return {
            "ok": True,
            "conversation_id": "conversation-1",
            "response_status": 200,
            "attachment_count": 0,
            "write_commit_proven": True,
            "write_commit_proof": "RESUME_FENCE",
            "canonical_committed": False,
            "committed_current_node": "",
            "stream_ended": False,
            "stream_terminal_observed": False,
            "stream_resume_present": True,
            "stream_resume_handoff_written": True,
            "stream_resume_value": "resume-secret",
        }

    def fake_resume(**kwargs):
        canonical = _final_canonical_for_prompt(
            kwargs["text"], assistant_text="continued"
        )
        return {
            "ok": True,
            "status": 200,
            "conversation_id": "conversation-1",
            "canonical_body_base64": base64.b64encode(
                json.dumps(canonical).encode("utf-8")
            ).decode("ascii"),
        }

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    monkeypatch.setattr(provider, "_resume_via_curl_ws_second_leg", fake_resume)

    with provider.require_profile("DEEP"):
        result = provider.send_text_streaming(
            "continue",
            conversation="conversation-1",
            on_text_event=lambda event: None,
        )

    assert result.conversation_id == "conversation-1"
    assert len(commands) == 1
    command = commands[0]
    assert "--minimal-security-shell" in command
    request = requests[0]
    assert request["minimal_conversation_id"] == "conversation-1"
    assert request["minimal_parent_message_id"] == "assistant-message-before"
    assert request["expected_current_node"] == "node-before"
    assert "conversation-1" not in command
    assert "assistant-message-before" not in command


def test_wkwebview_minimal_security_shell_terminal_continuation_skips_resume(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    prewrite = {
        "current_node": "node-before",
        "mapping": {
            "node-before": {
                "message": {
                    "id": "assistant-message-before",
                    "author": {"role": "assistant"},
                    "content": {"parts": ["previous answer"]},
                }
            }
        },
    }
    monkeypatch.setattr(
        provider, "read_conversation_payload", lambda *args, **kwargs: prewrite
    )

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        return {
            "ok": True,
            "conversation_id": "conversation-1",
            "response_status": 200,
            "attachment_count": 0,
            "write_commit_proven": True,
            "write_commit_proof": "PHASE_A_TERMINAL",
            "canonical_committed": False,
            "committed_current_node": "",
            "stream_ended": True,
            "stream_terminal_observed": True,
            "stream_resume_present": False,
            "stream_resume_handoff_written": False,
        }

    def fail_resume(**kwargs):
        raise AssertionError("terminal Phase A must not start a resume second leg")

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    monkeypatch.setattr(provider, "_resume_via_curl_ws_second_leg", fail_resume)

    with provider.require_profile("FAST"):
        result = provider.send_text_streaming(
            "continue",
            conversation="conversation-1",
            on_text_event=lambda event: None,
        )

    assert result.conversation_id == "conversation-1"
    assert result.phase_a_transport == "wkwebview_minimal_security_shell"
    assert result.phase_b_transport == "phase_one_terminal"
    assert result.phase_b_fallback_reason is None


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
        read_catalog=lambda *args, **kwargs: {
            "items": [{"id": "conversation-recovered"}],
            "total": 1,
        }
    )
    monkeypatch.setattr(
        provider,
        "_read_conversation_payload_via_curl",
        lambda *args, **kwargs: canonical,
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


def test_wkwebview_identity_recovery_curl_failure_fails_closed_without_wk_fallback(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        return {
            "ok": True,
            "identity_recovery_required": True,
            "client_message_id": "client-message-timeout",
            "conversation_id": "",
            "response_status": 200,
            "attachment_count": 0,
            "write_commit_proven": False,
            "canonical_committed": False,
            "stream_terminal_observed": False,
            "stream_resume_present": False,
            "stream_resume_handoff_written": False,
        }

    provider._lightweight_transport = SimpleNamespace(
        read_catalog=lambda *args, **kwargs: {
            "items": [{"id": "conversation-unreadable"}],
            "total": 1,
        }
    )
    monkeypatch.setattr(
        provider,
        "_read_conversation_payload_via_curl",
        lambda *args, **kwargs: None,
    )

    def fail_generic_recovery(*args, **kwargs):
        raise AssertionError("identity recovery must never invoke WK fallback")

    monkeypatch.setattr(provider, "read_catalog_payload", fail_generic_recovery)
    monkeypatch.setattr(
        provider, "_read_conversation_payload_uncached", fail_generic_recovery
    )
    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)

    with provider.require_profile("FAST"):
        with pytest.raises(RequestError, match="WKWEBVIEW_IDENTITY_RECOVERY_TIMEOUT"):
            provider.send_text_streaming(
                "recover without curl",
                timeout=0.1,
                on_text_event=lambda event: None,
            )


def test_wkwebview_minimal_security_shell_continuation_preserves_canonical_selection(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    prewrite = {
        "current_node": "node-before",
        "default_model_slug": "gpt-5-6-thinking",
        "mapping": {
            "node-before": {
                "message": {
                    "id": "assistant-message-before",
                    "author": {"role": "assistant"},
                    "metadata": {"thinking_effort": "extended"},
                    "content": {"parts": ["previous answer"]},
                }
            }
        },
    }
    monkeypatch.setattr(
        provider, "read_conversation_payload", lambda *args, **kwargs: prewrite
    )
    commands: list[list[str]] = []
    requests: list[dict] = []

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        commands.append(_invocation_argv(command))
        requests.append(_invocation_request(command))
        return {
            "ok": True,
            "conversation_id": "conversation-1",
            "response_status": 200,
            "attachment_count": 0,
            "write_commit_proven": True,
            "write_commit_proof": "RESUME_FENCE",
            "canonical_committed": False,
            "committed_current_node": "",
            "stream_ended": False,
            "stream_terminal_observed": False,
            "stream_resume_present": True,
            "stream_resume_handoff_written": True,
            "stream_resume_value": "resume-secret",
        }

    def fake_resume(**kwargs):
        canonical = _final_canonical_for_prompt(
            kwargs["text"], assistant_text="continued"
        )
        return {
            "ok": True,
            "status": 200,
            "conversation_id": "conversation-1",
            "canonical_body_base64": base64.b64encode(
                json.dumps(canonical).encode("utf-8")
            ).decode("ascii"),
        }

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    monkeypatch.setattr(provider, "_resume_via_curl_ws_second_leg", fake_resume)

    result = provider.send_text_streaming(
        "continue",
        conversation="conversation-1",
        on_text_event=lambda event: None,
    )

    assert result.conversation_id == "conversation-1"
    command = commands[0]
    assert "--minimal-security-shell" in command
    request = requests[0]
    assert request["minimal_model_slug"] == "gpt-5-6-thinking"
    assert request["minimal_thinking_effort"] == "extended"
    assert "gpt-5-6-thinking" not in command
    assert "extended" not in command


def test_wkwebview_minimal_security_shell_uploads_attachments_before_wk(
    monkeypatch, tmp_path
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    attachment = tmp_path / "red.png"
    attachment.write_bytes(b"fake-png")

    class FakeSourceClient:
        def wk_transport_upload_media_files(self, media):
            assert len(media) == 1
            assert Path(media[0][0]) == attachment
            return [
                {
                    "file_id": "file-1",
                    "file_name": "red.png",
                    "file_size": 8,
                    "mime_type": "image/png",
                    "width": 64,
                    "height": 64,
                }
            ]

    provider.build_canonical_client(FakeSourceClient())
    commands: list[list[str]] = []
    requests: list[dict] = []

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        commands.append(_invocation_argv(command))
        requests.append(_invocation_request(command))
        return {
            "ok": True,
            "conversation_id": "conversation-1",
            "response_status": 200,
            "attachment_count": 1,
            "write_commit_proven": True,
            "write_commit_proof": "RESUME_FENCE",
            "canonical_committed": False,
            "committed_current_node": "",
            "stream_ended": False,
            "stream_terminal_observed": False,
            "stream_resume_present": True,
            "stream_resume_handoff_written": True,
            "stream_resume_value": "resume-secret",
        }

    def fake_resume(**kwargs):
        canonical = _final_canonical_for_prompt(
            kwargs["text"], assistant_text="image ok"
        )
        return {
            "ok": True,
            "status": 200,
            "conversation_id": "conversation-1",
            "canonical_body_base64": base64.b64encode(
                json.dumps(canonical).encode("utf-8")
            ).decode("ascii"),
        }

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    monkeypatch.setattr(provider, "_resume_via_curl_ws_second_leg", fake_resume)

    result = provider.send_text_streaming(
        "inspect image",
        attachment_paths=[attachment],
        on_text_event=lambda event: None,
    )

    assert result.attachment_count == 1
    assert len(commands) == 1
    command = commands[0]
    assert "--minimal-security-shell" in command
    assert "--attach" not in command
    descriptors = requests[0]["minimal_attachments"]
    assert descriptors == [
        {
            "file_id": "file-1",
            "file_name": "red.png",
            "file_size": 8,
            "mime_type": "image/png",
            "width": 64,
            "height": 64,
        }
    ]


def test_wkwebview_minimal_security_attachment_transport_failure_falls_back_to_spa(
    monkeypatch, tmp_path
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    attachment = tmp_path / "red.png"
    attachment.write_bytes(b"fake-png")

    class FailingSourceClient:
        def wk_transport_upload_media_files(self, media):
            raise OSError("upload transport unavailable")

    provider.build_canonical_client(FailingSourceClient())
    commands: list[list[str]] = []
    requests: list[dict] = []

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        commands.append(_invocation_argv(command))
        requests.append(_invocation_request(command))
        return {
            "ok": True,
            "conversation_id": "conversation-1",
            "response_status": 200,
            "attachment_count": 1,
            "write_commit_proven": True,
            "write_commit_proof": "RESUME_FENCE",
            "canonical_committed": False,
            "committed_current_node": "",
            "stream_ended": False,
            "stream_terminal_observed": False,
            "stream_resume_present": True,
            "stream_resume_handoff_written": True,
            "stream_resume_value": "resume-secret",
        }

    def fake_resume(**kwargs):
        canonical = _final_canonical_for_prompt(
            kwargs["text"], assistant_text="image ok"
        )
        return {
            "ok": True,
            "status": 200,
            "conversation_id": "conversation-1",
            "canonical_body_base64": base64.b64encode(
                json.dumps(canonical).encode("utf-8")
            ).decode("ascii"),
        }

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    monkeypatch.setattr(provider, "_resume_via_curl_ws_second_leg", fake_resume)

    result = provider.send_text_streaming(
        "inspect image",
        attachment_paths=[attachment],
        on_text_event=lambda event: None,
    )

    assert result.attachment_count == 1
    command = commands[0]
    assert "--minimal-security-shell" not in command
    assert requests[0]["attachments"] == [str(attachment.resolve())]
    assert str(attachment.resolve()) not in command


def test_wkwebview_minimal_security_continuation_without_assistant_parent_falls_back(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    prewrite = {
        "current_node": "user-before",
        "mapping": {
            "user-before": {
                "message": {
                    "id": "user-message-before",
                    "author": {"role": "user"},
                    "content": {"parts": ["unfinished"]},
                }
            }
        },
    }
    monkeypatch.setattr(
        provider, "read_conversation_payload", lambda *args, **kwargs: prewrite
    )
    commands: list[list[str]] = []

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        commands.append(_invocation_argv(command))
        return {
            "ok": True,
            "conversation_id": "conversation-1",
            "response_status": 200,
            "attachment_count": 0,
            "write_commit_proven": True,
            "write_commit_proof": "RESUME_FENCE",
            "canonical_committed": False,
            "committed_current_node": "",
            "stream_ended": False,
            "stream_terminal_observed": False,
            "stream_resume_present": True,
            "stream_resume_handoff_written": True,
            "stream_resume_value": "resume-secret",
        }

    def fake_resume(**kwargs):
        canonical = _final_canonical_for_prompt(
            kwargs["text"], assistant_text="continued"
        )
        return {
            "ok": True,
            "status": 200,
            "conversation_id": "conversation-1",
            "canonical_body_base64": base64.b64encode(
                json.dumps(canonical).encode("utf-8")
            ).decode("ascii"),
        }

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    monkeypatch.setattr(provider, "_resume_via_curl_ws_second_leg", fake_resume)

    result = provider.send_text_streaming(
        "continue",
        conversation="conversation-1",
        on_text_event=lambda event: None,
    )

    assert result.conversation_id == "conversation-1"
    command = commands[0]
    assert "--minimal-security-shell" not in command
    assert "--minimal-conversation-id" not in command


def test_wkwebview_curl_ws_resume_runs_after_phase_one_stream_eof(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    final_canonical = _final_canonical_for_prompt("hello", assistant_text="hello world")

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        return {
            "ok": True,
            "conversation_id": "conversation-1",
            "response_status": 200,
            "final_url": "https://chatgpt.com/c/conversation-1",
            "attachment_count": 0,
            "elapsed_ms": 100,
            "load_elapsed_ms": 50,
            "write_commit_proven": True,
            "write_commit_proof": "RESUME_FENCE",
            "canonical_committed": False,
            "committed_current_node": "",
            "stream_ended": True,
            "stream_terminal_observed": False,
            "stream_resume_present": True,
            "stream_resume_handoff_written": True,
            "stream_resume_value": "resume-secret",
        }

    calls: list[dict] = []

    def fake_resume(**kwargs):
        calls.append(dict(kwargs))
        assert kwargs["resume_value"] == "resume-secret"
        kwargs["relay_text_event"](
            {
                "type": "assistant_text_delta",
                "sequence": 1,
                "message_id": "assistant-1",
                "delta": "world",
            }
        )
        return {
            "ok": True,
            "status": 200,
            "conversation_id": "conversation-1",
            "canonical_body_base64": base64.b64encode(
                json.dumps(final_canonical).encode("utf-8")
            ).decode("ascii"),
        }

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    monkeypatch.setattr(provider, "_resume_via_curl_ws_second_leg", fake_resume)
    events: list[dict] = []

    result = provider.send_text_streaming("hello", on_text_event=events.append)

    assert result.conversation_id == "conversation-1"
    assert result.passive_observer_armed is False
    assert result.canonical_read_transport is None
    assert result.phase_a_transport == "wkwebview_minimal_security_shell"
    assert isinstance(result.phase_a_gate_wait_ms, int)
    assert isinstance(result.phase_a_elapsed_ms, int)
    assert result.phase_b_transport == "curl_cffi_websocket"
    assert result.phase_b_fallback_reason is None
    assert isinstance(result.phase_b_elapsed_ms, int)
    assert [event["delta"] for event in events] == ["world"]
    assert len(calls) == 1
    assert calls[0]["conversation_id"] == "conversation-1"


def test_wkwebview_curl_ws_transport_failure_arms_passive_observer(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))

    def fake_stream(*args, **kwargs):
        return {
            "ok": True,
            "conversation_id": "conversation-1",
            "response_status": 200,
            "attachment_count": 0,
            "write_commit_proven": True,
            "stream_terminal_observed": False,
            "stream_resume_value": "resume-secret",
        }

    def fake_resume(**kwargs):
        raise RequestError("ws unavailable", request_stage="transport")

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    monkeypatch.setattr(provider, "_resume_via_curl_ws_second_leg", fake_resume)

    result = provider.send_text_streaming("hello", on_text_event=lambda event: None)

    assert result.passive_observer_armed is True
    assert result.phase_b_transport == "passive_canonical_observer"
    assert result.phase_b_fallback_reason == "transport"
    assert isinstance(result.phase_b_elapsed_ms, int)


def test_wkwebview_curl_ws_auth_failure_does_not_fallback(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))

    def fake_stream(*args, **kwargs):
        return {
            "ok": True,
            "conversation_id": "conversation-1",
            "response_status": 200,
            "attachment_count": 0,
            "write_commit_proven": True,
            "stream_terminal_observed": False,
            "stream_resume_value": "resume-secret",
        }

    def fake_resume(**kwargs):
        raise RequestError(
            "WKWEBVIEW_CURL_WS_CANONICAL_HTTP:401",
            request_stage="wkwebview_curl_ws_second_leg",
            status_code=401,
        )

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    monkeypatch.setattr(provider, "_resume_via_curl_ws_second_leg", fake_resume)

    with pytest.raises(RequestError, match="WKWEBVIEW_CURL_WS_CANONICAL_HTTP:401"):
        provider.send_text_streaming("hello", on_text_event=lambda event: None)


def test_minimal_security_shell_keeps_named_stage_boundaries() -> None:
    source = (
        Path(__file__).parents[1]
        / "src"
        / "chatgpt_web_adapter"
        / "wkwebview_helper"
        / "minimal_security_shell.js"
    ).read_text(encoding="utf-8")

    stages = (
        "bootstrapProductResources",
        "loadIntegrityRuntime",
        "loadSession",
        "loadModelCatalog",
        "resolveModelSelection",
        "acquireIntegrityBundle",
        "prepareConversation",
        "protectedWrite",
    )
    for stage_name in stages:
        assert f"const {stage_name} =" in source

    entrypoint = source.rsplit("  (async () => {", 1)[1]
    assert "await fetch(" not in entrypoint
    for invocation in (
        "bootstrapProductResources()",
        "loadIntegrityRuntime(integrityURL)",
        "loadSession()",
        "loadModelCatalog(accessToken)",
        "resolveModelSelection(modelsPayload)",
        "acquireIntegrityBundle(acquireIntegrity)",
        "prepareConversation({",
        "protectedWrite({",
    ):
        assert invocation in entrypoint


def test_wkwebview_status_does_not_hide_programming_errors(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()

    def fail_helper():
        raise ValueError("broken invariant")

    monkeypatch.setattr(provider, "_ensure_helper", fail_helper)

    with pytest.raises(ValueError, match="broken invariant"):
        provider.status()


def test_wkwebview_write_commit_retry_uses_bounded_backoff(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    calls = []
    final_payload = _final_canonical_for_prompt("hello", assistant_text="done")
    payloads = [
        {"current_node": "node-before", "mapping": {}},
        final_payload,
    ]
    monkeypatch.setattr(
        provider,
        "read_conversation_payload",
        lambda *args, **kwargs: payloads.pop(0),
    )
    monkeypatch.setattr(
        "chatgpt_web_adapter.wkwebview_provider.time.sleep",
        calls.append,
    )

    result = provider._wait_for_canonical_write_commit(
        conversation_id="conversation-1",
        text="hello",
        baseline_current_node="node-before",
        timeout=30,
    )

    assert result == final_payload
    assert calls == [1.0]


def test_wkwebview_identity_recovery_retry_uses_bounded_backoff(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    final_payload = _final_canonical_for_prompt("recover me", assistant_text="done")
    final_payload["mapping"]["user-final"]["message"]["id"] = "client-message-1"
    reads = [None, final_payload]
    provider._lightweight_transport = SimpleNamespace(
        read_catalog=lambda *args, **kwargs: {
            "items": [{"id": "conversation-recovered"}],
            "total": 1,
        }
    )
    monkeypatch.setattr(
        provider,
        "_read_conversation_payload_via_curl",
        lambda *args, **kwargs: reads.pop(0),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(
        "chatgpt_web_adapter.wkwebview_turn_orchestrator.time.sleep",
        sleeps.append,
    )

    result = provider._turn_orchestrator.recover_phase_one_identity(
        {
            "identity_recovery_required": True,
            "client_message_id": "client-message-1",
            "conversation_id": "",
        },
        prepared=SimpleNamespace(conversation_id=None),
        text="recover me",
        total_timeout=30,
        started=time.monotonic(),
    )

    assert result["conversation_id"] == "conversation-recovered"
    assert result["_cwa_identity_recovered"] is True
    assert sleeps == [1.0]


def test_wkwebview_write_commit_does_not_hide_programming_errors(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()

    def fail_read(*args, **kwargs):
        raise ValueError("broken canonical parser")

    monkeypatch.setattr(provider, "read_conversation_payload", fail_read)

    with pytest.raises(ValueError, match="broken canonical parser"):
        provider._wait_for_canonical_write_commit(
            conversation_id="conversation-1",
            text="hello",
            baseline_current_node="node-before",
            timeout=5,
        )


def test_wkwebview_observer_uses_conservative_canonical_poll_interval(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    captured = {}

    def fake_observer(invocation, **kwargs):
        captured["request"] = dict(invocation.request)
        return kwargs["on_event"](
            {
                "type": "canonical_payload",
                "status": 200,
                "body_base64": base64.b64encode(
                    json.dumps(
                        {
                            "current_node": "assistant-1",
                            "mapping": {
                                "assistant-1": {
                                    "message": {
                                        "id": "assistant-1",
                                        "author": {"role": "assistant"},
                                        "recipient": "all",
                                        "status": "finished_successfully",
                                        "content": {
                                            "content_type": "text",
                                            "parts": ["done"],
                                        },
                                        "metadata": {
                                            "finish_details": {"type": "stop"},
                                        },
                                    }
                                }
                            },
                        }
                    ).encode()
                ).decode(),
            }
        )

    monkeypatch.setattr(provider._helper_runtime, "run_event_observer", fake_observer)

    result = provider.observe_turn(
        conversation_id="conversation-1",
        turn_exchange_id=None,
        browser_authority_lease_id="lease-1",
        timeout=30,
    )

    assert result["ok"] is True
    assert captured["request"]["poll_interval"] == 15.0


def test_wkwebview_helper_observer_backs_off_after_429() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src/chatgpt_web_adapter/wkwebview_helper/WKChatGPTAuthority.m"
    ).read_text(encoding="utf-8")

    assert "observerStatus.integerValue == 429" in source
    assert "MAX(observerPollInterval, 60.0)" in source
    assert "cacheKey='__cwaAuthorityAccessToken'" in source
    assert "canonicalPollDelay = 1.0" in source
    assert "commitPollDelay = 1.0" in source


def test_wkwebview_observer_does_not_hide_malformed_canonical_payload(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))

    def fake_observer(invocation, **kwargs):
        return kwargs["on_event"]({"type": "canonical_payload", "status": 200})

    monkeypatch.setattr(provider._helper_runtime, "run_event_observer", fake_observer)

    with pytest.raises(RequestError, match="WKWEBVIEW_CANONICAL_OBSERVER_BODY_MISSING"):
        provider.observe_turn(
            conversation_id="conversation-1",
            turn_exchange_id=None,
            browser_authority_lease_id="lease-1",
            timeout=5,
        )


def test_wkwebview_dependencies_are_owned_by_cwa_packaging() -> None:
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert "wk-curl =" not in pyproject
    assert "wkwebview = [" not in pyproject
    assert "\"curl-cffi==0.16.3; sys_platform == 'darwin'\"" in pyproject
    assert "\"websockets==16.1.1; sys_platform == 'darwin'\"" in pyproject


def test_wkwebview_helper_rejects_macos_before_12(monkeypatch, tmp_path) -> None:
    runtime = WKWebViewHelperRuntime(tmp_path, build_timeout=1)
    monkeypatch.setattr(
        "chatgpt_web_adapter.wkwebview_helper_runtime.sys.platform", "darwin"
    )
    monkeypatch.setattr(runtime, "macos_version", lambda: (11, 7))

    with pytest.raises(RequestError, match=r"macOS 12\+ is required"):
        runtime.ensure_helper()


def test_wkwebview_helper_rejects_unknown_macos_version(monkeypatch, tmp_path) -> None:
    runtime = WKWebViewHelperRuntime(tmp_path, build_timeout=1)
    monkeypatch.setattr(
        "chatgpt_web_adapter.wkwebview_helper_runtime.sys.platform", "darwin"
    )
    monkeypatch.setattr(runtime, "macos_version", lambda: None)

    with pytest.raises(RequestError, match=r"macOS 12\+ is required"):
        runtime.ensure_helper()


def test_wkwebview_architecture_docs_capture_production_boundaries() -> None:
    root = Path(__file__).resolve().parents[1]
    architecture = (root / "docs" / "architecture.md").read_text(encoding="utf-8")

    for required in (
        "WKWebView backend — macOS 12+ pre-release path",
        "globally serialized short-lived WK protected write",
        "RESUME_FENCE + browser-issued resume handoff",
        "curl_cffi/WebSocket continuation stream",
        "inherited anonymous FD/pipe",
        "Authentication failures, malformed canonical/schema data",
        "CWA_WK_FORCE_LEGACY=1",
        "WKWEBVIEW_CANONICAL_READ",
        "actual read transport is reported separately",
        "Fallback reasons are normalized",
    ):
        assert required in architecture
