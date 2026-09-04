from __future__ import annotations

import inspect
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .browser_native_provider import BrowserNativeTurnProvider
from .exceptions import ConversationTimeoutError, RequestError
from .message_text import extract_message_text
from .messages import _chat_message_from_node, _current_branch_nodes
from .product_media import current_browser_owned_attachment_paths
from .revision_safe_streaming_pr8_9 import RevisionSafeTextAccumulator
from .status import _status_from_payload
from .types import (
    AttachedConversation,
    ChatConversation,
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
_CANONICAL_RATE_LIMIT_BACKOFF_SECONDS = 15.0
_PASSIVE_FINAL_RECONCILE_RETRY_SECONDS = 5.0
_PASSIVE_FINAL_RECONCILE_SETTLE_SECONDS = 4.0
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


def set_browser_native_turn_provider(self: Any, provider: BrowserNativeTurnProvider | None) -> None:
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
                redacted[rendered_key] = _redact_intermediate_value(item, depth=depth + 1)
        return redacted
    if isinstance(value, list):
        items = [_redact_intermediate_value(item, depth=depth + 1) for item in value[:128]]
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


def _tool_call_label(raw_message: dict[str, Any], metadata: dict[str, Any], recipient: str) -> str | None:
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
            tool_name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else recipient
            label = metadata.get("tool_invoked_message")
            text = _sanitize_intermediate_text(extract_message_text(raw_message))
        elif role == "assistant" and content_type == "reasoning_recap":
            kind = "reasoning"
            label = metadata.get("reasoning_title") or "Reasoning summary"
            text = _sanitize_intermediate_text(extract_message_text(raw_message))
        elif role == "assistant" and content_type == "thoughts":
            reasoning_title = metadata.get("reasoning_title")
            if isinstance(reasoning_title, str) and reasoning_title.strip():
                kind = "reasoning"
                label = reasoning_title.strip()
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
        if revision_sensitive and node_id == current_node:
            continue

        emitted_message_ids.add(message_id)
        event = {
            "type": "canonical_intermediate_message",
            "message_id": message_id,
            "message_kind": kind,
            "text": text,
            "label": label.strip() if isinstance(label, str) and label.strip() else None,
            "tool_name": tool_name.strip() if isinstance(tool_name, str) and tool_name.strip() else None,
        }
        if submission_id is not None:
            event["submission_id"] = submission_id
        events.append(event)
    return events


def _assistant_candidates_from_payload(
    payload: dict[str, Any],
    *,
    baseline_assistant_ids: set[str] | frozenset[str],
    turn_exchange_id: str | None = None,
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
        has_finish_reason = isinstance(finish_reason, str) and bool(finish_reason.strip())
        if raw_message.get("end_turn") is not True and not has_finish_reason:
            continue
        message_id = getattr(message, "message_id", None)
        if not isinstance(message_id, str) or message_id in baseline_assistant_ids:
            continue
        if not bool(getattr(message, "text", "").strip()):
            continue
        candidates.append(message)
    return candidates


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
                if error.status_code != 404 and not transient_400 and not retryable_error:
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
                )
                for candidate in reversed(candidates):
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


def submit_browser_native(
    self: Any,
    prompt: str,
    *,
    conversation: ConversationRef | ChatConversation | dict[str, Any] | str | None = None,
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
    if normalized_attachment_paths and not _callable_accepts_attachment_paths(provider.send_text):
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
            supplied_completion_age_ms = int(time.time() * 1000) - _prewrite_canonical_completed_at_ms
        reusable_commit_completion = (
            is_continuation
            and canonical_status_before_turn == "completed"
            and isinstance(_prewrite_canonical_payload, dict)
            and isinstance(supplied_completion_age_ms, int)
            and 0 <= supplied_completion_age_ms <= _PREWRITE_CANONICAL_COMPLETION_MAX_AGE_MS
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
                canonical_status_recovery_confirm = _canonical_status_value(self, conversation)
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

    def handle_text_event(event: dict[str, Any]) -> None:
        normalized = stream_state.apply(event)
        if normalized is not None:
            normalized = {**normalized, "submission_id": submission_id}
            _emit_revision_safe_event(self, on_event, normalized)

    streaming_requested = (
        on_event is not None and _provider_supports_revision_safe_streaming(provider)
    )
    attachment_kwargs = (
        {"attachment_paths": normalized_attachment_paths}
        if normalized_attachment_paths
        else {}
    )
    model_kwargs = {"model_slug": normalized_model_slug} if normalized_model_slug else {}
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
            turn = recovery_stream_send(
                prompt,
                conversation=conversation,
                timeout=timeout,
                canonical_completed_at_ms=canonical_completed_at_ms,
                on_text_event=handle_text_event,
                **provider_kwargs,
            )
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
        if normalized_attachment_paths and not _callable_accepts_attachment_paths(stream_send):
            raise RequestError(
                "BROWSER_NATIVE_RICH_INPUT_STREAM_PROVIDER_UNSUPPORTED",
                request_stage="browser_native_turn_preflight",
            )
        turn = stream_send(
            prompt,
            conversation=conversation,
            timeout=timeout,
            on_text_event=handle_text_event,
            **provider_kwargs,
        )
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
    authority_lease_id = getattr(turn, "browser_authority_lease_id", None)
    observed_turn_exchange_id = getattr(turn, "turn_exchange_id", None)
    passive_observer_used = False

    if (
        bool(getattr(turn, "passive_observer_armed", False))
        and callable(observe_turn)
        and isinstance(authority_lease_id, str)
        and authority_lease_id
    ):
        def handle_passive_event(event: dict[str, Any]) -> None:
            if not isinstance(event, dict):
                return
            if event.get("type") == "passive_observer_heartbeat":
                return
            normalized = {**event, "submission_id": submission.submission_id}
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
            if isinstance(learned_turn_exchange_id, str) and learned_turn_exchange_id.strip():
                observed_turn_exchange_id = learned_turn_exchange_id.strip()
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
                    submission.timeout - (time.monotonic() - submission.started_monotonic),
                )
        except (RequestError, OSError, EOFError, ValueError) as error:
            self._emit_event(
                submission.on_event,
                "browser_native_passive_observer_fallback",
                submission_id=submission.submission_id,
                reason=str(error),
            )

    final_message, canonical_payload, canonical_payload_read_count = _wait_for_new_final_assistant(
        self,
        turn.conversation_id,
        baseline_assistant_ids=submission.baseline_assistant_ids,
        baseline_message_ids=submission.baseline_message_ids,
        timeout=remaining,
        interval=_PASSIVE_FINAL_RECONCILE_RETRY_SECONDS if passive_observer_used else submission.poll_interval,
        include_readback=True,
        turn_exchange_id=observed_turn_exchange_id,
        retry_400_until_timeout=retry_400_until_timeout,
        on_event=None if passive_observer_used else submission.on_event,
        submission_id=submission.submission_id,
        minimum_poll_interval=_PASSIVE_FINAL_RECONCILE_RETRY_SECONDS if passive_observer_used else None,
    )

    if canonical_payload is not None:
        result_conversation = ChatConversation(
            conversation_id=turn.conversation_id,
            message_id=final_message.message_id,
            parent_message_id=final_message.message_id,
            finish_reason=final_message.finish_reason,
            is_thinking=False,
        )
        attached = AttachedConversation.from_payload(
            canonical_payload,
            conversation=result_conversation,
        )
    else:
        attached = self.attach_conversation(turn.conversation_id)
        conversation_data = attached.conversation.to_dict()
        conversation_data.update(
            {
                "conversation_id": turn.conversation_id,
                "message_id": final_message.message_id,
                "finish_reason": final_message.finish_reason,
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
        finish_reason=final_message.finish_reason,
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
        canonical_finality_proven=True,
    )
    submission.final_response = response
    return response


def send_browser_native(
    self: Any,
    prompt: str,
    *,
    conversation: ConversationRef | ChatConversation | dict[str, Any] | str | None = None,
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
