from __future__ import annotations

import json
import os
import queue
import secrets
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .browser_native_protocol import (
    PROTOCOL_VERSION,
    bridge_descriptor_path,
    read_native_message,
    recv_local_message,
    send_local_message,
    write_native_message,
)


class _BrokerServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _BrokerHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        broker: BrowserNativeBroker = self.server.broker  # type: ignore[attr-defined]
        request: dict[str, Any] | None = None
        try:
            request = recv_local_message(self.request)

            def emit_event(message: dict[str, Any]) -> None:
                try:
                    send_local_message(self.request, message)
                except OSError:
                    # Intermediate delivery cannot authorize replay or alter the
                    # already-delegated product write outcome.
                    pass

            operation = request.get("type")
            response = broker.handle_local_request(
                request,
                event_sink=emit_event
                if request.get("streamTextObservations") is True
                or operation in {"canonical_read", "catalog_read", "observe_turn"}
                else None,
            )
        except Exception as error:
            response = {
                "protocol": PROTOCOL_VERSION,
                "request_id": request.get("request_id")
                if isinstance(request, dict)
                else None,
                "ok": False,
                "error": f"BROWSER_NATIVE_BROKER_ERROR:{error}",
            }
        try:
            send_local_message(self.request, response)
        except OSError:
            pass


# Crash-safety bound if Python exits before classifying canonical read terminality.
AUTHORITY_READBACK_RESERVATION_SECONDS = 120.0


class BrowserNativeBroker:
    def __init__(self, *, state_dir: str | Path | None = None) -> None:
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.descriptor_path = bridge_descriptor_path(self.state_dir)
        self.token = secrets.token_urlsafe(32)
        self.pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self.pending_requests: dict[str, dict[str, Any]] = {}
        self.stopped_observe_leases: dict[str, str | None] = {}
        self.pending_lock = threading.Lock()
        self.write_lock = threading.Lock()
        # Browser writes, canonical reads, and runtime-tab disposal share one
        # authority lane. A CLOSE cannot race an authenticated read or write.
        self.turn_lock = threading.Lock()
        self._authority_reservation_guard = threading.Lock()
        self._authority_reserved_lease_id: str | None = None
        self._authority_reservation_timer: threading.Timer | None = None
        self.extension_connected = False
        self.extension_id: str | None = None
        self.runtime_tab_id: int | None = None
        self._server = _BrokerServer(("127.0.0.1", 0), _BrokerHandler)
        self._server.broker = self  # type: ignore[attr-defined]
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="browser-native-loopback",
            daemon=True,
        )

    def start(self) -> None:
        self._server_thread.start()
        self._write_descriptor()

    def _write_descriptor(self) -> None:
        path = self.descriptor_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "protocol": PROTOCOL_VERSION,
            "host": "127.0.0.1",
            "port": int(self._server.server_address[1]),
            "token": self.token,
            "pid": os.getpid(),
            "startedAt": time.time(),
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def close(self) -> None:
        self._clear_authority_reservation(release_lane=True)
        self._server.shutdown()
        self._server.server_close()
        try:
            payload = json.loads(self.descriptor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict) and payload.get("pid") == os.getpid():
            try:
                self.descriptor_path.unlink()
            except OSError:
                pass
        with self.pending_lock:
            queues = list(self.pending.values())
            self.pending.clear()
            self.pending_requests.clear()
            self.stopped_observe_leases.clear()
        for waiter in queues:
            waiter.put(
                {
                    "protocol": PROTOCOL_VERSION,
                    "ok": False,
                    "error": "BROWSER_NATIVE_HOST_SHUTDOWN",
                }
            )

    @staticmethod
    def _request_lease_id(request: dict[str, Any]) -> str | None:
        value = request.get("browserAuthorityLeaseId")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _expire_authority_reservation(self, lease_id: str) -> None:
        release_lane = False
        with self._authority_reservation_guard:
            if self._authority_reserved_lease_id == lease_id:
                self._authority_reserved_lease_id = None
                self._authority_reservation_timer = None
                release_lane = True
        if release_lane:
            self.turn_lock.release()

    def _reserve_authority_for_readback(self, lease_id: str) -> None:
        timer = threading.Timer(
            AUTHORITY_READBACK_RESERVATION_SECONDS,
            self._expire_authority_reservation,
            args=(lease_id,),
        )
        timer.daemon = True
        with self._authority_reservation_guard:
            self._authority_reserved_lease_id = lease_id
            self._authority_reservation_timer = timer
        timer.start()

    def _refresh_authority_reservation(self, lease_id: str) -> bool:
        timer = threading.Timer(
            AUTHORITY_READBACK_RESERVATION_SECONDS,
            self._expire_authority_reservation,
            args=(lease_id,),
        )
        timer.daemon = True
        with self._authority_reservation_guard:
            if self._authority_reserved_lease_id != lease_id:
                return False
            previous = self._authority_reservation_timer
            self._authority_reservation_timer = timer
            if previous is not None:
                previous.cancel()
        timer.start()
        return True

    def _authority_reservation_matches(self, lease_id: str) -> bool:
        with self._authority_reservation_guard:
            reserved = self._authority_reserved_lease_id
            return reserved is not None and secrets.compare_digest(lease_id, reserved)

    def _claim_authority_lane(self, operation: str, lease_id: str | None) -> bool:
        with self._authority_reservation_guard:
            reserved_lease_id = self._authority_reserved_lease_id
            if reserved_lease_id is not None:
                if (
                    operation == "canonical_read"
                    and lease_id is not None
                    and secrets.compare_digest(lease_id, reserved_lease_id)
                ):
                    timer = self._authority_reservation_timer
                    self._authority_reserved_lease_id = None
                    self._authority_reservation_timer = None
                    if timer is not None:
                        timer.cancel()
                    return True
                return False
        return self.turn_lock.acquire(blocking=False)

    def _complete_authority_reservation(self, lease_id: str) -> str:
        with self._authority_reservation_guard:
            reserved_lease_id = self._authority_reserved_lease_id
            if reserved_lease_id is None:
                return (
                    "BROWSER_NATIVE_BRIDGE_BUSY"
                    if self.turn_lock.locked()
                    else "BROWSER_NATIVE_AUTHORITY_RESERVATION_LOST"
                )
            if not secrets.compare_digest(lease_id, reserved_lease_id):
                return "BROWSER_NATIVE_AUTHORITY_LEASE_MISMATCH"
            timer = self._authority_reservation_timer
            self._authority_reserved_lease_id = None
            self._authority_reservation_timer = None
            if timer is not None:
                timer.cancel()
        self.turn_lock.release()
        return "OK"

    def _clear_authority_reservation(self, *, release_lane: bool) -> None:
        had_reservation = False
        with self._authority_reservation_guard:
            if self._authority_reserved_lease_id is not None:
                had_reservation = True
                self._authority_reserved_lease_id = None
                timer = self._authority_reservation_timer
                self._authority_reservation_timer = None
                if timer is not None:
                    timer.cancel()
        if had_reservation and release_lane:
            self.turn_lock.release()

    def _active_turn_lease_for_stop(self) -> str | None:
        with self.pending_lock:
            for request in self.pending_requests.values():
                if request.get("type") != "turn":
                    continue
                lease_id = self._request_lease_id(request)
                if lease_id is not None:
                    return lease_id
        with self._authority_reservation_guard:
            reserved = self._authority_reserved_lease_id
        return reserved if isinstance(reserved, str) and reserved else None

    def _wake_observe_turn_after_stop(
        self, conversation_id: str | None, lease_id: str | None
    ) -> None:
        if not isinstance(lease_id, str) or not lease_id:
            return
        normalized = (
            conversation_id.strip()
            if isinstance(conversation_id, str) and conversation_id.strip()
            else None
        )
        deliveries: list[tuple[queue.Queue[dict[str, Any]], dict[str, Any]]] = []
        with self.pending_lock:
            self.stopped_observe_leases[lease_id] = normalized
            for request_id, request in self.pending_requests.items():
                if request.get("type") != "observe_turn":
                    continue
                if self._request_lease_id(request) != lease_id:
                    continue
                expected = request.get("conversationId")
                if (
                    normalized is not None
                    and isinstance(expected, str)
                    and expected.strip()
                    and expected.strip() != normalized
                ):
                    continue
                waiter = self.pending.get(request_id)
                if waiter is None:
                    continue
                deliveries.append(
                    (
                        waiter,
                        {
                            "protocol": PROTOCOL_VERSION,
                            "type": "observe_turn_result",
                            "request_id": request_id,
                            "ok": True,
                            "conversationId": normalized or expected,
                            "turnExchangeId": request.get("turnExchangeId"),
                            "messageId": None,
                            "finishReason": "stopped",
                            "source": "explicit_stop_broker",
                        },
                    )
                )
            if deliveries:
                self.stopped_observe_leases.pop(lease_id, None)
        for waiter, message in deliveries:
            waiter.put_nowait(message)

    @staticmethod
    def _ui_liveness_base(
        base: dict[str, Any],
        *,
        state: str,
        reason_code: str,
        extension_connected: bool,
        runtime_tab_present: bool | None,
    ) -> dict[str, Any]:
        return {
            **base,
            "type": "ui_liveness_result",
            "ok": True,
            "state": state,
            "reasonCode": reason_code,
            "observedAtMs": max(1, int(time.time() * 1000)),
            "bridgeAvailable": True,
            "extensionConnected": extension_connected,
            "runtimeTabPresent": runtime_tab_present,
            "composerVisible": None,
            "generationControlVisible": None,
            "composerBusy": None,
            "rawDomExported": False,
            "navigationPerformed": False,
            "runtimeTabCreated": False,
            "writePerformed": False,
            "canonicalReadPerformed": False,
            "canonicalFinalityProven": False,
            "grantsWriteAuthority": False,
            "grantsRetryAuthority": False,
        }

    def _handle_ui_liveness_request(
        self,
        request: dict[str, Any],
        base: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = request.get("request_id")
        for key in (
            "text",
            "conversationId",
            "attachmentPaths",
            "browserAuthorityLeaseId",
        ):
            if request.get(key) is not None:
                return {
                    **base,
                    "type": "ui_liveness_result",
                    "ok": False,
                    "error": "UI_LIVENESS_WRITE_BEARING_FIELDS_FORBIDDEN",
                }
        if not self.extension_connected:
            return self._ui_liveness_base(
                base,
                state="UNAVAILABLE",
                reason_code="EXTENSION_DISCONNECTED",
                extension_connected=False,
                runtime_tab_present=None,
            )
        if self.runtime_tab_id is None:
            return self._ui_liveness_base(
                base,
                state="UNAVAILABLE",
                reason_code="RUNTIME_TAB_ABSENT",
                extension_connected=True,
                runtime_tab_present=False,
            )
        if self.turn_lock.locked():
            # Observation never competes with an authoritative write/read for CDP.
            # Return immediately rather than forwarding a debugger probe.
            return self._ui_liveness_base(
                base,
                state="UNKNOWN",
                reason_code="AUTHORITY_LANE_ACTIVE",
                extension_connected=True,
                runtime_tab_present=True,
            )

        timeout_ms = request.get("timeoutMs")
        timeout = max(0.25, min(float(timeout_ms or 3_000) / 1000.0, 10.0))
        waiter: queue.Queue[dict[str, Any]] = queue.Queue()
        with self.pending_lock:
            self.pending[request_id] = waiter
        forwarded = {key: value for key, value in request.items() if key != "token"}
        try:
            with self.write_lock:
                write_native_message(sys.stdout.buffer, forwarded)
            try:
                return waiter.get(timeout=timeout + 1.0)
            except queue.Empty:
                return self._ui_liveness_base(
                    base,
                    state="UNKNOWN",
                    reason_code="OBSERVATION_EXTENSION_TIMEOUT",
                    extension_connected=True,
                    runtime_tab_present=True,
                )
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def handle_local_request(
        self,
        request: dict[str, Any],
        *,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        request_id = request.get("request_id")
        base = {
            "protocol": PROTOCOL_VERSION,
            "request_id": request_id,
        }
        if request.get("protocol") != PROTOCOL_VERSION:
            return {**base, "ok": False, "error": "BROWSER_NATIVE_PROTOCOL_MISMATCH"}
        if not secrets.compare_digest(str(request.get("token") or ""), self.token):
            return {**base, "ok": False, "error": "BROWSER_NATIVE_UNAUTHORIZED"}

        operation = request.get("type")
        if operation == "ping":
            return {
                **base,
                "ok": True,
                "extensionConnected": self.extension_connected,
                "hostPid": os.getpid(),
                "extensionId": self.extension_id,
                "runtimeTabId": self.runtime_tab_id,
            }

        if not isinstance(request_id, str) or not request_id:
            return {**base, "ok": False, "error": "BROWSER_NATIVE_REQUEST_ID_REQUIRED"}
        if operation == "ui_liveness":
            # PR11.5 does not acquire turn_lock. If the authority lane is active,
            # the handler returns UNKNOWN before touching the extension/debugger.
            return self._handle_ui_liveness_request(request, base)
        if operation == "stop_generation":
            # Stop is intentionally out-of-band: it must be able to reach the
            # runtime tab while the normal turn owns the authority lane.
            if not self.extension_connected:
                return {
                    **base,
                    "ok": False,
                    "error": "BROWSER_NATIVE_EXTENSION_NOT_CONNECTED",
                }
            timeout_ms = request.get("timeoutMs")
            timeout = max(1.0, float(timeout_ms or 10_000) / 1000.0)
            stop_lease_id = self._active_turn_lease_for_stop()
            waiter: queue.Queue[dict[str, Any]] = queue.Queue()
            with self.pending_lock:
                self.pending[request_id] = waiter
            forwarded = {key: value for key, value in request.items() if key != "token"}
            try:
                with self.write_lock:
                    write_native_message(sys.stdout.buffer, forwarded)
                try:
                    response = waiter.get(timeout=timeout + 2.0)
                    if response.get("ok") is True and response.get("stopped") is True:
                        stop_lease_id = (
                            stop_lease_id or self._active_turn_lease_for_stop()
                        )
                        self._wake_observe_turn_after_stop(
                            response.get("conversationId"), stop_lease_id
                        )
                    return response
                except queue.Empty:
                    return {
                        **base,
                        "ok": False,
                        "error": "BROWSER_NATIVE_EXTENSION_TIMEOUT",
                    }
            finally:
                with self.pending_lock:
                    self.pending.pop(request_id, None)

        if operation not in {
            "turn",
            "observe_turn",
            "canonical_read",
            "catalog_read",
            "canonical_read_complete",
            "release_runtime_tab",
        }:
            return {**base, "ok": False, "error": "BROWSER_NATIVE_UNKNOWN_OPERATION"}
        lease_id = self._request_lease_id(request)
        if operation == "observe_turn":
            if lease_id is None:
                return {
                    **base,
                    "ok": False,
                    "error": "BROWSER_NATIVE_AUTHORITY_LEASE_REQUIRED",
                }
            if not self.extension_connected:
                return {
                    **base,
                    "ok": False,
                    "error": "BROWSER_NATIVE_EXTENSION_NOT_CONNECTED",
                }
            if not self._authority_reservation_matches(lease_id):
                return {
                    **base,
                    "ok": False,
                    "error": "BROWSER_NATIVE_AUTHORITY_LEASE_MISMATCH",
                }
            self._refresh_authority_reservation(lease_id)
            with self.pending_lock:
                stopped_pending = lease_id in self.stopped_observe_leases
                stopped_conversation_id = (
                    self.stopped_observe_leases.pop(lease_id, None)
                    if stopped_pending
                    else None
                )
            if stopped_pending:
                requested_conversation_id = request.get("conversationId")
                return {
                    **base,
                    "type": "observe_turn_result",
                    "ok": True,
                    "conversationId": stopped_conversation_id
                    or requested_conversation_id,
                    "turnExchangeId": request.get("turnExchangeId"),
                    "messageId": None,
                    "finishReason": "stopped",
                    "source": "explicit_stop_broker",
                }
            timeout_ms = request.get("timeoutMs")
            timeout = max(1.0, float(timeout_ms or 120_000) / 1000.0)
            waiter: queue.Queue[dict[str, Any]] = queue.Queue()
            with self.pending_lock:
                self.pending[request_id] = waiter
                self.pending_requests[request_id] = dict(request)
            forwarded = {key: value for key, value in request.items() if key != "token"}
            try:
                with self.write_lock:
                    write_native_message(sys.stdout.buffer, forwarded)
                deadline = time.monotonic() + timeout + 5.0
                while True:
                    try:
                        message = waiter.get(
                            timeout=max(0.01, deadline - time.monotonic())
                        )
                    except queue.Empty:
                        return {
                            **base,
                            "ok": False,
                            "error": "BROWSER_NATIVE_EXTENSION_TIMEOUT",
                        }
                    if message.get("type") == "turn_event":
                        self._refresh_authority_reservation(lease_id)
                        if event_sink is not None:
                            event_sink(message)
                        continue
                    self._refresh_authority_reservation(lease_id)
                    return message
            finally:
                with self.pending_lock:
                    self.pending.pop(request_id, None)
                    self.pending_requests.pop(request_id, None)

        if operation == "canonical_read_complete":
            if lease_id is None:
                return {
                    **base,
                    "ok": False,
                    "error": "BROWSER_NATIVE_AUTHORITY_LEASE_REQUIRED",
                }
            completion = self._complete_authority_reservation(lease_id)
            if completion != "OK":
                return {**base, "ok": False, "error": completion}
            return {**base, "ok": True, "type": "canonical_read_complete_result"}
        if not self.extension_connected:
            return {
                **base,
                "ok": False,
                "error": "BROWSER_NATIVE_EXTENSION_NOT_CONNECTED",
            }
        if not self._claim_authority_lane(operation, lease_id):
            return {**base, "ok": False, "error": "BROWSER_NATIVE_BRIDGE_BUSY"}

        release_lane = True
        try:
            timeout_ms = request.get("timeoutMs")
            default_timeout_ms = {
                "turn": 120_000,
                "canonical_read": 30_000,
                "catalog_read": 30_000,
                "release_runtime_tab": 10_000,
            }[operation]
            timeout = max(
                1.0,
                float(timeout_ms or default_timeout_ms) / 1000.0,
            )
            waiter: queue.Queue[dict[str, Any]] = queue.Queue()
            with self.pending_lock:
                self.pending[request_id] = waiter
                self.pending_requests[request_id] = dict(request)
            forwarded = {key: value for key, value in request.items() if key != "token"}
            try:
                with self.write_lock:
                    write_native_message(sys.stdout.buffer, forwarded)
                deadline = time.monotonic() + timeout + 5.0
                while True:
                    try:
                        message = waiter.get(
                            timeout=max(0.01, deadline - time.monotonic())
                        )
                    except queue.Empty:
                        return {
                            **base,
                            "ok": False,
                            "error": "BROWSER_NATIVE_EXTENSION_TIMEOUT",
                        }
                    if message.get("type") in {"turn_event", "canonical_read_chunk"}:
                        if event_sink is not None:
                            event_sink(message)
                        continue
                    if lease_id is not None and (
                        operation == "canonical_read"
                        or (
                            operation == "turn"
                            and message.get("ok") is True
                            and request.get("conversationMode") != "temporary"
                        )
                    ):
                        # Normal turns retain the cross-request lane until Python has
                        # classified terminal canonical readback. Temporary Chat is
                        # page-finalized and intentionally has no canonical readback,
                        # so its lane is released with the completed turn.
                        self._reserve_authority_for_readback(lease_id)
                        release_lane = False
                    return message
            finally:
                with self.pending_lock:
                    self.pending.pop(request_id, None)
                    self.pending_requests.pop(request_id, None)
        finally:
            if release_lane and operation == "canonical_read" and lease_id is not None:
                # Read timeout/error still needs terminal classification before a
                # subsequent write or runtime-tab disposal may enter this lane.
                self._reserve_authority_for_readback(lease_id)
                release_lane = False
            if release_lane:
                self.turn_lock.release()

    def route_native_message(self, message: dict[str, Any]) -> None:
        if message.get("protocol") != PROTOCOL_VERSION:
            return
        if message.get("type") == "hello":
            self.extension_connected = True
            self.extension_id = (
                message.get("extensionId")
                if isinstance(message.get("extensionId"), str)
                else None
            )
            self.runtime_tab_id = (
                message.get("runtimeTabId")
                if isinstance(message.get("runtimeTabId"), int)
                else None
            )
            return
        if message.get("type") == "runtime_state":
            self.runtime_tab_id = (
                message.get("runtimeTabId")
                if isinstance(message.get("runtimeTabId"), int)
                else None
            )
            return
        request_id = message.get("request_id")
        if not isinstance(request_id, str):
            return
        with self.pending_lock:
            waiter = self.pending.get(request_id)
        if waiter is not None:
            waiter.put_nowait(message)


def main() -> int:
    broker = BrowserNativeBroker()
    broker.start()
    try:
        while True:
            try:
                message = read_native_message(sys.stdin.buffer)
            except EOFError:
                break
            broker.route_native_message(message)
    except Exception as error:
        print(f"browser-native host error: {error}", file=sys.stderr, flush=True)
        return 2
    finally:
        broker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
