from __future__ import annotations

from typing import Any

from .browser_context_canonical import BrowserContextCanonicalClient
from .types import ConversationRef

WKWEBVIEW_CONTEXT_CANONICAL_READ_PLANE = "WKWEBVIEW_CONTEXT_CANONICAL_HTTP"


class WKWebViewCanonicalTransport:
    """Fetch canonical ChatGPT JSON inside the persistent WKWebView origin."""

    def __init__(self, provider: Any, *, read_timeout: float = 30.0) -> None:
        if not callable(getattr(provider, "read_conversation_payload", None)):
            raise TypeError("provider must expose read_conversation_payload()")
        if read_timeout <= 0:
            raise ValueError("read_timeout must be positive")
        self.provider = provider
        self.read_timeout = float(read_timeout)

    def read_conversation(
        self,
        conversation_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        ref = ConversationRef(conversation_id)
        read_timeout = self.read_timeout if timeout is None else float(timeout)
        if read_timeout <= 0:
            raise ValueError("timeout must be positive")
        return self.provider.read_conversation_payload(
            ref.conversation_id,
            timeout=read_timeout,
        )

    def complete_readback(self) -> bool:
        # WK canonical reads are one-shot helper processes; there is no retained
        # host reservation to acknowledge after Python reaches finality.
        return True

    def read_catalog(self, catalog: str, **kwargs: Any) -> dict[str, Any]:
        reader = getattr(self.provider, "read_catalog_payload", None)
        if not callable(reader):
            raise RuntimeError("WKWebView canonical catalog reads are not implemented yet")
        return reader(catalog, **kwargs)


class WKWebViewCanonicalClient(BrowserContextCanonicalClient):
    """Existing canonical interpretation policy over WKWebView-fetched JSON."""

    def __init__(
        self,
        source_client: Any,
        provider: Any,
        *,
        read_timeout: float = 30.0,
    ) -> None:
        self.source_client = source_client
        self.provider = provider
        self.transport = WKWebViewCanonicalTransport(
            provider,
            read_timeout=read_timeout,
        )
        self.canonical_read_plane = WKWEBVIEW_CONTEXT_CANONICAL_READ_PLANE
        self._browser_native_turn_provider = provider


__all__ = [
    "WKWEBVIEW_CONTEXT_CANONICAL_READ_PLANE",
    "WKWebViewCanonicalClient",
    "WKWebViewCanonicalTransport",
]
