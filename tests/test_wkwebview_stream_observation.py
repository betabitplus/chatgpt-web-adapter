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


def test_passive_stream_observation_emits_sanitized_raw_payloads() -> None:
    observation = _passive_stream_observation_script()
    harness = f"""
const events = [];
globalThis.window = globalThis;
globalThis.location = {{href: 'https://chatgpt.com/', origin: 'https://chatgpt.com'}};
window.webkit = {{messageHandlers: {{cwaStream: {{postMessage: (value) => events.push(value)}}}}}};
const encoder = new TextEncoder();
const payloads = [
  {{
    v: {{
      message: {{
        id: 'tool-call-1',
        author: {{role: 'assistant'}},
        recipient: 'web.run',
        status: 'finished_successfully',
        content: {{content_type: 'text', parts: ['{{"query":"python"}}']}},
        metadata: {{turn_exchange_id: 'turn-1'}},
        end_turn: false
      }}
    }}
  }},
  {{
    type: 'resume_conversation_token',
    token: '[REDACTED_SECRET]',
    conversation_id: 'conversation-1'
  }}
];
window.fetch = async () => new Response(new ReadableStream({{
  start(controller) {{
    for (const payload of payloads) {{
      controller.enqueue(encoder.encode('data: ' + JSON.stringify(payload) + '\\n\\n'));
    }}
    controller.close();
  }}
}}), {{status: 200}});
eval({json.dumps(observation)});
await window.fetch('/backend-api/f/conversation', {{method: 'POST'}});
await new Promise((resolve) => setTimeout(resolve, 25));
const raw = events.filter((event) => event && event.phase === 'raw');
const serialized = JSON.stringify(raw);
console.log(JSON.stringify({{
  raw_count: raw.length,
  tool_message_id: raw[0] && raw[0].parsed && raw[0].parsed.v && raw[0].parsed.v.message && raw[0].parsed.v.message.id,
  resume_type: raw[1] && raw[1].parsed && raw[1].parsed.type,
  resume_has_token: raw[1] && raw[1].parsed && Object.prototype.hasOwnProperty.call(raw[1].parsed, 'token'),
  leaked_secret: serialized.includes('[REDACTED_SECRET]')
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
        "raw_count": 2,
        "tool_message_id": "tool-call-1",
        "resume_type": "resume_conversation_token",
        "resume_has_token": False,
        "leaked_secret": False,
    }


def test_passive_stream_observation_detects_top_level_final_message() -> None:
    observation = _passive_stream_observation_script()
    harness = f"""
const events = [];
globalThis.window = globalThis;
globalThis.location = {{href: 'https://chatgpt.com/', origin: 'https://chatgpt.com'}};
window.webkit = {{messageHandlers: {{cwaStream: {{postMessage: (value) => events.push(value)}}}}}};
const encoder = new TextEncoder();
const payload = {{
  message: {{
    id: 'final-1',
    author: {{role: 'assistant'}},
    recipient: 'all',
    content: {{content_type: 'text', parts: ['FINAL']}},
    metadata: {{finish_details: {{type: 'stop'}}}},
    end_turn: true
  }}
}};
window.fetch = async () => new Response(new ReadableStream({{
  start(controller) {{
    controller.enqueue(encoder.encode('data: ' + JSON.stringify(payload) + '\\n\\n'));
    controller.close();
  }}
}}), {{status: 200}});
eval({json.dumps(observation)});
await window.fetch('/backend-api/f/conversation', {{method: 'POST'}});
await new Promise((resolve) => setTimeout(resolve, 25));
console.log(JSON.stringify({{
  terminal: events.some((event) => event && event.phase === 'terminal' && event.message_id === 'final-1'),
  text: events.some((event) => event && event.phase === 'text' && event.message_id === 'final-1' && event.text === 'FINAL'),
  ended: events.some((event) => event && event.phase === 'ended')
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
        "terminal": True,
        "text": True,
        "ended": True,
    }


def test_passive_stream_observation_flushes_resume_event_at_eof_without_blank_line() -> (
    None
):
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


def test_passive_stream_observation_emits_done_phase_for_done_sentinel() -> None:
    observation = _passive_stream_observation_script()
    harness = f"""
const events = [];
globalThis.window = globalThis;
globalThis.location = {{href: 'https://chatgpt.com/', origin: 'https://chatgpt.com'}};
window.webkit = {{messageHandlers: {{cwaStream: {{postMessage: (value) => events.push(value)}}}}}};
const encoder = new TextEncoder();
window.fetch = async () => new Response(new ReadableStream({{
  start(controller) {{
    controller.enqueue(encoder.encode('data: [DONE]\\n\\n'));
    controller.close();
  }}
}}), {{status: 200}});
eval({json.dumps(observation)});
await window.fetch('/backend-api/f/conversation', {{method: 'POST'}});
await new Promise((resolve) => setTimeout(resolve, 25));
console.log(JSON.stringify({{
  done: events.some((event) => event && event.phase === 'done'),
  ended: events.some((event) => event && event.phase === 'ended')
}}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", harness],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {"done": True, "ended": True}


def test_native_stream_handoff_does_not_wait_for_resume_token() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "chatgpt_web_adapter"
        / "wkwebview_helper"
        / "WKChatGPTAuthority.m"
    ).read_text(encoding="utf-8")
    assert "BOOL topicHandoffFence = responseOK" in source
    assert "terminalCompletionFence || topicHandoffFence" in source
    assert "&& delegate.streamTopicId.length == 0" in source
    assert '@"TOPIC_HANDOFF_FENCE"' in source


def test_native_stream_bridge_forwards_done_as_raw_ws_done() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "chatgpt_web_adapter"
        / "wkwebview_helper"
        / "WKChatGPTAuthority.m"
    ).read_text(encoding="utf-8")
    assert 'else if ([phase isEqualToString:@"done"]) {' in source
    assert 'PrintEvent(@{@"type":@"raw_ws_done"});' in source
