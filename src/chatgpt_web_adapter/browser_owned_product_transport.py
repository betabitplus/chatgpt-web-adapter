from __future__ import annotations

import threading
from typing import Any

from . import browser_owned_product_transport_core as _core
from .browser_authority_lease import (
    BrowserAuthorityPolicy,
    resolve_browser_authority_policy,
)
from .browser_context_canonical import (
    BrowserContextCanonicalClient as _LegacyBrowserContextCanonicalClient,
)
from .browser_context_canonical_v2 import BrowserContextCanonicalClientV2
from .browser_native_provider import BrowserNativeTurnProvider
from .browser_owned_submission_lifecycle import BrowserOwnedSubmissionLifecycle
from .browser_owned_write_runtime import BrowserOwnedProductWriteRuntime
from .product_rich_input_capability_gate_pr9_4 import (
    gate_browser_owned_rich_input_capabilities,
)
from .product_transport import require_canonical_conversation_client
from .product_web_search_capability_gate_pr9_3 import (
    gate_browser_owned_web_search_capability,
)
from .temporary_product_runtime_pr8_13 import TemporaryProductWriteRuntime


class BrowserOwnedProductTransport(_core.BrowserOwnedProductTransport):
    """Browser-owned transport with statically composed proven capabilities."""

    def __init__(
        self,
        canonical_client: Any,
        *,
        provider: BrowserNativeTurnProvider | None = None,
        browser_authority_policy: BrowserAuthorityPolicy | str | None = None,
        browser_authority_ttl_ms: int | None = None,
    ) -> None:
        # Keep constructor ownership in the public module. Besides making the
        # composition point explicit, this preserves the long-standing test and
        # integration seam where BrowserOwnedProductWriteRuntime can be replaced
        # on chatgpt_web_adapter.browser_owned_product_transport.
        source_canonical = require_canonical_conversation_client(canonical_client)
        if provider is None:
            from .product_model_profile_pr8_10 import ProductModelProfileProvider

            provider = ProductModelProfileProvider()
        self.provider = provider
        canonical_builder = getattr(self.provider, "build_canonical_client", None)
        self._browser_context_canonical_enabled = isinstance(
            self.provider,
            BrowserNativeTurnProvider,
        ) or callable(canonical_builder)
        if isinstance(source_canonical, _LegacyBrowserContextCanonicalClient):
            self.canonical_client = source_canonical
        elif callable(canonical_builder):
            self.canonical_client = canonical_builder(source_canonical)
        elif isinstance(self.provider, BrowserNativeTurnProvider):
            self.canonical_client = BrowserContextCanonicalClientV2(
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
            getattr(self.provider, "temporary_chat_supported", True) is True
        )
        self._media_supported = (
            getattr(self.provider, "supports_attachment_paths", False) is True
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
            _core._authority_override_kwargs(
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

    def stop_generation(
        self,
        conversation: Any = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        helper = getattr(self.provider, "stop_generation", None)
        if not callable(helper):
            raise RuntimeError(
                "stop generation is unavailable for the configured browser authority provider"
            )
        conversation_id = None
        if conversation is not None:
            from .types import ConversationRef

            conversation_id = ConversationRef.from_any(conversation).conversation_id
        return helper(conversation_id, timeout=timeout)

    def governance(self) -> dict[str, Any]:
        governance = dict(super().governance())
        governance.update(
            {
                "browser_authority_backend": getattr(
                    self.provider,
                    "browser_authority_backend",
                    "chrome-native",
                ),
                "model_slug_product_runtime_selection_supported": getattr(
                    self.provider,
                    "supports_model_slug",
                    True,
                ),
                "temporary_chat_product_runtime_selection_supported": (
                    self._temporary_chat_supported
                ),
                "media_product_runtime_supported": self._media_supported,
                "media_semantic_default_model_profile_supported": (
                    self._media_supported and self._model_profile_selection_supported
                ),
                "streaming_source": getattr(
                    self.provider,
                    "streaming_source",
                    governance.get("streaming_source"),
                ),
                "streaming_canonical_finality": (
                    getattr(
                        self.canonical_client,
                        "canonical_read_plane",
                        governance.get("streaming_canonical_finality"),
                    )
                    if self._browser_context_canonical_enabled
                    else governance.get("streaming_canonical_finality")
                ),
            }
        )
        return governance

    capabilities = gate_browser_owned_rich_input_capabilities(
        gate_browser_owned_web_search_capability(
            _core.BrowserOwnedProductTransport.capabilities
        )
    )


def __getattr__(name: str) -> Any:
    """Delegate untouched implementation details to the frozen legacy core."""

    return getattr(_core, name)
