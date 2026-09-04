from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "src" / "chatgpt_web_adapter" / "browser_native_extension"
MAIN = EXTENSION / "passive_stream_tap_main.js"
BRIDGE = EXTENSION / "passive_stream_bridge.js"
WORKER = EXTENSION / "service_worker_passive_stream_observer.js"
MANIFEST = EXTENSION / "manifest.json"
RUNTIME_OBSERVATION = EXTENSION / "service_worker_runtime_observation.js"
BOOTSTRAP = EXTENSION / "service_worker_browser_runtime_v2.js"


def test_manifest_installs_passive_bridge_and_main_world_tap_at_document_start() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    scripts = manifest["content_scripts"]
    assert scripts == [
        {
            "matches": ["https://chatgpt.com/*"],
            "js": ["passive_stream_bridge.js"],
            "run_at": "document_start",
            "all_frames": False,
        },
        {
            "matches": ["https://chatgpt.com/*"],
            "js": ["passive_stream_tap_main.js"],
            "run_at": "document_start",
            "all_frames": False,
            "world": "MAIN",
        },
    ]
    assert "scripting" not in manifest["permissions"]


def test_passive_tap_has_no_network_dom_or_debugger_side_channel() -> None:
    source = MAIN.read_text(encoding="utf-8")
    assert "response.clone()" in source
    assert "response.body.getReader()" in source
    assert "isConversationWrite" in source
    assert "window.fetch = new Proxy(originalFetch" in source
    assert "Reflect.apply(target, thisArg, args)" in source
    assert "const originalWebSocket = window.WebSocket" in source
    assert "window.WebSocket = WebSocketProxy" in source
    assert "Reflect.construct(target, args, constructor)" in source
    assert "new WebSocket(" not in source
    for forbidden in (
        "/api/auth/session",
        "backend-api/conversation/",
        "MutationObserver",
        "chrome.debugger",
        "XMLHttpRequest",
        "document.querySelector",
        "browserAuthorityLeaseId",
        "leaseId",
    ):
        assert forbidden not in source

    bridge = BRIDGE.read_text(encoding="utf-8")
    assert "chrome.runtime.sendMessage" in bridge
    assert "window.postMessage" in bridge
    assert "fetch(" not in bridge
    assert "browserAuthorityLeaseId" not in bridge
    assert "leaseId" not in bridge
    assert "observerId" in bridge


def test_passive_layer_is_terminal_after_stable_runtime_assembly() -> None:
    source = RUNTIME_OBSERVATION.read_text(encoding="utf-8")
    connector = 'importScripts("service_worker_connector_support_pr10_0.js");'
    liveness = 'importScripts("service_worker_ui_liveness.js");'
    assert source.index(connector) < source.index(liveness)
    assert 'service_worker_passive_stream_observer.js' not in source

    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    runtime = 'importScripts("service_worker_runtime.js");'
    passive = 'importScripts("service_worker_passive_stream_observer.js");'
    assert bootstrap.index(runtime) < bootstrap.index(passive)
    assert bootstrap.rstrip().endswith(passive)

    worker = WORKER.read_text(encoding="utf-8")
    assert 'message?.type !== "observe_turn"' in worker
    assert 'type: "passive_observer_heartbeat"' in worker
    assert "CWA_PASSIVE_MAX_BUFFERED_EVENTS = 128" in worker
    assert "_cwaPassiveSessionsByObserverId" in worker
    assert "_cwaPassiveNewObserverId" in worker
    assert 'chrome.tabs.sendMessage(tabId, { type, observerId })' in worker
    assert "chrome.debugger" not in worker
    assert "fetch(" not in worker


def test_main_world_tap_streams_normalized_blocks_from_one_existing_post() -> None:
    harness = f"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync({json.dumps(str(MAIN))}, 'utf8');
const listeners = new Map();
const posted = [];
let fetchCount = 0;
const fakeWindow = {{
  fetch: async (_input, _init) => {{
    fetchCount += 1;
    const blocks = [
      {{
        message: {{
          id: 'thinking-1',
          author: {{role: 'assistant'}},
          recipient: 'all',
          content: {{content_type: 'text', parts: ['Первый']}},
          metadata: {{is_thinking_preamble_message: true, message_status: 'finished_successfully'}},
          end_turn: false
        }}
      }},
      {{
        message: {{
          id: 'thinking-1',
          author: {{role: 'assistant'}},
          recipient: 'all',
          content: {{content_type: 'text', parts: ['Первый нюанс уже появился: читаю workspace последовательно.']}},
          metadata: {{is_thinking_preamble_message: true, message_status: 'finished_successfully'}},
          end_turn: false
        }}
      }},
      {{
        message: {{
          id: 'tool-1',
          author: {{role: 'assistant'}},
          recipient: 'api_tool.call_tool',
          content: {{content_type: 'code', parts: [JSON.stringify({{path:'/CodexTool/x/git_status',args:{{}}}})]}},
          metadata: {{message_status: 'finished_successfully'}},
          end_turn: false
        }}
      }},
      {{
        message: {{
          id: 'final-1',
          author: {{role: 'assistant'}},
          recipient: 'all',
          content: {{content_type: 'text', parts: ['FINAL']}},
          metadata: {{finish_details: {{type: 'stop'}}}},
          end_turn: true
        }}
      }}
    ];
    const body = blocks.map((value) => `data: ${{JSON.stringify(value)}}\\n\\n`).join('') + 'data: [DONE]\\n\\n';
    return new Response(body, {{status: 200, headers: {{'content-type':'text/event-stream'}}}});
  }},
  postMessage: (value, _origin) => posted.push(value),
  addEventListener: (type, fn) => listeners.set(type, fn),
}};
global.window = fakeWindow;
global.location = {{origin:'https://chatgpt.com', href:'https://chatgpt.com/', pathname:'/c/conversation-1'}};
global.Request = Request;
vm.runInThisContext(source, {{filename:'passive_stream_tap_main.js'}});
const onMessage = listeners.get('message');
onMessage({{
  source: fakeWindow,
  origin: 'https://chatgpt.com',
  data: {{channel:'cwa-passive-stream-v1', direction:'control', action:'arm', observerId:'observer-1'}}
}});
(async () => {{
  await fakeWindow.fetch('https://chatgpt.com/backend-api/conversation', {{method:'POST'}});
  await new Promise((resolve) => setTimeout(resolve, 30));
  const events = posted.filter((value) => value?.direction === 'event').map((value) => value.event);
  console.log(JSON.stringify({{fetchCount, events}}));
}})();
"""
    completed = subprocess.run(
        ["node", "-e", harness],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    payload = json.loads(completed.stdout.strip())
    assert payload["fetchCount"] == 1
    assert [event["type"] for event in payload["events"]] == [
        "passive_stream_started",
        "passive_thinking_block",
        "passive_tool_call",
        "passive_turn_terminal",
        "passive_stream_ended",
    ]
    assert payload["events"][1]["text"] == "Первый нюанс уже появился: читаю workspace последовательно."
    assert payload["events"][2]["tool_name"] == "api_tool.call_tool"
    assert payload["events"][2]["label"] == "Reading git status..."
    assert payload["events"][3]["message_id"] == "final-1"


def test_passive_worker_requires_real_stream_start_before_long_observation() -> None:
    worker = WORKER.read_text(encoding="utf-8")
    assert "CWA_PASSIVE_STREAM_START_GRACE_MS = 2_000" in worker
    assert "CWA_PASSIVE_STREAM_END_GRACE_MS = 2_000" in worker
    assert "CWA_PASSIVE_HANDOFF_START_GRACE_MS = 5_000" in worker
    assert 'message.event?.type === "passive_stream_handoff"' in worker
    assert 'message.event?.type === "passive_stream_started"' in worker
    assert 'message.event?.type === "passive_stream_ended"' in worker
    assert "session.handoffPending = true" in worker
    assert "session.streamObserved = true" in worker
    assert "session.activeStreamCount += 1" in worker
    assert "session.activeStreamCount = Math.max(0, session.activeStreamCount - 1)" in worker
    assert 'error: "PASSIVE_OBSERVER_STREAM_NOT_OBSERVED"' in worker
    assert 'error: "PASSIVE_OBSERVER_STREAM_ENDED_WITHOUT_TERMINAL"' in worker
    assert "session.startupTimer = setTimeout" in worker
    assert "_cwaPassiveScheduleEndedFallback(session)" in worker


def test_passive_tap_follows_existing_page_websocket_handoff_without_new_network() -> None:
    harness = f"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync({json.dumps(str(MAIN))}, 'utf8');
const listeners = new Map();
const posted = [];
let fetchCount = 0;
class FakeWebSocket {{
  constructor(url) {{ this.url = url; this.listeners = new Map(); FakeWebSocket.last = this; }}
  addEventListener(type, fn) {{
    const current = this.listeners.get(type) || [];
    current.push(fn);
    this.listeners.set(type, current);
  }}
  emit(type, data) {{
    for (const fn of this.listeners.get(type) || []) fn({{data}});
  }}
}}
const fakeWindow = {{
  fetch: async (_input, _init) => {{
    fetchCount += 1;
    const handoff = {{
      type: 'stream_handoff',
      conversation_id: 'conversation-1',
      turn_exchange_id: 'turn-ws-1',
      options: [{{type:'subscribe_ws_topic', topic_id:'topic-1'}}]
    }};
    const body = `data: ${{JSON.stringify(handoff)}}\\n\\ndata: [DONE]\\n\\n`;
    return new Response(body, {{status: 200, headers: {{'content-type':'text/event-stream'}}}});
  }},
  WebSocket: FakeWebSocket,
  postMessage: (value, _origin) => posted.push(value),
  addEventListener: (type, fn) => listeners.set(type, fn),
}};
global.window = fakeWindow;
global.location = {{origin:'https://chatgpt.com', href:'https://chatgpt.com/c/conversation-1', pathname:'/c/conversation-1'}};
global.Request = Request;
vm.runInThisContext(source, {{filename:'passive_stream_tap_main.js'}});
const onMessage = listeners.get('message');
onMessage({{
  source: fakeWindow,
  origin: 'https://chatgpt.com',
  data: {{channel:'cwa-passive-stream-v1', direction:'control', action:'arm', observerId:'observer-ws'}}
}});
(async () => {{
  const socket = new fakeWindow.WebSocket('wss://example.invalid/celsius');
  await fakeWindow.fetch('https://chatgpt.com/backend-api/f/conversation', {{method:'POST'}});
  await new Promise((resolve) => setTimeout(resolve, 30));
  const finalPayload = {{
    message: {{
      id: 'final-ws-1',
      author: {{role:'assistant'}},
      recipient: 'all',
      content: {{content_type:'text', parts:['WS_FINAL']}},
      metadata: {{finish_details: {{type:'stop'}}, turn_exchange_id:'turn-ws-1'}},
      end_turn: true
    }}
  }};
  const encoded = `data: ${{JSON.stringify(finalPayload)}}\\n\\n`;
  socket.emit('message', JSON.stringify([{{
    type:'message',
    topic_id:'topic-1',
    payload: {{type:'conversation-turn-stream', payload: {{type:'stream-item', encoded_item: encoded}}}}
  }}]));
  socket.emit('message', JSON.stringify([{{
    type:'message',
    topic_id:'topic-1',
    payload: {{type:'conversation-turn-stream', payload: {{type:'done'}}}}
  }}]));
  await new Promise((resolve) => setTimeout(resolve, 20));
  const events = posted.filter((value) => value?.direction === 'event').map((value) => value.event);
  console.log(JSON.stringify({{fetchCount, events, proxied: fakeWindow.WebSocket !== FakeWebSocket}}));
}})();
"""
    completed = subprocess.run(
        ["node", "-e", harness],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    payload = json.loads(completed.stdout.strip())
    assert payload["fetchCount"] == 1
    assert payload["proxied"] is True
    assert [event["type"] for event in payload["events"]] == [
        "passive_stream_started",
        "passive_stream_handoff",
        "passive_stream_ended",
        "passive_stream_started",
        "passive_turn_terminal",
        "passive_stream_ended",
    ]
    assert payload["events"][4]["message_id"] == "final-ws-1"
    assert payload["events"][4]["turn_exchange_id"] == "turn-ws-1"
