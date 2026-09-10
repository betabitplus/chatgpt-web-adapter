# WKWebView Lightweight Authority — Main Promotion Plan

_Status: ACTIVE · alternative implementation remains opt-in_  
_Last audit: 2026-09-10_  
_Canonical tracker for promotion work: this file_

This document is the single source of truth for turning the current lightweight WKWebView authority experiment into the primary macOS browser-authority implementation. Do not reconstruct promotion status from chat history or the optimization experiment log; update this file whenever a promotion item is completed, changed, rejected, or newly discovered.

Related evidence:

- [`../../experiments/wkwebview_authority/max_optimization_log.md`](../../experiments/wkwebview_authority/max_optimization_log.md) — performance and live experiment evidence.
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
             RESUME_FENCE
                  |
                  v
          curl_cffi + WebSocket stream
                  |
                  v
             canonical finality
```

Required invariants:

- browser-protected mutation remains owned by real WebKit and current product resources;
- no challenge solving, token fabrication/replay, or reconstructed anti-abuse machinery;
- exactly one protected write attempt per logical turn unless the caller explicitly starts another turn;
- browser authority is released after a proven `RESUME_FENCE` and is not retained through generation/finality;
- expensive browser Phase A is globally serialized; lightweight reads/streams remain parallel;
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
| WK-MAIN-003 | P0 | IN PROGRESS | Refactor `wkwebview_provider.py` orchestration | Provider reduced from >2.1k lines to ~1.4k by extracting native helper lifecycle and lightweight transport. Continue with observer/stop process plumbing and turn orchestration until the provider is primarily coordination. |
| WK-MAIN-004 | P0 | LIVE PROVEN | Replace private `ChatGPTWebClient` coupling | Provider now consumes an explicit `WKLightweightTransport` / source-client contract and no longer reaches into the listed private methods. New-chat, continuation and image live gates passed on the refactored boundary. |
| WK-MAIN-005 | P0 | DONE | Remove/isolate superseded resume implementations | Removed the superseded shared-WK broker/daemon/socket/env path. The remaining direct-WK resume is isolated in an explicit compatibility fallback method; curl/WS remains the primary second leg. |
| WK-MAIN-006 | P0 | OPEN | Add macOS availability contract | Explicitly require macOS 12+ for the minimal WK path because `loadSimulatedRequest:responseHTMLString:` is macOS 12+. Older macOS/non-Darwin must use the existing supported backend. |
| WK-MAIN-007 | P0 | OPEN | Add macOS CI/release coverage | Add `macos-latest` coverage for helper build, strict native warnings, minimal-shell JS syntax, targeted WK tests and installed-wheel helper/resource smoke. |
| WK-MAIN-008 | P0 | OPEN | Make branch pass repository quality gate | `uv run python tools/engineering_quality_gate.py --base-ref main` must pass. Apply/accept Ruff formatting policy for the branch delta. |
| WK-MAIN-009 | P0 | DONE | Native helper warning-clean | `clang -Wall -Wextra -Werror` passes after cleaning the `main` parameter and attachment-count warning paths. |
| WK-MAIN-010 | P0 | OPEN | Correct canonical transport provenance | Hot-path canonical reads are now curl, not WK. Rename stale `WKWEBVIEW_CONTEXT_CANONICAL_HTTP` semantics/docs and expose the actual transport/fallback used. |
| WK-MAIN-011 | P0 | OPEN | Add lightweight transport/fallback observability | Record transport selection, fallback reason, Phase-A gate wait and relevant timing without secrets. Do not silently turn important curl/auth failures into an unexplained heavy WK fallback. |
| WK-MAIN-012 | P0 | OPEN | Stabilize minimal-shell upstream-drift tests | Add sanitized offline fixtures for at least two known `conversation-small-*` layouts/deploys and test integrity helper/initializer discovery without live product access. |
| WK-MAIN-013 | P0 | OPEN | Define primary-backend promotion policy | macOS 12+ should select lightweight WK as primary only after all P0/P1 gates pass. Keep one explicit emergency/legacy kill-switch; remove the two experimental env flags from normal user configuration. |

## Feature-parity work before default

| ID | Priority | Status | Item | Required outcome |
|---|---|---|---|---|
| WK-MAIN-020 | P1 | OPEN | Temporary Chat parity/routing | `WKWebViewTurnProvider.temporary_chat_supported` is currently false. Either implement/live-prove temporary lifecycle on the new path or explicitly route `/temporary` to the existing supported backend without hidden semantic loss. |
| WK-MAIN-021 | P1 | OPEN | Exact model selection parity/routing | `supports_model_slug` is currently false and explicit `model_slug` falls back. Either support/live-prove exact model selection in the lightweight path or make the compatibility routing explicit and tested. |
| WK-MAIN-022 | P1 | OPEN | General attachment parity | Image attachment is live-proven. Prove at least representative PDF/general-file input, or restrict lightweight attachment eligibility to proven MIME/classes and fallback for the rest. |
| WK-MAIN-023 | P1 | OPEN | No-stream resilience | Keep `--no-stream` as a mandatory live gate. One audit run produced `WKWEBVIEW_MINIMAL_SECURITY_RESUME_FENCE_MISSING`, an immediate retry succeeded. Add regression/resilience coverage so internal observation remains correct when user token streaming is disabled. |
| WK-MAIN-024 | P1 | DONE | Continuation canonical pre-read uses lightweight transport | Normal continuation pre-read now prefers authenticated Safari-impersonated curl and retains WK only as fail-safe fallback. Four simultaneous continuation test reduced peak from ~705 MB to ~403 MB with `max_canonical=0`. |
| WK-MAIN-025 | P1 | LIVE PROVEN | Continuation identity/current-node safety | Existing-conversation minimal turns preserve conversation identity, advance current node and keep exact canonical matching. |
| WK-MAIN-026 | P1 | LIVE PROVEN | Image attachment path | Real image upload + minimal protected write + curl/WS + canonical finality passed; model observed the supplied image. |
| WK-MAIN-027 | P1 | LIVE PROVEN | Stop semantics | Progressive stream + user stop reached canonical interrupted/client-stopped state and did not emit the forbidden final marker. |
| WK-MAIN-028 | P1 | LIVE PROVEN | Tool-loop semantics | Minimal Phase A followed by real tool calls and final canonical response passed with stream == canonical. |
| WK-MAIN-029 | P1 | LIVE PROVEN | HIGH/DEEP profile semantics | Current wire contract verified as thinking model + extended effort; full continuation preserves selection. |

## Maintainability and cleanup

| ID | Priority | Status | Item | Required outcome |
|---|---|---|---|---|
| WK-MAIN-030 | P1 | OPEN | Split minimal shell into named stages | Refactor the ~570-line single async flow into testable helpers: bootstrap, integrity discovery, session/catalog, model resolution, integrity acquisition, prepare and protected write. |
| WK-MAIN-031 | P1 | OPEN | Tighten exception/fallback boundaries | Replace broad `except Exception` where it can hide actionable transport/auth/schema defects. Keep best-effort cleanup exceptions where appropriate and document them. |
| WK-MAIN-032 | P1 | OPEN | CWA owns lightweight transport dependencies | Final dependency ownership should live in CWA. gptty currently carries `curl-cffi`/`websockets` to make the branch integration work; remove that duplication when CWA packaging exposes the production path normally. |
| WK-MAIN-033 | P1 | OPEN | Update architecture documentation | Add the actual lightweight lifecycle, browser/session ownership, global gate, security boundary, macOS minimum and fallback policy to normal architecture docs. Experiment log must remain evidence, not the only explanation of production design. |
| WK-MAIN-034 | P1 | OPEN | Extend JS/package release gates | Existing JS syntax tooling only scans `browser_native_extension/*.js`; release gate focuses on extension package data. Include `wkwebview_helper/minimal_security_shell.js`, `.m`, `.plist` and installed-wheel resource checks. |
| WK-MAIN-035 | P1 | OPEN | Add gptty macOS integration CI | When WK becomes the macOS default, add an integration/package smoke that proves gptty resolves and can assemble the CWA WK runtime from installed artifacts. |
| WK-MAIN-036 | P2 | OPEN | Prepare clean merge history | Before final merge, squash/rebase experimental CWA steps into a small reviewable series: implementation/refactor, tests/CI/docs, default promotion. Do the same only as needed for the small gptty branch. |
| WK-MAIN-037 | P2 | OPEN | Remove obsolete comments/names after refactor | Delete terminology that implies retained tabs/WK canonical hot path where the new implementation no longer behaves that way. |

## Final repository cleanup before acceptance

This is a deliberate end-of-hardening phase, not opportunistic cleanup while implementation is still moving. The frozen experiment branch keeps the research history; the pre-release branch should contain only what belongs in the eventual `main` implementation.

| ID | Priority | Status | Item | Required outcome |
|---|---|---|---|---|
| WK-MAIN-040 | P1 | OPEN | Remove experiment-only runtime code | Delete superseded probes, experiment-only switches, unused compatibility implementations and code paths that are no longer part of the chosen production/fallback architecture. Preserve their history on `experiment/wk-curl-max-optimization`, not in future `main`. |
| WK-MAIN-041 | P1 | OPEN | Remove experiment-only files/assets | Audit `experiments/`, temporary harnesses, generated helpers/assets, obsolete docs and package-data entries. Future `main` must not ship research-only artifacts merely because they existed on the experiment branch. Keep only evidence/docs that are intentionally part of the repository. |
| WK-MAIN-042 | P1 | OPEN | Dead-code/import/config sweep | Run static/search review after refactoring and delete unreachable methods, unused imports, stale environment variables, obsolete constants, stale package extras and unreferenced helper assets. No compatibility flag may remain without a documented consumer and test. |
| WK-MAIN-043 | P1 | OPEN | Public capability parity diff | Compare runtime capabilities/governance and gptty user-visible commands/options against the current production backend. Every lost capability must be implemented, explicitly routed to the supported fallback, or consciously documented as unsupported before promotion. |
| WK-MAIN-044 | P1 | OPEN | Documentation/history cleanup | Normal architecture/release docs must describe the final design. Experiment narrative must not be required to understand production code. Remove stale claims/names and retain links to the frozen experiment branch/log only where useful as evidence. |
| WK-MAIN-045 | P1 | OPEN | Clean installed-artifact audit | Build wheel/sdist from a clean checkout of the pre-release branch and inspect their contents/dependencies. No experiment-only file, local path, secret, temp artifact or unintended helper resource may ship. |
| WK-MAIN-046 | P1 | OPEN | Clean-tree reproducibility | From a clean checkout with no prior helper build/cache assumptions, run the non-live acceptance suite and verify no untracked build/temp files are left behind except explicitly ignored tool output. |

## Already accepted architecture/performance evidence

These items are not reasons to redesign the solution unless later evidence invalidates them.

| ID | Status | Evidence |
|---|---|---|
| WK-EVID-001 | LIVE PROVEN | Minimal real protected write reaches HTTP 200 and `RESUME_FENCE` without loading the full ChatGPT SPA. |
| WK-EVID-002 | LIVE PROVEN | Full path `minimal WK → protected write → RESUME_FENCE → curl/WS → canonical` returns exact stream/canonical response. |
| WK-EVID-003 | LIVE PROVEN | Single-provider measured peak ~235 MB versus roughly ~598 MB for full-SPA Phase A. |
| WK-EVID-004 | LIVE PROVEN | Four simultaneous new/continuation/mixed gptty workloads retain `max_phaseA=1`; lightweight streams and canonical reads overlap in parallel. |
| WK-EVID-005 | LIVE PROVEN | After curl canonical pre-read fix, four simultaneous continuations peak at ~403 MB and mixed four-console workload at ~423 MB with no WK canonical/observer helpers. |
| WK-EVID-006 | LIVE PROVEN | Dynamic integrity-export discovery survived a real upstream `conversation-small-*` deployment that renamed minified exports. |
| WK-EVID-007 | DONE | Wheel packaging includes `minimal_security_shell.js`; source-tree-only package-data omission was fixed. |
| WK-EVID-008 | DONE | gptty branch installs the required lightweight transport dependencies and passes dependency compatibility including the `auth` extra. This is temporary ownership until WK-MAIN-032. |

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
6. **Final repository cleanup** — WK-MAIN-040 through 046. Remove experiment/legacy debris only from the pre-release branch; the frozen experiment branch remains intact.
7. **Full acceptance matrix and load re-run on the cleaned tree**.
8. **History cleanup** — WK-MAIN-036.
9. **Default promotion** — WK-MAIN-013 as a separate final change with an emergency legacy switch.

Do not switch the default early merely because an intermediate stage is green.

## Progress log

Append concise entries here whenever a tracker state changes. Reference task IDs and commits where available.

### 2026-09-10 — Audit baseline

- Created this promotion tracker from the full pre-main audit.
- Current CWA branch: `experiment/wk-curl-max-optimization`; current gptty branch: `feature/wkwebview-authority-e2e`.
- Current implementation remains opt-in; production/default backend has not been changed.
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
- `WK-MAIN-005` is done: the experimental shared-WK resume broker, daemon/socket runtime and `CWA_WK_SHARED_RESUME_BROKER` path were removed from the pre-release implementation. Direct WK resume remains only as a named compatibility fallback.
- Added focused lightweight transport unit coverage for authenticated canonical reads, attachment upload and resume-topic streaming/finality. Refactored WK/transport targeted suite is 25/25 green; full CWA is 2143/2143 green and downstream gptty remains 286/286 green.
- Live regression on the refactored boundary passed a new-chat exact-marker retry, a two-turn continuation with exact markers on both turns, and an image upload where the model correctly identified the generated solid-red fixture. One initial new-chat attempt hit the previously observed intermittent `WKWEBVIEW_MINIMAL_SECURITY_RESUME_FENCE_MISSING`; the immediate retry succeeded, so resilience remains tracked under `WK-MAIN-023` rather than being hidden by the refactor.

## Rules for maintaining this tracker

- Never mark an item `DONE` from code inspection alone when its acceptance criterion requires a live or installed-artifact test.
- Add newly discovered promotion defects here before fixing them so they cannot disappear into chat history.
- If an item is intentionally not fixed, mark it `DEFERRED` or `REJECTED` and record the reason in the progress log.
- Keep experiment measurements in the optimization log; keep promotion decision state here.
- Treat `experiment/wk-curl-max-optimization` as immutable evidence unless a new experiment is intentionally started on a separate experiment branch.
- Do not mark final acceptance complete until WK-MAIN-040 through WK-MAIN-046 have been completed on the pre-release branch and the acceptance matrix has been rerun afterward.
- Keep the final default switch separate from refactor/hardening commits so rollback remains straightforward.
