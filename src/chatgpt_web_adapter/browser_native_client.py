from __future__ import annotations

import inspect
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .browser_native_provider import BrowserNativeTurnProvider
from .canonical_product_observation_gate_pr9_3 import (
    _gate_send_browser_native,
    _gate_wait_for_new_final_assistant,
)
from .exceptions import ConversationTimeoutError, RequestError
from .message_text import extract_message_text
from .messages import _chat_message_from_node, _current_branch_nodes
from .product_media import current_browser_owned_attachment_paths
from .revision_safe_streaming_pr8_9 import (
    ASSISTANT_TEXT_DELTA,
    ASSISTANT_TEXT_REVISION,
    ASSISTANT_TEXT_SNAPSHOT,
    RevisionSafeTextAccumulator,
)
from .status import _status_from_payload
from .types import (
    AttachedConversation,
    ChatConversation,
    ChatMessage,
    ChatMetrics,
    ChatRequestDiagnostics,
    ChatResponse,
    ConversationRef,
)


@dataclass
class BrowserNativeSubmission:
    """Internal accepted-write state awaiting canonical finality."""

    submission_id: str
    turn: Any
    baseline_assistant_ids: frozenset[str]
    baseline_message_ids: frozenset[str]
    timeout: float
    poll_interval: float
    started_monotonic: float
    accepted_at_ms: int
    is_continuation: bool
    attachment_count: int
    stream_state: RevisionSafeTextAccumulator
    on_token: Callable[[str], None] | None
    on_event: Callable[[dict[str, Any]], None] | None
    final_response: ChatResponse | None = None


_CANONICAL_LIVE_POLL_INTERVAL_SECONDS = 15.0
_ACTIVE_SEND_CANONICAL_POLL_INTERVAL_SECONDS = 2.0
_CANONICAL_RATE_LIMIT_BACKOFF_SECONDS = 15.0
_PASSIVE_FINAL_RECONCILE_RETRY_SECONDS = 5.0
_PASSIVE_FINAL_RECONCILE_SETTLE_SECONDS = 4.0
_PASSIVE_STREAM_ENDED_RECONCILE_SECONDS = 5.0
_PREWRITE_CANONICAL_COMPLETION_MAX_AGE_MS = 5_000
_CANONICAL_INTERMEDIATE_MAX_TEXT_CHARS = 6_000
_SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|cookie|set[-_]?cookie|access[-_]?token|refresh[-_]?token|"
    r"id[-_]?token|api[-_]?key|password|passwd|secret|session[-_]?token|csrf)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_SENSITIVE_LINE_RE = re.compile(
    r"(?im)^(\s*(?:authorization|cookie|set[-_]?cookie|access[-_]?token|"
    r"refresh[-_]?token|id[-_]?token|api[-_]?key|password|passwd|secret|"
    r"session[-_]?token|csrf)\s*[:=]\s*).+$"
)


def set_browser_native_turn_provider(
    self: Any, provider: BrowserNativeTurnProvider | None
) -> None:
    if provider is not None and not callable(getattr(provider, "send_text", None)):
        raise TypeError("provider must expose a callable send_text() or be None")
    self._browser_native_turn_provider = provider


def _assistant_message_ids(self: Any, conversation: Any) -> set[str]:
    messages = self.get_messages(
        conversation,
        limit=None,
        roles={"assistant"},
        include_empty=True,
    )
    return {
        message.message_id
        for message in messages
        if isinstance(getattr(message, "message_id", None), str)
    }


def _canonical_status_value(self: Any, conversation: Any) -> str | None:
    try:
        status = self.get_status(conversation)
    except Exception:
        return None
    value = getattr(status, "status", None)
    return value if isinstance(value, str) else None


def _canonical_prewrite_snapshot(
    self: Any,
    conversation: Any,
    *,
    canonical_payload: dict[str, Any] | None = None,
) -> tuple[set[str], set[str], str | None]:
    """Resolve continuation baseline IDs/status, reusing a caller-owned commit snapshot."""

    payload = canonical_payload
    if payload is None:
        canonical_reader = getattr(self, "_get_conversation_payload", None)
        if callable(canonical_reader):
            ref = ConversationRef.from_any(conversation)
            candidate = canonical_reader(ref.conversation_id)
            if isinstance(candidate, dict):
                payload = candidate
    if isinstance(payload, dict):
        message_ids: set[str] = set()
        assistant_ids: set[str] = set()
        for node_id, node in _current_branch_nodes(payload):
            message = _chat_message_from_node(node_id, node)
            if message is None:
                continue
            message_id = getattr(message, "message_id", None)
            if not isinstance(message_id, str):
                continue
            message_ids.add(message_id)
            if getattr(message, "role", None) == "assistant":
                assistant_ids.add(message_id)
        status = _status_from_payload(payload)
        status_value = getattr(status, "status", None)
        return (
            message_ids,
            assistant_ids,
            status_value if isinstance(status_value, str) else None,
        )

    messages = self.get_messages(
        conversation,
        limit=None,
        roles=None,
        include_empty=True,
    )
    message_ids = {
        message.message_id
        for message in messages
        if isinstance(getattr(message, "message_id", None), str)
    }
    assistant_ids = {
        message.message_id
        for message in messages
        if getattr(message, "role", None) == "assistant"
        and isinstance(getattr(message, "message_id", None), str)
    }
    return message_ids, assistant_ids, _canonical_status_value(self, conversation)


def _status_finalizes_message(status: Any, message_id: str) -> bool:
    if status is None or not isinstance(message_id, str) or not message_id:
        return False
    return (
        getattr(status, "status", None) == "completed"
        and getattr(status, "message_id", None) == message_id
    )


def _node_turn_exchange_id(node: dict[str, Any]) -> str | None:
    message = node.get("message")
    if not isinstance(message, dict):
        return None
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        return None
    for key in ("turn_exchange_id", "working_turn_id"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _redact_intermediate_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 128:
                redacted["..."] = "[TRUNCATED]"
                break
            rendered_key = str(key)
            if _SENSITIVE_KEY_RE.search(rendered_key):
                redacted[rendered_key] = "[REDACTED]"
            else:
                redacted[rendered_key] = _redact_intermediate_value(
                    item, depth=depth + 1
                )
        return redacted
    if isinstance(value, list):
        items = [
            _redact_intermediate_value(item, depth=depth + 1) for item in value[:128]
        ]
        if len(value) > 128:
            items.append("[TRUNCATED]")
        return items
    if isinstance(value, str):
        return _redact_intermediate_string(value)
    return value


def _redact_intermediate_string(value: str) -> str:
    text = _BEARER_RE.sub("Bearer [REDACTED]", value)
    return _SENSITIVE_LINE_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", text)


def _sanitize_intermediate_text(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if len(text) <= 200_000 and text[:1] in {"{", "["}:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            pass
        else:
            text = json.dumps(
                _redact_intermediate_value(parsed),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
    text = _redact_intermediate_string(text)
    if len(text) > _CANONICAL_INTERMEDIATE_MAX_TEXT_CHARS:
        text = text[:_CANONICAL_INTERMEDIATE_MAX_TEXT_CHARS].rstrip() + "\n…[truncated]"
    return text


def _tool_call_label(
    raw_message: dict[str, Any], metadata: dict[str, Any], recipient: str
) -> str | None:
    explicit = metadata.get("tool_invoking_message")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    raw = extract_message_text(raw_message).strip()
    if not raw or len(raw) > 200_000 or raw[:1] not in {"{", "["}:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    if recipient == "api_tool.list_resources":
        query = payload.get("query")
        if isinstance(query, str) and query.strip():
            return f"Discovering {query.strip()}..."
        paths = payload.get("paths")
        if isinstance(paths, list) and paths and isinstance(paths[0], str):
            return f"Discovering {paths[0]} tools..."
        return "Discovering tools..."

    if recipient != "api_tool.call_tool":
        return None

    resource_path = payload.get("path")
    action = None
    if isinstance(resource_path, str) and resource_path.strip():
        action = resource_path.rstrip("/").rsplit("/", 1)[-1].strip() or None
    args = payload.get("args")
    if not isinstance(args, dict):
        args = {}

    if action == "git_status":
        return "Reading git status..."
    if action == "show_changes":
        return "Reviewing changes..."
    if action == "open_workspace":
        return "Opening workspace..."
    if action == "read":
        path = args.get("path")
        if isinstance(path, str) and path.strip():
            return f"Reading {path.strip()}..."
        return "Reading file..."
    if action == "tree":
        path = args.get("path")
        if isinstance(path, str) and path.strip():
            return f"Reading tree {path.strip()}..."
        return "Reading tree..."
    if action == "search":
        query = args.get("query")
        if isinstance(query, str) and query.strip():
            return f"Searching {query.strip()}..."
        return "Searching workspace..."
    if action == "bash":
        return "Running command..."
    if action:
        return f"Calling {action.replace('_', ' ')}..."
    return None


def _canonical_intermediate_events(
    payload: dict[str, Any],
    *,
    baseline_message_ids: set[str] | frozenset[str],
    emitted_message_ids: set[str],
    submission_id: str | None,
) -> list[dict[str, Any]]:
    current_node = payload.get("current_node")
    events: list[dict[str, Any]] = []
    for node_id, node in _current_branch_nodes(payload):
        raw_message = node.get("message")
        if not isinstance(raw_message, dict):
            continue
        metadata = raw_message.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        if metadata.get("is_visually_hidden_from_conversation") is True:
            continue
        message_id = raw_message.get("id")
        if not isinstance(message_id, str) or not message_id.strip():
            message_id = node_id
        if message_id in baseline_message_ids or message_id in emitted_message_ids:
            continue

        author = raw_message.get("author")
        if not isinstance(author, dict):
            author = {}
        role = author.get("role")
        recipient = raw_message.get("recipient")
        recipient = recipient.strip() if isinstance(recipient, str) else "all"
        content = raw_message.get("content")
        if not isinstance(content, dict):
            content = {}
        content_type = content.get("content_type")
        text = ""
        kind: str | None = None
        label: str | None = None
        tool_name: str | None = None

        if role == "assistant" and recipient not in {"", "all"}:
            kind = "tool_call"
            tool_name = recipient
            label = _tool_call_label(raw_message, metadata, recipient)
            text = _sanitize_intermediate_text(extract_message_text(raw_message))
        elif role == "tool":
            kind = "tool_result"
            raw_name = author.get("name")
            tool_name = (
                raw_name.strip()
                if isinstance(raw_name, str) and raw_name.strip()
                else recipient
            )
            label = metadata.get("tool_invoked_message")
            text = _sanitize_intermediate_text(extract_message_text(raw_message))
        elif role == "assistant" and content_type == "reasoning_recap":
            kind = "reasoning"
            label = metadata.get("reasoning_title") or "Reasoning summary"
            text = _sanitize_intermediate_text(extract_message_text(raw_message))
        elif role == "assistant" and content_type == "thoughts":
            text = _sanitize_intermediate_text(extract_message_text(raw_message))
            reasoning_title = metadata.get("reasoning_title")
            if isinstance(reasoning_title, str) and reasoning_title.strip():
                label = reasoning_title.strip()
            if text or label:
                kind = "reasoning"
        elif (
            role == "assistant"
            and recipient in {"", "all"}
            and metadata.get("is_thinking_preamble_message") is True
        ):
            kind = "assistant_progress"
            text = _sanitize_intermediate_text(extract_message_text(raw_message))
        elif content_type == "tether_browsing_display":
            kind = "activity"
            label = "Browsing update"
            text = _sanitize_intermediate_text(extract_message_text(raw_message))

        if kind is None:
            continue

        # User-visible thinking/preamble text is revision-prone while it remains
        # the conversation current_node. Never freeze a partial first snapshot
        # such as "Первый". Tool calls can be shown immediately, but thinking text
        # is emitted only after ChatGPT advances to the next canonical node.
        revision_sensitive = kind in {"assistant_progress", "reasoning"} and bool(text)
        if (
            revision_sensitive
            and node_id == current_node
            and not _stream_message_completed(raw_message)
        ):
            continue

        emitted_message_ids.add(message_id)
        event = {
            "type": "canonical_intermediate_message",
            "message_id": message_id,
            "message_kind": kind,
            "text": text,
            "label": label.strip()
            if isinstance(label, str) and label.strip()
            else None,
            "tool_name": tool_name.strip()
            if isinstance(tool_name, str) and tool_name.strip()
            else None,
        }
        if submission_id is not None:
            event["submission_id"] = submission_id
        events.append(event)
    return events


def _canonical_stream_identity(
    payload: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Return the latest stream topic and turn exchange id from canonical history."""

    for _node_id, node in reversed(_current_branch_nodes(payload)):
        raw_message = node.get("message")
        if not isinstance(raw_message, dict):
            continue
        metadata = raw_message.get("metadata")
        if not isinstance(metadata, dict):
            continue
        stream_topic_id = metadata.get("stream_topic_id")
        turn_exchange_id = None
        for key in ("turn_exchange_id", "working_turn_id"):
            candidate = metadata.get(key)
            if isinstance(candidate, str) and candidate.strip():
                turn_exchange_id = candidate.strip()
                break
        if isinstance(stream_topic_id, str) and stream_topic_id.strip():
            return stream_topic_id.strip(), turn_exchange_id
        if turn_exchange_id is not None:
            return f"conversation-turn-{turn_exchange_id}", turn_exchange_id
    return None, None


def _canonical_stream_answer_seed(
    payload: dict[str, Any],
    *,
    turn_exchange_id: str | None,
) -> tuple[str | None, str]:
    for node_id, node in reversed(_current_branch_nodes(payload)):
        if turn_exchange_id is not None:
            node_turn_id = _node_turn_exchange_id(node)
            if node_turn_id is not None and node_turn_id != turn_exchange_id:
                continue
        raw_message = node.get("message")
        if not isinstance(raw_message, dict):
            continue
        author = raw_message.get("author")
        if not isinstance(author, dict) or author.get("role") != "assistant":
            continue
        recipient = raw_message.get("recipient")
        if isinstance(recipient, str) and recipient.strip() not in {"", "all"}:
            continue
        metadata = raw_message.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        if metadata.get("is_thinking_preamble_message") is True:
            continue
        content = raw_message.get("content")
        if not isinstance(content, dict) or content.get("content_type") != "text":
            continue
        message_id = raw_message.get("id")
        if not isinstance(message_id, str) or not message_id.strip():
            message_id = node_id
        return message_id, extract_message_text(raw_message)
    return None, ""


def _stream_message_completed(message: dict[str, Any]) -> bool:
    if message.get("end_turn") is True:
        return True
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    values = (
        message.get("status"),
        message.get("async_status"),
        metadata.get("message_status"),
        metadata.get("status"),
        metadata.get("async_status"),
    )
    completed = {
        "completed",
        "complete",
        "finished",
        "done",
        "success",
        "succeeded",
        "finished_successfully",
    }
    if any(isinstance(value, str) and value.strip().lower() in completed for value in values):
        return True
    finish_details = metadata.get("finish_details")
    if isinstance(finish_details, dict):
        finish_type = finish_details.get("type")
        if isinstance(finish_type, str) and finish_type.strip():
            return True
    for value in (metadata.get("finish_reason"), message.get("finish_reason")):
        if isinstance(value, str) and value.strip():
            return True
    return False


def _clone_stream_message(message: dict[str, Any]) -> dict[str, Any]:
    cloned = dict(message)
    content = message.get("content")
    if isinstance(content, dict):
        cloned_content = dict(content)
        parts = content.get("parts")
        if isinstance(parts, list):
            cloned_content["parts"] = list(parts)
        cloned["content"] = cloned_content
    metadata = message.get("metadata")
    if isinstance(metadata, dict):
        cloned["metadata"] = dict(metadata)
    author = message.get("author")
    if isinstance(author, dict):
        cloned["author"] = dict(author)
    return cloned


class CanonicalTopicStreamNormalizer:
    """Normalize Celsius topic frames into the same live events gptty already renders."""

    _CHILD_KEYS = ("message", "messages", "data", "result", "payload", "turn", "v", "value")

    def __init__(
        self,
        *,
        emitted_message_ids: Sequence[str] = (),
        answer_message_id: str | None = None,
        answer_text: str = "",
    ) -> None:
        self.emitted_message_ids = {
            str(message_id).strip()
            for message_id in emitted_message_ids
            if str(message_id).strip()
        }
        self.answer_message_id = (
            answer_message_id.strip()
            if isinstance(answer_message_id, str) and answer_message_id.strip()
            else None
        )
        self.answer_text = answer_text if isinstance(answer_text, str) else ""
        self.sequence = 0
        self.current_patch_message: dict[str, Any] | None = None
        self.pending_thinking: dict[str, dict[str, Any]] = {}
        self.catchup_remaining = 0
        self.turn_completed = False

    def feed_transport_event(self, event: Any) -> list[dict[str, Any]]:
        if not isinstance(event, dict):
            return []
        if event.get("type") == "stream_handoff_ws_subscribed":
            catchup_count = event.get("catchup_count")
            self.catchup_remaining = (
                max(0, catchup_count)
                if isinstance(catchup_count, int) and not isinstance(catchup_count, bool)
                else 0
            )
            return []
        if event.get("type") != "raw_ws_event":
            return []
        payload = event.get("parsed")
        if not isinstance(payload, dict):
            return []
        output: list[dict[str, Any]] = []
        self._process_payload(payload, output)
        if self.catchup_remaining > 0:
            self.catchup_remaining -= 1
        return output

    def _message_id(self, message: dict[str, Any]) -> str | None:
        message_id = message.get("id")
        return message_id.strip() if isinstance(message_id, str) and message_id.strip() else None

    def _flush_pending_thinking(
        self,
        output: list[dict[str, Any]],
        *,
        except_id: str | None = None,
    ) -> None:
        for message_id, entry in list(self.pending_thinking.items()):
            if message_id == except_id or message_id in self.emitted_message_ids:
                continue
            text = entry.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            self.emitted_message_ids.add(message_id)
            self.pending_thinking.pop(message_id, None)
            output.append(
                {
                    "type": "canonical_intermediate_message",
                    "message_id": message_id,
                    "message_kind": "assistant_progress",
                    "text": _sanitize_intermediate_text(text),
                    "label": None,
                    "tool_name": None,
                }
            )

    def _emit_answer(
        self,
        message: dict[str, Any],
        output: list[dict[str, Any]],
    ) -> None:
        message_id = self._message_id(message)
        if message_id is None:
            return
        text = extract_message_text(message)
        if message_id != self.answer_message_id:
            self.answer_message_id = message_id
            self.answer_text = ""
        if text == self.answer_text:
            return
        if (
            self.catchup_remaining > 0
            and self.answer_text
            and self.answer_text.startswith(text)
        ):
            return
        self.sequence += 1
        if text.startswith(self.answer_text):
            delta = text[len(self.answer_text) :]
            self.answer_text = text
            if delta:
                output.append(
                    {
                        "type": ASSISTANT_TEXT_DELTA,
                        "message_id": message_id,
                        "sequence": self.sequence,
                        "delta": delta,
                    }
                )
            return
        self.answer_text = text
        output.append(
            {
                "type": ASSISTANT_TEXT_REVISION,
                "message_id": message_id,
                "sequence": self.sequence,
                "text": text,
            }
        )

    def _inspect_message(
        self,
        message: dict[str, Any],
        output: list[dict[str, Any]],
    ) -> None:
        metadata = message.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        if metadata.get("is_visually_hidden_from_conversation") is True:
            return
        message_id = self._message_id(message)
        if message_id is None:
            return
        author = message.get("author")
        if not isinstance(author, dict):
            author = {}
        role = author.get("role")
        recipient = message.get("recipient")
        recipient = recipient.strip() if isinstance(recipient, str) else "all"
        content = message.get("content")
        if not isinstance(content, dict):
            content = {}
        content_type = content.get("content_type")
        if (
            role == "assistant"
            and recipient in {"", "all"}
            and message.get("end_turn") is True
        ):
            self.turn_completed = True

        if (
            role == "assistant"
            and recipient in {"", "all"}
            and metadata.get("is_thinking_preamble_message") is True
        ):
            text = extract_message_text(message)
            if text.strip():
                self.pending_thinking[message_id] = {"text": text}
            return

        if role == "assistant" and recipient in {"", "all"} and content_type == "reasoning_recap":
            self._flush_pending_thinking(output, except_id=message_id)
            text = _sanitize_intermediate_text(extract_message_text(message))
            if text and _stream_message_completed(message) and message_id not in self.emitted_message_ids:
                self.emitted_message_ids.add(message_id)
                output.append(
                    {
                        "type": "canonical_intermediate_message",
                        "message_id": message_id,
                        "message_kind": "reasoning",
                        "text": text,
                        "label": metadata.get("reasoning_title") or "Reasoning summary",
                        "tool_name": None,
                    }
                )
            return

        if role == "assistant" and recipient in {"", "all"} and content_type == "thoughts":
            text = _sanitize_intermediate_text(extract_message_text(message))
            reasoning_title = metadata.get("reasoning_title")
            label = (
                reasoning_title.strip()
                if isinstance(reasoning_title, str) and reasoning_title.strip()
                else None
            )
            if (
                (text or label)
                and _stream_message_completed(message)
                and message_id not in self.emitted_message_ids
            ):
                self.emitted_message_ids.add(message_id)
                output.append(
                    {
                        "type": "canonical_intermediate_message",
                        "message_id": message_id,
                        "message_kind": "reasoning",
                        "text": text,
                        "label": label,
                        "tool_name": None,
                    }
                )
            return

        if role == "assistant" and recipient not in {"", "all"}:
            self._flush_pending_thinking(output)
            if message_id in self.emitted_message_ids:
                return
            label = _tool_call_label(message, metadata, recipient)
            if label is None and not _stream_message_completed(message):
                return
            self.emitted_message_ids.add(message_id)
            output.append(
                {
                    "type": "canonical_intermediate_message",
                    "message_id": message_id,
                    "message_kind": "tool_call",
                    "text": _sanitize_intermediate_text(extract_message_text(message)),
                    "label": label or "Using tool...",
                    "tool_name": recipient,
                }
            )
            return

        if role == "tool":
            self._flush_pending_thinking(output)
            if message_id in self.emitted_message_ids or not _stream_message_completed(message):
                return
            self.emitted_message_ids.add(message_id)
            raw_name = author.get("name")
            tool_name = (
                raw_name.strip()
                if isinstance(raw_name, str) and raw_name.strip()
                else recipient
            )
            output.append(
                {
                    "type": "canonical_intermediate_message",
                    "message_id": message_id,
                    "message_kind": "tool_result",
                    "text": _sanitize_intermediate_text(extract_message_text(message)),
                    "label": metadata.get("tool_invoked_message"),
                    "tool_name": tool_name,
                }
            )
            return

        if content_type == "tether_browsing_display":
            if message_id in self.emitted_message_ids or not _stream_message_completed(message):
                return
            self.emitted_message_ids.add(message_id)
            output.append(
                {
                    "type": "canonical_intermediate_message",
                    "message_id": message_id,
                    "message_kind": "activity",
                    "text": _sanitize_intermediate_text(extract_message_text(message)),
                    "label": "Browsing update",
                    "tool_name": None,
                }
            )
            return

        if (
            role == "assistant"
            and recipient in {"", "all"}
            and content_type == "text"
            and metadata.get("is_thinking_preamble_message") is not True
        ):
            self._flush_pending_thinking(output)
            self._emit_answer(message, output)

    def _collect_messages(
        self,
        value: Any,
        output: list[dict[str, Any]],
        *,
        depth: int = 0,
        seen: set[int] | None = None,
    ) -> None:
        if value is None or depth > 7:
            return
        if seen is None:
            seen = set()
        if isinstance(value, list):
            for item in value[:128]:
                self._collect_messages(item, output, depth=depth + 1, seen=seen)
            return
        if not isinstance(value, dict):
            return
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        if isinstance(value.get("author"), dict) and isinstance(value.get("content"), dict):
            self._inspect_message(value, output)
        for key in self._CHILD_KEYS:
            if key in value:
                self._collect_messages(value[key], output, depth=depth + 1, seen=seen)

    def _select_patch_message(
        self,
        message: dict[str, Any],
        output: list[dict[str, Any]],
    ) -> None:
        self.current_patch_message = _clone_stream_message(message)
        self._inspect_message(self.current_patch_message, output)

    def _apply_patch_item(
        self,
        item: Any,
        output: list[dict[str, Any]],
    ) -> None:
        if not isinstance(item, dict):
            return
        value = item.get("v")
        if isinstance(value, dict) and isinstance(value.get("message"), dict):
            self._select_patch_message(value["message"], output)
            return
        message = self.current_patch_message
        if message is None:
            return
        path = item.get("p")
        if (path in {None, "", "/message/content/parts/0"}) and isinstance(value, str):
            content = message.get("content")
            if not isinstance(content, dict):
                content = {"content_type": "text", "parts": []}
                message["content"] = content
            parts = content.get("parts")
            if not isinstance(parts, list):
                parts = []
            parts = list(parts)
            previous = parts[0] if parts and isinstance(parts[0], str) else ""
            if parts:
                parts[0] = previous + value
            else:
                parts.append(value)
            content["parts"] = parts
        elif path == "/message/content" and isinstance(value, dict):
            message["content"] = dict(value)
        elif path == "/message/content/thoughts" and isinstance(value, list):
            content = message.get("content")
            if not isinstance(content, dict):
                content = {"content_type": "thoughts"}
                message["content"] = content
            content["thoughts"] = [dict(item) if isinstance(item, dict) else item for item in value]
        elif (
            isinstance(path, str)
            and isinstance(value, str)
            and (match := re.fullmatch(r"/message/content/thoughts/(\d+)/summary", path))
        ):
            content = message.get("content")
            if not isinstance(content, dict):
                content = {"content_type": "thoughts"}
                message["content"] = content
            thoughts = content.get("thoughts")
            if not isinstance(thoughts, list):
                thoughts = []
            thoughts = list(thoughts)
            index = int(match.group(1))
            while len(thoughts) <= index:
                thoughts.append({})
            entry = thoughts[index]
            if not isinstance(entry, dict):
                entry = {}
            else:
                entry = dict(entry)
            previous = entry.get("summary")
            entry["summary"] = (previous if isinstance(previous, str) else "") + value
            thoughts[index] = entry
            content["thoughts"] = thoughts
        elif path == "/message/status":
            message["status"] = value
        elif path == "/message/end_turn":
            message["end_turn"] = value
        elif path == "/message/metadata" and isinstance(value, dict):
            metadata = message.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            message["metadata"] = {**metadata, **value}
        else:
            return
        self._inspect_message(message, output)

    def _process_payload(
        self,
        payload: dict[str, Any],
        output: list[dict[str, Any]],
    ) -> None:
        self._collect_messages(payload, output)
        self._apply_patch_item(payload, output)
        value = payload.get("v")
        if isinstance(value, list):
            for item in value[:128]:
                self._apply_patch_item(item, output)


def _assistant_candidates_from_payload(
    payload: dict[str, Any],
    *,
    baseline_assistant_ids: set[str] | frozenset[str],
    turn_exchange_id: str | None = None,
    allow_unfinished: bool = False,
) -> list[Any]:
    branch = _current_branch_nodes(payload)
    normalized_turn_exchange_id = (
        turn_exchange_id.strip()
        if isinstance(turn_exchange_id, str) and turn_exchange_id.strip()
        else None
    )
    turn_metadata_present = normalized_turn_exchange_id is not None and any(
        _node_turn_exchange_id(node) is not None for _, node in branch
    )

    candidates: list[Any] = []
    for node_id, node in branch:
        message = _chat_message_from_node(node_id, node)
        if message is None or getattr(message, "role", None) != "assistant":
            continue
        recipient = getattr(message, "recipient", None)
        if isinstance(recipient, str) and recipient.strip() not in {"", "all"}:
            continue
        if (
            turn_metadata_present
            and _node_turn_exchange_id(node) != normalized_turn_exchange_id
        ):
            continue
        raw_message = node.get("message")
        if not isinstance(raw_message, dict):
            continue
        metadata = raw_message.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        content = raw_message.get("content")
        if not isinstance(content, dict):
            content = {}
        content_type = content.get("content_type")
        if metadata.get("is_thinking_preamble_message") is True:
            continue
        if content_type in {"thoughts", "reasoning_recap"}:
            continue
        finish_reason = getattr(message, "finish_reason", None)
        has_finish_reason = isinstance(finish_reason, str) and bool(
            finish_reason.strip()
        )
        if (
            not allow_unfinished
            and raw_message.get("end_turn") is not True
            and not has_finish_reason
        ):
            continue
        message_id = getattr(message, "message_id", None)
        if not isinstance(message_id, str) or message_id in baseline_assistant_ids:
            continue
        if not bool(getattr(message, "text", "").strip()):
            continue
        candidates.append(message)
    return candidates


@_gate_wait_for_new_final_assistant
def _wait_for_new_final_assistant(
    self: Any,
    conversation_id: str,
    *,
    baseline_assistant_ids: set[str] | frozenset[str],
    baseline_message_ids: set[str] | frozenset[str] = frozenset(),
    timeout: float,
    interval: float,
    include_readback: bool = False,
    turn_exchange_id: str | None = None,
    retry_400_until_timeout: bool = False,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    submission_id: str | None = None,
    minimum_poll_interval: float | None = None,
    allow_unfinished: bool = False,
    stop_requested: Callable[[], bool] | None = None,
) -> Any | tuple[Any, dict[str, Any] | None, int | None]:
    """Wait for canonical finality, optionally returning the reused payload.

    The default return remains the historical assistant-message object. PR8.11
    production callers opt into ``include_readback=True`` so status, assistant
    messages and attach metadata can be derived from one canonical payload per
    poll instead of issuing three serial reads after browser completion.
    Lightweight/custom clients without the private canonical reader retain the
    previous get_status/get_messages path.
    """

    poll_started = time.monotonic()
    deadline = poll_started + timeout
    last_status = None
    canonical_reader = getattr(self, "_get_conversation_payload", None)
    use_single_payload = callable(canonical_reader)
    canonical_payload_read_count = 0
    emitted_message_ids = set(baseline_message_ids)

    while True:
        if stop_requested is not None and stop_requested():
            raise ConversationTimeoutError(
                "browser-native turn stopped by user",
                timeout=0.0,
                last_status="stopped",
            )
        rate_limited_read_failure = False
        if use_single_payload:
            payload = None
            try:
                canonical_payload_read_count += 1
                payload = canonical_reader(conversation_id)
            except RequestError as error:
                # A freshly created conversation can briefly be absent from the
                # canonical read plane. After an early-detached new-chat write, the
                # browser route may also resolve while canonical GET still returns
                # 400. Retry that new-chat-only condition until the caller's overall
                # turn deadline; continuations keep failing deterministic 400s fast.
                # Explicitly retry transport-marked temporary failures such as 429
                # without turning backend throttling into a semantic turn failure.
                transient_400 = error.status_code == 400 and retry_400_until_timeout
                retryable_error = bool(getattr(error, "retryable", False))
                if (
                    error.status_code != 404
                    and not transient_400
                    and not retryable_error
                ):
                    raise
                rate_limited_read_failure = error.status_code == 429
                payload = None

            if isinstance(payload, dict):
                last_status = _status_from_payload(payload)
                for event in _canonical_intermediate_events(
                    payload,
                    baseline_message_ids=baseline_message_ids,
                    emitted_message_ids=emitted_message_ids,
                    submission_id=submission_id,
                ):
                    _emit_revision_safe_event(self, on_event, event)
                candidates = _assistant_candidates_from_payload(
                    payload,
                    baseline_assistant_ids=baseline_assistant_ids,
                    turn_exchange_id=turn_exchange_id,
                    allow_unfinished=allow_unfinished,
                )
                for candidate in reversed(candidates):
                    if allow_unfinished:
                        if include_readback:
                            return candidate, payload, canonical_payload_read_count
                        return candidate
                    finish_reason = getattr(candidate, "finish_reason", None)
                    if isinstance(finish_reason, str) and bool(finish_reason.strip()):
                        if include_readback:
                            return candidate, payload, canonical_payload_read_count
                        return candidate
                    if _status_finalizes_message(last_status, candidate.message_id):
                        if include_readback:
                            return candidate, payload, canonical_payload_read_count
                        return candidate
        else:
            try:
                last_status = self.get_status(conversation_id)
            except Exception:
                last_status = None
            messages = self.get_messages(
                conversation_id,
                limit=None,
                roles={"assistant"},
                include_empty=True,
            )
            candidates = [
                message
                for message in messages
                if isinstance(getattr(message, "message_id", None), str)
                and message.message_id not in baseline_assistant_ids
                and (
                    not isinstance(getattr(message, "recipient", None), str)
                    or getattr(message, "recipient", None).strip() in {"", "all"}
                )
                and bool(getattr(message, "text", "").strip())
            ]
            for candidate in reversed(candidates):
                if allow_unfinished:
                    if include_readback:
                        return candidate, None, None
                    return candidate
                finish_reason = getattr(candidate, "finish_reason", None)
                if isinstance(finish_reason, str) and bool(finish_reason.strip()):
                    if include_readback:
                        return candidate, None, None
                    return candidate
                if _status_finalizes_message(last_status, candidate.message_id):
                    if include_readback:
                        return candidate, None, None
                    return candidate

        if time.monotonic() >= deadline:
            raise ConversationTimeoutError(
                "browser-native write completed but canonical assistant readback did not finish",
                timeout=timeout,
                last_status=last_status,
            )
        retry_floor = (
            _CANONICAL_RATE_LIMIT_BACKOFF_SECONDS
            if rate_limited_read_failure
            else (
                _CANONICAL_LIVE_POLL_INTERVAL_SECONDS
                if minimum_poll_interval is None
                else max(0.0, float(minimum_poll_interval))
            )
        )
        sleep_for = max(retry_floor, interval)
        remaining_sleep = max(0.0, deadline - time.monotonic())
        if remaining_sleep > 0:
            time.sleep(min(sleep_for, remaining_sleep))


def _emit_revision_safe_event(
    self: Any,
    on_event: Callable[[dict[str, Any]], None] | None,
    event: dict[str, Any],
) -> None:
    if on_event is None:
        return
    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type:
        return
    payload = {key: value for key, value in event.items() if key != "type"}
    try:
        self._emit_event(on_event, event_type, **payload)
    except Exception:
        # PR8.9 observation callbacks never change write authority or authorize
        # replay after delegation.
        pass


def _provider_supports_revision_safe_streaming(provider: Any) -> bool:
    if not callable(getattr(provider, "send_text_streaming", None)):
        return False
    if getattr(provider, "revision_safe_streaming_supported", False) is True:
        return True
    rpc = getattr(provider, "_rpc", None)
    if not callable(rpc):
        return False
    try:
        parameters = inspect.signature(rpc).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "on_event" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _callable_accepts_attachment_paths(value: Any) -> bool:
    if not callable(value):
        return False
    try:
        parameters = inspect.signature(value).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "attachment_paths"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _callable_accepts_model_slug(value: Any) -> bool:
    if not callable(value):
        return False
    try:
        parameters = inspect.signature(value).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "model_slug"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _callable_accepts_write_identity(value: Any) -> bool:
    if not callable(value):
        return False
    try:
        parameters = inspect.signature(value).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "on_write_identity"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _callable_accepts_stream_transport_event(value: Any) -> bool:
    if not callable(value):
        return False
    try:
        parameters = inspect.signature(value).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "on_transport_event"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _callable_accepts_stream_should_stop(value: Any) -> bool:
    if not callable(value):
        return False
    try:
        parameters = inspect.signature(value).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "stream_should_stop"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def submit_browser_native(
    self: Any,
    prompt: str,
    *,
    conversation: ConversationRef
    | ChatConversation
    | dict[str, Any]
    | str
    | None = None,
    timeout: float = 150.0,
    poll_interval: float = 0.5,
    on_token: Callable[[str], None] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    attachment_paths: Sequence[str | Path] | None = None,
    model_slug: str | None = None,
    _prewrite_canonical_payload: dict[str, Any] | None = None,
    _prewrite_canonical_completed_at_ms: int | None = None,
) -> BrowserNativeSubmission:
    """Perform exactly one browser-owned write and return before canonical finality."""

    provider = getattr(self, "_browser_native_turn_provider", None)
    if not callable(getattr(provider, "send_text", None)):
        raise RequestError(
            "BROWSER_NATIVE_PROVIDER_NOT_CONFIGURED",
            request_stage="browser_native_turn",
        )
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")

    if attachment_paths is None:
        scoped_paths = current_browser_owned_attachment_paths()
        if scoped_paths is not None:
            attachment_paths = scoped_paths
    normalized_attachment_paths = tuple(attachment_paths or ())
    if normalized_attachment_paths and not _callable_accepts_attachment_paths(
        provider.send_text
    ):
        raise RequestError(
            "BROWSER_NATIVE_RICH_INPUT_PROVIDER_UNSUPPORTED",
            request_stage="browser_native_turn_preflight",
        )
    normalized_model_slug = model_slug.strip() if isinstance(model_slug, str) else None
    if model_slug is not None and not normalized_model_slug:
        raise ValueError("model_slug must be a non-empty string or None")
    if normalized_model_slug and not _callable_accepts_model_slug(provider.send_text):
        raise RequestError(
            "BROWSER_NATIVE_MODEL_SLUG_PROVIDER_UNSUPPORTED",
            request_stage="browser_native_turn_preflight",
        )

    started = time.monotonic()
    submission_id = str(uuid.uuid4())
    baseline_assistant_ids: set[str] = set()
    baseline_message_ids: set[str] = set()
    is_continuation = conversation is not None
    canonical_status_before_turn = None

    # BrowserAuthorityLease fences the browser write and its terminal readback.
    # Continuation preflight reads happen before any write is submitted, so they
    # must not inherit the newly-issued write lease. A persistent runtime tab
    # may still carry the previous completed turn's lease in extension storage;
    # presenting the new lease during these reads would fail closed before the
    # new turn has a chance to replace it.
    current_lease = getattr(provider, "_current_browser_authority_lease_id", None)
    clear_lease = getattr(provider, "clear_browser_authority_lease", None)
    set_lease = getattr(provider, "set_browser_authority_lease", None)
    suspended_lease_id = current_lease() if callable(current_lease) else None
    suspend_prewrite_lease = (
        conversation is not None
        and isinstance(suspended_lease_id, str)
        and bool(suspended_lease_id)
        and callable(clear_lease)
        and callable(set_lease)
    )
    if suspend_prewrite_lease:
        clear_lease()
    try:
        if conversation is not None:
            (
                baseline_message_ids,
                baseline_assistant_ids,
                canonical_status_before_turn,
            ) = _canonical_prewrite_snapshot(
                self,
                conversation,
                canonical_payload=_prewrite_canonical_payload,
            )

        recovery_send = getattr(provider, "send_text_with_stale_ui_recovery", None)
        recovery_stream_send = getattr(
            provider, "send_text_with_stale_ui_recovery_streaming", None
        )
        stream_send = getattr(provider, "send_text_streaming", None)
        canonical_status_recovery_confirm = None
        recovery_authorized = False
        recovery_completed_at_ms: int | None = None
        supplied_completion_age_ms = None
        if (
            isinstance(_prewrite_canonical_completed_at_ms, int)
            and not isinstance(_prewrite_canonical_completed_at_ms, bool)
            and _prewrite_canonical_completed_at_ms > 0
        ):
            supplied_completion_age_ms = (
                int(time.time() * 1000) - _prewrite_canonical_completed_at_ms
            )
        reusable_commit_completion = (
            is_continuation
            and canonical_status_before_turn == "completed"
            and isinstance(_prewrite_canonical_payload, dict)
            and isinstance(supplied_completion_age_ms, int)
            and 0
            <= supplied_completion_age_ms
            <= _PREWRITE_CANONICAL_COMPLETION_MAX_AGE_MS
        )
        if (
            is_continuation
            and canonical_status_before_turn == "completed"
            and callable(recovery_send)
        ):
            if reusable_commit_completion:
                canonical_status_recovery_confirm = "completed"
                recovery_authorized = True
                recovery_completed_at_ms = _prewrite_canonical_completed_at_ms
            else:
                canonical_status_recovery_confirm = _canonical_status_value(
                    self, conversation
                )
                recovery_authorized = canonical_status_recovery_confirm == "completed"
                if recovery_authorized:
                    recovery_completed_at_ms = int(time.time() * 1000)
    finally:
        if suspend_prewrite_lease:
            set_lease(suspended_lease_id)

    self._emit_event(
        on_event,
        "browser_native_turn_started",
        submission_id=submission_id,
        is_continuation=is_continuation,
        canonical_status_before_turn=canonical_status_before_turn,
        canonical_status_recovery_confirm=canonical_status_recovery_confirm,
        stale_ui_recovery_authorized=recovery_authorized,
        attachment_count=len(normalized_attachment_paths),
    )

    stream_state = RevisionSafeTextAccumulator()
    topic_normalizer = CanonicalTopicStreamNormalizer(
        emitted_message_ids=tuple(baseline_message_ids),
    )
    transport_sequence = 0
    observer_lock = threading.Lock()
    observer_stop = threading.Event()
    observer_thread: threading.Thread | None = None
    observer_deadline = started + timeout

    def handle_text_event(event: dict[str, Any]) -> None:
        normalized = stream_state.apply(event)
        if normalized is not None:
            normalized = {**normalized, "submission_id": submission_id}
            _emit_revision_safe_event(self, on_event, normalized)
        with observer_lock:
            topic_normalizer.answer_message_id = stream_state.message_id
            topic_normalizer.answer_text = stream_state.text

    def handle_transport_event(event: dict[str, Any]) -> None:
        nonlocal transport_sequence
        with observer_lock:
            topic_normalizer.answer_message_id = stream_state.message_id
            topic_normalizer.answer_text = stream_state.text
            normalized_events = topic_normalizer.feed_transport_event(event)
        for normalized in normalized_events:
            if normalized.get("type") in {
                ASSISTANT_TEXT_SNAPSHOT,
                ASSISTANT_TEXT_DELTA,
                ASSISTANT_TEXT_REVISION,
            }:
                transport_sequence = max(
                    transport_sequence + 1,
                    stream_state.last_sequence + 1,
                )
                handle_text_event({**normalized, "sequence": transport_sequence})
                continue
            _emit_revision_safe_event(
                self,
                on_event,
                {**normalized, "submission_id": submission_id},
            )

    def stream_should_stop() -> bool:
        with observer_lock:
            return topic_normalizer.turn_completed

    def stop_canonical_observer() -> None:
        observer_stop.set()
        thread = observer_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.2)

    def start_canonical_observer(conversation_id: str) -> None:
        nonlocal observer_thread
        if observer_thread is not None:
            return
        read_payload = getattr(self, "_get_conversation_payload", None)
        if not callable(read_payload):
            return

        def worker() -> None:
            while not observer_stop.is_set() and time.monotonic() < observer_deadline:
                try:
                    payload = read_payload(conversation_id)
                except Exception:
                    if observer_stop.wait(_ACTIVE_SEND_CANONICAL_POLL_INTERVAL_SECONDS):
                        return
                    continue
                if observer_stop.is_set():
                    return
                with observer_lock:
                    events = _canonical_intermediate_events(
                        payload,
                        baseline_message_ids=baseline_message_ids,
                        emitted_message_ids=topic_normalizer.emitted_message_ids,
                        submission_id=submission_id,
                    )
                for normalized in events:
                    _emit_revision_safe_event(self, on_event, normalized)
                if observer_stop.wait(_ACTIVE_SEND_CANONICAL_POLL_INTERVAL_SECONDS):
                    return

        observer_thread = threading.Thread(
            target=worker,
            name="cwa-active-send-canonical-observer",
            daemon=True,
        )
        observer_thread.start()

    def handle_write_identity(event: dict[str, Any]) -> None:
        if (
            not isinstance(event, dict)
            or event.get("type") != "write_identity_resolved"
        ):
            return
        conversation_id = event.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            return
        status_code = event.get("submit_response_status")
        normalized_conversation_id = conversation_id.strip()
        self._emit_event(
            on_event,
            "browser_native_write_identity_resolved",
            submission_id=submission_id,
            conversation_id=normalized_conversation_id,
            submit_response_observed=bool(event.get("submit_response_observed")),
            status_code=(
                status_code
                if isinstance(status_code, int) and not isinstance(status_code, bool)
                else None
            ),
        )
        start_canonical_observer(normalized_conversation_id)

    streaming_requested = (
        on_event is not None and _provider_supports_revision_safe_streaming(provider)
    )
    attachment_kwargs = (
        {"attachment_paths": normalized_attachment_paths}
        if normalized_attachment_paths
        else {}
    )
    model_kwargs = (
        {"model_slug": normalized_model_slug} if normalized_model_slug else {}
    )
    provider_kwargs = {**attachment_kwargs, **model_kwargs}
    if recovery_authorized:
        canonical_completed_at_ms = recovery_completed_at_ms or int(time.time() * 1000)
        if streaming_requested and callable(recovery_stream_send):
            if normalized_attachment_paths and not _callable_accepts_attachment_paths(
                recovery_stream_send
            ):
                raise RequestError(
                    "BROWSER_NATIVE_RICH_INPUT_RECOVERY_PROVIDER_UNSUPPORTED",
                    request_stage="browser_native_turn_preflight",
                )
            write_identity_kwargs = (
                {"on_write_identity": handle_write_identity}
                if _callable_accepts_write_identity(recovery_stream_send)
                else {}
            )
            transport_stream_kwargs: dict[str, Any] = {}
            if _callable_accepts_stream_transport_event(recovery_stream_send):
                transport_stream_kwargs["on_transport_event"] = handle_transport_event
            if _callable_accepts_stream_should_stop(recovery_stream_send):
                transport_stream_kwargs["stream_should_stop"] = stream_should_stop
            try:
                turn = recovery_stream_send(
                    prompt,
                    conversation=conversation,
                    timeout=timeout,
                    canonical_completed_at_ms=canonical_completed_at_ms,
                    on_text_event=handle_text_event,
                    **write_identity_kwargs,
                    **transport_stream_kwargs,
                    **provider_kwargs,
                )
            finally:
                stop_canonical_observer()
        else:
            if normalized_attachment_paths and not _callable_accepts_attachment_paths(
                recovery_send
            ):
                raise RequestError(
                    "BROWSER_NATIVE_RICH_INPUT_RECOVERY_PROVIDER_UNSUPPORTED",
                    request_stage="browser_native_turn_preflight",
                )
            turn = recovery_send(
                prompt,
                conversation=conversation,
                timeout=timeout,
                canonical_completed_at_ms=canonical_completed_at_ms,
                **provider_kwargs,
            )
    elif streaming_requested and callable(stream_send):
        if normalized_attachment_paths and not _callable_accepts_attachment_paths(
            stream_send
        ):
            raise RequestError(
                "BROWSER_NATIVE_RICH_INPUT_STREAM_PROVIDER_UNSUPPORTED",
                request_stage="browser_native_turn_preflight",
            )
        write_identity_kwargs = (
            {"on_write_identity": handle_write_identity}
            if _callable_accepts_write_identity(stream_send)
            else {}
        )
        transport_stream_kwargs = {}
        if _callable_accepts_stream_transport_event(stream_send):
            transport_stream_kwargs["on_transport_event"] = handle_transport_event
        if _callable_accepts_stream_should_stop(stream_send):
            transport_stream_kwargs["stream_should_stop"] = stream_should_stop
        try:
            turn = stream_send(
                prompt,
                conversation=conversation,
                timeout=timeout,
                on_text_event=handle_text_event,
                **write_identity_kwargs,
                **transport_stream_kwargs,
                **provider_kwargs,
            )
        finally:
            stop_canonical_observer()
    else:
        turn = provider.send_text(
            prompt,
            conversation=conversation,
            timeout=timeout,
            **provider_kwargs,
        )

    raw_attachment_count = getattr(turn, "attachment_count", None)
    if normalized_attachment_paths:
        expected_attachment_count = len(normalized_attachment_paths)
        if (
            not isinstance(raw_attachment_count, int)
            or isinstance(raw_attachment_count, bool)
            or raw_attachment_count != expected_attachment_count
        ):
            raise RequestError(
                "BROWSER_NATIVE_RICH_INPUT_ATTACHMENT_CONFIRMATION_MISMATCH",
                request_stage="browser_native_turn_postwrite",
            )
        attachment_count = raw_attachment_count
    elif (
        isinstance(raw_attachment_count, int)
        and not isinstance(raw_attachment_count, bool)
        and raw_attachment_count >= 0
    ):
        attachment_count = raw_attachment_count
    else:
        attachment_count = 0

    accepted_at_ms = int(time.time() * 1000)
    self._emit_event(
        on_event,
        "browser_native_write_completed",
        submission_id=submission_id,
        accepted_at_ms=accepted_at_ms,
        conversation_id=turn.conversation_id,
        turn_exchange_id=turn.turn_exchange_id,
        status_code=turn.response_status,
        elapsed_ms=turn.elapsed_ms,
        runtime_reloaded=turn.runtime_reloaded,
        runtime_reload_ms=turn.runtime_reload_ms,
        runtime_tab_id=turn.tab_id,
        runtime_tab_preexisting=turn.runtime_tab_preexisting,
        runtime_tab_created_for_turn=turn.runtime_tab_created_for_turn,
        tab_was_active_at_write_start=turn.tab_was_active,
        tab_active_after_write=turn.tab_active_after,
        tab_activated_during_turn=turn.tab_activated_during_turn,
        foreground_activation_observed=turn.foreground_activation_observed,
        attachment_count=attachment_count,
        canonical_read_transport=getattr(turn, "canonical_read_transport", None),
        canonical_read_fallback_reason=getattr(
            turn, "canonical_read_fallback_reason", None
        ),
        phase_a_transport=getattr(turn, "phase_a_transport", None),
        phase_a_gate_wait_ms=getattr(turn, "phase_a_gate_wait_ms", None),
        phase_a_elapsed_ms=getattr(turn, "phase_a_elapsed_ms", None),
        phase_b_transport=getattr(turn, "phase_b_transport", None),
        phase_b_fallback_reason=getattr(turn, "phase_b_fallback_reason", None),
        phase_b_elapsed_ms=getattr(turn, "phase_b_elapsed_ms", None),
        revision_safe_stream_observation_count=stream_state.observation_count,
        canonical_finality_proven=False,
    )

    return BrowserNativeSubmission(
        submission_id=submission_id,
        turn=turn,
        baseline_assistant_ids=frozenset(baseline_assistant_ids),
        baseline_message_ids=frozenset(baseline_message_ids),
        timeout=float(timeout),
        poll_interval=float(poll_interval),
        started_monotonic=started,
        accepted_at_ms=accepted_at_ms,
        is_continuation=is_continuation,
        attachment_count=attachment_count,
        stream_state=stream_state,
        on_token=on_token,
        on_event=on_event,
    )


def await_browser_native_final(
    self: Any,
    submission: BrowserNativeSubmission,
) -> ChatResponse:
    """Resolve canonical finality for one previously acknowledged browser write."""

    if not isinstance(submission, BrowserNativeSubmission):
        raise TypeError("submission must be BrowserNativeSubmission")
    if submission.final_response is not None:
        return submission.final_response

    turn = submission.turn
    remaining = max(
        1.0,
        submission.timeout - (time.monotonic() - submission.started_monotonic),
    )
    retry_400_until_timeout = not submission.is_continuation
    provider = getattr(self, "_browser_native_turn_provider", None)
    observe_turn = getattr(provider, "observe_turn", None)
    stop_requested_for = getattr(provider, "stop_requested_for", None)

    def provider_stop_requested() -> bool:
        return bool(
            callable(stop_requested_for) and stop_requested_for(turn.conversation_id)
        )

    authority_lease_id = getattr(turn, "browser_authority_lease_id", None)
    observed_turn_exchange_id = getattr(turn, "turn_exchange_id", None)
    passive_observer_used = False
    passive_finish_reason: str | None = None
    passive_message_id: str | None = None
    passive_stream_ended_without_terminal = False
    incomplete_without_terminal = False
    passive_emitted_message_ids = set(submission.baseline_message_ids)
    # A provider may deliver the first part of a turn from the write response
    # itself and then hand finality/continuation to the passive canonical
    # observer. Seed the observer from the already-normalized stream state so
    # sequence numbers remain monotonic and the first canonical snapshot can be
    # emitted as a delta (or revision) instead of restarting the stream.
    passive_text_sequence = submission.stream_state.last_sequence
    passive_text_message_id: str | None = submission.stream_state.message_id
    passive_text_snapshot = submission.stream_state.text

    if (
        bool(getattr(turn, "passive_observer_armed", False))
        and callable(observe_turn)
        and isinstance(authority_lease_id, str)
        and authority_lease_id
    ):

        def handle_passive_event(event: dict[str, Any]) -> None:
            nonlocal \
                passive_text_sequence, \
                passive_text_message_id, \
                passive_text_snapshot
            if not isinstance(event, dict):
                return
            event_type = event.get("type")
            if event_type == "passive_observer_heartbeat":
                return
            if event_type != "canonical_payload_snapshot":
                normalized = {**event, "submission_id": submission.submission_id}
                _emit_revision_safe_event(self, submission.on_event, normalized)
                return

            payload = event.get("payload")
            if not isinstance(payload, dict):
                return
            for intermediate in _canonical_intermediate_events(
                payload,
                baseline_message_ids=submission.baseline_message_ids,
                emitted_message_ids=passive_emitted_message_ids,
                submission_id=submission.submission_id,
            ):
                _emit_revision_safe_event(self, submission.on_event, intermediate)

            candidates = _assistant_candidates_from_payload(
                payload,
                baseline_assistant_ids=submission.baseline_assistant_ids,
                turn_exchange_id=observed_turn_exchange_id,
                allow_unfinished=True,
            )
            if not candidates:
                return
            candidate = candidates[-1]
            message_id = getattr(candidate, "message_id", None)
            text = getattr(candidate, "text", "")
            if not isinstance(text, str) or not text:
                return
            if message_id == passive_text_message_id and text == passive_text_snapshot:
                return

            passive_text_sequence += 1
            if passive_text_message_id is None or message_id != passive_text_message_id:
                text_event = {
                    "type": ASSISTANT_TEXT_SNAPSHOT,
                    "sequence": passive_text_sequence,
                    "message_id": message_id,
                    "text": text,
                }
            elif text.startswith(passive_text_snapshot):
                delta = text[len(passive_text_snapshot) :]
                if not delta:
                    return
                text_event = {
                    "type": ASSISTANT_TEXT_DELTA,
                    "sequence": passive_text_sequence,
                    "message_id": message_id,
                    "delta": delta,
                }
            else:
                text_event = {
                    "type": ASSISTANT_TEXT_REVISION,
                    "sequence": passive_text_sequence,
                    "message_id": message_id,
                    "text": text,
                }

            passive_text_message_id = message_id
            passive_text_snapshot = text
            normalized = submission.stream_state.apply(text_event)
            if normalized is not None:
                normalized = {**normalized, "submission_id": submission.submission_id}
                _emit_revision_safe_event(self, submission.on_event, normalized)

        try:
            observe_result = observe_turn(
                conversation_id=turn.conversation_id,
                turn_exchange_id=observed_turn_exchange_id,
                browser_authority_lease_id=authority_lease_id,
                timeout=remaining,
                on_event=handle_passive_event,
            )
            learned_turn_exchange_id = (
                observe_result.get("turnExchangeId")
                if isinstance(observe_result, dict)
                else None
            )
            if (
                isinstance(learned_turn_exchange_id, str)
                and learned_turn_exchange_id.strip()
            ):
                observed_turn_exchange_id = learned_turn_exchange_id.strip()
            learned_finish_reason = (
                observe_result.get("finishReason")
                if isinstance(observe_result, dict)
                else None
            )
            if isinstance(learned_finish_reason, str) and learned_finish_reason.strip():
                passive_finish_reason = learned_finish_reason.strip()
            learned_message_id = (
                observe_result.get("messageId")
                if isinstance(observe_result, dict)
                else None
            )
            if isinstance(learned_message_id, str) and learned_message_id.strip():
                passive_message_id = learned_message_id.strip()
            passive_observer_used = True
            remaining = max(
                1.0,
                submission.timeout - (time.monotonic() - submission.started_monotonic),
            )
            settle_seconds = min(
                _PASSIVE_FINAL_RECONCILE_SETTLE_SECONDS,
                max(0.0, remaining - 1.0),
            )
            if settle_seconds > 0:
                time.sleep(settle_seconds)
                remaining = max(
                    1.0,
                    submission.timeout
                    - (time.monotonic() - submission.started_monotonic),
                )
        except (RequestError, OSError, EOFError, ValueError) as error:
            reason = str(error)
            if (
                isinstance(error, RequestError)
                and "PASSIVE_OBSERVER_STREAM_ENDED_WITHOUT_TERMINAL" in reason
            ):
                passive_stream_ended_without_terminal = True
                passive_observer_used = True
                remaining = min(remaining, _PASSIVE_STREAM_ENDED_RECONCILE_SECONDS)
            self._emit_event(
                submission.on_event,
                "browser_native_passive_observer_fallback",
                submission_id=submission.submission_id,
                reason=reason,
            )

    passive_stopped = passive_finish_reason == "stopped"
    stopped_by_user = passive_stopped or provider_stop_requested()
    readback_timeout = min(remaining, 1.0) if stopped_by_user else remaining
    try:
        final_message, canonical_payload, canonical_payload_read_count = (
            _wait_for_new_final_assistant(
                self,
                turn.conversation_id,
                baseline_assistant_ids=submission.baseline_assistant_ids,
                baseline_message_ids=submission.baseline_message_ids,
                timeout=readback_timeout,
                interval=_PASSIVE_FINAL_RECONCILE_RETRY_SECONDS
                if passive_observer_used
                else submission.poll_interval,
                include_readback=True,
                turn_exchange_id=observed_turn_exchange_id,
                retry_400_until_timeout=retry_400_until_timeout,
                on_event=None if passive_observer_used else submission.on_event,
                submission_id=submission.submission_id,
                minimum_poll_interval=_PASSIVE_FINAL_RECONCILE_RETRY_SECONDS
                if passive_observer_used
                else None,
                allow_unfinished=stopped_by_user,
                stop_requested=None if passive_stopped else provider_stop_requested,
            )
        )
    except ConversationTimeoutError:
        stopped_by_user = stopped_by_user or provider_stop_requested()
        if not stopped_by_user and not passive_stream_ended_without_terminal:
            raise
        incomplete_without_terminal = (
            passive_stream_ended_without_terminal and not stopped_by_user
        )
        final_message = ChatMessage(
            message_id=passive_message_id,
            role="assistant",
            text="",
            finish_reason="stopped" if stopped_by_user else "incomplete",
        )
        canonical_payload = None
        canonical_payload_read_count = None

    stopped_by_user = stopped_by_user or provider_stop_requested()
    result_finish_reason = "stopped" if stopped_by_user else final_message.finish_reason

    if canonical_payload is not None:
        result_conversation = ChatConversation(
            conversation_id=turn.conversation_id,
            message_id=final_message.message_id,
            parent_message_id=final_message.message_id,
            finish_reason=result_finish_reason,
            is_thinking=False,
        )
        attached = AttachedConversation.from_payload(
            canonical_payload,
            conversation=result_conversation,
        )
    elif stopped_by_user or incomplete_without_terminal:
        result_conversation = ChatConversation(
            conversation_id=turn.conversation_id,
            message_id=final_message.message_id,
            parent_message_id=final_message.message_id,
            finish_reason=result_finish_reason,
            is_thinking=False,
        )
        attached = AttachedConversation(conversation=result_conversation)
    else:
        attached = self.attach_conversation(turn.conversation_id)
        conversation_data = attached.conversation.to_dict()
        conversation_data.update(
            {
                "conversation_id": turn.conversation_id,
                "message_id": final_message.message_id,
                "finish_reason": result_finish_reason,
                "is_thinking": False,
            }
        )
        result_conversation = ChatConversation.from_dict(conversation_data)

    total = time.monotonic() - submission.started_monotonic
    response = ChatResponse(
        text=final_message.text,
        title=attached.title,
        conversation=result_conversation,
        metrics=ChatMetrics(total=total, backend_status=turn.response_status),
        request=ChatRequestDiagnostics(
            conversation_id=turn.conversation_id,
            is_continuation=submission.is_continuation,
            observed_model=final_message.model,
            turn_exchange_id=observed_turn_exchange_id,
        ),
    )

    finalization = submission.stream_state.finalization_event(
        canonical_text=final_message.text,
        conversation_id=turn.conversation_id,
        message_id=final_message.message_id,
        model=final_message.model,
        finish_reason=result_finish_reason,
    )
    finalization = {**finalization, "submission_id": submission.submission_id}
    _emit_revision_safe_event(self, submission.on_event, finalization)

    if submission.on_token is not None and response.text:
        submission.on_token(response.text)
    self._emit_event(
        submission.on_event,
        "browser_native_readback_completed",
        submission_id=submission.submission_id,
        conversation_id=turn.conversation_id,
        message_id=final_message.message_id,
        model=final_message.model,
        total=total,
        attachment_count=submission.attachment_count,
        canonical_payload_read_count=canonical_payload_read_count,
        canonical_payload_reused_for_attach=canonical_payload is not None,
        stream_canonical_reconciliation=finalization["reconciliation"],
        revision_safe_stream_observation_count=submission.stream_state.observation_count,
        revision_safe_stream_revision_count=submission.stream_state.revision_count,
        revision_safe_stream_delivery_incomplete=submission.stream_state.delivery_incomplete,
        canonical_finality_proven=not stopped_by_user
        and not incomplete_without_terminal,
        stopped_by_user=stopped_by_user,
        incomplete_without_terminal=incomplete_without_terminal,
    )
    if stopped_by_user:
        clear_stop_requested_for = getattr(provider, "clear_stop_requested_for", None)
        if callable(clear_stop_requested_for):
            clear_stop_requested_for(turn.conversation_id)
    submission.final_response = response
    return response


@_gate_send_browser_native
def send_browser_native(
    self: Any,
    prompt: str,
    *,
    conversation: ConversationRef
    | ChatConversation
    | dict[str, Any]
    | str
    | None = None,
    timeout: float = 150.0,
    poll_interval: float = 0.5,
    on_token: Callable[[str], None] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    attachment_paths: Sequence[str | Path] | None = None,
    model_slug: str | None = None,
    _prewrite_canonical_payload: dict[str, Any] | None = None,
    _prewrite_canonical_completed_at_ms: int | None = None,
) -> ChatResponse:
    """Compatibility composition: submit exactly once, then await canonical finality."""

    submission = submit_browser_native(
        self,
        prompt,
        conversation=conversation,
        timeout=timeout,
        poll_interval=poll_interval,
        on_token=on_token,
        on_event=on_event,
        attachment_paths=attachment_paths,
        model_slug=model_slug,
        _prewrite_canonical_payload=_prewrite_canonical_payload,
        _prewrite_canonical_completed_at_ms=_prewrite_canonical_completed_at_ms,
    )
    return await_browser_native_final(self, submission)
