from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import socket
import time
import uuid
from typing import Any

from .attach import attach_conversation
from .browser_native_protocol import (
    PROTOCOL_VERSION,
    recv_local_message,
    send_local_message,
)
from .browser_native_provider import BrowserNativeTurnProvider
from .client import ChatGPTWebClient
from .exceptions import RequestError
from .messages import get_messages
from .status import get_status
from .types import (
    AttachedConversation,
    ChatConversation,
    ChatMessage,
    ConversationRef,
    ConversationStatus,
)

BROWSER_CONTEXT_CANONICAL_READ_PLANE = "BROWSER_CONTEXT_CANONICAL_HTTP"
_CANONICAL_READ_STAGE = "browser_context_canonical_read"
_REASON_RE = re.compile(r"^[A-Z0-9_]+$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_RETRYABLE_CANONICAL_READ_REASONS = frozenset(
    {
        "CANONICAL_READ_TIMEOUT",
        "CANONICAL_READ_NETWORK_ERROR",
        "CANONICAL_READ_BRIDGE_FAILURE",
    }
)


class BrowserContextCanonicalReadError(RequestError):
    """Sanitized failure metadata from one authenticated browser-context read."""

    def __init__(
        self,
        reason_code: str,
        *,
        conversation_id: str,
        status_code: int | None = None,
        content_type: str | None = None,
        retryable: bool = False,
    ) -> None:
        normalized_reason = (
            reason_code
            if isinstance(reason_code, str) and _REASON_RE.fullmatch(reason_code)
            else "CANONICAL_READ_FAILED"
        )
        self.reason_code = normalized_reason
        self.conversation_id = ConversationRef(conversation_id).conversation_id
        self.content_type = (
            content_type[:128]
            if isinstance(content_type, str) and content_type
            else None
        )
        self.retryable = bool(retryable) or normalized_reason in _RETRYABLE_CANONICAL_READ_REASONS
        details = [f"reason={self.reason_code}"]
        if status_code is not None:
            details.append(f"status={status_code}")
        if self.content_type:
            details.append(f"content_type={self.content_type}")
        super().__init__(
            f"browser-context canonical read failed: {' '.join(details)}",
            status_code=status_code,
            endpoint="conversation",
            request_stage=_CANONICAL_READ_STAGE,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload.update(
            {
                "reason_code": self.reason_code,
                "conversation_id": self.conversation_id,
                "content_type": self.content_type,
                "retryable": self.retryable,
            }
        )
        return payload


class _CanonicalReadChunkCollector:
    """Reassemble exact response bytes only after a sealed integrity proof."""

    def __init__(self, *, request_id: str) -> None:
        self.request_id = request_id
        self.chunks: dict[int, bytes] = {}
        self.chunk_count: int | None = None
        self.total_bytes: int | None = None
        self.sha256: str | None = None

    def add(self, frame: dict[str, Any]) -> None:
        if frame.get("request_id") != self.request_id:
            raise ValueError("CANONICAL_READ_CHUNK_REQUEST_MISMATCH")
        index = frame.get("chunkIndex")
        count = frame.get("chunkCount")
        total_bytes = frame.get("totalBytes")
        digest = frame.get("sha256")
        data = frame.get("data")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            or not 0 <= index < count
        ):
            raise ValueError("CANONICAL_READ_CHUNK_INDEX_INVALID")
        if (
            isinstance(total_bytes, bool)
            or not isinstance(total_bytes, int)
            or total_bytes < 0
        ):
            raise ValueError("CANONICAL_READ_TOTAL_BYTES_INVALID")
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            raise ValueError("CANONICAL_READ_DIGEST_INVALID")
        if not isinstance(data, str):
            raise ValueError("CANONICAL_READ_CHUNK_DATA_INVALID")
        manifest = (count, total_bytes, digest)
        if self.chunk_count is not None and manifest != (
            self.chunk_count,
            self.total_bytes,
            self.sha256,
        ):
            raise ValueError("CANONICAL_READ_CHUNK_MANIFEST_MISMATCH")
        if index != len(self.chunks):
            raise ValueError("CANONICAL_READ_CHUNK_ORDER_INVALID")
        if index in self.chunks:
            raise ValueError("CANONICAL_READ_CHUNK_DUPLICATE")
        try:
            decoded = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("CANONICAL_READ_CHUNK_BASE64_INVALID") from error
        self.chunk_count, self.total_bytes, self.sha256 = manifest
        self.chunks[index] = decoded

    def finish(self, response: dict[str, Any]) -> bytes:
        expected_manifest = (self.chunk_count, self.total_bytes, self.sha256)
        final_manifest = (
            response.get("chunkCount"),
            response.get("totalBytes"),
            response.get("sha256"),
        )
        if expected_manifest != final_manifest:
            raise ValueError("CANONICAL_READ_FINAL_MANIFEST_MISMATCH")
        if self.chunk_count is None or len(self.chunks) != self.chunk_count:
            raise ValueError("CANONICAL_READ_CHUNK_MISSING")
        body = b"".join(self.chunks[index] for index in range(self.chunk_count))
        if len(body) != self.total_bytes:
            raise ValueError("CANONICAL_READ_TOTAL_BYTES_MISMATCH")
        actual_digest = hashlib.sha256(body).hexdigest()
        if self.sha256 is None or not hmac.compare_digest(actual_digest, self.sha256):
            raise ValueError("CANONICAL_READ_DIGEST_MISMATCH")
        return body


class BrowserContextCanonicalTransport:
    """Read canonical JSON through the authenticated Chrome runtime context."""

    def __init__(
        self,
        provider: BrowserNativeTurnProvider,
        *,
        read_timeout: float = 30.0,
    ) -> None:
        if not isinstance(provider, BrowserNativeTurnProvider) and not callable(
            getattr(provider, "_load_descriptor", None)
        ):
            raise TypeError("provider must expose the browser-native bridge descriptor")
        if read_timeout <= 0:
            raise ValueError("read_timeout must be positive")
        self.provider = provider
        self.read_timeout = float(read_timeout)

    def _lease_id(self) -> str | None:
        getter = getattr(self.provider, "_current_browser_authority_lease_id", None)
        if not callable(getter):
            return None
        value = getter()
        return value if isinstance(value, str) and value else None

    def _descriptor(self) -> dict[str, Any]:
        return self.provider._load_descriptor()

    def complete_readback(self) -> bool:
        """Release a matching host reservation after Python reaches terminality."""

        lease_id = self._lease_id()
        if lease_id is None:
            return True
        deadline = time.monotonic() + max(
            1.0,
            float(getattr(self.provider, "connect_timeout", 3.0)) + 5.5,
        )
        while time.monotonic() < deadline:
            descriptor = self._descriptor()
            request_id = str(uuid.uuid4())
            request = {
                "protocol": PROTOCOL_VERSION,
                "token": descriptor["token"],
                "type": "canonical_read_complete",
                "request_id": request_id,
                "browserAuthorityLeaseId": lease_id,
            }
            response: dict[str, Any] | None = None
            try:
                remaining = max(0.1, deadline - time.monotonic())
                with socket.create_connection(
                    (descriptor["host"], descriptor["port"]),
                    timeout=min(
                        float(getattr(self.provider, "connect_timeout", 3.0)),
                        remaining,
                    ),
                ) as sock:
                    sock.settimeout(remaining)
                    send_local_message(sock, request)
                    response = recv_local_message(sock)
            except (OSError, EOFError, ValueError):
                response = None
            if (
                isinstance(response, dict)
                and response.get("protocol") == PROTOCOL_VERSION
                and response.get("request_id") == request_id
                and response.get("ok") is True
                and response.get("type") == "canonical_read_complete_result"
            ):
                return True
            if (
                isinstance(response, dict)
                and response.get("error") != "BROWSER_NATIVE_BRIDGE_BUSY"
            ):
                return False
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return False

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
        descriptor = self._descriptor()
        request_id = str(uuid.uuid4())
        request = {
            "protocol": PROTOCOL_VERSION,
            "token": descriptor["token"],
            "type": "canonical_read",
            "request_id": request_id,
            "conversationId": ref.conversation_id,
            "timeoutMs": int(read_timeout * 1000),
            "browserAuthorityLeaseId": self._lease_id(),
        }
        collector = _CanonicalReadChunkCollector(request_id=request_id)
        deadline = time.monotonic() + read_timeout + 6.0
        try:
            remaining = max(0.1, deadline - time.monotonic())
            with socket.create_connection(
                (descriptor["host"], descriptor["port"]),
                timeout=min(
                    float(getattr(self.provider, "connect_timeout", 3.0)),
                    remaining,
                ),
            ) as sock:
                sock.settimeout(remaining)
                send_local_message(sock, request)
                while True:
                    frame = recv_local_message(sock)
                    if frame.get("protocol") != PROTOCOL_VERSION:
                        raise BrowserContextCanonicalReadError(
                            "CANONICAL_READ_PROTOCOL_MISMATCH",
                            conversation_id=ref.conversation_id,
                        )
                    if frame.get("request_id") != request_id:
                        raise BrowserContextCanonicalReadError(
                            "CANONICAL_READ_RESPONSE_MISMATCH",
                            conversation_id=ref.conversation_id,
                        )
                    if frame.get("type") == "canonical_read_chunk":
                        try:
                            collector.add(frame)
                        except ValueError as error:
                            raise BrowserContextCanonicalReadError(
                                str(error),
                                conversation_id=ref.conversation_id,
                            ) from error
                        continue
                    response = frame
                    break
        except BrowserContextCanonicalReadError:
            raise
        except (OSError, EOFError, ValueError) as error:
            raise BrowserContextCanonicalReadError(
                "CANONICAL_READ_BRIDGE_FAILURE",
                conversation_id=ref.conversation_id,
            ) from error

        if response.get("ok") is not True:
            reason = response.get("reasonCode") or response.get("error")
            status = response.get("status")
            status_code = (
                status
                if isinstance(status, int) and not isinstance(status, bool)
                else None
            )
            raise BrowserContextCanonicalReadError(
                reason if isinstance(reason, str) else "CANONICAL_READ_FAILED",
                conversation_id=ref.conversation_id,
                status_code=status_code,
                content_type=response.get("contentType")
                if isinstance(response.get("contentType"), str)
                else None,
                retryable=response.get("retryable") is True,
            )
        if response.get("type") != "canonical_read_result":
            raise BrowserContextCanonicalReadError(
                "CANONICAL_READ_RESULT_TYPE_INVALID",
                conversation_id=ref.conversation_id,
            )
        try:
            raw_body = collector.finish(response)
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            reason = str(error)
            raise BrowserContextCanonicalReadError(
                reason
                if _REASON_RE.fullmatch(reason or "")
                else "CANONICAL_READ_MALFORMED_JSON",
                conversation_id=ref.conversation_id,
                status_code=response.get("status")
                if isinstance(response.get("status"), int)
                and not isinstance(response.get("status"), bool)
                else None,
                content_type=response.get("contentType")
                if isinstance(response.get("contentType"), str)
                else None,
            ) from error
        if not isinstance(payload, dict):
            raise BrowserContextCanonicalReadError(
                "CANONICAL_READ_JSON_OBJECT_REQUIRED",
                conversation_id=ref.conversation_id,
            )
        return payload

    def read_catalog(
        self,
        catalog: str,
        *,
        offset: int = 0,
        limit: int = 100,
        is_archived: bool = False,
        is_starred: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        normalized = catalog.strip().lower() if isinstance(catalog, str) else ""
        if normalized not in {"conversations", "models"}:
            raise ValueError("catalog must be conversations or models")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative int")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an int between 1 and 100")
        read_timeout = self.read_timeout if timeout is None else float(timeout)
        if read_timeout <= 0:
            raise ValueError("timeout must be positive")

        descriptor = self._descriptor()
        request_id = str(uuid.uuid4())
        request = {
            "protocol": PROTOCOL_VERSION,
            "token": descriptor["token"],
            "type": "catalog_read",
            "request_id": request_id,
            "catalog": normalized,
            "offset": offset,
            "limit": limit,
            "isArchived": bool(is_archived),
            "isStarred": bool(is_starred),
            "timeoutMs": int(read_timeout * 1000),
        }
        collector = _CanonicalReadChunkCollector(request_id=request_id)
        deadline = time.monotonic() + read_timeout + 6.0
        try:
            remaining = max(0.1, deadline - time.monotonic())
            with socket.create_connection(
                (descriptor["host"], descriptor["port"]),
                timeout=min(
                    float(getattr(self.provider, "connect_timeout", 3.0)),
                    remaining,
                ),
            ) as sock:
                sock.settimeout(remaining)
                send_local_message(sock, request)
                while True:
                    frame = recv_local_message(sock)
                    if frame.get("protocol") != PROTOCOL_VERSION:
                        raise RequestError(
                            "CATALOG_READ_PROTOCOL_MISMATCH",
                            request_stage=_CANONICAL_READ_STAGE,
                        )
                    if frame.get("request_id") != request_id:
                        raise RequestError(
                            "CATALOG_READ_RESPONSE_MISMATCH",
                            request_stage=_CANONICAL_READ_STAGE,
                        )
                    if frame.get("type") == "canonical_read_chunk":
                        collector.add(frame)
                        continue
                    response = frame
                    break
        except RequestError:
            raise
        except (OSError, EOFError, ValueError) as error:
            raise RequestError(
                "CATALOG_READ_BRIDGE_FAILURE",
                request_stage=_CANONICAL_READ_STAGE,
            ) from error

        if response.get("ok") is not True:
            reason = response.get("reasonCode") or response.get("error")
            safe_reason = (
                reason
                if isinstance(reason, str) and _REASON_RE.fullmatch(reason)
                else "CATALOG_READ_FAILED"
            )
            status = response.get("status")
            raise RequestError(
                safe_reason,
                status_code=(
                    status
                    if isinstance(status, int) and not isinstance(status, bool)
                    else None
                ),
                request_stage=_CANONICAL_READ_STAGE,
            )
        if response.get("type") != "catalog_read_result":
            raise RequestError(
                "CATALOG_READ_RESULT_TYPE_INVALID",
                request_stage=_CANONICAL_READ_STAGE,
            )
        try:
            raw_body = collector.finish(response)
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise RequestError(
                "CATALOG_READ_MALFORMED_JSON",
                request_stage=_CANONICAL_READ_STAGE,
            ) from error
        if not isinstance(payload, dict):
            raise RequestError(
                "CATALOG_READ_JSON_OBJECT_REQUIRED",
                request_stage=_CANONICAL_READ_STAGE,
            )
        return payload


class BrowserContextCanonicalClient:
    """Interpret exact Chrome-fetched canonical payloads using existing Python policy."""

    def __init__(
        self,
        source_client: Any,
        provider: BrowserNativeTurnProvider,
        *,
        read_timeout: float = 30.0,
    ) -> None:
        self.source_client = source_client
        self.provider = provider
        self.transport = BrowserContextCanonicalTransport(
            provider,
            read_timeout=read_timeout,
        )
        self._browser_native_turn_provider = provider

    def _get_conversation_payload(self, conversation_id: str) -> dict[str, Any]:
        return self.transport.read_conversation(conversation_id)

    def complete_canonical_readback(self) -> bool:
        return self.transport.complete_readback()

    def get_status(
        self,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str,
    ) -> ConversationStatus:
        return get_status(self, conversation)

    def get_messages(
        self,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str,
        **kwargs: Any,
    ) -> list[ChatMessage]:
        return get_messages(self, conversation, **kwargs)

    def get_conversation_payload(
        self,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str,
    ) -> dict[str, Any]:
        """Return the exact canonical conversation payload fetched in browser context."""
        ref = ConversationRef.from_any(conversation)
        return self._get_conversation_payload(ref.conversation_id)

    def attach_conversation(
        self,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str,
    ) -> AttachedConversation:
        return attach_conversation(self, conversation)

    def list_conversations(self) -> list[dict[str, Any]]:
        conversations: dict[str, dict[str, Any]] = {}
        page_size = 100
        for archived in (False, True):
            for starred in (False, True):
                offset = 0
                while True:
                    payload = self.transport.read_catalog(
                        "conversations",
                        offset=offset,
                        limit=page_size,
                        is_archived=archived,
                        is_starred=starred,
                    )
                    raw_items = payload.get("items")
                    items = raw_items if isinstance(raw_items, list) else []
                    valid = [item for item in items if isinstance(item, dict)]
                    for item in valid:
                        conversation_id = item.get("id")
                        if isinstance(conversation_id, str) and conversation_id.strip():
                            conversations[conversation_id.strip()] = dict(item)
                    offset += len(valid)
                    total = payload.get("total")
                    if not valid or len(valid) < page_size:
                        break
                    if isinstance(total, int) and not isinstance(total, bool) and offset >= total:
                        break
        return sorted(
            conversations.values(),
            key=lambda item: str(item.get("update_time") or ""),
            reverse=True,
        )

    def list_models(self) -> list[dict[str, Any]]:
        payload = self.transport.read_catalog("models")
        raw_models = payload.get("models")
        models = raw_models if isinstance(raw_models, list) else []
        return [
            dict(model)
            for model in models
            if isinstance(model, dict)
            and isinstance(model.get("slug"), str)
            and model["slug"].strip()
        ]

    def conversation_snapshot(
        self,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        ref = ConversationRef.from_any(conversation)
        payload = self._get_conversation_payload(ref.conversation_id)

        class _SnapshotReader:
            def _get_conversation_payload(self, _conversation_id: str) -> dict[str, Any]:
                return payload

        reader = _SnapshotReader()
        return {
            "status": get_status(reader, ref),
            "messages": get_messages(reader, ref, limit=limit),
        }

    @staticmethod
    def _emit_event(callback: Any, event_type: str, **payload: Any) -> None:
        if callback is not None:
            callback({"type": event_type, **payload})

    @staticmethod
    def _current_message_from_conversation(
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        return ChatGPTWebClient._current_message_from_conversation(payload)

    @staticmethod
    def _latest_assistant_from_conversation(
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        return ChatGPTWebClient._latest_assistant_from_conversation(payload)

    @staticmethod
    def _latest_message_any_from_conversation(
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        return ChatGPTWebClient._latest_message_any_from_conversation(payload)
