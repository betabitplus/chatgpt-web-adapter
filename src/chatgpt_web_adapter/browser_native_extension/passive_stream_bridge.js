(() => {
  "use strict";
  const CHANNEL = "cwa-passive-stream-v1";

  function postControl(action, observerId) {
    window.postMessage({
      channel: CHANNEL,
      direction: "control",
      action,
      observerId: typeof observerId === "string" ? observerId : null,
    }, location.origin);
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "cwa_passive_arm") {
      postControl("arm", message.observerId);
      sendResponse?.({ ok: true });
      return false;
    }
    if (message?.type === "cwa_passive_disarm") {
      postControl("disarm", message.observerId);
      sendResponse?.({ ok: true });
      return false;
    }
    return false;
  });

  window.addEventListener("message", (event) => {
    if (event.source !== window || event.origin !== location.origin) return;
    const data = event.data;
    if (!data || data.channel !== CHANNEL || data.direction !== "event") return;
    if (typeof data.observerId !== "string" || !data.observerId) return;
    if (!data.event || typeof data.event !== "object") return;
    try {
      chrome.runtime.sendMessage({
        type: "cwa_passive_stream_event",
        observerId: data.observerId,
        event: data.event,
      });
    } catch {
      // Extension reload/disconnect cannot perturb the page.
    }
  });

  try {
    chrome.runtime.sendMessage({ type: "cwa_passive_bridge_ready" });
  } catch {}
})();
