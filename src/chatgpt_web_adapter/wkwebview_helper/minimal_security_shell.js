// Opt-in WKWebView Phase-A shell. It keeps the protected conversation write in the
// real chatgpt.com browser context while avoiding hydration of the full ChatGPT SPA.
// Only current server-provided bootstrap/security modules are used; integrity values
// stay inside the browser request path, and any unsupported shape fails closed.
(() => {
  let sharedConversationInitializationPromise = null;
  const sharedModelCatalogPromises = new Map();

  const runMinimalSecurityShell = (config = null) => {
  const requestConfig = config && typeof config === "object" ? config : null;
  const requestId = typeof requestConfig?.request_id === "string" ? requestConfig.request_id : "";
  const pageBaseURL = document.baseURI || location.href;
  const tagged = (body) => requestId ? {...body, request_id: requestId} : body;
  const post = (body) => {
    try {
      window.webkit.messageHandlers.cwaCanonical.postMessage(tagged(body));
    } catch (_) {}
  };
  const postStream = (body) => {
    try {
      window.webkit.messageHandlers.cwaStream.postMessage(tagged(body));
    } catch (_) {}
  };
  const postSubmit = (body) => {
    if (!requestId) return;
    try {
      window.webkit.messageHandlers.cwaSubmit.postMessage(tagged(body));
    } catch (_) {}
  };
  const prompt = typeof requestConfig?.prompt === "string"
    ? requestConfig.prompt
    : (typeof window.__CWA_MINIMAL_PROMPT__ === "string" ? window.__CWA_MINIMAL_PROMPT__ : "");
  const profile = typeof requestConfig?.profile === "string"
    ? requestConfig.profile
    : (typeof window.__CWA_MINIMAL_PROFILE__ === "string" ? window.__CWA_MINIMAL_PROFILE__ : "");
  const temporary = requestConfig?.temporary === true || window.__CWA_MINIMAL_TEMPORARY__ === true;
  const conversationId = typeof requestConfig?.conversation_id === "string"
    ? requestConfig.conversation_id
    : (typeof window.__CWA_MINIMAL_CONVERSATION_ID__ === "string"
      ? window.__CWA_MINIMAL_CONVERSATION_ID__
      : "");
  const parentMessageId = typeof requestConfig?.parent_message_id === "string"
    ? requestConfig.parent_message_id
    : (typeof window.__CWA_MINIMAL_PARENT_MESSAGE_ID__ === "string"
      ? window.__CWA_MINIMAL_PARENT_MESSAGE_ID__
      : "");
  const selectedModelSlug = typeof requestConfig?.selected_model_slug === "string"
    ? requestConfig.selected_model_slug
    : (typeof window.__CWA_MINIMAL_SELECTED_MODEL_SLUG__ === "string"
      ? window.__CWA_MINIMAL_SELECTED_MODEL_SLUG__
      : "");
  const selectedThinkingEffort = typeof requestConfig?.selected_thinking_effort === "string"
    ? requestConfig.selected_thinking_effort
    : (typeof window.__CWA_MINIMAL_SELECTED_THINKING_EFFORT__ === "string"
      ? window.__CWA_MINIMAL_SELECTED_THINKING_EFFORT__
      : "");
  const attachmentsBase64 = typeof requestConfig?.attachments_base64 === "string"
    ? requestConfig.attachments_base64
    : (typeof window.__CWA_MINIMAL_ATTACHMENTS_BASE64__ === "string"
      ? window.__CWA_MINIMAL_ATTACHMENTS_BASE64__
      : "");
  const handoffAttemptId = typeof requestConfig?.handoff_attempt_id === "string"
    ? requestConfig.handoff_attempt_id
    : (typeof window.__CWA_MINIMAL_HANDOFF_ATTEMPT_ID__ === "string"
      ? window.__CWA_MINIMAL_HANDOFF_ATTEMPT_ID__
      : "");
  let stage = "start";
  if (!requestId) window.__CWA_MINIMAL_STAGE__ = stage;
  const setStage = (value) => {
    stage = value;
    if (!requestId) window.__CWA_MINIMAL_STAGE__ = value;
  };
  let sharedRuntimeURL = "";
  let integrityDiscoveryCacheKey = "";
  const observeProtectedWriteResponse =
    typeof window.__cwaObserveStreamResponse === "function"
      ? window.__cwaObserveStreamResponse
      : null;

  const matchingBrace = (source, opening) => {
    if (opening < 0 || opening >= source.length || source[opening] !== "{") return null;
    let depth = 0;
    let index = opening;
    let state = "code";
    let quote = "";
    while (index < source.length) {
      const char = source[index];
      const next = index + 1 < source.length ? source[index + 1] : "";
      if (state === "line_comment") {
        if (char === "\r" || char === "\n") state = "code";
        index += 1;
        continue;
      }
      if (state === "block_comment") {
        if (char === "*" && next === "/") {
          state = "code";
          index += 2;
        } else index += 1;
        continue;
      }
      if (state === "string") {
        if (char === "\\") {
          index += 2;
          continue;
        }
        if (char === quote) state = "code";
        index += 1;
        continue;
      }
      if (state === "template") {
        if (char === "\\") {
          index += 2;
          continue;
        }
        if (char === "`") state = "code";
        index += 1;
        continue;
      }
      if (char === "/" && next === "/") {
        state = "line_comment";
        index += 2;
        continue;
      }
      if (char === "/" && next === "*") {
        state = "block_comment";
        index += 2;
        continue;
      }
      if (char === "'" || char === '"') {
        state = "string";
        quote = char;
        index += 1;
        continue;
      }
      if (char === "`") {
        state = "template";
        index += 1;
        continue;
      }
      if (char === "{") depth += 1;
      else if (char === "}") {
        depth -= 1;
        if (depth === 0) return index;
      }
      index += 1;
    }
    return null;
  };

  const escapeRegex = (value) => value.replaceAll("$", "\\x24");
  const exportAlias = (source, internalName) => {
    if (!internalName) return null;
    const expression = new RegExp(
      `\\b${escapeRegex(internalName)}\\s+as\\s+([A-Za-z_$][A-Za-z0-9_$]*)(?=[,};\\s])`,
      "g",
    );
    let alias = null;
    for (const match of source.matchAll(expression)) alias = match[1];
    return alias;
  };

  const integrityMarkers = [
    "chatReq:e,turnstileToken:r,proofToken:l",
    "chatReq:e,turnstileToken:",
    "turnstileToken:r,proofToken:l",
  ];

  const discoverIntegrityNames = (source) => {
    const marker = integrityMarkers.find((candidate) => source.includes(candidate));
    if (!marker) throw new Error("MINIMAL_INTEGRITY_RESULT_MARKER_MISSING");
    const markerIndex = source.indexOf(marker);
    const searchStart = Math.max(0, markerIndex - 40000);
    const prefix = source.slice(searchStart, markerIndex);
    const functionPattern = /function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(/g;
    const candidates = [];
    for (const match of prefix.matchAll(functionPattern)) {
      const absoluteStart = searchStart + match.index;
      const searchFrom = searchStart + match.index + match[0].length;
      const opening = source.indexOf("{", searchFrom);
      if (opening < 0 || opening >= markerIndex || opening - searchFrom > 2000) continue;
      const closing = matchingBrace(source, opening);
      if (closing == null || closing < markerIndex) continue;
      candidates.push({ name: match[1], start: absoluteStart, closing });
    }
    if (!candidates.length) throw new Error("MINIMAL_INTEGRITY_HELPER_DISCOVERY_FAILED");
    candidates.sort((left, right) => left.start - right.start);
    const helper = candidates[0];
    const suffix = source.slice(helper.closing + 1, helper.closing + 12000);
    const initializerMatch =
      /(?:^|[;,}])\s*var\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*e\(\(\(\)\s*=>\s*\{/.exec(suffix) ||
      /(?:^|[;,}])\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*e\(\(\(\)\s*=>\s*\{/.exec(suffix);
    if (!initializerMatch) throw new Error("MINIMAL_INTEGRITY_INITIALIZER_DISCOVERY_FAILED");
    return { helperName: helper.name, initializerName: initializerMatch[1] };
  };

  const conversationTransportMarkers = [
    "retryConfig:{MIN_RETRY_INTERVAL",
    "shouldRetry:t=()=>!0,retryConfig:",
  ];

  const initializerAfter = (source, offset, maxDistance = 16000) => {
    const suffix = source.slice(offset, offset + maxDistance);
    const match =
      /(?:^|[;,}])\s*var\s+(?:[A-Za-z_$][A-Za-z0-9_$]*\s*,\s*)*([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*e\(\(\(\)\s*=>\s*\{/.exec(suffix) ||
      /(?:^|[;,}])\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*e\(\(\(\)\s*=>\s*\{/.exec(suffix);
    return match ? match[1] : null;
  };

  const discoverConversationTransportNames = (source) => {
    const marker = conversationTransportMarkers.find((candidate) => source.includes(candidate));
    if (!marker) throw new Error("MINIMAL_CONVERSATION_TRANSPORT_MARKER_MISSING");
    const markerIndex = source.indexOf(marker);
    const searchStart = Math.max(0, markerIndex - 7000);
    const prefix = source.slice(searchStart, markerIndex);
    const functionPattern = /async\s+function\*\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\(/g;
    let transportName = null;
    let transportStart = -1;
    for (const match of prefix.matchAll(functionPattern)) {
      transportName = match[1];
      transportStart = searchStart + match.index;
    }
    if (!transportName || transportStart < 0) {
      throw new Error("MINIMAL_CONVERSATION_TRANSPORT_DISCOVERY_FAILED");
    }
    const opening = source.indexOf("{", transportStart);
    if (opening < 0 || opening >= markerIndex || opening - transportStart > 2000) {
      throw new Error("MINIMAL_CONVERSATION_TRANSPORT_BODY_MISSING");
    }
    const closing = matchingBrace(source, opening);
    if (closing == null) throw new Error("MINIMAL_CONVERSATION_TRANSPORT_BODY_INCOMPLETE");
    const initializerName = initializerAfter(source, closing + 1);
    if (!initializerName) {
      throw new Error("MINIMAL_CONVERSATION_TRANSPORT_INITIALIZER_DISCOVERY_FAILED");
    }
    return { transportName, initializerName };
  };

  const discoverSharedRuntimeCandidate = (source) => {
    const importPattern = /import\{([\s\S]*?)\}from[\"']([^\"']+)[\"']/g;
    let selected = null;
    for (const match of source.matchAll(importPattern)) {
      const clause = match[1];
      const modulePath = match[2];
      if (!modulePath.startsWith("./") || !modulePath.includes(".js")) {
        continue;
      }
      if (!selected || clause.length > selected.clauseLength) {
        selected = { modulePath, clauseLength: clause.length };
      }
    }
    return selected;
  };

  const discoverSharedRuntimePath = (source) =>
    discoverSharedRuntimeCandidate(source)?.modulePath ?? null;

  const discoverSharedRequestClientExports = (source) => {
    const marker = "safePost(e,...t){return this.maybeJsonRequest";
    const markerIndex = source.indexOf(marker);
    if (markerIndex < 0) throw new Error("MINIMAL_REQUEST_CLIENT_MARKER_MISSING");
    const searchStart = Math.max(0, markerIndex - 20000);
    const prefix = source.slice(searchStart, markerIndex);
    const initializerPattern =
      /var\s+(?:[A-Za-z_$][A-Za-z0-9_$]*\s*,\s*)*([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*e\(\(\(\)\s*=>\s*\{/g;
    let initializerMatch = null;
    let initializerOpening = -1;
    let initializerClosing = null;
    for (const match of prefix.matchAll(initializerPattern)) {
      const initializerStart = searchStart + match.index;
      const opening = initializerStart + match[0].lastIndexOf("{");
      const closing = matchingBrace(source, opening);
      if (closing != null && closing >= markerIndex) {
        initializerMatch = match;
        initializerOpening = opening;
        initializerClosing = closing;
      }
    }
    if (!initializerMatch || initializerOpening < 0 || initializerClosing == null) {
      throw new Error("MINIMAL_REQUEST_CLIENT_INITIALIZER_DISCOVERY_FAILED");
    }
    const initializerName = initializerMatch[1];
    const opening = initializerOpening;
    const closing = initializerClosing;
    const block = source.slice(opening, closing + 1);
    const clientMatch =
      /([A-Za-z_$][A-Za-z0-9_$]*)=new\s+[A-Za-z_$][A-Za-z0-9_$]*\(\{includeBusinessAgentPreviewSession:!0,baseUrl:[A-Za-z_$][A-Za-z0-9_$]*,basePathForTargetHeaders:void 0,targetRoutesAreCanonical:!0\}\)/.exec(block);
    if (!clientMatch) throw new Error("MINIMAL_REQUEST_CLIENT_SINGLETON_DISCOVERY_FAILED");
    const clientName = clientMatch[1];
    const initializerExport = exportAlias(source, initializerName);
    const clientExport = exportAlias(source, clientName);
    if (!initializerExport || !clientExport) {
      throw new Error("MINIMAL_REQUEST_CLIENT_EXPORT_DISCOVERY_FAILED");
    }
    return { initializerExport, clientExport };
  };

  const discoverIntegrityExports = (source) => {
    const names = discoverIntegrityNames(source);
    const transportNames = discoverConversationTransportNames(source);
    const sharedRuntimePath = discoverSharedRuntimePath(source);
    const helperExport = exportAlias(source, names.helperName);
    const initializerExport = exportAlias(source, names.initializerName);
    const transportExport = exportAlias(source, transportNames.transportName);
    const transportInitializerExport = exportAlias(
      source,
      transportNames.initializerName,
    );
    if (
      !helperExport
      || !initializerExport
      || !transportExport
      || !transportInitializerExport
      || !sharedRuntimePath
    ) {
      throw new Error("MINIMAL_INTEGRITY_EXPORT_DISCOVERY_FAILED");
    }
    return {
      helperExport,
      initializerExport,
      transportExport,
      transportInitializerExport,
      sharedRuntimePath,
    };
  };

  const discoverIntegrityExportsStreaming = async (response) => {
    if (!response.body || typeof response.body.getReader !== "function") {
      return discoverIntegrityExports(await response.text());
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let prefixCarry = "";
    let discoveryWindow = "";
    let markerSeen = false;
    let names = null;
    let transportPrefixCarry = "";
    let transportDiscoveryWindow = "";
    let transportMarkerSeen = false;
    let transportNames = null;
    let sharedImportCarry = "";
    let sharedRuntimeCandidate = null;
    let aliasCarry = "";
    let helperExport = null;
    let initializerExport = null;
    let transportExport = null;
    let transportInitializerExport = null;

    const scanAliases = (source) => {
      if (names) {
        if (!helperExport) helperExport = exportAlias(source, names.helperName);
        if (!initializerExport) initializerExport = exportAlias(source, names.initializerName);
      }
      if (transportNames) {
        if (!transportExport) {
          transportExport = exportAlias(source, transportNames.transportName);
        }
        if (!transportInitializerExport) {
          transportInitializerExport = exportAlias(
            source,
            transportNames.initializerName,
          );
        }
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      const chunk = done ? decoder.decode() : decoder.decode(value, { stream: true });

      {
        const combined = sharedImportCarry + chunk;
        const candidate = discoverSharedRuntimeCandidate(combined);
        if (
          candidate
          && (
            sharedRuntimeCandidate === null
            || candidate.clauseLength > sharedRuntimeCandidate.clauseLength
          )
        ) {
          sharedRuntimeCandidate = candidate;
        }
        sharedImportCarry = combined.slice(-32000);
      }

      if (!names) {
        if (!markerSeen) {
          const combined = prefixCarry + chunk;
          const marker = integrityMarkers.find((candidate) => combined.includes(candidate));
          if (marker) {
            markerSeen = true;
            const markerIndex = combined.indexOf(marker);
            discoveryWindow = combined.slice(Math.max(0, markerIndex - 45000));
          } else {
            prefixCarry = combined.slice(-45000);
          }
        } else {
          discoveryWindow += chunk;
        }
        if (markerSeen) {
          try {
            names = discoverIntegrityNames(discoveryWindow);
          } catch (error) {
            if (discoveryWindow.length > 140000) throw error;
          }
          if (names) {
            scanAliases(discoveryWindow);
            aliasCarry = discoveryWindow.slice(-4096);
            discoveryWindow = "";
          }
        }
      }

      if (!transportNames) {
        if (!transportMarkerSeen) {
          const combined = transportPrefixCarry + chunk;
          const marker = conversationTransportMarkers.find(
            (candidate) => combined.includes(candidate),
          );
          if (marker) {
            transportMarkerSeen = true;
            const markerIndex = combined.indexOf(marker);
            transportDiscoveryWindow = combined.slice(Math.max(0, markerIndex - 7000));
          } else {
            transportPrefixCarry = combined.slice(-7000);
          }
        } else {
          transportDiscoveryWindow += chunk;
        }
        if (transportMarkerSeen) {
          try {
            transportNames = discoverConversationTransportNames(
              transportDiscoveryWindow,
            );
          } catch (error) {
            if (transportDiscoveryWindow.length > 60000) throw error;
          }
          if (transportNames) {
            scanAliases(transportDiscoveryWindow);
            transportDiscoveryWindow = "";
          }
        }
      }

      if (names || transportNames) {
        const combined = aliasCarry + chunk;
        scanAliases(combined);
        aliasCarry = combined.slice(-4096);
      }

      if (
        names
        && transportNames
        && helperExport
        && initializerExport
        && transportExport
        && transportInitializerExport
        && sharedRuntimeCandidate
      ) {
        try {
          await reader.cancel();
        } catch (_) {}
        return {
          helperExport,
          initializerExport,
          transportExport,
          transportInitializerExport,
          sharedRuntimePath: sharedRuntimeCandidate.modulePath,
        };
      }
      if (done) break;
    }
    throw new Error("MINIMAL_INTEGRITY_STREAM_DISCOVERY_FAILED");
  };

  const decodeAttachmentDescriptors = () => {
    if (!attachmentsBase64) return [];
    let descriptors;
    try {
      const raw = atob(attachmentsBase64);
      const bytes = new Uint8Array(raw.length);
      for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
      descriptors = JSON.parse(new TextDecoder().decode(bytes));
      if (!Array.isArray(descriptors)) throw new Error("not_array");
    } catch (_) {
      throw new Error("MINIMAL_ATTACHMENT_DESCRIPTOR_DECODE_FAILED");
    }
    for (const descriptor of descriptors) {
      if (!descriptor || typeof descriptor.file_id !== "string" || !descriptor.file_id) {
        throw new Error("MINIMAL_ATTACHMENT_DESCRIPTOR_INVALID");
      }
    }
    return descriptors;
  };

  const waitForSentinel = async () => {
    for (let i = 0; i < 80 && !window.__cwaSentinelLoaded; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (!window.SentinelSDK) throw new Error("MINIMAL_SENTINEL_SDK_MISSING");
  };

  const bootstrapProductResources = async () => {
    setStage("root");
    const rootResponse = await fetch("/", {
      credentials: "include",
      cache: "no-store",
      headers: { accept: "text/html" },
    });
    if (!rootResponse.ok) throw new Error(`MINIMAL_ROOT_HTTP_${rootResponse.status}`);
    const rootText = await rootResponse.text();
    const parsed = new DOMParser().parseFromString(rootText, "text/html");
    const bootstrap = parsed.getElementById("client-bootstrap");
    if (!bootstrap) throw new Error("MINIMAL_CLIENT_BOOTSTRAP_MISSING");
    const localBootstrap = document.createElement("script");
    localBootstrap.type = "application/json";
    localBootstrap.id = "client-bootstrap";
    localBootstrap.textContent = bootstrap.textContent || "";
    document.head.appendChild(localBootstrap);

    const productAssets = [...parsed.querySelectorAll("script[src],link[href]")]
      .map((element) => element.getAttribute("src") || element.getAttribute("href") || "");
    const integrityAsset = productAssets.find(
      (value) => value.includes("/conversation-small-") && value.includes(".js"),
    );
    if (!integrityAsset) throw new Error("MINIMAL_INTEGRITY_ASSET_MISSING");
    return new URL(integrityAsset, pageBaseURL).href;
  };

  const loadIntegrityRuntime = async (integrityURL) => {
    setStage("integrity_discovery");
    const integrityCacheKey = `__cwa_integrity_exports_v5:${integrityURL}`;
    integrityDiscoveryCacheKey = integrityCacheKey;
    let integrityExports = null;
    try {
      const cached = JSON.parse(localStorage.getItem(integrityCacheKey) || "null");
      if (
        cached
        && typeof cached.helperExport === "string"
        && cached.helperExport
        && typeof cached.initializerExport === "string"
        && cached.initializerExport
        && typeof cached.transportExport === "string"
        && cached.transportExport
        && typeof cached.transportInitializerExport === "string"
        && cached.transportInitializerExport
        && typeof cached.sharedRuntimePath === "string"
        && cached.sharedRuntimePath
      ) {
        integrityExports = cached;
      }
    } catch (_) {}
    if (!integrityExports) {
      const integritySourceResponse = await fetch(integrityURL, {
        credentials: "include",
        cache: "no-store",
      });
      if (!integritySourceResponse.ok) {
        throw new Error(`MINIMAL_INTEGRITY_SOURCE_HTTP_${integritySourceResponse.status}`);
      }
      integrityExports = await discoverIntegrityExportsStreaming(integritySourceResponse);
      try {
        localStorage.setItem(integrityCacheKey, JSON.stringify(integrityExports));
      } catch (_) {}
    }
    sharedRuntimeURL = new URL(integrityExports.sharedRuntimePath, integrityURL).href;
    setStage("integrity_import");
    const integrityModule = await import(integrityURL);
    const initializeIntegrity = integrityModule[integrityExports.initializerExport];
    const acquireIntegrity = integrityModule[integrityExports.helperExport];
    const officialConversationTransport =
      integrityModule[integrityExports.transportExport];
    const initializeConversationTransport =
      integrityModule[integrityExports.transportInitializerExport];
    if (
      typeof initializeIntegrity !== "function"
      || typeof acquireIntegrity !== "function"
      || typeof officialConversationTransport !== "function"
      || typeof initializeConversationTransport !== "function"
    ) {
      throw new Error("MINIMAL_INTEGRITY_EXPORTS_MISSING");
    }
    setStage("integrity_init");
    initializeIntegrity();
    return Object.freeze({
      acquireIntegrity,
      officialConversationTransport,
      initializeConversationTransport,
    });
  };

  const loadOfficialApiClient = async () => {
    if (!sharedRuntimeURL) throw new Error("MINIMAL_SHARED_RUNTIME_URL_MISSING");
    const cacheKey = "__cwa_request_client_exports_v1:" + sharedRuntimeURL;
    let exports = null;
    try {
      const cached = JSON.parse(localStorage.getItem(cacheKey) || "null");
      if (
        cached
        && typeof cached.initializerExport === "string"
        && cached.initializerExport
        && typeof cached.clientExport === "string"
        && cached.clientExport
      ) {
        exports = cached;
      }
    } catch (_) {}

    if (!exports) {
      setStage("request_client_discovery");
      const sourceResponse = await fetch(sharedRuntimeURL, {
        credentials: "include",
        cache: "force-cache",
      });
      if (!sourceResponse.ok) {
        throw new Error(`MINIMAL_SHARED_RUNTIME_SOURCE_HTTP_${sourceResponse.status}`);
      }
      const sharedSourceText = await sourceResponse.text();
      try {
        exports = discoverSharedRequestClientExports(sharedSourceText);
      } catch (error) {
        try {
          if (integrityDiscoveryCacheKey) {
            localStorage.removeItem(integrityDiscoveryCacheKey);
          }
        } catch (_) {}
        throw error;
      }
      try {
        localStorage.setItem(cacheKey, JSON.stringify(exports));
      } catch (_) {}
    }

    setStage("request_client_import");
    const sharedModule = await import(sharedRuntimeURL);
    const initializeRequestClient = sharedModule[exports.initializerExport];
    if (typeof initializeRequestClient !== "function") {
      throw new Error("MINIMAL_OFFICIAL_API_CLIENT_INITIALIZER_MISSING");
    }
    initializeRequestClient();
    const requestClient = sharedModule[exports.clientExport];
    if (
      !requestClient
      || typeof requestClient.safePost !== "function"
      || typeof requestClient.safeGet !== "function"
    ) {
      throw new Error("MINIMAL_OFFICIAL_API_CLIENT_MISSING");
    }
    return Object.freeze({ requestClient, sharedModule });
  };

  const loadSession = async () => {
    setStage("session");
    const sessionResponse = await fetch("/api/auth/session", {
      credentials: "include",
      cache: "no-store",
    });
    const session = await sessionResponse.json();
    const accessToken = session && session.accessToken;
    if (!accessToken) throw new Error("MINIMAL_ACCESS_TOKEN_MISSING");
    return accessToken;
  };

  const loadModelCatalog = async (accessToken) => {
    setStage("models");
    const modelsResponse = await fetch(
      `/backend-api/models?history_and_training_disabled=${temporary ? "true" : "false"}`,
      {
        credentials: "include",
        cache: "no-store",
        headers: { Authorization: `Bearer ${accessToken}` },
      },
    );
    return modelsResponse.json();
  };

  const loadSharedConversationInitialization = async () => {
    if (!sharedConversationInitializationPromise) {
      const candidate = (async () => {
        await waitForSentinel();
        const integrityURL = await bootstrapProductResources();
        const runtime = await loadIntegrityRuntime(integrityURL);
        setStage("conversation_transport_init");
        runtime.initializeConversationTransport();
        const accessToken = await loadSession();
        setStage("request_client_init");
        const loadedApiClient = await loadOfficialApiClient();
        return Object.freeze({
          accessToken,
          acquireIntegrity: runtime.acquireIntegrity,
          officialConversationTransport: runtime.officialConversationTransport,
          officialApiClient: loadedApiClient.requestClient,
        });
      })();
      sharedConversationInitializationPromise = candidate;
      void candidate.catch(() => {
        if (sharedConversationInitializationPromise === candidate) {
          sharedConversationInitializationPromise = null;
        }
      });
    }
    setStage("shared_conversation_init");
    return sharedConversationInitializationPromise;
  };

  const loadSharedModelCatalog = async (accessToken) => {
    const key = temporary ? "temporary" : "normal";
    let promise = sharedModelCatalogPromises.get(key);
    if (!promise) {
      const candidate = loadModelCatalog(accessToken);
      sharedModelCatalogPromises.set(key, candidate);
      void candidate.catch(() => {
        if (sharedModelCatalogPromises.get(key) === candidate) {
          sharedModelCatalogPromises.delete(key);
        }
      });
      promise = candidate;
    } else {
      setStage("models_shared");
    }
    return promise;
  };

  const resolveModelSelection = (modelsPayload) => {
    const models = Array.isArray(modelsPayload.models) ? modelsPayload.models : [];
    const bySlug = new Map(
      models
        .filter((item) => item && typeof item.slug === "string")
        .map((item) => [item.slug, item]),
    );
    const defaultModelSlug = typeof modelsPayload.default_model_slug === "string"
      ? modelsPayload.default_model_slug
      : "";
    const categories = Array.isArray(modelsPayload.categories) ? modelsPayload.categories : [];
    const autoCategory = categories.find(
      (item) => item && typeof item.category === "string" && /_auto$/i.test(item.category)
        && typeof item.default_model === "string",
    );
    const preferredBaseModel =
      defaultModelSlug && defaultModelSlug !== "auto" && bySlug.has(defaultModelSlug)
        ? defaultModelSlug
        : autoCategory && typeof autoCategory.default_model === "string"
          ? autoCategory.default_model
          : defaultModelSlug;
    let model = selectedModelSlug || defaultModelSlug || preferredBaseModel;
    let thinkingEffort = selectedThinkingEffort || null;
    if (profile === "INSTANT") {
      const candidate = preferredBaseModel ? `${preferredBaseModel}-instant` : "";
      if (candidate && bySlug.has(candidate)) model = candidate;
      else {
        const category = categories.find(
          (item) => item && item.human_category_name === "Instant"
            && typeof item.default_model === "string"
            && item.default_model !== defaultModelSlug,
        );
        model = category && typeof category.default_model === "string" ? category.default_model : model;
      }
    } else if (profile === "MEDIUM" || profile === "HIGH") {
      const candidate = preferredBaseModel ? `${preferredBaseModel}-thinking` : "";
      if (candidate && bySlug.has(candidate)) model = candidate;
      else {
        const baseTitle = bySlug.get(preferredBaseModel)?.title;
        const category = categories.find((item) => {
          if (!item || item.human_category_name !== "Thinking" || typeof item.default_model !== "string") {
            return false;
          }
          if (!baseTitle) return true;
          return bySlug.get(item.default_model)?.title === baseTitle;
        });
        model = category && typeof category.default_model === "string" ? category.default_model : model;
      }
      thinkingEffort = profile === "HIGH" ? "extended" : "standard";
      const selected = bySlug.get(model);
      const availableEfforts = selected && Array.isArray(selected.thinking_efforts)
        ? selected.thinking_efforts
            .map((item) => item && item.thinking_effort)
            .filter((value) => typeof value === "string")
        : [];
      if (!availableEfforts.includes(thinkingEffort)) {
        throw new Error("MINIMAL_PROFILE_UNSUPPORTED_BY_MODEL");
      }
    }
    if (!profile && selectedThinkingEffort) {
      const selected = bySlug.get(model);
      const availableEfforts = selected && Array.isArray(selected.thinking_efforts)
        ? selected.thinking_efforts
            .map((item) => item && item.thinking_effort)
            .filter((value) => typeof value === "string")
        : [];
      if (!availableEfforts.includes(selectedThinkingEffort)) {
        throw new Error("MINIMAL_CONVERSATION_THINKING_EFFORT_UNSUPPORTED");
      }
    }
    if (!model || (!bySlug.has(model) && model !== "auto")) {
      throw new Error("MINIMAL_MODEL_MISSING");
    }
    return { model, thinkingEffort };
  };

  const acquireIntegrityBundle = async (acquireIntegrity) => {
    const deviceMatch = document.cookie.match(/(?:^|;\s*)oai-did=([^;]+)/);
    const deviceId = deviceMatch ? decodeURIComponent(deviceMatch[1]) : "";
    setStage("integrity_bundle");
    const integrity = await acquireIntegrity({});
    const chatReq = integrity && integrity.chatReq && typeof integrity.chatReq === "object"
      ? integrity.chatReq
      : {};
    const turnstileToken = integrity && typeof integrity.turnstileToken === "string"
      ? integrity.turnstileToken
      : "";
    const proofToken = integrity && typeof integrity.proofToken === "string"
      ? integrity.proofToken
      : "";
    if (
      chatReq.force_login === true ||
      !(chatReq.token || chatReq.prepare_token) ||
      !turnstileToken ||
      !proofToken
    ) {
      throw new Error("MINIMAL_INTEGRITY_INCOMPLETE");
    }

    setStage("sentinel");
    try {
      await window.SentinelSDK.init("conversation");
      await window.SentinelSDK.token("conversation");
    } catch (_) {}
    const telemetry = window.SentinelSDK.timing?.();
    return { chatReq, turnstileToken, proofToken, telemetry, deviceId };
  };

  const buildConversationMessage = (attachmentDescriptors) => {
    const messageId = crypto.randomUUID();
    const effectiveParentMessageId = conversationId ? parentMessageId : "client-created-root";
    if (conversationId && !effectiveParentMessageId) {
      throw new Error("MINIMAL_CONTINUATION_PARENT_MISSING");
    }
    const attachmentParts = attachmentDescriptors.map((descriptor) => ({
      asset_pointer: `file-service://${descriptor.file_id}`,
      height: Number.isInteger(descriptor.height) ? descriptor.height : null,
      size_bytes: Number.isInteger(descriptor.file_size) ? descriptor.file_size : null,
      width: Number.isInteger(descriptor.width) ? descriptor.width : null,
    }));
    const messageContent = attachmentParts.length
      ? { content_type: "multimodal_text", parts: [...attachmentParts, prompt] }
      : { content_type: "text", parts: [prompt] };
    const messageMetadata = attachmentDescriptors.length
      ? {
          attachments: attachmentDescriptors.map((descriptor) => ({
            id: descriptor.file_id,
            mimeType: typeof descriptor.mime_type === "string" ? descriptor.mime_type : null,
            name: typeof descriptor.file_name === "string" ? descriptor.file_name : "attachment",
            size: Number.isInteger(descriptor.file_size) ? descriptor.file_size : null,
            ...(Number.isInteger(descriptor.width) && Number.isInteger(descriptor.height)
              ? { width: descriptor.width, height: descriptor.height }
              : {}),
          })),
        }
      : { serialization_metadata: { custom_symbol_offsets: [] } };
    return { messageId, effectiveParentMessageId, messageContent, messageMetadata };
  };

  const requestHeaders = (accessToken, deviceId, path) => {
    const headers = {
      authorization: `Bearer ${accessToken}`,
      "content-type": "application/json",
      origin: "https://chatgpt.com",
      referer: pageBaseURL,
      "oai-genui-client-actions": "open_entity_detail",
      "x-openai-target-path": path,
      "x-openai-target-route": path,
    };
    if (deviceId) headers["oai-device-id"] = deviceId;
    return headers;
  };

  const prepareConversation = async ({
    accessToken,
    deviceId,
    model,
    thinkingEffort,
    attachmentDescriptors,
    messageId,
    effectiveParentMessageId,
    messageContent,
    officialApiClient,
    onSubmitReady,
    turnTraceId,
  }) => {
    const runPrepare = async ({
      prepareState,
      prepareDispatch,
      prepareSource,
      includePartialQuery,
    }) => {
      const preparePayload = {
        action: "next",
        fork_from_shared_post: false,
        ...(conversationId ? { conversation_id: conversationId } : {}),
        parent_message_id: effectiveParentMessageId,
        model,
        ...(thinkingEffort ? { thinking_effort: thinkingEffort } : {}),
        client_prepare_state: prepareState,
        client_prepare_dispatch: prepareDispatch,
        client_prepare_source: prepareSource,
        conversation_mode: { kind: "primary_assistant" },
        ...(temporary ? { history_and_training_disabled: true } : {}),
        system_hints: [],
        supports_buffering: true,
        supported_encodings: ["v1"],
        client_contextual_info: { app_name: "chatgpt.com" },
        ...(includePartialQuery && attachmentDescriptors.length === 0
          ? {
              partial_query: {
                id: messageId,
                author: { role: "user" },
                content: messageContent,
              },
            }
          : {}),
      };
      setStage("prepare");
      if (officialApiClient && typeof officialApiClient.safePost === "function") {
        const prepareResult = await officialApiClient.safePost(
          "/f/conversation/prepare",
          {
            accessToken,
            requestBody: preparePayload,
            additionalHeaders: {
              "oai-genui-client-actions": "open_entity_detail",
              ...(turnTraceId ? { "x-oai-turn-trace-id": turnTraceId } : {}),
            },
          },
        );
        const conduitToken = prepareResult && typeof prepareResult.conduit_token === "string"
          ? prepareResult.conduit_token
          : "";
        if (!conduitToken) {
          throw new Error("MINIMAL_PREPARE_NO_CONDUIT");
        }
        return conduitToken;
      }

      const prepareResponse = await fetch("/backend-api/f/conversation/prepare", {
        method: "POST",
        credentials: "include",
        headers: {
          ...requestHeaders(accessToken, deviceId, "/backend-api/f/conversation/prepare"),
          accept: "application/json",
        },
        body: JSON.stringify(preparePayload),
      });
      const prepareResult = await prepareResponse.json();
      const conduitToken = prepareResult && typeof prepareResult.conduit_token === "string"
        ? prepareResult.conduit_token
        : "";
      if (!prepareResponse.ok || !conduitToken) {
        throw new Error(`MINIMAL_PREPARE_HTTP_${prepareResponse.status}`);
      }
      return conduitToken;
    };

    if (!conversationId) {
      return runPrepare({
        prepareState: "none",
        prepareDispatch: "debounced",
        prepareSource: "window_focus",
        includePartialQuery: false,
      });
    }

    if (temporary) {
      const token = await runPrepare({
        prepareState: "success",
        prepareDispatch: undefined,
        prepareSource: undefined,
        includePartialQuery: true,
      });
      if (typeof onSubmitReady === "function") onSubmitReady();
      return token || "";
    }

    if (officialApiClient) {
      const firstToken = await runPrepare({
        prepareState: "none",
        prepareDispatch: "immediate",
        prepareSource: "context_change",
        includePartialQuery: false,
      });
      const secondPromise = runPrepare({
        prepareState: "sent",
        prepareDispatch: "debounced",
        prepareSource: "composer_editor_state",
        includePartialQuery: true,
      });
      if (typeof onSubmitReady === "function") onSubmitReady();
      const secondToken = await secondPromise;
      return secondToken || firstToken || "";
    }

    return Promise.all([
      runPrepare({
        prepareState: "none",
        prepareDispatch: "immediate",
        prepareSource: "context_change",
        includePartialQuery: false,
      }),
      runPrepare({
        prepareState: "sent",
        prepareDispatch: "debounced",
        prepareSource: "composer_editor_state",
        includePartialQuery: true,
      }),
    ]).then((tokens) => tokens[1] || tokens[0] || "");
  };

  let lastBrokerResumeToken = "";
  let lastBrokerHandoffTopic = "";
  let brokerHandoffResolved = false;
  let resolveBrokerHandoff = null;
  const brokerHandoffPromise = new Promise((resolve) => {
    resolveBrokerHandoff = resolve;
  });
  const noteBrokerHandoff = () => {
    if (brokerHandoffResolved) return;
    brokerHandoffResolved = true;
    if (typeof resolveBrokerHandoff === "function") resolveBrokerHandoff();
  };
  let lastBrokerTextMessageId = "";
  let lastBrokerTextSnapshot = "";
  let brokerTextSequence = 0;
  let brokerCurrentMessageId = "";
  let brokerCurrentRecipient = "all";
  let brokerCurrentText = "";
  let brokerCurrentIsFinalText = false;
  let brokerTerminalSent = false;
  const transportString = (value) => typeof value === "string" && value.trim() ? value.trim() : "";
  const brokerContentText = (content) => {
    if (!content || typeof content !== "object") return "";
    if (typeof content.text === "string") return content.text;
    if (typeof content.content === "string") return content.content;
    const parts = Array.isArray(content.parts) ? content.parts : [];
    let text = "";
    for (const part of parts.slice(0, 64)) {
      if (typeof part === "string") text += part;
      else if (part && typeof part.text === "string") text += part.text;
    }
    return text;
  };
  const emitBrokerText = (type, messageId, value) => {
    if (!messageId || typeof value !== "string") return;
    brokerTextSequence += 1;
    const event = {
      phase: "text",
      type,
      sequence: brokerTextSequence,
      message_id: messageId,
    };
    if (type === "assistant_text_delta") event.delta = value;
    else event.text = value;
    postStream(event);
  };
  const applyBrokerText = (text) => {
    if (!brokerCurrentIsFinalText || !brokerCurrentMessageId || typeof text !== "string" || text === brokerCurrentText) return;
    if (text.startsWith(brokerCurrentText)) {
      const delta = text.slice(brokerCurrentText.length);
      brokerCurrentText = text;
      if (delta) emitBrokerText("assistant_text_delta", brokerCurrentMessageId, delta);
      return;
    }
    brokerCurrentText = text;
    emitBrokerText("assistant_text_revision", brokerCurrentMessageId, text);
  };
  const brokerCompletedStatus = (value) => [
    "completed", "complete", "finished", "done", "success", "succeeded", "finished_successfully",
  ].includes(String(value || "").toLowerCase());
  const selectBrokerMessage = (message) => {
    if (!message || typeof message !== "object") return;
    const messageId = transportString(message.id);
    const previousMessageId = brokerCurrentMessageId;
    const author = message.author && typeof message.author === "object" ? message.author : null;
    const role = transportString(author && author.role);
    const recipient = transportString(message.recipient) || "all";
    const content = message.content && typeof message.content === "object" ? message.content : null;
    const contentType = transportString(content && content.content_type);
    const metadata = message.metadata && typeof message.metadata === "object" ? message.metadata : null;
    const finalText = role === "assistant"
      && recipient === "all"
      && contentType === "text"
      && !(metadata && metadata.is_thinking_preamble_message === true);
    brokerCurrentRecipient = recipient;
    brokerCurrentIsFinalText = finalText;
    if (messageId) brokerCurrentMessageId = messageId;
    if (!finalText || !brokerCurrentMessageId) return;
    const snapshot = brokerContentText(content);
    if (messageId && messageId !== previousMessageId) {
      brokerCurrentText = "";
      if (snapshot) {
        brokerCurrentText = snapshot;
        lastBrokerTextMessageId = brokerCurrentMessageId;
        lastBrokerTextSnapshot = snapshot;
        emitBrokerText("assistant_text_snapshot", brokerCurrentMessageId, snapshot);
      }
    } else if (!brokerCurrentText && snapshot) {
      brokerCurrentText = snapshot;
      lastBrokerTextMessageId = brokerCurrentMessageId;
      lastBrokerTextSnapshot = snapshot;
      emitBrokerText("assistant_text_snapshot", brokerCurrentMessageId, snapshot);
    } else {
      applyBrokerText(snapshot);
    }
  };
  const inspectBrokerTerminalPatch = (path, value) => {
    if (!brokerCurrentIsFinalText || brokerTerminalSent) return;
    const normalized = String(path || "");
    const terminal = (normalized === "/message/end_turn" && value === true)
      || (normalized === "/message/status" && brokerCompletedStatus(value))
      || (normalized === "/message/async_status" && brokerCompletedStatus(value))
      || (normalized === "/message/metadata/status" && brokerCompletedStatus(value))
      || (normalized === "/message/metadata/async_status" && brokerCompletedStatus(value))
      || (normalized === "/message/metadata/finish_reason" && !!transportString(value))
      || (normalized === "/message/finish_reason" && !!transportString(value))
      || (normalized === "/message/metadata/finish_details"
        && value && typeof value === "object" && !!transportString(value.type));
    if (terminal) {
      brokerTerminalSent = true;
      postStream({ phase: "terminal", message_id: brokerCurrentMessageId || lastBrokerTextMessageId || null });
    }
  };
  const processBrokerPayload = (payload) => {
    inspectProtectedTransportData(payload);
    if (!payload || typeof payload !== "object") return;
    const value = payload.v;
    const path = payload.p;
    if (value && typeof value === "object" && !Array.isArray(value) && value.message) {
      selectBrokerMessage(value.message);
    }
    if (typeof value === "string" && brokerCurrentRecipient === "all"
      && (path == null || path === "/message/content/parts/0")) {
      brokerCurrentText += value;
      lastBrokerTextMessageId = brokerCurrentMessageId || lastBrokerTextMessageId;
      lastBrokerTextSnapshot = brokerCurrentText;
      emitBrokerText("assistant_text_delta", brokerCurrentMessageId, value);
    } else if (path === "/message/content" && value && typeof value === "object"
      && brokerCurrentRecipient === "all") {
      applyBrokerText(brokerContentText(value));
    }
    inspectBrokerTerminalPatch(path, value);
    if (!Array.isArray(value)) return;
    for (const item of value.slice(0, 128)) {
      if (!item || typeof item !== "object") continue;
      if (item.v && typeof item.v === "object" && !Array.isArray(item.v) && item.v.message) {
        selectBrokerMessage(item.v.message);
      }
      if (item.p === "/message/content/parts/0" && typeof item.v === "string"
        && brokerCurrentRecipient === "all") {
        brokerCurrentText += item.v;
        lastBrokerTextMessageId = brokerCurrentMessageId || lastBrokerTextMessageId;
        lastBrokerTextSnapshot = brokerCurrentText;
        emitBrokerText("assistant_text_delta", brokerCurrentMessageId, item.v);
      } else if (item.p === "/message/content" && item.v && typeof item.v === "object"
        && brokerCurrentRecipient === "all") {
        applyBrokerText(brokerContentText(item.v));
      }
      inspectBrokerTerminalPatch(item.p, item.v);
    }
  };
  const inspectProtectedTransportData = (value, depth = 0, seen = null) => {
    if (!requestId || value == null || depth > 10 || typeof value !== "object") return;
    const visited = seen || new Set();
    if (visited.has(value)) return;
    visited.add(value);
    if (Array.isArray(value)) {
      for (const item of value.slice(0, 128)) inspectProtectedTransportData(item, depth + 1, visited);
      return;
    }
    const type = transportString(value.type);
    const conversation = transportString(value.conversation_id) || conversationId;
    if (type === "resume_conversation_token") {
      const token = transportString(value.token);
      if (token && token !== lastBrokerResumeToken) {
        lastBrokerResumeToken = token;
        noteBrokerHandoff();
        postStream({ phase: "resume", token, conversation_id: conversation });
      }
    }
    const exchange = transportString(value.turn_exchange_id) || transportString(value.working_turn_id);
    let topic = "";
    const options = Array.isArray(value.options) ? value.options : [];
    for (const option of options.slice(0, 32)) {
      if (option && option.type === "subscribe_ws_topic") {
        topic = transportString(option.topic_id);
        if (topic) break;
      }
    }
    if (!topic && exchange) topic = `conversation-turn-${exchange}`;
    if (topic && topic !== lastBrokerHandoffTopic) {
      lastBrokerHandoffTopic = topic;
      noteBrokerHandoff();
      postStream({
        phase: "handoff",
        topic_id: topic,
        conversation_id: conversation,
        turn_exchange_id: exchange,
      });
    }
    const author = value.author && typeof value.author === "object" ? value.author : null;
    const content = value.content && typeof value.content === "object" ? value.content : null;
    const messageId = transportString(value.id);
    if (author && author.role === "assistant" && content && Array.isArray(content.parts) && messageId) {
      const snapshot = content.parts.filter((part) => typeof part === "string").join("\n");
      if (snapshot && (messageId !== lastBrokerTextMessageId || snapshot !== lastBrokerTextSnapshot)) {
        lastBrokerTextMessageId = messageId;
        lastBrokerTextSnapshot = snapshot;
        brokerTextSequence += 1;
        postStream({
          phase: "text",
          type: "assistant_text_snapshot",
          sequence: brokerTextSequence,
          message_id: messageId,
          text: snapshot,
        });
      }
    }
    const metadata = value.metadata && typeof value.metadata === "object" ? value.metadata : null;
    const finishDetails = metadata && metadata.finish_details && typeof metadata.finish_details === "object"
      ? metadata.finish_details
      : null;
    const terminalStatus = ["completed", "complete", "finished", "done", "success", "succeeded", "finished_successfully"];
    const explicitTerminalType = ["conversation_turn_complete", "conversation_turn_completed", "turn_complete"].includes(type);
    const recipient = transportString(value.recipient) || "all";
    const contentType = transportString(content && content.content_type);
    const visibleFinalAssistant = author && author.role === "assistant"
      && recipient === "all"
      && contentType === "text"
      && !(metadata && metadata.is_thinking_preamble_message === true);
    const assistantTerminal = visibleFinalAssistant && (
      value.end_turn === true
      || terminalStatus.includes(String(value.status || "").toLowerCase())
      || terminalStatus.includes(String(value.async_status || "").toLowerCase())
      || (metadata && terminalStatus.includes(String(metadata.status || "").toLowerCase()))
      || (finishDetails && transportString(finishDetails.type))
    );
    const terminal = assistantTerminal
      || (explicitTerminalType && brokerCurrentIsFinalText && !!brokerCurrentMessageId);
    for (const key of Object.keys(value).slice(0, 128)) {
      if (/token|secret|authorization|cookie/i.test(key)) continue;
      inspectProtectedTransportData(value[key], depth + 1, visited);
    }
    if (terminal && !brokerTerminalSent) {
      brokerTerminalSent = true;
      postStream({ phase: "terminal", message_id: transportString(value.id) || lastBrokerTextMessageId || null });
    }
  };

  const observeDirectWriteResponse = async (response) => {
    if (!response || !response.body) return;
    let observedResponse = response;
    try {
      observedResponse = response.clone();
    } catch (_) {}
    if (observeProtectedWriteResponse) {
      try {
        await observeProtectedWriteResponse(observedResponse);
        return;
      } catch (_) {}
    }
    const body = observedResponse && observedResponse.body;
    if (!body || typeof body.getReader !== "function") return;
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let releasedForHandoff = false;
    const processBlock = (block) => {
      const data = String(block || "")
        .split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n")
        .trim();
      if (!data) return;
      if (data === "[DONE]") {
        postStream({ phase: "done" });
        return;
      }
      try {
        const parsed = JSON.parse(data);
        if (parsed && typeof parsed === "object") {
          postStream({ phase: "raw", parsed });
          processBrokerPayload(parsed);
        }
      } catch (_) {}
    };
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) {
          buffer += decoder.decode();
          break;
        }
        buffer += decoder.decode(chunk.value, { stream: true });
        if (buffer.length > 1000000) buffer = buffer.slice(-1000000);
        while (true) {
          const match = /\r?\n\r?\n/.exec(buffer);
          if (!match) break;
          const block = buffer.slice(0, match.index);
          buffer = buffer.slice(match.index + match[0].length);
          processBlock(block);
          if (temporary && brokerHandoffResolved) {
            releasedForHandoff = true;
            break;
          }
        }
        if (releasedForHandoff) {
          try { void reader.cancel(); } catch (_) {}
          buffer = "";
          break;
        }
      }
      if (!releasedForHandoff && buffer.trim()) processBlock(buffer.trim());
    } catch (_) {
    } finally {
      try { reader.releaseLock(); } catch (_) {}
      if (temporary && releasedForHandoff) {
        postStream({ phase: "handoff_released" });
      }
      postStream({ phase: "ended" });
    }
  };

  const protectedWrite = async ({
    accessToken,
    deviceId,
    model,
    thinkingEffort,
    messageId,
    effectiveParentMessageId,
    messageContent,
    messageMetadata,
    conduitToken,
    turnTraceId,
    integrityBundle,
    officialConversationTransport,
  }) => {
    const message = {
      id: messageId,
      author: { role: "user" },
      content: messageContent,
      metadata: messageMetadata,
      create_time: Date.now() / 1000,
    };
    const writePayload = {
      action: "next",
      ...(conversationId ? { fork_from_shared_post: false, conversation_id: conversationId } : {}),
      parent_message_id: effectiveParentMessageId,
      model,
      ...(thinkingEffort ? { thinking_effort: thinkingEffort } : {}),
      conversation_mode: { kind: "primary_assistant" },
      ...(temporary ? { history_and_training_disabled: true } : {}),
      enable_message_followups: false,
      supports_buffering: true,
      supported_encodings: ["v1"],
      messages: [message],
      client_prepare_state: temporary ? "success" : (conversationId ? "sent" : "success"),
      client_contextual_info: { app_name: "chatgpt.com" },
      system_hints: [],
    };
    const useOfficialConversationTransport =
      conversationId
      && !temporary
      && typeof officialConversationTransport === "function";
    const writeHeaders = {
      ...requestHeaders(accessToken, deviceId, "/backend-api/f/conversation"),
      accept: "text/event-stream",
      "x-oai-turn-trace-id": turnTraceId,
      ...(handoffAttemptId
        ? { "x-oai-stream-handoff-attempt-id": handoffAttemptId }
        : {}),
    };
    if (conduitToken) {
      writeHeaders["x-conduit-token"] = conduitToken;
      postStream({
        phase: "stop_context",
        conduit_token: conduitToken,
        turn_trace_id: turnTraceId,
      });
    }
    if (integrityBundle.chatReq.token) {
      writeHeaders["OpenAI-Sentinel-Chat-Requirements-Token"] = integrityBundle.chatReq.token;
    } else {
      writeHeaders["OpenAI-Sentinel-Chat-Requirements-Prepare-Token"] =
        integrityBundle.chatReq.prepare_token;
    }
    writeHeaders["OpenAI-Sentinel-Turnstile-Token"] = integrityBundle.turnstileToken;
    writeHeaders["OpenAI-Sentinel-Proof-Token"] = integrityBundle.proofToken;
    if (typeof integrityBundle.telemetry === "string" && integrityBundle.telemetry) {
      writeHeaders["OAI-Telemetry"] = integrityBundle.telemetry;
    }

    setStage("write");
    if (useOfficialConversationTransport) {
      const officialAdditionalHeaders = { ...writeHeaders };
      const middlewareOwnedHeaders = new Set([
        "authorization",
        "content-type",
        "origin",
        "referer",
        "oai-device-id",
        "x-oai-is-client-observation",
        "x-oai-is-pending-updates",
        "x-openai-target-path",
        "x-openai-target-route",
      ]);
      for (const key of Object.keys(officialAdditionalHeaders)) {
        if (middlewareOwnedHeaders.has(key.toLowerCase())) {
          delete officialAdditionalHeaders[key];
        }
      }
      let resolveOpen;
      let rejectOpen;
      let opened = false;
      const openedPromise = new Promise((resolve, reject) => {
        resolveOpen = resolve;
        rejectOpen = reject;
      });
      postSubmit({ phase: "request", temporary_mode: temporary });
      const stream = officialConversationTransport(
        new URL("/backend-api/f/conversation", pageBaseURL).href,
        {
          accessToken,
          method: "POST",
          headers: officialAdditionalHeaders,
          body: writePayload,
          targetBaseUrl: "https://chatgpt.com/backend-api",
          routeName: "/f/conversation",
          compressionEligible: true,
          initialOpenTimeoutMs: 30000,
          idleTimeoutMs: 60000,
          onBeforeRequestStart: () => ({}),
          shouldRetry: () => false,
          retryConfig: {
            MIN_RETRY_INTERVAL: 300,
            MAX_RETRY_INTERVAL: 5000,
            RETRY_FACTOR: 1.5,
            MAX_RETRY_COUNT: 0,
          },
          observer: {
            onOpen: (response) => {
              opened = true;
              postSubmit({ phase: "response", status: response.status });
              postStream({ phase: "started", status: response.status, ok: response.ok === true });
              post({
                ok: response.ok,
                status: response.status,
                minimal_security_write_started: true,
                official_conversation_transport: true,
              });
              resolveOpen(response);
            },
          },
        },
      );
      if (!stream || typeof stream[Symbol.asyncIterator] !== "function") {
        const streamType = stream === null ? "null" : typeof stream;
        const streamKeys = stream && typeof stream === "object"
          ? Object.keys(stream).sort().join(",")
          : "";
        throw new Error(
          `MINIMAL_OFFICIAL_CONVERSATION_STREAM_INVALID type=${streamType} keys=${streamKeys}`,
        );
      }
      void (async () => {
        let releasedForHandoff = false;
        try {
          for await (const transportEvent of stream) {
            if (
              transportEvent
              && typeof transportEvent === "object"
              && transportEvent.data
              && typeof transportEvent.data === "object"
            ) {
              postStream({ phase: "raw", parsed: transportEvent.data });
              processBrokerPayload(transportEvent.data);
              if (temporary && brokerHandoffResolved) {
                releasedForHandoff = true;
                break;
              }
            }
          }
          if (temporary && releasedForHandoff) {
            postStream({ phase: "handoff_released" });
          }
          postStream({ phase: "ended" });
          if (!opened) {
            rejectOpen(new Error("MINIMAL_OFFICIAL_CONVERSATION_STREAM_CLOSED_BEFORE_OPEN"));
          }
        } catch (error) {
          if (!opened) {
            postSubmit({ phase: "error", error: String(error) });
            rejectOpen(error);
          }
        }
      })();
      await openedPromise;
      return;
    }

    postSubmit({ phase: "request", temporary_mode: temporary });
    const writeResponse = await fetch("/backend-api/f/conversation", {
      method: "POST",
      credentials: "include",
      headers: writeHeaders,
      body: JSON.stringify(writePayload),
      __cwaDirectObserve: true,
    });
    postSubmit({ phase: "response", status: writeResponse.status });
    postStream({ phase: "started", status: writeResponse.status, ok: writeResponse.ok === true });
    post({
      ok: writeResponse.ok,
      status: writeResponse.status,
      minimal_security_write_started: true,
      official_conversation_transport: false,
    });
    if (writeResponse.ok) {
      try {
        void observeDirectWriteResponse(writeResponse);
      } catch (_) {}
    } else {
      try {
        await writeResponse.body?.cancel();
      } catch (_) {}
    }
  };

  (async () => {
    if (!prompt) throw new Error("MINIMAL_PROMPT_MISSING");
    const attachmentDescriptors = decodeAttachmentDescriptors();
    let accessToken = "";
    let acquireIntegrity = null;
    let officialConversationTransport = null;
    let officialApiClient = null;

    if (conversationId) {
      const shared = await loadSharedConversationInitialization();
      accessToken = shared.accessToken;
      acquireIntegrity = shared.acquireIntegrity;
      officialConversationTransport = shared.officialConversationTransport;
      officialApiClient = shared.officialApiClient;
      if (typeof officialConversationTransport !== "function") {
        throw new Error("MINIMAL_OFFICIAL_CONVERSATION_TRANSPORT_MISSING");
      }
      setStage("session");
    } else {
      await waitForSentinel();
      const integrityURL = await bootstrapProductResources();
      const runtime = await loadIntegrityRuntime(integrityURL);
      acquireIntegrity = runtime.acquireIntegrity;
      officialConversationTransport = runtime.officialConversationTransport;
      accessToken = await loadSession();
      if (temporary) {
        sharedConversationInitializationPromise = Promise.resolve(Object.freeze({
          accessToken,
          acquireIntegrity,
          officialConversationTransport,
          officialApiClient: null,
        }));
      }
    }

    let modelsPayload;
    if (conversationId) {
      modelsPayload = await loadSharedModelCatalog(accessToken);
    } else {
      modelsPayload = await loadModelCatalog(accessToken);
      if (temporary) {
        sharedModelCatalogPromises.set("temporary", Promise.resolve(modelsPayload));
      }
    }
    const { model, thinkingEffort } = resolveModelSelection(modelsPayload);
    const integrityBundle = await acquireIntegrityBundle(acquireIntegrity);

    const message = buildConversationMessage(attachmentDescriptors);
    postStream({ phase: "client_message", message_id: message.messageId });
    const turnTraceId = crypto.randomUUID();
    let resolveSubmitReady;
    const submitReadyPromise = new Promise((resolve) => {
      resolveSubmitReady = resolve;
    });
    const conduitPromise = prepareConversation({
      accessToken,
      deviceId: integrityBundle.deviceId,
      model,
      thinkingEffort,
      attachmentDescriptors,
      messageId: message.messageId,
      effectiveParentMessageId: message.effectiveParentMessageId,
      messageContent: message.messageContent,
      officialApiClient,
      onSubmitReady: officialApiClient ? resolveSubmitReady : null,
      turnTraceId,
    });

    if (conversationId && !temporary) {
      await submitReadyPromise;
      const writePromise = protectedWrite({
        accessToken,
        deviceId: integrityBundle.deviceId,
        model,
        thinkingEffort,
        ...message,
        conduitToken: "",
        turnTraceId,
        integrityBundle,
        officialConversationTransport,
      });
      const conduitToken = await conduitPromise;
      if (conduitToken) {
        postStream({
          phase: "stop_context",
          conduit_token: conduitToken,
          turn_trace_id: turnTraceId,
        });
      }
      await writePromise;
    } else {
      const conduitToken = await conduitPromise;
      await protectedWrite({
        accessToken,
        deviceId: integrityBundle.deviceId,
        model,
        thinkingEffort,
        ...message,
        conduitToken,
        turnTraceId,
        integrityBundle,
        officialConversationTransport,
      });
    }
  })().catch((error) => {
    const renderedError = error && error.stack ? `${String(error)}\n${String(error.stack)}` : String(error);
    if (!requestId) window.__CWA_MINIMAL_LAST_ERROR__ = renderedError;
    post({
      ok: false,
      status: 0,
      stage,
      error: renderedError,
    });
  });
  return true;
  };
  window.__cwaRunMinimalSecurityShell = runMinimalSecurityShell;
  if (window.__CWA_BROKER_MANAGED__ === true || location.hash.startsWith("#cwa-turn=")) return true;
  return runMinimalSecurityShell();
})();
