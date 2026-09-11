from __future__ import annotations

import mimetypes
import threading
import time
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
        on_token: Callable[[str], None] | None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None: ...

    def wk_transport_upload_media_files(
        self, media: Sequence[tuple[Any, str | None]]
    ) -> list[dict[str, Any]]: ...


_LIGHTWEIGHT_MEDIA_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".webp"})
_CHAT_FILES_URL = "https://chatgpt.com/backend-api/files"
_ATTACHMENT_UPLOAD_TIMEOUT_SECONDS = 60.0


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
                time.sleep(min(0.25, max(0.05, remaining)))

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

    def _stream_resume_topic(
        self,
        *,
        conversation_id: str,
        resume_value: str,
        timeout: float,
        relay_text_event: Callable[[dict[str, Any]], None],
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

        curl_requests = self._curl_requests()
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

        raw_sequence = 0

        def on_token(value: str) -> None:
            nonlocal raw_sequence
            if not isinstance(value, str) or not value:
                return
            raw_sequence += 1
            event: dict[str, Any] = {
                "type": "assistant_text_delta",
                "sequence": raw_sequence,
                "delta": value,
            }
            message_id = state.get("message_id")
            if isinstance(message_id, str) and message_id:
                event["message_id"] = message_id
            relay_text_event(event)

        started = time.monotonic()
        try:
            self.source_client.wk_transport_stream_topic(
                topic_id,
                websocket_url=websocket_url,
                state=state,
                on_token=on_token,
                should_stop=(
                    (lambda: self._stop_requested(conversation_id))
                    if self._stop_requested is not None
                    else None
                ),
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
        text: str,
        baseline_current_node: str | None,
    ) -> dict[str, Any]:
        curl_requests, _state, raw_sequence, started = self._stream_resume_topic(
            conversation_id=conversation_id,
            resume_value=resume_value,
            timeout=timeout,
            relay_text_event=relay_text_event,
        )
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
                "ws_token_events": raw_sequence,
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
                    time.sleep(min(0.5, max(0.05, remaining)))
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
                time.sleep(min(0.5, max(0.05, remaining)))
