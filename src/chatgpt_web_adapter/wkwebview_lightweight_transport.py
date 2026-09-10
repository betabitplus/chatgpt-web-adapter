from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .exceptions import RequestError


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
    ) -> None: ...

    def wk_transport_upload_media_files(
        self, media: Sequence[tuple[Any, str | None]]
    ) -> list[dict[str, Any]]: ...


class WKLightweightTransport:
    """Low-overhead continuation/read transport used after WK proves the write."""

    def __init__(
        self,
        source_client: WKLightweightSourceClient,
        *,
        canonical_matches_write: Callable[..., bool],
        cache_final_payload: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.source_client = source_client
        self._canonical_matches_write = canonical_matches_write
        self._cache_final_payload = cache_final_payload

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
        try:
            headers = self.source_client.wk_transport_headers(
                {
                    "accept": "application/json",
                    "referer": f"https://chatgpt.com/c/{conversation_id}",
                }
            )
            curl_requests = self._curl_requests()
        except (AttributeError, RequestError):
            return None

        url = f"https://chatgpt.com/backend-api/conversation/{conversation_id}"
        try:
            with curl_requests.Session(impersonate="safari") as session:
                response = session.get(
                    url,
                    headers=headers,
                    timeout=max(1.0, float(timeout)),
                )
            if response.status_code != 200:
                return None
            payload = response.json()
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def upload_attachments(
        self,
        attachment_paths: Sequence[str],
    ) -> tuple[dict[str, Any], ...] | None:
        if not attachment_paths:
            return ()
        media = [(Path(path), Path(path).name) for path in attachment_paths]
        try:
            uploaded = self.source_client.wk_transport_upload_media_files(media)
        except Exception:
            return None
        if not isinstance(uploaded, list) or len(uploaded) != len(attachment_paths):
            return None
        descriptors: list[dict[str, Any]] = []
        for item in uploaded:
            if not isinstance(item, dict):
                return None
            file_id = item.get("file_id")
            if not isinstance(file_id, str) or not file_id.strip():
                return None
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
        with curl_requests.Session(impersonate="safari") as celsius_session:
            celsius = celsius_session.get(
                "https://chatgpt.com" + celsius_path,
                headers=celsius_headers,
                timeout=min(20.0, max(1.0, float(timeout))),
            )
            if celsius.status_code != 200:
                raise RequestError(
                    f"WKWEBVIEW_CURL_WS_CELSIUS_HTTP:{celsius.status_code}",
                    request_stage="wkwebview_curl_ws_second_leg",
                    status_code=celsius.status_code,
                )
            celsius_payload = celsius.json()
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
            )
        except AttributeError as error:
            raise RequestError(
                "WKWEBVIEW_CURL_WS_SOURCE_CONTRACT_MISSING",
                request_stage="wkwebview_curl_ws_second_leg",
            ) from error

        deadline = started + max(1.0, float(timeout))
        canonical_url = f"https://chatgpt.com/backend-api/conversation/{conversation_id}"
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
                response = canonical_session.get(
                    canonical_url,
                    headers=canonical_headers,
                    timeout=min(20.0, max(1.0, remaining)),
                )
                last_status = response.status_code
                if response.status_code in {401, 403}:
                    raise RequestError(
                        f"WKWEBVIEW_CURL_WS_CANONICAL_HTTP:{response.status_code}",
                        request_stage="wkwebview_curl_ws_second_leg",
                        status_code=response.status_code,
                    )
                if response.status_code == 200:
                    canonical_payload = response.json()
                    if isinstance(
                        canonical_payload, dict
                    ) and self._canonical_matches_write(
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
