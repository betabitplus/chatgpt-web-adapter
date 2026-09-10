// Opt-in WKWebView Phase-A shell. It keeps the protected conversation write in the
// real chatgpt.com browser context while avoiding hydration of the full ChatGPT SPA.
// Only current server-provided bootstrap/security modules are used; integrity values
// stay inside the browser request path, and any unsupported shape fails closed.
(() => {
  const post = (body) => {
    try {
      window.webkit.messageHandlers.cwaCanonical.postMessage(body);
    } catch (_) {}
  };
  const prompt = typeof window.__CWA_MINIMAL_PROMPT__ === "string" ? window.__CWA_MINIMAL_PROMPT__ : "";
  const profile = typeof window.__CWA_MINIMAL_PROFILE__ === "string" ? window.__CWA_MINIMAL_PROFILE__ : "";
  const conversationId = typeof window.__CWA_MINIMAL_CONVERSATION_ID__ === "string"
    ? window.__CWA_MINIMAL_CONVERSATION_ID__
    : "";
  const parentMessageId = typeof window.__CWA_MINIMAL_PARENT_MESSAGE_ID__ === "string"
    ? window.__CWA_MINIMAL_PARENT_MESSAGE_ID__
    : "";
  const selectedModelSlug = typeof window.__CWA_MINIMAL_SELECTED_MODEL_SLUG__ === "string"
    ? window.__CWA_MINIMAL_SELECTED_MODEL_SLUG__
    : "";
  const selectedThinkingEffort = typeof window.__CWA_MINIMAL_SELECTED_THINKING_EFFORT__ === "string"
    ? window.__CWA_MINIMAL_SELECTED_THINKING_EFFORT__
    : "";
  const attachmentsBase64 = typeof window.__CWA_MINIMAL_ATTACHMENTS_BASE64__ === "string"
    ? window.__CWA_MINIMAL_ATTACHMENTS_BASE64__
    : "";
  let stage = "start";

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
      `\\b${escapeRegex(internalName)}\\s+as\\s+([A-Za-z_$][A-Za-z0-9_$]*)\\b`,
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
      /(?:^|[;,])var\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*e\(\(\(\)\s*=>\s*\{/.exec(suffix) ||
      /(?:^|[;,])([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*e\(\(\(\)\s*=>\s*\{/.exec(suffix);
    if (!initializerMatch) throw new Error("MINIMAL_INTEGRITY_INITIALIZER_DISCOVERY_FAILED");
    return { helperName: helper.name, initializerName: initializerMatch[1] };
  };

  const discoverIntegrityExports = (source) => {
    const names = discoverIntegrityNames(source);
    const helperExport = exportAlias(source, names.helperName);
    const initializerExport = exportAlias(source, names.initializerName);
    if (!helperExport || !initializerExport) {
      throw new Error("MINIMAL_INTEGRITY_EXPORT_DISCOVERY_FAILED");
    }
    return { helperExport, initializerExport };
  };

  const discoverIntegrityExportsStreaming = async (response) => {
    if (!response.body || typeof response.body.getReader !== "function") {
      return discoverIntegrityExports(await response.text());
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let prefixCarry = "";
    let discoveryWindow = "";
    let aliasCarry = "";
    let markerSeen = false;
    let names = null;
    let helperExport = null;
    let initializerExport = null;

    const scanAliases = (source) => {
      if (!names) return;
      if (!helperExport) helperExport = exportAlias(source, names.helperName);
      if (!initializerExport) initializerExport = exportAlias(source, names.initializerName);
    };

    while (true) {
      const { value, done } = await reader.read();
      const chunk = done ? decoder.decode() : decoder.decode(value, { stream: true });
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
      } else {
        const combined = aliasCarry + chunk;
        scanAliases(combined);
        aliasCarry = combined.slice(-4096);
      }
      if (names && helperExport && initializerExport) {
        try {
          await reader.cancel();
        } catch (_) {}
        return { helperExport, initializerExport };
      }
      if (done) break;
    }
    throw new Error("MINIMAL_INTEGRITY_STREAM_DISCOVERY_FAILED");
  };

  (async () => {
    if (!prompt) throw new Error("MINIMAL_PROMPT_MISSING");
    let attachmentDescriptors = [];
    if (attachmentsBase64) {
      try {
        const raw = atob(attachmentsBase64);
        const bytes = new Uint8Array(raw.length);
        for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
        const parsedAttachments = JSON.parse(new TextDecoder().decode(bytes));
        if (!Array.isArray(parsedAttachments)) throw new Error("not_array");
        attachmentDescriptors = parsedAttachments;
      } catch (_) {
        throw new Error("MINIMAL_ATTACHMENT_DESCRIPTOR_DECODE_FAILED");
      }
      for (const descriptor of attachmentDescriptors) {
        if (!descriptor || typeof descriptor.file_id !== "string" || !descriptor.file_id) {
          throw new Error("MINIMAL_ATTACHMENT_DESCRIPTOR_INVALID");
        }
      }
    }
    for (let i = 0; i < 80 && !window.__cwaSentinelLoaded; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (!window.SentinelSDK) throw new Error("MINIMAL_SENTINEL_SDK_MISSING");

    stage = "root";
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

    const integrityAsset = [...parsed.querySelectorAll("script[src],link[href]")]
      .map((element) => element.getAttribute("src") || element.getAttribute("href") || "")
      .find((value) => value.includes("/conversation-small-") && value.includes(".js"));
    if (!integrityAsset) throw new Error("MINIMAL_INTEGRITY_ASSET_MISSING");
    const integrityURL = new URL(integrityAsset, location.href).href;
    stage = "integrity_discovery";
    const integrityCacheKey = `__cwa_integrity_exports_v1:${integrityURL}`;
    let integrityExports = null;
    try {
      const cached = JSON.parse(localStorage.getItem(integrityCacheKey) || "null");
      if (
        cached
        && typeof cached.helperExport === "string"
        && cached.helperExport
        && typeof cached.initializerExport === "string"
        && cached.initializerExport
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
    stage = "integrity_import";
    const integrityModule = await import(integrityURL);
    const initializeIntegrity = integrityModule[integrityExports.initializerExport];
    const acquireIntegrity = integrityModule[integrityExports.helperExport];
    if (typeof initializeIntegrity !== "function" || typeof acquireIntegrity !== "function") {
      throw new Error("MINIMAL_INTEGRITY_EXPORTS_MISSING");
    }
    stage = "integrity_init";
    initializeIntegrity();

    stage = "session";
    const sessionResponse = await fetch("/api/auth/session", {
      credentials: "include",
      cache: "no-store",
    });
    const session = await sessionResponse.json();
    const accessToken = session && session.accessToken;
    if (!accessToken) throw new Error("MINIMAL_ACCESS_TOKEN_MISSING");

    stage = "models";
    const modelsResponse = await fetch(
      "/backend-api/models?history_and_training_disabled=false",
      {
        credentials: "include",
        cache: "no-store",
        headers: { Authorization: `Bearer ${accessToken}` },
      },
    );
    const modelsPayload = await modelsResponse.json();
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
      const supported = availableEfforts.includes(thinkingEffort);
      if (!supported) throw new Error("MINIMAL_PROFILE_UNSUPPORTED_BY_MODEL");
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

    const deviceMatch = document.cookie.match(/(?:^|;\s*)oai-did=([^;]+)/);
    const deviceId = deviceMatch ? decodeURIComponent(deviceMatch[1]) : "";
    stage = "integrity_bundle";
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

    stage = "sentinel";
    try {
      await window.SentinelSDK.init("conversation");
      await window.SentinelSDK.token("conversation");
    } catch (_) {}
    const telemetry = window.SentinelSDK.timing?.();

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
    const authorization = `Bearer ${accessToken}`;
    const commonHeaders = (path) => {
      const headers = {
        authorization,
        "content-type": "application/json",
        origin: "https://chatgpt.com",
        referer: location.href,
        "x-openai-target-path": path,
        "x-openai-target-route": path,
      };
      if (deviceId) headers["oai-device-id"] = deviceId;
      return headers;
    };

    const preparePayload = {
      action: "next",
      fork_from_shared_post: false,
      ...(conversationId ? { conversation_id: conversationId } : {}),
      parent_message_id: effectiveParentMessageId,
      model,
      ...(thinkingEffort ? { thinking_effort: thinkingEffort } : {}),
      client_prepare_state: "none",
      client_prepare_dispatch: "debounced",
      client_prepare_source: "window_focus",
      conversation_mode: { kind: "primary_assistant" },
      system_hints: [],
      supports_buffering: true,
      supported_encodings: ["v1"],
      client_contextual_info: { app_name: "chatgpt.com" },
      ...(attachmentDescriptors.length
        ? {}
        : {
            partial_query: {
              id: messageId,
              author: { role: "user" },
              content: messageContent,
            },
          }),
    };
    stage = "prepare";
    const prepareResponse = await fetch("/backend-api/f/conversation/prepare", {
      method: "POST",
      credentials: "include",
      headers: {
        ...commonHeaders("/backend-api/f/conversation/prepare"),
        accept: "application/json",
        "x-conduit-token": "no-token",
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

    const message = {
      id: messageId,
      author: { role: "user" },
      content: messageContent,
      metadata: messageMetadata,
      create_time: Date.now() / 1000,
    };
    const writePayload = {
      action: "next",
      ...(conversationId ? { conversation_id: conversationId } : {}),
      parent_message_id: effectiveParentMessageId,
      model,
      ...(thinkingEffort ? { thinking_effort: thinkingEffort } : {}),
      conversation_mode: { kind: "primary_assistant" },
      enable_message_followups: false,
      supports_buffering: true,
      supported_encodings: ["v1"],
      messages: [message],
      client_prepare_state: "success",
      client_contextual_info: { app_name: "chatgpt.com" },
      system_hints: [],
    };
    const writeHeaders = {
      ...commonHeaders("/backend-api/f/conversation"),
      accept: "text/event-stream",
      "x-conduit-token": conduitToken,
      "x-oai-turn-trace-id": crypto.randomUUID(),
    };
    if (chatReq.token) {
      writeHeaders["OpenAI-Sentinel-Chat-Requirements-Token"] = chatReq.token;
    } else {
      writeHeaders["OpenAI-Sentinel-Chat-Requirements-Prepare-Token"] = chatReq.prepare_token;
    }
    writeHeaders["OpenAI-Sentinel-Turnstile-Token"] = turnstileToken;
    writeHeaders["OpenAI-Sentinel-Proof-Token"] = proofToken;
    if (typeof telemetry === "string" && telemetry) writeHeaders["OAI-Telemetry"] = telemetry;

    stage = "write";
    const writeResponse = await fetch("/backend-api/f/conversation", {
      method: "POST",
      credentials: "include",
      headers: writeHeaders,
      body: JSON.stringify(writePayload),
    });
    post({
      ok: writeResponse.ok,
      status: writeResponse.status,
      minimal_security_write_started: true,
    });
    if (!writeResponse.ok) {
      try {
        await writeResponse.body?.cancel();
      } catch (_) {}
    }
  })().catch((error) => post({
    ok: false,
    status: 0,
    stage,
    error: String(error),
  }));
  return true;
})();
