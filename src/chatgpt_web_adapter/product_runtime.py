from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from . import product_runtime_core as _core
from .auth import DEFAULT_AUTH_FILE
from .browser_authority_backend import (
    WKWEBVIEW_BROWSER_AUTHORITY_BACKEND,
    assemble_browser_authority_provider,
    normalize_browser_authority_backend,
    resolve_browser_authority_backend,
)
from .client import DEFAULT_TIMEOUT_SECONDS, ChatGPTWebClient
from .product_runtime_observation_gate import gate_product_runtime_send_text_observed
from .product_submission import ProductSubmissionAck
from .product_transport import (
    BROWSER_OWNED_PRODUCT_TRANSPORT as BROWSER_OWNED_PRODUCT_TRANSPORT,
)
from .product_transport import (
    DEFAULT_PRODUCT_TRANSPORT,
    ConversationInput,
    EventCallback,
    ProductRuntimeExecution,
    ProductWriteTransport,
    TokenCallback,
    normalize_product_transport,
    require_canonical_conversation_client,
    require_product_write_transport,
)
from .product_transport import (
    SUPPORTED_PRODUCT_TRANSPORTS as SUPPORTED_PRODUCT_TRANSPORTS,
)
from .product_transport import (
    CanonicalConversationClient as CanonicalConversationClient,
)
from .product_transport import ProductRuntimeHealth as ProductRuntimeHealth
from .product_ui_liveness import BrowserUILivenessObservation
from .types import ChatResponse, MediaItem

ProductConversationModeUnavailableError = _core.ProductConversationModeUnavailableError
ProductRichInputUnavailableError = _core.ProductRichInputUnavailableError

# Keep historical internal helpers import-compatible while making the public runtime
# class and assembly functions explicit in this module.
_assemble_default_write_transport = _core._assemble_default_write_transport


def __getattr__(name: str) -> Any:
    """Delegate untouched runtime implementation details to the frozen core."""

    return getattr(_core, name)


class ChatGPTProductRuntime(_core.ChatGPTProductRuntime):
    """Explicitly composed ordinary ChatGPT product runtime."""

    def __init__(
        self,
        client: Any,
        *,
        transport: str = DEFAULT_PRODUCT_TRANSPORT,
        provider: Any | None = None,
        write_transport: ProductWriteTransport | None = None,
        browser_authority_policy: str | None = None,
        browser_authority_ttl_ms: int | None = None,
    ) -> None:
        self.transport = normalize_product_transport(transport)
        self.client = require_canonical_conversation_client(client)
        self.canonical = self.client

        if write_transport is not None and provider is not None:
            raise ValueError("provider and write_transport are mutually exclusive")
        if write_transport is not None and (
            browser_authority_policy is not None or browser_authority_ttl_ms is not None
        ):
            raise ValueError(
                "browser authority runtime defaults require runtime-owned transport assembly"
            )

        if write_transport is None:
            assembly_kwargs: dict[str, Any] = {
                "transport": self.transport,
                "provider": provider,
            }
            if (
                browser_authority_policy is not None
                or browser_authority_ttl_ms is not None
            ):
                assembly_kwargs.update(
                    {
                        "browser_authority_policy": browser_authority_policy,
                        "browser_authority_ttl_ms": browser_authority_ttl_ms,
                    }
                )
            write_transport = _assemble_default_write_transport(
                self.canonical,
                **assembly_kwargs,
            )
        else:
            write_transport = require_product_write_transport(write_transport)
            injected_id = write_transport.transport_id.strip().lower()
            if injected_id != self.transport:
                raise ValueError(
                    "write transport identity does not match selected transport: "
                    f"{injected_id!r} != {self.transport!r}"
                )

        self.write_transport = write_transport
        transport_canonical = getattr(write_transport, "canonical_client", None)
        if transport_canonical is not None:
            self.canonical = require_canonical_conversation_client(transport_canonical)
        self._transport = write_transport
        self._writer = getattr(write_transport, "_runtime", write_transport)

    def stop_generation(
        self,
        conversation: ConversationInput = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        helper = getattr(self.write_transport, "stop_generation", None)
        if not callable(helper):
            raise RuntimeError(
                "stop generation is unavailable for the selected product transport"
            )
        return helper(conversation, timeout=timeout)

    def list_conversations(self) -> list[dict[str, Any]]:
        helper = getattr(self.canonical, "list_conversations", None)
        if not callable(helper):
            raise RuntimeError(
                "canonical conversation catalog is unavailable on the selected canonical client"
            )
        return [dict(item) for item in helper()]

    def list_models(self) -> list[dict[str, Any]]:
        helper = getattr(self.canonical, "list_models", None)
        if not callable(helper):
            raise RuntimeError(
                "canonical model catalog is unavailable on the selected canonical client"
            )
        return [dict(item) for item in helper()]

    def conversation_snapshot(self, conversation: Any, **kwargs: Any) -> dict[str, Any]:
        helper = getattr(self.canonical, "conversation_snapshot", None)
        if not callable(helper):
            raise RuntimeError(
                "canonical conversation snapshot is unavailable on the selected canonical client"
            )
        return dict(helper(conversation, **kwargs))

    def get_conversation_payload(self, conversation: Any) -> dict[str, Any]:
        helper = getattr(self.canonical, "get_conversation_payload", None)
        if not callable(helper):
            raise RuntimeError(
                "raw canonical conversation payload is unavailable on the selected canonical client"
            )
        return dict(helper(conversation))

    @gate_product_runtime_send_text_observed
    def send_text_observed(
        self,
        text: str,
        *,
        conversation: ConversationInput = None,
        timeout: float = 150.0,
        poll_interval: float = 0.5,
        on_token: TokenCallback = None,
        on_event: EventCallback = None,
        conversation_mode: str = "normal",
        browser_authority_policy: str | None = None,
        browser_authority_ttl_ms: int | None = None,
        model_profile: str | None = None,
        model: str | None = None,
        media: Sequence[MediaItem] | None = None,
    ) -> ProductRuntimeExecution:
        return super().send_text_observed(
            text,
            conversation=conversation,
            timeout=timeout,
            poll_interval=poll_interval,
            on_token=on_token,
            on_event=on_event,
            conversation_mode=conversation_mode,
            browser_authority_policy=browser_authority_policy,
            browser_authority_ttl_ms=browser_authority_ttl_ms,
            model_profile=model_profile,
            model=model,
            media=media,
        )

    def submit(
        self,
        text: str,
        *,
        conversation: ConversationInput = None,
        timeout: float = 150.0,
        poll_interval: float = 0.5,
        on_token: TokenCallback = None,
        on_event: EventCallback = None,
        conversation_mode: str = "normal",
        browser_authority_policy: str | None = None,
        browser_authority_ttl_ms: int | None = None,
        model_profile: str | None = None,
        media: Sequence[MediaItem] | None = None,
    ) -> ProductSubmissionAck:
        return super().submit(
            text,
            conversation=conversation,
            timeout=timeout,
            poll_interval=poll_interval,
            on_token=on_token,
            on_event=on_event,
            conversation_mode=conversation_mode,
            browser_authority_policy=browser_authority_policy,
            browser_authority_ttl_ms=browser_authority_ttl_ms,
            model_profile=model_profile,
            media=media,
        )

    def await_final(self, submission: ProductSubmissionAck) -> ChatResponse:
        return super().await_final(submission)

    def submission_lifecycle_snapshot(self) -> dict[str, Any]:
        return super().submission_lifecycle_snapshot()

    def observe_ui_liveness(
        self,
        *,
        timeout: float = 3.0,
    ) -> BrowserUILivenessObservation:
        return super().observe_ui_liveness(timeout=timeout)

    def governance(self) -> dict[str, Any]:
        return super().governance()


def assemble_product_runtime(
    *,
    transport: str = DEFAULT_PRODUCT_TRANSPORT,
    client: Any | None = None,
    provider: Any | None = None,
    write_transport: ProductWriteTransport | None = None,
    browser_authority_backend: str | None = None,
    browser_authority_policy: str | None = None,
    browser_authority_ttl_ms: int | None = None,
    auth_file: str | Path = DEFAULT_AUTH_FILE,
    client_timeout: int = DEFAULT_TIMEOUT_SECONDS,
    auto_refresh_auth: bool = True,
    persist_refreshed_auth: bool = True,
) -> ChatGPTProductRuntime:
    """Assemble an explicit ordinary-ChatGPT product runtime without fallback."""

    normalized = normalize_product_transport(transport)
    normalized_backend: str | None = None
    if browser_authority_backend is not None:
        normalized_backend = normalize_browser_authority_backend(
            browser_authority_backend
        )
        if normalized != BROWSER_OWNED_PRODUCT_TRANSPORT:
            raise ValueError(
                "browser authority backend selection requires transport='browser-owned'"
            )
        if provider is not None:
            raise ValueError(
                "provider and browser_authority_backend are mutually exclusive"
            )
        if write_transport is not None:
            raise ValueError(
                "write_transport and browser_authority_backend are mutually exclusive"
            )
    elif (
        normalized == BROWSER_OWNED_PRODUCT_TRANSPORT
        and provider is None
        and write_transport is None
    ):
        normalized_backend = resolve_browser_authority_backend(None)

    if normalized_backend is not None:
        provider = assemble_browser_authority_provider(normalized_backend)
        if (
            normalized_backend == WKWEBVIEW_BROWSER_AUTHORITY_BACKEND
            and browser_authority_policy is None
        ):
            browser_authority_policy = "TURN_SCOPED"

    if client is None:
        client = ChatGPTWebClient(
            auth_file=auth_file,
            timeout=client_timeout,
            auto_refresh_auth=auto_refresh_auth,
            persist_refreshed_auth=persist_refreshed_auth,
            auto_login=False,
            auto_sentinel=False,
        )

    canonical = require_canonical_conversation_client(client)

    runtime_browser_authority_policy = browser_authority_policy
    runtime_browser_authority_ttl_ms = browser_authority_ttl_ms
    if write_transport is None:
        assembly_kwargs: dict[str, Any] = {
            "transport": normalized,
            "provider": provider,
        }
        if browser_authority_policy is not None or browser_authority_ttl_ms is not None:
            assembly_kwargs.update(
                {
                    "browser_authority_policy": browser_authority_policy,
                    "browser_authority_ttl_ms": browser_authority_ttl_ms,
                }
            )
        write_transport = _assemble_default_write_transport(
            canonical,
            **assembly_kwargs,
        )
        provider = None
        runtime_browser_authority_policy = None
        runtime_browser_authority_ttl_ms = None

    return ChatGPTProductRuntime(
        canonical,
        transport=normalized,
        provider=provider,
        write_transport=write_transport,
        browser_authority_policy=runtime_browser_authority_policy,
        browser_authority_ttl_ms=runtime_browser_authority_ttl_ms,
    )
