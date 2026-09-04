const _cwaCanonicalPriorOnNativeMessage = onNativeMessage;
const CWA_CANONICAL_CHUNK_BASE64_CHARS = 600_000;
const CWA_CANONICAL_READ_TAB_KEY = "browserNativeCanonicalReadTabIdV1";
const CWA_CANONICAL_READ_URL = `${CHATGPT_ORIGIN}/robots.txt`;

function _cwaCanonicalConversationId(value) {
  const conversationId = typeof value === "string" ? value.trim() : "";
  if (
    !conversationId ||
    conversationId.includes("/") ||
    conversationId.includes("?") ||
    conversationId.includes("#")
  ) {
    throw new Error("CANONICAL_READ_CONVERSATION_ID_REQUIRED");
  }
  return conversationId;
}

function _cwaCanonicalStableReason(error) {
  const message = error instanceof Error ? error.message : String(error || "");
  return /^[A-Z0-9_]+$/.test(message)
    ? message
    : "CANONICAL_READ_BROWSER_ERROR";
}

async function _cwaCanonicalPruneOrphanedChatGPTTabs(canonicalTabId) {
  const runtimeTabId = await storedRuntimeTabId();
  const keep = new Set(
    [runtimeTabId, canonicalTabId].filter((tabId) => Number.isInteger(tabId))
  );
  const tabs = await chrome.tabs.query({ url: `${CHATGPT_ORIGIN}/*` });
  const orphanIds = tabs
    .map((tab) => tab?.id)
    .filter((tabId) => Number.isInteger(tabId) && !keep.has(tabId));
  if (orphanIds.length > 0) {
    try {
      await chrome.tabs.remove(orphanIds);
    } catch {}
  }
}

async function _cwaCanonicalRuntimeTab() {
  const stored = await chrome.storage.local.get(CWA_CANONICAL_READ_TAB_KEY);
  const storedId = stored?.[CWA_CANONICAL_READ_TAB_KEY];
  if (Number.isInteger(storedId)) {
    try {
      let tab = await chrome.tabs.get(storedId);
      if (isChatGPTUrl(tab?.url || "")) {
        if (tab.url !== CWA_CANONICAL_READ_URL) {
          tab = await chrome.tabs.update(storedId, {
            url: CWA_CANONICAL_READ_URL,
            active: false,
          });
        }
        const ready = tab.status === "complete" ? tab : await waitForTabComplete(storedId);
        await _cwaCanonicalPruneOrphanedChatGPTTabs(storedId);
        return ready;
      }
    } catch {
      await chrome.storage.local.remove(CWA_CANONICAL_READ_TAB_KEY);
    }
  }

  const tab = await chrome.tabs.create({ url: CWA_CANONICAL_READ_URL, active: false });
  if (!Number.isInteger(tab?.id)) {
    throw new Error("CANONICAL_READ_RUNTIME_TAB_CREATE_FAILED");
  }
  await chrome.storage.local.set({ [CWA_CANONICAL_READ_TAB_KEY]: tab.id });
  const ready = await waitForTabComplete(tab.id);
  await _cwaCanonicalPruneOrphanedChatGPTTabs(tab.id);
  return ready;
}

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const stored = await chrome.storage.local.get(CWA_CANONICAL_READ_TAB_KEY);
  if (stored?.[CWA_CANONICAL_READ_TAB_KEY] === tabId) {
    await chrome.storage.local.remove(CWA_CANONICAL_READ_TAB_KEY);
  }
});

async function _cwaCanonicalFetch(tabId, conversationId, timeoutMs) {
  const debuggee = { tabId };
  const endpoint = `${CHATGPT_ORIGIN}/backend-api/conversation/${encodeURIComponent(conversationId)}`;
  const expression = `(async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ${JSON.stringify(timeoutMs)});
    try {
      const cacheKey = "__cwaCanonicalAccessTokenV1";
      const cacheTtlMs = 60_000;
      const now = Date.now();
      const cached = globalThis[cacheKey];
      let accessToken = (
        cached &&
        typeof cached.token === "string" &&
        cached.token &&
        Number.isFinite(cached.cachedAtMs) &&
        now - cached.cachedAtMs >= 0 &&
        now - cached.cachedAtMs < cacheTtlMs
      ) ? cached.token : "";

      const refreshAccessToken = async () => {
        const sessionResponse = await fetch("/api/auth/session", {
          method: "GET",
          credentials: "include",
          cache: "no-store",
          headers: { accept: "application/json" },
          signal: controller.signal
        });
        let sessionPayload = null;
        try {
          sessionPayload = await sessionResponse.json();
        } catch {}
        const token = typeof sessionPayload?.accessToken === "string"
          ? sessionPayload.accessToken.trim()
          : "";
        if (!sessionResponse.ok || !token) {
          return {
            ok: false,
            status: sessionResponse.status,
            contentType: (sessionResponse.headers.get("content-type") || "").slice(0, 128),
            token: ""
          };
        }
        globalThis[cacheKey] = { token, cachedAtMs: Date.now() };
        return { ok: true, status: sessionResponse.status, contentType: "", token };
      };

      if (!accessToken) {
        const refreshed = await refreshAccessToken();
        if (!refreshed.ok) {
          return {
            ok: false,
            status: refreshed.status,
            contentType: refreshed.contentType,
            reasonCode: "CANONICAL_READ_AUTHENTICATION_REQUIRED",
            retryable: false
          };
        }
        accessToken = refreshed.token;
      }

      const fetchConversation = (token) => fetch(${JSON.stringify(endpoint)}, {
        method: "GET",
        credentials: "include",
        cache: "no-store",
        headers: {
          accept: "application/json",
          authorization: "Bearer " + token
        },
        signal: controller.signal
      });

      let response = await fetchConversation(accessToken);
      if ((response.status === 401 || response.status === 403) && globalThis[cacheKey]) {
        delete globalThis[cacheKey];
        const refreshed = await refreshAccessToken();
        if (refreshed.ok) {
          accessToken = refreshed.token;
          response = await fetchConversation(accessToken);
        }
      }
      const contentType = (response.headers.get("content-type") || "").slice(0, 128);
      if (!response.ok) {
        const reasonCode = response.status === 404
          ? "CANONICAL_READ_NOT_VISIBLE"
          : response.status === 401
            ? "CANONICAL_READ_AUTHENTICATION_REQUIRED"
            : response.status === 403
              ? "CANONICAL_READ_ACCESS_CHALLENGED"
              : response.status === 429
                ? "CANONICAL_READ_RATE_LIMITED"
                : "CANONICAL_READ_HTTP_ERROR";
        return {
          ok: false,
          status: response.status,
          contentType,
          reasonCode,
          retryable: response.status === 404 || response.status === 429
        };
      }
      if (!contentType.toLowerCase().includes("json")) {
        return {
          ok: false,
          status: response.status,
          contentType,
          reasonCode: "CANONICAL_READ_NON_JSON",
          retryable: false
        };
      }

      const bytes = new Uint8Array(await response.arrayBuffer());
      const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
      const sha256 = Array.from(
        digest,
        (value) => value.toString(16).padStart(2, "0")
      ).join("");
      let bodyBase64 = "";
      const binaryBlockBytes = 24_576;
      for (let offset = 0; offset < bytes.length; offset += binaryBlockBytes) {
        const block = bytes.subarray(offset, offset + binaryBlockBytes);
        let binary = "";
        for (let index = 0; index < block.length; index += 1) {
          binary += String.fromCharCode(block[index]);
        }
        bodyBase64 += btoa(binary);
      }
      return {
        ok: true,
        status: response.status,
        contentType,
        totalBytes: bytes.length,
        sha256,
        bodyBase64
      };
    } catch (error) {
      return {
        ok: false,
        status: null,
        contentType: null,
        reasonCode: error?.name === "AbortError"
          ? "CANONICAL_READ_TIMEOUT"
          : "CANONICAL_READ_NETWORK_ERROR",
        retryable: false
      };
    } finally {
      clearTimeout(timer);
    }
  })()`;

  let attached = false;
  try {
    await chrome.debugger.attach(debuggee, CDP_PROTOCOL_VERSION);
    attached = true;
    const result = await chrome.debugger.sendCommand(debuggee, "Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true
    });
    if (result?.exceptionDetails) {
      throw new Error("CANONICAL_READ_RUNTIME_EVALUATION_FAILED");
    }
    const value = result?.result?.value;
    if (!value || typeof value !== "object") {
      throw new Error("CANONICAL_READ_RUNTIME_RESULT_INVALID");
    }
    return value;
  } finally {
    if (attached) {
      try { await chrome.debugger.detach(debuggee); } catch {}
    }
  }
}

async function _cwaCanonicalRead(message, port) {
  const requestId = message.request_id;
  const conversationId = _cwaCanonicalConversationId(message.conversationId);
  const timeoutMs = Number.isFinite(message.timeoutMs)
    ? Math.max(1_000, Math.min(Number(message.timeoutMs), 120_000))
    : 30_000;
  const leaseId =
    typeof message.browserAuthorityLeaseId === "string" &&
    message.browserAuthorityLeaseId.trim()
      ? message.browserAuthorityLeaseId.trim()
      : null;

  if (leaseId !== null) {
    const storedLeaseId = await _pr88StoredLeaseId();
    if (storedLeaseId !== leaseId) {
      throw new Error("CANONICAL_READ_AUTHORITY_LEASE_MISMATCH");
    }
  }

  const tab = await _cwaCanonicalRuntimeTab();
  if (!Number.isInteger(tab?.id)) {
    throw new Error("CANONICAL_READ_RUNTIME_TAB_REQUIRED");
  }
  const fetched = await _cwaCanonicalFetch(tab.id, conversationId, timeoutMs);
  if (fetched.ok !== true) {
    safePortPost(port, {
      protocol: BRIDGE_PROTOCOL_VERSION,
      type: "canonical_read_result",
      request_id: requestId,
      ok: false,
      reasonCode: fetched.reasonCode,
      status: fetched.status,
      contentType: fetched.contentType,
      retryable: fetched.retryable === true
    });
    return;
  }

  const bodyBase64 = fetched.bodyBase64;
  if (
    typeof bodyBase64 !== "string" ||
    !/^[0-9a-f]{64}$/.test(fetched.sha256 || "")
  ) {
    throw new Error("CANONICAL_READ_TRANSFER_SOURCE_INVALID");
  }
  const chunkCount = Math.max(
    1,
    Math.ceil(bodyBase64.length / CWA_CANONICAL_CHUNK_BASE64_CHARS)
  );

  for (let chunkIndex = 0; chunkIndex < chunkCount; chunkIndex += 1) {
    const data = bodyBase64.slice(
      chunkIndex * CWA_CANONICAL_CHUNK_BASE64_CHARS,
      (chunkIndex + 1) * CWA_CANONICAL_CHUNK_BASE64_CHARS
    );
    if (!safePortPost(port, {
      protocol: BRIDGE_PROTOCOL_VERSION,
      type: "canonical_read_chunk",
      request_id: requestId,
      chunkIndex,
      chunkCount,
      totalBytes: fetched.totalBytes,
      sha256: fetched.sha256,
      data
    })) {
      throw new Error("CANONICAL_READ_CHUNK_DELIVERY_FAILED");
    }
  }

  safePortPost(port, {
    protocol: BRIDGE_PROTOCOL_VERSION,
    type: "canonical_read_result",
    request_id: requestId,
    ok: true,
    status: fetched.status,
    contentType: fetched.contentType,
    chunkCount,
    totalBytes: fetched.totalBytes,
    sha256: fetched.sha256,
    browserAuthorityLeaseId: leaseId,
    runtimeTabId: tab.id
  });
}

onNativeMessage = async function _cwaOnNativeMessageWithCanonicalRead(message, port) {
  if (
    message?.protocol !== BRIDGE_PROTOCOL_VERSION ||
    message?.type !== "canonical_read"
  ) {
    return _cwaCanonicalPriorOnNativeMessage(message, port);
  }
  const requestId = message.request_id;
  if (typeof requestId !== "string" || !requestId) return;
  if (activeRequestId !== null) {
    safePortPost(port, {
      protocol: BRIDGE_PROTOCOL_VERSION,
      type: "canonical_read_result",
      request_id: requestId,
      ok: false,
      reasonCode: "BROWSER_NATIVE_EXTENSION_BUSY",
      retryable: false
    });
    return;
  }

  activeRequestId = requestId;
  try {
    await _cwaCanonicalRead(message, port);
  } catch (error) {
    safePortPost(port, {
      protocol: BRIDGE_PROTOCOL_VERSION,
      type: "canonical_read_result",
      request_id: requestId,
      ok: false,
      reasonCode: _cwaCanonicalStableReason(error),
      retryable: false
    });
  } finally {
    activeRequestId = null;
  }
};
