// Passive page-stream observation for long browser-owned turns.
//
// Browser Authority still owns submit proof and detaches promptly. After that,
// this layer receives only normalized events from a MAIN-world fetch tap that
// clones the response stream already consumed by ChatGPT. It performs no HTTP
// requests, no DOM scraping, and no debugger work.

const _cwaPassivePriorExecuteNativeTurn = executeNativeTurn;
const _cwaPassivePriorExecuteOfficialPageTurn = executeOfficialPageTurn;
const _cwaPassivePriorOnNativeMessage = onNativeMessage;
const CWA_PASSIVE_MAX_BUFFERED_EVENTS = 128;
const CWA_PASSIVE_MAX_TEXT_CHARS = 12_000;
const CWA_PASSIVE_STREAM_START_GRACE_MS = 2_000;

let _cwaPassivePendingLeaseId = null;
const _cwaPassiveSessions = new Map();
const _cwaPassiveSessionsByObserverId = new Map();

function _cwaPassiveText(value, max = CWA_PASSIVE_MAX_TEXT_CHARS) {
  if (typeof value !== "string") return null;
  const normalized = value.replace(/\u0000/g, "").trim();
  if (!normalized) return null;
  return normalized.slice(0, max);
}


function _cwaPassiveNewObserverId() {
  if (typeof crypto?.randomUUID === "function") return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function _cwaPassiveSessionForLease(leaseId) {
  return typeof leaseId === "string" && leaseId
    ? _cwaPassiveSessions.get(leaseId) || null
    : null;
}

async function _cwaPassiveSendTabControl(tabId, type, observerId) {
  if (!Number.isInteger(tabId)) return false;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const result = await chrome.tabs.sendMessage(tabId, { type, observerId });
      if (result?.ok === true) return true;
    } catch {}
    if (attempt < 2) await sleep(25);
  }
  return false;
}

function _cwaPassiveNormalizeEvent(raw) {
  if (!raw || typeof raw !== "object") return null;
  const type = raw.type;
  const messageId = _cwaPassiveText(raw.message_id, 256);
  const turnExchangeId = _cwaPassiveText(raw.turn_exchange_id, 256);
  const conversationId = _cwaPassiveText(raw.conversation_id, 256);

  if (type === "passive_thinking_block") {
    const text = _cwaPassiveText(raw.text);
    if (!text) return null;
    return {
      type: "canonical_intermediate_message",
      message_kind: "assistant_progress",
      message_id: messageId,
      text,
      source: "passive_page_stream",
      turn_exchange_id: turnExchangeId,
      conversation_id: conversationId,
    };
  }
  if (type === "passive_tool_call") {
    const toolName = _cwaPassiveText(raw.tool_name, 160);
    const label = _cwaPassiveText(raw.label, 1000);
    if (!toolName) return null;
    return {
      type: "canonical_intermediate_message",
      message_kind: "tool_call",
      message_id: messageId,
      tool_name: toolName,
      label: label || "Using tool...",
      source: "passive_page_stream",
      turn_exchange_id: turnExchangeId,
      conversation_id: conversationId,
    };
  }
  if (type === "passive_turn_terminal") {
    return {
      type: "passive_turn_terminal",
      message_id: messageId,
      finish_reason: _cwaPassiveText(raw.finish_reason, 160),
      source: "passive_page_stream",
      turn_exchange_id: turnExchangeId,
      conversation_id: conversationId,
    };
  }
  return null;
}

function _cwaPassiveEventMatchesSession(session, event) {
  if (!session || !event) return false;
  if (
    session.conversationId &&
    event.conversation_id &&
    session.conversationId !== event.conversation_id
  ) return false;
  if (
    session.turnExchangeId &&
    event.turn_exchange_id &&
    session.turnExchangeId !== event.turn_exchange_id
  ) return false;
  return true;
}

function _cwaPassivePostTurnEvent(session, event) {
  if (!session?.subscriberRequestId || event.type === "passive_turn_terminal") return;
  postNative({
    protocol: BRIDGE_PROTOCOL_VERSION,
    type: "turn_event",
    request_id: session.subscriberRequestId,
    event,
  });
}

function _cwaPassiveCompleteSubscription(session, terminal) {
  if (!session?.subscriberRequestId) return;
  const requestId = session.subscriberRequestId;
  session.subscriberRequestId = null;
  if (session.observeTimer !== null) {
    clearTimeout(session.observeTimer);
    session.observeTimer = null;
  }
  postNative({
    protocol: BRIDGE_PROTOCOL_VERSION,
    type: "observe_turn_result",
    request_id: requestId,
    ok: true,
    conversationId: terminal?.conversation_id || session.conversationId || null,
    turnExchangeId: terminal?.turn_exchange_id || session.turnExchangeId || null,
    messageId: terminal?.message_id || null,
    finishReason: terminal?.finish_reason || null,
    source: "passive_page_stream",
  });
}

async function _cwaPassiveCloseSession(session) {
  if (!session) return;
  _cwaPassiveSessions.delete(session.leaseId);
  _cwaPassiveSessionsByObserverId.delete(session.observerId);
  if (session.observeTimer !== null) clearTimeout(session.observeTimer);
  if (session.startupTimer !== null) clearTimeout(session.startupTimer);
  if (session.heartbeatTimer !== null) clearInterval(session.heartbeatTimer);
  session.observeTimer = null;
  session.startupTimer = null;
  session.heartbeatTimer = null;
  try {
    await _cwaPassiveSendTabControl(session.tabId, "cwa_passive_disarm", session.observerId);
  } catch {}
}

function _cwaPassiveAcceptEvent(session, event) {
  if (!_cwaPassiveEventMatchesSession(session, event)) return;
  if (event.conversation_id && !session.conversationId) session.conversationId = event.conversation_id;
  if (event.turn_exchange_id && !session.turnExchangeId) session.turnExchangeId = event.turn_exchange_id;

  if (event.type === "passive_turn_terminal") {
    session.terminal = event;
    if (session.subscriberRequestId !== null) {
      _cwaPassiveCompleteSubscription(session, event);
      void _cwaPassiveCloseSession(session);
    }
    return;
  }

  if (session.subscriberRequestId !== null) {
    _cwaPassivePostTurnEvent(session, event);
    return;
  }
  if (session.buffer.length >= CWA_PASSIVE_MAX_BUFFERED_EVENTS) session.buffer.shift();
  session.buffer.push(event);
}

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type === "cwa_passive_bridge_ready") {
    const tabId = sender?.tab?.id;
    if (!Number.isInteger(tabId)) return false;
    for (const session of _cwaPassiveSessions.values()) {
      if (session.tabId === tabId) {
        void _cwaPassiveSendTabControl(tabId, "cwa_passive_arm", session.observerId)
          .then((armed) => { session.armed = session.armed || armed; });
      }
    }
    return false;
  }

  if (message?.type !== "cwa_passive_stream_event") return false;
  const observerId = typeof message.observerId === "string" ? message.observerId.trim() : "";
  const session = observerId ? _cwaPassiveSessionsByObserverId.get(observerId) || null : null;
  if (!session || sender?.tab?.id !== session.tabId) return false;
  if (message.event?.type === "passive_stream_started") {
    session.streamObserved = true;
    if (session.startupTimer !== null) {
      clearTimeout(session.startupTimer);
      session.startupTimer = null;
    }
    return false;
  }
  const event = _cwaPassiveNormalizeEvent(message.event);
  if (event) _cwaPassiveAcceptEvent(session, event);
  return false;
});

executeNativeTurn = async function _cwaExecuteNativeTurnWithPassiveObservation(message) {
  const leaseId = message?.passiveObserve === true && typeof message?.browserAuthorityLeaseId === "string"
    ? message.browserAuthorityLeaseId.trim()
    : "";
  if (!leaseId) return _cwaPassivePriorExecuteNativeTurn(message);

  _cwaPassivePendingLeaseId = leaseId;
  try {
    const result = await _cwaPassivePriorExecuteNativeTurn(message);
    const session = _cwaPassiveSessionForLease(leaseId);
    if (session) {
      if (typeof result?.conversationId === "string" && result.conversationId) {
        session.conversationId = result.conversationId;
      }
      if (typeof result?.turnExchangeId === "string" && result.turnExchangeId) {
        session.turnExchangeId = result.turnExchangeId;
      }
    }
    return {
      ...result,
      passiveObserverArmed: session?.armed === true,
    };
  } catch (error) {
    const session = _cwaPassiveSessionForLease(leaseId);
    if (session) await _cwaPassiveCloseSession(session);
    throw error;
  } finally {
    if (_cwaPassivePendingLeaseId === leaseId) _cwaPassivePendingLeaseId = null;
  }
};

executeOfficialPageTurn = async function _cwaExecuteOfficialPageTurnWithPassiveArm(args) {
  const leaseId = _cwaPassivePendingLeaseId;
  if (!leaseId || !Number.isInteger(args?.tabId)) {
    return _cwaPassivePriorExecuteOfficialPageTurn(args);
  }

  const existing = _cwaPassiveSessionForLease(leaseId);
  if (existing) await _cwaPassiveCloseSession(existing);
  const session = {
    leaseId,
    observerId: _cwaPassiveNewObserverId(),
    tabId: args.tabId,
    armed: false,
    conversationId: null,
    turnExchangeId: null,
    buffer: [],
    terminal: null,
    streamObserved: false,
    subscriberRequestId: null,
    observeTimer: null,
    startupTimer: null,
    heartbeatTimer: null,
  };
  _cwaPassiveSessions.set(leaseId, session);
  _cwaPassiveSessionsByObserverId.set(session.observerId, session);
  session.armed = await _cwaPassiveSendTabControl(
    args.tabId,
    "cwa_passive_arm",
    session.observerId
  );
  return _cwaPassivePriorExecuteOfficialPageTurn(args);
};

onNativeMessage = async function _cwaOnNativeMessageWithPassiveObserve(message, port) {
  if (
    message?.protocol !== BRIDGE_PROTOCOL_VERSION ||
    message?.type !== "observe_turn"
  ) {
    return _cwaPassivePriorOnNativeMessage(message, port);
  }

  const requestId = typeof message.request_id === "string" ? message.request_id.trim() : "";
  const leaseId = typeof message.browserAuthorityLeaseId === "string"
    ? message.browserAuthorityLeaseId.trim()
    : "";
  if (!requestId || !leaseId) return;
  const session = _cwaPassiveSessionForLease(leaseId);
  if (!session || session.armed !== true) {
    safePortPost(port, {
      protocol: BRIDGE_PROTOCOL_VERSION,
      type: "observe_turn_result",
      request_id: requestId,
      ok: false,
      error: "PASSIVE_OBSERVER_UNAVAILABLE",
    });
    return;
  }
  if (session.subscriberRequestId !== null) {
    safePortPost(port, {
      protocol: BRIDGE_PROTOCOL_VERSION,
      type: "observe_turn_result",
      request_id: requestId,
      ok: false,
      error: "PASSIVE_OBSERVER_ALREADY_SUBSCRIBED",
    });
    return;
  }

  const expectedConversationId = _cwaPassiveText(message.conversationId, 256);
  const expectedTurnExchangeId = _cwaPassiveText(message.turnExchangeId, 256);
  if (expectedConversationId) session.conversationId = expectedConversationId;
  if (expectedTurnExchangeId) session.turnExchangeId = expectedTurnExchangeId;
  session.subscriberRequestId = requestId;
  session.heartbeatTimer = setInterval(() => {
    if (session.subscriberRequestId !== requestId) return;
    postNative({
      protocol: BRIDGE_PROTOCOL_VERSION,
      type: "turn_event",
      request_id: requestId,
      event: { type: "passive_observer_heartbeat", source: "passive_page_stream" },
    });
  }, 30_000);

  for (const event of session.buffer) _cwaPassivePostTurnEvent(session, event);
  session.buffer.length = 0;
  if (session.terminal) {
    _cwaPassiveCompleteSubscription(session, session.terminal);
    await _cwaPassiveCloseSession(session);
    return;
  }

  if (!session.streamObserved) {
    session.startupTimer = setTimeout(() => {
      if (session.subscriberRequestId !== requestId || session.streamObserved) return;
      session.subscriberRequestId = null;
      safePortPost(port, {
        protocol: BRIDGE_PROTOCOL_VERSION,
        type: "observe_turn_result",
        request_id: requestId,
        ok: false,
        error: "PASSIVE_OBSERVER_STREAM_NOT_OBSERVED",
      });
      void _cwaPassiveCloseSession(session);
    }, CWA_PASSIVE_STREAM_START_GRACE_MS);
  }

  const timeoutMs = Number.isFinite(message.timeoutMs)
    ? Math.max(1_000, Number(message.timeoutMs))
    : 120_000;
  session.observeTimer = setTimeout(() => {
    if (session.subscriberRequestId !== requestId) return;
    session.subscriberRequestId = null;
    session.observeTimer = null;
    if (session.heartbeatTimer !== null) clearInterval(session.heartbeatTimer);
    session.heartbeatTimer = null;
    safePortPost(port, {
      protocol: BRIDGE_PROTOCOL_VERSION,
      type: "observe_turn_result",
      request_id: requestId,
      ok: false,
      error: "PASSIVE_OBSERVER_TIMEOUT",
    });
  }, timeoutMs);
};
