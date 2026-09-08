from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    WKWEBVIEW_CONTEXT_CANONICAL_READ_PLANE,
    WKWebViewCanonicalClient,
)
from chatgpt_web_adapter.wkwebview_provider import WKWebViewTurnProvider


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


def test_browser_authority_backend_selection_is_closed() -> None:
    assert normalize_browser_authority_backend(" chrome-native ") == (
        CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND
    )
    assert normalize_browser_authority_backend(" WKWEBVIEW ") == (
        WKWEBVIEW_BROWSER_AUTHORITY_BACKEND
    )
    with pytest.raises(ValueError, match="unsupported browser authority backend"):
        normalize_browser_authority_backend("webkit-ish")


def test_runtime_assembles_wkwebview_behind_browser_owned_boundary() -> None:
    runtime = assemble_product_runtime(
        client=_Client(),
        browser_authority_backend="wkwebview",
        browser_authority_policy="TURN_SCOPED",
    )

    assert isinstance(runtime.write_transport.provider, WKWebViewTurnProvider)
    assert isinstance(runtime.canonical, WKWebViewCanonicalClient)
    assert runtime.canonical.canonical_read_plane == WKWEBVIEW_CONTEXT_CANONICAL_READ_PLANE
    governance = runtime.governance()
    assert governance["browser_authority_backend"] == "wkwebview"
    assert governance["browser_authority_effective_runtime_default_policy"] == "TURN_SCOPED"
    assert governance["model_slug_product_runtime_selection_supported"] is False
    assert governance["media_product_runtime_supported"] is True
    assert governance["media_semantic_default_model_profile_supported"] is True
    assert governance["temporary_chat_product_runtime_selection_supported"] is False
    assert runtime.capabilities().state(IMAGES) is CapabilityState.AVAILABLE
    assert runtime.capabilities().state(TEMPORARY_CHAT) is CapabilityState.UNIMPLEMENTED
    assert governance["streaming_source"] == "WKWEBVIEW_PASSIVE_SSE"
    assert governance["streaming_canonical_finality"] == (
        WKWEBVIEW_CONTEXT_CANONICAL_READ_PLANE
    )
    with pytest.raises(ValueError, match="model selection is unavailable"):
        runtime.send_text("hello", model="gpt-5-6")


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


def test_wkwebview_catalog_read_uses_helper_and_decodes_json(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    commands: list[list[str]] = []
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))

    def fake_run(command, *, timeout):
        commands.append(list(command))
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

    assert command[command.index("--expected-current-node") + 1] == "node-7"


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


def test_wkwebview_declares_revision_safe_streaming_capability() -> None:
    provider = WKWebViewTurnProvider()

    assert provider.revision_safe_streaming_supported is True
    assert callable(provider.send_text_streaming)
    assert callable(provider.send_text_with_stale_ui_recovery_streaming)


def test_wkwebview_streaming_detaches_write_page_and_resumes_in_lightweight_context(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))

    def fail_if_duplicate_commit_check_runs(**kwargs):
        raise AssertionError("helper canonical commit proof must avoid duplicate provider polling")

    monkeypatch.setattr(
        provider,
        "_wait_for_canonical_write_commit",
        fail_if_duplicate_commit_check_runs,
    )

    calls: list[tuple[list[str], dict[str, str] | None]] = []
    final_canonical = {
        "current_node": "node-final",
        "mapping": {"node-final": {"id": "node-final"}},
    }

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        calls.append((list(command), dict(extra_env) if extra_env else None))
        if "--resume-conversation" not in command:
            assert callable(on_lifecycle_event)
            assert isinstance(extra_env, dict)
            handoff_path = Path(extra_env["CWA_WK_RESUME_HANDOFF_FILE"])
            assert handoff_path.exists()
            assert handoff_path.stat().st_mode & 0o777 == 0o600
            handoff_path.write_text("resume-secret", encoding="utf-8")
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
            }

        assert extra_env == {"CWA_WK_RESUME_VALUE": "resume-secret"}
        assert "resume-secret" not in command
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
    first_command, first_env = calls[0]
    assert isinstance(first_env, dict)
    assert set(first_env) == {"CWA_WK_RESUME_HANDOFF_FILE"}
    assert not Path(first_env["CWA_WK_RESUME_HANDOFF_FILE"]).exists()
    assert "--stream-probe-until-resume-token" in first_command
    assert "--stream-probe-until-end" not in first_command
    second_command, second_env = calls[1]
    assert second_env == {"CWA_WK_RESUME_VALUE": "resume-secret"}
    assert second_command[second_command.index("--resume-offset") + 1] == "0"
    assert "--resume-value" not in second_command

    def fail_if_helper_runs(*args, **kwargs):
        raise AssertionError("cached final canonical payload must avoid another helper read")

    monkeypatch.setattr(provider, "_run_helper", fail_if_helper_runs)
    assert provider.read_conversation_payload("conversation-1", timeout=5) == final_canonical


def test_wkwebview_streaming_without_resume_keeps_heavy_page_until_final_canonical(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))

    def fail_if_duplicate_commit_check_runs(**kwargs):
        raise AssertionError("final canonical helper proof must avoid duplicate polling")

    monkeypatch.setattr(
        provider,
        "_wait_for_canonical_write_commit",
        fail_if_duplicate_commit_check_runs,
    )
    final_canonical = {
        "current_node": "node-final",
        "mapping": {"node-final": {"id": "node-final"}},
    }
    calls: list[list[str]] = []
    handoff_paths: list[Path] = []

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        calls.append(list(command))
        assert "--resume-conversation" not in command
        assert isinstance(extra_env, dict)
        handoff_path = Path(extra_env["CWA_WK_RESUME_HANDOFF_FILE"])
        handoff_paths.append(handoff_path)
        assert handoff_path.exists()
        assert handoff_path.stat().st_mode & 0o777 == 0o600
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
    assert len(handoff_paths) == 1
    assert not handoff_paths[0].exists()
    assert result.passive_observer_armed is False
    assert events[0]["text"] == "done"

    def fail_if_helper_runs(*args, **kwargs):
        raise AssertionError("cached final canonical payload must avoid another helper read")

    monkeypatch.setattr(provider, "_run_helper", fail_if_helper_runs)
    assert provider.read_conversation_payload("conversation-1", timeout=5) == final_canonical


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
        raise AssertionError("worker stopped-final proof must avoid fallback canonical reads")

    monkeypatch.setattr(provider, "_wait_for_canonical_stop_proof", fail_if_fallback_reads)

    result = provider.stop_generation("conversation-1", timeout=5)

    assert result["stopped"] is True
    assert result["conversationId"] == "conversation-1"
    assert provider.stop_requested_for("conversation-1") is True
