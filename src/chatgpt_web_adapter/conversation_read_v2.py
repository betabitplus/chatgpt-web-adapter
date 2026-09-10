from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from .exceptions import RequestError
from .messages import get_messages as _legacy_get_messages
from .types import ChatConversation, ConversationRef

# Product-observed server query hint. This is not a client-side message-count
# guarantee: a live long-chat response returned far more than 20 messages while
# still requiring this smaller value to avoid an upstream HTTP 500.
CURRENT_CONVERSATION_NUM_TURNS = 20
MAX_CANONICAL_CONVERSATION_PAGES = 100


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _message_from_flat_item(item: Any) -> tuple[str, dict[str, Any], str | None]:
    if not isinstance(item, dict):
        raise RequestError(
            "canonical conversation messages[] item must be an object",
            request_stage="conversation_fetch",
        )

    nested = item.get("message")
    message = nested if isinstance(nested, dict) else item
    message_id = _optional_str(message.get("id"))
    if message_id is None:
        raise RequestError(
            "canonical conversation messages[] item missing stable message id",
            request_stage="conversation_fetch",
        )
    source_node_id = _optional_str(item.get("id")) if nested is not None else None
    return message_id, dict(message), source_node_id


def normalize_conversation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize current flat and legacy tree conversation payloads.

    CWA consumers historically interpret the canonical current branch through a
    ``mapping`` tree. The current ChatGPT read endpoint instead returns the current
    branch as an ordered, paginated ``messages[]`` list. For that wire shape we
    construct a deterministic linear mapping keyed by product-owned message ids.

    No sibling/version tree is fabricated: the synthetic parent/children links
    represent only the ordered current branch supplied by the product.
    """

    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")
    if isinstance(payload.get("mapping"), dict):
        return dict(payload)

    flat_messages = payload.get("messages")
    if not isinstance(flat_messages, list):
        raise RequestError(
            "canonical conversation response has neither mapping nor messages[]",
            request_stage="conversation_fetch",
        )

    records: list[tuple[str, dict[str, Any], str | None]] = []
    seen_ids: set[str] = set()
    source_aliases: dict[str, str] = {}
    for item in flat_messages:
        message_id, message, source_node_id = _message_from_flat_item(item)
        if message_id in seen_ids:
            raise RequestError(
                "canonical conversation messages[] contains duplicate message id",
                request_stage="conversation_fetch",
            )
        seen_ids.add(message_id)
        records.append((message_id, message, source_node_id))
        if source_node_id is not None:
            source_aliases[source_node_id] = message_id

    mapping: dict[str, dict[str, Any]] = {}
    for index, (message_id, message, _source_node_id) in enumerate(records):
        parent = records[index - 1][0] if index > 0 else None
        children = [records[index + 1][0]] if index + 1 < len(records) else []
        mapping[message_id] = {
            "id": message_id,
            "parent": parent,
            "children": children,
            "message": message,
        }

    source_current_node = _optional_str(payload.get("current_node"))
    normalized_current_node: str | None = None
    if source_current_node is not None:
        candidate = source_aliases.get(source_current_node, source_current_node)
        if candidate not in mapping:
            raise RequestError(
                "canonical conversation current_node is not present in messages[]",
                request_stage="conversation_fetch",
            )
        normalized_current_node = candidate
    elif records:
        normalized_current_node = records[-1][0]

    normalized = dict(payload)
    normalized["mapping"] = mapping
    normalized["current_node"] = normalized_current_node
    return normalized


def _flat_item_identity(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    nested = item.get("message")
    if isinstance(nested, dict):
        return _optional_str(nested.get("id")) or _optional_str(item.get("id"))
    return _optional_str(item.get("id"))


def merge_conversation_pages(
    pages_latest_first: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge paginated current-endpoint pages into one ordered wire payload."""

    if not pages_latest_first:
        raise RequestError(
            "canonical conversation pagination returned no pages",
            request_stage="conversation_fetch",
        )

    latest = pages_latest_first[0]
    expected_conversation_id = _optional_str(latest.get("conversation_id"))
    merged_messages: list[Any] = []
    seen_message_ids: set[str] = set()

    for page in reversed(pages_latest_first):
        if not isinstance(page, dict):
            raise RequestError(
                "canonical conversation pagination page must be an object",
                request_stage="conversation_fetch",
            )
        page_conversation_id = _optional_str(page.get("conversation_id"))
        if (
            expected_conversation_id is not None
            and page_conversation_id is not None
            and page_conversation_id != expected_conversation_id
        ):
            raise RequestError(
                "canonical conversation pagination identity mismatch",
                request_stage="conversation_fetch",
            )
        page_messages = page.get("messages")
        if not isinstance(page_messages, list):
            raise RequestError(
                "canonical conversation pagination page missing messages[]",
                request_stage="conversation_fetch",
            )
        for item in page_messages:
            identity = _flat_item_identity(item)
            if identity is not None and identity in seen_message_ids:
                continue
            if identity is not None:
                seen_message_ids.add(identity)
            merged_messages.append(item)

    merged = dict(latest)
    merged["messages"] = merged_messages
    return merged


def _page_cursor(payload: dict[str, Any]) -> str | None:
    page_info = payload.get("page_info")
    if (
        not isinstance(page_info, dict)
        or page_info.get("has_previous_page") is not True
    ):
        return None
    cursor = _optional_str(page_info.get("start_cursor"))
    if cursor is None:
        raise RequestError(
            "canonical conversation pagination is missing start_cursor",
            request_stage="conversation_fetch",
        )
    return cursor


def _has_previous_page(payload: dict[str, Any]) -> bool:
    page_info = payload.get("page_info")
    return isinstance(page_info, dict) and page_info.get("has_previous_page") is True


def _current_conversation_url(
    base_url: str,
    conversation_id: str,
    *,
    before: str | None = None,
) -> str:
    params: dict[str, str | int] = {
        "include_has_versions": "true",
        "num_turns": CURRENT_CONVERSATION_NUM_TURNS,
    }
    if before is not None:
        params["before"] = before
    return (
        f"{base_url.rstrip('/')}/{quote(conversation_id, safe='')}?{urlencode(params)}"
    )


def read_conversation_payload_v2(
    client: Any,
    conversation_id: str,
    *,
    current_base_url: str,
    legacy_url_template: str,
    include_all_pages: bool = False,
) -> dict[str, Any]:
    """Read the current conversation endpoint with a cohort-safe legacy fallback."""

    ref = ConversationRef(conversation_id)
    headers = client._build_headers(
        {
            "accept": "application/json",
            "referer": f"https://chatgpt.com/c/{ref.conversation_id}",
        }
    )

    current_url = _current_conversation_url(current_base_url, ref.conversation_id)
    status, data = client._json_request("GET", current_url, None, headers)
    if status == 404:
        legacy_url = legacy_url_template.format(conversation_id=ref.conversation_id)
        legacy_status, legacy_data = client._json_request(
            "GET", legacy_url, None, headers
        )
        if legacy_status >= 400:
            raise RequestError(
                f"conversation status={legacy_status}: {legacy_data}",
                status_code=legacy_status,
                endpoint="conversation",
                request_stage="conversation_fetch",
            )
        if not isinstance(legacy_data, dict):
            raise RequestError(
                "conversation response expected JSON object",
                request_stage="conversation_fetch",
            )
        return normalize_conversation_payload(legacy_data)

    if status >= 400:
        raise RequestError(
            f"conversation status={status}: {data}",
            status_code=status,
            endpoint="conversations",
            request_stage="conversation_fetch",
        )
    if not isinstance(data, dict):
        raise RequestError(
            "conversation response expected JSON object",
            request_stage="conversation_fetch",
        )

    if not include_all_pages or not isinstance(data.get("messages"), list):
        return normalize_conversation_payload(data)

    pages = [data]
    seen_cursors: set[str] = set()
    while True:
        cursor = _page_cursor(pages[-1])
        if cursor is None:
            break
        if cursor in seen_cursors:
            raise RequestError(
                "canonical conversation pagination cursor repeated",
                request_stage="conversation_fetch",
            )
        if len(pages) >= MAX_CANONICAL_CONVERSATION_PAGES:
            raise RequestError(
                "canonical conversation pagination exceeded bounded page limit",
                request_stage="conversation_fetch",
            )
        seen_cursors.add(cursor)
        page_url = _current_conversation_url(
            current_base_url,
            ref.conversation_id,
            before=cursor,
        )
        page_status, page_data = client._json_request("GET", page_url, None, headers)
        if page_status >= 400:
            raise RequestError(
                f"conversation status={page_status}: {page_data}",
                status_code=page_status,
                endpoint="conversations",
                request_stage="conversation_fetch",
            )
        if not isinstance(page_data, dict):
            raise RequestError(
                "conversation pagination response expected JSON object",
                request_stage="conversation_fetch",
            )
        pages.append(page_data)

    return normalize_conversation_payload(merge_conversation_pages(pages))


@dataclass
class _FixedConversationPayloadReader:
    payload: Any

    def _get_conversation_payload(self, _conversation_id: str) -> Any:
        return self.payload


def get_messages_v2(
    self: Any,
    url_or_id: ConversationRef | ChatConversation | dict[str, Any] | str,
    **kwargs: Any,
):
    """Use one bounded page when sufficient; paginate only when history requires it."""

    # Preserve the long-standing injected-reader seam. Tests and integrations may
    # replace ``_get_conversation_payload`` on one client instance with an exact
    # payload source. A class-level full reader must not bypass that explicit
    # instance override.
    if "_get_conversation_payload" in getattr(self, "__dict__", {}):
        return _legacy_get_messages(self, url_or_id, **kwargs)

    limit = kwargs.get("limit")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        return _legacy_get_messages(self, url_or_id, **kwargs)

    full_reader = getattr(self, "_get_full_conversation_payload", None)
    if not callable(full_reader):
        return _legacy_get_messages(self, url_or_id, **kwargs)

    ref = ConversationRef.from_any(url_or_id)
    if limit is None:
        payload = full_reader(ref.conversation_id)
        return _legacy_get_messages(_FixedConversationPayloadReader(payload), ref, **kwargs)

    latest_reader = getattr(self, "_get_conversation_payload", None)
    if not callable(latest_reader):
        return _legacy_get_messages(self, ref, **kwargs)

    latest_payload = latest_reader(ref.conversation_id)
    latest_messages = _legacy_get_messages(
        _FixedConversationPayloadReader(latest_payload),
        ref,
        **kwargs,
    )
    if (
        len(latest_messages) >= limit
        or not isinstance(latest_payload, dict)
        or not _has_previous_page(latest_payload)
    ):
        return latest_messages

    payload = full_reader(ref.conversation_id)
    return _legacy_get_messages(_FixedConversationPayloadReader(payload), ref, **kwargs)
