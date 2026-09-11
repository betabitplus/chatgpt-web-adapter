from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def _passive_stream_observation_script() -> str:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "chatgpt_web_adapter"
        / "wkwebview_helper"
        / "WKChatGPTAuthority.m"
    ).read_text(encoding="utf-8")
    start = source.index("static NSString *PassiveStreamObservationScript(void)")
    end = source.index("\nstatic NSString *", start + 1)
    function = source[start:end]
    chunks = re.findall(r'@?"((?:\\.|[^"\\])*)"', function)
    return "".join(json.loads(f'"{chunk}"') for chunk in chunks)


def test_passive_stream_observation_flushes_resume_event_at_eof_without_blank_line() -> None:
    observation = _passive_stream_observation_script()
    harness = f"""
const events = [];
globalThis.window = globalThis;
globalThis.location = {{href: 'https://chatgpt.com/', origin: 'https://chatgpt.com'}};
window.webkit = {{messageHandlers: {{cwaStream: {{postMessage: (value) => events.push(value)}}}}}};
const encoder = new TextEncoder();
window.fetch = async () => new Response(new ReadableStream({{
  start(controller) {{
    controller.enqueue(encoder.encode('data: {{"type":"resume_conversation_token","token":"[REDACTED_SECRET]","conversation_id":"temporary-1"}}'));
    controller.close();
  }}
}}), {{status: 200}});
eval({json.dumps(observation)});
await window.fetch('/backend-api/f/conversation', {{method: 'POST'}});
await new Promise((resolve) => setTimeout(resolve, 25));
const resume = events.find((event) => event && event.phase === 'resume');
console.log(JSON.stringify({{
  resume_observed: Boolean(resume),
  conversation_id: resume && resume.conversation_id,
  ended_observed: events.some((event) => event && event.phase === 'ended')
}}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", harness],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    assert result == {
        "resume_observed": True,
        "conversation_id": "temporary-1",
        "ended_observed": True,
    }
