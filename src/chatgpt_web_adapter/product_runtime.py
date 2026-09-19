from __future__ import annotations

import time
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
from .browser_native_client import (
    CanonicalTopicStreamNormalizer,
    _canonical_intermediate_events,
    _canonical_stream_answer_seed,
    _canonical_stream_identity,
    _is_stream_health_event,
    _make_passive_terminal_stop_check,
)
from .client import DEFAULT_TIMEOUT_SECONDS, ChatGPTWebClient
from .exceptions import RequestError
from .messages import _current_branch_nodes, get_messages
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
from .status import get_status
from .types import (
    ChatMessage,
    ChatResponse,
    ConversationRef,
    ConversationStatus,
    MediaItem,
)

ProductConversationModeUnavailableError = _core.ProductConversationModeUnavailableError
ProductRichInputUnavailableError = _core.ProductRichInputUnavailableError

_FOLLOW_UNFINISHED_STATUSES = {
    "running",
    "streaming",
    "tool_running",
    "tool_calling",
    "user_last_message",
}
_TERMINAL_STREAM_STATUSES = {"COMPLETE", "IS_STOP_REQUESTED"}

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

    def list_recent_conversations(self, *, limit: int = 100) -> list[dict[str, Any]]:
        helper = getattr(self.canonical, "list_recent_conversations", None)
        if callable(helper):
            return [dict(item) for item in helper(limit=limit)]
        return self.list_conversations()[:limit]

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

    def _full_resume_messages(
        self,
        reader: Any,
        ref: ConversationRef,
        payload: dict[str, Any],
    ) -> list[Any]:
        messages = get_messages(
            reader,
            ref,
            limit=None,
            include_empty=True,
        )
        mapping = payload.get("mapping")
        mapping = mapping if isinstance(mapping, dict) else {}
        attachment_reader = getattr(self.canonical, "read_text_attachment", None)

        for message in messages:
            if getattr(message, "role", None) != "user":
                continue
            node_id = getattr(message, "node_id", None)
            node = mapping.get(node_id) if isinstance(node_id, str) else None
            raw_message = node.get("message") if isinstance(node, dict) else None
            metadata = (
                raw_message.get("metadata")
                if isinstance(raw_message, dict)
                and isinstance(raw_message.get("metadata"), dict)
                else {}
            )
            attachments = metadata.get("attachments")
            if not isinstance(attachments, list) or not attachments:
                continue

            blocks: list[str] = []
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                name = str(attachment.get("name") or "attachment").strip()
                mime_type = str(
                    attachment.get("mime_type") or "application/octet-stream"
                ).strip()
                size = attachment.get("size")
                details = [name, mime_type]
                if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                    details.append(f"{size} bytes")
                header = f"[attachment: {' · '.join(details)}]"

                attachment_text: str | None = None
                if callable(attachment_reader):
                    try:
                        attachment_text = attachment_reader(attachment)
                    except Exception:  # noqa: BLE001 - resume history must degrade to metadata.
                        attachment_text = None
                if attachment_text:
                    blocks.append(f"{header}\n{attachment_text}")
                else:
                    blocks.append(header)

            if not blocks:
                continue
            current_text = str(getattr(message, "text", "") or "").strip()
            attachment_text = "\n\n".join(blocks)
            message.text = (
                f"{current_text}\n\n{attachment_text}"
                if current_text
                else attachment_text
            )

        return [
            message
            for message in messages
            if str(getattr(message, "text", "") or "").strip()
        ]

    def _follow_snapshot_from_payload(
        self,
        ref: ConversationRef,
        payload: dict[str, Any],
        *,
        emitted_message_ids: Sequence[str],
        limit: int | None,
        canonical_cache_age_seconds: float | None = None,
        active_stream: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        class _PayloadReader:
            def _get_conversation_payload(
                self, _conversation_id: str
            ) -> dict[str, Any]:
                return payload

        reader = _PayloadReader()
        emitted = {
            str(message_id).strip()
            for message_id in emitted_message_ids
            if str(message_id).strip()
        }
        events = _canonical_intermediate_events(
            payload,
            baseline_message_ids=frozenset(),
            emitted_message_ids=emitted,
            submission_id=None,
        )
        stream_topic_id, turn_exchange_id = _canonical_stream_identity(payload)
        registry_overrode_stream = False
        if active_stream is not None:
            active_topic = active_stream.get("topic_id")
            if isinstance(active_topic, str) and active_topic.strip():
                normalized_active_topic = active_topic.strip()
                registry_overrode_stream = normalized_active_topic != stream_topic_id
                stream_topic_id = normalized_active_topic
            if active_stream.get("pending_topic") is True:
                turn_exchange_id = None
            else:
                active_turn_exchange = active_stream.get("turn_exchange_id")
                if (
                    isinstance(active_turn_exchange, str)
                    and active_turn_exchange.strip()
                ):
                    turn_exchange_id = active_turn_exchange.strip()

        current_turn_event_ids = sorted(
            str(event.get("message_id")).strip()
            for event in events
            if isinstance(event.get("message_id"), str)
            and str(event.get("message_id")).strip()
            and turn_exchange_id is not None
            and event.get("turn_exchange_id") == turn_exchange_id
        )
        answer_message_id, answer_text = _canonical_stream_answer_seed(
            payload,
            turn_exchange_id=turn_exchange_id,
        )
        if registry_overrode_stream:
            answer_message_id = None
            answer_text = ""
        status = get_status(reader, ref)
        if active_stream is not None:
            status.status = "running"
            status.finish_reason = None
            status.pending_approval = False
        messages = (
            self._full_resume_messages(reader, ref, payload)
            if limit is None
            else get_messages(reader, ref, limit=limit)
        )
        return {
            "status": status,
            "messages": messages,
            "events": events,
            "emitted_message_ids": sorted(emitted),
            "current_turn_event_ids": current_turn_event_ids,
            "stream_topic_id": stream_topic_id,
            "turn_exchange_id": turn_exchange_id,
            "stream_answer_message_id": answer_message_id,
            "stream_answer_text": answer_text,
            "canonical_cache_stale": canonical_cache_age_seconds is not None,
            "canonical_cache_age_seconds": canonical_cache_age_seconds,
            "active_stream_registry": active_stream is not None,
        }

    def _verify_initial_follow_terminal_status(
        self,
        ref: ConversationRef,
        snapshot: dict[str, Any],
        *,
        emitted_message_ids: Sequence[str],
        limit: int | None,
        probe_timeout: float,
    ) -> dict[str, Any]:
        status = snapshot.get("status")
        canonical_status = getattr(status, "status", None)
        if canonical_status not in _FOLLOW_UNFINISHED_STATUSES:
            return snapshot
        # A live local registry is stronger evidence of a newer in-flight turn
        # than the conversation-scoped stream_status endpoint. Never let a stale
        # COMPLETE from the previous turn suppress an actively registered turn.
        if snapshot.get("active_stream_registry") is True:
            snapshot["backend_stream_status_checked"] = False
            snapshot["backend_stream_status_skip_reason"] = "active_stream_registry"
            return snapshot

        provider = getattr(self.write_transport, "provider", None)
        probe = getattr(provider, "probe_stream_status", None)
        if not callable(probe):
            return snapshot
        try:
            backend_status = probe(
                ref.conversation_id,
                turn_trace_id=(
                    snapshot.get("turn_exchange_id")
                    if isinstance(snapshot.get("turn_exchange_id"), str)
                    else None
                ),
                timeout=max(0.1, float(probe_timeout)),
            )
        except Exception:
            # Initial resume verification is advisory. A failed independent probe
            # must never make an otherwise readable canonical conversation fail.
            backend_status = None
        if not isinstance(backend_status, str) or not backend_status.strip():
            return snapshot

        normalized_backend_status = backend_status.strip().upper()
        snapshot["backend_stream_status"] = normalized_backend_status
        snapshot["backend_stream_status_checked"] = True
        if normalized_backend_status not in _TERMINAL_STREAM_STATUSES:
            return snapshot

        topic_id = snapshot.get("stream_topic_id")
        shared_final_reader = getattr(provider, "wait_for_shared_final_payload", None)
        if (
            callable(shared_final_reader)
            and isinstance(topic_id, str)
            and topic_id.strip()
            and not topic_id.startswith("cwa-local-pending:")
        ):
            try:
                candidate = shared_final_reader(
                    ref.conversation_id,
                    topic_id=topic_id.strip(),
                    timeout=0.0,
                )
            except Exception:
                candidate = None
            if isinstance(candidate, dict):
                recovered = self._follow_snapshot_from_payload(
                    ref,
                    candidate,
                    emitted_message_ids=emitted_message_ids,
                    limit=limit,
                )
                recovered["backend_stream_status"] = normalized_backend_status
                recovered["backend_stream_status_checked"] = True
                recovered["backend_terminal_status_proven"] = True
                recovered["canonical_status_overridden"] = True
                recovered["canonical_status_before_override"] = canonical_status
                recovered["canonical_terminal_text_missing"] = False
                recovered["shared_final_cache"] = True
                return recovered

        status.status = "completed"
        status.finish_reason = status.finish_reason or "stop"
        status.pending_approval = False
        snapshot["backend_terminal_status_proven"] = True
        snapshot["canonical_status_overridden"] = True
        snapshot["canonical_status_before_override"] = canonical_status
        snapshot["canonical_terminal_text_missing"] = not bool(
            str(snapshot.get("stream_answer_text") or "").strip()
        )
        snapshot["active_stream_registry"] = False
        return snapshot

    @staticmethod
    def _follow_snapshot_from_stream_terminal(
        normalizer: CanonicalTopicStreamNormalizer,
        *,
        topic_id: str,
        follow_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        result = follow_result if isinstance(follow_result, dict) else {}
        message_id = normalizer.answer_message_id
        if not message_id:
            candidate = result.get("message_id")
            if isinstance(candidate, str) and candidate.strip():
                message_id = candidate.strip()
        finish_reason = result.get("finish_reason")
        if not isinstance(finish_reason, str) or not finish_reason.strip():
            finish_reason = "stop"
        else:
            finish_reason = finish_reason.strip()
        model = result.get("observed_model")
        if not isinstance(model, str) or not model.strip():
            model = None
        else:
            model = model.strip()
        turn_exchange_id = result.get("turn_exchange_id")
        if not isinstance(turn_exchange_id, str) or not turn_exchange_id.strip():
            turn_exchange_id = None
        else:
            turn_exchange_id = turn_exchange_id.strip()

        status = ConversationStatus(
            status="completed",
            node_id=message_id,
            message_id=message_id,
            role="assistant",
            recipient="all",
            finish_reason=finish_reason,
            metadata_preview={
                "finish_details": {"type": finish_reason},
                "model_slug": model,
                "turn_exchange_id": turn_exchange_id,
            },
        )
        messages = []
        if message_id:
            messages.append(
                ChatMessage(
                    node_id=message_id,
                    message_id=message_id,
                    role="assistant",
                    text=normalizer.answer_text,
                    recipient="all",
                    model=model,
                    finish_reason=finish_reason,
                    metadata_preview={
                        "finish_details": {"type": finish_reason},
                        "model_slug": model,
                        "turn_exchange_id": turn_exchange_id,
                    },
                )
            )
        return {
            "status": status,
            "messages": messages,
            "events": [],
            "emitted_message_ids": sorted(normalizer.emitted_message_ids),
            "current_turn_event_ids": [],
            "stream_topic_id": topic_id,
            "turn_exchange_id": turn_exchange_id,
            "stream_answer_message_id": message_id,
            "stream_answer_text": normalizer.answer_text,
            "canonical_cache_stale": False,
            "canonical_cache_age_seconds": None,
            "active_stream_registry": False,
            "stream_completed": True,
            "shared_final_cache": False,
            "stream_terminal_snapshot": True,
        }

    def conversation_follow_snapshot(
        self,
        conversation: Any,
        *,
        emitted_message_ids: Sequence[str] = (),
        limit: int | None = 128,
        verify_terminal_status: bool = False,
        terminal_probe_timeout: float = 3.0,
    ) -> dict[str, Any]:
        ref = ConversationRef.from_any(conversation)
        canonical_cache_age_seconds: float | None = None
        provider = getattr(self.write_transport, "provider", None)
        active_stream: dict[str, Any] | None = None
        active_stream_reader = getattr(provider, "active_stream_info", None)
        if callable(active_stream_reader):
            candidate = active_stream_reader(ref.conversation_id)
            if isinstance(candidate, dict):
                active_stream = candidate
        if active_stream is not None and not active_stream.get("topic_id"):
            pending_topic_factory = getattr(provider, "pending_stream_topic_id", None)
            if callable(pending_topic_factory):
                pending_topic = pending_topic_factory(
                    ref.conversation_id,
                    active_stream,
                )
                if isinstance(pending_topic, str) and pending_topic.strip():
                    active_stream = {
                        **active_stream,
                        "topic_id": pending_topic.strip(),
                        "pending_topic": True,
                    }

        cache_reader = getattr(
            self.canonical,
            "read_cached_conversation_payload",
            None,
        )
        cached = (
            cache_reader(ref.conversation_id)
            if active_stream is not None and callable(cache_reader)
            else None
        )
        use_cached_active_snapshot = (
            active_stream is not None
            and isinstance(cached, tuple)
            and len(cached) == 2
            and isinstance(cached[0], dict)
        )
        if use_cached_active_snapshot:
            payload = cached[0]
            age_value = cached[1]
            canonical_cache_age_seconds = (
                max(0.0, float(age_value))
                if isinstance(age_value, (int, float))
                and not isinstance(age_value, bool)
                else None
            )
        else:
            try:
                payload = self.get_conversation_payload(ref)
            except RequestError as error:
                if error.status_code != 429:
                    raise
                if cached is None and callable(cache_reader):
                    cached = cache_reader(ref.conversation_id)
                if not (
                    isinstance(cached, tuple)
                    and len(cached) == 2
                    and isinstance(cached[0], dict)
                ):
                    raise
                payload = cached[0]
                age_value = cached[1]
                canonical_cache_age_seconds = (
                    max(0.0, float(age_value))
                    if isinstance(age_value, (int, float))
                    and not isinstance(age_value, bool)
                    else None
                )

        snapshot = self._follow_snapshot_from_payload(
            ref,
            payload,
            emitted_message_ids=emitted_message_ids,
            limit=limit,
            canonical_cache_age_seconds=canonical_cache_age_seconds,
            active_stream=active_stream,
        )
        if verify_terminal_status:
            snapshot = self._verify_initial_follow_terminal_status(
                ref,
                snapshot,
                emitted_message_ids=emitted_message_ids,
                limit=limit,
                probe_timeout=terminal_probe_timeout,
            )
        return snapshot

    @staticmethod
    def _relay_pending_canonical_payload(
        payload: dict[str, Any],
        *,
        baseline_current_node: str | None,
        normalizer: CanonicalTopicStreamNormalizer,
        on_event: Any = None,
    ) -> bool:
        if (
            not isinstance(baseline_current_node, str)
            or not baseline_current_node.strip()
        ):
            return False
        branch = _current_branch_nodes(payload)
        baseline_index: int | None = None
        for index, (node_id, _node) in enumerate(branch):
            if node_id == baseline_current_node:
                baseline_index = index
                break
        if baseline_index is None or baseline_index >= len(branch) - 1:
            return False
        advanced = False
        normalized_emitted = False
        first_new_message: dict[str, Any] | None = None
        for _node_id, node in branch[baseline_index + 1 :]:
            message = node.get("message")
            if not isinstance(message, dict):
                continue
            if first_new_message is None:
                first_new_message = message
            advanced = True
            transport_event = {
                "type": "raw_ws_event",
                "parsed": {"v": {"message": message}},
            }
            for normalized in normalizer.feed_transport_event(transport_event):
                normalized_emitted = True
                if on_event is not None:
                    on_event(normalized)
        if advanced and not normalized_emitted and isinstance(first_new_message, dict):
            author = first_new_message.get("author")
            role = author.get("role") if isinstance(author, dict) else None
            message_id = first_new_message.get("id")
            if (
                role == "user"
                and isinstance(message_id, str)
                and message_id.strip()
                and message_id not in normalizer.emitted_message_ids
            ):
                normalized_id = message_id.strip()
                normalizer.emitted_message_ids.add(normalized_id)
                if on_event is not None:
                    on_event(
                        {
                            "type": "canonical_intermediate_message",
                            "message_id": normalized_id,
                            "message_kind": "activity",
                            "text": "",
                            "label": "Request accepted",
                            "tool_name": None,
                        }
                    )
        return advanced

    def conversation_follow_stream(
        self,
        conversation: Any,
        *,
        topic_id: str,
        emitted_message_ids: Sequence[str] = (),
        answer_message_id: str | None = None,
        answer_text: str = "",
        timeout: float = 2 * 60 * 60,
        limit: int | None = 128,
        on_event: Any = None,
        should_stop: Any = None,
    ) -> dict[str, Any]:
        ref = ConversationRef.from_any(conversation)
        provider = getattr(self.write_transport, "provider", None)
        helper = getattr(provider, "follow_stream_topic", None)
        if not callable(helper):
            raise RuntimeError(
                "live topic follow is unavailable on the selected browser authority provider"
            )
        normalizer = CanonicalTopicStreamNormalizer(
            emitted_message_ids=emitted_message_ids,
            answer_message_id=answer_message_id,
            answer_text=answer_text,
        )

        def relay_transport_event(event: dict[str, Any]) -> None:
            if _is_stream_health_event(event) and on_event is not None:
                on_event(event)
            for normalized in normalizer.feed_transport_event(event):
                if on_event is not None:
                    on_event(normalized)

        stream_should_stop = _make_passive_terminal_stop_check(
            lambda: normalizer.turn_completed,
            cancelled=should_stop,
            settled=lambda: normalizer.segment_kind is None,
        )

        actual_topic_id = topic_id
        local_final_payload: dict[str, Any] | None = None
        if topic_id.startswith("cwa-local-pending:"):
            active_stream_reader = getattr(provider, "active_stream_info", None)
            cache_reader = getattr(
                self.canonical, "read_cached_conversation_payload", None
            )
            final_predicate = getattr(provider, "canonical_payload_is_final", None)
            deadline = time.monotonic() + max(0.0, float(timeout))
            baseline_current_node: str | None = None
            while True:
                cancelled = False
                if should_stop is not None:
                    try:
                        cancelled = bool(should_stop())
                    except Exception:
                        cancelled = False
                if cancelled:
                    return {
                        "stream_completed": False,
                        "stream_cancelled": True,
                        "stream_topic_id": actual_topic_id,
                        "emitted_message_ids": sorted(normalizer.emitted_message_ids),
                    }

                active_stream = (
                    active_stream_reader(ref.conversation_id)
                    if callable(active_stream_reader)
                    else None
                )
                if isinstance(active_stream, dict):
                    active_baseline = active_stream.get("baseline_current_node")
                    if (
                        baseline_current_node is None
                        and isinstance(active_baseline, str)
                        and active_baseline.strip()
                    ):
                        baseline_current_node = active_baseline.strip()
                    active_topic = active_stream.get("topic_id")
                    if isinstance(active_topic, str) and active_topic.strip():
                        actual_topic_id = active_topic.strip()
                        break

                cached = (
                    cache_reader(ref.conversation_id)
                    if callable(cache_reader)
                    else None
                )
                if (
                    isinstance(cached, tuple)
                    and len(cached) == 2
                    and isinstance(cached[0], dict)
                ):
                    cached_payload = cached[0]
                    advanced = self._relay_pending_canonical_payload(
                        cached_payload,
                        baseline_current_node=baseline_current_node,
                        normalizer=normalizer,
                        on_event=on_event,
                    )
                    if (
                        advanced
                        and callable(final_predicate)
                        and bool(final_predicate(cached_payload))
                    ):
                        local_final_payload = cached_payload
                        break

                if active_stream is None or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)

            if local_final_payload is not None:
                recovered_snapshot = self._follow_snapshot_from_payload(
                    ref,
                    local_final_payload,
                    emitted_message_ids=tuple(normalizer.emitted_message_ids),
                    limit=limit,
                )
                recovered_snapshot["stream_completed"] = True
                recovered_snapshot["stream_topic_id"] = actual_topic_id
                recovered_snapshot["shared_final_cache"] = True
                recovered_snapshot["stream_recovered_from_local_cache"] = True
                return recovered_snapshot

            if actual_topic_id.startswith("cwa-local-pending:"):
                return {
                    "stream_completed": False,
                    "stream_cancelled": False,
                    "stream_topic_id": actual_topic_id,
                    "emitted_message_ids": sorted(normalizer.emitted_message_ids),
                }

        try:
            follow_result = helper(
                conversation_id=ref.conversation_id,
                topic_id=actual_topic_id,
                timeout=timeout,
                on_event=relay_transport_event,
                should_stop=stream_should_stop,
            )
        except Exception:
            active_stream_reader = getattr(provider, "active_stream_info", None)
            active_stream = (
                active_stream_reader(ref.conversation_id)
                if callable(active_stream_reader)
                else None
            )
            if isinstance(active_stream, dict):
                active_topic = active_stream.get("topic_id")
                if isinstance(active_topic, str) and active_topic.strip():
                    actual_topic_id = active_topic.strip()
            shared_final_reader = getattr(
                provider, "wait_for_shared_final_payload", None
            )
            if (
                callable(shared_final_reader)
                and isinstance(actual_topic_id, str)
                and actual_topic_id
                and not actual_topic_id.startswith("cwa-local-pending:")
            ):
                candidate = shared_final_reader(
                    ref.conversation_id,
                    topic_id=actual_topic_id,
                    timeout=timeout,
                    should_stop=should_stop,
                )
                if isinstance(candidate, dict):
                    recovered_snapshot = self._follow_snapshot_from_payload(
                        ref,
                        candidate,
                        emitted_message_ids=tuple(normalizer.emitted_message_ids),
                        limit=limit,
                    )
                    recovered_snapshot["stream_completed"] = True
                    recovered_snapshot["stream_topic_id"] = actual_topic_id
                    recovered_snapshot["shared_final_cache"] = True
                    recovered_snapshot["stream_recovered_from_local_final"] = True
                    return recovered_snapshot
            raise
        if isinstance(follow_result, dict):
            result_topic_id = follow_result.get("topic_id")
            if isinstance(result_topic_id, str) and result_topic_id.strip():
                actual_topic_id = result_topic_id.strip()
        external_completion_observed = bool(
            isinstance(follow_result, dict)
            and follow_result.get("external_completion_observed") is True
        )
        terminal_stream_status = (
            follow_result.get("terminal_stream_status")
            if isinstance(follow_result, dict)
            else None
        )
        completed = normalizer.turn_completed or external_completion_observed
        if not completed:
            cancelled = False
            if should_stop is not None:
                try:
                    cancelled = bool(should_stop())
                except Exception:
                    cancelled = False
            return {
                "stream_completed": False,
                "stream_cancelled": cancelled,
                "stream_topic_id": actual_topic_id,
                "emitted_message_ids": sorted(normalizer.emitted_message_ids),
            }

        shared_final_payload: dict[str, Any] | None = None
        shared_final_reader = getattr(provider, "wait_for_shared_final_payload", None)
        if callable(shared_final_reader):
            candidate = shared_final_reader(
                ref.conversation_id,
                topic_id=actual_topic_id,
                timeout=0.0,
            )
            if isinstance(candidate, dict):
                shared_final_payload = candidate

        if shared_final_payload is not None:
            final_snapshot = self._follow_snapshot_from_payload(
                ref,
                shared_final_payload,
                emitted_message_ids=tuple(normalizer.emitted_message_ids),
                limit=limit,
            )
            final_snapshot["shared_final_cache"] = True
            final_snapshot["stream_completed"] = True
            final_snapshot["stream_topic_id"] = actual_topic_id
            return final_snapshot

        if terminal_stream_status in {"IS_STOP_REQUESTED", "COMPLETE"}:
            terminal_snapshot = self._follow_snapshot_from_stream_terminal(
                normalizer,
                topic_id=actual_topic_id,
                follow_result=follow_result if isinstance(follow_result, dict) else None,
            )
            terminal_snapshot["stream_terminal_status"] = terminal_stream_status
            terminal_snapshot["stream_terminal_status_proven"] = True
            return terminal_snapshot

        # A passive topic terminal proves lifecycle completion, but not that every
        # text patch was observed or reconstructed losslessly. This matters most
        # when attaching to a turn that was already in flight before gptty joined.
        # Reconcile exactly once against canonical state at terminal; never poll.
        try:
            terminal_payload = self.get_conversation_payload(ref)
        except (RequestError, RuntimeError, OSError, ValueError):
            terminal_payload = None
        if isinstance(terminal_payload, dict):
            terminal_snapshot = self._follow_snapshot_from_payload(
                ref,
                terminal_payload,
                emitted_message_ids=tuple(normalizer.emitted_message_ids),
                limit=limit,
            )
            terminal_status = terminal_snapshot.get("status")
            if getattr(terminal_status, "status", None) == "completed":
                terminal_snapshot["shared_final_cache"] = False
                terminal_snapshot["stream_completed"] = True
                terminal_snapshot["stream_topic_id"] = actual_topic_id
                terminal_snapshot["stream_terminal_reconciled"] = True
                terminal_snapshot["stream_terminal_provisional_text"] = (
                    normalizer.answer_text
                )
                return terminal_snapshot

        return self._follow_snapshot_from_stream_terminal(
            normalizer,
            topic_id=actual_topic_id,
            follow_result=follow_result if isinstance(follow_result, dict) else None,
        )

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
