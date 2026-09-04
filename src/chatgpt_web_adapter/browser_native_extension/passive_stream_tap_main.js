(() => {
  "use strict";

  const CHANNEL = "cwa-passive-stream-v1";
  const MAX_TEXT = 12000;
  const MAX_TOOL_TEXT = 16000;
  const COMPLETED = new Set(["finished_successfully", "completed"]);

  if (window.__cwaPassiveStreamTapInstalled === true) return;
  Object.defineProperty(window, "__cwaPassiveStreamTapInstalled", {
    value: true,
    enumerable: false,
    configurable: false,
    writable: false,
  });

  let activeObserverId = null;
  let syntheticId = 0;
  let currentPatchMessage = null;
  const emittedMessageIds = new Set();
  const pendingThinking = new Map();
  const originalFetch = window.fetch.bind(window);

  function stringValue(value) {
    return typeof value === "string" && value.trim() ? value.trim() : null;
  }

  function messageId(message) {
    const id = stringValue(message?.id);
    if (id) return id;
    syntheticId += 1;
    return `passive-${syntheticId}`;
  }

  function bounded(value, max = MAX_TEXT) {
    if (typeof value !== "string") return "";
    const clean = value.replace(/\u0000/g, "");
    return clean.length <= max ? clean : clean.slice(0, max);
  }

  function messageText(message, max = MAX_TEXT) {
    const content = message?.content;
    if (!content || typeof content !== "object") return "";
    if (typeof content.text === "string") return bounded(content.text, max);
    if (typeof content.content === "string") return bounded(content.content, max);
    const parts = Array.isArray(content.parts) ? content.parts : [];
    let text = "";
    for (const part of parts.slice(0, 32)) {
      if (typeof part === "string") text += part;
      else if (part && typeof part === "object" && typeof part.text === "string") text += part.text;
      if (text.length >= max) break;
    }
    return bounded(text, max);
  }

  function turnExchangeId(message) {
    const metadata = message?.metadata;
    if (!metadata || typeof metadata !== "object") return null;
    return stringValue(metadata.turn_exchange_id) || stringValue(metadata.working_turn_id);
  }

  function currentConversationId() {
    try {
      const match = location.pathname.match(/^\/c\/([^/]+)/);
      if (!match) return null;
      const value = decodeURIComponent(match[1]);
      if (!value || value.startsWith("WEB:")) return null;
      return value;
    } catch {
      return null;
    }
  }

  function emit(event) {
    if (!activeObserverId || !event || typeof event !== "object") return;
    window.postMessage({
      channel: CHANNEL,
      direction: "event",
      observerId: activeObserverId,
      event: {
        ...event,
        conversation_id: event.conversation_id || currentConversationId(),
      },
    }, location.origin);
  }

  function completedMessage(message) {
    const status = stringValue(message?.status) || stringValue(message?.metadata?.message_status);
    return message?.end_turn === true || (status != null && COMPLETED.has(status));
  }

  function finishReason(message) {
    const direct = stringValue(message?.finish_reason);
    if (direct) return direct;
    const details = message?.metadata?.finish_details;
    if (details && typeof details === "object") return stringValue(details.type);
    return null;
  }

  function flushPendingThinking(exceptId = null) {
    for (const [id, entry] of pendingThinking) {
      if (id === exceptId || emittedMessageIds.has(id)) continue;
      if (!entry.text.trim()) continue;
      emittedMessageIds.add(id);
      pendingThinking.delete(id);
      emit({
        type: "passive_thinking_block",
        message_id: id,
        text: entry.text,
        turn_exchange_id: entry.turnExchangeId,
      });
    }
  }

  function explicitToolLabel(message) {
    const metadata = message?.metadata;
    if (!metadata || typeof metadata !== "object") return null;
    for (const key of ["tool_invoking_message", "invoking_message", "title", "label"]) {
      const value = stringValue(metadata[key]);
      if (value) return value;
    }
    return null;
  }

  function friendlyToolLabel(message, recipient) {
    const explicit = explicitToolLabel(message);
    if (explicit) return explicit;
    const raw = messageText(message, MAX_TOOL_TEXT).trim();
    if (!raw || !["{", "["].includes(raw[0])) {
      if (recipient === "api_tool.list_resources") return "Discovering tools...";
      return null;
    }
    let payload;
    try { payload = JSON.parse(raw); } catch { return null; }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;

    if (recipient === "api_tool.list_resources") {
      const query = stringValue(payload.query);
      if (query) return `Discovering ${query}...`;
      if (Array.isArray(payload.paths) && stringValue(payload.paths[0])) {
        return `Discovering ${stringValue(payload.paths[0])} tools...`;
      }
      return "Discovering tools...";
    }
    if (recipient !== "api_tool.call_tool") return null;

    const path = stringValue(payload.path);
    const action = path ? path.replace(/\/+$/, "").split("/").pop() : null;
    const args = payload.args && typeof payload.args === "object" && !Array.isArray(payload.args)
      ? payload.args
      : {};
    if (action === "git_status") return "Reading git status...";
    if (action === "show_changes") return "Reviewing changes...";
    if (action === "open_workspace" || action === "open_current_workspace") return "Opening workspace...";
    if (action === "read") {
      const target = stringValue(args.path);
      return target ? `Reading ${target}...` : "Reading file...";
    }
    if (action === "tree") {
      const target = stringValue(args.path);
      return target ? `Reading tree ${target}...` : "Reading tree...";
    }
    if (action === "search") {
      const query = stringValue(args.query);
      return query ? `Searching ${query}...` : "Searching workspace...";
    }
    if (action === "bash") return "Running command...";
    if (action) return `Calling ${action.replace(/_/g, " ")}...`;
    return null;
  }

  function inspectMessage(message) {
    if (!activeObserverId || !message || typeof message !== "object") return;
    if (message?.metadata?.is_visually_hidden_from_conversation === true) return;

    const id = messageId(message);
    const role = stringValue(message?.author?.role) || "";
    const recipient = stringValue(message.recipient) || "all";
    const contentType = stringValue(message?.content?.content_type) || "";
    const exchange = turnExchangeId(message);

    if (role === "assistant" && recipient === "all" && message?.metadata?.is_thinking_preamble_message === true) {
      const text = messageText(message).trim();
      if (text) pendingThinking.set(id, { text, turnExchangeId: exchange });
      return;
    }

    if (role === "assistant" && recipient === "all" && contentType === "reasoning_recap") {
      flushPendingThinking(id);
      const text = messageText(message).trim();
      if (text && completedMessage(message) && !emittedMessageIds.has(id)) {
        emittedMessageIds.add(id);
        emit({
          type: "passive_thinking_block",
          message_id: id,
          text,
          turn_exchange_id: exchange,
        });
      }
      return;
    }

    if (role === "assistant" && recipient !== "all") {
      flushPendingThinking(null);
      if (emittedMessageIds.has(id)) return;
      const label = friendlyToolLabel(message, recipient);
      if (!label && !completedMessage(message)) return;
      emittedMessageIds.add(id);
      emit({
        type: "passive_tool_call",
        message_id: id,
        tool_name: recipient,
        label: label || "Using tool...",
        turn_exchange_id: exchange,
      });
      return;
    }

    if (role === "assistant" && recipient === "all" && contentType === "text" && message?.metadata?.is_thinking_preamble_message !== true) {
      flushPendingThinking(null);
      const finish = finishReason(message);
      if (message?.end_turn === true || finish) {
        emit({
          type: "passive_turn_terminal",
          message_id: id,
          turn_exchange_id: exchange,
          finish_reason: finish,
        });
      }
      return;
    }

    if (role === "tool" || role === "assistant") flushPendingThinking(null);
  }

  function collectMessages(value, output, depth = 0) {
    if (value == null || depth > 7) return;
    if (Array.isArray(value)) {
      for (const item of value.slice(0, 128)) collectMessages(item, output, depth + 1);
      return;
    }
    if (typeof value !== "object") return;
    if (value.author && value.content) output.push(value);
    for (const key of ["message", "messages", "data", "result", "payload", "turn", "v", "value"]) {
      if (Object.prototype.hasOwnProperty.call(value, key)) collectMessages(value[key], output, depth + 1);
    }
  }

  function inspectIdentity(value, context, depth = 0) {
    if (value == null || depth > 7) return;
    if (Array.isArray(value)) {
      for (const item of value.slice(0, 128)) inspectIdentity(item, context, depth + 1);
      return;
    }
    if (typeof value !== "object") return;
    if (value.type === "stream_handoff") {
      context.conversationId = stringValue(value.conversation_id) || context.conversationId;
      context.turnExchangeId = stringValue(value.turn_exchange_id) || context.turnExchangeId;
    }
    for (const key of ["message", "messages", "data", "result", "payload", "turn", "v", "value"]) {
      if (Object.prototype.hasOwnProperty.call(value, key)) inspectIdentity(value[key], context, depth + 1);
    }
  }

  function selectPatchMessage(message) {
    if (!message || typeof message !== "object") return;
    currentPatchMessage = {
      ...message,
      id: stringValue(message.id) || `passive-patch-${++syntheticId}`,
      content: message.content && typeof message.content === "object"
        ? { ...message.content, parts: Array.isArray(message.content.parts) ? [...message.content.parts] : [] }
        : { content_type: "text", parts: [] },
      metadata: message.metadata && typeof message.metadata === "object" ? { ...message.metadata } : {},
    };
    inspectMessage(currentPatchMessage);
  }

  function applyPatchItem(item) {
    if (!item || typeof item !== "object") return;
    const path = typeof item.p === "string" ? item.p : null;
    const value = item.v;
    if (value && typeof value === "object" && !Array.isArray(value) && value.message) {
      selectPatchMessage(value.message);
      return;
    }
    if (!currentPatchMessage) return;

    if ((path === null || path === "/message/content/parts/0") && typeof value === "string") {
      const parts = Array.isArray(currentPatchMessage.content?.parts)
        ? [...currentPatchMessage.content.parts]
        : [];
      const previous = typeof parts[0] === "string" ? parts[0] : "";
      parts[0] = previous + value;
      currentPatchMessage.content = { ...(currentPatchMessage.content || {}), parts };
      inspectMessage(currentPatchMessage);
    } else if (path === "/message/content" && value && typeof value === "object") {
      currentPatchMessage.content = { ...value };
      inspectMessage(currentPatchMessage);
    } else if (path === "/message/status") {
      currentPatchMessage.status = value;
      inspectMessage(currentPatchMessage);
    } else if (path === "/message/end_turn") {
      currentPatchMessage.end_turn = value;
      inspectMessage(currentPatchMessage);
    } else if (path === "/message/metadata" && value && typeof value === "object") {
      currentPatchMessage.metadata = { ...(currentPatchMessage.metadata || {}), ...value };
      inspectMessage(currentPatchMessage);
    }
  }

  function processPayload(payload, context) {
    inspectIdentity(payload, context);
    const messages = [];
    collectMessages(payload, messages);
    const seen = new Set();
    for (const message of messages) {
      if (seen.has(message)) continue;
      seen.add(message);
      inspectMessage(message);
    }
    applyPatchItem(payload);
    if (Array.isArray(payload?.v)) {
      for (const item of payload.v.slice(0, 128)) applyPatchItem(item);
    }
  }

  function isConversationWrite(url, method) {
    if (String(method || "GET").toUpperCase() !== "POST") return false;
    try {
      const parsed = new URL(url, location.href);
      if (parsed.origin !== location.origin) return false;
      const path = parsed.pathname.replace(/\/+$/, "");
      return path.endsWith("/backend-api/conversation") || path.endsWith("/backend-api/f/conversation");
    } catch {
      return false;
    }
  }

  async function observeResponse(response, observerId) {
    if (!response?.body || !observerId) return;
    emit({ type: "passive_stream_started" });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const context = { conversationId: null, turnExchangeId: null };
    let buffer = "";
    try {
      while (activeObserverId === observerId) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        if (buffer.length > 1_000_000) buffer = buffer.slice(-1_000_000);
        while (true) {
          const match = /\r?\n\r?\n/.exec(buffer);
          if (!match) break;
          const block = buffer.slice(0, match.index);
          buffer = buffer.slice(match.index + match[0].length);
          const data = block.split(/\r?\n/)
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trimStart())
            .join("\n")
            .trim();
          if (!data || data === "[DONE]") continue;
          let payload;
          try { payload = JSON.parse(data); } catch { continue; }
          processPayload(payload, context);
        }
      }
    } catch {
      // Passive observation must never perturb the page request.
    } finally {
      try { reader.releaseLock(); } catch {}
    }
  }

  window.fetch = async function cwaPassiveFetch(input, init) {
    const response = await originalFetch(input, init);
    const observerId = activeObserverId;
    if (!observerId) return response;
    let url = "";
    let method = "GET";
    try {
      if (input instanceof Request) {
        url = input.url;
        method = init?.method || input.method;
      } else {
        url = String(input || "");
        method = init?.method || "GET";
      }
    } catch {}
    if (!isConversationWrite(url, method)) return response;
    try {
      const clone = response.clone();
      void observeResponse(clone, observerId);
    } catch {
      // Clone failure is observation-only.
    }
    return response;
  };

  window.addEventListener("message", (event) => {
    if (event.source !== window || event.origin !== location.origin) return;
    const data = event.data;
    if (!data || data.channel !== CHANNEL || data.direction !== "control") return;
    const observerId = stringValue(data.observerId);
    if (data.action === "arm" && observerId) {
      activeObserverId = observerId;
      currentPatchMessage = null;
      emittedMessageIds.clear();
      pendingThinking.clear();
    } else if (data.action === "disarm" && (!observerId || observerId === activeObserverId)) {
      activeObserverId = null;
      currentPatchMessage = null;
      emittedMessageIds.clear();
      pendingThinking.clear();
    }
  });
})();
