from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .browser_native_provider import BrowserNativeTurnResult
from .exceptions import RequestError
from .types import ConversationRef
from .wkwebview_lightweight_transport import WKLightweightTransport


@dataclass(frozen=True)
class PreparedWKTurn:
    conversation_id: str | None
    baseline_current_node: str | None
    attachments: tuple[str, ...]
    use_minimal_security_shell: bool
    minimal_parent_message_id: str | None
    minimal_model_slug: str | None
    minimal_thinking_effort: str | None
    minimal_attachment_descriptors: tuple[dict[str, Any], ...]
    canonical_read_transport: str | None
    canonical_read_fallback_reason: str | None


@dataclass(frozen=True)
class WKPhaseOneResult:
    conversation_id: str
    response_status: int
    attachment_count: int
    phase_one_final_cached: bool


class WKTurnOrchestrator:
    """Coordinate one WK protected write and its lightweight continuation."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    @staticmethod
    def stream_relay_factory(
        on_text_event: Callable[[dict[str, Any]], None],
    ) -> Callable[[], Callable[[dict[str, Any]], None]]:
        stream_sequence = 0

        def make_stream_relay() -> Callable[[dict[str, Any]], None]:
            phase_last_sequence = 0

            def relay(event: dict[str, Any]) -> None:
                nonlocal stream_sequence, phase_last_sequence
                if not isinstance(event, dict):
                    return
                raw_sequence = event.get("sequence")
                if (
                    isinstance(raw_sequence, int)
                    and not isinstance(raw_sequence, bool)
                    and raw_sequence > 0
                ):
                    if phase_last_sequence and raw_sequence <= phase_last_sequence:
                        return
                    increment = (
                        raw_sequence - phase_last_sequence if phase_last_sequence else 1
                    )
                    phase_last_sequence = raw_sequence
                else:
                    increment = 1
                stream_sequence += max(1, increment)
                normalized = dict(event)
                normalized["sequence"] = stream_sequence
                on_text_event(normalized)

            return relay

        return make_stream_relay

    @staticmethod
    def _payload_contains_client_message(
        payload: dict[str, Any],
        *,
        client_message_id: str,
        text: str,
    ) -> bool:
        mapping = payload.get("mapping")
        if not isinstance(mapping, dict):
            return False
        expected_text = text.strip()
        for node in mapping.values():
            if not isinstance(node, dict):
                continue
            message = node.get("message")
            if not isinstance(message, dict) or message.get("id") != client_message_id:
                continue
            author = message.get("author")
            if not isinstance(author, dict) or author.get("role") != "user":
                continue
            content = message.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                return False
            rendered = "\n".join(
                part for part in parts if isinstance(part, str)
            ).strip()
            return rendered == expected_text
        return False

    def recover_phase_one_identity(
        self,
        payload: dict[str, Any],
        *,
        prepared: PreparedWKTurn,
        text: str,
        total_timeout: float,
        started: float,
    ) -> dict[str, Any]:
        if payload.get("identity_recovery_required") is not True:
            return payload

        client_message_id = payload.get("client_message_id")
        if not isinstance(client_message_id, str) or not client_message_id.strip():
            raise RequestError(
                "WKWEBVIEW_IDENTITY_RECOVERY_MESSAGE_ID_MISSING",
                request_stage="wkwebview_authority_turn",
            )
        client_message_id = client_message_id.strip()
        provider = self.provider
        transport = provider._lightweight_transport
        if transport is None:
            raise RequestError(
                "WKWEBVIEW_IDENTITY_RECOVERY_LIGHTWEIGHT_UNAVAILABLE",
                request_stage="wkwebview_identity_recovery",
            )
        deadline = started + max(1.0, float(total_timeout))
        candidate = payload.get("conversation_id")
        candidate_id = (
            candidate.strip()
            if isinstance(candidate, str) and candidate.strip()
            else prepared.conversation_id
        )
        matched_payload: dict[str, Any] | None = None
        retry_delay = 1.0

        def wait_before_retry() -> None:
            nonlocal retry_delay
            remaining_wait = max(0.0, deadline - time.monotonic())
            if remaining_wait <= 0:
                return
            time.sleep(min(retry_delay, remaining_wait))
            retry_delay = min(retry_delay * 2.0, 8.0)

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RequestError(
                    "WKWEBVIEW_IDENTITY_RECOVERY_TIMEOUT",
                    request_stage="wkwebview_identity_recovery",
                )

            if candidate_id is not None:
                canonical = provider._read_conversation_payload_via_curl(
                    candidate_id,
                    timeout=min(8.0, max(1.0, remaining)),
                )
                if not isinstance(canonical, dict):
                    wait_before_retry()
                    continue
                if self._payload_contains_client_message(
                    canonical,
                    client_message_id=client_message_id,
                    text=text,
                ):
                    matched_payload = canonical
                    if provider._canonical_state.payload_is_final(canonical):
                        provider._cache_final_payload(candidate_id, canonical)
                        payload.update(
                            {
                                "conversation_id": candidate_id,
                                "write_commit_proven": True,
                                "write_commit_proof": "CANONICAL_MESSAGE_ID_RECOVERY",
                                "canonical_committed": True,
                                "canonical_final_completed": True,
                                "committed_current_node": canonical.get("current_node")
                                if isinstance(canonical.get("current_node"), str)
                                else "",
                                "stream_terminal_observed": True,
                                "_cwa_identity_recovered": True,
                                "_cwa_identity_recovery_transport": "curl_cffi",
                                "_cwa_identity_recovery_elapsed_ms": int(
                                    (time.monotonic() - started) * 1000
                                ),
                            }
                        )
                        return payload
                    wait_before_retry()
                    continue
                if prepared.conversation_id is not None:
                    wait_before_retry()
                    continue
                candidate_id = None

            catalog = transport.read_catalog(
                "conversations",
                offset=0,
                limit=12,
                timeout=min(8.0, max(1.0, remaining)),
            )
            if not isinstance(catalog, dict):
                wait_before_retry()
                continue
            raw_items = catalog.get("items")
            items = raw_items if isinstance(raw_items, list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                value = item.get("id")
                if not isinstance(value, str) or not value.strip():
                    continue
                conversation_id = value.strip()
                scan_remaining = deadline - time.monotonic()
                if scan_remaining <= 0:
                    raise RequestError(
                        "WKWEBVIEW_IDENTITY_RECOVERY_TIMEOUT",
                        request_stage="wkwebview_identity_recovery",
                    )
                canonical = provider._read_conversation_payload_via_curl(
                    conversation_id,
                    timeout=min(6.0, max(1.0, scan_remaining)),
                )
                if not isinstance(canonical, dict):
                    continue
                if self._payload_contains_client_message(
                    canonical,
                    client_message_id=client_message_id,
                    text=text,
                ):
                    candidate_id = conversation_id
                    matched_payload = canonical
                    break

            if candidate_id is None:
                wait_before_retry()
                continue

            if (
                matched_payload is not None
                and provider._canonical_state.payload_is_final(matched_payload)
            ):
                provider._cache_final_payload(candidate_id, matched_payload)
                payload.update(
                    {
                        "conversation_id": candidate_id,
                        "write_commit_proven": True,
                        "write_commit_proof": "CANONICAL_MESSAGE_ID_RECOVERY",
                        "canonical_committed": True,
                        "canonical_final_completed": True,
                        "committed_current_node": matched_payload.get("current_node")
                        if isinstance(matched_payload.get("current_node"), str)
                        else "",
                        "stream_terminal_observed": True,
                        "_cwa_identity_recovered": True,
                        "_cwa_identity_recovery_transport": "curl_cffi",
                        "_cwa_identity_recovery_elapsed_ms": int(
                            (time.monotonic() - started) * 1000
                        ),
                    }
                )
                return payload
            matched_payload = None

    def prepare_turn(
        self,
        *,
        conversation: Any,
        total_timeout: float,
        attachment_paths: Sequence[str | Path] | None,
        model_slug: str | None,
        streaming: bool,
    ) -> PreparedWKTurn:
        provider = self.provider
        provider._record_canonical_read_observation(None)
        conversation_id = None
        baseline_current_node = None
        prewrite_payload: dict[str, Any] | None = None
        if conversation is not None:
            conversation_id = ConversationRef.from_any(conversation).conversation_id
            provider.clear_stop_requested_for(conversation_id)
            baseline_current_node = provider._cached_current_node(conversation_id)
            if baseline_current_node is None:
                prewrite_payload = provider.read_conversation_payload(
                    conversation_id,
                    timeout=min(15.0, total_timeout),
                )
                value = prewrite_payload.get("current_node")
                baseline_current_node = (
                    value if isinstance(value, str) and value else None
                )

        attachments = provider._normalize_attachment_paths(attachment_paths)
        selected_profile = getattr(provider._profile_context, "profile", None)
        use_minimal_security_shell = streaming and provider._lightweight_path_enabled()
        minimal_parent_message_id = None
        minimal_model_slug = (
            model_slug.strip()
            if isinstance(model_slug, str) and model_slug.strip()
            else None
        )
        minimal_thinking_effort = None
        minimal_attachment_descriptors: tuple[dict[str, Any], ...] = ()

        if use_minimal_security_shell and conversation_id is not None:
            if not isinstance(prewrite_payload, dict):
                prewrite_payload = provider.read_conversation_payload(
                    conversation_id,
                    timeout=min(15.0, total_timeout),
                )
            current_value = prewrite_payload.get("current_node")
            baseline_current_node = (
                current_value
                if isinstance(current_value, str) and current_value
                else None
            )
            mapping = (
                prewrite_payload.get("mapping")
                if isinstance(prewrite_payload.get("mapping"), dict)
                else {}
            )
            node = mapping.get(baseline_current_node) if baseline_current_node else None
            message = node.get("message") if isinstance(node, dict) else None
            author = message.get("author") if isinstance(message, dict) else None
            role = author.get("role") if isinstance(author, dict) else None
            message_id = message.get("id") if isinstance(message, dict) else None
            if (
                role == "assistant"
                and isinstance(message_id, str)
                and message_id.strip()
            ):
                minimal_parent_message_id = message_id.strip()
            else:
                use_minimal_security_shell = False
            if (
                use_minimal_security_shell
                and minimal_model_slug is None
                and not isinstance(selected_profile, str)
            ):
                selected_model = prewrite_payload.get("default_model_slug")
                metadata = (
                    message.get("metadata") if isinstance(message, dict) else None
                )
                selected_effort = (
                    metadata.get("thinking_effort")
                    if isinstance(metadata, dict)
                    else None
                )
                if isinstance(selected_model, str) and selected_model.strip():
                    minimal_model_slug = selected_model.strip()
                    if isinstance(selected_effort, str) and selected_effort.strip():
                        minimal_thinking_effort = selected_effort.strip()
                    elif "thinking" in minimal_model_slug.lower():
                        use_minimal_security_shell = False
                else:
                    use_minimal_security_shell = False

        if use_minimal_security_shell and attachments:
            uploaded_descriptors = provider._upload_minimal_security_attachments(
                attachments
            )
            if uploaded_descriptors is None:
                use_minimal_security_shell = False
            else:
                minimal_attachment_descriptors = uploaded_descriptors

        canonical_read_transport, canonical_read_fallback_reason = (
            provider._canonical_read_observation()
        )
        return PreparedWKTurn(
            conversation_id=conversation_id,
            baseline_current_node=baseline_current_node,
            attachments=attachments,
            use_minimal_security_shell=use_minimal_security_shell,
            minimal_parent_message_id=minimal_parent_message_id,
            minimal_model_slug=minimal_model_slug,
            minimal_thinking_effort=minimal_thinking_effort,
            minimal_attachment_descriptors=minimal_attachment_descriptors,
            canonical_read_transport=canonical_read_transport,
            canonical_read_fallback_reason=canonical_read_fallback_reason,
        )

    def build_turn_invocation(
        self,
        *,
        text: str,
        total_timeout: float,
        prepared: PreparedWKTurn,
        streaming: bool,
    ) -> Any:
        provider = self.provider
        invocation = provider._helper_command(
            conversation_id=prepared.conversation_id,
            text=text,
            timeout=total_timeout,
            attachment_paths=(
                () if prepared.use_minimal_security_shell else prepared.attachments
            ),
            expected_current_node=prepared.baseline_current_node,
        )
        if not streaming:
            return invocation

        invocation.command += [
            "--observe-submit",
            "--observe-stream",
            "--observe-stream-until-resume-token",
        ]
        invocation.capture_resume = True
        if not prepared.use_minimal_security_shell:
            return invocation

        invocation.command.append("--minimal-security-shell")
        if (
            prepared.conversation_id is not None
            and prepared.minimal_parent_message_id is not None
        ):
            invocation.request["minimal_conversation_id"] = prepared.conversation_id
            invocation.request["minimal_parent_message_id"] = (
                prepared.minimal_parent_message_id
            )
        if prepared.minimal_model_slug is not None:
            invocation.request["minimal_model_slug"] = prepared.minimal_model_slug
        if prepared.minimal_thinking_effort is not None:
            invocation.request["minimal_thinking_effort"] = (
                prepared.minimal_thinking_effort
            )
        if prepared.minimal_attachment_descriptors:
            invocation.request["minimal_attachments"] = list(
                prepared.minimal_attachment_descriptors
            )
        return invocation

    def run_protected_phase_one(
        self,
        invocation: Any,
        *,
        total_timeout: float,
        started: float,
        streaming: bool,
        on_text_event: Any,
        on_write_identity: Any,
    ) -> dict[str, Any]:
        provider = self.provider
        phase_started = time.monotonic()
        phase_a_transport = (
            "wkwebview_minimal_security_shell"
            if "--minimal-security-shell" in invocation.command
            else "wkwebview_full_page"
        )
        if not streaming:
            payload = provider._run_helper(invocation, timeout=total_timeout)
            payload["_cwa_phase_a_transport"] = phase_a_transport
            payload["_cwa_phase_a_gate_wait_ms"] = 0
            payload["_cwa_phase_a_elapsed_ms"] = int(
                (time.monotonic() - phase_started) * 1000
            )
            return payload

        phase_timeout = total_timeout
        gate_wait_ms = 0
        if provider._lightweight_path_enabled():
            gate_wait = max(0.001, total_timeout - (time.monotonic() - started))
            with provider._heavy_submit_gate(gate_wait) as gate_wait_ms:
                phase_timeout = max(1.0, total_timeout - (time.monotonic() - started))
                payload = provider._run_helper_streaming(
                    invocation,
                    timeout=phase_timeout,
                    on_text_event=on_text_event,
                    on_lifecycle_event=on_write_identity,
                )
        else:
            payload = provider._run_helper_streaming(
                invocation,
                timeout=phase_timeout,
                on_text_event=on_text_event,
                on_lifecycle_event=on_write_identity,
            )
        payload["_cwa_phase_a_transport"] = phase_a_transport
        payload["_cwa_phase_a_gate_wait_ms"] = gate_wait_ms
        payload["_cwa_phase_a_elapsed_ms"] = int(
            (time.monotonic() - phase_started) * 1000
        )
        return payload

    def validate_phase_one(
        self,
        payload: dict[str, Any],
        *,
        prepared: Any,
        text: str,
        total_timeout: float,
    ) -> WKPhaseOneResult:
        provider = self.provider
        result_conversation_id = payload.get("conversation_id")
        if (
            not isinstance(result_conversation_id, str)
            or not result_conversation_id.strip()
        ):
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_CONVERSATION_ID_UNRESOLVED",
                request_stage="wkwebview_authority_turn",
            )
        result_conversation_id = result_conversation_id.strip()

        response_status = payload.get("response_status", 200)
        if not isinstance(response_status, int) or not (200 <= response_status < 300):
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_HTTP_STATUS:{response_status}",
                request_stage="wkwebview_authority_turn",
                status_code=response_status
                if isinstance(response_status, int)
                else None,
            )

        stop_conduit_token = payload.pop("_cwa_stop_conduit_token", None)
        stop_turn_trace_id = payload.pop("_cwa_stop_turn_trace_id", None)
        provider._store_stop_context(
            result_conversation_id,
            stop_conduit_token if isinstance(stop_conduit_token, str) else None,
            stop_turn_trace_id if isinstance(stop_turn_trace_id, str) else None,
        )

        attachment_count = payload.get("attachment_count", 0)
        if not isinstance(attachment_count, int) or isinstance(attachment_count, bool):
            attachment_count = 0
        if prepared.attachments and attachment_count != len(prepared.attachments):
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_ATTACHMENT_COUNT_MISMATCH",
                request_stage="wkwebview_authority_turn",
            )

        helper_canonical_committed = payload.get("canonical_committed") is True
        helper_write_commit_proven = (
            payload.get("write_commit_proven") is True or helper_canonical_committed
        )
        committed_current_node = payload.get("committed_current_node")
        if helper_canonical_committed:
            provider._canonical_state.set_current_node(
                result_conversation_id, committed_current_node
            )
        elif not helper_write_commit_proven:
            provider._wait_for_canonical_write_commit(
                conversation_id=result_conversation_id,
                text=text,
                baseline_current_node=prepared.baseline_current_node,
                timeout=min(30.0, total_timeout),
            )

        phase_one_final_cached = False
        encoded_phase_one_final = payload.get("canonical_body_base64")
        if isinstance(encoded_phase_one_final, str) and encoded_phase_one_final:
            final_payload = provider._decode_helper_json(
                {
                    "status": payload.get("response_status", 200),
                    "body_base64": encoded_phase_one_final,
                },
                request_stage="wkwebview_stream_finality",
                error_prefix="WKWEBVIEW_STREAM_CANONICAL",
            )
            if provider._canonical_payload_matches_write(
                final_payload,
                text=text,
                baseline_current_node=prepared.baseline_current_node,
            ):
                provider._cache_final_payload(result_conversation_id, final_payload)
                phase_one_final_cached = True

        return WKPhaseOneResult(
            conversation_id=result_conversation_id,
            response_status=response_status,
            attachment_count=attachment_count,
            phase_one_final_cached=phase_one_final_cached,
        )

    def resume_after_phase_one(
        self,
        payload: dict[str, Any],
        *,
        result_conversation_id: str,
        phase_one_final_cached: bool,
        prepared: Any,
        text: str,
        total_timeout: float,
        started: float,
        streaming: bool,
        make_stream_relay: Callable[[], Callable[[dict[str, Any]], None]],
    ) -> bool:
        provider = self.provider
        phase_b_started = time.monotonic()

        def record_phase_b(transport: str, fallback_reason: str | None = None) -> None:
            payload["_cwa_phase_b_transport"] = transport
            payload["_cwa_phase_b_fallback_reason"] = fallback_reason
            payload["_cwa_phase_b_elapsed_ms"] = int(
                (time.monotonic() - phase_b_started) * 1000
            )

        passive_observer_armed = not streaming
        if not streaming:
            record_phase_b("passive_canonical_observer")
            return passive_observer_armed

        resume_value = payload.pop("stream_resume_value", None)
        if payload.get("_cwa_identity_recovered") is True:
            record_phase_b("canonical_message_id_recovery")
            return passive_observer_armed
        phase_one_completed = phase_one_final_cached or bool(
            payload.get("stream_terminal_observed")
        )
        if phase_one_completed:
            record_phase_b("phase_one_terminal")
            return passive_observer_armed
        if not isinstance(resume_value, str) or not resume_value:
            record_phase_b(
                "passive_canonical_observer",
                "WKWEBVIEW_RESUME_VALUE_MISSING",
            )
            return True

        remaining = max(1.0, total_timeout - (time.monotonic() - started))
        phase_b_transport = (
            "curl_cffi_websocket"
            if provider._lightweight_path_enabled()
            else "wkwebview_direct_resume"
        )
        try:
            if provider._lightweight_path_enabled():
                resume_payload = provider._resume_via_curl_ws_second_leg(
                    conversation_id=result_conversation_id,
                    resume_value=resume_value,
                    timeout=remaining,
                    relay_text_event=make_stream_relay(),
                    text=text,
                    baseline_current_node=prepared.baseline_current_node,
                )
            else:
                resume_payload = provider._resume_via_direct_wk(
                    conversation_id=result_conversation_id,
                    resume_value=resume_value,
                    timeout=remaining,
                    on_text_event=make_stream_relay(),
                )
            encoded_final = resume_payload.get("canonical_body_base64")
            if isinstance(encoded_final, str) and encoded_final:
                final_payload = provider._decode_helper_json(
                    {
                        "status": resume_payload.get("status", 200),
                        "body_base64": encoded_final,
                    },
                    request_stage="wkwebview_resume_finality",
                    error_prefix="WKWEBVIEW_RESUME_CANONICAL",
                )
                if provider._canonical_payload_matches_write(
                    final_payload,
                    text=text,
                    baseline_current_node=prepared.baseline_current_node,
                ):
                    provider._cache_final_payload(result_conversation_id, final_payload)
                else:
                    passive_observer_armed = True
                    record_phase_b(
                        phase_b_transport,
                        "WKWEBVIEW_RESUME_CANONICAL_MISMATCH",
                    )
                    return passive_observer_armed
            record_phase_b(phase_b_transport)
        except RequestError as error:
            if (
                provider._lightweight_path_enabled()
                and not WKLightweightTransport.resume_error_allows_passive_fallback(
                    error
                )
            ):
                raise
            passive_observer_armed = True
            record_phase_b(
                "passive_canonical_observer",
                WKLightweightTransport.safe_fallback_reason(error),
            )
        return passive_observer_armed

    def build_turn_result(
        self,
        payload: dict[str, Any],
        *,
        phase_one: WKPhaseOneResult,
        prepared: PreparedWKTurn,
        started: float,
        passive_observer_armed: bool,
    ) -> BrowserNativeTurnResult:
        provider = self.provider
        elapsed_ms = payload.get("elapsed_ms")
        if not isinstance(elapsed_ms, int):
            elapsed_ms = int((time.monotonic() - started) * 1000)
        return BrowserNativeTurnResult(
            conversation_id=phase_one.conversation_id,
            turn_exchange_id=(
                payload.get("turn_exchange_id")
                if isinstance(payload.get("turn_exchange_id"), str)
                else None
            ),
            response_status=phase_one.response_status,
            response_mime_type="text/event-stream",
            final_url=(
                payload.get("final_url")
                if isinstance(payload.get("final_url"), str)
                else None
            ),
            tab_id=None,
            tab_was_active=False,
            elapsed_ms=elapsed_ms,
            runtime_reloaded=True,
            runtime_reload_ms=(
                payload.get("load_elapsed_ms")
                if isinstance(payload.get("load_elapsed_ms"), int)
                else None
            ),
            runtime_tab_preexisting=False,
            runtime_tab_created_for_turn=True,
            tab_active_after=False,
            tab_activated_during_turn=False,
            foreground_activation_observed=False,
            browser_authority_lease_id=provider._current_browser_authority_lease_id(),
            attachment_count=phase_one.attachment_count,
            passive_observer_armed=passive_observer_armed,
            canonical_read_transport=prepared.canonical_read_transport,
            canonical_read_fallback_reason=prepared.canonical_read_fallback_reason,
            phase_a_transport=(
                payload.get("_cwa_phase_a_transport")
                if isinstance(payload.get("_cwa_phase_a_transport"), str)
                else None
            ),
            phase_a_gate_wait_ms=(
                payload.get("_cwa_phase_a_gate_wait_ms")
                if isinstance(payload.get("_cwa_phase_a_gate_wait_ms"), int)
                else None
            ),
            phase_a_elapsed_ms=(
                payload.get("_cwa_phase_a_elapsed_ms")
                if isinstance(payload.get("_cwa_phase_a_elapsed_ms"), int)
                else None
            ),
            phase_b_transport=(
                payload.get("_cwa_phase_b_transport")
                if isinstance(payload.get("_cwa_phase_b_transport"), str)
                else None
            ),
            phase_b_fallback_reason=(
                payload.get("_cwa_phase_b_fallback_reason")
                if isinstance(payload.get("_cwa_phase_b_fallback_reason"), str)
                else None
            ),
            phase_b_elapsed_ms=(
                payload.get("_cwa_phase_b_elapsed_ms")
                if isinstance(payload.get("_cwa_phase_b_elapsed_ms"), int)
                else None
            ),
        )
