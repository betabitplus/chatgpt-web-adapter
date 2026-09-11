# WKWebView Lightweight Authority — Main Promotion Plan

_Status: ACTIVE · macOS 12+ source default promoted in pre-release, not yet released_
_Last audit: 2026-09-11_
_Canonical tracker for promotion work: this file_

This document is the single source of truth for turning the current lightweight WKWebView authority experiment into the primary macOS browser-authority implementation. Do not reconstruct promotion status from chat history or the optimization experiment log; update this file whenever a promotion item is completed, changed, rejected, or newly discovered.

Related evidence:

- Performance/live experiment evidence is retained on frozen branch `experiment/wk-curl-max-optimization` at `experiments/wkwebview_authority/max_optimization_log.md`; the pre-release branch intentionally does not retain the experiment harness/files.
- Frozen CWA experiment/evidence branch: `experiment/wk-curl-max-optimization` at promotion fork point `ddf371b`. Do not rewrite or clean this branch; it is the reproducible checkpoint for the working alternative implementation and experiment history.
- CWA pre-release hardening branch: `prerelease/wkwebview-main-hardening`. All cleanup, refactoring, parity, CI and promotion work happens here.
- gptty integration branch: `feature/wkwebview-authority-e2e`.

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
- the primary macOS path must be installable from the built wheel, not only runnable from a source checkout.

## Status legend

- `OPEN` — not started or not proven.
- `IN PROGRESS` — implementation/research active.
- `DONE` — implemented and locally/regression tested.
- `LIVE PROVEN` — exercised successfully against the real product path.
- `DEFERRED` — intentionally postponed with a documented reason.
- `REJECTED` — investigated and intentionally not adopted.

## Promotion blockers

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
| WK-MAIN-013 | P0 | LIVE PROVEN | Promote the primary macOS browser-authority backend | The pre-release source now resolves the implicit browser-owned backend to `wkwebview` on identifiable macOS >=12 and keeps `chrome-native` on older/unknown macOS and non-Darwin hosts; an explicit backend selection still overrides the default. The WK backend itself remains lightweight-by-default, with `CWA_WK_FORCE_LEGACY=1` only changing its internal topology to full-page/direct-WK. Live source-only evidence on 2026-09-11, with no explicit backend argument, returned exact `WK_FINAL_IMPLICIT_DEFAULT_OK_20260911` with `default_backend=wkwebview` and provider `WKWebViewTurnProvider`. This is a pre-release source promotion only: stable/global installed tooling and public packages remain unchanged until `WK-MAIN-036`, `045/046`, and `014` complete. |
| WK-MAIN-014 | P0 | STAGED | Make the promoted runtime distributable as a new package version | CWA package metadata is staged at `0.3.1` while `v0.3.0` remains the latest public release. Real 0.3.1 wheel+sdist pass the strengthened candidate release gate, installed-wheel smoke (`wk_helper_build=true`) and `twine check`; strict `--tag v0.3.1` intentionally fails while the changelog remains `Unreleased`. Tagged PyPI publishing is blocked on macOS WK verification. Final `DONE` comes only after `WK-MAIN-036` establishes a safe exact post-selector candidate commit, `WK-MAIN-045/046` pass from that clean checkout, the dated 0.3.1 changelog/tag is finalized, and public PyPI/post-publish verification succeeds. |
| WK-MAIN-015 | P0 | STAGED | Make the downstream gptty rollout distributable without accepting old CWA | gptty is staged at `0.1.2` while dated `0.1.1` remains historical. Its wheel+sdist pass `twine check`; an isolated explicit install with CWA 0.3.1 passes `pip check` and `GPTTY_WK_INSTALLED_SMOKE_OK`. Installing the same gptty 0.1.2 wheel from current `>=0.3.0,<0.4.0` metadata resolves public CWA 0.3.0 and the WK smoke correctly fails because that release lacks the CWA-owned Darwin transport dependencies. gptty publishing is release-tag-only, requires a dated 0.1.2 changelog, blocks on macOS installed-WK smoke, and explicitly requires `chatgpt-web-adapter>=0.3.1,<0.4.0`. Final `DONE` is sequenced after CWA 0.3.1 is public: raise the floor, replace the temporary pre-release-branch CI checkout with public CWA 0.3.1 coverage, rerun downstream clean/package gates, publish 0.1.2, then verify the public install. |
| WK-MAIN-016 | P0 | DONE | Recover an HTTP-accepted protected write that misses the resume fence without replaying it | The minimal shell emits its unique client user-message id before submit. After a successful product response, missing resume/terminal evidence is given only a bounded grace period; the WK helper then releases the heavy-submit gate with an explicit recovery state instead of waiting for the whole turn timeout or retrying the write. CWA performs read-only `curl_cffi` recent-catalog/canonical lookup, requires the exact client message id plus prompt, waits for canonical finality and caches that payload. Unit coverage proves no second write/WS resume occurs. The real pre-fix stalled marker was independently found in canonical history, confirming the accepted-write failure mode; post-fix four-process mixed load is 4/4 exact with `max_phaseA=1` and no fallback. |

## Feature-parity work before default

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
| WK-MAIN-036 | P2 | READY | Prepare clean merge history | The current prerelease tree already contains the selector promotion, so the safe-history task is to split/reconstruct the intended logical boundaries once an approved commit/rebase primitive exists: CWA hardening/cleanup, CWA 0.3.1 staging, isolated macOS default-selector promotion, then dated release finalization; gptty integration/CI, 0.1.2 staging, then post-CWA-0.3.1 dependency-floor/public-CWA transition and release finalization. Incidental formatter/fixups are squashed into their owning boundary. Execution remains blocked only by the absence of a safe commit/rebase primitive; shell-git workarounds are not used. |
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
| WK-MAIN-045 | P1 | OPEN | Clean installed-artifact audit | The release gate rejects experiment/cache/temp debris, local checkout paths and removed WK broker runtime in packaged production content; negative regressions remain green. On 2026-09-11 the current dirty candidate rebuilt fresh `0.3.1` wheel+sdist after the final recovery-observability change, passed `twine check`, the strengthened release/content gate, and installed-wheel smoke from `site-packages` with `wk_helper_build=true`. Formal `DONE` still requires repeating these exact gates from a git-clean candidate checkout after checkpoint/commit. |
| WK-MAIN-046 | P1 | OPEN | Clean-tree reproducibility | The earlier cache-free source snapshot passed its independent environment, full CWA 2175/2175, 114 packaged-JS checks, strict native compile, fresh artifacts and unchanged source manifest. The current 2026-09-11 dirty candidate additionally passes full CWA 2188/2188, downstream gptty 289/289, 114 packaged-JS checks, strict native compile, quality gate, fresh 0.3.1 artifact build/release gate and installed-wheel helper smoke. Formal `DONE` still requires the same run from a git-clean exact candidate checkout after checkpoint/commit. |

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

## Required acceptance gates before switching the default

All P0 items, applicable P1 parity items, and **all WK-MAIN-040 through WK-MAIN-046 cleanup items** must be `DONE` or `LIVE PROVEN` before this section starts. Final acceptance is intentionally run *after* repository cleanup so deleted legacy/experiment code cannot mask a regression.

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
10. **Release sequencing** — WK-MAIN-014 publishes/verifies CWA 0.3.1, then the gptty floor/public-CWA transition completes WK-MAIN-015.

The source selector is already promoted; do not treat dirty-tree success as permission to publish before the clean committed-candidate gates are satisfied.

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
- The available CodexPro tool inventory was re-audited on 2026-09-11 and exposes status/diff/review/snapshot primitives but **no commit, checkpoint or rebase action**. Therefore `WK-MAIN-036` and the formal clean-commit forms of `WK-MAIN-045/046` remain blocked; shell-git workarounds are still prohibited.

## Rules for maintaining this tracker

- Never mark an item `DONE` from code inspection alone when its acceptance criterion requires a live or installed-artifact test.
- Add newly discovered promotion defects here before fixing them so they cannot disappear into chat history.
- If an item is intentionally not fixed, mark it `DEFERRED` or `REJECTED` and record the reason in the progress log.
- Keep experiment measurements in the optimization log; keep promotion decision state here.
- Treat `experiment/wk-curl-max-optimization` as immutable evidence unless a new experiment is intentionally started on a separate experiment branch.
- Do not mark final acceptance complete until `WK-MAIN-013` through `WK-MAIN-015` and `WK-MAIN-040` through `WK-MAIN-046` have reached their final states in the required sequence, with the post-switch acceptance matrix rerun from the exact clean candidate afterward.
- When `WK-MAIN-036` reconstructs the dirty pre-release history, keep the already-applied macOS default-selector promotion in its own logical commit so rollback remains straightforward.
- Target CWA history when a safe commit/rebase primitive is available: (1) production WK refactor/hardening + experiment cleanup, (2) 0.3.1 release staging/gates, (3) isolated macOS default-selector promotion, (4) dated 0.3.1 release finalization only after post-switch clean acceptance.
- Target gptty history: (1) CWA-owned dependency/integration CI, (2) 0.1.2 release staging/publish gates, then after public CWA 0.3.1 (3) dependency-floor/public-CWA CI transition and release finalization. Squash incidental formatter/fixup commits into their owning logical boundary rather than preserving experiment chronology.
