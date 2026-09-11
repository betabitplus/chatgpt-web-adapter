from __future__ import annotations

import threading
import time
from typing import Any

from .browser_context_canonical import BrowserContextCanonicalClient
from .types import ConversationRef

WKWEBVIEW_CANONICAL_READ_PLANE = "WKWEBVIEW_CANONICAL_READ"


class WKCanonicalState:
    """Thread-safe canonical identity/finality state for the WK provider."""

    def __init__(self) -> None:
        self._current_nodes = threading.local()
        self._final_payload_lock = threading.Lock()
        self._final_payload_cache: dict[str, dict[str, Any]] = {}
        self._stop_final_condition = threading.Condition()
        self._stopped_final_payloads: dict[str, dict[str, Any]] = {}

    @staticmethod
    def payload_is_final(payload: dict[str, Any]) -> bool:
        mapping = payload.get("mapping")
        current_node = payload.get("current_node")
        if (
            not isinstance(mapping, dict)
            or not isinstance(current_node, str)
            or not current_node
        ):
            return False
        node = mapping.get(current_node)
        message = node.get("message") if isinstance(node, dict) else None
        if not isinstance(message, dict):
            return False
        author = message.get("author")
        metadata = message.get("metadata")
        finish = metadata.get("finish_details") if isinstance(metadata, dict) else None
        role = author.get("role") if isinstance(author, dict) else None
        if role != "assistant" or message.get("recipient") not in {None, "all"}:
            return False
        active = {
            "running",
            "in_progress",
            "pending",
            "queued",
            "started",
            "streaming",
        }
        statuses = [
            payload.get("async_status"),
            payload.get("status"),
            node.get("async_status") if isinstance(node, dict) else None,
            node.get("status") if isinstance(node, dict) else None,
            metadata.get("async_status") if isinstance(metadata, dict) else None,
            metadata.get("status") if isinstance(metadata, dict) else None,
            message.get("status"),
        ]
        if any(str(value or "").lower() in active for value in statuses):
            return False
        completed = {
            "completed",
            "complete",
            "finished",
            "done",
            "success",
            "succeeded",
            "finished_successfully",
        }
        finish_present = isinstance(finish, dict) and (
            isinstance(finish.get("type"), str) or isinstance(finish.get("reason"), str)
        )
        return bool(
            finish_present
            or message.get("end_turn") is True
            or any(str(value or "").lower() in completed for value in statuses)
        )

    @staticmethod
    def current_branch_contains_user_text(payload: dict[str, Any], text: str) -> bool:
        mapping = payload.get("mapping")
        current = payload.get("current_node")
        if not isinstance(mapping, dict) or not isinstance(current, str):
            return False
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            node = mapping.get(current)
            if not isinstance(node, dict):
                return False
            message = node.get("message")
            if isinstance(message, dict):
                author = message.get("author")
                content = message.get("content")
                parts = content.get("parts") if isinstance(content, dict) else None
                if (
                    isinstance(author, dict)
                    and author.get("role") == "user"
                    and isinstance(parts, list)
                ):
                    rendered = "\n".join(
                        part for part in parts if isinstance(part, str)
                    )
                    if rendered.strip() == text.strip():
                        return True
            parent = node.get("parent")
            current = parent if isinstance(parent, str) else ""
        return False

    def payload_matches_write(
        self,
        payload: dict[str, Any],
        *,
        text: str,
        baseline_current_node: str | None,
    ) -> bool:
        current_node = payload.get("current_node")
        if baseline_current_node is not None and (
            not isinstance(current_node, str)
            or not current_node
            or current_node == baseline_current_node
        ):
            return False
        return self.current_branch_contains_user_text(
            payload,
            text,
        ) and self.payload_is_final(payload)

    @staticmethod
    def is_client_stopped_payload(payload: dict[str, Any]) -> bool:
        mapping = payload.get("mapping")
        current_node = payload.get("current_node")
        if (
            not isinstance(mapping, dict)
            or not isinstance(current_node, str)
            or not current_node
        ):
            return False
        node = mapping.get(current_node)
        message = node.get("message") if isinstance(node, dict) else None
        if not isinstance(message, dict):
            return False
        author = message.get("author")
        metadata = message.get("metadata")
        finish = metadata.get("finish_details") if isinstance(metadata, dict) else None
        return (
            isinstance(author, dict)
            and author.get("role") == "assistant"
            and message.get("recipient") in {None, "all"}
            and isinstance(finish, dict)
            and finish.get("type") == "interrupted"
            and finish.get("reason") == "client_stopped"
        )

    def _current_node_cache(self) -> dict[str, str | None]:
        cache = getattr(self._current_nodes, "values", None)
        if not isinstance(cache, dict):
            cache = {}
            self._current_nodes.values = cache
        return cache

    def set_current_node(self, conversation_id: str, current_node: Any) -> None:
        self._current_node_cache()[conversation_id] = (
            current_node if isinstance(current_node, str) and current_node else None
        )

    def cached_current_node(self, conversation_id: str | None) -> str | None:
        if not conversation_id:
            return None
        value = self._current_node_cache().get(conversation_id)
        return value if isinstance(value, str) and value else None

    def cache_final_payload(
        self, conversation_id: str, payload: dict[str, Any]
    ) -> None:
        with self._final_payload_lock:
            self._final_payload_cache[conversation_id] = payload
        self.set_current_node(conversation_id, payload.get("current_node"))
        if self.is_client_stopped_payload(payload):
            with self._stop_final_condition:
                self._stopped_final_payloads[conversation_id] = payload
                self._stop_final_condition.notify_all()

    def take_final_payload(self, conversation_id: str) -> dict[str, Any] | None:
        with self._final_payload_lock:
            payload = self._final_payload_cache.pop(conversation_id, None)
        if isinstance(payload, dict):
            self.set_current_node(conversation_id, payload.get("current_node"))
            return payload
        return None

    def stopped_final_payload(self, conversation_id: str) -> dict[str, Any] | None:
        with self._stop_final_condition:
            payload = self._stopped_final_payloads.get(conversation_id)
        return payload if isinstance(payload, dict) else None

    def wait_for_stopped_final_payload(
        self,
        conversation_id: str,
        *,
        timeout: float,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._stop_final_condition:
            while True:
                payload = self._stopped_final_payloads.get(conversation_id)
                if isinstance(payload, dict):
                    return payload
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._stop_final_condition.wait(timeout=min(0.5, remaining))

    def clear_stopped_final_payload(self, conversation_id: str) -> None:
        with self._stop_final_condition:
            self._stopped_final_payloads.pop(conversation_id, None)


class WKWebViewCanonicalTransport:
    """Fetch canonical ChatGPT JSON through the WK provider transport."""

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
        # Canonical reads are one-shot transport operations; there is no retained
        # browser host reservation to acknowledge after Python reaches finality.
        return True

    def read_catalog(self, catalog: str, **kwargs: Any) -> dict[str, Any]:
        reader = getattr(self.provider, "read_catalog_payload", None)
        if not callable(reader):
            raise RuntimeError(
                "WKWebView canonical catalog reads are not implemented yet"
            )
        return reader(catalog, **kwargs)


class WKWebViewCanonicalClient(BrowserContextCanonicalClient):
    """Existing canonical interpretation policy over WK-provider-fetched JSON."""

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
        self.canonical_read_plane = WKWEBVIEW_CANONICAL_READ_PLANE
        self._browser_native_turn_provider = provider


__all__ = [
    "WKCanonicalState",
    "WKWEBVIEW_CANONICAL_READ_PLANE",
    "WKWebViewCanonicalClient",
    "WKWebViewCanonicalTransport",
]
