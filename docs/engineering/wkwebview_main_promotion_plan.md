# WKWebView Lightweight Authority — Promotion Plan (Closed)

_Status: CLOSED · engineering work complete; Chrome remains stable main, WK retained as tested alternative_
_Last audit: 2026-09-11_
_Canonical tracker for the completed WK hardening/promotion investigation: this file_

This document started as the single source of truth for promoting the lightweight WKWebView authority experiment into the primary macOS browser-authority implementation. The engineering work was completed and live-proven, but the final product decision on 2026-09-11 was to keep the proven Chrome browser-native implementation as stable `main` and retain the optimized WK implementation as a separate, fully tested alternative. The default-promotion and public-release steps are therefore superseded, not unfinished.

Related evidence:

- Performance/live experiment evidence is retained on frozen branch `experiment/wk-curl-max-optimization` at `experiments/wkwebview_authority/max_optimization_log.md`; the pre-release branch intentionally does not retain the experiment harness/files.
- Frozen CWA experiment/evidence branch: `experiment/wk-curl-max-optimization` at promotion fork point `ddf371b`. Do not rewrite or clean this branch; it is the reproducible checkpoint for the working alternative implementation and experiment history.
- Historical CWA pre-release hardening branch: `prerelease/wkwebview-main-hardening`.
- Final tested WK alternative: `integration/upstream-wkwebview-0.3.1`; transport-integration baseline `1e4e972fa2f219e2363456e78bebc7f3ca00b7b5`, public catalog delegation restored in `c7c17ed`, and the complete gptty read/resume surface restored in `8fd303b`.
- Stable Chrome main: `main` / `origin/main` at `524f400c74da6fdf8ff3aa2ab22cc497b8b69168`.
- gptty integration branch: `feature/wkwebview-authority-e2e`.

## Final disposition — 2026-09-11

- **Stable/default implementation:** Chrome browser-native stays on `main` at `524f400`. It was rechecked from a clean checkout with full CWA regression (2115/2115), downstream gptty (289/289), and a real authenticated browser-owned turn returning exact `CWA_CHROME_MAIN_LIVE_AUTH_OK_20260911` with HTTP 200, canonical readback and observed model `gpt-5-6-thinking`.
- **Alternative implementation:** optimized WK stays on `integration/upstream-wkwebview-0.3.1` at `1e4e972`. Final integration verification passed full CWA 2252/2252, downstream gptty 289/289, quality/JS/native/package/install gates, live Stop/Temporary/exact-model/file/image coverage, and the four-process new/continuation/mixed load matrix with globally serialized Phase A and lightweight curl/WebSocket finality.
- **Historical evidence:** experiment and prerelease branches are retained. In particular `experiment/wk-curl-max-optimization` remains immutable evidence and must not be cleaned or rewritten.
- **Release/default promotion:** canceled by product decision. `WK-MAIN-013`, `WK-MAIN-014` and `WK-MAIN-015` are `SUPERSEDED`; there is no remaining requirement to make WK the implicit macOS default or publish the staged WK CWA/gptty releases.
- **Upstream boundary:** no write interaction with the foreign `kymuco/chatgpt-web-adapter` repository is part of this plan. The user's fork may retain and publish its own branches/checkpoints as needed.

## Target architecture

```text
multiple gptty processes
        |
        +--> lightweight canonical reads ---------------------- curl_cffi
        |
        +--> global protected-write gate
                  |
                  v
          short-lived minimal WKWebView
          official browser-owned security/write semantics
                  |
                  +--> normal RESUME_FENCE ------> curl_cffi + WebSocket stream
                  |
                  +--> terminal continuation ----> canonical finality
                  |
                  +--> accepted/no fence --------> exact client-message-id
                                                   curl catalog/canonical recovery
                  |
                  v
             canonical finality
```

Required invariants:

- browser-protected mutation remains owned by real WebKit and current product resources;
- no challenge solving, token fabrication/replay, or reconstructed anti-abuse machinery;
- exactly one protected write attempt per logical turn unless the caller explicitly starts another turn;
- browser authority is released only after a normal resume fence, an already-terminal known continuation, or a bounded HTTP-accepted handoff into exact client-message-id canonical recovery; it is not retained through finality merely for observation;
- expensive browser Phase A is globally serialized; lightweight reads/streams/recovery remain parallel;
- canonical current-node/write matching remains final authority;
- failure paths fail closed or use an explicitly documented compatibility fallback;
- secrets, prompts, and protected credentials do not appear in stdout, logs, process argv, or persistent temp files;
- the WK alternative must be installable from the built wheel, not only runnable from a source checkout.

## Status legend

- `OPEN` — not started or not proven.
- `IN PROGRESS` — implementation/research active.
- `DONE` — implemented and locally/regression tested.
- `LIVE PROVEN` — exercised successfully against the real product path.
- `DEFERRED` — intentionally postponed with a documented reason.
- `REJECTED` — investigated and intentionally not adopted.
- `SUPERSEDED` — implementation/work was completed or staged, but the original product/release objective was explicitly replaced by the final dual-backend decision; no action remains.

## Promotion work (closed)

| ID | Priority | Status | Item | Required outcome |
|---|---|---|---|---|
| WK-MAIN-001 | P0 | LIVE PROVEN | Remove turn data from process argv | Prompt, conversation identifiers where avoidable, attachment descriptors, model/profile data and other per-turn payload move to stdin or inherited/private IPC. `ps` must not expose prompt text. |
| WK-MAIN-002 | P0 | LIVE PROVEN | Remove named resume-secret handoff file | Replace `cwa-wk-resume-*` file transport with an inherited pipe/FD or unlink-after-open equivalent. Abrupt process death must not leave resume credentials on disk. |
| WK-MAIN-003 | P0 | DONE | Refactor `wkwebview_provider.py` orchestration | Provider reduced from >2.1k lines to ~814 by extracting native helper lifecycle, lightweight transport, canonical state, passive observer/Stop lifecycle and turn orchestration. Provider now primarily coordinates the separated components. |
| WK-MAIN-004 | P0 | LIVE PROVEN | Replace private `ChatGPTWebClient` coupling | Provider now consumes an explicit `WKLightweightTransport` / source-client contract and no longer reaches into the listed private methods. New-chat, continuation and image live gates passed on the refactored boundary. |
| WK-MAIN-005 | P0 | DONE | Remove/isolate superseded resume implementations | Removed the superseded shared-WK broker/daemon/socket/env path. The remaining direct-WK resume is isolated in an explicit compatibility fallback method; curl/WS remains the primary second leg. |
| WK-MAIN-006 | P0 | DONE | Add macOS availability contract | WK helper construction now fails closed unless the host is Darwin with an identifiable macOS version >=12. Unit regressions cover macOS 11 and unknown-version rejection; the current macOS live WK path still passes. |
| WK-MAIN-007 | P0 | DONE | Add macOS CI/release coverage | CI now has a blocking `macos-latest` WK job covering packaged JS syntax, `clang -Wall -Wextra -Werror`, targeted WK/release tests, artifact build/release gate, and installed-wheel helper build from `site-packages`; local equivalents are green. |
| WK-MAIN-008 | P0 | DONE | Make branch pass repository quality gate | `uv run python tools/engineering_quality_gate.py --base-ref main` passes: Ruff lint is clean, all 14 `main...HEAD` Python delta files satisfy Ruff formatting, and the architecture-debt checks report no new PR/repair-named production modules or module-level runtime mutation. On the 2026-09-11 current candidate the focused WK/lightweight/stream suite is 61/61, full CWA is 2188/2188, and downstream gptty is 289/289. |
| WK-MAIN-009 | P0 | DONE | Native helper warning-clean | `clang -Wall -Wextra -Werror` passes after cleaning the `main` parameter and attachment-count warning paths. |
| WK-MAIN-010 | P0 | LIVE PROVEN | Correct canonical transport provenance | Canonical plane is now transport-neutral `WKWEBVIEW_CANONICAL_READ`; actual reads report `curl_cffi`, `wkwebview`, or `cache` separately. A live continuation reported `canonical_read_transport=curl_cffi` while preserving conversation identity. |
| WK-MAIN-011 | P0 | LIVE PROVEN | Add lightweight transport/fallback observability | Existing browser-owned provenance reports canonical read transport/fallback, Phase-A implementation/gate wait/elapsed time and Phase-B implementation/fallback/elapsed time. Normal lightweight turns report `curl_cffi_websocket`, already-terminal Phase A reports `phase_one_terminal`, and the accepted-write identity recovery added by `WK-MAIN-016` reports the distinct `canonical_message_id_recovery` path instead of being hidden as a generic terminal turn. Fallback reasons remain bounded safe codes rather than arbitrary exception text. |
| WK-MAIN-012 | P0 | DONE | Stabilize minimal-shell upstream-drift tests | Two sanitized known `conversation-small-*` layout fixtures now exercise the exact production helper/initializer/export discovery in direct and aggressively chunked streaming modes. The fixtures exposed and now guard a premature partial-export-alias bug; live minimal-shell protected write passed after the fix. |
| WK-MAIN-013 | P0 | SUPERSEDED | Promote the primary macOS browser-authority backend | The pre-release selector promotion was implemented and live-proven (`WK_FINAL_IMPLICIT_DEFAULT_OK_20260911`), but it is intentionally not the final stable policy. Stable `main` remains Chrome browser-native; WK is retained as an explicitly selected alternative on the integration branch. No promotion action remains. |
| WK-MAIN-014 | P0 | SUPERSEDED | Make the promoted runtime distributable as a new package version | The WK candidate was made release-ready locally: 0.3.1 artifacts, `twine check`, release gate and installed-wheel smoke all passed. Public WK promotion/release is intentionally canceled by the final dual-backend decision, so publication/post-publish verification is no longer an acceptance requirement. |
| WK-MAIN-015 | P0 | SUPERSEDED | Make the downstream gptty rollout distributable without accepting old CWA | The gptty 0.1.2 WK rollout was staged and package/install-smoke proven. The corresponding public dependency-floor and release transition is intentionally canceled with the WK default/public-release decision. No rollout action remains. |
| WK-MAIN-016 | P0 | DONE | Recover an HTTP-accepted protected write that misses the resume fence without replaying it | The minimal shell emits its unique client user-message id before submit. After a successful product response, missing resume/terminal evidence is given only a bounded grace period; the WK helper then releases the heavy-submit gate with an explicit recovery state instead of waiting for the whole turn timeout or retrying the write. CWA performs read-only `curl_cffi` recent-catalog/canonical lookup, requires the exact client message id plus prompt, waits for canonical finality and caches that payload. Unit coverage proves no second write/WS resume occurs. The real pre-fix stalled marker was independently found in canonical history, confirming the accepted-write failure mode; post-fix four-process mixed load is 4/4 exact with `max_phaseA=1` and no fallback. |

## Feature-parity work

| ID | Priority | Status | Item | Required outcome |
|---|---|---|---|---|
| WK-MAIN-020 | P1 | LIVE PROVEN | Temporary Chat parity/routing | Temporary Chat is self-contained on WK: the minimal WebKit write is browser-observed with `history_and_training_disabled=true`, final text comes from the resumed product WebSocket without durable canonical GET, same-runtime continuation is bound to process-local lifecycle identity + latest assistant parent, and explicit end destroys that binding. A two-turn live Temporary session preserved one ephemeral conversation and ended cleanly; the current 2026-09-11 candidate also returned exact `WK_FINAL_TEMP_OK_20260911`. |
| WK-MAIN-021 | P1 | LIVE PROVEN | Exact model selection parity/routing | Explicit `model_slug` stays on the minimal WK path and is resolved against the live product model catalog. The current candidate returned exact `WK_FINAL_MODEL_SINGLE_OK_20260911` with `observed_model=gpt-5-6-thinking`; no compatibility route was required. |
| WK-MAIN-022 | P1 | LIVE PROVEN | General attachment parity | General files are uploaded through the authenticated ChatGPT file create/upload/finalize flow in `WKLightweightTransport` before descriptor handoff to the minimal browser-owned write. On 2026-09-11 the model read an exact marker from a generated `.txt` file and returned `WK_FINAL_FILE_SINGLE_OK_20260911`; image upload keeps its existing media path. |
| WK-MAIN-023 | P1 | LIVE PROVEN | No-stream resilience | The passive SSE observer flushes `TextDecoder` plus any residual unterminated final SSE block and never replays the protected write. Exact-JS regression covers the EOF case, while `WK-MAIN-016` separately covers an HTTP-accepted write that never yields a resume fence. Existing five-new/one-continuation no-stream evidence remains green, and the current candidate returned exact `WK_FINAL_NOSTREAM_OK_20260911`. |
| WK-MAIN-024 | P1 | DONE | Continuation canonical pre-read uses lightweight transport | Normal continuation pre-read prefers authenticated Safari-impersonated curl and retains WK only as fail-safe fallback. The 2026-09-11 four-process continuation gate was 4/4 exact at ~406.7 MB peak, every pre-read reported `canonical_read_transport=curl_cffi`, and `max_phaseA=1`. |
| WK-MAIN-025 | P1 | LIVE PROVEN | Continuation identity/current-node safety | Existing-conversation minimal turns preserve conversation identity, advance current node and keep exact canonical matching; the 2026-09-11 four-process continuation gate was 4/4 exact on the original four conversation identities. |
| WK-MAIN-026 | P1 | LIVE PROVEN | Image attachment path | Real image upload + minimal protected write + curl/WS + canonical finality passed. On the current candidate the model inspected a locally generated solid-red PNG and returned exact `WK_FINAL_IMAGE_SINGLE_OK_20260911`. |
| WK-MAIN-027 | P1 | LIVE PROVEN | Stop semantics | Stop is triggered only after real assistant text begins. On 2026-09-11 the provider returned `stopped=true` with stream-status proof, the send completed with `finish_reason=stopped`, the forbidden tail marker was absent, and bounded subsequent canonical readback showed `finish_details.type=interrupted` / `reason=client_stopped`. |
| WK-MAIN-028 | P1 | LIVE PROVEN | Tool-loop semantics | Minimal Phase A followed by real tool calls and final canonical response passed with stream == canonical. |
| WK-MAIN-029 | P1 | LIVE PROVEN | HIGH/DEEP profile semantics | Current wire contract remains thinking model + extended effort; the 2026-09-11 candidate returned exact `WK_FINAL_DEEP_OK_20260911`, while explicit-model evidence independently observed `gpt-5-6-thinking`. |

## Maintainability and cleanup

| ID | Priority | Status | Item | Required outcome |
|---|---|---|---|---|
| WK-MAIN-030 | P1 | DONE | Split minimal shell into named stages | Minimal shell now has explicit helpers for bootstrap, integrity discovery/runtime, session/catalog, model resolution, integrity acquisition, prepare and protected write; the entrypoint only orchestrates stages and has an offline structural regression. |
| WK-MAIN-031 | P1 | DONE | Tighten exception/fallback boundaries | WK production path now fails closed for auth, schema, source-contract and programming defects; passive/SPA fallback is limited to explicit transport/timeouts/retryable HTTP cases. Remaining broad catches only isolate documented observational callbacks. |
| WK-MAIN-032 | P1 | DONE | CWA owns lightweight transport dependencies | `curl-cffi` and `websockets` are now Darwin-only CWA runtime dependencies. gptty depends only on CWA and no longer owns transport-library names or versions; installed metadata and a live source-run prove resolution through CWA. |
| WK-MAIN-033 | P1 | DONE | Update architecture documentation | `docs/architecture.md` documents WebKit session ownership, the global Phase-A gate, stdin/anonymous-FD security boundary, macOS 12+ requirement, normal resume-fenced curl/WS flow, already-terminal continuation flow, bounded accepted-write client-message-id recovery and the no-replay fail-closed policy. |
| WK-MAIN-034 | P1 | DONE | Extend JS/package release gates | Package JS syntax validation now includes `wkwebview_helper/minimal_security_shell.js`; release gate requires WK `.m`/`.plist`/`.js` resources in wheel and sdist, and installed-wheel smoke verifies the helper resource directory from site-packages. |
| WK-MAIN-035 | P1 | DONE | Add gptty macOS integration CI | gptty CI now has a blocking `macos-wk-installed` job that checks out the pinned CWA promotion candidate from the writable `betabitplus/chatgpt-web-adapter` fork, builds both exact wheels, installs them together, runs `pip check`, and executes an offline installed-artifact smoke. The smoke proves both imports come from `site-packages`, CWA—not gptty—owns the Darwin curl/WebSocket dependencies, gptty assembles `WKWebViewTurnProvider`, and the native helper builds from packaged CWA resources. Local reproduction returned `GPTTY_WK_INSTALLED_SMOKE_OK`; full gptty is 288/288 green. |
| WK-MAIN-036 | P2 | DONE | Prepare clean merge history/checkpoint | Terminal Git was explicitly authorized on 2026-09-11. The previously dirty prerelease state was checkpointed as `d05d521` (`Promote WKWebView authority prerelease candidate`), then normalized with the project lock-pinned Ruff 0.16.6 as `d274c25` (`Normalize prerelease candidate with locked Ruff`). This provides a recoverable clean post-selector candidate before release finalization; no history rewrite or shell workaround outside normal Git operations was needed. |
| WK-MAIN-037 | P2 | DONE | Remove obsolete comments/names after refactor | WK runtime/test naming now reflects the final topology: streaming provenance is `WKWEBVIEW_RESUME_FENCED_PRODUCT_STREAM`, the explicit fallback is `wkwebview_direct_resume` / `_resume_via_direct_wk`, and the obsolete compatibility-routing test name/claim was replaced by native product-routing coverage. Exact stale-identifier search is clean. |

## Final repository cleanup before acceptance

This is a deliberate end-of-hardening phase, not opportunistic cleanup while implementation is still moving. The frozen experiment branch keeps the research history; the pre-release branch should contain only what belongs in the eventual `main` implementation.

| ID | Priority | Status | Item | Required outcome |
|---|---|---|---|---|
| WK-MAIN-040 | P1 | DONE | Remove experiment-only runtime code | The packaged helper no longer contains the superseded shared-WK resume broker (`RunResumeBroker`, iframe broker scripts/delegate/scheduler, `--resume-broker`) or stale stream-probe IPC terminology. Provider refactor shims without consumers were also removed. Strict native compile, targeted 75/75, full CWA 2170/2170, downstream gptty 287/287 and live `WK_MAIN040_CLEAN_RUNTIME_OK_20260910` all pass on the cleaned runtime. |
| WK-MAIN-041 | P1 | DONE | Remove experiment-only files/assets | The pre-release tree no longer contains the old `experiments/browser_native_bridge` or `experiments/wkwebview_authority` harnesses, duplicate helper source/plist/Makefile, local optimization log, PR8.0 feasibility doc, stress verifier example, or tests that existed only to validate those deleted prototypes. Production resources live only under `src/chatgpt_web_adapter/...`; experiment history/evidence remains on frozen branch `experiment/wk-curl-max-optimization`. |
| WK-MAIN-042 | P1 | DONE | Dead-code/import/config sweep | WK-specific source/config search is clean for shared-broker/runtime experiment debris and obsolete `wk-curl` packaging. The two experimental enable flags have now been removed from production/tests; the explicit WK backend uses its lightweight topology by default and retains only `CWA_WK_FORCE_LEGACY=1` as the emergency full-page/direct-WK escape hatch. Changed-scope Ruff is clean. |
| WK-MAIN-043 | P1 | LIVE PROVEN | Public capability parity diff | Programmatic comparison now shows `wkwebview` and `chrome-native` publish identical states for every `PRODUCT_CAPABILITY_NAMES` entry. WK explicitly declares live-proven general-file and multimodal-continuation support; a same-conversation live continuation with a solid-red PNG returned exact `WK_MAIN043_MULTIMODAL_CONT_OK_20260910`. A regression test locks the complete capability-state parity, and downstream gptty remains 287/287 green against the candidate. |
| WK-MAIN-044 | P1 | DONE | Documentation/history cleanup | `docs/architecture.md` now describes the two-phase WK lifecycle, provider-specific rich-input mechanics, direct-WK fallback and capability parity without requiring experiment narrative; `docs/release_checklist.md` carries the WK public-capability parity gate. References to deleted feasibility/stress assets and stale WK identifiers are gone from normal docs. The promotion tracker alone retains an explicit frozen-branch evidence pointer. |
| WK-MAIN-045 | P1 | DONE | Clean installed-artifact audit | From detached clean checkout `d274c25c5957cc3b21b0023a1980553b3d4b69ef`, fresh `0.3.1` wheel+sdist built successfully, `twine check` passed, the strengthened release/content gate returned `ok=true`, and installed-wheel smoke imported from `site-packages` with `wk_helper_build=true`. Packaged content contained 120 extension files and 3 WK helper resources and retained the experiment/cache/temp/local-path/removed-broker exclusions. |
| WK-MAIN-046 | P1 | DONE | Clean-tree reproducibility | A detached checkout of exact candidate `d274c25c5957cc3b21b0023a1980553b3d4b69ef` created its own environment from project metadata/lock and passed full CWA 2188/2188, engineering quality gate with lock-pinned Ruff 0.16.6, 114 packaged-JS syntax checks, strict native `-Wall -Wextra -Werror` compile, docs/release 37/37, fresh 0.3.1 artifact build, release gate and installed-wheel helper smoke. Tracked source remained clean before and after the acceptance runs. Downstream gptty 289/289 also passed with imports explicitly resolved from this exact checkout and implicit backend `wkwebview`. |

## Already accepted architecture/performance evidence

These items are not reasons to redesign the solution unless later evidence invalidates them.

| ID | Status | Evidence |
|---|---|---|
| WK-EVID-001 | LIVE PROVEN | Minimal real protected write reaches HTTP 200 and `RESUME_FENCE` without loading the full ChatGPT SPA. |
| WK-EVID-002 | LIVE PROVEN | Full path `minimal WK → protected write → RESUME_FENCE → curl/WS → canonical` returns exact stream/canonical response. |
| WK-EVID-003 | LIVE PROVEN | Single-provider measured peak ~235 MB versus roughly ~598 MB for full-SPA Phase A. |
| WK-EVID-004 | LIVE PROVEN | Four simultaneous new/continuation/mixed gptty workloads retain `max_phaseA=1`; lightweight streams and canonical reads overlap in parallel. |
| WK-EVID-005 | LIVE PROVEN | 2026-09-11 release-load rehearsal: four new chats were 4/4 exact at ~353.7 MB peak, four simultaneous continuations were 4/4 exact at ~406.7 MB with all canonical pre-reads on `curl_cffi`, and mixed two-new/two-continuation was 4/4 exact at ~424.9 MB. Every run retained `max_phaseA=1`, used lightweight Phase B without fallback, and left no persistent WK helper. |
| WK-EVID-006 | LIVE PROVEN | Dynamic integrity-export discovery survived a real upstream `conversation-small-*` deployment that renamed minified exports. |
| WK-EVID-007 | DONE | Wheel packaging includes `minimal_security_shell.js`; source-tree-only package-data omission was fixed. |
| WK-EVID-008 | DONE | Historical experiment evidence: gptty temporarily carried the lightweight transport dependencies while the topology was being proven. This ownership was superseded by `WK-MAIN-032`; the release candidate now correctly makes CWA the Darwin curl/WebSocket dependency owner. |
| WK-EVID-009 | DONE | A real pre-fix mixed-load turn returned HTTP acceptance but never emitted a resume fence; its exact prompt marker was later found in canonical history, proving that replay would have duplicated an accepted write. `WK-MAIN-016` now handles this state through bounded read-only client-message-id recovery and exposes it distinctly as `canonical_message_id_recovery`. |

## Acceptance gates for the alternative candidate

These gates were used to prove that the WK implementation was production-quality before the final policy decision. They were completed for the candidate and remain evidence for the alternative implementation; they no longer imply a public release or default promotion.

Run and record all of the following on the exact cleaned candidate commit:

- repository engineering quality gate against `main`;
- Ruff lint and formatter checks for changed scope;
- dead-code/import/config sweep and explicit review of remaining WK-related environment flags;
- full CWA test suite;
- full downstream gptty suite against the exact CWA source/candidate artifact;
- public capability/governance comparison against the current production backend and gptty command surface;
- minimal-shell JavaScript syntax and offline drift fixtures;
- native helper compile with `-Wall -Wextra -Werror`;
- wheel/sdist build + release gate + installed-wheel WK resource/helper smoke on macOS;
- built-artifact content audit confirming no experiment-only files or unintended resources are shipped;
- live text new-chat exact marker;
- live continuation exact marker/current-node advancement;
- HIGH/DEEP profile;
- no-stream;
- image plus representative general-file attachment;
- Stop;
- real tool-loop;
- explicit model and Temporary Chat behavior according to the chosen parity/fallback policy;
- four-process new-chat load;
- four-process simultaneous continuation load;
- mixed/goal-like multi-console load;
- no orphan `WKChatGPTAuthority`/WebKit processes after stress;
- no prompt/credential exposure in process command lines or residual named temp files;
- clean working tree after all acceptance runs, with no unexplained generated/temp artifacts.

Performance regression guardrails for the current machine/environment should be compared with, not blindly hard-coded to, the accepted evidence above. Investigate any large regression before promotion.

## Promotion sequence

1. **Security/IPC hardening** — WK-MAIN-001, 002.
2. **Architecture refactor** — WK-MAIN-003, 004, 005, 030, 031.
3. **Platform/CI hardening** — WK-MAIN-006, 007, 008, 009, 012, 034.
4. **Feature parity** — WK-MAIN-020 through 023 plus any newly discovered parity gaps.
5. **Observability/docs/dependency ownership** — WK-MAIN-010, 011, 032, 033, 035, 037.
6. **Final repository cleanup** — WK-MAIN-040 through 044. Remove experiment/legacy debris only from the pre-release branch; the frozen experiment branch remains intact.
7. **Default promotion + dirty-tree live rehearsal** — WK-MAIN-013 is already live-proven in the pre-release source; rerun the functionality/load matrix without publishing or changing the stable/global install.
8. **History/checkpoint cleanup** — WK-MAIN-036 reconstructs the intended logical commit boundaries once a safe commit/rebase primitive is available.
9. **Exact clean post-selector acceptance** — WK-MAIN-045 and 046 from the committed candidate, including fresh artifacts and installed-wheel smoke.
10. **Release sequencing — SUPERSEDED** — the staged CWA/gptty publication path was intentionally canceled when Chrome was retained as stable main and WK was retained as an alternative.

Steps 1-9 are complete as engineering/acceptance work. Step 10 is intentionally superseded. The plan therefore has no remaining open implementation, validation, promotion or release action.

## Progress log

Append concise entries here whenever a tracker state changes. Reference task IDs and commits where available.

### 2026-09-10 — Audit baseline

- Created this promotion tracker from the full pre-main audit.
- Current CWA branch: `experiment/wk-curl-max-optimization`; current gptty branch: `feature/wkwebview-authority-e2e`.
- At this audit baseline the implementation was still opt-in and the production/default backend had not yet changed. This historical state is superseded by the 2026-09-11 `WK-MAIN-013` source-selector promotion recorded below.
- Audit identified the P0 blockers above: IPC/argv privacy, provider/private-client refactor, legacy path isolation, macOS 12+ contract and CI, repository format/native warning gates, drift fixtures, honest canonical provenance/observability, and explicit default-promotion policy.
- Audit also identified P1 parity work for Temporary Chat, explicit model selection, general files and no-stream resilience.
- Previously completed continuation/image/Stop/tool-loop/profile/concurrency evidence is recorded above and in the optimization log.

### 2026-09-10 — Pre-release hardening branch created

- Froze `experiment/wk-curl-max-optimization` at `ddf371b` as the retained working/evidence checkpoint. Do not perform cleanup or history rewriting there.
- Created `prerelease/wkwebview-main-hardening` from that checkpoint for all productionization work.
- Added WK-MAIN-040 through WK-MAIN-046 as a mandatory final cleanup phase before acceptance testing: remove experiment-only code/files, sweep dead code/config, compare public capability parity, clean docs/history, inspect built artifacts, and prove clean-checkout reproducibility.
- Final acceptance is now explicitly ordered after cleanup so the exact code intended for `main`, rather than a richer experiment tree, must pass all functionality and load gates.

### 2026-09-10 — Private helper IPC hardening

- `WK-MAIN-001` is live-proven: turn payload now travels through a JSON stdin envelope instead of process argv. The live gptty/WK run completed exactly and the actual helper application process did not expose the marker/prompt in its command line.
- `WK-MAIN-002` is live-proven: resume credentials now return through an anonymous inherited pipe/FD. The legacy named `cwa-wk-resume-*` handoff and `CWA_WK_RESUME_*` environment transports were removed from the pre-release source; live verification created zero new named resume files.
- Canonical/observer conversation identifiers were also moved off argv onto the stdin envelope so compatibility fallback modes respect the same privacy boundary.
- Added a real subprocess IPC regression test in addition to provider mocks. Targeted WK suite is 26/26 green.
- `WK-MAIN-009` is done: the native helper compiles cleanly with `clang -Wall -Wextra -Werror`.
- Full CWA regression is 2144/2144 green using `uv run python -m pytest -q`; downstream gptty is 286/286 green against the exact pre-release source.
- Local `uv run pytest` resolved a stale console-script/import path from another checkout; module invocation (`uv run python -m pytest`) is the reproducible repo-root test command and successfully collects/runs the release tests from this workspace.

### 2026-09-10 — Helper runtime and lightweight transport refactor

- `WK-MAIN-003` moved to `IN PROGRESS`: `wkwebview_provider.py` has been reduced from more than 2.1k lines to roughly 1.4k by extracting native helper build/process/IPC lifecycle into `wkwebview_helper_runtime.py` and curl/WS/canonical/upload work into `wkwebview_lightweight_transport.py`.
- `WK-MAIN-004` is live-proven: the provider no longer reaches directly into `_build_headers`, `_capture_resume_token_diagnostics`, `_stream_handoff_via_ws_topic`, `_probe_celsius_ws_user` or `_upload_media_files`. `ChatGPTWebClient` exposes a small explicit WK transport boundary consumed through an internal protocol.
- `WK-MAIN-005` is done: the experimental shared-WK resume broker, daemon/socket runtime and `CWA_WK_SHARED_RESUME_BROKER` path were removed from the pre-release implementation. Direct WK resume remains only as the explicit direct-WK fallback.
- Added focused lightweight transport unit coverage for authenticated canonical reads, attachment upload and resume-topic streaming/finality. Refactored WK/transport targeted suite is 25/25 green; full CWA is 2143/2143 green and downstream gptty remains 286/286 green.
- Live regression on the refactored boundary passed a new-chat exact-marker retry, a two-turn continuation with exact markers on both turns, and an image upload where the model correctly identified the generated solid-red fixture. One initial new-chat attempt hit the previously observed intermittent `WKWEBVIEW_MINIMAL_SECURITY_RESUME_FENCE_MISSING`; the immediate retry succeeded, so resilience remains tracked under `WK-MAIN-023` rather than being hidden by the refactor.

### 2026-09-10 — Provider orchestration refactor completed

- `WK-MAIN-003` is done: `wkwebview_provider.py` is now 814 lines, down from more than 2.1k before hardening.
- Extracted canonical identity/finality state into `wkwebview_canonical.py`, native event-observer process lifecycle into `wkwebview_helper_runtime.py`, passive observer/Stop orchestration into `wkwebview_turn_observer.py`, and protected-write/phase-two turn orchestration into `wkwebview_turn_orchestrator.py`.
- Existing provider-facing contracts and test monkeypatch boundaries remain intact; the provider now coordinates the separated components instead of owning their process loops and phase sequencing inline.
- Refactor verification: WK/browser-native targeted regression 59/59 green, full CWA 2143/2143 green, Ruff clean for all refactored files, and downstream gptty 286/286 green against the exact pre-release CWA source.
- Repository engineering quality gate is still intentionally red under `WK-MAIN-008` because existing branch-delta files require Ruff formatting. This is tracked separately and was not hidden inside the orchestration refactor.

### 2026-09-10 — Minimal shell stages and fallback boundaries hardened

- `WK-MAIN-030` is done: the minimal security shell is split into named bootstrap, integrity runtime, session/catalog, model-selection, integrity-bundle, prepare and protected-write helpers. The entrypoint is orchestration-only and an offline structural regression prevents the fetch-heavy flow from being inlined again.
- `WK-MAIN-031` is done: lightweight canonical/auth/schema/source-contract errors fail closed, attachment fallback is limited to explicit recoverable transport cases, and phase-two passive-observer fallback no longer hides auth/contract/schema failures. Programming errors in provider status/write-commit and malformed observer payloads now propagate.
- The only remaining broad `except Exception` blocks in the WK Python production path isolate user/consumer callbacks; each is documented as observational best-effort so callback failures cannot corrupt transport finality.
- Verification after both changes: WK/lightweight/browser-native targeted regression 76/76 green, full CWA 2157/2157 green, downstream gptty 286/286 green against the exact pre-release CWA source, and a source-only live WK new-chat retry returned exactly `WK_MAIN031_BOUNDARY_RETRY_OK_20260910` with exit 0. The first live attempt timed out without a result and was not counted as a pass.

### 2026-09-10 — Lightweight dependency ownership moved to CWA

- `WK-MAIN-032` is done: `curl-cffi==0.16.3` and `websockets==16.1.1` are declared by CWA itself with Darwin-only markers. The experimental `wk-curl` extra was removed from production metadata.
- gptty no longer names or versions either transport library and depends only on `chatgpt-web-adapter>=0.3.0,<0.4.0`; a packaging regression in gptty prevents the dependency knowledge from drifting back downstream.
- Installed metadata from a local pre-release resolve shows the two transport dependencies under CWA and not gptty. Full CWA is 2158/2158 green; downstream gptty is 287/287 green against the exact pre-release source after re-resolution.
- A source-only live WK new-chat returned exactly `WK_MAIN032_OWNERSHIP_OK_20260910` with exit 0 after dependency ownership moved, proving the resolved environment still assembles and executes the WK path.

### 2026-09-10 — macOS contract, production docs and artifact gates completed

- `WK-MAIN-006` is done: helper construction now requires Darwin with an identifiable macOS version >=12; macOS 11 and unknown-version regressions fail closed before build, while the current macOS live path remains green.
- `WK-MAIN-033` is done: `docs/architecture.md` now explains the WK backend without depending on experiment history, including WebKit default-data-store session ownership, globally serialized short-lived Phase A, `RESUME_FENCE`, anonymous-FD resume handoff, curl/WS finality and bounded fail-closed fallback rules.
- `WK-MAIN-034` is done: the existing package JavaScript gate includes the minimal security shell; release artifacts must contain the WK Objective-C/plist/JS resources; installed-wheel smoke verifies those resources from `site-packages`.
- Artifact evidence: package JS syntax gate passed for 114 packaged JS files; a real 0.3.0 wheel and sdist passed `release_gate.py`; the exact wheel passed isolated Python 3.10 installed-wheel smoke. Targeted WK/release tests are 49/49 green, full CWA is 2163/2163 green and downstream gptty is 287/287 green against the exact pre-release CWA source.
- Live evidence after the macOS gate and package changes: source-only WK new-chat returned exactly `WK_MAIN034_PACKAGE_OK_20260910` with exit 0.

### 2026-09-10 — Canonical transport provenance and fallback observability live-proven

- `WK-MAIN-010` is live-proven: the canonical read-plane name is transport-neutral `WKWEBVIEW_CANONICAL_READ`; the stale context-HTTP name was removed. Actual canonical read transport is reported independently as `curl_cffi`, `wkwebview`, or `cache`, with a bounded fallback reason when curl falls back to WK.
- `WK-MAIN-011` is live-proven through the existing `BrowserOwnedWriteObservation`/`ProductExecutionProvenance.transport_metadata` path rather than a new WK-only API. Metadata records canonical read transport/fallback, Phase-A implementation/global-gate wait/elapsed time, and Phase-B implementation/fallback/elapsed time.
- Fallback reasons are normalized before entering provenance: stable WK error prefixes, request-stage names and HTTP status classes are allowed; arbitrary dependency exception text, prompts, cookies, authorization data and resume values are not.
- Live legacy-direct-WK evidence reported `phase_a_transport=wkwebview_full_page` and the direct-WK resume path; current provenance names that second leg `wkwebview_direct_resume`, distinguishing it from the primary lightweight curl/WebSocket path.
- Live lightweight new-chat evidence returned exactly `WK_MAIN011_LIGHTWEIGHT_OK_20260910` and reported `phase_a_transport=wkwebview_minimal_security_shell`, `phase_a_gate_wait_ms=0`, `phase_b_transport=curl_cffi_websocket`, with non-null phase timings and no fallback reason.
- Live lightweight continuation returned exactly `WK_MAIN010_CONT_OK_20260910`, preserved the same conversation identity, and reported `canonical_read_transport=curl_cffi` followed by the minimal-shell/curl-WebSocket path.

### 2026-09-10 — WK feature parity and no-stream resilience live-proven

- `WK-MAIN-020` is live-proven without Chrome compatibility: Temporary Chat uses minimal WK with browser-observed `history_and_training_disabled=true`, resumes through the lightweight WebSocket stream without durable canonical GET, keeps same-runtime continuation bound to process-local lifecycle identity plus latest assistant parent, and destroys that binding on explicit end. A two-turn live Temporary session returned exactly `WK_MAIN020_TEMP_FIRST_OK_20260910` and `WK_MAIN020_TEMP_CONT_OK_20260910` in the same ephemeral conversation, then ended to `NOT_ESTABLISHED`.
- `WK-MAIN-021` is live-proven natively: explicit model selection stays in the minimal shell, is checked against the live product catalog, and a live exact-model turn returned `WK_MAIN021_MODEL_PROVEN_OK_20260910` with `observed_model=gpt-5-6-thinking` on `wkwebview_minimal_security_shell -> curl_cffi_websocket`.
- `WK-MAIN-022` is live-proven natively: general files use ChatGPT file create/upload/finalize through `WKLightweightTransport`; a live `.txt` attachment marker was read correctly and returned `WK_MAIN022_FILE_OK_20260910` on the lightweight WK path. The temporary normal-turn Chrome compatibility router was removed because model/file/Temporary semantics are now self-contained.
- `WK-MAIN-023` is live-proven after identifying a concrete passive-SSE EOF race: the observer previously discarded a final unterminated SSE block and could therefore miss the browser-issued resume event after the protected write had already been accepted. The parser now flushes the decoder and residual block at EOF; it does not retry or replay the protected write. An offline Node regression executes the exact production `PassiveStreamObservationScript` with a final resume event lacking a trailing blank line.
- Live no-stream evidence after the fix: five consecutive new-chat runs returned exactly `WK_MAIN023_NOSTREAM_A_OK_20260910` through `WK_MAIN023_NOSTREAM_E_OK_20260910`, and a continuation returned exactly `WK_MAIN023_NOSTREAM_CONT_OK_20260910`; no run lost the resume fence. The gptty SDK adapter already strips the CLI `stream` display option before CWA, so `--no-stream` affects token rendering only and does not disable internal WK observation.
- Verification at this checkpoint: targeted WK/stream/release regression 68/68 green, full CWA 2178/2178 green, downstream gptty 287/287 green against the exact pre-release source, and native helper still compiles with `clang -Wall -Wextra -Werror`.

### 2026-09-10 — Quality and installed gptty gates completed

- `WK-MAIN-008`: engineering quality gate passes; targeted CWA 100/100, full CWA 2170/2170, downstream gptty 287/287.
- `WK-MAIN-035`: blocking `macos-wk-installed` gptty CI builds and installs exact gptty+CWA wheels, verifies dependency ownership with `pip check`, assembles WK from installed packages, and builds the packaged helper offline. Local reproduction returned `GPTTY_WK_INSTALLED_SMOKE_OK`; full gptty is 288/288.

### 2026-09-10 — Final policy preparation and clean-source rehearsal

- Explicit `wkwebview` is lightweight-by-default with no enable flags. `CWA_WK_FORCE_LEGACY=1` is the sole emergency full-page/direct-WK switch. Live evidence returned `WK_MAIN013_NOFLAG_OK_20260910` for the default WK topology and `WK_MAIN013_LEGACY_OK_20260910` for the forced-legacy topology; full CWA is 2175/2175 and downstream gptty is 288/288.
- The strengthened artifact gate has 22/22 release regressions and rejects experiment/cache/temp debris, local checkout paths and removed WK broker runtime in production package contents.
- A cache-free snapshot of the current candidate independently created its environment and passed 2175/2175 CWA tests, 114 packaged-JS syntax checks, strict native compile, fresh wheel+sdist build, release gate and installed-wheel helper build. Its 736-file source manifest remained unchanged outside allowed generated directories. This rehearses `WK-MAIN-045/046` but does not replace their required git-clean exact-checkout run.
- `WK-MAIN-014` records the release-deliverability requirement discovered during final review: public/datable `0.3.0` must not be reused for the WK promotion. Repository policy points to the next compatible `0.3.x` release, expected `0.3.1`; gptty's minimum CWA version must be raised only after that exact candidate is validated/released.
- At this 2026-09-10 checkpoint the intended sequence was to create a safe checkpoint before the selector change. The 2026-09-11 audit supersedes that assumption because the selector is already live in the dirty pre-release tree. The current required sequence is `WK-MAIN-036` safe history/checkpoint reconstruction -> exact clean post-selector `WK-MAIN-045/046` -> CWA 0.3.1 publish/verify under `WK-MAIN-014` -> gptty dependency-floor raise and `WK-MAIN-015`. Shell-git workarounds remain prohibited.

### 2026-09-10 — 0.3.1 / 0.1.2 release staging

- CWA is staged as 0.3.1 while public v0.3.0 remains unchanged. Real 0.3.1 wheel+sdist pass the strengthened candidate release gate, `twine check`, isolated installed-wheel smoke with `wk_helper_build=true`, full CWA 2175/2175 and the engineering quality gate. The strict v0.3.1 tag gate intentionally remains closed until a dated 0.3.1 changelog entry is finalized on the exact release commit.
- CWA's PyPI workflow now blocks upload on a tagged macOS WK job that checks the exact release tag, packaged JavaScript, strict native helper compile, targeted WK/release tests, artifacts, strict tag contract and installed-wheel smoke.
- gptty is staged as 0.1.2 while its current CWA floor stays at >=0.3.0 until CWA 0.3.1 is public. The 0.1.2 wheel+sdist pass `twine check`; an explicit CWA 0.3.1 + gptty 0.1.2 isolated install passes `pip check` and `GPTTY_WK_INSTALLED_SMOKE_OK`; full gptty is 289/289.
- The downstream negative release proof is explicit: installing only the staged gptty 0.1.2 wheel currently resolves public CWA 0.3.0, and the WK smoke fails on the missing CWA-owned Darwin transport dependency. gptty's publish workflow therefore requires both a dated 0.1.2 release entry and the exact dependency floor `chatgpt-web-adapter>=0.3.1,<0.4.0` before its macOS installed-WK gate can authorize PyPI publishing.
- Current promotion ordering is now acyclic: `013 selector live in prerelease source -> 036 safe history/checkpoint -> exact clean post-selector 045/046 -> 014 CWA 0.3.1 release -> gptty floor bump -> 015 gptty 0.1.2 release`.

### 2026-09-11 — Current-candidate hardening and full rehearsal

- A real mixed-load failure exposed a second `RESUME_FENCE_MISSING` class distinct from the earlier SSE EOF parser bug: ChatGPT had accepted the protected new-chat write, but no resume fence arrived and the helper held the global Phase-A gate until timeout. The exact stalled marker was later found in canonical history, proving that replaying the write would have risked a duplicate.
- `WK-MAIN-016` closes that state without replay: the minimal shell exposes its unique client user-message id, accepted/no-fence Phase A exits after a bounded grace period, releases the heavy-submit gate, and read-only `curl_cffi` catalog/canonical recovery requires the exact message id plus prompt before accepting canonical finality. Recovery provenance is distinct as `canonical_message_id_recovery`; focused WK/lightweight/stream regression is 61/61.
- Fresh four-process gates on the current path are green: new-chat 4/4 exact at ~353.7 MB peak, continuation 4/4 exact at ~406.7 MB with every pre-read on `curl_cffi`, and mixed two-new/two-continuation 4/4 exact at ~424.9 MB. All retained `max_phaseA=1`, had no lightweight fallback, and left no persistent WK helper.
- Current live gates after the helper/recovery changes are green: `WK_FINAL_DEEP_OK_20260911`, `WK_FINAL_NOSTREAM_OK_20260911`, `WK_FINAL_TEMP_OK_20260911`, exact-model `WK_FINAL_MODEL_SINGLE_OK_20260911` with `observed_model=gpt-5-6-thinking`, image `WK_FINAL_IMAGE_SINGLE_OK_20260911`, and general-file `WK_FINAL_FILE_SINGLE_OK_20260911`.
- Stop was rechecked only after real assistant text began. The provider returned `stopped=true` with stream-status proof, the send returned `finish_reason=stopped`, the forbidden tail marker was absent, and subsequent canonical readback showed `finish_details={type: interrupted, reason: client_stopped}`.
- Exact-current regression/package rehearsal is green: full CWA 2188/2188, downstream gptty 289/289, engineering quality gate PASS, 114 packaged-JS files PASS, strict native `-Wall -Wextra -Werror` PASS, and doc/release regression 37/37 PASS. After the final selector-documentation/changelog consistency edits, 0.3.1 wheel+sdist were rebuilt again and passed `twine check`, the strengthened release/content gate, and installed-wheel smoke from `site-packages` with `wk_helper_build=true`.
- Final security review confirms the accepted-write recovery preserves the no-replay boundary: Phase A releases the global submit gate before recovery; recovery uses only read-only `curl_cffi` catalog/canonical requests and fails closed instead of invoking generic/WK canonical fallback; the client message id is the exact id used by prepare/protected write. Resume/conduit/turn-trace values cross native/Python only through the anonymous inherited FD, use opaque private keys, are removed from the turn payload before result/provenance assembly, live only in the in-memory Stop context, and are cleared on failure/completion/Stop.
- The source selector is now live: identifiable macOS >=12 resolves implicit browser-owned runtime assembly to `wkwebview`, while older/unknown macOS and non-Darwin hosts retain `chrome-native`; explicit backend selection still overrides this. Live no-argument proof returned `WK_FINAL_IMPLICIT_DEFAULT_OK_20260911` with `default_backend=wkwebview` and `WKWebViewTurnProvider`. This has not been published or installed into the user's stable/global CLI.
- Terminal Git was explicitly authorized later on 2026-09-11. The candidate was checkpointed at `d05d521`, then lock-pinned Ruff 0.16.6 exposed and normalized 12 formatting-only files in `d274c25`. A detached clean checkout of `d274c25` then passed `WK-MAIN-045/046` in full, including CWA 2188/2188, gptty 289/289 against that exact source, quality/native/JS/docs gates, clean tracked tree, fresh artifacts, release gate and installed-wheel smoke.

### 2026-09-11 — Final upstream-integration verification and plan closure

- The final WK alternative was integrated with the newer upstream architecture on `integration/upstream-wkwebview-0.3.1` and fixed at `1e4e972fa2f219e2363456e78bebc7f3ca00b7b5`. Integration defects found during live work were fixed, including the lost WK lightweight source-client contract and the early-Stop/canonical-poll race.
- Final exact-source verification is green after the catalog/read-surface repairs: CWA 2256/2256, downstream gptty 289/289, engineering quality gate PASS, packaged JavaScript PASS, strict native compile PASS, fresh wheel/sdist + `twine check` + release gate + installed-wheel smoke PASS. Exporter compatibility was then exercised through the integration worktree: 36 conversations discovered, a full canonical payload with 1426 mapping nodes fetched, and one real temporary-directory export completed with 44 messages / 61,458 bytes. The exporter fix is finalized on `betabit-playground` as `0e2ee9c`; the installed CLI also resolves the integration worktree from package `direct_url.json` when launched outside the checkout, and an installed-command dry-run from `/tmp` passed.
- Final live WK evidence is green for Stop, Temporary Chat, exact `gpt-5-6-thinking` selection, representative text-file upload/readback and image understanding. Four-process new, continuation and mixed gates were 4/4 exact with `max_phaseA=1`, lightweight curl/WebSocket transport and no fallback in the final runs. A final post-closure manual turn also returned exact `WK_ALTERNATIVE_FINAL_CONFIRM_OK_20260911` with HTTP 200, `phase_a_transport=wkwebview_minimal_security_shell`, `phase_b_transport=curl_cffi_websocket` and no Phase-B fallback.
- Stable Chrome `main` was restored to `524f400c74da6fdf8ff3aa2ab22cc497b8b69168` locally and on `origin/main`, then independently rechecked: CWA 2115/2115, downstream gptty 289/289, and an authenticated live browser-native turn returned exact `CWA_CHROME_MAIN_LIVE_AUTH_OK_20260911` with HTTP 200 and canonical completion.
- Final product decision: keep Chrome as stable/default `main`; keep the optimized WK solution as a separate tested alternative. No historical experiment/prerelease branches are deleted or rewritten. `WK-MAIN-013/014/015` are therefore `SUPERSEDED`, and this tracker is closed with no remaining action item.

### 2026-09-12 — gptty resume/read-surface compatibility repair

- A real first-use `/resume` on conversation `6aa33498-f770-83ed-a42b-4fe22f438807` exposed a missing `ChatGPTProductRuntime.conversation_snapshot` proxy after the upstream integration. Audit of the full gptty compatibility surface also found `list_models` missing, so both were restored together rather than fixing `/resume` alone.
- `8fd303b` restores `conversation_snapshot` and `list_models` delegation and adds a contract test covering the complete gptty canonical read surface (`attach_conversation`, `get_messages`, `get_status`, `list_conversations`, `list_models`, `conversation_snapshot`, `get_conversation_payload`).
- Exact live verification used the installed gptty environment and the same conversation: snapshot returned 2047 messages with status `tool_calling`, model catalog returned 17 models, and the real enhanced `gptty chat` path `/resume 6aa33498-f770-83ed-a42b-4fe22f438807` reached `Resumed:` and rendered the conversation. A wider read-only check also passed catalog, status, messages and attach against the same conversation.
- Full post-fix suites: CWA 2256/2256 and gptty 289/289. The fix was pushed to both the public fork integration branch and private backup before this evidence entry was recorded.

### 2026-09-12 — resumed-chat live-follow regression repair

- A second real first-use check exposed a separate regression: `/resume` loaded the canonical snapshot, but an already-running browser turn stopped updating in gptty after the first visible reasoning entries while ChatGPT Web continued adding reasoning/tool activity. The regression traced to gptty commit `fcd409b` (`Make resumed chats non-blocking`), which correctly removed the old blocking follow loop but also removed live follow entirely.
- CWA commit `d61b039` adds `conversation_follow_snapshot()`: one canonical payload read produces status, bounded visible messages and revision-safe canonical intermediate events using the same classifier as normal browser-owned sends. The first snapshot seeds already-seen event IDs; later polls therefore emit only new reasoning/tool/activity nodes.
- gptty commit `0218ef6` restores follow as a non-blocking enhanced-loop state machine rather than reverting the old blocker. The prompt remains usable while an unfinished resumed turn is followed; user text queues until that turn completes, commands run between canonical reads, Ctrl-C maps to `/stop`, and polling stops on completion/context change/approval/timeout. Older CWA clients fall back to the ordinary snapshot path.
- Exact live verification used active conversation `6aa4861b-475c-83eb-b7da-4406ce8c8b3b`. A canonical seed recorded 601 historical intermediate nodes and the next poll returned exactly one new tool call without replaying the old events. Source enhanced `gptty chat` then reached `Resumed:` and rendered a new post-resume tool block. Finally, the installed `/Users/stas/.local/bin/gptty` was replaced with a clean wheel built from exact commit `0218ef6` and repeated the same check successfully (`GPTTY_INSTALLED_LIVE_FOLLOW_OK tool`).
- Full post-fix suites: CWA 2257/2257 and gptty 291/291; targeted Ruff checks and `git diff --check` passed. CWA `d61b039` is pushed to the public integration branch and private backup; gptty `0218ef6` is pushed to the user's `origin/feature/wkwebview-authority-e2e`.

### 2026-09-12 — push-based resumed-turn follow for the WK alternative

- The 15/30/60 second canonical polling repair above was correct as a low-risk first fix, but it still spent one full conversation GET per follow interval and therefore could not match ChatGPT Web's immediate resumed-turn updates. A follow-up experiment started from CWA `82a4c67` on `experiment/wk-passive-original-stream` and kept Chrome completely out of the alternative design.
- The investigation first considered keeping a WK page alive and passively cloning the browser-owned fetch/WebSocket stream. The smaller solution proved better: the canonical active-turn messages already carry `turn_exchange_id` / `working_turn_id`; on the real account, reconstructing `conversation-turn-<turn_exchange_id>` and subscribing through the existing Celsius user WebSocket returned the complete turn catchup. A read-only proof on conversation `6aa4861b-475c-83eb-b7da-4406ce8c8b3b` recovered 268 stream tokens / 16,382 characters from a completed turn, and raw catchup inspection showed reasoning, tool calls/results, assistant text and patch updates.
- CWA now exposes the stream identity in the initial `conversation_follow_snapshot()`, reuses the existing `curl_cffi` Safari-impersonated Celsius bootstrap, and adds `conversation_follow_stream()`. A stateful `CanonicalTopicStreamNormalizer` converts Celsius frames into the same revision-safe `assistant_text_*` and `canonical_intermediate_message` events already consumed by gptty. Initial canonical event IDs seed the normalizer so catchup does not replay old reasoning/tool blocks; seeded answer text also prevents an older catchup prefix from visually rewinding an already-rendered partial answer. Exactly one final canonical reconciliation is performed when the WS reports terminal completion.
- gptty now prefers this push stream whenever the initial resume snapshot carries a topic. The enhanced prompt remains non-blocking, text still queues until the resumed turn finishes, and command/stop handling first asks the subscriber to exit so CWA calls do not overlap. If topic metadata is absent, the WS bootstrap fails, or a stream ends before terminal completion, gptty disables the stream attempt for that follow session and falls back to the proven adaptive canonical polling path.
- Exact live source verification used the same previously failing active conversation `6aa4861b-475c-83eb-b7da-4406ce8c8b3b`, still in `tool_calling`. With a production-like installed dependency environment and current source paths, enhanced `/resume <id>` reached `Following active response via live stream…` after 7.21 seconds including rendering 1,246 historical messages, then rendered a new live `Thinking` block at 8.29 seconds — about 1.08 seconds after stream follow began. The source-only gptty virtualenv intentionally lacked `curl_cffi`; reproducing with the installed tool interpreter (`curl_cffi 0.16.3`) confirmed this was a test-environment dependency mismatch, not a product-path failure.
- A 35-second before/after measurement on the same real active conversation compared the previous 15-second polling algorithm with the new push algorithm after the identical initial canonical snapshot. Polling performed 3 canonical snapshots, peaked at 132.22 MiB after attach, and consumed 0.441 CPU-seconds (1.23% average). WS follow performed 1 initial snapshot and then delivered 8 live events without polling, peaked at 122.16 MiB, and consumed 0.219 CPU-seconds (0.59% average). This is a measured delta of **-10.06 MiB peak RSS** and approximately **-50% CPU time** over the follow window. The measured process tree contained only `python3.10`; no Chrome, WK helper or WebContent child process was created for the follow phase.
- This optimization does not reopen the stable-backend decision. Chrome remains the unchanged stable-main implementation; the optimized alternative remains explicitly WK-based for browser authority/writes, while resumed-turn observation after the canonical attach uses the lightweight Celsius WebSocket and requires no browser process at all.

## Rules for maintaining this tracker

- Never mark an item `DONE` from code inspection alone when its acceptance criterion requires a live or installed-artifact test.
- This tracker is closed. Reopen it only if the product decision changes and WK promotion/public release is intentionally resumed.
- Keep experiment measurements in the optimization log; keep the final dual-backend decision and any future reversal here.
- Treat `experiment/wk-curl-max-optimization` and the other historical experiment branches as retained evidence; do not delete, clean or rewrite them as part of normal maintenance.
- Stable `main` remains the Chrome browser-native line unless a new explicit promotion decision is recorded first.
- The final WK alternative remains on `integration/upstream-wkwebview-0.3.1`; future WK fixes should start from that tested line or a new descendant branch, not by rewriting historical checkpoints.
- Do not create issues, pull requests, comments, reviews, pushes or other write interactions against the foreign `kymuco/chatgpt-web-adapter` repository without explicit user approval.
