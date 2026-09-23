// Production terminal-outcome observation.
//
// Browser-owned writes already read the exact completed conversation SSE body for
// request-bound identity. Extend that existing read only with bounded top-level
// server error metadata; never export raw SSE or message content.

const _cwaTerminalOutcomePriorExtractSafeStreamMetadata = extractSafeStreamMetadata;
const CWA_TERMINAL_ERROR_CODE_MAX_CHARS = 128;
const CWA_TERMINAL_ERROR_MAX_CHARS = 1000;

function _cwaTerminalOutcomeDecodeBody(body, base64Encoded) {
  if (typeof body !== "string") return null;
  if (base64Encoded !== true) return body;
  try {
    const binary = atob(body);
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    return new TextDecoder("utf-8").decode(bytes);
  } catch {
    return null;
  }
}

function _cwaTerminalOutcomeBoundedString(value, maxChars) {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  if (!normalized) return null;
  return normalized.slice(0, maxChars);
}

function _cwaTerminalOutcomeFromSse(body, base64Encoded) {
  const decoded = _cwaTerminalOutcomeDecodeBody(body, base64Encoded);
  if (typeof decoded !== "string") {
    return { terminalErrorCode: null, terminalError: null };
  }

  let terminalErrorCode = null;
  let terminalError = null;
  for (const block of decoded.split(/\r?\n\r?\n/)) {
    const dataLines = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    const payloadText = dataLines.join("\n").trim();
    if (!payloadText || payloadText === "[DONE]" || !payloadText.startsWith("{")) continue;
    try {
      const payload = JSON.parse(payloadText);
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) continue;
      const errorCode = _cwaTerminalOutcomeBoundedString(
        payload.error_code,
        CWA_TERMINAL_ERROR_CODE_MAX_CHARS
      );
      const errorText = _cwaTerminalOutcomeBoundedString(
        payload.error,
        CWA_TERMINAL_ERROR_MAX_CHARS
      );
      if (errorCode !== null || errorText !== null) {
        terminalErrorCode = errorCode;
        terminalError = errorText;
      }
    } catch {
      // Ignore partial/non-JSON SSE. No raw response data leaves this worker.
    }
  }
  return { terminalErrorCode, terminalError };
}

extractSafeStreamMetadata = function _cwaTerminalOutcomeExtractSafeStreamMetadata(
  body,
  base64Encoded
) {
  let prior = {};
  try {
    const value = _cwaTerminalOutcomePriorExtractSafeStreamMetadata(body, base64Encoded);
    if (value && typeof value === "object") prior = value;
  } catch {
    // Terminal observability must never perturb identity/finality behavior.
  }
  return {
    ...prior,
    ..._cwaTerminalOutcomeFromSse(body, base64Encoded)
  };
};

