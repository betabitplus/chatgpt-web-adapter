from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import queue
import select
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .exceptions import RequestError

_EVENT_PREFIX = "WK_EVENT "


class WKTurnBrokerConnection:
    def __init__(self, client: socket.socket, request_id: str) -> None:
        self.client = client
        self.request_id = request_id
        self._buffer = bytearray()
        self._closed = False

    def recv_envelope(self, timeout: float) -> dict[str, Any] | None:
        if self._closed:
            raise RequestError(
                "WKWEBVIEW_TURN_BROKER_CONNECTION_CLOSED",
                request_stage="wkwebview_authority_turn",
            )
        newline = self._buffer.find(b"\n")
        if newline < 0:
            ready, _, _ = select.select([self.client], [], [], max(0.0, float(timeout)))
            if not ready:
                return None
            try:
                chunk = self.client.recv(65536)
            except OSError as error:
                raise RequestError(
                    f"WKWEBVIEW_TURN_BROKER_READ_FAILED: {error}",
                    request_stage="wkwebview_authority_turn",
                ) from error
            if not chunk:
                raise RequestError(
                    "WKWEBVIEW_TURN_BROKER_CONNECTION_ENDED",
                    request_stage="wkwebview_authority_turn",
                )
            self._buffer.extend(chunk)
            newline = self._buffer.find(b"\n")
            if newline < 0:
                return None
        raw = bytes(self._buffer[:newline])
        del self._buffer[: newline + 1]
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.client.close()
        except OSError:
            pass


class WKTemporaryLifecycleLease:
    """Keep one Temporary WK lifecycle alive until the owning client releases it."""

    def __init__(self, client: socket.socket, lifecycle_id: str) -> None:
        self.client = client
        self.lifecycle_id = lifecycle_id
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.client.close()
        except OSError:
            pass


class WKSystemTurnBrokerClient:
    """Connect many CWA processes to one per-user WK turn authority process."""

    def __init__(
        self,
        helper: Path,
        *,
        runtime_dir: Path | None = None,
        idle_timeout: float = 2.0,
    ) -> None:
        self.helper = Path(helper)
        self.runtime_dir = (
            Path(runtime_dir)
            if runtime_dir is not None
            else Path("/tmp") / f"cwa-wk-turn-broker-{os.getuid()}"
        )
        self.idle_timeout = max(0.5, float(idle_timeout))
        self.socket_path = self.runtime_dir / "broker.sock"
        if len(os.fsencode(str(self.socket_path))) >= 104:
            raise ValueError("runtime_dir produces an overlong Unix socket path")
        self.launch_lock_path = self.runtime_dir / "launch.lock"

    def _connect_once(self, timeout: float) -> socket.socket:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(max(0.1, float(timeout)))
        try:
            client.connect(str(self.socket_path))
        except Exception:
            client.close()
            raise
        client.settimeout(None)
        return client

    def _ensure_connection(self, timeout: float) -> socket.socket:
        try:
            return self._connect_once(min(0.25, timeout))
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
            pass

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.runtime_dir, 0o700)
        fd = os.open(self.launch_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        started = time.monotonic()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            remaining = max(0.1, timeout - (time.monotonic() - started))
            try:
                return self._connect_once(min(0.25, remaining))
            except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
                self.socket_path.unlink(missing_ok=True)

            command = [
                sys.executable,
                "-m",
                "chatgpt_web_adapter.wkwebview_turn_broker",
                "--serve",
                "--socket",
                str(self.socket_path),
                "--helper",
                str(self.helper),
                "--idle-timeout",
                f"{self.idle_timeout:.3f}",
            ]
            env = os.environ.copy()
            package_root = str(Path(__file__).resolve().parents[1])
            existing_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                package_root
                if not existing_pythonpath
                else package_root + os.pathsep + existing_pythonpath
            )
            try:
                subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                    env=env,
                )
            except OSError as error:
                raise RequestError(
                    f"WKWEBVIEW_TURN_BROKER_LAUNCH_FAILED: {error}",
                    request_stage="wkwebview_authority_turn",
                ) from error

            deadline = time.monotonic() + remaining
            while time.monotonic() < deadline:
                try:
                    return self._connect_once(
                        min(0.25, max(0.1, deadline - time.monotonic()))
                    )
                except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
                    time.sleep(0.05)
            raise RequestError(
                "WKWEBVIEW_TURN_BROKER_READY_TIMEOUT",
                request_stage="wkwebview_authority_turn",
            )
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def start_turn(self, request: dict[str, Any], *, timeout: float) -> WKTurnBrokerConnection:
        total_timeout = max(0.1, float(timeout))
        client = self._ensure_connection(min(10.0, total_timeout))
        request_id = str(uuid.uuid4())
        payload = {
            "type": "turn",
            "request_id": request_id,
            "request": request,
            "timeout": total_timeout,
        }
        try:
            client.sendall(
                (json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n").encode(
                    "utf-8"
                )
            )
        except OSError as error:
            client.close()
            raise RequestError(
                f"WKWEBVIEW_TURN_BROKER_WRITE_FAILED: {error}",
                request_stage="wkwebview_authority_turn",
            ) from error
        return WKTurnBrokerConnection(client, request_id)

    def acquire_temporary_lifecycle(
        self,
        lifecycle_id: str,
        *,
        timeout: float = 10.0,
    ) -> WKTemporaryLifecycleLease:
        normalized = lifecycle_id.strip() if isinstance(lifecycle_id, str) else ""
        if not normalized:
            raise ValueError("temporary lifecycle id is required")
        total_timeout = max(0.1, float(timeout))
        client = self._ensure_connection(min(10.0, total_timeout))
        request_id = str(uuid.uuid4())
        payload = {
            "type": "temporary_lease",
            "request_id": request_id,
            "lifecycle_id": normalized,
        }
        try:
            client.sendall(
                (json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n").encode(
                    "utf-8"
                )
            )
            client.settimeout(total_timeout)
            buffer = bytearray()
            while b"\n" not in buffer:
                chunk = client.recv(65536)
                if not chunk:
                    raise RequestError(
                        "WKWEBVIEW_TEMPORARY_LEASE_CONNECTION_ENDED",
                        request_stage="wkwebview_temporary_lifecycle",
                    )
                buffer.extend(chunk)
            raw, _, remainder = bytes(buffer).partition(b"\n")
            if remainder:
                raise RequestError(
                    "WKWEBVIEW_TEMPORARY_LEASE_PROTOCOL_INVALID",
                    request_stage="wkwebview_temporary_lifecycle",
                )
            envelope = json.loads(raw.decode("utf-8"))
            if not isinstance(envelope, dict) or envelope.get("type") != "temporary_lease_ready":
                raise RequestError(
                    str(
                        envelope.get("error")
                        if isinstance(envelope, dict)
                        else "WKWEBVIEW_TEMPORARY_LEASE_FAILED"
                    ),
                    request_stage="wkwebview_temporary_lifecycle",
                )
            client.settimeout(None)
            return WKTemporaryLifecycleLease(client, normalized)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            client.close()
            raise RequestError(
                f"WKWEBVIEW_TEMPORARY_LEASE_FAILED: {error}",
                request_stage="wkwebview_temporary_lifecycle",
            ) from error
        except Exception:
            client.close()
            raise


class _NativeTurnBroker:
    def __init__(self, helper: Path) -> None:
        self.helper = Path(helper)
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._ready: threading.Event | None = None
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._contexts: dict[str, dict[str, Any]] = {}
        self._proxy_active: set[str] = set()
        self._stderr_tail: list[str] = []

    def _drain_stderr(self, process: subprocess.Popen[str]) -> None:
        stream = process.stderr
        if stream is None:
            return
        try:
            for line in stream:
                value = line.rstrip()
                if not value:
                    continue
                with self._lock:
                    self._stderr_tail.append(value)
                    if len(self._stderr_tail) > 20:
                        del self._stderr_tail[:-20]
        except Exception:
            pass

    def _fail_process(self, process: subprocess.Popen[str], error: str) -> None:
        with self._lock:
            if self._process is not process:
                return
            pending = list(self._pending.values())
            self._pending.clear()
            self._contexts.clear()
            self._proxy_active.clear()
            self._process = None
            self._ready = None
        envelope = {"type": "error", "error": error}
        for target in pending:
            target.put(envelope)

    def _emit_proxy_state(
        self,
        request_id: str,
        state: str,
        *,
        status: int | None = None,
        byte_count: int | None = None,
        chunk_count: int | None = None,
        error_code: str | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "type": "proxy_fetch_state",
            "state": state,
        }
        if isinstance(status, int):
            event["status"] = status
        if isinstance(byte_count, int):
            event["byte_count"] = byte_count
        if isinstance(chunk_count, int):
            event["chunk_count"] = chunk_count
        if isinstance(error_code, str) and error_code:
            event["error_code"] = error_code
        with self._lock:
            target = self._pending.get(request_id)
        if target is not None:
            target.put({"type": "event", "request_id": request_id, "event": event})

    @staticmethod
    def _proxy_response_headers(raw_headers: list[tuple[str, str]]) -> dict[str, str]:
        excluded = {
            "connection",
            "content-encoding",
            "content-length",
            "set-cookie",
            "transfer-encoding",
        }
        result: dict[str, str] = {}
        for name, value in raw_headers:
            normalized = name.strip().lower()
            if not normalized or normalized in excluded:
                continue
            result[normalized] = value.strip()
        return result

    def _proxy_fetch_error(
        self,
        helper_process: subprocess.Popen[str],
        request_id: str,
        proxy_id: str,
        message: str,
    ) -> None:
        self._emit_proxy_state(request_id, "error", error_code=message)
        try:
            self._send(
                helper_process,
                {
                    "type": "proxy_fetch_error",
                    "request_id": request_id,
                    "proxy_id": proxy_id,
                    "message": message,
                },
            )
        except Exception:
            pass

    def _run_proxy_fetch(
        self,
        helper_process: subprocess.Popen[str],
        request_id: str,
        event: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        proxy_id = event.get("proxy_id")
        try:
            if not isinstance(proxy_id, str) or not proxy_id:
                return
            if context.get("proxy_protected_write") is not True:
                self._proxy_fetch_error(
                    helper_process,
                    request_id,
                    proxy_id,
                    "WKWEBVIEW_PROXY_FETCH_NOT_ENABLED",
                )
                return
            url = event.get("url")
            method = event.get("method")
            body = event.get("body")
            parsed = urlparse(url) if isinstance(url, str) else None
            if (
                parsed is None
                or parsed.scheme != "https"
                or parsed.hostname != "chatgpt.com"
                or parsed.path.rstrip("/") != "/backend-api/f/conversation"
                or str(method or "").upper() != "POST"
                or not isinstance(body, str)
            ):
                self._proxy_fetch_error(
                    helper_process,
                    request_id,
                    proxy_id,
                    "WKWEBVIEW_PROXY_FETCH_SCOPE_INVALID",
                )
                return

            self._emit_proxy_state(request_id, "started")
            headers: dict[str, str] = {}
            raw_headers = event.get("headers")
            if isinstance(raw_headers, list):
                for item in raw_headers:
                    if (
                        isinstance(item, list)
                        and len(item) == 2
                        and isinstance(item[0], str)
                        and isinstance(item[1], str)
                    ):
                        name = item[0].strip()
                        if name and "\r" not in name and "\n" not in name:
                            value = item[1]
                            if "\r" not in value and "\n" not in value:
                                headers[name] = value
            cookie_header = context.get("proxy_cookie_header")
            if isinstance(cookie_header, str) and cookie_header:
                headers.setdefault("cookie", cookie_header)
            user_agent = event.get("user_agent")
            if isinstance(user_agent, str) and user_agent:
                headers.setdefault("user-agent", user_agent)
            headers.setdefault("origin", "https://chatgpt.com")
            referer = context.get("url")
            headers.setdefault(
                "referer",
                referer if isinstance(referer, str) and referer.startswith("https://chatgpt.com/") else "https://chatgpt.com/",
            )

            timeout = context.get("timeout")
            try:
                timeout_value = max(1.0, float(timeout))
            except (TypeError, ValueError):
                timeout_value = 150.0

            from curl_cffi import requests as curl_requests

            with curl_requests.Session(impersonate="safari") as session:
                response = session.post(
                    url,
                    headers=headers,
                    data=body.encode("utf-8"),
                    timeout=timeout_value,
                    allow_redirects=False,
                    stream=True,
                )
                status = int(response.status_code or 0)
                if status <= 0:
                    self._proxy_fetch_error(
                        helper_process,
                        request_id,
                        proxy_id,
                        "WKWEBVIEW_PROXY_FETCH_HEADERS_MISSING",
                    )
                    response.close()
                    return

                raw_response_headers: list[tuple[str, str]] = []
                try:
                    raw_response_headers = [
                        (str(name), str(value)) for name, value in response.headers.items()
                    ]
                except Exception:
                    raw_response_headers = []
                self._emit_proxy_state(request_id, "headers", status=status)
                self._send(
                    helper_process,
                    {
                        "type": "proxy_fetch_headers",
                        "request_id": request_id,
                        "proxy_id": proxy_id,
                        "status": status,
                        "headers": self._proxy_response_headers(raw_response_headers),
                    },
                )

                byte_count = 0
                chunk_count = 0
                try:
                    for chunk in response.iter_content(chunk_size=16384):
                        if not chunk:
                            continue
                        if isinstance(chunk, str):
                            chunk = chunk.encode("utf-8")
                        byte_count += len(chunk)
                        chunk_count += 1
                        self._send(
                            helper_process,
                            {
                                "type": "proxy_fetch_chunk",
                                "request_id": request_id,
                                "proxy_id": proxy_id,
                                "chunk_base64": base64.b64encode(chunk).decode("ascii"),
                            },
                        )
                finally:
                    response.close()

            self._emit_proxy_state(
                request_id,
                "end",
                status=status,
                byte_count=byte_count,
                chunk_count=chunk_count,
            )
            self._send(
                helper_process,
                {
                    "type": "proxy_fetch_end",
                    "request_id": request_id,
                    "proxy_id": proxy_id,
                },
            )
        except Exception:
            if isinstance(proxy_id, str) and proxy_id:
                self._proxy_fetch_error(
                    helper_process,
                    request_id,
                    proxy_id,
                    "WKWEBVIEW_PROXY_FETCH_INTERNAL_FAILED",
                )
        finally:
            with self._lock:
                self._proxy_active.discard(request_id)

    def _reader_loop(self, process: subprocess.Popen[str], ready: threading.Event) -> None:
        stream = process.stdout
        if stream is None:
            self._fail_process(process, "WKWEBVIEW_TURN_BROKER_STDOUT_MISSING")
            return
        try:
            for raw in stream:
                if not raw.startswith(_EVENT_PREFIX):
                    continue
                try:
                    event = json.loads(raw[len(_EVENT_PREFIX) :])
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "broker_ready":
                    ready.set()
                    continue
                request_id = event.get("request_id")
                if not isinstance(request_id, str) or not request_id:
                    continue
                with self._lock:
                    target = self._pending.get(request_id)
                if target is None:
                    continue
                if event.get("type") == "broker_proxy_fetch_request":
                    with self._lock:
                        context = dict(self._contexts.get(request_id, {}))
                        duplicate = request_id in self._proxy_active
                        if not duplicate:
                            self._proxy_active.add(request_id)
                    proxy_id = event.get("proxy_id")
                    if duplicate:
                        self._emit_proxy_state(
                            request_id, "duplicate", error_code="WKWEBVIEW_PROXY_FETCH_DUPLICATE"
                        )
                        if isinstance(proxy_id, str) and proxy_id:
                            self._proxy_fetch_error(
                                process, request_id, proxy_id, "WKWEBVIEW_PROXY_FETCH_DUPLICATE"
                            )
                    else:
                        threading.Thread(
                            target=self._run_proxy_fetch,
                            args=(process, request_id, dict(event), context),
                            daemon=True,
                            name="wk-proxy-fetch",
                        ).start()
                    continue
                if event.get("type") == "broker_result":
                    result = event.get("result")
                    target.put(
                        {
                            "type": "result",
                            "request_id": request_id,
                            "result": result if isinstance(result, dict) else {},
                        }
                    )
                else:
                    forwarded = dict(event)
                    forwarded.pop("request_id", None)
                    target.put(
                        {"type": "event", "request_id": request_id, "event": forwarded}
                    )
        finally:
            self._fail_process(process, "WKWEBVIEW_TURN_BROKER_PROCESS_ENDED")

    def _send(self, process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n"
        try:
            with self._write_lock:
                if process.stdin is None:
                    raise BrokenPipeError("broker stdin unavailable")
                process.stdin.write(encoded)
                process.stdin.flush()
        except (OSError, BrokenPipeError, ValueError) as error:
            raise RequestError(
                f"WKWEBVIEW_TURN_BROKER_NATIVE_WRITE_FAILED: {error}",
                request_stage="wkwebview_authority_turn",
            ) from error

    def _ensure_started(self) -> subprocess.Popen[str]:
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                ready = self._ready
            else:
                ready = threading.Event()
                try:
                    process = subprocess.Popen(
                        [str(self.helper), "--turn-broker"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                        env={**os.environ, "NSUnbufferedIO": "YES"},
                    )
                except OSError as error:
                    raise RequestError(
                        f"WKWEBVIEW_TURN_BROKER_NATIVE_LAUNCH_FAILED: {error}",
                        request_stage="wkwebview_authority_turn",
                    ) from error
                self._process = process
                self._ready = ready
                threading.Thread(
                    target=self._reader_loop,
                    args=(process, ready),
                    daemon=True,
                    name="wk-turn-broker-reader",
                ).start()
                threading.Thread(
                    target=self._drain_stderr,
                    args=(process,),
                    daemon=True,
                    name="wk-turn-broker-stderr",
                ).start()
        if ready is None or not ready.wait(timeout=10.0):
            detail = ""
            with self._lock:
                if self._stderr_tail:
                    detail = ": " + self._stderr_tail[-1]
            self._fail_process(process, "WKWEBVIEW_TURN_BROKER_NATIVE_READY_TIMEOUT")
            if process.poll() is None:
                process.terminate()
            raise RequestError(
                "WKWEBVIEW_TURN_BROKER_NATIVE_READY_TIMEOUT" + detail,
                request_stage="wkwebview_authority_turn",
            )
        return process

    def _start_command(
        self,
        request_id: str,
        payload: dict[str, Any],
    ) -> queue.Queue[dict[str, Any]]:
        process = self._ensure_started()
        target: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            if request_id in self._pending:
                raise RequestError(
                    "WKWEBVIEW_TURN_BROKER_DUPLICATE_REQUEST",
                    request_stage="wkwebview_authority_turn",
                )
            self._pending[request_id] = target
            request_context = payload.get("request")
            if isinstance(request_context, dict):
                self._contexts[request_id] = dict(request_context)
        try:
            self._send(process, payload)
        except Exception:
            with self._lock:
                self._pending.pop(request_id, None)
                self._contexts.pop(request_id, None)
                self._proxy_active.discard(request_id)
            raise
        return target

    def start(self, request_id: str, request: dict[str, Any]) -> queue.Queue[dict[str, Any]]:
        return self._start_command(
            request_id,
            {"type": "start_turn", "request_id": request_id, "request": request},
        )

    def end_temporary_lifecycle(self, lifecycle_id: str, *, timeout: float = 5.0) -> None:
        normalized = lifecycle_id.strip() if isinstance(lifecycle_id, str) else ""
        if not normalized:
            return
        request_id = str(uuid.uuid4())
        target = self._start_command(
            request_id,
            {
                "type": "end_temporary_lifecycle",
                "request_id": request_id,
                "lifecycle_id": normalized,
            },
        )
        try:
            envelope = target.get(timeout=max(0.5, float(timeout)))
            if envelope.get("type") == "error":
                raise RequestError(
                    str(envelope.get("error") or "WKWEBVIEW_TEMPORARY_LIFECYCLE_CLOSE_FAILED"),
                    request_stage="wkwebview_temporary_lifecycle",
                )
            result = envelope.get("result")
            if envelope.get("type") != "result" or not isinstance(result, dict) or result.get("ok") is not True:
                raise RequestError(
                    "WKWEBVIEW_TEMPORARY_LIFECYCLE_CLOSE_FAILED",
                    request_stage="wkwebview_temporary_lifecycle",
                )
        except queue.Empty as error:
            raise RequestError(
                "WKWEBVIEW_TEMPORARY_LIFECYCLE_CLOSE_TIMEOUT",
                request_stage="wkwebview_temporary_lifecycle",
            ) from error
        finally:
            self.finish(request_id, cancel=False)

    def finish(self, request_id: str, *, cancel: bool) -> None:
        with self._lock:
            self._pending.pop(request_id, None)
            self._contexts.pop(request_id, None)
            self._proxy_active.discard(request_id)
            process = self._process
        if cancel and process is not None and process.poll() is None:
            try:
                self._send(process, {"type": "cancel", "request_id": request_id})
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            self._ready = None
            pending = list(self._pending.values())
            self._pending.clear()
            self._contexts.clear()
            self._proxy_active.clear()
        for target in pending:
            target.put({"type": "error", "error": "WKWEBVIEW_TURN_BROKER_CLOSED"})
        if process is None:
            return
        try:
            self._send(process, {"type": "shutdown"})
        except Exception:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.terminate()


def _send_envelope(connection: socket.socket, payload: dict[str, Any]) -> None:
    connection.sendall(
        (json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n").encode(
            "utf-8"
        )
    )


def _connection_peer_closed(connection: socket.socket) -> bool:
    """Return True when the client disconnected without consuming socket data."""

    try:
        ready, _, _ = select.select([connection], [], [], 0)
    except (OSError, ValueError):
        return True
    if not ready:
        return False
    try:
        return connection.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True


def _serve_client(
    connection: socket.socket,
    broker: _NativeTurnBroker,
    active_lock: threading.Lock,
    active_state: dict[str, Any],
) -> None:
    request_id = ""
    completed = False
    try:
        connection.settimeout(5.0)
        reader = connection.makefile("r", encoding="utf-8")
        raw = reader.readline()
        if not raw:
            return
        command = json.loads(raw)
        if not isinstance(command, dict):
            return
        command_type = command.get("type")
        request_id = (
            command.get("request_id") if isinstance(command.get("request_id"), str) else ""
        )
        if command_type == "temporary_lease":
            lifecycle_id = (
                command.get("lifecycle_id")
                if isinstance(command.get("lifecycle_id"), str)
                else ""
            ).strip()
            if not request_id or not lifecycle_id:
                _send_envelope(
                    connection,
                    {
                        "type": "error",
                        "request_id": request_id,
                        "error": "WKWEBVIEW_TEMPORARY_LEASE_INPUT_INVALID",
                    },
                )
                return
            with active_lock:
                leases = active_state.setdefault("temporary_leases", set())
                if lifecycle_id in leases:
                    duplicate = True
                else:
                    leases.add(lifecycle_id)
                    duplicate = False
            if duplicate:
                _send_envelope(
                    connection,
                    {
                        "type": "error",
                        "request_id": request_id,
                        "error": "WKWEBVIEW_TEMPORARY_LEASE_ALREADY_ACTIVE",
                    },
                )
                return
            try:
                _send_envelope(
                    connection,
                    {
                        "type": "temporary_lease_ready",
                        "request_id": request_id,
                        "lifecycle_id": lifecycle_id,
                    },
                )
                connection.settimeout(0.5)
                while True:
                    try:
                        chunk = connection.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
            finally:
                try:
                    broker.end_temporary_lifecycle(lifecycle_id)
                except Exception:
                    pass
                with active_lock:
                    active_state.setdefault("temporary_leases", set()).discard(lifecycle_id)
            completed = True
            request_id = ""
            return
        if command_type != "turn":
            return
        request = command.get("request")
        timeout = float(command.get("timeout", 0))
        if not request_id or not isinstance(request, dict) or timeout <= 0:
            _send_envelope(
                connection,
                {
                    "type": "error",
                    "request_id": request_id,
                    "error": "WKWEBVIEW_TURN_BROKER_INPUT_INVALID",
                },
            )
            return
        target = broker.start(request_id, request)
        deadline = time.monotonic() + timeout + 5.0
        connection.settimeout(None)
        while time.monotonic() < deadline:
            try:
                envelope = target.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                if _connection_peer_closed(connection):
                    return
                continue
            if envelope.get("type") == "error":
                _send_envelope(
                    connection,
                    {
                        "type": "error",
                        "request_id": request_id,
                        "error": str(envelope.get("error") or "WKWEBVIEW_TURN_BROKER_FAILED"),
                    },
                )
                return
            _send_envelope(connection, envelope)
            if envelope.get("type") == "result":
                completed = True
                return
        _send_envelope(
            connection,
            {
                "type": "error",
                "request_id": request_id,
                "error": "WKWEBVIEW_TURN_BROKER_REQUEST_TIMEOUT",
            },
        )
    except (OSError, ValueError, json.JSONDecodeError, RequestError) as error:
        try:
            _send_envelope(
                connection,
                {
                    "type": "error",
                    "request_id": request_id,
                    "error": f"{type(error).__name__}:{error}",
                },
            )
        except OSError:
            pass
    finally:
        if request_id:
            broker.finish(request_id, cancel=not completed)
        try:
            connection.close()
        except OSError:
            pass
        with active_lock:
            active_state["count"] -= 1
            active_state["last_activity"] = time.monotonic()


def serve(socket_path: Path, helper: Path, *, idle_timeout: float) -> int:
    socket_path = Path(socket_path)
    runtime_dir = socket_path.parent
    runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime_dir, 0o700)
    socket_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    broker = _NativeTurnBroker(Path(helper))
    active_lock = threading.Lock()
    active_state: dict[str, Any] = {"count": 0, "last_activity": time.monotonic()}
    try:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        server.listen(32)
        server.settimeout(0.2)
        while True:
            with active_lock:
                active = int(active_state["count"])
                idle_for = time.monotonic() - float(active_state["last_activity"])
            if active == 0 and idle_for >= idle_timeout:
                break
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            with active_lock:
                active_state["count"] += 1
                active_state["last_activity"] = time.monotonic()
            threading.Thread(
                target=_serve_client,
                args=(connection, broker, active_lock, active_state),
                daemon=True,
                name="wk-system-turn-client",
            ).start()
        return 0
    finally:
        broker.close()
        server.close()
        socket_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--socket")
    parser.add_argument("--helper")
    parser.add_argument("--idle-timeout", type=float, default=2.0)
    args = parser.parse_args(argv)
    if not args.serve or not args.socket or not args.helper:
        return 64
    return serve(
        Path(args.socket),
        Path(args.helper),
        idle_timeout=max(0.5, args.idle_timeout),
    )


if __name__ == "__main__":
    raise SystemExit(main())
