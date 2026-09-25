from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

EXTENSION_STOP = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "chatgpt_web_adapter"
    / "browser_native_extension"
    / "service_worker_stop_generation.js"
)


def _run_stop_scenario(tmp_path: Path, scenario: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")

    harness = tmp_path / "stop_identity_harness.cjs"
    harness.write_text(
        r"""
const fs = require("fs");
const vm = require("vm");

const sourcePath = process.argv[2];
const scenario = process.argv[3];
let tabGetCount = 0;
const stopEvents = [];

function routeUrl() {
  if (scenario === "verified") return "https://chatgpt.com/c/conversation-1";
  if (scenario === "drift") {
    return tabGetCount === 1
      ? "https://chatgpt.com/c/conversation-1"
      : "https://chatgpt.com/c/conversation-2";
  }
  return "https://chatgpt.com/";
}

const context = {
  console,
  Map,
  setTimeout,
  clearTimeout,
  BRIDGE_PROTOCOL_VERSION: 1,
  onNativeMessage: async () => {},
  safePortPost: () => {},
  storedRuntimeTabId: async () => 42,
  isChatGPTUrl: (url) => typeof url === "string" && url.startsWith("https://chatgpt.com/"),
  conversationIdFromUrl: (url) => {
    const match = typeof url === "string" ? url.match(/\/c\/([^/?#]+)/) : null;
    return match ? match[1] : null;
  },
  _cwaPassiveSessions: new Map([
    ["session-1", {
      tabId: 42,
      conversationId: "conversation-1",
      turnExchangeId: "turn-1",
    }],
  ]),
  _cwaPassiveAcceptEvent: (_session, event) => stopEvents.push(event),
  chrome: {
    tabs: {
      get: async () => {
        tabGetCount += 1;
        return { url: routeUrl() };
      },
      sendMessage: async () => ({ ok: true, stopped: true }),
    },
  },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(sourcePath, "utf8"), context, { filename: sourcePath });

(async () => {
  const result = await context._cwaExecuteStopGeneration({
    conversationId: "conversation-1",
    timeoutMs: scenario === "unresolved" ? 250 : 1000,
  });
  process.stdout.write(JSON.stringify({ result, stopEvents }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
""",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [node, str(harness), str(EXTENSION_STOP), scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(completed.stdout)


def test_browser_stop_proves_identity_on_committed_requested_route(tmp_path: Path) -> None:
    payload = _run_stop_scenario(tmp_path, "verified")
    result = payload["result"]

    assert result["ok"] is True
    assert result["stopped"] is True
    assert result["conversationId"] == "conversation-1"
    assert result["proof"] == "browser_stop_control"
    assert result["conversationIdentityVerified"] is True
    assert result["reason"] is None
    assert payload["stopEvents"][0]["conversation_id"] == "conversation-1"


def test_browser_stop_keeps_identity_unverified_when_route_never_commits(
    tmp_path: Path,
) -> None:
    payload = _run_stop_scenario(tmp_path, "unresolved")
    result = payload["result"]

    assert result["ok"] is True
    assert result["stopped"] is True
    assert result["conversationId"] == "conversation-1"
    assert result["proof"] == "browser_stop_control"
    assert result["conversationIdentityVerified"] is False
    assert result["reason"] == "conversation_route_unresolved"
    assert payload["stopEvents"][0]["conversation_id"] == "conversation-1"


def test_browser_stop_does_not_transfer_identity_when_route_drifts_after_click(
    tmp_path: Path,
) -> None:
    payload = _run_stop_scenario(tmp_path, "drift")
    result = payload["result"]

    assert result["ok"] is True
    assert result["stopped"] is True
    assert result["conversationId"] == "conversation-2"
    assert result["proof"] == "browser_stop_control"
    assert result["conversationIdentityVerified"] is False
    assert result["reason"] == "conversation_route_unresolved"
    # The passive session keeps its own known conversation identity; an unverified
    # post-click route must not rewrite the event onto another conversation.
    assert payload["stopEvents"][0]["conversation_id"] == "conversation-1"
