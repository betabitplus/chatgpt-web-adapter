from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "src" / "chatgpt_web_adapter" / "browser_native_extension"
OVERLAY = EXT / "service_worker_terminal_outcome.js"
RUNTIME_OBSERVATION = EXT / "service_worker_runtime_observation.js"


def _run_overlay(body: str, *, base64_encoded: bool = False) -> dict[str, object]:
    source = OVERLAY.read_text(encoding="utf-8")
    encoded = body
    if base64_encoded:
        import base64

        encoded = base64.b64encode(body.encode()).decode()
    script = f"""
const atob = (value) => Buffer.from(value, 'base64').toString('binary');
const TextDecoder = require('util').TextDecoder;
let extractSafeStreamMetadata = () => ({{conversationId:'conversation-1', turnExchangeId:'turn-1'}});
{source}
const result = extractSafeStreamMetadata({json.dumps(encoded)}, {str(base64_encoded).lower()});
console.log(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


def test_terminal_outcome_overlay_loaded_after_write_domain() -> None:
    text = RUNTIME_OBSERVATION.read_text(encoding="utf-8")
    assert 'importScripts("service_worker_terminal_outcome.js");' in text


def test_terminal_outcome_extracts_post_final_conversation_limit_error() -> None:
    body = "\n\n".join(
        [
            'data: {"message":{"status":"finished_successfully","end_turn":true}}',
            'data: {"message":null,"conversation_id":"conversation-1",'
            '"error":"You\\u0027ve reached the maximum length for this conversation, but you can keep talking by starting a new chat.",'
            '"error_code":"conversation_too_large"}',
            "data: [DONE]",
        ]
    )
    result = _run_overlay(body)
    assert result == {
        "conversationId": "conversation-1",
        "turnExchangeId": "turn-1",
        "terminalErrorCode": "conversation_too_large",
        "terminalError": (
            "You've reached the maximum length for this conversation, "
            "but you can keep talking by starting a new chat."
        ),
    }


def test_terminal_outcome_preserves_historical_text_only_limit_frame() -> None:
    body = "\n\n".join(
        [
            'data: {"message":{"status":"finished_successfully","end_turn":true}}',
            'data: {"message":null,"conversation_id":"conversation-1",'
            '"error":"You\\u0027ve reached the maximum length for this conversation, '
            'but you can keep talking by starting a new chat."}',
            "data: [DONE]",
        ]
    )
    result = _run_overlay(body)

    assert result["terminalErrorCode"] is None
    assert result["terminalError"] == (
        "You've reached the maximum length for this conversation, "
        "but you can keep talking by starting a new chat."
    )


def test_terminal_outcome_supports_base64_and_does_not_invent_error() -> None:
    clean = 'data: {"type":"stream_handoff","conversation_id":"conversation-1"}\n\ndata: [DONE]'
    result = _run_overlay(clean, base64_encoded=True)
    assert result["terminalErrorCode"] is None
    assert result["terminalError"] is None
    assert result["conversationId"] == "conversation-1"
