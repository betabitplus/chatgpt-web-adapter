# WKWebView browser-authority checkpoint

This directory preserves the macOS `WKWebView` experiment from 2026-09-08 so the work is reproducible without relying on `/tmp` prototypes or the Chromium runtime.

It is deliberately an **experiment, not production CWA code**. The current direction is to keep one serialized WKWebView authority for submit/stop/attachment/tool interactions while allowing many accepted turns to continue concurrently on the ChatGPT server.

No cookies, access tokens, login data, conversation IDs, or other account secrets are stored in this directory.

## Why this exists

The original problem is the resource cost of keeping the full ChatGPT React application inside dedicated Chromium renderers, especially for long conversations and several simultaneous chats.

The live experiment established that macOS system WebKit can load and operate the real ChatGPT web application with a much smaller browser shell:

- real `chatgpt.com` frontend and JavaScript;
- persistent paid-account login via `WKWebsiteDataStore.defaultDataStore`;
- existing conversation continuation;
- High reasoning mode;
- normal send/stream/final;
- Stop via the current `data-testid="stop-button"` / `aria-label="Stop answering"` control;
- image attachment through the real file input and `WKUIDelegate` file chooser;
- connected CodexTool/MCP tool loop;
- multiple accepted turns continuing server-side after their WKWebView process exits.

The important architectural result is that **one WebKit authority should be serialized across chats**. Running one WKWebView per active chat makes resource use add almost linearly and is not the intended design.

## Files

- `WKChatGPTAuthority.m` — small generic reproduction harness.
- `Info.plist` — stable bundle identity `local.gptty.webkit-authority`; this lets WebKit keep its normal persistent website data store between runs.
- `Makefile` — local build/login/smoke helpers.

The harness uses only public Cocoa/WebKit APIs and the normal ChatGPT web UI. It does not copy browser cookies, forge network fingerprints, or implement Sentinel/Cloudflare challenge logic itself.

## Build

macOS with the Command Line Tools/Xcode SDK is sufficient:

```bash
cd experiments/wkwebview_authority
make build
```

The app is created at:

```text
build/WKChatGPTAuthority.app
```

Build artifacts are experimental/local and should not be committed.

## One-time login

```bash
make login
```

A normal WebKit window opens. Log in to ChatGPT normally and then close the process. The experiment intentionally does not inspect or export the credentials/cookies.

The stable bundle identifier plus `WKWebsiteDataStore.defaultDataStore` is the persistence mechanism used in the successful live tests.

## Basic smoke

**This sends a real ChatGPT message. Do not run it from CI.**

```bash
make smoke
```

Equivalent direct invocation:

```bash
build/WKChatGPTAuthority.app/Contents/MacOS/WKChatGPTAuthority \
  --prompt 'Reply with exactly: WK_SEQ_OK' \
  --expect-marker WK_SEQ_OK \
  --timeout 35
```

Existing conversation:

```bash
build/WKChatGPTAuthority.app/Contents/MacOS/WKChatGPTAuthority \
  --url 'https://chatgpt.com/c/<conversation-id>' \
  --prompt 'Reply with exactly: WK_SEQ_OK' \
  --expect-marker WK_SEQ_OK \
  --timeout 45
```

Stop a long turn five seconds after submit:

```bash
build/WKChatGPTAuthority.app/Contents/MacOS/WKChatGPTAuthority \
  --prompt 'Write a very long report...' \
  --stop-after 5 \
  --timeout 40
```

Attachment:

```bash
build/WKChatGPTAuthority.app/Contents/MacOS/WKChatGPTAuthority \
  --attach /absolute/path/to/image.png \
  --prompt 'Describe the attached image.' \
  --timeout 60
```

The timings in this reproduction harness are intentionally conservative. Production integration should replace fixed sleeps with readiness/lifecycle events.

## Live verification matrix already completed

The following checks were completed against the paid account before this checkpoint was created:

| Gate | Result |
| --- | --- |
| Anonymous ChatGPT load | pass |
| Persistent paid login | pass |
| Plus account route/history | pass |
| Existing conversation continuation | pass |
| High reasoning | pass |
| Long High response | pass; final response 54,429 chars with requested marker |
| Stop in the same live WKWebView | pass; canonical turn completed with truncated response |
| Image attachment through real file chooser | pass; model identified the test image and returned requested marker |
| Connected CodexTool tool loop | pass; canonical graph contained assistant tool request, real tool result, and final marker |
| 10 sequential paid High short turns | **10/10 pass** |
| Canonical audit of those 10 turns | each had exactly 1 user message + 1 requested final response; no duplicate branch |
| Cloudflare/CAPTCHA/403 during the 10-turn series | none observed |
| Two simultaneous WKWebViews | functionally pass, but resource usage adds substantially; not recommended architecture |
| Three serialized submits with authority destroyed between them | pass; all three continued concurrently server-side and completed with their markers |

### Resource observations

These are point-in-time `ps` RSS/CPU observations, useful for direction rather than laboratory-grade memory accounting.

A logged-in old conversation with no DOM probing settled approximately as follows:

| elapsed | total new WK host/WebKit RSS | CPU snapshot |
| ---: | ---: | ---: |
| 5 s | ~631 MB | ~14% |
| 20 s | ~458 MB | ~7% |
| 60 s | ~290 MB | ~3.8% |
| 90 s | ~200 MB | ~3.6% |

A long High turn stayed roughly in the ~270–460 MB range in sampled points, but WebContent CPU was bursty and sometimes reached ~60–90%. WKWebView is therefore **not free CPU-wise**: the ChatGPT React frontend remains expensive while it is active.

Two simultaneous WKWebViews were the wrong architecture:

| elapsed | combined RSS | CPU snapshot |
| ---: | ---: | ---: |
| 10 s | ~914 MB | ~138% |
| 30 s | ~905 MB | ~36% |
| 60 s | ~801 MB | ~15% |
| 120 s | ~708 MB | ~13% |
| 145 s | ~650 MB | ~17% |

By contrast, three **serialized cold authority runs** each stayed around ~400–530 MB rather than stacking, although individual startup CPU bursts still reached roughly 100–120% in two runs. After submit each authority process was destroyed, while all accepted turns continued concurrently on the server.

This is the main reason the proposed production topology is a single serialized authority queue, not one web view per chat.

## Known weaknesses / unresolved work

1. **Large old conversation lazy-mount.** On the heavily inflated test conversation, an offscreen view sometimes failed to mount the composer even after ~30 s. Fresh conversations did not show this problem. Production code must use deterministic readiness/navigation rather than brute-force scrolling and fixed sleeps.
2. **Reattach to an already-running turn.** Stop works reliably in the same WKWebView that submitted the turn. Reopening a running conversation did not always immediately expose the live stop control. This needs a deliberate recovery/stop path.
3. **Startup CPU burst.** WebKit is much smaller than the Chromium path in memory, but starting/rendering the full ChatGPT React app can still consume a core briefly. Serialized authority prevents several such bursts from stacking but does not remove one burst.
4. **Streaming after authority release.** The server continues generation after WebKit exits, proven live. A production backend still needs a clean lightweight observation/delivery plane so gptty gets progressive output/finality without retaining the full page.
5. **Fixed timing in this harness.** The experiment uses conservative timers only to reproduce behavior. Production must wait for DOM/product lifecycle facts and be fail-closed around ambiguous submits.
6. **Frontend selectors can change.** Current successful selectors include `#prompt-textarea`, `textarea[aria-label="Chat with ChatGPT"]`, `data-testid="send-button"`, `data-testid="stop-button"`, and `#upload-photos`. Treat them as observed frontend details, not a stable API contract.

## Current proposed topology

```text
many gptty chats
      |
      v
serialized authority queue
      |
      v
one WKWebView authority
      |
      +-- submit / attach / tool UI / stop when live
      |
      v
accepted turn
      |
      +--> authority can be released
      |
      v
ChatGPT server continues reasoning independently
```

The queue is important: active server-side turns can be numerous, while local browser authority remains bounded to one WebKit instance.

## Next comparison work

Before promoting this experiment into the CWA/gptty production path, compare the same live gate against the remaining plausible alternatives (notably Camoufox and lightweight independent browser-engine candidates). If none beats this combination of compatibility, resource use, and reliability, use this checkpoint as the basis for the production WKWebView authority backend.
