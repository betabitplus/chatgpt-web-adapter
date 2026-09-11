from __future__ import annotations

import threading
from contextlib import nullcontext
from functools import wraps
from typing import Any

from .browser_authority_lease import (
    BrowserAuthorityPolicy,
    resolve_browser_authority_policy,
)
from .browser_context_canonical import (
    BROWSER_CONTEXT_CANONICAL_READ_PLANE,
    BrowserContextCanonicalClient,
)
from .browser_native_provider import BrowserNativeTurnProvider
from .browser_owned_submission_lifecycle import BrowserOwnedSubmissionLifecycle
from .browser_owned_write_runtime import (
    BrowserOwnedProductWriteRuntime,
    BrowserOwnedWriteExecution,
    BrowserOwnedWriteRuntimeHealth,
)
from .product_capabilities import (
    APPROVALS,
    CANONICAL_READBACK,
    CONTINUATION,
    CONVERSATION_ATTACH,
    CONVERSATION_BRANCHING,
    CONVERSATION_READ,
    CONVERSATION_STATUS,
    FILES,
    IMAGES,
    MODEL_PRESERVATION,
    MODEL_SELECTION,
    MULTIMODAL_CONTINUATION,
    NEW_CHAT,
    ORDINARY_CHATGPT_PRODUCT_SEMANTICS,
    PRODUCT_CAPABILITY_NAMES,
    PRODUCT_MEMORY_PERSONALIZATION,
    REASONING_PRESERVATION,
    REASONING_SELECTION,
    STREAMING,
    TEMPORARY_CHAT,
    TEXT_TURNS,
    TOOLS_CONNECTORS,
    WEB_SEARCH,
    CapabilityOwner,
    CapabilityState,
    ProductCapabilities,
    ProductCapability,
)
from .product_submission import (
    ProductSubmissionAck,
    ProductSubmissionProvenance,
    SubmissionEvidenceSource,
)
from .product_transport import (
    BROWSER_OWNED_PRODUCT_TRANSPORT,
    ConversationInput,
    EventCallback,
    ProductRuntimeExecution,
    ProductRuntimeHealth,
    TokenCallback,
    require_canonical_conversation_client,
)
from .temporary_product_runtime_pr8_13 import (
    TEMPORARY_READBACK_PLANE,
    TemporaryProductWriteRuntime,
    TemporaryProductWriteRuntimeError,
)
from .types import ChatResponse, ConversationRef

_NORMAL_CONVERSATION_MODE = "normal"
_TEMPORARY_CONVERSATION_MODE = "temporary"
_LEGACY_CANONICAL_READ_PLANE = "BROWSERLESS_CANONICAL_HTTP"

_BROWSER_OWNED_CAPABILITY_STATES: dict[str, CapabilityState] = {
    TEXT_TURNS: CapabilityState.AVAILABLE,
    NEW_CHAT: CapabilityState.AVAILABLE,
    CONTINUATION: CapabilityState.AVAILABLE,
    CANONICAL_READBACK: CapabilityState.AVAILABLE,
    CONVERSATION_ATTACH: CapabilityState.AVAILABLE,
    CONVERSATION_READ: CapabilityState.AVAILABLE,
    CONVERSATION_STATUS: CapabilityState.AVAILABLE,
    STREAMING: CapabilityState.AVAILABLE,
    IMAGES: CapabilityState.UNIMPLEMENTED,
    FILES: CapabilityState.UNKNOWN,
    WEB_SEARCH: CapabilityState.UNKNOWN,
    TEMPORARY_CHAT: CapabilityState.AVAILABLE,
    MODEL_SELECTION: CapabilityState.AVAILABLE,
    MODEL_PRESERVATION: CapabilityState.UNKNOWN,
    REASONING_SELECTION: CapabilityState.AVAILABLE,
    REASONING_PRESERVATION: CapabilityState.UNKNOWN,
    PRODUCT_MEMORY_PERSONALIZATION: CapabilityState.UNKNOWN,
    TOOLS_CONNECTORS: CapabilityState.UNKNOWN,
    APPROVALS: CapabilityState.UNIMPLEMENTED,
    CONVERSATION_BRANCHING: CapabilityState.UNKNOWN,
    MULTIMODAL_CONTINUATION: CapabilityState.UNIMPLEMENTED,
}

_BROWSER_OWNED_CAPABILITY_OWNERS: dict[str, CapabilityOwner] = {
    CANONICAL_READBACK: CapabilityOwner.CANONICAL,
    CONVERSATION_ATTACH: CapabilityOwner.CANONICAL,
    CONVERSATION_READ: CapabilityOwner.CANONICAL,
    CONVERSATION_STATUS: CapabilityOwner.CANONICAL,
    PRODUCT_MEMORY_PERSONALIZATION: CapabilityOwner.PRODUCT,
}

_BROWSER_OWNED_CAPABILITY_EVIDENCE: dict[str, str] = {
    TEXT_TURNS: "PR8.3 live ordinary-product text turns",
    NEW_CHAT: "PR8.3 live new-chat production gate",
    CONTINUATION: "PR8.3 live continuation production gate",
    CANONICAL_READBACK: "exact canonical payload fetched in authenticated Chrome and interpreted in Python",
    CONVERSATION_ATTACH: "browser-context canonical payload with Python attach semantics",
    CONVERSATION_READ: "browser-context canonical payload with Python current-branch semantics",
    CONVERSATION_STATUS: "browser-context canonical payload with Python status semantics",
    STREAMING: (
        "PR8.9.3 production live gate: revision-safe visible assistant text reached "
        "ChatGPTProductRuntime.on_event before browser write completion with EXACT_MATCH "
        "canonical reconciliation; PR8.12 adds normalized user-visible activity and "
        "final-only rendering"
    ),
    IMAGES: "production ProductWriteTransport currently exposes text turns only",
    TEMPORARY_CHAT: (
        "PR8.13 production live gate: two Temporary writes completed in one proven LIVE "
        "lifecycle with Fetch-paused history_and_training_disabled prewrite proof, stable "
        "session routing identity, page-owned finality, canonical 404 while live and after "
        "close, explicit lifecycle end, post-end continuation blocked before write, no "
        "automatic write retry, and no durable fallback; full regression 1222 passed"
    ),
    MODEL_SELECTION: (
        "PR8.10.1 production live gate: FAST/DEEP/BALANCED strictly selected "
        "INSTANT/HIGH/MEDIUM before write across slider states 0/2/1 with no "
        "automatic write retry"
    ),
    REASONING_SELECTION: (
        "PR8.10.1 production live gate: semantic reasoning profiles mapped to the "
        "proven INSTANT/MEDIUM/HIGH effort slider and were independently proven "
        "before each conversation write"
    ),
    APPROVALS: "production ProductWriteTransport has no approval continuation surface",
    MULTIMODAL_CONTINUATION: "production ProductWriteTransport currently exposes text turns only",
}

_PROFILE_SELECTION_CAPABILITIES = frozenset({MODEL_SELECTION, REASONING_SELECTION})


def _build_browser_owned_capabilities(
    *,
    profile_selection_supported: bool = True,
    temporary_chat_supported: bool = True,
    media_supported: bool = False,
    files_supported: bool = False,
    multimodal_continuation_supported: bool = False,
) -> ProductCapabilities:
    provider_available = {
        IMAGES: media_supported,
        FILES: files_supported,
        MULTIMODAL_CONTINUATION: multimodal_continuation_supported,
    }
    provider_evidence = {
        IMAGES: "configured browser authority provider has live-proven execution-local image attachment support",
        FILES: "configured browser authority provider has live-proven general-file attachment support",
        MULTIMODAL_CONTINUATION: "configured browser authority provider has live-proven multimodal continuation support",
    }

    def capability_state(name: str) -> CapabilityState:
        if name == TEMPORARY_CHAT and not temporary_chat_supported:
            return CapabilityState.UNIMPLEMENTED
        if provider_available.get(name, False):
            return CapabilityState.AVAILABLE
        if profile_selection_supported or name not in _PROFILE_SELECTION_CAPABILITIES:
            return _BROWSER_OWNED_CAPABILITY_STATES[name]
        return CapabilityState.UNKNOWN

    def capability_evidence(name: str) -> str | None:
        if name == TEMPORARY_CHAT and not temporary_chat_supported:
            return "configured browser authority provider does not implement Temporary Chat"
        if provider_available.get(name, False):
            return provider_evidence[name]
        if profile_selection_supported or name not in _PROFILE_SELECTION_CAPABILITIES:
            return _BROWSER_OWNED_CAPABILITY_EVIDENCE.get(name)
        return "configured browser-native provider does not expose PR8.10 profile requirements"

    return ProductCapabilities.from_entries(
        transport=BROWSER_OWNED_PRODUCT_TRANSPORT,
        product_semantics=ORDINARY_CHATGPT_PRODUCT_SEMANTICS,
        entries=(
            ProductCapability(
                name=name,
                state=capability_state(name),
                owner=_BROWSER_OWNED_CAPABILITY_OWNERS.get(
                    name,
                    CapabilityOwner.TRANSPORT,
                ),
                evidence=capability_evidence(name),
            )
            for name in PRODUCT_CAPABILITY_NAMES
        ),
    )


_BROWSER_OWNED_CAPABILITIES = _build_browser_owned_capabilities()


def _authority_override_kwargs(
    *,
    browser_authority_policy: BrowserAuthorityPolicy | str | None,
    browser_authority_ttl_ms: int | None,
) -> dict[str, Any]:
    if browser_authority_policy is None and browser_authority_ttl_ms is None:
        return {}
    return {
        "browser_authority_policy": browser_authority_policy,
        "browser_authority_ttl_ms": browser_authority_ttl_ms,
    }


def _normalize_mode(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("conversation_mode must be a string")
    mode = value.strip().lower()
    if mode not in {_NORMAL_CONVERSATION_MODE, _TEMPORARY_CONVERSATION_MODE}:
        raise ValueError("conversation_mode must be 'normal' or 'temporary'")
    return mode


def _serialize_submission_operation(method: Any) -> Any:
    """Serialize every browser-owned write/finality operation on one transport."""

    @wraps(method)
    def serialized(
        self: "BrowserOwnedProductTransport", *args: Any, **kwargs: Any
    ) -> Any:
        with self._submission_dispatch_lock:
            return method(self, *args, **kwargs)

    return serialized


class BrowserOwnedProductTransport:
    """Browser-owned ordinary-product transport with mode-specific finality."""

    transport_id = BROWSER_OWNED_PRODUCT_TRANSPORT

    def __init__(
        self,
        canonical_client: Any,
        *,
        provider: BrowserNativeTurnProvider | None = None,
        browser_authority_policy: BrowserAuthorityPolicy | str | None = None,
        browser_authority_ttl_ms: int | None = None,
    ) -> None:
        source_canonical = require_canonical_conversation_client(canonical_client)
        if provider is None:
            from .product_model_profile_pr8_10 import ProductModelProfileProvider

            provider = ProductModelProfileProvider()
        self.provider = provider
        canonical_builder = getattr(self.provider, "build_canonical_client", None)
        self._browser_context_canonical_enabled = isinstance(
            self.provider, BrowserNativeTurnProvider
        ) or callable(canonical_builder)
        if isinstance(source_canonical, BrowserContextCanonicalClient):
            self.canonical_client = source_canonical
        elif callable(canonical_builder):
            self.canonical_client = canonical_builder(source_canonical)
        elif isinstance(self.provider, BrowserNativeTurnProvider):
            self.canonical_client = BrowserContextCanonicalClient(
                source_canonical,
                self.provider,
            )
        else:
            self.canonical_client = source_canonical
        self._model_profile_selection_supported = callable(
            getattr(self.provider, "require_profile", None)
        )
        temporary_provider_builder = getattr(
            self.provider,
            "build_temporary_chat_provider",
            None,
        )
        self._temporary_provider = (
            temporary_provider_builder()
            if callable(temporary_provider_builder)
            else self.provider
        )
        self._temporary_chat_supported = (
            getattr(
                self.provider,
                "temporary_chat_supported",
                True,
            )
            is True
        )
        self._media_supported = (
            getattr(
                self.provider,
                "supports_attachment_paths",
                False,
            )
            is True
        )
        self._files_supported = getattr(self.provider, "supports_files", False) is True
        self._multimodal_continuation_supported = (
            getattr(self.provider, "supports_multimodal_continuation", False) is True
        )
        self._browser_authority_runtime_policy = browser_authority_policy
        self._browser_authority_runtime_ttl_ms = browser_authority_ttl_ms
        self._browser_authority_default_resolution = resolve_browser_authority_policy(
            runtime_policy=browser_authority_policy,
            runtime_ttl_ms=browser_authority_ttl_ms,
        )

        runtime_kwargs: dict[str, Any] = {"provider": self.provider}
        runtime_kwargs.update(
            _authority_override_kwargs(
                browser_authority_policy=browser_authority_policy,
                browser_authority_ttl_ms=browser_authority_ttl_ms,
            )
        )
        self._runtime = BrowserOwnedProductWriteRuntime(
            self.canonical_client,
            **runtime_kwargs,
        )
        self._submission_dispatch_lock = threading.RLock()
        self._submission_lifecycle = BrowserOwnedSubmissionLifecycle(self._runtime)
        self._temporary_runtime = TemporaryProductWriteRuntime(self._temporary_provider)

    @staticmethod
    def _health_from_runtime(
        health: BrowserOwnedWriteRuntimeHealth,
    ) -> ProductRuntimeHealth:
        return ProductRuntimeHealth(
            transport=BROWSER_OWNED_PRODUCT_TRANSPORT,
            ready=health.ready,
            reason=health.reason,
            conversation_id=health.conversation_id,
            canonical_status=health.canonical_status,
            canonical_read_checked=health.canonical_read_checked,
            read_plane=health.read_plane,
            canonical_read_reason_code=health.canonical_read_reason_code,
            canonical_read_status_code=health.canonical_read_status_code,
            canonical_read_content_type=health.canonical_read_content_type,
            session_plane=health.session_plane,
            write_plane=health.write_plane,
            automatic_write_retry=health.automatic_write_retry,
            fallback_transport=None,
            bridge_available=health.bridge_available,
            extension_connected=health.extension_connected,
            runtime_tab_id=health.runtime_tab_id,
            runtime_tab_preexisting=health.runtime_tab_preexisting,
        )

    def _model_profile_context(self, model_profile: str | None):
        if model_profile is None:
            return nullcontext()
        require_profile = getattr(self.provider, "require_profile", None)
        if not callable(require_profile):
            raise ValueError(
                "model profile selection is unavailable for the configured browser-native provider"
            )
        return require_profile(model_profile)

    @staticmethod
    def _require_temporary_default_authority_policy(
        *,
        browser_authority_policy: BrowserAuthorityPolicy | str | None,
        browser_authority_ttl_ms: int | None,
    ) -> None:
        if browser_authority_policy is not None or browser_authority_ttl_ms is not None:
            raise ValueError(
                "PR8.13 Temporary Chat does not yet expose Browser Authority TTL overrides; "
                "the live Temporary lifecycle owns its dedicated tab until explicit end"
            )

    def _temporary_session_routing_conversation(
        self,
        conversation: ConversationInput,
    ) -> str | None:
        """Resolve private Temporary routing without exposing id-based continuation.

        Public callers must omit ``conversation`` for Temporary mode. If a LIVE
        Temporary lifecycle already exists in this transport instance, its stored
        ephemeral conversation id is used only as the low-level product routing
        key. The id is never accepted as public continuation authority.
        """

        if conversation is not None:
            raise TemporaryProductWriteRuntimeError(
                "PR8_13_1_TEMPORARY_EXPLICIT_CONVERSATION_FORBIDDEN: "
                "Temporary continuation is session-scoped; omit conversation and reuse "
                "the same ChatGPTProductRuntime instance",
                write_may_have_been_submitted=False,
                reconciliation_required=False,
                request_stage="temporary_session_api_preflight",
            )

        snapshot = self._temporary_runtime.lifecycle_snapshot()
        if snapshot.get("state") != "LIVE":
            return None

        conversation_id = snapshot.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise TemporaryProductWriteRuntimeError(
                "PR8_13_1_TEMPORARY_LIVE_SESSION_ROUTING_ID_MISSING",
                write_may_have_been_submitted=False,
                reconciliation_required=False,
                request_stage="temporary_session_api_preflight",
            )
        return conversation_id.strip()

    def health(
        self,
        conversation: ConversationInput = None,
    ) -> ProductRuntimeHealth:
        return self._health_from_runtime(self._runtime.health(conversation))

    def capabilities(self) -> ProductCapabilities:
        if (
            self._model_profile_selection_supported
            and self._temporary_chat_supported
            and not self._media_supported
            and not self._files_supported
            and not self._multimodal_continuation_supported
        ):
            return _BROWSER_OWNED_CAPABILITIES
        return _build_browser_owned_capabilities(
            profile_selection_supported=self._model_profile_selection_supported,
            temporary_chat_supported=self._temporary_chat_supported,
            media_supported=self._media_supported,
            files_supported=self._files_supported,
            multimodal_continuation_supported=self._multimodal_continuation_supported,
        )

    def stop_generation(
        self,
        conversation: ConversationInput = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        conversation_id = None
        if conversation is not None:
            conversation_id = ConversationRef.from_any(conversation).conversation_id
        return self.provider.stop_generation(conversation_id, timeout=timeout)

    @_serialize_submission_operation
    def send_text(
        self,
        text: str,
        *,
        conversation: ConversationInput = None,
        timeout: float = 150.0,
        poll_interval: float = 0.5,
        on_token: TokenCallback = None,
        on_event: EventCallback = None,
        browser_authority_policy: BrowserAuthorityPolicy | str | None = None,
        browser_authority_ttl_ms: int | None = None,
        model_profile: str | None = None,
        model_slug: str | None = None,
        conversation_mode: str = _NORMAL_CONVERSATION_MODE,
    ) -> ChatResponse:
        self._submission_lifecycle.ensure_no_pending_submission()
        if model_profile is not None and model_slug is not None:
            raise ValueError("model_profile and model_slug are mutually exclusive")
        mode = _normalize_mode(conversation_mode)
        with self._model_profile_context(model_profile):
            if mode == _TEMPORARY_CONVERSATION_MODE:
                self._require_temporary_default_authority_policy(
                    browser_authority_policy=browser_authority_policy,
                    browser_authority_ttl_ms=browser_authority_ttl_ms,
                )
                routing_conversation = self._temporary_session_routing_conversation(
                    conversation
                )
                return self._temporary_runtime.send_text(
                    text,
                    conversation=routing_conversation,
                    timeout=timeout,
                    poll_interval=poll_interval,
                    on_token=on_token,
                    on_event=on_event,
                )

            authority_kwargs = _authority_override_kwargs(
                browser_authority_policy=browser_authority_policy,
                browser_authority_ttl_ms=browser_authority_ttl_ms,
            )
            model_kwargs = {"model_slug": model_slug} if model_slug is not None else {}
            return self._runtime.send_text(
                text,
                conversation=conversation,
                timeout=timeout,
                poll_interval=poll_interval,
                on_token=on_token,
                on_event=on_event,
                **model_kwargs,
                **authority_kwargs,
            )

    @_serialize_submission_operation
    def submit_text(
        self,
        text: str,
        *,
        conversation: ConversationInput = None,
        timeout: float = 150.0,
        poll_interval: float = 0.5,
        on_token: TokenCallback = None,
        on_event: EventCallback = None,
        browser_authority_policy: BrowserAuthorityPolicy | str | None = None,
        browser_authority_ttl_ms: int | None = None,
        model_profile: str | None = None,
        conversation_mode: str = _NORMAL_CONVERSATION_MODE,
    ) -> ProductSubmissionAck:
        mode = _normalize_mode(conversation_mode)
        if mode != _NORMAL_CONVERSATION_MODE:
            raise ValueError(
                "first-class submit/await_final is currently available only for normal browser-owned turns"
            )
        authority_kwargs = _authority_override_kwargs(
            browser_authority_policy=browser_authority_policy,
            browser_authority_ttl_ms=browser_authority_ttl_ms,
        )
        with self._model_profile_context(model_profile):
            ack = self._submission_lifecycle.submit_text(
                text,
                conversation=conversation,
                timeout=timeout,
                poll_interval=poll_interval,
                on_token=on_token,
                on_event=on_event,
                **authority_kwargs,
            )
        provenance = ProductSubmissionProvenance(
            product_semantics=ORDINARY_CHATGPT_PRODUCT_SEMANTICS,
            transport=self.transport_id,
            write_plane=self._runtime.governance()["write_plane"],
            evidence_source=SubmissionEvidenceSource.BROWSER_NATIVE_WRITE_COMPLETED,
            write_acknowledged=True,
            canonical_finality_proven=False,
            automatic_write_retry=False,
            fallback_transport=None,
        )
        return ProductSubmissionAck(
            submission_id=ack.submission_id,
            transport=self.transport_id,
            conversation_id=ack.conversation_id,
            turn_exchange_id=ack.turn_exchange_id,
            accepted_at_ms=ack.accepted_at_ms,
            turn_lifecycle_id=ack.observation.turn_lifecycle_id,
            write_may_have_committed=True,
            automatic_retry_allowed=False,
            canonical_finality_proven=False,
            provenance=provenance,
        )

    @_serialize_submission_operation
    def await_final(self, submission: ProductSubmissionAck) -> ChatResponse:
        if not isinstance(submission, ProductSubmissionAck):
            raise TypeError("submission must be ProductSubmissionAck")
        if submission.transport != self.transport_id:
            raise ValueError(
                "submission transport does not match browser-owned transport"
            )
        return self._submission_lifecycle.await_final(submission.submission_id)

    def submission_lifecycle_snapshot(self) -> dict[str, Any]:
        return self._submission_lifecycle.snapshot()

    @_serialize_submission_operation
    def send_text_observed(
        self,
        text: str,
        *,
        conversation: ConversationInput = None,
        timeout: float = 150.0,
        poll_interval: float = 0.5,
        on_token: TokenCallback = None,
        on_event: EventCallback = None,
        browser_authority_policy: BrowserAuthorityPolicy | str | None = None,
        browser_authority_ttl_ms: int | None = None,
        model_profile: str | None = None,
        model_slug: str | None = None,
        conversation_mode: str = _NORMAL_CONVERSATION_MODE,
    ) -> ProductRuntimeExecution:
        self._submission_lifecycle.ensure_no_pending_submission()
        if model_profile is not None and model_slug is not None:
            raise ValueError("model_profile and model_slug are mutually exclusive")
        mode = _normalize_mode(conversation_mode)
        with self._model_profile_context(model_profile):
            if mode == _TEMPORARY_CONVERSATION_MODE:
                self._require_temporary_default_authority_policy(
                    browser_authority_policy=browser_authority_policy,
                    browser_authority_ttl_ms=browser_authority_ttl_ms,
                )
                routing_conversation = self._temporary_session_routing_conversation(
                    conversation
                )
                return self._temporary_runtime.send_text_observed(
                    text,
                    conversation=routing_conversation,
                    timeout=timeout,
                    poll_interval=poll_interval,
                    on_token=on_token,
                    on_event=on_event,
                )

            authority_kwargs = _authority_override_kwargs(
                browser_authority_policy=browser_authority_policy,
                browser_authority_ttl_ms=browser_authority_ttl_ms,
            )
            model_kwargs = {"model_slug": model_slug} if model_slug is not None else {}
            execution: BrowserOwnedWriteExecution = self._runtime.send_text_observed(
                text,
                conversation=conversation,
                timeout=timeout,
                poll_interval=poll_interval,
                on_token=on_token,
                on_event=on_event,
                **model_kwargs,
                **authority_kwargs,
            )
        return ProductRuntimeExecution(
            transport=self.transport_id,
            response=execution.response,
            observation=execution.observation,
        )

    @_serialize_submission_operation
    def end_temporary_lifecycle(self) -> bool:
        self._submission_lifecycle.ensure_no_pending_submission()
        return self._temporary_runtime.close()

    def temporary_lifecycle_snapshot(self) -> dict[str, Any]:
        return self._temporary_runtime.lifecycle_snapshot()

    def governance(self) -> dict[str, Any]:
        governance = dict(self._runtime.governance())
        resolution = self._browser_authority_default_resolution
        governance.update(
            {
                "product_semantics": ORDINARY_CHATGPT_PRODUCT_SEMANTICS,
                "fallback_transport": None,
                "legacy_direct_write_fallback": False,
                "browser_authority_product_runtime_policy_supported": True,
                "browser_authority_backend": getattr(
                    self.provider,
                    "browser_authority_backend",
                    "chrome-native",
                ),
                "browser_authority_runtime_default_configurable": True,
                "browser_authority_per_turn_override_configurable": True,
                "browser_authority_policy_configuration_surface": "PRODUCT_RUNTIME",
                "browser_authority_policy_contract_scope": "RESOURCE_LIFECYCLE_ONLY",
                "browser_authority_effective_runtime_default_policy": resolution.policy.value,
                "browser_authority_effective_runtime_default_ttl_ms": resolution.ttl_ms,
                "browser_authority_runtime_default_policy_source": resolution.policy_source.value,
                "browser_authority_configured_runtime_ttl_ms": self._browser_authority_runtime_ttl_ms,
                "browser_authority_policy_exposes_runtime_tab_identity": False,
                "browser_authority_policy_requires_native_messaging_details": False,
                "model_slug_product_runtime_selection_supported": getattr(
                    self.provider,
                    "supports_model_slug",
                    True,
                ),
                "model_profile_product_runtime_selection_supported": self._model_profile_selection_supported,
                "media_product_runtime_supported": self._media_supported,
                "media_semantic_default_model_profile_supported": (
                    self._media_supported and self._model_profile_selection_supported
                ),
                "model_profile_request_values": ["FAST", "BALANCED", "DEEP"],
                "model_profile_product_modes": {
                    "FAST": "INSTANT",
                    "BALANCED": "MEDIUM",
                    "DEEP": "HIGH",
                },
                "model_profile_slider_indices": {"FAST": 0, "BALANCED": 1, "DEEP": 2},
                "model_profile_max_mapped": False,
                "model_profile_fallback": None,
                "silent_model_profile_fallback": False,
                "model_profile_strict_prewrite_verification": True,
                "model_profile_state_scope": "TURN_REQUIREMENT",
                "model_profile_preservation_scope_proven": False,
                "model_profile_automatic_write_retry": False,
                "streaming_supported": True,
                "streaming_contract_version": 1,
                "streaming_event_surface": "on_event",
                "streaming_event_types": [
                    "assistant_text_snapshot",
                    "assistant_text_delta",
                    "assistant_text_revision",
                    "canonical_text_finalized",
                    "activity_started",
                    "activity_text_snapshot",
                    "activity_text_delta",
                    "activity_text_revision",
                    "activity_completed",
                ],
                "streaming_source": getattr(
                    self.provider,
                    "streaming_source",
                    "CDP_NETWORK_STREAM_RESOURCE_CONTENT",
                ),
                "streaming_delivery": "REVISION_SAFE_EVENT_STREAM",
                "streaming_canonical_finality": (
                    getattr(
                        self.canonical_client,
                        "canonical_read_plane",
                        BROWSER_CONTEXT_CANONICAL_READ_PLANE,
                    )
                    if self._browser_context_canonical_enabled
                    else _LEGACY_CANONICAL_READ_PLANE
                ),
                "streaming_canonical_finality_authoritative": True,
                "incremental_observation_is_canonical_finality": False,
                "streaming_reconciliation_states": [
                    "EXACT_MATCH",
                    "CANONICAL_EXTENDS_STREAM",
                    "STREAM_REVISED_BY_CANONICAL",
                    "STREAM_INCOMPLETE",
                    "UNAVAILABLE",
                ],
                "streaming_legacy_on_token_semantics": "FINAL_ONLY",
                "streaming_raw_sse_exported": False,
                "streaming_automatic_write_retry": False,
                "temporary_chat_product_runtime_selection_supported": True,
                "temporary_chat_capability_live_graduated": True,
                "temporary_chat_prewrite_proof": (
                    "FETCH_PAUSED_PAGE_GENERATED_HISTORY_AND_TRAINING_DISABLED_TRUE"
                ),
                "temporary_chat_request_body_exported": False,
                "temporary_chat_request_body_rewritten": False,
                "temporary_chat_durable_fallback": False,
                "temporary_chat_automatic_write_retry": False,
                "temporary_chat_canonical_get_required": False,
                "temporary_chat_finality_plane": TEMPORARY_READBACK_PLANE,
                "temporary_chat_lifecycle_authority": "OPAQUE_PROCESS_LOCAL_TOKEN",
                "temporary_chat_conversation_id_alone_is_authority": False,
                "temporary_chat_runtime_reassembly_restores_lifecycle": False,
                "temporary_chat_tab_recreation_restores_lifecycle": False,
                "temporary_chat_explicit_lifecycle_end_supported": True,
                "temporary_chat_browser_authority_ttl_override_supported": False,
                "temporary_chat_live_gate_product_write_budget": 2,
                "temporary_chat_live_gate_product_write_completions": 2,
                "temporary_chat_full_regression_passed": 1222,
                "temporary_chat_public_continuation_model": "LIVE_RUNTIME_SESSION_ONLY",
                "temporary_chat_public_conversation_argument_supported": False,
                "temporary_chat_same_runtime_implicit_continuation": True,
                "temporary_chat_explicit_conversation_argument_fail_closed_before_write": True,
                "temporary_chat_internal_routing_identity_is_public_authority": False,
                "temporary_chat_new_session_after_explicit_end": True,
                "submission_lifecycle_supported": True,
                "submission_lifecycle_normal_mode_only": True,
                "submission_ack_model": "ProductSubmissionAck",
                "submission_acceptance_evidence": (
                    SubmissionEvidenceSource.BROWSER_NATIVE_WRITE_COMPLETED.value
                ),
                "submission_ack_is_canonical_finality": False,
                "submission_pending_limit": 1,
                "submission_pending_blocks_new_write": True,
                "submission_await_required_for_canonical_finality": True,
                "submission_handle_runtime_bound": True,
                "submission_dispatch_serialized": True,
                "submission_await_serialized": True,
                "submission_automatic_write_retry": False,
                "submission_fallback_transport": None,
                "browser_native_send_composes_submit_and_await_final": True,
            }
        )
        if not self._temporary_chat_supported:
            governance["temporary_chat_product_runtime_selection_supported"] = False
            governance["temporary_chat_capability_live_graduated"] = False
        return governance
