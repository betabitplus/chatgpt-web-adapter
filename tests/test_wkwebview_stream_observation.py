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


def test_passive_stream_observation_derives_topic_from_nested_turn_identity() -> None:
    observation = _passive_stream_observation_script()
    harness = f"""
const events = [];
globalThis.window = globalThis;
globalThis.location = {{href: 'https://chatgpt.com/', origin: 'https://chatgpt.com'}};
window.webkit = {{messageHandlers: {{cwaStream: {{postMessage: (value) => events.push(value)}}}}}};
const encoder = new TextEncoder();
const payload = {{
  envelope: {{
    arbitrary_container: {{
      message: {{
        id: 'user-1',
        author: {{role: 'user'}},
        content: {{content_type: 'text', parts: ['hello']}},
        metadata: {{
          turn_exchange_id: '11111111-2222-3333-4444-555555555555'
        }}
      }}
    }}
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
const handoff = events.find((event) => event && event.phase === 'handoff');
console.log(JSON.stringify({{
  observed: Boolean(handoff),
  topic_id: handoff && handoff.topic_id,
  turn_exchange_id: handoff && handoff.turn_exchange_id,
  conversation_id: handoff && handoff.conversation_id
}}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", harness],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "observed": True,
        "topic_id": "conversation-turn-11111111-2222-3333-4444-555555555555",
        "turn_exchange_id": "11111111-2222-3333-4444-555555555555",
        "conversation_id": None,
    }


def test_native_handoff_preserves_known_conversation_and_derives_topic() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "chatgpt_web_adapter"
        / "wkwebview_helper"
        / "WKChatGPTAuthority.m"
    ).read_text(encoding="utf-8")

    assert (
        "if (handoffConversationId.length > 0) self.streamConversationId = handoffConversationId;"
        in source
    )
    assert (
        'handoffTopicId = [@"conversation-turn-" stringByAppendingString:self.streamTurnExchangeId];'
        in source
    )
    assert (
        'self.streamConversationId = [body[@"conversation_id"] isKindOfClass:[NSString class]]'
        not in source
    )


def test_minimal_security_prepare_matches_verified_continuation_dispatch() -> None:
    root = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "chatgpt_web_adapter"
        / "wkwebview_helper"
    )
    shell = (root / "minimal_security_shell.js").read_text(encoding="utf-8")

    assert 'prepareState: "none"' in shell
    assert 'prepareDispatch: "immediate"' in shell
    assert 'prepareSource: "context_change"' in shell
    assert "includePartialQuery: false" in shell
    assert 'prepareState: "sent"' in shell
    assert 'prepareDispatch: "debounced"' in shell
    assert 'prepareSource: "composer_editor_state"' in shell
    assert "includePartialQuery: true" in shell
    assert 'client_prepare_state: temporary ? "success" : (conversationId ? "sent" : "success")' in shell
    assert 'if (temporary) {' in shell
    assert 'prepareState: "success"' in shell
    assert 'includePartialQuery: true' in shell
    assert 'fork_from_shared_post: false' in shell
    assert '"x-conduit-token": "no-token"' not in shell
    assert 'officialApiClient.safePost(' in shell
    assert '"/f/conversation/prepare"' in shell
    assert 'onSubmitReady: conversationId && !temporary ? resolveSubmitReady : null' in shell
    assert 'await submitReadyPromise;' in shell
    assert 'if (conversationId && !temporary) {' in shell
    assert 'const conduitToken = await conduitPromise;' in shell
    assert 'const useOfficialConversationTransport =' in shell
    assert 'conversationId\n      && !temporary\n      && !proxyProtectedWrite\n      && typeof officialConversationTransport === "function"' in shell
    assert 'sharedConversationInitializationPromise = Promise.resolve(Object.freeze({' in shell
    assert 'sharedModelCatalogPromises.set("temporary", Promise.resolve(modelsPayload));' in shell
    assert 'postStream({ phase: "handoff_released" });' in shell
    assert 'content.parts.filter((part) => typeof part === "string").join("")' in shell
    assert 'content.parts.filter((part) => typeof part === "string").join("\\n")' not in shell
    assert 'try { void reader.cancel(); } catch (_) {}' in shell
    assert 'try { await reader.cancel(); } catch (_) {}' not in shell
    assert 'const brokerHandoffPromise = new Promise((resolve) =>' in shell
    assert 'brokerHandoffPromise.then(async () =>' not in shell
    assert 'compressionEligible: true' in shell
    assert 'routeName: "/f/conversation"' in shell
    assert 'targetBaseUrl: "https://chatgpt.com/backend-api"' in shell
    assert '"x-oai-stream-handoff-attempt-id"' in shell
    assert 'discoverConversationTransportNames' in shell
    assert 'const transportExport = exportAlias(source, transportNames.transportName);' in shell
    assert 'integrityModule[integrityExports.transportExport]' in shell
    assert 'discoverSharedRuntimeCandidate' in shell
    assert 'discoverSharedRuntimePath' in shell
    assert 'discoverSharedRequestClientExports' in shell
    assert '__cwa_integrity_exports_v5:' in shell
    assert 'localStorage.removeItem(integrityDiscoveryCacheKey)' in shell
    assert 'Object.values(integrityModule)' not in shell
    assert 'Function.prototype.toString.call(value)' not in shell
    assert 'MINIMAL_OFFICIAL_CONVERSATION_TRANSPORT_MISSING' in shell
    assert "4813494d" not in shell
    assert "integrityBurstSnapshot" not in shell
    assert "installIntegrityRequestCapture" not in shell
    assert "officialIntegrityRuntime" not in shell
    assert ".IGt" not in shell
    assert ".d3" not in shell
    assert ".s3" not in shell
    raw_forward = 'postStream({ phase: "raw", parsed: transportEvent.data });'
    transport_reduce = "processBrokerPayload(transportEvent.data);"
    assert shell.index(raw_forward) < shell.index(transport_reduce, shell.index(raw_forward))
    assert "let brokerTextSequence = 0;" in shell
    assert "brokerTextSequence += 1;" in shell
    assert "sequence: brokerTextSequence," in shell
    assert 'path === "/message/content/parts/0"' in shell
    assert 'path === "/message/content"' in shell
    assert "inspectBrokerTerminalPatch(path, value);" in shell
    reducer_start = shell.index("const processBrokerPayload = (payload) =>")
    reducer_end = shell.index("const inspectProtectedTransportData =", reducer_start)
    reducer = shell[reducer_start:reducer_end]
    assert reducer.index('emitBrokerText("assistant_text_delta"') < reducer.index(
        "inspectBrokerTerminalPatch(path, value);"
    )
    assert 'const visibleFinalAssistant = author && author.role === "assistant"' in shell
    assert 'const assistantTerminal = visibleFinalAssistant && (' in shell
    assert 'explicitTerminalType && brokerCurrentIsFinalText && !!brokerCurrentMessageId' in shell


def test_minimal_security_protected_write_observes_response_directly() -> None:
    root = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "chatgpt_web_adapter"
        / "wkwebview_helper"
    )
    native_source = (root / "WKChatGPTAuthority.m").read_text(encoding="utf-8")
    shell = (root / "minimal_security_shell.js").read_text(encoding="utf-8")

    assert "window.__cwaObserveStreamResponse=observe;" in native_source
    assert native_source.count("initWithSource:SubmitObservationScript()") >= 2
    assert 'window.__CWA_DEFER_SUBMIT_OBSERVER__=true;' in native_source
    assert "window.__CWA_DEFER_SUBMIT_OBSERVER__===true?true:install()" in native_source
    assert "window.__cwaRestoreSubmitFetchObserver" in native_source
    assert '@"proxy_protected_write": @(entry.proxyProtectedWrite)' in native_source
    assert "const proxyProtectedWrite = requestConfig" in shell
    assert "requestConfig.proxy_protected_write === true" in shell
    assert "__cwaProxyProtectedWrite: proxyProtectedWrite" in shell
    assert "__cwaRequestId: requestId" in shell
    assert "MINIMAL_PROXY_SUBMIT_OBSERVER_INSTALL_FAILED" in shell
    assert "window.__cwaRestoreSubmitFetchObserver" not in shell
    assert "const observeProtectedWriteResponse =" in shell
    assert "const observeDirectWriteResponse = async (response) =>" in shell
    assert "observedResponse = response.clone()" in shell
    assert "await observeProtectedWriteResponse(observedResponse)" in shell
    assert 'postStream({ phase: "raw", parsed });' in shell
    assert "processBrokerPayload(parsed);" in shell
    assert "__cwaDirectObserve: true" in shell
    assert "void observeDirectWriteResponse(writeResponse)" in shell
    assert shell.index("const observeProtectedWriteResponse =") < shell.index(
        "loadIntegrityRuntime(integrityURL)"
    )


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
    assert '@"type":@"raw_ws_done"' in source
    assert "PrintEventForRequest" in source
    assert "if (!self.submitProxyDispatch || !self.submitTemporaryModeObserved)" in source
