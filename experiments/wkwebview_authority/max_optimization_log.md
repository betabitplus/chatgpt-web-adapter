# WK + curl_cffi maximum optimization log

Branch: `experiment/wk-curl-max-optimization`
Base checkpoint: `4fbd3e5` (`experiment/shared-wk-multiplex`)
Date: 2026-09-09

Goal: minimize RAM and CPU in both steady-state and peak while preserving text, image attachment, tool-loop, Stop, canonical finality, session safety, and exactly-once write behavior.

## Proven baseline before this branch

| Variant | Peak RAM delta | Long-tail / steady RAM | CPU | Functional status |
|---|---:|---:|---:|---|
| Old dual WK lifecycle | ~1.13 GB | ~233 MB | peak ~325%, steady ~3.6% | text/image/tool/Stop green |
| System-wide shared WK broker + heavy gate | ~818 MB | ~126–139 MB sustained tail; ~169 MB post-heavy median | steady ~1–2% | text/image/tool/Stop green; CWA 2133/2133; gptty 286/286 |
| `curl_cffi` controlled 1–10 streams | ~32 MB process peak | ~22–24 MB total | ~0% idle | controlled streaming/control/WebSocket green |
| Synthetic blank WK auth/read bootstrap | ~120–135 MB transient | exits immediately | short burst | authenticated read-path demonstrated |

## Rules

1. Keep installed user-facing `gptty`/CWA on stable `main`; experiments only in branches/source runs.
2. Change one optimization dimension at a time where possible.
3. Record measured RAM/CPU before accepting an optimization.
4. Reject any optimization that breaks text, image, tool-loop, Stop, canonical finality, session/auth correctness, or exactly-once write safety.
5. Protected browser-owned operations remain browser-owned unless an allowed, explicitly verified path proves otherwise. Do not weaken anti-abuse/browser verification boundaries.
6. Prefer fewer persistent processes and shared resources; no prewarming that improves latency by increasing idle RAM unless separately justified.

## Planned optimization sequence

1. Heavy WK resource-cost attribution and safe resource blocking.
2. Heavy WK viewport/rendering-surface minimization.
3. Heavy WK lifetime/phase shutdown tightening.
4. Shared broker idle footprint minimization without browser-timer regressions.
5. `curl_cffi` transport/process micro-optimization for allowed steady-state/read workloads.
6. Re-run full functional gates and cross-process benchmarks after every accepted bundle of changes.

## Results

### 2026-09-09 — Heavy page resource blocking

Resource inventory on the heavy `https://chatgpt.com/` page showed roughly 15 scripts, 16–17 stylesheets, ~100 `link`-initiated resources, and 20–30 startup fetches. Large decoded startup fetches included `/backend-api/models`, `/backend-api/conversations`, `/backend-api/accounts/check/...`, connectors, and prompt-library data.

Screening observations:

- Blocking `script` reduced memory but `composer_ready=false`; rejected.
- Blocking `style-sheet`, `image`, `media`, `font`, `raw`, `svg-document`, `other`, `ping`, or `websocket` did not produce a stable memory win; several increased peak/CPU; rejected.
- Blocking all `fetch` produced one low no-submit peak (~340 MB versus ~407 MB in that immediate comparison) while still hydrating the composer, but it necessarily blocks auth/submit/control fetches and therefore cannot be a production policy.
- Selective startup endpoint blocking was tested in four randomized round-robin runs. Median peak RAM (MB): baseline **545.2**, `accounts` **618.6**, `apps_sources` **569.7**, `celsius` **554.0**, `user_system_messages` **534.6**, combined **603.9**. The page itself varied from ~409–648 MB and no endpoint block produced a reproducible improvement.

Verdict: **reject startup resource/API blocking as a primary optimization**. Savings are not reproducible and feature risk is disproportionate. Keep default network behavior intact.

### 2026-09-09 — Viewport / rendering surface

Heavy page viewport screening found a shallow local minimum around `500x400`: randomized 4-run median was ~**550.9 MB** versus ~**594.5 MB** at `1000x700` (~7% improvement). Smaller viewports (`400x300`, `240x180`, `160x120`) did not improve further and often increased memory due to responsive-layout changes. Because the gain is small relative to page variance and adds responsive-layout feature risk, this optimization is **not being pursued**.

Shared broker viewport was also tested from `1000x700` down to `1x1`. Median idle/steady RAM stayed essentially flat: ~**114 MB** at `1000x700`, ~**107 MB** at `128x128`, ~**108 MB** at `32x32`, and ~**115 MB** at `1x1`; peak remained ~132–140 MB. This proves the broker cost is mostly WebKit/network/runtime floor, not rendering surface. Verdict: **reject broker viewport tuning**.

### 2026-09-09 — Heavy Phase-A lifetime

A live timeline split the full-page authority burst into stages: navigation finished at roughly **2.7 s**, hydrated composer ready at ~**4.1 s**, Send action at ~**4.8 s**, conversation POST observed around **6.2 s**, submit response 200 around **10.1 s**, and browser-owned resume credential observed around **10.6 s**. The helper exits immediately after the resume fence, so there is no meaningful local post-fence heavy-page tail left to trim. Most of the remaining 4+ seconds after POST dispatch are server/response latency. Verdict: **no large local lifetime optimization available after the resume fence**.

### 2026-09-09 — curl_cffi canonical-only long-tail

The same canonical endpoint that returned a JS challenge/403 through the ordinary CWA curl path returned **200 JSON** through `curl_cffi` with Safari impersonation. A turn also completed server-side without calling `/conversation/resume`, proving generation itself continues after Phase A exits.

However, progressive streaming is not available from canonical snapshots. In a 900-word live run, the assistant node appeared `in_progress` with `text_len=0` and stayed empty through the generation, then jumped directly to `finished_successfully` with **9028 characters** at the final poll. All 16 canonical polls returned 200 and final marker/finality were correct, but there were no usable intermediate text deltas.

Verdict: **canonical via curl_cffi is accepted for finality/recovery, rejected as the primary streaming transport** because it would regress live streaming.

### 2026-09-09 — curl_cffi + WebSocket second leg

A stronger transport split was proven: keep only the official WK Phase A through `RESUME_FENCE`, decode the already-observed turn topic identity locally, use `curl_cffi` Safari impersonation for the current `/backend-api/celsius/ws/user` bootstrap, then use the existing CWA WebSocket topic parser for the live second leg. The current Celsius endpoint returned **200 + wss://ws.chatgpt.com** through curl_cffi while the ordinary CWA curl path received a JS challenge/403.

Single-turn live proof (500-word response): **55 separate live delta callbacks**, first delta ~15.8 s, last ~28.4 s, exact marker present in stream and canonical, streamed text length **3934** exactly matched canonical length **3934**, `passive_observer_armed=false`, and no WK resume broker was used.

Clean two-process 700-word benchmark after prebuilding the helper once:

- both independent processes returned rc=0;
- delta callbacks: **90** and **80** (88/80 distinct callback times);
- both stream markers and canonical markers present;
- both streamed texts exactly matched canonical;
- both `passive=false`;
- maximum simultaneous heavy authority helper: **1**;
- peak RAM delta: **~634.6 MB**;
- peak CPU: **~182%**;
- post-heavy median RAM: **~56.6 MB total** for the two worker processes;
- post-heavy range: **~33–74 MB** while active;
- post-heavy median CPU: **~0.5%**.

Compared with the shared-WK checkpoint (~818 MB peak, ~169 MB post-heavy median, ~126–139 MB sustained WK tail, ~1.6% steady CPU), this is approximately **22% lower peak RAM**, **66% lower post-heavy median RAM**, and roughly **70% lower steady CPU**, while preserving real progressive streaming and canonical equivalence.

A separate cold-start two-process benchmark exposed a helper-build race when both processes tried to create/sign the same helper app simultaneously. The benchmark was repeated with one prebuild and then passed; the cold-build race is tracked as a functional issue to fix with an OS-wide build lock before accepting the new transport.

Verdict so far: **major optimization candidate accepted for continued feature validation**. Required remaining gates: image attachment, Stop, tool-loop, cross-process/cold-start build locking, full CWA/gptty suites, and cleanup/security review.

### 2026-09-09 — Cold-start build lock closed

The helper builder now takes an OS-wide `flock` on `wkwebview-authority/.build.lock` in addition to the in-process lock and re-checks the source digest after acquiring it. A clean two-process cold-start test removed the state directory first, launched two builders concurrently, and both returned rc=0 with the same helper binary. The binary, digest stamp, and lock file were all present. Total concurrent cold-build wall time was about **1.9 s**.

Verdict: **accepted**. The previously observed simultaneous clang/codesign race is closed.

### 2026-09-09 — Canonical identity/finality hardening

Multi-turn continuation exposed a propagation race: after the WebSocket leg ended, a canonical GET could briefly return the previous completed assistant. The old predicate accepted any completed assistant and could therefore cache the previous node as the parent for the next turn.

The finality rule is now write-bound across Phase A, curl+WS canonical polling, and shared/native resume canonical bodies. A canonical payload is cacheable only when its current node has advanced beyond the baseline parent, the active branch contains the current user text, and the current assistant is final. `_cache_final_payload` also updates the canonical current-node cache atomically with the cached payload. Regression coverage includes repeated identical user text so text matching alone cannot admit a stale previous turn.

Verdict: **accepted reliability fix**. Targeted WK backend suite: **17/17 passed**.

### 2026-09-09 — `num_turns` / Statsig layer experiment

Frontend reverse engineering confirmed `2605344799` is a Statsig **Layer** and `getLayer("2605344799").get("num_turns")` resolves to **10**. The official layer override API was also identified. However, even a no-op `overrideLayer(..., {num_turns: 10})` changed enough client state to make continuation unreliable, and earlier JSON/bootstrap interception was similarly non-transparent.

Verdict: **reject changing the frontend conversation pagination window**. Keep the official `num_turns=10` behavior intact.

### 2026-09-09 — Rate-limit diagnosis and cleanup/security review

Repeated live E2E writes eventually produced `SEND_CONTROL_NOT_READY`. DOM diagnostics showed this was not a hydration failure: ChatGPT was displaying its own "Too many requests" guard and temporarily limiting conversation access. The helper now classifies that known state as `WKWEBVIEW_CHATGPT_RATE_LIMITED` instead of misreporting a generic send-control failure.

Rejected experiment-only production hooks were removed: resource/content blocking, authority/broker viewport overrides, resource-probe mode, Statsig overrides, and refill-based Send recovery. Review also found and removed an unused early-experiment access-token handoff that could send the browser access token to native code and write it to a file. The accepted curl+WS path does not require that handoff; it decodes only the already-observed resume token metadata needed to identify the turn topic and does not persist the raw resume token beyond the existing mode-0600 temporary handoff file, which is unlinked after Phase A.

Native helper recompilation succeeded after cleanup. Full CWA suite on the cleaned branch: **2135/2135 passed**. The downstream gptty suite, run against this CWA source tree via `PYTHONPATH`, also passed **286/286** without modifying gptty.

### 2026-09-09 — Final curl+WS live feature gates

After the temporary ChatGPT write-rate guard cleared, a single short continuation confirmed the classifier was no longer active and the curl+WS path remained `passive=false` with exact streamed output.

- **Image attachment:** one real image attachment was accepted in browser-owned Phase A (`attachment_count=1`); second-leg streaming remained `passive=false`; streamed response was exact, canonical response was exact, and stream matched canonical.
- **Stop:** write identity resolved and progressive deltas were observed before Stop; `stop_generation` returned `stopped=true`; the worker exited without passive fallback; canonical finish was `interrupted / client_stopped`; the deliberately final-only marker appeared in neither stream nor canonical.
- **Tool-loop:** a real connected CodexTool workflow completed through `list_resources` and the tool invocation. Canonical evidence contained **2 tool-role nodes**, non-`all` recipients `api_tool.list_resources` and `api_tool.call_tool`, and **4 toolish nodes** total. The final marker was exact, `passive=false`, and streamed text matched canonical.

Verdict: **all required live feature gates are green** for the WK Phase-A → curl_cffi + WebSocket second-leg candidate: text/progressive streaming, multi-turn continuation, image attachment, Stop, tool-loop, canonical finality, exactly-once/current-node safety, cross-process heavy gating, and cold-start helper building. No rollout or installation has been performed from this experiment branch.

Final lifecycle cleanup made both `curl_cffi` HTTP sessions deterministic context-managed resources: the Celsius bootstrap session closes before the WebSocket leg, and the canonical polling session closes on success or exception instead of relying on GC. A post-change live continuation remained `passive=false` with exact streamed output. Changed-file Ruff passed, the final CWA suite remained **2135/2135**, and downstream gptty remained **286/286** against this exact source state.

### 2026-09-10 — Minimal browser security shell replaces the full ChatGPT SPA in Phase A

The largest remaining cost was isolated to the ChatGPT application WebContent process rather than WKWebView itself. A self-contained `loadSimulatedRequest` shell was therefore tested at the real `https://chatgpt.com/` origin while keeping the normal WK website data store/session. The shell does not hydrate the ChatGPT UI. It loads only the server-provided bootstrap/security resources required for the browser-owned conversation write, then exits at the same existing `RESUME_FENCE`; the accepted `curl_cffi` + WebSocket second leg remains unchanged.

Read-only probes first proved that the simulated origin retained authenticated same-origin API access. Loading the consumer Sentinel frame/SDK separately kept the WebContent process near the small-shell floor rather than the full SPA cost. The official conversation integrity module was then loaded from the current server HTML and used inside the real WK browser context. No external challenge solver or synthetic proof generator is used.

A live protected new-chat write from this shell returned **HTTP 200**, produced a real conversation id and resume credential, and reached `RESUME_FENCE`. The same shell was then wired into `WKWebViewTurnProvider` behind the opt-in `CWA_WK_MINIMAL_SECURITY_SHELL=1` gate, which also requires the curl+WS second leg. A full provider run proved:

- `minimal WK shell -> protected write -> RESUME_FENCE -> curl_cffi/WebSocket -> canonical`;
- `passive=false`;
- streamed marker exact;
- canonical marker exact;
- streamed text exactly equals canonical text;
- canonical current-node cache populated.

Measured full single-provider pipeline peak was **~235.3 MB**, with **~42.2 MB** post-WK median and **~0.3% CPU** after the heavy phase. This is roughly **~360 MB below** the representative ~598 MB full-SPA Phase-A measurement and substantially below the earlier ~563–635 MB curl+WS candidate peaks that still loaded the full ChatGPT application.

The current ChatGPT asset deploy changed during the experiment and renamed minified `conversation-small-*` exports, immediately proving that fixed export aliases were not a viable production contract. The shell now discovers the current integrity helper/initializer from the official module by stable structural markers, resolves the current export aliases dynamically, and caches only those non-secret alias names per asset URL. The discovery logic was verified against two different live asset revisions and the newer revision completed a 200/resume-fence write after the old aliases had become invalid.

### 2026-09-10 — Minimal-shell product semantics and feature gates

Model/profile behavior is server-driven rather than hardcoded. The shell obtains the authenticated model catalog and preserves the current product mapping. A live normal HIGH submit on the full SPA established the wire contract as `gpt-5-6-thinking` with `thinking_effort=extended`; the minimal HIGH shell then completed a protected **200 + RESUME_FENCE** write with the same profile semantics. Default/no-profile turns continue to use the server-selected default/auto route.

Existing-conversation support uses the canonical current assistant message as `parent_message_id`, preserves the conversation id, carries the canonical model/effort selection when no explicit profile is requested, and remains write-bound to the previous canonical current node. A live two-turn HIGH gate passed end-to-end:

- first and second turns both `passive=false`;
- both stream and canonical outputs exact;
- same conversation id on both turns;
- canonical current node advanced;
- both assistant turns remained `gpt-5-6-thinking` + `extended`.

Attachment support performs the existing authenticated media upload before opening WK, passes only the returned file descriptor metadata into the minimal shell, constructs the normal multimodal conversation message, and then uses the same protected write/resume fence. Live image gates passed:

- upload returned one real image file id with PNG metadata/dimensions;
- minimal Phase A returned **HTTP 200 + RESUME_FENCE**, `attachment_count=1`;
- full provider/curl+WS run correctly recognized the solid-red image;
- stream exact, canonical exact, stream equals canonical;
- canonical user message remained `multimodal_text` with one attachment;
- the server normalized the submitted file pointer to the canonical `sediment://` scheme, confirming attachment identity was preserved.

Fail-safe behavior remains conservative. If pre-upload is unavailable/fails, the provider falls back to the original SPA attachment path. If an existing conversation lacks a usable current assistant parent, or its required model/effort selection cannot be safely preserved, minimal continuation is not attempted and the full SPA path remains available. Explicit `model_slug` is still excluded from the minimal path until separately characterized.

Stop and tool-loop were also re-run specifically with the minimal shell enabled rather than relying on the earlier full-SPA Phase-A evidence:

- **Stop:** write identity and progressive deltas were observed before Stop; `stop_generation` returned `stopped=true`; the worker exited `passive=false`; canonical finish was `interrupted / client_stopped`; the deliberately final-only marker appeared in neither stream nor canonical.
- **Tool-loop:** a real connected CodexTool workflow completed through `api_tool.list_resources` and `api_tool.call_tool`; canonical evidence contained 2 tool-role nodes and 4 toolish nodes; the final marker was exact, `passive=false`, and streamed text exactly matched canonical.

### 2026-09-10 — Minimal-shell cross-process resource gate

A clean two-process benchmark launched two independent minimal-shell + curl/WS turns concurrently under the existing OS-wide heavy-submit gate:

- both workers returned rc=0;
- both remained `passive=false`;
- both stream markers and canonical markers were exact;
- both streamed texts exactly matched canonical;
- **maximum simultaneous heavy WK helpers: 1**;
- peak combined RAM delta: **~272.6 MB**;
- peak CPU: **~106.8%**;
- post-heavy median RAM: **~28.5 MB** for the active worker processes;
- post-heavy median CPU: **~0.9%**.

This confirms that replacing the full SPA does not regress cross-process heavy-phase serialization and reduces the two-request peak by hundreds of megabytes versus the previous full-SPA curl+WS benchmark.

Final regression checkpoint after the continuation/image/cross-process gates: JavaScript syntax check passed, the Objective-C helper compiled cleanly, changed-file Ruff passed, the targeted WK suite passed **23/23**, the full CWA suite passed **2141/2141**, and downstream gptty passed **286/286** against this exact CWA source tree.

Final cleanup removed one-off `.tmp_*` probes, generated probe `.app` bundles, the incidental untracked `uv.lock`, and local build/dist output. The retained branch surface is limited to the provider/native-helper implementation, the packaged minimal-shell JavaScript resource, dependency/package-data metadata, regression coverage, and this experiment record. A real wheel build verified that `wkwebview_helper/minimal_security_shell.js` is included in the installed package; this caught and fixed a package-data omission that source-tree tests alone would not detect. The full regression checkpoint above was repeated after the cleanup changes and remained green.

Current status: the minimal security shell is the strongest optimization candidate on this branch and the technical feature/performance gates are green, but it remains **opt-in only**. No merge, installed-default change, or consumer rollout has been performed. Promotion to default remains a separate policy decision.
