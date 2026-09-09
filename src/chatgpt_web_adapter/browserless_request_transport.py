from __future__ import annotations

from time import monotonic
from typing import Any

from . import browserless_request_transport_core as _core
from .browserless_request_guards import gate_browserless_canonical_finalize
from .browserless_request_scope import (
    gate_browserless_request_execute,
    gate_browserless_request_health,
)
from .browserless_shared_write_fence import gate_browserless_transport_init
from .exceptions import RequestError
from .product_transport import (
    ConversationInput,
    EventCallback,
    ProductRuntimeExecution,
    TokenCallback,
)
from .revision_safe_streaming_pr8_9 import RevisionSafeTextAccumulator
from .types import ChatResponse

BrowserlessRequestTransportError = _core.BrowserlessRequestTransportError
BrowserlessChallengeBoundaryError = _core.BrowserlessChallengeBoundaryError
BrowserlessProtocolDriftError = _core.BrowserlessProtocolDriftError
BrowserlessRequestObservation = _core.BrowserlessRequestObservation
_BROWSERLESS_WRITE_PLANE = _core._BROWSERLESS_WRITE_PLANE


class BrowserlessRequestTransport(_core.BrowserlessRequestTransport):
    """Explicitly composed browserless transport over the frozen historical core."""

    __init__ = gate_browserless_transport_init(
        _core.BrowserlessRequestTransport.__init__
    )
    _canonical_finalize = gate_browserless_canonical_finalize(
        _core.BrowserlessRequestTransport._canonical_finalize
    )
    health = gate_browserless_request_health(_core.BrowserlessRequestTransport.health)

    @gate_browserless_request_execute
    def _execute(
        self,
        text: str,
        *,
        conversation: ConversationInput,
        timeout: float,
        poll_interval: float,
        on_token: TokenCallback,
        on_event: EventCallback,
    ) -> ProductRuntimeExecution:
        """Execute one direct request while preserving the public deadline seam.

        The historical implementation remains inherited from the frozen core, but
        this method is intentionally owned by the public module because callers and
        regression tests have long patched ``browserless_request_transport.monotonic``
        to prove total-deadline behavior. Moving the function object to another
        module changes its ``__globals__`` and therefore changes that contract.
        """

        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than 0")

        started = monotonic()
        deadline = started + timeout
        with self._write_lock:
            self._remaining_before_write(
                deadline,
                request_stage="browserless_write_queue",
            )
            self._assert_execution_provider_boundary()
            resolved_conversation = self._resolve_conversation(conversation)
            previous_message_id = self._parent_message_id(resolved_conversation)
            requirements = self._acquire_unprotected_requirements()
            self._assert_execution_provider_boundary()
            accumulator = RevisionSafeTextAccumulator()
            sequence = 0
            write_state = {"final_write_started": False}

            def stream_token(token: str) -> None:
                nonlocal sequence
                if not isinstance(token, str) or not token:
                    return
                sequence += 1
                event = {
                    "type": "assistant_text_delta",
                    "sequence": sequence,
                    "delta": token,
                }
                normalized = accumulator.apply(event)
                if normalized is not None and on_event is not None:
                    on_event(normalized)
                if on_token is not None:
                    on_token(token)

            self._remaining_before_write(
                deadline,
                request_stage="browserless_prepared_write_binding",
            )
            try:
                with self._bind_current_prepared_write(
                    requirements,
                    write_state=write_state,
                    deadline=deadline,
                ):
                    response = self._direct_send(
                        text,
                        conversation=resolved_conversation,
                        on_token=stream_token,
                        on_event=None,
                    )
            except BrowserlessRequestTransportError:
                raise
            except RequestError as error:
                raise self._classify_request_error(
                    error,
                    final_write_started=bool(write_state["final_write_started"]),
                ) from error
            except Exception as error:
                ambiguous = bool(write_state["final_write_started"])
                raise BrowserlessRequestTransportError(
                    f"browserless direct request failed: {type(error).__name__}: {error}",
                    request_stage=(
                        "conversation_stream" if ambiguous else "conversation_prepare"
                    ),
                    write_may_have_been_submitted=ambiguous,
                    reconciliation_required=ambiguous,
                ) from error

            if not isinstance(response, ChatResponse):
                raise BrowserlessRequestTransportError(
                    "browserless direct request returned an unexpected response type",
                    request_stage="conversation_stream",
                    write_may_have_been_submitted=True,
                    reconciliation_required=True,
                )

            remaining = max(0.0, deadline - monotonic())
            canonical_status, canonical_message, canonical_text = (
                self._canonical_finalize(
                    response,
                    previous_message_id=previous_message_id,
                    timeout=remaining,
                    poll_interval=poll_interval,
                )
            )
            reconciliation = accumulator.reconcile(canonical_text)

            response.text = canonical_text
            response.conversation.message_id = canonical_message.message_id
            response.conversation.parent_message_id = canonical_message.message_id
            finish_reason = getattr(canonical_status, "finish_reason", None)
            if isinstance(finish_reason, str) and finish_reason.strip():
                response.conversation.finish_reason = finish_reason.strip()
            elif canonical_message.finish_reason:
                response.conversation.finish_reason = canonical_message.finish_reason

            conversation_id = response.conversation.conversation_id
            if not isinstance(conversation_id, str) or not conversation_id.strip():
                raise BrowserlessRequestTransportError(
                    "canonical finality succeeded without a conversation id",
                    request_stage="canonical_reconciliation",
                    write_may_have_been_submitted=True,
                    reconciliation_required=True,
                )

            if on_event is not None:
                on_event(
                    accumulator.finalization_event(
                        canonical_text=canonical_text,
                        conversation_id=conversation_id,
                        message_id=canonical_message.message_id,
                        model=(
                            canonical_message.model
                            or getattr(response.request, "observed_model", None)
                        ),
                        finish_reason=response.conversation.finish_reason,
                    )
                )

            persona = requirements.get("persona")
            observation = BrowserlessRequestObservation(
                transport=self.transport_id,
                write_plane=_BROWSERLESS_WRITE_PLANE,
                requirements_persona=(
                    persona.strip()
                    if isinstance(persona, str) and persona.strip()
                    else None
                ),
                requirements_token_present=True,
                protected_challenges=(),
                sentinel_protocol="TWO_PHASE_PREPARE_FINALIZE",
                conversation_prepare_protocol="PREPARE_CONDUIT_FINAL_WRITE",
                canonical_status="completed",
                canonical_message_id=canonical_message.message_id,
                reconciliation=reconciliation,
                stream_observation_count=accumulator.observation_count,
                stream_revision_count=accumulator.revision_count,
                stream_delta_count=accumulator.delta_count,
                stream_delivery_incomplete=accumulator.delivery_incomplete,
            )
            return ProductRuntimeExecution(
                transport=self.transport_id,
                response=response,
                observation=observation,
            )


def __getattr__(name: str) -> Any:
    """Delegate untouched implementation details to the frozen legacy core."""

    return getattr(_core, name)
