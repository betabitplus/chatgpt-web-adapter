// Browser-runtime Stop generating domain.
//
// A new-chat turn can temporarily use a WEB:* route before ChatGPT commits the
// real /c/<conversation-id> route. Stop waits for both that committed route and
// the real Stop control before clicking, so a stopped first turn remains resumable.
// After a successful click it also completes the matching passive-observer session:
// explicit user Stop is a terminal turn boundary even when the cloned page stream
// does not emit its normal terminal marker. No ChatGPT network polling happens here.

function _cwaCommittedStopConversationId(value) {
  return typeof value === "string" && value && !value.startsWith("WEB:")
    ? value
    : null;
}

function _cwaStopConversationIdentityVerified(
  requestedConversationId,
  committedConversationId,
  finalConversationId,
  resolvedConversationId
) {
  const routeConversationId = finalConversationId || committedConversationId;
  if (!routeConversationId || !resolvedConversationId) return false;
  if (requestedConversationId !== null) {
    return (
      routeConversationId === requestedConversationId &&
      resolvedConversationId === requestedConversationId
    );
  }
  return resolvedConversationId === routeConversationId;
}

async function _cwaClickStopControl(tabId) {
  try {
    return await chrome.tabs.sendMessage(tabId, { type: "cwa_stop_generation" });
  } catch (error) {
    return {
      ok: false,
      stopped: false,
      reason: error instanceof Error ? error.message : String(error),
    };
  }
}

function _cwaCompletePassiveObserverForStop(tabId, conversationId) {
  if (
    typeof _cwaPassiveSessions === "undefined" ||
    typeof _cwaPassiveAcceptEvent !== "function"
  ) return;

  for (const session of _cwaPassiveSessions.values()) {
    if (session?.tabId !== tabId) continue;
    _cwaPassiveAcceptEvent(session, {
      type: "passive_turn_terminal",
      message_id: null,
      finish_reason: "stopped",
      source: "explicit_stop_generation",
      turn_exchange_id: session.turnExchangeId || null,
      conversation_id: conversationId || session.conversationId || null,
    });
  }
}

async function _cwaExecuteStopGeneration(message) {
  const storedId = await storedRuntimeTabId();
  if (!Number.isInteger(storedId)) {
    return { ok: false, error: "CHATGPT_RUNTIME_TAB_MISSING" };
  }

  const requestedConversationId = typeof message.conversationId === "string" && message.conversationId.trim()
    ? message.conversationId.trim()
    : null;
  const timeoutMs = Number.isFinite(message.timeoutMs)
    ? Math.max(250, Math.min(Number(message.timeoutMs), 30_000))
    : 30_000;
  const deadline = Date.now() + timeoutMs;
  let lastReason = "stop_control_not_visible";
  let committedConversationId = null;

  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(storedId);
    if (!isChatGPTUrl(tab.url || "")) {
      return { ok: false, error: "CHATGPT_RUNTIME_TAB_INVALID" };
    }

    committedConversationId = _cwaCommittedStopConversationId(
      conversationIdFromUrl(tab.url || "")
    );
    if (
      requestedConversationId !== null &&
      committedConversationId !== null &&
      committedConversationId !== requestedConversationId
    ) {
      return {
        ok: false,
        error: "CHATGPT_STOP_CONVERSATION_MISMATCH",
        conversationId: committedConversationId,
        tabId: storedId,
      };
    }

    const routeReady = requestedConversationId !== null
      ? committedConversationId === requestedConversationId
      : committedConversationId !== null;
    if (!routeReady) {
      lastReason = "conversation_route_not_committed";
      await new Promise((resolve) => setTimeout(resolve, 100));
      continue;
    }

    const result = await _cwaClickStopControl(storedId);
    if (result?.stopped === true) {
      const finalTab = await chrome.tabs.get(storedId);
      const finalConversationId = _cwaCommittedStopConversationId(
        conversationIdFromUrl(finalTab.url || "")
      );
      const resolvedConversationId =
        finalConversationId || committedConversationId || requestedConversationId;
      const conversationIdentityVerified = _cwaStopConversationIdentityVerified(
        requestedConversationId,
        committedConversationId,
        finalConversationId,
        resolvedConversationId
      );
      _cwaCompletePassiveObserverForStop(
        storedId,
        conversationIdentityVerified ? resolvedConversationId : null
      );
      return {
        ok: true,
        stopped: true,
        reason: conversationIdentityVerified ? null : "conversation_route_unresolved",
        conversationId: resolvedConversationId,
        tabId: storedId,
        proof: "browser_stop_control",
        conversationIdentityVerified,
      };
    }
    if (typeof result?.reason === "string" && result.reason) {
      lastReason = result.reason;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }

  // Honor the explicit Stop intent even if ChatGPT never committed a normal route.
  // Never expose the internal WEB:* route to callers.
  const finalAttempt = await _cwaClickStopControl(storedId);
  let finalConversationId = null;
  if (finalAttempt?.stopped === true) {
    try {
      const finalTab = await chrome.tabs.get(storedId);
      finalConversationId = _cwaCommittedStopConversationId(
        conversationIdFromUrl(finalTab.url || "")
      );
    } catch {}
  }
  const resolvedConversationId =
    finalConversationId || committedConversationId || requestedConversationId;
  const conversationIdentityVerified = finalAttempt?.stopped === true
    ? _cwaStopConversationIdentityVerified(
        requestedConversationId,
        committedConversationId,
        finalConversationId,
        resolvedConversationId
      )
    : false;
  if (finalAttempt?.stopped === true) {
    _cwaCompletePassiveObserverForStop(
      storedId,
      conversationIdentityVerified ? resolvedConversationId : null
    );
  }
  return {
    ok: true,
    stopped: finalAttempt?.stopped === true,
    reason: finalAttempt?.stopped === true
      ? (conversationIdentityVerified ? null : "conversation_route_unresolved")
      : (typeof finalAttempt?.reason === "string" && finalAttempt.reason ? finalAttempt.reason : lastReason),
    conversationId: resolvedConversationId,
    tabId: storedId,
    proof: finalAttempt?.stopped === true ? "browser_stop_control" : null,
    conversationIdentityVerified,
  };
}

const _cwaStopPriorOnNativeMessage = onNativeMessage;
onNativeMessage = async function _cwaOnNativeMessageWithStopGeneration(message, port) {
  if (
    message?.protocol !== BRIDGE_PROTOCOL_VERSION ||
    message?.type !== "stop_generation"
  ) {
    return _cwaStopPriorOnNativeMessage(message, port);
  }

  const requestId = message.request_id;
  if (typeof requestId !== "string" || !requestId) return;
  try {
    const result = await _cwaExecuteStopGeneration(message);
    safePortPost(port, {
      protocol: BRIDGE_PROTOCOL_VERSION,
      type: "stop_generation_result",
      request_id: requestId,
      ...result,
    });
  } catch (error) {
    safePortPost(port, {
      protocol: BRIDGE_PROTOCOL_VERSION,
      type: "stop_generation_result",
      request_id: requestId,
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    });
  }
};
