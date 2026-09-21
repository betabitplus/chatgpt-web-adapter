from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import queue
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import chatgpt_web_adapter.browser_authority_backend as browser_backend
import chatgpt_web_adapter.wkwebview_turn_broker as turn_broker
from chatgpt_web_adapter.browser_authority_backend import (
    CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND,
    WKWEBVIEW_BROWSER_AUTHORITY_BACKEND,
    normalize_browser_authority_backend,
)
from chatgpt_web_adapter.browser_native_client import (
    _canonical_intermediate_events,
    _canonical_prewrite_snapshot,
)
from chatgpt_web_adapter.browser_owned_write_runtime import _canonical_commit_snapshot
from chatgpt_web_adapter.client import ChatGPTWebClient
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
from chatgpt_web_adapter.wkwebview_turn_orchestrator import WKTurnOrchestrator


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


def test_wkwebview_probe_stream_status_is_one_shot_and_fail_soft(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    calls = []

    class Transport:
        def read_stream_status(self, conversation_id, *, turn_trace_id, timeout):
            calls.append((conversation_id, turn_trace_id, timeout))
            return "complete"

    provider._lightweight_transport = Transport()
    monkeypatch.setattr(provider, "_lightweight_path_enabled", lambda: True)

    assert (
        provider.probe_stream_status(
            "conversation-1",
            turn_trace_id="turn-current",
            timeout=1.25,
        )
        == "COMPLETE"
    )
    assert calls == [("conversation-1", "turn-current", 1.25)]

    def failed_status(*_args, **_kwargs):
        raise RequestError("STREAM_STATUS_FAILED", request_stage="test")

    provider._lightweight_transport.read_stream_status = failed_status
    assert provider.probe_stream_status("conversation-1", timeout=1.25) is None


def test_wkwebview_completed_turn_confirmation_rejects_stale_or_nonfinal() -> None:
    provider = WKWebViewTurnProvider()
    stale = _final_canonical_for_prompt("previous")
    current = _final_canonical_for_prompt("current")
    nonfinal = _final_canonical_for_prompt("current")
    nonfinal["mapping"]["node-final"]["message"]["status"] = "in_progress"
    nonfinal["mapping"]["node-final"]["message"]["end_turn"] = False

    assert (
        provider._canonical_confirms_completed_turn(
            stale,
            text="current",
            baseline_current_node="node-before",
        )
        is False
    )
    assert (
        provider._canonical_confirms_completed_turn(
            current,
            text="current",
            baseline_current_node="node-before",
        )
        is True
    )
    assert (
        provider._canonical_confirms_completed_turn(
            current,
            text="current",
            baseline_current_node="node-final",
        )
        is False
    )
    assert (
        provider._canonical_confirms_completed_turn(
            nonfinal,
            text="current",
            baseline_current_node="node-before",
        )
        is False
    )


def test_wkwebview_completion_push_candidate_distinguishes_stale_from_lagging() -> None:
    provider = WKWebViewTurnProvider()
    stale = _final_canonical_for_prompt("previous")
    stale["current_node"] = "node-before"

    pending = _final_canonical_for_prompt("current")
    pending["mapping"]["node-final"]["message"]["status"] = "in_progress"
    pending["mapping"]["node-final"]["message"]["end_turn"] = False

    complete = _final_canonical_for_prompt("current")

    assert (
        provider._classify_completion_push_candidate(
            stale,
            text="current",
            baseline_current_node="node-before",
        )
        == "stale"
    )
    assert (
        provider._classify_completion_push_candidate(
            pending,
            text="current",
            baseline_current_node="node-before",
        )
        == "pending"
    )
    assert (
        provider._classify_completion_push_candidate(
            complete,
            text="current",
            baseline_current_node="node-before",
        )
        == "complete"
    )


def test_wk_shared_final_wait_survives_registry_cleanup_race(
    monkeypatch, tmp_path
) -> None:
    provider = WKWebViewTurnProvider(state_dir=tmp_path)
    payload = _final_canonical_for_prompt("shared-final")
    payload["mapping"]["node-final"]["message"]["id"] = "assistant-final"

    reads = 0

    def read_cached(_conversation_id):
        nonlocal reads
        reads += 1
        if reads < 3:
            return None
        return payload, 0.0

    monkeypatch.setattr(provider, "read_cached_conversation_payload", read_cached)
    monkeypatch.setattr(provider, "active_stream_info", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "chatgpt_web_adapter.browser_native_client._canonical_stream_identity",
        lambda _payload: ("conversation-turn-final", "turn-final"),
    )

    result = provider.wait_for_shared_final_payload(
        "conversation-1",
        topic_id="conversation-turn-final",
        timeout=0.5,
    )

    assert result is payload
    assert reads >= 3


def test_wk_commit_snapshot_refreshes_full_prewrite_and_reuses_it_for_prepare(
    monkeypatch, tmp_path
) -> None:
    provider = WKWebViewTurnProvider(state_dir=tmp_path)

    stale = _final_canonical_for_prompt("stale")
    stale["mapping"]["node-final"]["message"]["id"] = "assistant-stale"
    provider._canonical_state.cache_final_payload("conversation-1", stale)

    fresh = _final_canonical_for_prompt("fresh")
    fresh["default_model_slug"] = "gpt-5-6-thinking"
    fresh["mapping"]["node-final"]["message"]["id"] = "assistant-fresh"
    fresh["mapping"]["node-final"]["message"]["metadata"] = {
        "thinking_effort": "extended"
    }

    calls = 0

    def network_read(*args, **kwargs):
        nonlocal calls
        calls += 1
        return fresh

    monkeypatch.setattr(provider, "_read_conversation_payload_uncached", network_read)
    client = WKWebViewCanonicalClient(SimpleNamespace(), provider)

    status, commit_payload, _checked_at = _canonical_commit_snapshot(
        client, "conversation-1"
    )

    assert calls == 1
    assert status == "completed"
    assert commit_payload is fresh
    assert commit_payload["mapping"]["node-final"]["message"]["id"] == "assistant-fresh"

    # The same full snapshot is handed to prepare_turn from the one-shot
    # in-memory handoff; no second canonical GET is spent.
    prewrite = provider.peek_prewrite_payload("conversation-1")
    assert prewrite == fresh

    prepared = provider._turn_orchestrator.prepare_turn(
        conversation="conversation-1",
        total_timeout=30,
        attachment_paths=None,
        model_slug=None,
        streaming=True,
    )

    assert calls == 1
    assert prepared.baseline_current_node == "node-final"
    assert prepared.minimal_parent_message_id == "assistant-fresh"
    assert prepared.minimal_model_slug == "gpt-5-6-thinking"
    assert prepared.minimal_thinking_effort == "extended"


def test_wk_regular_final_read_keeps_cursor_without_prewrite_handoff(
    monkeypatch, tmp_path
) -> None:
    provider = WKWebViewTurnProvider(state_dir=tmp_path)
    payload = _final_canonical_for_prompt("fresh")
    payload["default_model_slug"] = "gpt-5-6-thinking"
    payload["mapping"]["node-final"]["message"]["id"] = "assistant-fresh"

    calls = 0

    def network_read(*args, **kwargs):
        nonlocal calls
        calls += 1
        return payload

    monkeypatch.setattr(provider, "_read_conversation_payload_uncached", network_read)

    assert provider.read_conversation_payload("conversation-1", timeout=5) is payload
    assert calls == 1
    prewrite = provider.peek_prewrite_payload("conversation-1")
    assert isinstance(prewrite, dict)
    assert prewrite["current_node"] == "assistant-fresh"
    assert provider.take_prewrite_payload("conversation-1") is None


def test_full_prewrite_baseline_does_not_replay_historical_tool_events() -> None:
    full_prewrite = {
        "current_node": "old-final-node",
        "mapping": {
            "old-user-node": {
                "parent": None,
                "message": {
                    "id": "old-user",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["old prompt"]},
                },
            },
            "old-tool-call-node": {
                "parent": "old-user-node",
                "message": {
                    "id": "old-tool-call",
                    "author": {"role": "assistant"},
                    "recipient": "api_tool.call_tool",
                    "status": "finished_successfully",
                    "content": {
                        "content_type": "text",
                        "parts": ['{"action":"search","args":{"query":"old"}}'],
                    },
                },
            },
            "old-tool-result-node": {
                "parent": "old-tool-call-node",
                "message": {
                    "id": "old-tool-result",
                    "author": {"role": "tool", "name": "api_tool.call_tool"},
                    "recipient": "all",
                    "status": "finished_successfully",
                    "content": {"content_type": "text", "parts": ['{"ok":true}']},
                },
            },
            "old-final-node": {
                "parent": "old-tool-result-node",
                "message": {
                    "id": "old-final",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "status": "finished_successfully",
                    "end_turn": True,
                    "content": {"content_type": "text", "parts": ["old answer"]},
                },
            },
        },
    }
    cursor_only = {
        "current_node": "old-final",
        "mapping": {
            "old-final": {
                "parent": None,
                "message": full_prewrite["mapping"]["old-final-node"]["message"],
            }
        },
    }

    class Client:
        read_calls = 0
        peek_calls = 0

        def read_prewrite_canonical_payload(self, conversation_id):
            assert conversation_id == "conversation-1"
            self.read_calls += 1
            return full_prewrite

        def peek_prewrite_canonical_payload(self, conversation_id):
            assert conversation_id == "conversation-1"
            self.peek_calls += 1
            return cursor_only

    client = Client()
    status, commit_payload, _checked_at = _canonical_commit_snapshot(
        client, "conversation-1"
    )

    assert status == "completed"
    assert commit_payload is full_prewrite
    assert client.read_calls == 1
    assert client.peek_calls == 0

    baseline_message_ids, _assistant_ids, _status = _canonical_prewrite_snapshot(
        SimpleNamespace(),
        "conversation-1",
        canonical_payload=commit_payload,
    )
    assert {
        "old-user",
        "old-tool-call",
        "old-tool-result",
        "old-final",
    } <= baseline_message_ids

    after_submit = copy.deepcopy(full_prewrite)
    after_submit["mapping"].update(
        {
            "new-user-node": {
                "parent": "old-final-node",
                "message": {
                    "id": "new-user",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["new prompt"]},
                },
            },
            "new-tool-call-node": {
                "parent": "new-user-node",
                "message": {
                    "id": "new-tool-call",
                    "author": {"role": "assistant"},
                    "recipient": "api_tool.call_tool",
                    "status": "finished_successfully",
                    "content": {
                        "content_type": "text",
                        "parts": ['{"action":"edit","args":{"path":"new"}}'],
                    },
                },
            },
            "new-final-node": {
                "parent": "new-tool-call-node",
                "message": {
                    "id": "new-final",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "status": "finished_successfully",
                    "end_turn": True,
                    "content": {"content_type": "text", "parts": ["new answer"]},
                },
            },
        }
    )
    after_submit["current_node"] = "new-final-node"

    events = _canonical_intermediate_events(
        after_submit,
        baseline_message_ids=baseline_message_ids,
        emitted_message_ids=set(baseline_message_ids),
        submission_id="submission-1",
    )

    assert [event["message_id"] for event in events] == ["new-tool-call"]


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


def test_wkwebview_provider_persists_and_reads_canonical_cache(tmp_path) -> None:
    provider = WKWebViewTurnProvider(state_dir=tmp_path)
    payload = {
        "conversation_id": "conversation-cache",
        "current_node": "node-1",
        "mapping": {"node-1": {}},
    }

    provider._persist_canonical_payload(
        "conversation-cache",
        payload,
        min_interval_seconds=0,
    )

    cached = provider.read_cached_conversation_payload("conversation-cache")

    assert cached is not None
    cached_payload, age = cached
    assert cached_payload == payload
    assert age >= 0
    path = provider._canonical_cache_path("conversation-cache")
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600


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
        return {"current_node": "node-curl", "mapping": {"node-curl": {}}}, None

    monkeypatch.setattr(
        provider, "_read_conversation_payload_via_coordinated_curl", fake_curl_read
    )

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
        "_read_conversation_payload_via_coordinated_curl",
        lambda conversation_id, *, timeout: (None, None),
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


def test_wkwebview_canonical_fallback_records_reason(monkeypatch, tmp_path) -> None:
    provider = WKWebViewTurnProvider(state_dir=tmp_path)
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


def test_wkwebview_canonical_429_waits_and_retries_without_helper(
    monkeypatch, tmp_path
) -> None:
    provider = WKWebViewTurnProvider(state_dir=tmp_path)
    provider._canonical_read_min_spacing_seconds = 0.0
    provider._canonical_rate_limit_initial_backoff_seconds = 0.001
    provider._canonical_rate_limit_max_backoff_seconds = 0.001

    class _RateLimitedThenSuccess:
        def __init__(self) -> None:
            self.calls = 0
            self.reason: str | None = None

        def read_canonical(self, conversation_id: str, *, timeout: float):
            self.calls += 1
            if self.calls == 1:
                self.reason = "WKWEBVIEW_CURL_CANONICAL_HTTP:HTTP_429"
                return None
            self.reason = None
            return {"current_node": "node-ok", "mapping": {"node-ok": {}}}

        def take_canonical_fallback_reason(self):
            reason = self.reason
            self.reason = None
            return reason

    transport = _RateLimitedThenSuccess()
    provider._lightweight_transport = transport
    monkeypatch.setattr(
        provider,
        "_ensure_helper",
        lambda: (_ for _ in ()).throw(
            AssertionError("429 retry must not fall back to WK helper")
        ),
    )

    payload = provider.read_conversation_payload("conversation-429", timeout=1)

    assert payload["current_node"] == "node-ok"
    assert transport.calls == 2
    assert provider._canonical_read_observation() == ("curl_cffi", None)


def test_wkwebview_canonical_persistent_429_never_falls_back_to_helper(
    monkeypatch, tmp_path
) -> None:
    provider = WKWebViewTurnProvider(state_dir=tmp_path)
    provider._canonical_read_min_spacing_seconds = 0.0
    provider._canonical_rate_limit_initial_backoff_seconds = 0.05
    provider._canonical_rate_limit_max_backoff_seconds = 0.05

    class _AlwaysRateLimited:
        def __init__(self) -> None:
            self.calls = 0

        def read_canonical(self, conversation_id: str, *, timeout: float):
            self.calls += 1
            return None

        def take_canonical_fallback_reason(self):
            return "WKWEBVIEW_CURL_CANONICAL_HTTP:HTTP_429"

    transport = _AlwaysRateLimited()
    provider._lightweight_transport = transport
    helper_calls: list[str] = []

    def fail_helper():
        helper_calls.append("helper")
        raise AssertionError("persistent 429 must not launch WK helper")

    monkeypatch.setattr(provider, "_ensure_helper", fail_helper)

    with pytest.raises(RequestError, match="WKWEBVIEW_CANONICAL_RATE_LIMITED") as info:
        provider.read_conversation_payload("conversation-429", timeout=0.02)

    assert info.value.status_code == 429
    assert transport.calls == 1
    assert helper_calls == []


def test_wkwebview_canonical_gate_state_is_shared_between_provider_instances(
    tmp_path,
) -> None:
    first = WKWebViewTurnProvider(state_dir=tmp_path)
    second = WKWebViewTurnProvider(state_dir=tmp_path)
    first._canonical_read_min_spacing_seconds = 0.03
    second._canonical_read_min_spacing_seconds = 0.03

    class _SuccessTransport:
        def read_canonical(self, conversation_id: str, *, timeout: float):
            return {"current_node": conversation_id, "mapping": {}}

        def take_canonical_fallback_reason(self):
            return None

    first._lightweight_transport = _SuccessTransport()
    second._lightweight_transport = _SuccessTransport()

    first.read_conversation_payload("conversation-a", timeout=1)
    started = time.monotonic()
    second.read_conversation_payload("conversation-b", timeout=1)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.02


def test_wkwebview_active_stream_registry_lifecycle_preserves_baseline(
    tmp_path,
) -> None:
    provider = WKWebViewTurnProvider(state_dir=tmp_path)
    provider._canonical_state.set_current_node("conversation-1", "node-before")

    provider.begin_active_turn("conversation-1")
    pending = provider.active_stream_info("conversation-1")

    assert pending is not None
    assert pending["state"] == "pending"
    assert pending["topic_id"] is None
    assert pending["baseline_current_node"] == "node-before"

    provider._register_active_stream(
        "conversation-1",
        "conversation-turn-1",
        turn_exchange_id="turn-1",
    )
    streaming = provider.active_stream_info("conversation-1")

    assert streaming is not None
    assert streaming["state"] == "streaming"
    assert streaming["topic_id"] == "conversation-turn-1"
    assert streaming["turn_exchange_id"] == "turn-1"
    assert streaming["baseline_current_node"] == "node-before"

    provider.end_active_turn("conversation-1")
    assert provider.active_stream_info("conversation-1") is None
    assert not provider._active_stream_path("conversation-1").exists()


def test_wkwebview_active_stream_registry_prunes_dead_pid(
    monkeypatch, tmp_path
) -> None:
    provider = WKWebViewTurnProvider(state_dir=tmp_path)
    provider._register_active_stream("conversation-1", None)
    path = provider._active_stream_path("conversation-1")
    assert path.exists()

    monkeypatch.setattr(provider, "_process_is_alive", lambda _pid: False)

    assert provider.active_stream_info("conversation-1") is None
    assert not path.exists()


def test_wkwebview_ws_transport_uses_short_close_timeout(monkeypatch) -> None:
    client = object.__new__(ChatGPTWebClient)
    connect_kwargs: dict[str, Any] = {}
    sent: list[str] = []

    class FakeWebSocket:
        async def send(self, raw: str) -> None:
            sent.append(raw)

    class FakeConnect:
        async def __aenter__(self):
            return FakeWebSocket()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_connect(*args, **kwargs):
        connect_kwargs.update(kwargs)
        return FakeConnect()

    import websockets

    monkeypatch.setattr(websockets, "connect", fake_connect)
    monkeypatch.setattr(
        client,
        "_probe_celsius_ws_user",
        lambda: {"websocket_url": "wss://example.invalid/celsius"},
    )
    monkeypatch.setattr(client, "_build_headers", lambda extra=None: {})
    monkeypatch.setattr(
        client, "_capture_ws_url_diagnostics", lambda websocket_url, state: None
    )

    asyncio.run(
        client._stream_handoff_via_ws_topic_async(
            "conversation-turn-1",
            state={},
            on_event=None,
            on_token=None,
            cancel_check=lambda: True,
            stop_on_done=False,
        )
    )

    assert len(sent) == 1
    assert connect_kwargs["close_timeout"] == 0.25


def test_wkwebview_ws_transport_resumes_from_last_processed_offset(monkeypatch) -> None:
    client = object.__new__(ChatGPTWebClient)
    client.timeout = 30.0
    sent: list[str] = []
    stopped = False

    class FakeWebSocket:
        async def send(self, raw: str) -> None:
            sent.append(raw)

        async def recv(self) -> str:
            return json.dumps(
                {
                    "id": 2,
                    "reply": {
                        "recovered": True,
                        "catchups": [
                            {
                                "type": "message",
                                "topic_id": "conversation-turn-1",
                                "offset": "offset-new",
                                "payload": {"type": "ignored"},
                            }
                        ],
                        "last_offset": "offset-new",
                    },
                }
            )

    class FakeConnect:
        async def __aenter__(self):
            return FakeWebSocket()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    import websockets

    monkeypatch.setattr(websockets, "connect", lambda *args, **kwargs: FakeConnect())
    monkeypatch.setattr(
        client,
        "_probe_celsius_ws_user",
        lambda: {"websocket_url": "wss://example.invalid/celsius"},
    )
    monkeypatch.setattr(client, "_build_headers", lambda extra=None: {})
    monkeypatch.setattr(
        client, "_capture_ws_url_diagnostics", lambda websocket_url, state: None
    )

    state = {"resume_ws_offset": "offset-old"}

    def on_event(event: dict[str, Any]) -> None:
        nonlocal stopped
        if event.get("type") == "stream_handoff_ws_subscribed":
            stopped = True

    asyncio.run(
        client._stream_handoff_via_ws_topic_async(
            "conversation-turn-1",
            state=state,
            on_event=on_event,
            on_token=None,
            cancel_check=lambda: stopped,
            stop_on_done=False,
        )
    )

    commands = json.loads(sent[0])
    assert commands[1]["command"]["offset"] == "offset-old"
    assert state["resume_ws_offset"] == "offset-new"


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


def test_wkwebview_streaming_without_resume_uses_stream_terminal_proof(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))

    def fail_if_canonical_commit_check_runs(**kwargs):
        raise AssertionError(
            "healthy streaming terminal proof must not start canonical commit polling"
        )

    monkeypatch.setattr(
        provider,
        "_wait_for_canonical_write_commit",
        fail_if_canonical_commit_check_runs,
    )
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
            "write_commit_proof": "STREAM_TERMINAL",
            "canonical_committed": False,
            "canonical_final_completed": False,
            "canonical_body_base64": "",
            "committed_current_node": "",
            "stream_ended": False,
            "stream_terminal_observed": True,
            "stream_resume_present": False,
            "stream_resume_handoff_written": False,
        }

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    events: list[dict] = []

    result = provider.send_text_streaming("hello", on_text_event=events.append)

    assert len(calls) == 1
    assert result.passive_observer_armed is False
    assert events[0]["text"] == "done"


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


def test_wkwebview_continuation_reports_guard_unavailable_instead_of_none_crash(
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
    guard_reads = 0

    def missing_guard(
        _conversation_id: str,
        *,
        timeout: float,
        coordinated: bool = True,
    ):
        nonlocal guard_reads
        guard_reads += 1
        assert coordinated is True
        return None

    monkeypatch.setattr(
        provider,
        "_read_conversation_payload_uncached",
        missing_guard,
    )
    stream_calls = 0

    def fake_stream(command, **kwargs):
        nonlocal stream_calls
        stream_calls += 1
        raise RequestError(
            "WKWEBVIEW_PRE_SUBMIT_TIMEOUT",
            request_stage="wkwebview_authority_turn",
        )

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)

    with pytest.raises(
        RequestError,
        match="WKWEBVIEW_PRE_SUBMIT_GUARD_UNAVAILABLE",
    ):
        provider.send_text_streaming(
            "continue",
            conversation="conversation-1",
            model_slug="model-1",
            on_text_event=lambda _event: None,
        )

    assert stream_calls == 1
    assert guard_reads == 1


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


def test_wkwebview_stop_accepts_terminal_stream_status_when_stop_control_is_gone(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    monkeypatch.setattr(
        provider,
        "_run_helper",
        lambda command, *, timeout: (_ for _ in ()).throw(
            RequestError(
                "WKWEBVIEW_STOP_CONTROL_NOT_FOUND",
                request_stage="wkwebview_stop_generation",
            )
        ),
    )
    monkeypatch.setattr(
        provider,
        "_wait_for_stopped_final_payload",
        lambda conversation_id, *, timeout: None,
    )
    observed: list[tuple[str, str | None, float]] = []

    def stream_status(conversation_id, *, turn_trace_id, timeout):
        observed.append((conversation_id, turn_trace_id, timeout))
        return "COMPLETE"

    monkeypatch.setattr(provider, "_wait_for_stream_stop_proof", stream_status)

    result = provider.stop_generation("conversation-1", timeout=5)

    assert result["stopped"] is True
    assert result["proof"] == "stream_status_after_missing_control"
    assert result["streamStatus"] == "COMPLETE"
    assert observed
    assert provider.stop_requested_for("conversation-1") is True


def test_wkwebview_stop_missing_control_still_fails_without_independent_proof(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    monkeypatch.setattr(
        provider,
        "_run_helper",
        lambda command, *, timeout: (_ for _ in ()).throw(
            RequestError(
                "WKWEBVIEW_STOP_CONTROL_NOT_FOUND",
                request_stage="wkwebview_stop_generation",
            )
        ),
    )
    monkeypatch.setattr(
        provider,
        "_wait_for_stopped_final_payload",
        lambda conversation_id, *, timeout: None,
    )
    monkeypatch.setattr(
        provider,
        "_wait_for_stream_stop_proof",
        lambda conversation_id, *, turn_trace_id, timeout: None,
    )

    with pytest.raises(RequestError, match="WKWEBVIEW_STOP_CONTROL_NOT_FOUND"):
        provider.stop_generation("conversation-1", timeout=5)

    assert provider.stop_requested_for("conversation-1") is False


def test_wkwebview_stop_accepts_product_stop_control_click_without_canonical_polling(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    monkeypatch.setattr(
        provider,
        "_run_helper",
        lambda command, *, timeout: {
            "ok": True,
            "stop_requested": True,
            "stop_control_clicked": True,
            "conversation_id": "conversation-1",
        },
    )
    monkeypatch.setattr(
        provider,
        "_wait_for_stopped_final_payload",
        lambda conversation_id, *, timeout: None,
    )

    def fail_if_network_stop_proof_runs(*args, **kwargs):
        raise AssertionError("product Stop control click is already explicit stop proof")

    monkeypatch.setattr(provider, "_wait_for_stream_stop_proof", fail_if_network_stop_proof_runs)
    monkeypatch.setattr(provider, "_wait_for_canonical_stop_proof", fail_if_network_stop_proof_runs)

    result = provider.stop_generation("conversation-1", timeout=5)

    assert result["stopped"] is True
    assert result["proof"] == "browser_stop_control"
    assert provider.stop_requested_for("conversation-1") is True


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
    monkeypatch.delenv("CWA_WK_PROXY_PROTECTED_WRITE", raising=False)
    provider._lightweight_transport = SimpleNamespace(
        source_client=SimpleNamespace(
            auth=SimpleNamespace(cookies={"session": "test-cookie"})
        )
    )
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
    assert requests[0]["proxy_protected_write"] is True
    assert requests[0]["proxy_cookie_header"] == "session=test-cookie"
    assert requests[1]["proxy_protected_write"] is True
    assert requests[1]["proxy_cookie_header"] == "session=test-cookie"
    assert "minimal_model_slug" not in requests[0]
    assert requests[1]["minimal_model_slug"] == "gpt-5-6-thinking"


def test_wkwebview_minimal_security_shell_continuation_uses_canonical_parent(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.delenv("CWA_WK_PROXY_PROTECTED_WRITE", raising=False)
    provider._lightweight_transport = SimpleNamespace(
        source_client=SimpleNamespace(
            auth=SimpleNamespace(cookies={"session": "test-cookie"})
        ),
        arm_conversation_completion=lambda conversation_id, timeout: None,
    )
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
    assert request["proxy_protected_write"] is True
    assert request["proxy_cookie_header"] == "session=test-cookie"
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
    assert result.stream_finality_proven is True


def test_wkwebview_minimal_completion_verification_waits_for_post_submit_grace(
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

    clock = {"now": 100.0}
    completion = {"sequence": 10}
    reads = {"count": 0}
    provider._lightweight_transport = SimpleNamespace(
        source_client=SimpleNamespace(
            auth=SimpleNamespace(cookies={"session": "test-cookie"})
        ),
        arm_conversation_completion=lambda conversation_id, timeout: 10,
        current_completion_sequence=lambda: completion["sequence"],
        conversation_completion_sequence=lambda conversation_id: completion["sequence"],
    )
    monkeypatch.setattr(
        "chatgpt_web_adapter.wkwebview_provider.time.monotonic",
        lambda: clock["now"],
    )

    canonical = _final_canonical_for_prompt("continue")

    def read_once(conversation_id, *, timeout):
        reads["count"] += 1
        return canonical, None

    monkeypatch.setattr(
        provider, "_read_conversation_payload_via_coordinated_curl", read_once
    )

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        submit_started = provider._authority_context.on_submit_started
        completion_check = provider._authority_context.external_completion_check
        assert callable(submit_started)
        assert callable(completion_check)
        submit_started()
        completion["sequence"] = 11

        clock["now"] = 105.0
        assert completion_check() is False
        assert reads["count"] == 0

        clock["now"] = 119.9
        assert completion_check() is False
        assert reads["count"] == 0

        clock["now"] = 120.6
        assert completion_check() is True
        assert reads["count"] == 1

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

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    monkeypatch.setattr(
        provider,
        "_resume_via_curl_ws_second_leg",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("terminal Phase A must not start a resume second leg")
        ),
    )

    with provider.require_profile("FAST"):
        result = provider.send_text_streaming(
            "continue",
            conversation="conversation-1",
            on_text_event=lambda event: None,
        )

    assert result.stream_finality_proven is True
    assert reads["count"] == 1


def test_wkwebview_minimal_completion_verifies_each_passive_sequence_once(
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

    clock = {"now": 100.0}
    completion = {"sequence": 10}
    reads = {"count": 0}
    provider._lightweight_transport = SimpleNamespace(
        source_client=SimpleNamespace(
            auth=SimpleNamespace(cookies={"session": "test-cookie"})
        ),
        arm_conversation_completion=lambda conversation_id, timeout: 10,
        current_completion_sequence=lambda: completion["sequence"],
        conversation_completion_sequence=lambda conversation_id: completion["sequence"],
    )
    monkeypatch.setattr(
        "chatgpt_web_adapter.wkwebview_provider.time.monotonic",
        lambda: clock["now"],
    )

    pending = _final_canonical_for_prompt("continue")
    pending["mapping"]["node-final"]["message"]["status"] = "in_progress"
    pending["mapping"]["node-final"]["message"]["end_turn"] = False

    def read_pending(conversation_id, *, timeout):
        reads["count"] += 1
        return pending, None

    monkeypatch.setattr(
        provider, "_read_conversation_payload_via_coordinated_curl", read_pending
    )

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        extra_env=None,
    ):
        submit_started = provider._authority_context.on_submit_started
        completion_check = provider._authority_context.external_completion_check
        assert callable(submit_started)
        assert callable(completion_check)
        submit_started()

        completion["sequence"] = 11
        clock["now"] = 120.0
        assert completion_check() is False
        clock["now"] = 122.1
        assert completion_check() is False
        assert reads["count"] == 1

        for now in (124.5, 130.0, 145.0):
            clock["now"] = now
            assert completion_check() is False
            assert reads["count"] == 1

        completion["sequence"] = 12
        clock["now"] = 146.0
        assert completion_check() is False
        clock["now"] = 148.1
        assert completion_check() is False
        assert reads["count"] == 2

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

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    monkeypatch.setattr(
        provider,
        "_resume_via_curl_ws_second_leg",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("terminal Phase A must not start a resume second leg")
        ),
    )

    with provider.require_profile("FAST"):
        result = provider.send_text_streaming(
            "continue",
            conversation="conversation-1",
            on_text_event=lambda event: None,
        )

    assert result.stream_finality_proven is True
    assert reads["count"] == 2


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
        }
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
        source_client=SimpleNamespace(
            auth=SimpleNamespace(cookies={"session": "test-cookie"})
        ),
        read_catalog=lambda *args, **kwargs: {
            "items": [{"id": "conversation-unreadable"}],
            "total": 1,
        }
    )
    monkeypatch.setattr(
        provider,
        "_read_conversation_payload_via_coordinated_curl",
        lambda *args, **kwargs: (None, None),
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


def test_wkwebview_continuation_uses_direct_topic_handoff_without_resume_token(
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

    direct_calls: list[dict] = []

    def fake_stream(
        command,
        *,
        timeout,
        on_text_event,
        on_lifecycle_event=None,
        on_transport_event=None,
        extra_env=None,
    ):
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
            "stream_resume_present": False,
            "stream_resume_handoff_written": True,
            "stream_topic_id": "conversation-turn-topic-only",
            "turn_exchange_id": "turn-topic-only",
            "stream_conversation_id": "conversation-1",
        }

    def fake_direct_topic(**kwargs):
        direct_calls.append(kwargs)
        return {
            "ok": True,
            "conversation_id": "conversation-1",
            "message_id": "assistant-message-after",
            "segment_done_count": 1,
            "observed_model": "gpt-5-6-thinking",
            "observed_reasoning_effort": "extended",
        }

    def fail_resume_token_path(**kwargs):
        raise AssertionError(
            "resume-token second leg must not run for direct topic handoff"
        )

    monkeypatch.setattr(provider, "_run_helper_streaming", fake_stream)
    monkeypatch.setattr(
        provider, "_resume_via_curl_ws_topic_second_leg", fake_direct_topic
    )
    monkeypatch.setattr(
        provider, "_resume_via_curl_ws_second_leg", fail_resume_token_path
    )

    result = provider.send_text_streaming(
        "continue",
        conversation="conversation-1",
        on_text_event=lambda event: None,
        on_transport_event=lambda event: None,
    )

    assert result.conversation_id == "conversation-1"
    assert result.phase_b_transport == "curl_cffi_websocket_topic_handoff"
    assert len(direct_calls) == 1
    assert direct_calls[0]["topic_id"] == "conversation-turn-topic-only"
    assert direct_calls[0]["turn_exchange_id"] == "turn-topic-only"
    next_prewrite = provider.peek_prewrite_payload("conversation-1")
    assert isinstance(next_prewrite, dict)
    assert next_prewrite["current_node"] == "assistant-message-after"
    next_message = next_prewrite["mapping"]["assistant-message-after"]["message"]
    assert next_message["id"] == "assistant-message-after"
    assert next_prewrite["default_model_slug"] == "gpt-5-6-thinking"
    assert next_message["metadata"]["thinking_effort"] == "extended"


def test_wkwebview_minimal_security_shell_uploads_attachments_before_wk(
    monkeypatch, tmp_path
) -> None:
    provider = WKWebViewTurnProvider()
    monkeypatch.delenv("CWA_WK_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(provider, "_ensure_helper", lambda: Path("/tmp/wk-helper"))
    attachment = tmp_path / "red.png"
    attachment.write_bytes(b"fake-png")

    class FakeSourceClient:
        auth = SimpleNamespace(cookies={"session": "test-cookie"})

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


def test_minimal_security_shell_request_client_is_optional_for_continuation() -> None:
    source = (
        Path(__file__).parents[1]
        / "src"
        / "chatgpt_web_adapter"
        / "wkwebview_helper"
        / "minimal_security_shell.js"
    ).read_text(encoding="utf-8")

    shared_init = source[
        source.index("const loadSharedConversationInitialization = async () => {"):
        source.index("const loadSharedModelCatalog = async", source.index("const loadSharedConversationInitialization = async () => {"))
    ]
    assert "let officialApiClient = null;" in shared_init
    assert "const loadedApiClient = await loadOfficialApiClient();" in shared_init
    assert "catch (_)" in shared_init
    assert "officialApiClient = null;" in shared_init
    assert "officialConversationTransport: runtime.officialConversationTransport" in shared_init

    prepare_start = source.index("const prepareConversation = async ({")
    prepare_end = source.index("let lastBrokerResumeToken", prepare_start)
    prepare = source[prepare_start:prepare_end]
    fallback_start = prepare.index("const firstToken = await runPrepare({", prepare.index("if (officialApiClient)"))
    fallback = prepare[fallback_start:]
    second_start = fallback.index("const secondPromise = runPrepare({")
    submit_ready = fallback.index('if (typeof onSubmitReady === "function") onSubmitReady();')
    await_second = fallback.index("const secondToken = await secondPromise;")
    assert second_start < submit_ready < await_second

    entrypoint = source.rsplit("  (async () => {", 1)[1]
    assert "onSubmitReady: conversationId && !temporary ? resolveSubmitReady : null" in entrypoint


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


def test_wkwebview_identity_recovery_switches_to_passive_topic_after_one_read(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    provider._lightweight_transport = SimpleNamespace()
    partial_payload = {
        "current_node": "user-current",
        "mapping": {
            "node-before": {
                "id": "node-before",
                "parent": None,
                "children": ["user-current"],
                "message": {
                    "id": "assistant-before",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["before"]},
                    "metadata": {},
                    "status": "finished_successfully",
                    "end_turn": True,
                },
            },
            "user-current": {
                "id": "user-current",
                "parent": "node-before",
                "children": [],
                "message": {
                    "id": "client-message-1",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["recover live"]},
                    "metadata": {
                        "turn_exchange_id": "11111111-2222-3333-4444-555555555555"
                    },
                    "status": "finished_successfully",
                    "end_turn": None,
                },
            },
        },
    }
    reads = 0

    def read_once(*args, **kwargs):
        nonlocal reads
        reads += 1
        return partial_payload, None

    monkeypatch.setattr(
        provider,
        "_read_conversation_payload_via_coordinated_curl",
        read_once,
    )
    prepared = SimpleNamespace(
        conversation_id="conversation-1",
        baseline_current_node="node-before",
        minimal_model_slug=None,
        minimal_thinking_effort=None,
    )
    payload = provider._turn_orchestrator.recover_phase_one_identity(
        {
            "identity_recovery_required": True,
            "client_message_id": "client-message-1",
            "conversation_id": "conversation-1",
            "response_status": 200,
        },
        prepared=prepared,
        text="recover live",
        total_timeout=30,
        started=time.monotonic(),
    )

    assert reads == 1
    assert payload["stream_topic_id"] == (
        "conversation-turn-11111111-2222-3333-4444-555555555555"
    )
    assert payload["turn_exchange_id"] == "11111111-2222-3333-4444-555555555555"
    assert payload["_cwa_identity_recovery_kind"] == "stream_topic"
    assert payload["stream_terminal_observed"] is False

    followed = {}

    monkeypatch.setattr(provider, "_lightweight_path_enabled", lambda: True)

    def follow_topic(**kwargs):
        followed.update(kwargs)
        return {
            "stream_finality_proven": True,
            "message_id": "assistant-final",
            "finish_reason": "stop",
        }

    monkeypatch.setattr(provider, "_resume_via_curl_ws_topic_second_leg", follow_topic)
    monkeypatch.setattr(provider, "cache_continuation_cursor", lambda *args, **kwargs: None)

    passive = provider._turn_orchestrator.resume_after_phase_one(
        payload,
        result_conversation_id="conversation-1",
        phase_one_final_cached=False,
        prepared=prepared,
        text="recover live",
        total_timeout=30,
        started=time.monotonic(),
        streaming=True,
        make_stream_relay=lambda: (lambda _event: None),
    )

    assert passive is False
    assert followed["conversation_id"] == "conversation-1"
    assert followed["topic_id"] == (
        "conversation-turn-11111111-2222-3333-4444-555555555555"
    )
    assert followed["turn_exchange_id"] == "11111111-2222-3333-4444-555555555555"
    assert payload["_cwa_phase_b_transport"] == "curl_cffi_websocket_topic_handoff"


def test_wkwebview_verified_completion_reuses_canonical_without_second_read(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    final_payload = _final_canonical_for_prompt("verified final", assistant_text="done")
    provider._lightweight_transport = SimpleNamespace()

    def fail_read(*args, **kwargs):
        raise AssertionError("verified canonical must avoid a second network read")

    monkeypatch.setattr(
        provider,
        "_read_conversation_payload_via_coordinated_curl",
        fail_read,
    )

    result = provider._turn_orchestrator.recover_phase_one_identity(
        {
            "identity_recovery_required": True,
            "client_message_id": "",
            "conversation_id": "conversation-1",
            "response_status": 0,
        },
        prepared=SimpleNamespace(
            conversation_id="conversation-1",
            baseline_current_node="node-before",
        ),
        text="verified final",
        total_timeout=30,
        started=time.monotonic(),
        completion_watch_sequence=9,
        verified_canonical=final_payload,
    )

    assert result["conversation_id"] == "conversation-1"
    assert result["write_commit_proof"] == "PASSIVE_COMPLETION_PROMPT_RECOVERY"
    assert result["_cwa_identity_recovery_transport"] == (
        "conversations_ws_then_curl_cffi"
    )
    assert result["_cwa_identity_recovery_kind"] == "passive_completion"
    assert result["stream_terminal_observed"] is True
    assert result["_cwa_stream_finality_proven"] is True



def test_wkwebview_completion_check_captures_baseline_before_observer_start() -> None:
    provider = WKWebViewTurnProvider()
    current_sequence = 7
    observed_calls: list[tuple[str, int]] = []

    def sequence(conversation_id: str) -> int:
        assert conversation_id == "conversation-1"
        return current_sequence

    def ensure(*, timeout: float) -> bool:
        nonlocal current_sequence
        assert timeout == 12.0
        current_sequence = 8
        return True

    def observed(conversation_id: str, *, after_sequence: int) -> bool:
        observed_calls.append((conversation_id, after_sequence))
        return current_sequence > after_sequence

    provider._lightweight_transport = SimpleNamespace(
        ensure_conversation_completion_observer=ensure,
        conversation_completion_sequence=sequence,
        conversation_completion_observed=observed,
    )

    check = provider._conversation_completion_check(
        "conversation-1",
        timeout=30,
    )

    assert callable(check)
    assert check() is True
    assert observed_calls == [("conversation-1", 7)]


def test_wkwebview_known_continuation_waits_for_passive_completion_then_reads_once(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    final_payload = _final_canonical_for_prompt("push final", assistant_text="done")
    final_payload["mapping"]["user-final"]["message"]["id"] = "client-message-1"
    waits: list[dict[str, object]] = []

    def wait_for_completion(conversation_id, **kwargs):
        waits.append({"conversation_id": conversation_id, **kwargs})
        return True

    provider._lightweight_transport = SimpleNamespace(
        wait_for_conversation_completion=wait_for_completion
    )
    reads = 0

    def read_once(*args, **kwargs):
        nonlocal reads
        reads += 1
        return final_payload, None

    monkeypatch.setattr(
        provider,
        "_read_conversation_payload_via_coordinated_curl",
        read_once,
    )

    result = provider._turn_orchestrator.recover_phase_one_identity(
        {
            "identity_recovery_required": True,
            "client_message_id": "client-message-1",
            "conversation_id": "conversation-1",
            "response_status": 200,
        },
        prepared=SimpleNamespace(
            conversation_id="conversation-1",
            baseline_current_node="node-before",
        ),
        text="push final",
        total_timeout=30,
        started=time.monotonic(),
        completion_watch_sequence=7,
    )

    assert reads == 1
    assert len(waits) == 1
    assert waits[0]["conversation_id"] == "conversation-1"
    assert waits[0]["after_sequence"] == 7
    assert result["write_commit_proof"] == "PASSIVE_COMPLETION_MESSAGE_ID_RECOVERY"
    assert result["_cwa_identity_recovery_transport"] == (
        "conversations_ws_then_curl_cffi"
    )
    assert result["_cwa_identity_recovery_kind"] == "passive_completion"
    assert result["stream_terminal_observed"] is True


def test_wkwebview_known_continuation_recovers_without_client_message_id(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    final_payload = _final_canonical_for_prompt("queued", assistant_text="done")
    provider._lightweight_transport = SimpleNamespace()
    monkeypatch.setattr(
        provider,
        "_read_conversation_payload_via_coordinated_curl",
        lambda *args, **kwargs: (final_payload, None),
    )

    result = provider._turn_orchestrator.recover_phase_one_identity(
        {
            "identity_recovery_required": True,
            "client_message_id": "",
            "conversation_id": "conversation-1",
        },
        prepared=SimpleNamespace(
            conversation_id="conversation-1",
            baseline_current_node="node-before",
        ),
        text="queued",
        total_timeout=30,
        started=time.monotonic(),
    )

    assert result["conversation_id"] == "conversation-1"
    assert result["write_commit_proof"] == "CANONICAL_PROMPT_RECOVERY"
    assert result["_cwa_identity_recovered"] is True
    assert result["_cwa_identity_recovery_kind"] == "prompt"
    assert result["stream_terminal_observed"] is True
    assert (
        provider._canonical_state.take_final_payload("conversation-1") == final_payload
    )


def test_wkwebview_identity_recovery_retry_uses_bounded_backoff(
    monkeypatch,
) -> None:
    provider = WKWebViewTurnProvider()
    final_payload = _final_canonical_for_prompt("recover me", assistant_text="done")
    final_payload["mapping"]["user-final"]["message"]["id"] = "client-message-1"
    reads = [None, final_payload]
    provider._lightweight_transport = SimpleNamespace(
        source_client=SimpleNamespace(
            auth=SimpleNamespace(cookies={"session": "test-cookie"})
        ),
        read_catalog=lambda *args, **kwargs: {
            "items": [{"id": "conversation-recovered"}],
            "total": 1,
        }
    )
    monkeypatch.setattr(
        provider,
        "_read_conversation_payload_via_coordinated_curl",
        lambda *args, **kwargs: (reads.pop(0), None),
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


def test_wk_turn_broker_multiplexes_concurrent_normal_writes_request_locally() -> None:
    root = (
        Path(__file__).resolve().parents[1]
        / "src/chatgpt_web_adapter/wkwebview_helper"
    )
    helper = (root / "WKChatGPTAuthority.m").read_text(encoding="utf-8")
    shell = (root / "minimal_security_shell.js").read_text(encoding="utf-8")

    assert "__cwaRequestId: requestId" in shell
    assert "__cwaExpectedProfile: profile" in shell
    assert "__cwaExpectedParentMessageId: parentMessageId" in shell
    assert "__cwaProxyProtectedWrite: proxyProtectedWrite" in shell
    assert "__cwaRestoreSubmitFetchObserver" not in shell
    assert "(init&&init.__cwaRequestId)" in helper
    assert "(init&&init.__cwaExpectedProfile)" in helper
    assert "(init&&init.__cwaExpectedParentMessageId)" in helper
    assert "init.__cwaProxyProtectedWrite===true" in helper
    assert "concurrentNormalPages" not in helper


def test_wkwebview_helper_observer_backs_off_after_429_without_stream_polling() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src/chatgpt_web_adapter/wkwebview_helper/WKChatGPTAuthority.m"
    ).read_text(encoding="utf-8")

    assert "observerStatus.integerValue == 429" in source
    assert "MAX(observerPollInterval, 60.0)" in source
    assert "cacheKey='__cwaAuthorityAccessToken'" in source
    assert "commitPollDelay = 1.0" in source
    streaming_send = source[
        source.index("BOOL streamingResumeMode =") : source.index(
            "BOOL writeCommitProven =", source.index("BOOL streamingResumeMode =")
        )
    ]
    assert "canonicalPollDelay" not in streaming_send
    assert "CanonicalCompletionCheckScript" not in streaming_send
    assert '@"STREAM_TERMINAL"' in source
    assert "phase:'raw'" in source


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


def test_turn_broker_pre_submit_watchdog_fails_and_closes_connection(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        def recv_envelope(self, timeout: float):
            time.sleep(min(timeout, 0.01))
            return None

        def close(self) -> None:
            self.closed = True

    connection = FakeConnection()

    class FakeBrokerClient:
        def __init__(self, helper_binary: Path) -> None:
            self.helper_binary = helper_binary

        def start_turn(self, request: dict[str, Any], *, timeout: float):
            assert request == {"prompt": "hello"}
            assert timeout == 60.0
            return connection

    monkeypatch.setattr(
        "chatgpt_web_adapter.wkwebview_helper_runtime.WKSystemTurnBrokerClient",
        FakeBrokerClient,
    )
    monkeypatch.setattr(
        "chatgpt_web_adapter.wkwebview_helper_runtime._PRE_SUBMIT_TIMEOUT_SECONDS",
        0.05,
    )
    runtime = WKWebViewHelperRuntime(tmp_path, build_timeout=1)
    events: list[dict[str, Any]] = []
    invocation = SimpleNamespace(request={"prompt": "hello"})

    started = time.monotonic()
    with pytest.raises(RequestError, match="WKWEBVIEW_PRE_SUBMIT_TIMEOUT"):
        runtime._run_streaming_via_turn_broker(
            invocation,
            timeout=60.0,
            on_text_event=lambda _event: None,
            on_lifecycle_event=None,
            on_transport_event=events.append,
            on_submit_started=None,
            external_completion_check=None,
        )

    assert time.monotonic() - started < 0.5
    assert connection.closed is True
    assert events == [{"type": "pre_submit_timeout", "timeout_seconds": 0.05}]


def test_turn_broker_client_disconnect_cancels_native_request_promptly() -> None:
    class FakeBroker:
        def __init__(self) -> None:
            self.target: queue.Queue[dict[str, Any]] = queue.Queue()
            self.started = threading.Event()
            self.finished: list[tuple[str, bool]] = []

        def start(self, request_id: str, request: dict[str, Any]):
            assert request == {"prompt": "hello"}
            self.started.set()
            return self.target

        def finish(self, request_id: str, *, cancel: bool) -> None:
            self.finished.append((request_id, cancel))

    server, client = socket.socketpair()
    broker = FakeBroker()
    active_lock = threading.Lock()
    active_state: dict[str, Any] = {"count": 1, "last_activity": 0.0}
    worker = threading.Thread(
        target=turn_broker._serve_client,
        args=(server, broker, active_lock, active_state),
        daemon=True,
    )
    worker.start()
    client.sendall(
        json.dumps(
            {
                "type": "turn",
                "request_id": "request-1",
                "request": {"prompt": "hello"},
                "timeout": 60.0,
            }
        ).encode("utf-8")
        + b"\n"
    )
    assert broker.started.wait(timeout=1.0)

    client.close()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert broker.finished == [("request-1", True)]
    assert active_state["count"] == 0


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


def test_lightweight_phase_one_does_not_use_global_heavy_submit_gate() -> None:
    calls: list[str] = []

    class Provider:
        @staticmethod
        def _lightweight_path_enabled() -> bool:
            return True

        def _heavy_submit_gate(self, timeout: float):
            raise AssertionError("lightweight phase one must not enter global heavy submit gate")

        def _run_helper_streaming(
            self,
            invocation,
            *,
            timeout: float,
            on_text_event,
            on_lifecycle_event,
            **kwargs,
        ) -> dict[str, Any]:
            calls.append("stream")
            return {"ok": True, "conversation_id": "conv", "response_status": 200}

    orchestrator = WKTurnOrchestrator(Provider())
    result = orchestrator.run_protected_phase_one(
        SimpleNamespace(command=["helper", "--minimal-security-shell"]),
        total_timeout=30.0,
        started=time.monotonic(),
        streaming=True,
        on_text_event=lambda event: None,
        on_write_identity=lambda event: None,
    )

    assert calls == ["stream"]
    assert result["_cwa_phase_a_gate_wait_ms"] == 0
    assert result["_cwa_phase_a_transport"] == "wkwebview_minimal_security_shell"
