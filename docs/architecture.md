# Architecture

_Last updated: 2026-09-10_

`chatgpt-web-adapter` (CWA) is a layered product-runtime bridge around an existing ordinary ChatGPT web session. The current architecture is no longer a monolithic web-backend client and should not be understood primarily through historical Sentinel or direct-request internals.

The application-facing model is:

```text
application / HDE / CMA / terminal
                |
                v
        ChatGPTProductRuntime
      /            |            \
     /             |             \
canonical       product       structured
read/session     mutation      observations
    |               |              |
    |        ProductWriteTransport  |
    |               |              |
    |      browser-owned PRODUCTION|
    |      browserless EXPERIMENTAL|
    +---------------+--------------+
                    |
                    v
              ChatGPT product
```

The stable abstraction is the runtime and its contracts. Browser tabs, Native Messaging, extension worker composition, CDP targets, request correlation, Sentinel details, and research probes are implementation details below that boundary.

## 1. Product Runtime

Primary files:

- `src/chatgpt_web_adapter/product_runtime.py`
- `src/chatgpt_web_adapter/product_runtime_assembly.py`
- `src/chatgpt_web_adapter/product_transport.py`
- `src/chatgpt_web_adapter/product_capabilities.py`
- `src/chatgpt_web_adapter/product_provenance.py`
- `src/chatgpt_web_adapter/product_contract.py`
- `src/chatgpt_web_adapter/public_surface.py`

Responsibilities:

- expose the forward-looking application object `ChatGPTProductRuntime`;
- assemble an explicit product transport;
- keep canonical reads/finality separate from product mutation;
- expose provider-aware capability state;
- expose support-tier and runtime-contract metadata;
- preserve execution provenance;
- expose structured product observations without granting authority;
- fail closed rather than silently falling back to a legacy writer.

The intended application contract remains narrow:

```python
runtime.health(...)
runtime.capabilities()
runtime.send(...)
runtime.send_text_observed(...)
runtime.get_status(...)
runtime.get_messages(...)
runtime.attach_conversation(...)
```

Downstream callers should not need Chrome tab ids, extension worker names, Native Messaging details, debugger targets, minified React component names, or Sentinel internals.

## 2. Canonical Observation and Session Plane

The canonical plane is represented by the public `CanonicalConversationClient` contract used by `ChatGPTProductRuntime` for canonical conversation/session observation.

Primary areas:

- conversation attach/read/status;
- auth/session loading and refresh;
- canonical conversation/message identity;
- final assistant readback.

The canonical plane answers what durable conversation state exists and whether the exact submitted turn reached a canonical completed assistant message. A non-completed canonical status is not, by itself, proof that the browser is still generating: interrupted historical turns can remain `running`/`tool_running` after the product UI is ready again. Browser-owned continuation writes therefore keep canonical reads as a fail-closed durability check, but defer live-generation exclusion to the browser-native composer-readiness fence immediately before submit; explicit user-action states such as `awaiting_tool_approval` remain hard prewrite blocks. Transient browser-context canonical read transport failures (`TIMEOUT`, network, or bridge failure) are safe to retry within the already-accepted submission deadline because they are read-only reconciliation; they never authorize replaying the product write.

Core rule:

```text
incremental stream
!= structured observation
!= canonical finality
```

A successful protected turn ultimately requires canonical product evidence, not merely a DOM change, provisional SSE text, tool completion, or structured activity event.

Where supported, canonical reads and session renewal remain browserless even though production protected writes are browser-owned.

## 3. Product Mutation Plane

All product mutation flows through an explicit `ProductWriteTransport` selected by the runtime.

There is no automatic browser-owned ↔ browserless ↔ compatibility fallback.

### Browser-owned transport — `PRODUCTION`

The browser-owned product transport is the production mutation boundary. In the current pre-release source, implicit browser-owned runtime assembly selects WKWebView on identifiable macOS 12+ and Chrome Native on older/unknown macOS and non-Darwin hosts; explicit backend selection overrides that platform default. This selector has not yet been published into the stable package/global install: release still requires the clean committed-candidate and artifact gates in the WKWebView promotion checklist.

Important invariants shared by both browser-owned backends:

- exactly one delegated write attempt for one runtime invocation unless the caller explicitly starts another invocation;
- no automatic retry after an ambiguous write;
- no fallback to the experimental browserless-request transport;
- page-owned protection/challenge behavior is not reconstructed by the SDK;
- canonical assistant readback remains final authority for durable conversations; Temporary Chat uses its live product stream because the ephemeral conversation is intentionally not treated as canonical durable history.

#### Chrome Native backend

Primary areas:

- `browser_owned_product_transport.py`;
- `browser_owned_write_runtime.py`;
- browser-native client/provider;
- packaged MV3 extension;
- Native Messaging host.

The official ChatGPT page owns protected product-write semantics. CWA delegates a bounded write through the page and then returns to canonical observation for finality. A reusable runtime tab is an implementation resource, not public continuation authority. The extension does not intentionally request foreground activation, though Chrome may foreground a newly created cold-path tab.

#### WKWebView backend — macOS 12+ pre-release path

Primary areas:

- `wkwebview_provider.py`;
- `wkwebview_helper_runtime.py`;
- `wkwebview_turn_orchestrator.py`;
- `wkwebview_turn_observer.py`;
- `wkwebview_lightweight_transport.py`;
- `wkwebview_temporary.py`;
- packaged `wkwebview_helper/` Objective-C, plist, and minimal-shell resources.

The WK backend requires macOS 12 or newer and fails closed before helper construction on older or unidentifiable macOS versions. WebKit's default website data store owns the authenticated ChatGPT web-session state; CWA does not copy browser credentials into caller-controlled files.

The lightweight lifecycle is deliberately split into protected Phase A and lightweight finality/stream work:

```text
canonical prewrite state
-> globally serialized short-lived WK protected write
   |
   +-> normal: RESUME_FENCE + browser-issued resume handoff
   |      -> WK process released
   |      -> curl_cffi/WebSocket continuation stream
   |      -> lightweight canonical reconciliation/finality
   |
   +-> already terminal continuation
   |      -> WK process released
   |      -> canonical finality
   |
   +-> accepted write but no resume fence after bounded grace
          -> WK process released
          -> curl_cffi recent-catalog/canonical lookup
          -> exact client-message-id + prompt match
          -> canonical finality
```

The global heavy-submit gate is process/thread coordinated so several CWA/gptty sessions can stream and read concurrently while only the expensive browser-owned Phase A is admitted at once. Per-turn request data is sent to the native helper through stdin rather than process argv. The resume value is handed back over an inherited anonymous FD/pipe rather than a named secret file.

The minimal shell uses current server-provided ChatGPT bootstrap/security modules inside the browser context, performs prepare and the protected write there, and does not implement challenge solving or synthesize protection tokens outside that context. The normal path releases WebKit as soon as the browser-issued resume fence is proven. A continuation that is already terminal in Phase A may also release WebKit without opening a redundant resume leg. If ChatGPT accepts the protected write but no resume fence arrives, the helper waits only a bounded grace period, returns the non-secret client message id, and releases the heavy-submit gate. CWA then performs read-only lightweight recovery: it locates the canonical conversation containing that exact user-message id and prompt, waits for canonical finality, and never replays the protected write.

Exact model slugs are resolved against the live product model catalog inside the minimal shell and remain on the WK path. Image attachments use the established authenticated media uploader; general files use the same ChatGPT file create/upload/finalize flow through the lightweight transport before their descriptors enter the browser-owned write. Temporary Chat also remains self-contained: the WebKit submit observer proves `history_and_training_disabled=true` in the protected write body, a process-local lifecycle identity binds the ephemeral conversation to its latest assistant parent, and final text is taken from the product WebSocket stream without a durable canonical conversation GET. Explicit lifecycle end destroys that process-local binding.

The explicit WK backend uses the minimal-security Phase A plus lightweight curl/WebSocket second leg by default. `CWA_WK_FORCE_LEGACY=1` is the single emergency escape hatch that switches an explicitly selected WK backend back to the full-page Phase A plus direct-WK resume path; the former experimental enable flags are no longer part of the runtime contract. This switch does not change which browser-authority backend is selected globally.

Fallback and accepted-write recovery are bounded and must never replay the protected write. Recoverable lightweight transport failures, timeouts, and retryable HTTP states may fall back to passive/canonical observation or a one-shot WK read. Separately, an HTTP-accepted Phase-A write that lacks a resume fence may use only read-only canonical identity recovery keyed by the exact client user-message id and prompt; ambiguity or timeout fails closed. Authentication failures, malformed canonical/schema data, missing source-client contracts, and programming defects fail closed and propagate. The direct-WK resume path exists only as the explicit emergency path or bounded read/observation fallback; it is not a hidden retry after a failed protected write.

WK transport provenance is reported through the existing browser-owned write observation rather than a separate WK-only API. The canonical read plane is named `WKWEBVIEW_CANONICAL_READ`; the actual read transport is reported separately as `curl_cffi`, `wkwebview`, or `cache`. Per-turn metadata also records the Phase-A implementation, global-gate wait, Phase-A elapsed time, Phase-B implementation, Phase-B elapsed time, and a bounded fallback reason when fallback occurred. Fallback reasons are normalized to stable error/stage/HTTP codes and must not contain prompts, cookies, resume values, authorization headers, or arbitrary dependency error text.

### Browserless request transport — `EXPERIMENTAL`

`browserless-request` implements a direct-request transport behind the same runtime boundary but remains explicitly experimental because it depends more directly on changing undocumented web protocol behavior.

Its contract is fail-closed around current Sentinel/challenge requirements. CWA does not solve Turnstile, synthesize proof tokens, replay protected credentials, or fall back to browser-owned writes merely because direct admission fails.

Transport support tier and individual capability state remain separate contracts.

## 4. Rich Input Plane

Rich input is capability-gated per browser-owned provider rather than inherited from the transport name alone.

Supported evidence-backed paths include:

- image new chat;
- general file new chat;
- multimodal continuation.

The same protected-write and finality rules apply to rich input. Chrome Native and WKWebView expose the same capability states, but their upload mechanics remain provider-specific: Chrome Native carries validated local paths through Native Messaging so the official page owns upload and submit, while WKWebView performs the authenticated ChatGPT media/file upload through its lightweight transport before passing only the resulting descriptors into the browser-owned protected write. Both paths correlate the requested attachment set to the intended user message/conversation before treating the write as valid.

Capability graduation remains evidence-backed: an arbitrary custom provider does not inherit rich-input `AVAILABLE` state merely because it uses the browser-owned transport.

## 5. Structured Product Observation Plane

PR9.3 and PR10.0 add a bounded observation layer alongside, not inside, mutation/finality authority.

Root production observation values can represent:

- search activity;
- generic tool/activity points;
- source identity;
- citation-to-source relationships;
- required-action evidence.

Post-0.3 typed models additionally support stronger connector and required-action lifecycle representation when stable product identifiers are explicitly present.

Core rule:

```text
product observation
!= product approval
!= connector authorization
!= product write authority
!= retry authority
!= canonical finality
!= downstream filesystem/Git/workspace authority
```

The collector consumes only bounded standardized events. Raw tool arguments/results, raw connector payloads, arbitrary DOM text, private reasoning, credentials, cookies, authorization headers, signed URLs, and retrieved private connector content remain outside this typed observation boundary.

`tools_connectors` remains conservative (`UNKNOWN`) because current authenticated evidence does not prove a general stable connector execution contract.

## 6. Generated-Artifact Boundary

PR10.1 adds a narrow artifact observation model and characterizes current product surfaces.

The milestone deliberately stops before download authority.

```text
artifact observed
!= artifact locator exposed
!= download requested
!= destination authorized
!= overwrite authorized
!= canonical finality
```

Current frozen status:

```text
ARTIFACT_DOWNLOAD_HANDOFF_UNSUPPORTED_WITHOUT_STABLE_PRODUCT_IDENTITY
```

The current product characterization did not prove both a stable product-owned artifact identity and a safe browser-owned resolution path. CWA therefore does not synthesize identity from filename, message order, DOM position, assistant prose, URL similarity, or minified React/update-queue internals.

Historical PR10.1 characterization overlays remain in-tree as reproducible research evidence but are disabled from ordinary runtime startup by default.

## 7. Capability and Provenance Ownership

Capabilities answer whether a feature is implemented and evidence-backed for the selected runtime/provider path.

Canonical states:

- `AVAILABLE`;
- `UNSUPPORTED`;
- `UNKNOWN`;
- `UNIMPLEMENTED`.

Provenance describes what one execution actually observed: transport, completion source, canonical proof, request/conversation identity, and transport-specific metadata.

CWA does not fabricate provenance to make heterogeneous transports look identical.

## 8. Public Surface Tiers

The root package exposes machine-readable support classification through `PublicSurfaceTier`, `PUBLIC_SURFACE_CLASSIFICATION`, and `public_surface_tier()`.

### `PRIMARY_PRODUCTION`

Forward-looking runtime, product transport contract, canonical client contract, capabilities/provenance/contracts, and immutable structured observation value types.

### `SHARED_SUPPORT`

Auth/session helpers, common conversation/response/error types, and `MediaItem` / `MediaSource` rich-input types used by the primary runtime.

### `COMPATIBILITY`

`ChatGPTWebClient` / `WebChatClient` and historical workflows retained for existing callers without silent redirection into the product runtime.

### `EXPERIMENTAL`

Approval helpers, raw/prepared backend surfaces, payload helpers, and `browserless-request` where contracts depend more directly on undocumented product behavior.

### `RESEARCH_DIAGNOSTIC`

Direct browser-native provider/install APIs, Sentinel internals, feasibility probes, and product-characterization tooling used to investigate or repair boundaries.

Research artifacts are intentionally retained when they provide useful evidence, but their presence does not make them application APIs.

The historical tier decision remains documented in `docs/public_surface_pr8_6.md`.

## 9. Compatibility Boundary

`ChatGPTWebClient` remains import-compatible and useful for historical workflows. It is no longer the architecture reference for new product-turn integrations.

Do not silently route:

```text
ChatGPTWebClient.send()
-> ChatGPTProductRuntime
```

and do not silently fall back:

```text
ChatGPTProductRuntime
-> ChatGPTWebClient.send()
```

Migration remains explicit.

`USAGE.md` now documents the current runtime first and keeps compatibility/research paths separately discoverable.

## 10. Downstream Authority Boundary

CWA may provide product evidence to CMA, HDE, terminal tools, or arbitrary Python applications.

It does not own the meaning or authority those applications assign to that evidence.

Examples:

```text
CWA: "ChatGPT exposed a required authorization action"
caller: decides whether approval is allowed

CWA: "ChatGPT cited source X"
caller: decides how to use that source

CWA: "generated artifact observation exists"
caller: still has no download/filesystem authority unless a separate future handoff contract provides it
```

Project state, memory, task orchestration, Git policy, workspace mutation, and autonomous continuation remain outside this repository.

## 11. What Stays Out of CWA

The package should not become:

- a full chat application/TUI;
- HDE/CMA project memory or cognition;
- a generic project agent;
- a Git/filesystem authority layer;
- a browser-challenge circumvention toolkit;
- a caller-controlled abstraction over every internal ChatGPT tool;
- a stable SDK built on minified React/DOM internals;
- a generic multi-provider model abstraction before a real second product backend exists.

## 12. Architectural decision rule

When product behavior changes or a new capability is considered:

```text
observe narrowly
-> identify the decision-relevant product contract
-> preserve authority separation
-> add deterministic regression
-> perform bounded live validation when product-facing behavior changed
-> document the resulting capability/support boundary
```

Stop characterization when the architectural decision is already supported. Do not continue reverse engineering merely because deeper internal state is reachable.

See [`../ROADMAP.md`](../ROADMAP.md) for current development direction and [`README.md`](README.md) for the documentation map.
