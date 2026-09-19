from __future__ import annotations

import asyncio
import json
import mimetypes
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .exceptions import MediaError, RequestError


class WKLightweightSourceClient(Protocol):
    """Explicit source-client contract consumed by the WK lightweight transport."""

    def wk_transport_headers(
        self, extra: dict[str, str | None] | None = None
    ) -> dict[str, str]: ...

    def wk_transport_resume_state(
        self, resume_token: str, *, conversation_id: str
    ) -> tuple[str, dict[str, Any]]: ...

    def wk_transport_stream_topic(
        self,
        topic_id: str,
        *,
        websocket_url: str,
        state: dict[str, Any],
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_token: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        stop_on_done: bool = True,
    ) -> None: ...

    def wk_transport_upload_media_files(
        self, media: Sequence[tuple[Any, str | None]]
    ) -> list[dict[str, Any]]: ...


_LIGHTWEIGHT_MEDIA_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".webp"})
_CHAT_FILES_URL = "https://chatgpt.com/backend-api/files"
_ATTACHMENT_UPLOAD_TIMEOUT_SECONDS = 60.0
_CELSIUS_URL_CACHE_SECONDS = 300.0
_FOLLOW_TOPIC_IDLE_RECONNECT_SECONDS = 12.0
_FOLLOW_TOPIC_IDLE_RECONNECT_MAX_SECONDS = 60.0
_FOLLOW_TOPIC_SERVER_QUIET_SECONDS = 120.0
_FOLLOW_TOPIC_SERVER_STALL_SECONDS = 300.0
_FOLLOW_TOPIC_RECONNECT_BACKOFF_SECONDS = (0.25, 1.0, 2.0, 5.0, 10.0, 30.0)


def _celsius_offset_key(value: str | None) -> tuple[int, int] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    head, separator, tail = value.strip().partition("-")
    try:
        milliseconds = int(head)
        sequence = int(tail) if separator and tail else 0
    except ValueError:
        return None
    return milliseconds, sequence


def _celsius_offset_is_newer(candidate: str | None, current: str | None) -> bool:
    candidate_key = _celsius_offset_key(candidate)
    if candidate_key is None:
        return False
    current_key = _celsius_offset_key(current)
    return current_key is None or candidate_key > current_key


def _celsius_offset_wallclock(value: str | None) -> float | None:
    key = _celsius_offset_key(value)
    if key is None or key[0] <= 0:
        return None
    return key[0] / 1000.0


class WKLightweightTransport:
    """Low-overhead continuation/read transport used after WK proves the write."""

    @staticmethod
    def attachment_paths_are_lightweight_eligible(
        attachment_paths: Sequence[str | Path],
    ) -> bool:
        return all(
            Path(path).suffix.lower() in _LIGHTWEIGHT_MEDIA_SUFFIXES
            for path in attachment_paths
        )

    def __init__(
        self,
        source_client: WKLightweightSourceClient,
        *,
        canonical_matches_write: Callable[..., bool],
        cache_final_payload: Callable[[str, dict[str, Any]], None],
        stop_requested: Callable[[str], bool] | None = None,
    ) -> None:
        self.source_client = source_client
        self._canonical_matches_write = canonical_matches_write
        self._cache_final_payload = cache_final_payload
        self._stop_requested = stop_requested
        self._observation_context = threading.local()
        self._celsius_cache_lock = threading.Lock()
        self._celsius_cached_url: str | None = None
        self._celsius_cached_at = 0.0
        self._completion_condition = threading.Condition()
        self._completion_sequence = 0
        self._completion_by_conversation: dict[str, int] = {}
        self._completion_thread: threading.Thread | None = None
        self._completion_ready = threading.Event()
        self._completion_error: str | None = None
        self._early_handoff_expected: dict[str, str | None] = {}
        self._early_handoff_controls: dict[str, dict[str, str | None]] = {}

    @staticmethod
    def request_error_allows_fallback(error: RequestError) -> bool:
        status_code = error.status_code
        if status_code in {408, 425, 429}:
            return True
        if isinstance(status_code, int) and status_code >= 500:
            return True
        return error.request_stage == "transport"

    @classmethod
    def resume_error_allows_passive_fallback(cls, error: RequestError) -> bool:
        if cls.request_error_allows_fallback(error):
            return True
        return str(error).startswith("WKWEBVIEW_CURL_WS_CANONICAL_TIMEOUT")

    @staticmethod
    def safe_fallback_reason(error: RequestError) -> str:
        message = str(error)
        prefix = message.split(":", 1)[0]
        if prefix.startswith("WKWEBVIEW_"):
            if isinstance(error.status_code, int):
                return f"{prefix}:HTTP_{error.status_code}"
            return prefix
        if isinstance(error.status_code, int):
            return f"{error.request_stage or 'transport'}:HTTP_{error.status_code}"
        return error.request_stage or "transport"

    def _set_canonical_fallback_reason(self, reason: str | None) -> None:
        self._observation_context.canonical_fallback_reason = reason

    def take_canonical_fallback_reason(self) -> str | None:
        reason = getattr(self._observation_context, "canonical_fallback_reason", None)
        self._observation_context.canonical_fallback_reason = None
        return reason if isinstance(reason, str) and reason else None

    @staticmethod
    def _curl_requests() -> Any:
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as error:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_DEPENDENCY_MISSING",
                request_stage="wkwebview_curl_ws_second_leg",
            ) from error
        return curl_requests

    def read_canonical(
        self,
        conversation_id: str,
        *,
        timeout: float,
    ) -> dict[str, Any] | None:
        self._set_canonical_fallback_reason(None)
        try:
            headers = self.source_client.wk_transport_headers(
                {
                    "accept": "application/json",
                    "referer": f"https://chatgpt.com/c/{conversation_id}",
                }
            )
        except AttributeError as error:
            raise RequestError(
                "WKWEBVIEW_CURL_CANONICAL_SOURCE_CONTRACT_MISSING",
                request_stage="wkwebview_canonical_read",
            ) from error
        curl_requests = self._curl_requests()

        url = f"https://chatgpt.com/backend-api/conversation/{conversation_id}"
        try:
            with curl_requests.Session(impersonate="safari") as session:
                response = session.get(
                    url,
                    headers=headers,
                    timeout=max(1.0, float(timeout)),
                )
        except curl_requests.RequestsError:
            self._set_canonical_fallback_reason("WKWEBVIEW_CURL_CANONICAL_TRANSPORT")
            return None
        if response.status_code != 200:
            error = RequestError(
                f"WKWEBVIEW_CURL_CANONICAL_HTTP:{response.status_code}",
                request_stage="wkwebview_canonical_read",
                status_code=response.status_code,
            )
            if self.request_error_allows_fallback(error):
                self._set_canonical_fallback_reason(self.safe_fallback_reason(error))
                return None
            raise error
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise RequestError(
                "WKWEBVIEW_CURL_CANONICAL_INVALID_JSON",
                request_stage="wkwebview_canonical_read",
            ) from error
        if not isinstance(payload, dict):
            raise RequestError(
                "WKWEBVIEW_CURL_CANONICAL_INVALID_SCHEMA",
                request_stage="wkwebview_canonical_read",
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
        timeout: float,
    ) -> dict[str, Any] | None:
        normalized = catalog.strip().lower() if isinstance(catalog, str) else ""
        if normalized == "models":
            path = "/backend-api/models?history_and_training_disabled=false"
        elif normalized == "conversations":
            path = (
                "/backend-api/conversations"
                f"?offset={max(0, int(offset))}"
                f"&limit={max(1, min(100, int(limit)))}"
                "&order=updated"
                f"&is_archived={'true' if is_archived else 'false'}"
                f"&is_starred={'true' if is_starred else 'false'}"
            )
        else:
            raise ValueError("catalog must be conversations or models")

        self._set_canonical_fallback_reason(None)
        try:
            headers = self.source_client.wk_transport_headers(
                {
                    "accept": "application/json",
                    "referer": "https://chatgpt.com/",
                }
            )
        except AttributeError as error:
            raise RequestError(
                "WKWEBVIEW_CURL_CATALOG_SOURCE_CONTRACT_MISSING",
                request_stage="wkwebview_catalog_read",
            ) from error
        curl_requests = self._curl_requests()
        try:
            with curl_requests.Session(impersonate="safari") as session:
                response = session.get(
                    "https://chatgpt.com" + path,
                    headers=headers,
                    timeout=max(1.0, float(timeout)),
                )
        except curl_requests.RequestsError:
            self._set_canonical_fallback_reason("WKWEBVIEW_CURL_CATALOG_TRANSPORT")
            return None
        if response.status_code != 200:
            error = RequestError(
                f"WKWEBVIEW_CURL_CATALOG_HTTP:{response.status_code}",
                request_stage="wkwebview_catalog_read",
                status_code=response.status_code,
            )
            if self.request_error_allows_fallback(error):
                self._set_canonical_fallback_reason(self.safe_fallback_reason(error))
                return None
            raise error
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise RequestError(
                "WKWEBVIEW_CURL_CATALOG_INVALID_JSON",
                request_stage="wkwebview_catalog_read",
            ) from error
        if not isinstance(payload, dict):
            raise RequestError(
                "WKWEBVIEW_CURL_CATALOG_INVALID_SCHEMA",
                request_stage="wkwebview_catalog_read",
            )
        return payload

    def wait_for_stop_status(
        self,
        conversation_id: str,
        *,
        turn_trace_id: str | None,
        timeout: float,
    ) -> str | None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        if deadline <= time.monotonic():
            return None
        try:
            headers = self.source_client.wk_transport_headers(
                {
                    "accept": "application/json",
                    "referer": f"https://chatgpt.com/c/{conversation_id}",
                    "x-oai-turn-trace-id": turn_trace_id,
                }
            )
        except AttributeError as error:
            raise RequestError(
                "WKWEBVIEW_STOP_STATUS_SOURCE_CONTRACT_MISSING",
                request_stage="wkwebview_stop_generation",
            ) from error
        curl_requests = self._curl_requests()
        url = (
            "https://chatgpt.com/backend-api/conversation/"
            f"{conversation_id}/stream_status"
        )
        with curl_requests.Session(impersonate="safari") as session:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    response = session.get(
                        url,
                        headers=headers,
                        timeout=min(5.0, max(1.0, remaining)),
                    )
                except curl_requests.RequestsError:
                    return None
                if response.status_code != 200:
                    return None
                try:
                    payload = response.json()
                except (TypeError, ValueError):
                    return None
                status = payload.get("status") if isinstance(payload, dict) else None
                if status in {"IS_STOP_REQUESTED", "COMPLETE"}:
                    return str(status)
                time.sleep(min(1.0, max(0.05, remaining)))

    def _upload_generic_file(self, path: Path) -> dict[str, Any]:
        try:
            data = path.read_bytes()
        except OSError as error:
            raise RequestError(
                "WKWEBVIEW_ATTACHMENT_FILE_READ_FAILED",
                request_stage="wkwebview_attachment_upload",
            ) from error
        mime_type = (
            mimetypes.guess_type(path.name, strict=False)[0]
            or "application/octet-stream"
        )
        create_payload = {
            "file_name": path.name,
            "file_size": len(data),
            "use_case": "multimodal",
        }
        try:
            headers = self.source_client.wk_transport_headers(
                {
                    "accept": "application/json",
                    "content-type": "application/json",
                    "referer": "https://chatgpt.com/",
                }
            )
        except AttributeError as error:
            raise RequestError(
                "WKWEBVIEW_ATTACHMENT_SOURCE_CONTRACT_MISSING",
                request_stage="wkwebview_attachment_upload",
            ) from error
        curl_requests = self._curl_requests()
        try:
            with curl_requests.Session(impersonate="safari") as session:
                created_response = session.post(
                    _CHAT_FILES_URL,
                    headers=headers,
                    json=create_payload,
                    timeout=_ATTACHMENT_UPLOAD_TIMEOUT_SECONDS,
                )
                if created_response.status_code >= 400:
                    raise RequestError(
                        f"WKWEBVIEW_ATTACHMENT_CREATE_HTTP:{created_response.status_code}",
                        request_stage="wkwebview_attachment_upload",
                        status_code=created_response.status_code,
                    )
                try:
                    created = created_response.json()
                except (TypeError, ValueError) as error:
                    raise RequestError(
                        "WKWEBVIEW_ATTACHMENT_CREATE_INVALID_JSON",
                        request_stage="wkwebview_attachment_upload",
                    ) from error
                if not isinstance(created, dict):
                    raise RequestError(
                        "WKWEBVIEW_ATTACHMENT_CREATE_INVALID_SCHEMA",
                        request_stage="wkwebview_attachment_upload",
                    )
                upload_url = created.get("upload_url")
                file_id = created.get("file_id")
                if not isinstance(upload_url, str) or not upload_url:
                    raise RequestError(
                        "WKWEBVIEW_ATTACHMENT_UPLOAD_URL_MISSING",
                        request_stage="wkwebview_attachment_upload",
                    )
                if not isinstance(file_id, str) or not file_id:
                    raise RequestError(
                        "WKWEBVIEW_ATTACHMENT_FILE_ID_MISSING",
                        request_stage="wkwebview_attachment_upload",
                    )
                upload_response = session.put(
                    upload_url,
                    headers={
                        "content-type": mime_type,
                        "origin": "https://chatgpt.com",
                        "x-ms-blob-type": "BlockBlob",
                        "x-ms-version": "2020-04-08",
                    },
                    data=data,
                    timeout=_ATTACHMENT_UPLOAD_TIMEOUT_SECONDS,
                )
                if upload_response.status_code >= 400:
                    raise RequestError(
                        f"WKWEBVIEW_ATTACHMENT_BLOB_HTTP:{upload_response.status_code}",
                        request_stage="wkwebview_attachment_upload",
                        status_code=upload_response.status_code,
                    )
                finalized_response = session.post(
                    f"{_CHAT_FILES_URL}/{file_id}/uploaded",
                    headers=headers,
                    json={},
                    timeout=_ATTACHMENT_UPLOAD_TIMEOUT_SECONDS,
                )
                if finalized_response.status_code >= 400:
                    raise RequestError(
                        f"WKWEBVIEW_ATTACHMENT_FINALIZE_HTTP:{finalized_response.status_code}",
                        request_stage="wkwebview_attachment_upload",
                        status_code=finalized_response.status_code,
                    )
        except RequestError:
            raise
        except curl_requests.RequestsError as error:
            raise RequestError(
                "WKWEBVIEW_ATTACHMENT_TRANSPORT",
                request_stage="transport",
            ) from error
        return {
            **create_payload,
            **created,
            "mime_type": mime_type,
            "width": None,
            "height": None,
        }

    def download_attachment(
        self,
        file_id: str,
        *,
        timeout: float = 30.0,
    ) -> tuple[bytes, dict[str, Any]]:
        normalized_file_id = str(file_id).strip()
        if not normalized_file_id:
            raise ValueError("file_id is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        try:
            headers = self.source_client.wk_transport_headers(
                {
                    "accept": "application/json",
                    "referer": "https://chatgpt.com/",
                }
            )
        except AttributeError as error:
            raise RequestError(
                "WKWEBVIEW_ATTACHMENT_SOURCE_CONTRACT_MISSING",
                request_stage="wkwebview_attachment_download",
            ) from error
        curl_requests = self._curl_requests()
        try:
            with curl_requests.Session(impersonate="safari") as session:
                resolver = session.get(
                    f"{_CHAT_FILES_URL}/{normalized_file_id}/download",
                    headers=headers,
                    timeout=timeout,
                )
                if resolver.status_code >= 400:
                    raise RequestError(
                        f"WKWEBVIEW_ATTACHMENT_RESOLVE_HTTP:{resolver.status_code}",
                        request_stage="wkwebview_attachment_download",
                        status_code=resolver.status_code,
                    )
                try:
                    metadata = resolver.json()
                except (TypeError, ValueError) as error:
                    raise RequestError(
                        "WKWEBVIEW_ATTACHMENT_RESOLVE_INVALID_JSON",
                        request_stage="wkwebview_attachment_download",
                    ) from error
                if not isinstance(metadata, dict):
                    raise RequestError(
                        "WKWEBVIEW_ATTACHMENT_RESOLVE_INVALID_SCHEMA",
                        request_stage="wkwebview_attachment_download",
                    )
                download_url = metadata.get("download_url")
                if not isinstance(download_url, str) or not download_url:
                    raise RequestError(
                        "WKWEBVIEW_ATTACHMENT_DOWNLOAD_URL_MISSING",
                        request_stage="wkwebview_attachment_download",
                    )
                content_headers = self.source_client.wk_transport_headers(
                    {
                        "accept": "*/*",
                        "referer": "https://chatgpt.com/",
                    }
                )
                content = session.get(
                    download_url,
                    headers=content_headers,
                    timeout=timeout,
                )
                if content.status_code >= 400:
                    raise RequestError(
                        f"WKWEBVIEW_ATTACHMENT_CONTENT_HTTP:{content.status_code}",
                        request_stage="wkwebview_attachment_download",
                        status_code=content.status_code,
                    )
                return bytes(content.content), metadata
        except RequestError:
            raise
        except curl_requests.RequestsError as error:
            raise RequestError(
                "WKWEBVIEW_ATTACHMENT_DOWNLOAD_TRANSPORT",
                request_stage="transport",
            ) from error

    def upload_attachments(
        self,
        attachment_paths: Sequence[str],
    ) -> tuple[dict[str, Any], ...] | None:
        if not attachment_paths:
            return ()
        paths = [Path(path) for path in attachment_paths]
        if self.attachment_paths_are_lightweight_eligible(paths):
            media = [(path, path.name) for path in paths]
            try:
                uploaded = self.source_client.wk_transport_upload_media_files(media)
            except AttributeError as error:
                raise RequestError(
                    "WKWEBVIEW_ATTACHMENT_SOURCE_CONTRACT_MISSING",
                    request_stage="wkwebview_attachment_upload",
                ) from error
            except OSError:
                return None
            except MediaError:
                raise
            except RequestError as error:
                if self.request_error_allows_fallback(error):
                    return None
                raise
        else:
            uploaded = [
                self._upload_generic_file(path)
                if path.suffix.lower() not in _LIGHTWEIGHT_MEDIA_SUFFIXES
                else self.source_client.wk_transport_upload_media_files(
                    [(path, path.name)]
                )[0]
                for path in paths
            ]
        if not isinstance(uploaded, list) or len(uploaded) != len(attachment_paths):
            raise RequestError(
                "WKWEBVIEW_ATTACHMENT_UPLOAD_RESULT_MISMATCH",
                request_stage="wkwebview_attachment_upload",
            )
        descriptors: list[dict[str, Any]] = []
        for item in uploaded:
            if not isinstance(item, dict):
                raise RequestError(
                    "WKWEBVIEW_ATTACHMENT_UPLOAD_INVALID_SCHEMA",
                    request_stage="wkwebview_attachment_upload",
                )
            file_id = item.get("file_id")
            if not isinstance(file_id, str) or not file_id.strip():
                raise RequestError(
                    "WKWEBVIEW_ATTACHMENT_UPLOAD_FILE_ID_MISSING",
                    request_stage="wkwebview_attachment_upload",
                )
            descriptors.append(
                {
                    "file_id": file_id.strip(),
                    "file_name": item.get("file_name")
                    if isinstance(item.get("file_name"), str)
                    else "attachment",
                    "file_size": item.get("file_size")
                    if isinstance(item.get("file_size"), int)
                    else None,
                    "mime_type": item.get("mime_type")
                    if isinstance(item.get("mime_type"), str)
                    else None,
                    "width": item.get("width")
                    if isinstance(item.get("width"), int)
                    else None,
                    "height": item.get("height")
                    if isinstance(item.get("height"), int)
                    else None,
                }
            )
        return tuple(descriptors)

    def _invalidate_celsius_websocket_url(self) -> None:
        with self._celsius_cache_lock:
            self._celsius_cached_url = None
            self._celsius_cached_at = 0.0

    def _resolve_celsius_websocket_url(self, *, timeout: float) -> tuple[Any, str]:
        curl_requests = self._curl_requests()
        now = time.monotonic()
        with self._celsius_cache_lock:
            if (
                isinstance(self._celsius_cached_url, str)
                and self._celsius_cached_url
                and now - self._celsius_cached_at < _CELSIUS_URL_CACHE_SECONDS
            ):
                return curl_requests, self._celsius_cached_url
        celsius_path = "/backend-api/celsius/ws/user"
        try:
            celsius_headers = self.source_client.wk_transport_headers(
                {
                    "accept": "*/*",
                    "referer": "https://chatgpt.com/",
                    "x-openai-target-path": celsius_path,
                    "x-openai-target-route": celsius_path,
                }
            )
        except AttributeError as error:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_SOURCE_CONTRACT_MISSING",
                request_stage="wkwebview_curl_ws_second_leg",
            ) from error
        try:
            with curl_requests.Session(impersonate="safari") as celsius_session:
                celsius = celsius_session.get(
                    "https://chatgpt.com" + celsius_path,
                    headers=celsius_headers,
                    timeout=min(20.0, max(1.0, float(timeout))),
                )
        except curl_requests.RequestsError as error:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_CELSIUS_TRANSPORT",
                request_stage="transport",
            ) from error
        if celsius.status_code != 200:
            raise RequestError(
                f"WKWEBVIEW_CURL_WS_CELSIUS_HTTP:{celsius.status_code}",
                request_stage="wkwebview_curl_ws_second_leg",
                status_code=celsius.status_code,
            )
        try:
            celsius_payload = celsius.json()
        except (TypeError, ValueError) as error:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_CELSIUS_INVALID_JSON",
                request_stage="wkwebview_curl_ws_second_leg",
            ) from error
        websocket_url = (
            celsius_payload.get("websocket_url")
            if isinstance(celsius_payload, dict)
            else None
        )
        if not isinstance(websocket_url, str) or not websocket_url:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_URL_MISSING",
                request_stage="wkwebview_curl_ws_second_leg",
            )
        with self._celsius_cache_lock:
            self._celsius_cached_url = websocket_url
            self._celsius_cached_at = time.monotonic()
        return curl_requests, websocket_url

    def _record_conversation_completion(self, conversation_id: str) -> None:
        normalized = conversation_id.strip() if isinstance(conversation_id, str) else ""
        if not normalized:
            return
        with self._completion_condition:
            self._completion_sequence += 1
            self._completion_by_conversation[normalized] = self._completion_sequence
            self._completion_condition.notify_all()

    @staticmethod
    def _conversation_completion_commands() -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "command": {
                    "type": "connect",
                    "presence": {
                        "type": "presence",
                        "state": "foreground",
                    },
                },
            },
            {
                "id": 2,
                "command": {
                    "type": "subscribe",
                    "topic_id": "conversations",
                },
            },
        ]

    @staticmethod
    def _conversation_completion_ids(raw_frame: str) -> tuple[str, ...]:
        if not isinstance(raw_frame, str) or not raw_frame:
            return ()
        try:
            items = json.loads(raw_frame)
        except ValueError:
            return ()
        if not isinstance(items, list):
            items = [items]
        conversation_ids: list[str] = []
        for item in items[:128]:
            if (
                not isinstance(item, dict)
                or item.get("type") != "message"
                or item.get("topic_id") != "conversations"
            ):
                continue
            payload = item.get("payload")
            if (
                not isinstance(payload, dict)
                or payload.get("type") != "conversation-turn-complete"
            ):
                continue
            inner = payload.get("payload")
            conversation_id = (
                inner.get("conversation_id") if isinstance(inner, dict) else None
            )
            if isinstance(conversation_id, str) and conversation_id.strip():
                conversation_ids.append(conversation_id.strip())
        return tuple(conversation_ids)

    @staticmethod
    def _early_handoff_control(raw_frame: str) -> dict[str, str | None] | None:
        if not isinstance(raw_frame, str) or not raw_frame:
            return None
        try:
            event = json.loads(raw_frame)
        except ValueError:
            return None
        if not isinstance(event, dict) or event.get("type") != (
            "conversation-turn-handoff-control"
        ):
            return None
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        attempt_id = payload.get("handoff_attempt_id")
        conversation_id = payload.get("conversation_id")
        turn_exchange_id = payload.get("turn_exchange_id")
        topic_id = payload.get("topic_id")
        if (
            not isinstance(attempt_id, str)
            or not attempt_id.strip()
            or not isinstance(conversation_id, str)
            or not conversation_id.strip()
            or not isinstance(turn_exchange_id, str)
            or not turn_exchange_id.strip()
            or not isinstance(topic_id, str)
            or not topic_id.strip()
            or not (
                topic_id.startswith("conversation-")
                or topic_id.startswith("conv-turn-low-ttl-")
            )
        ):
            return None
        server_request_id = payload.get("server_request_id")
        return {
            "attempt_id": attempt_id.strip(),
            "conversation_id": conversation_id.strip(),
            "turn_exchange_id": turn_exchange_id.strip(),
            "topic_id": topic_id.strip(),
            "server_request_id": (
                server_request_id.strip()
                if isinstance(server_request_id, str) and server_request_id.strip()
                else None
            ),
        }

    def _record_early_handoff_control(
        self, control: dict[str, str | None]
    ) -> None:
        attempt_id = control.get("attempt_id")
        conversation_id = control.get("conversation_id")
        if not isinstance(attempt_id, str) or not isinstance(conversation_id, str):
            return
        with self._completion_condition:
            if attempt_id not in self._early_handoff_expected:
                return
            expected_conversation_id = self._early_handoff_expected[attempt_id]
            if (
                expected_conversation_id is not None
                and expected_conversation_id != conversation_id
            ):
                return
            self._early_handoff_controls[attempt_id] = dict(control)
            self._completion_condition.notify_all()

    def arm_early_handoff(self, conversation_id: str | None = None) -> str | None:
        if conversation_id is None:
            normalized: str | None = None
        elif isinstance(conversation_id, str):
            normalized = conversation_id.strip()
            if not normalized:
                return None
        else:
            return None
        attempt_id = str(uuid.uuid4())
        with self._completion_condition:
            if self._completion_error is not None:
                return None
            self._early_handoff_expected[attempt_id] = normalized
            self._early_handoff_controls.pop(attempt_id, None)
        return attempt_id

    def early_handoff_control(
        self, attempt_id: str, *, consume: bool = False
    ) -> dict[str, str | None] | None:
        normalized = attempt_id.strip() if isinstance(attempt_id, str) else ""
        if not normalized:
            return None
        with self._completion_condition:
            value = self._early_handoff_controls.get(normalized)
            if value is None:
                return None
            result = dict(value)
            if consume:
                self._early_handoff_controls.pop(normalized, None)
                self._early_handoff_expected.pop(normalized, None)
            return result

    def release_early_handoff(self, attempt_id: str | None) -> None:
        normalized = attempt_id.strip() if isinstance(attempt_id, str) else ""
        if not normalized:
            return
        with self._completion_condition:
            self._early_handoff_controls.pop(normalized, None)
            self._early_handoff_expected.pop(normalized, None)

    async def _run_conversation_completion_socket(self) -> None:
        import websockets

        _curl_requests, websocket_url = self._resolve_celsius_websocket_url(
            timeout=20.0
        )
        try:
            headers = self.source_client.wk_transport_headers(
                {"origin": "https://chatgpt.com"}
            )
        except AttributeError as error:
            raise RequestError(
                "WKWEBVIEW_GLOBAL_WS_SOURCE_CONTRACT_MISSING",
                request_stage="wkwebview_global_completion_observer",
            ) from error

        async with websockets.connect(
            websocket_url,
            additional_headers=headers,
            open_timeout=10,
            close_timeout=0.25,
            ping_interval=None,
            ping_timeout=None,
            max_size=None,
        ) as websocket:
            await websocket.send(
                json.dumps(
                    self._conversation_completion_commands(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            self._completion_ready.set()
            while True:
                raw_frame = await websocket.recv()
                if not isinstance(raw_frame, str):
                    continue
                control = self._early_handoff_control(raw_frame)
                if control is not None:
                    self._record_early_handoff_control(control)
                for conversation_id in self._conversation_completion_ids(raw_frame):
                    self._record_conversation_completion(conversation_id)

    def _conversation_completion_observer_main(self) -> None:
        error: str | None = None
        try:
            asyncio.run(self._run_conversation_completion_socket())
        except Exception as exc:
            error = type(exc).__name__
        finally:
            with self._completion_condition:
                self._completion_error = error
                self._completion_condition.notify_all()
            self._completion_ready.set()

    def ensure_conversation_completion_observer(
        self,
        *,
        timeout: float = 12.0,
    ) -> bool:
        with self._completion_condition:
            thread = self._completion_thread
            if thread is None or not thread.is_alive():
                self._completion_ready.clear()
                self._completion_error = None
                thread = threading.Thread(
                    target=self._conversation_completion_observer_main,
                    name="cwa-wk-conversation-completion",
                    daemon=True,
                )
                self._completion_thread = thread
                thread.start()
        if not self._completion_ready.wait(max(0.1, float(timeout))):
            return False
        with self._completion_condition:
            return self._completion_error is None

    def arm_conversation_completion(
        self,
        conversation_id: str,
        *,
        timeout: float = 12.0,
    ) -> int | None:
        normalized = conversation_id.strip() if isinstance(conversation_id, str) else ""
        if not normalized:
            return None
        if not self.ensure_conversation_completion_observer(timeout=timeout):
            return None
        with self._completion_condition:
            return self._completion_sequence

    def current_completion_sequence(self) -> int:
        with self._completion_condition:
            return self._completion_sequence

    def conversation_completion_sequence(self, conversation_id: str) -> int:
        normalized = conversation_id.strip() if isinstance(conversation_id, str) else ""
        if not normalized:
            return 0
        with self._completion_condition:
            return self._completion_by_conversation.get(normalized, 0)

    def conversation_completion_observed(
        self,
        conversation_id: str,
        *,
        after_sequence: int,
    ) -> bool:
        return self.conversation_completion_sequence(conversation_id) > int(
            after_sequence
        )

    def wait_for_conversation_completion(
        self,
        conversation_id: str,
        *,
        after_sequence: int,
        timeout: float,
        should_stop: Callable[[], bool] | None = None,
    ) -> bool:
        normalized = conversation_id.strip() if isinstance(conversation_id, str) else ""
        if not normalized:
            return False
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._completion_condition:
            while True:
                latest = self._completion_by_conversation.get(normalized, 0)
                if latest > int(after_sequence):
                    return True
                if self._completion_error is not None:
                    return False
                if should_stop is not None:
                    try:
                        if bool(should_stop()):
                            return False
                    except Exception:
                        pass
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._completion_condition.wait(timeout=min(0.25, remaining))

    def _stream_resume_topic(
        self,
        *,
        conversation_id: str,
        resume_value: str,
        timeout: float,
        relay_text_event: Callable[[dict[str, Any]], None],
        on_transport_event: Callable[[dict[str, Any]], None] | None = None,
        stream_should_stop: Callable[[], bool] | None = None,
        passive_completion_check: Callable[[], bool] | None = None,
    ) -> tuple[Any, dict[str, Any], int, float]:
        try:
            topic_id, state = self.source_client.wk_transport_resume_state(
                resume_value,
                conversation_id=conversation_id,
            )
        except AttributeError as error:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_SOURCE_CONTRACT_MISSING",
                request_stage="wkwebview_curl_ws_second_leg",
            ) from error
        if not isinstance(topic_id, str) or not topic_id:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_TOPIC_UNRESOLVED",
                request_stage="wkwebview_curl_ws_second_leg",
            )

        curl_requests, websocket_url = self._resolve_celsius_websocket_url(
            timeout=timeout
        )

        raw_sequence = 0
        raw_live_observer = callable(on_transport_event) and callable(
            stream_should_stop
        )

        def on_token(value: str) -> None:
            nonlocal raw_sequence
            if not isinstance(value, str) or not value:
                return
            raw_sequence += 1
            if raw_live_observer:
                return
            event: dict[str, Any] = {
                "type": "assistant_text_delta",
                "sequence": raw_sequence,
                "delta": value,
            }
            message_id = state.get("message_id")
            if isinstance(message_id, str) and message_id:
                event["message_id"] = message_id
            relay_text_event(event)

        def should_stop() -> bool:
            if self._stop_requested is not None and self._stop_requested(
                conversation_id
            ):
                return True
            if raw_live_observer and stream_should_stop is not None:
                try:
                    if bool(stream_should_stop()):
                        state["stream_terminal_observed"] = True
                        state.setdefault("finish_reason", "stream_terminal")
                        return True
                except Exception:
                    pass
            if passive_completion_check is not None:
                try:
                    passive_terminal = bool(passive_completion_check())
                except Exception:
                    passive_terminal = False
                if passive_terminal:
                    state["stream_terminal_observed"] = True
                    state.setdefault("finish_reason", "conversation_turn_complete")
                    return True
            return False

        stream_kwargs: dict[str, Any] = {
            "on_token": on_token,
            "should_stop": should_stop,
        }
        if raw_live_observer:
            stream_kwargs["on_event"] = on_transport_event
            stream_kwargs["stop_on_done"] = False

        started = time.monotonic()
        try:
            self.source_client.wk_transport_stream_topic(
                topic_id,
                websocket_url=websocket_url,
                state=state,
                **stream_kwargs,
            )
        except AttributeError as error:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_SOURCE_CONTRACT_MISSING",
                request_stage="wkwebview_curl_ws_second_leg",
            ) from error
        except RequestError as error:
            if error.request_stage is not None or error.status_code is not None:
                raise
            raise RequestError(
                str(error),
                request_stage="transport",
            ) from error
        return curl_requests, state, raw_sequence, started

    def follow_topic(
        self,
        *,
        conversation_id: str,
        topic_id: str,
        timeout: float,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_token: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        passive_completion_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        normalized_topic = topic_id.strip() if isinstance(topic_id, str) else ""
        if not normalized_topic:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_TOPIC_UNRESOLVED",
                request_stage="wkwebview_curl_ws_follow",
            )

        state: dict[str, Any] = {
            "conversation_id": conversation_id,
            "resume_turn_topic_id": normalized_topic,
        }
        segment_done_count = 0
        external_completion_observed = False
        caller_stop_observed = False
        deadline_expired = False
        reconnect_count = 0
        reconnect_reason: str | None = None
        started = time.monotonic()
        deadline = started + max(1.0, float(timeout))
        last_topic_activity_at = started
        last_server_activity_at = started
        last_server_offset: str | None = None
        last_server_wallclock_at: float | None = None
        server_quiet_emitted = False
        server_stalled_emitted = False
        delivery_recovery_count = 0
        idle_reconnect_seconds = _FOLLOW_TOPIC_IDLE_RECONNECT_SECONDS

        def server_idle_seconds(now: float | None = None) -> float:
            current = time.monotonic() if now is None else now
            return max(0.0, current - last_server_activity_at)

        def emit_transport_event(event: dict[str, Any]) -> None:
            if on_event is not None:
                on_event(event)

        def relay_event(event: dict[str, Any]) -> None:
            nonlocal segment_done_count, last_topic_activity_at, idle_reconnect_seconds
            nonlocal last_server_activity_at, last_server_offset
            nonlocal last_server_wallclock_at, server_quiet_emitted
            nonlocal server_stalled_emitted, delivery_recovery_count
            if not isinstance(event, dict):
                return
            event_type = event.get("type")
            now = time.monotonic()
            if event_type in {
                "stream_handoff_ws_subscribed",
                "raw_ws_event",
                "raw_ws_done",
            }:
                last_topic_activity_at = now

            if event_type in {"raw_ws_event", "raw_ws_done"}:
                offset = event.get("offset")
                normalized_offset = (
                    offset.strip()
                    if isinstance(offset, str) and offset.strip()
                    else None
                )
                progressed = (
                    normalized_offset is None
                    or _celsius_offset_is_newer(normalized_offset, last_server_offset)
                )
                if progressed:
                    silent_for = server_idle_seconds(now)
                    if normalized_offset is not None:
                        last_server_offset = normalized_offset
                        observed_wallclock = _celsius_offset_wallclock(normalized_offset)
                        if observed_wallclock is not None:
                            last_server_wallclock_at = observed_wallclock
                            observed_age = max(
                                0.0,
                                time.time() - last_server_wallclock_at,
                            )
                            last_server_activity_at = now - observed_age
                        else:
                            last_server_activity_at = now
                    else:
                        last_server_activity_at = now
                    if server_quiet_emitted or server_stalled_emitted:
                        emit_transport_event(
                            {
                                "type": "stream_handoff_server_resumed",
                                "topic_id": normalized_topic,
                                "silent_seconds": silent_for,
                                "last_offset": last_server_offset,
                            }
                        )
                    server_quiet_emitted = False
                    server_stalled_emitted = False

            if event_type == "stream_handoff_ws_subscribed":
                reported_offset = event.get("last_offset")
                normalized_reported_offset = (
                    reported_offset.strip()
                    if isinstance(reported_offset, str) and reported_offset.strip()
                    else None
                )
                reported_progress = _celsius_offset_is_newer(
                    normalized_reported_offset,
                    last_server_offset,
                )
                if reported_progress and normalized_reported_offset is not None:
                    silent_for = server_idle_seconds(now)
                    last_server_offset = normalized_reported_offset
                    observed_wallclock = _celsius_offset_wallclock(last_server_offset)
                    if observed_wallclock is not None:
                        last_server_wallclock_at = observed_wallclock
                        observed_age = max(
                            0.0,
                            time.time() - last_server_wallclock_at,
                        )
                        last_server_activity_at = now - observed_age
                    else:
                        last_server_activity_at = now
                    if server_quiet_emitted or server_stalled_emitted:
                        emit_transport_event(
                            {
                                "type": "stream_handoff_server_resumed",
                                "topic_id": normalized_topic,
                                "silent_seconds": silent_for,
                                "last_offset": last_server_offset,
                            }
                        )
                    server_quiet_emitted = False
                    server_stalled_emitted = False
                if reconnect_count > 0:
                    catchup_count = event.get("catchup_count")
                    if isinstance(catchup_count, int) and not isinstance(
                        catchup_count, bool
                    ):
                        if catchup_count > 0 and reported_progress:
                            idle_reconnect_seconds = _FOLLOW_TOPIC_IDLE_RECONNECT_SECONDS
                            delivery_recovery_count += 1
                            emit_transport_event(
                                {
                                    "type": "stream_handoff_delivery_recovered",
                                    "topic_id": normalized_topic,
                                    "attempt": reconnect_count,
                                    "catchup_count": catchup_count,
                                    "last_offset": last_server_offset,
                                }
                            )
                        elif catchup_count <= 0:
                            idle_reconnect_seconds = min(
                                _FOLLOW_TOPIC_IDLE_RECONNECT_MAX_SECONDS,
                                max(
                                    _FOLLOW_TOPIC_IDLE_RECONNECT_SECONDS,
                                    idle_reconnect_seconds * 2.0,
                                ),
                            )
            if event_type == "raw_ws_done":
                segment_done_count += 1
            emit_transport_event(event)

        while True:
            reconnect_requested = False

            def topic_should_stop() -> bool:
                nonlocal external_completion_observed
                nonlocal caller_stop_observed
                nonlocal deadline_expired
                nonlocal reconnect_requested
                nonlocal reconnect_reason
                nonlocal server_quiet_emitted
                nonlocal server_stalled_emitted
                if should_stop is not None:
                    try:
                        if bool(should_stop()):
                            caller_stop_observed = True
                            state["stream_terminal_observed"] = True
                            state.setdefault("finish_reason", "stream_terminal")
                            return True
                    except Exception:
                        pass
                if passive_completion_check is not None:
                    try:
                        terminal = bool(passive_completion_check())
                    except Exception:
                        terminal = False
                    if terminal:
                        external_completion_observed = True
                        state["stream_terminal_observed"] = True
                        state["finish_reason"] = "conversation_turn_complete"
                        return True
                now = time.monotonic()
                silent_for = server_idle_seconds(now)
                offset_age = (
                    max(0.0, time.time() - last_server_wallclock_at)
                    if last_server_wallclock_at is not None
                    else None
                )
                if (
                    not server_quiet_emitted
                    and silent_for >= _FOLLOW_TOPIC_SERVER_QUIET_SECONDS
                ):
                    server_quiet_emitted = True
                    emit_transport_event(
                        {
                            "type": "stream_handoff_server_quiet",
                            "topic_id": normalized_topic,
                            "server_idle_seconds": silent_for,
                            "last_offset": last_server_offset,
                            "last_offset_age_seconds": offset_age,
                            "reconnect_count": reconnect_count,
                        }
                    )
                if (
                    not server_stalled_emitted
                    and silent_for >= _FOLLOW_TOPIC_SERVER_STALL_SECONDS
                ):
                    server_stalled_emitted = True
                    emit_transport_event(
                        {
                            "type": "stream_handoff_server_stalled",
                            "topic_id": normalized_topic,
                            "server_idle_seconds": silent_for,
                            "last_offset": last_server_offset,
                            "last_offset_age_seconds": offset_age,
                            "reconnect_count": reconnect_count,
                        }
                    )
                if now >= deadline:
                    deadline_expired = True
                    return True
                if now - last_topic_activity_at >= idle_reconnect_seconds:
                    reconnect_requested = True
                    reconnect_reason = "topic_idle"
                    return True
                return False

            remaining = max(1.0, deadline - time.monotonic())
            try:
                _curl_requests, websocket_url = self._resolve_celsius_websocket_url(
                    timeout=remaining
                )
                self.source_client.wk_transport_stream_topic(
                    normalized_topic,
                    websocket_url=websocket_url,
                    state=state,
                    on_event=relay_event,
                    on_token=on_token,
                    should_stop=topic_should_stop,
                    stop_on_done=False,
                )
            except AttributeError as error:
                raise RequestError(
                    "WKWEBVIEW_CURL_WS_SOURCE_CONTRACT_MISSING",
                    request_stage="wkwebview_curl_ws_follow",
                ) from error
            except RequestError as error:
                if time.monotonic() >= deadline:
                    raise
                retryable_stream_error = (
                    error.request_stage is None
                    or self.request_error_allows_fallback(error)
                )
                if not retryable_stream_error:
                    raise
                reconnect_requested = True
                reconnect_reason = self.safe_fallback_reason(error)

            if external_completion_observed or caller_stop_observed:
                break
            if not reconnect_requested:
                break

            reconnect_count += 1
            if reconnect_reason != "topic_idle":
                self._invalidate_celsius_websocket_url()
            if on_event is not None:
                on_event(
                    {
                        "type": "stream_handoff_ws_reconnecting",
                        "topic_id": normalized_topic,
                        "reason": reconnect_reason or "transport",
                        "attempt": reconnect_count,
                        "server_idle_seconds": server_idle_seconds(),
                        "last_offset": last_server_offset,
                    }
                )
            backoff_index = min(
                reconnect_count - 1,
                len(_FOLLOW_TOPIC_RECONNECT_BACKOFF_SECONDS) - 1,
            )
            sleep_for = _FOLLOW_TOPIC_RECONNECT_BACKOFF_SECONDS[backoff_index]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                caller_stop_observed = True
                break
            time.sleep(min(sleep_for, remaining))
            last_topic_activity_at = time.monotonic()

        message_id = state.get("message_id")
        finish_reason = state.get("finish_reason")
        stream_finality_proven = False
        if not external_completion_observed and not deadline_expired:
            stream_finality_proven = caller_stop_observed or (
                segment_done_count > 0
                and isinstance(message_id, str)
                and bool(message_id.strip())
                and isinstance(finish_reason, str)
                and bool(finish_reason.strip())
            )
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "topic_id": normalized_topic,
            "message_id": message_id,
            "turn_exchange_id": state.get("turn_exchange_id"),
            "finish_reason": finish_reason,
            "observed_model": state.get("observed_model"),
            "observed_reasoning_effort": state.get("observed_reasoning_effort"),
            "stream_finality_proven": stream_finality_proven,
            "external_completion_observed": external_completion_observed,
            "caller_stop_observed": caller_stop_observed,
            "completed": stream_finality_proven or external_completion_observed,
            "segment_done_count": segment_done_count,
            "reconnect_count": reconnect_count,
            "reconnect_reason": reconnect_reason,
            "idle_reconnect_seconds": idle_reconnect_seconds,
            "server_idle_seconds": server_idle_seconds(),
            "server_quiet_observed": server_quiet_emitted,
            "server_stalled_observed": server_stalled_emitted,
            "last_server_offset": last_server_offset,
            "delivery_recovery_count": delivery_recovery_count,
            "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
        }

    def stream_temporary_turn(
        self,
        *,
        conversation_id: str,
        resume_value: str,
        timeout: float,
        relay_text_event: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        _curl_requests, state, raw_sequence, started = self._stream_resume_topic(
            conversation_id=conversation_id,
            resume_value=resume_value,
            timeout=timeout,
            relay_text_event=relay_text_event,
        )
        message_id = state.get("message_id")
        if (
            raw_sequence <= 0
            or not isinstance(message_id, str)
            or not message_id.strip()
        ):
            raise RequestError(
                "WKWEBVIEW_TEMPORARY_STREAM_FINALITY_MISSING",
                request_stage="wkwebview_temporary_stream",
            )
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "message_id": message_id.strip(),
            "parent_message_id": (
                state.get("parent_message_id")
                if isinstance(state.get("parent_message_id"), str)
                else None
            ),
            "finish_reason": (
                state.get("finish_reason")
                if isinstance(state.get("finish_reason"), str)
                else None
            ),
            "turn_exchange_id": (
                state.get("turn_exchange_id")
                if isinstance(state.get("turn_exchange_id"), str)
                else None
            ),
            "ws_token_events": raw_sequence,
            "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
        }

    def resume_turn(
        self,
        *,
        conversation_id: str,
        resume_value: str,
        timeout: float,
        relay_text_event: Callable[[dict[str, Any]], None],
        on_transport_event: Callable[[dict[str, Any]], None] | None = None,
        stream_should_stop: Callable[[], bool] | None = None,
        passive_completion_check: Callable[[], bool] | None = None,
        text: str,
        baseline_current_node: str | None,
    ) -> dict[str, Any]:
        curl_requests, state, raw_sequence, started = self._stream_resume_topic(
            conversation_id=conversation_id,
            resume_value=resume_value,
            timeout=timeout,
            relay_text_event=relay_text_event,
            on_transport_event=on_transport_event,
            stream_should_stop=stream_should_stop,
            passive_completion_check=passive_completion_check,
        )
        message_id = state.get("message_id")
        finish_reason = state.get("finish_reason")
        observed_model = state.get("observed_model")
        observed_effort = state.get("observed_reasoning_effort")
        if self._stop_requested is not None and self._stop_requested(conversation_id):
            return {
                "ok": True,
                "status": 200,
                "conversation_id": conversation_id,
                "canonical_completed": False,
                "stream_started": True,
                "stream_ended": True,
                "stream_terminal_observed": True,
                "stop_requested": True,
                "message_id": message_id,
                "finish_reason": finish_reason,
                "observed_model": observed_model,
                "observed_reasoning_effort": observed_effort,
                "ws_token_events": raw_sequence,
            }
        legacy_terminal = (
            raw_sequence > 0
            and isinstance(message_id, str)
            and bool(message_id.strip())
            and isinstance(finish_reason, str)
            and bool(finish_reason.strip())
        )
        passive_terminal = (
            state.get("stream_terminal_observed") is True
            and isinstance(finish_reason, str)
            and bool(finish_reason.strip())
        )
        if legacy_terminal or passive_terminal:
            return {
                "ok": True,
                "status": 200,
                "conversation_id": conversation_id,
                "canonical_completed": False,
                "stream_started": True,
                "stream_ended": True,
                "stream_terminal_observed": True,
                "stream_finality_proven": True,
                "message_id": (
                    message_id.strip()
                    if isinstance(message_id, str) and message_id.strip()
                    else None
                ),
                "finish_reason": finish_reason.strip(),
                "observed_model": observed_model,
                "observed_reasoning_effort": observed_effort,
                "ws_token_events": raw_sequence,
                "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
            }
        deadline = started + max(1.0, float(timeout))
        canonical_url = (
            f"https://chatgpt.com/backend-api/conversation/{conversation_id}"
        )
        try:
            canonical_headers = self.source_client.wk_transport_headers(
                {
                    "accept": "application/json",
                    "referer": f"https://chatgpt.com/c/{conversation_id}",
                }
            )
        except AttributeError as error:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_SOURCE_CONTRACT_MISSING",
                request_stage="wkwebview_curl_ws_second_leg",
            ) from error
        last_status = 0
        retry_delay = 1.0
        with curl_requests.Session(impersonate="safari") as canonical_session:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RequestError(
                        f"WKWEBVIEW_CURL_WS_CANONICAL_TIMEOUT:last_status={last_status}",
                        request_stage="wkwebview_curl_ws_second_leg",
                    )
                try:
                    response = canonical_session.get(
                        canonical_url,
                        headers=canonical_headers,
                        timeout=min(20.0, max(1.0, remaining)),
                    )
                except curl_requests.RequestsError as error:
                    raise RequestError(
                        "WKWEBVIEW_CURL_WS_CANONICAL_TRANSPORT",
                        request_stage="transport",
                    ) from error
                last_status = response.status_code
                if response.status_code != 200:
                    error = RequestError(
                        f"WKWEBVIEW_CURL_WS_CANONICAL_HTTP:{response.status_code}",
                        request_stage="wkwebview_curl_ws_second_leg",
                        status_code=response.status_code,
                    )
                    if not self.request_error_allows_fallback(error):
                        raise error
                    sleep_delay = 60.0 if response.status_code == 429 else retry_delay
                    time.sleep(min(sleep_delay, max(0.05, remaining)))
                    if response.status_code != 429:
                        retry_delay = min(retry_delay * 2.0, 8.0)
                    continue
                try:
                    canonical_payload = response.json()
                except (TypeError, ValueError) as error:
                    raise RequestError(
                        "WKWEBVIEW_CURL_WS_CANONICAL_INVALID_JSON",
                        request_stage="wkwebview_curl_ws_second_leg",
                    ) from error
                if not isinstance(canonical_payload, dict):
                    raise RequestError(
                        "WKWEBVIEW_CURL_WS_CANONICAL_INVALID_SCHEMA",
                        request_stage="wkwebview_curl_ws_second_leg",
                    )
                if self._canonical_matches_write(
                    canonical_payload,
                    text=text,
                    baseline_current_node=baseline_current_node,
                ):
                    self._cache_final_payload(conversation_id, canonical_payload)
                    return {
                        "ok": True,
                        "status": 200,
                        "conversation_id": conversation_id,
                        "canonical_completed": True,
                        "stream_started": True,
                        "stream_ended": True,
                        "stream_terminal_observed": True,
                        "ws_token_events": raw_sequence,
                    }
                time.sleep(min(retry_delay, max(0.05, remaining)))
                retry_delay = min(retry_delay * 2.0, 8.0)
