from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .exceptions import RequestError


class WKSystemResumeBrokerClient:
    """Client for the per-user shared WK resume broker daemon."""

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
            else Path("/tmp") / f"cwa-wk-shared-resume-{os.getuid()}"
        )
        self.idle_timeout = max(0.5, float(idle_timeout))
        self.socket_path = self.runtime_dir / "broker.sock"
        if len(os.fsencode(str(self.socket_path))) >= 104:
            raise ValueError("runtime_dir produces an overlong Unix socket path")
        self.launch_lock_path = self.runtime_dir / "launch.lock"

    def _connect_once(self, timeout: float) -> socket.socket:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(max(0.1, timeout))
        try:
            client.connect(str(self.socket_path))
        except Exception:
            client.close()
            raise
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
                "chatgpt_web_adapter.wkwebview_shared_broker",
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
                    f"WKWEBVIEW_SHARED_BROKER_LAUNCH_FAILED: {error}",
                    request_stage="wkwebview_shared_resume",
                ) from error

            deadline = time.monotonic() + max(0.1, remaining)
            while time.monotonic() < deadline:
                try:
                    return self._connect_once(min(0.25, max(0.1, deadline - time.monotonic())))
                except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
                    time.sleep(0.05)
            raise RequestError(
                "WKWEBVIEW_SHARED_BROKER_READY_TIMEOUT",
                request_stage="wkwebview_shared_resume",
            )
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def resume(
        self,
        *,
        conversation_id: str,
        resume_token: str,
        offset: int,
        timeout: float,
        on_text_event: Any,
    ) -> dict[str, Any]:
        if not callable(on_text_event):
            raise TypeError("on_text_event must be callable")
        total_timeout = float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")
        request_id = str(uuid.uuid4())
        client = self._ensure_connection(min(10.0, total_timeout))
        client.settimeout(total_timeout)
        request = {
            "type": "resume",
            "request_id": request_id,
            "conversation_id": conversation_id,
            "resume_token": resume_token,
            "offset": int(offset),
            "timeout": total_timeout,
        }
        try:
            client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
            reader = client.makefile("r", encoding="utf-8")
            while True:
                try:
                    raw = reader.readline()
                except (socket.timeout, TimeoutError) as error:
                    raise RequestError(
                        "WKWEBVIEW_SHARED_BROKER_CLIENT_TIMEOUT",
                        request_stage="wkwebview_shared_resume",
                    ) from error
                if not raw:
                    raise RequestError(
                        "WKWEBVIEW_SHARED_BROKER_CONNECTION_ENDED",
                        request_stage="wkwebview_shared_resume",
                    )
                try:
                    envelope = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(envelope, dict) or envelope.get("request_id") != request_id:
                    continue
                kind = envelope.get("type")
                if kind == "event":
                    event = envelope.get("event")
                    if isinstance(event, dict):
                        try:
                            on_text_event(event)
                        except Exception:
                            pass
                    continue
                if kind == "result":
                    result = envelope.get("result")
                    if not isinstance(result, dict):
                        raise RequestError(
                            "WKWEBVIEW_SHARED_BROKER_RESULT_INVALID",
                            request_stage="wkwebview_shared_resume",
                        )
                    return result
                if kind == "error":
                    error = envelope.get("error")
                    raise RequestError(
                        str(error or "WKWEBVIEW_SHARED_BROKER_FAILED"),
                        request_stage="wkwebview_shared_resume",
                    )
        finally:
            client.close()

    def close(self) -> None:
        # The daemon exits automatically after its system-wide idle timeout.
        return


def _send_envelope(connection: socket.socket, lock: threading.Lock, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    with lock:
        connection.sendall(encoded)


def _serve_client(
    connection: socket.socket,
    broker: Any,
    active_lock: threading.Lock,
    active_state: dict[str, Any],
) -> None:
    writer_lock = threading.Lock()
    request_id = ""
    try:
        connection.settimeout(5.0)
        reader = connection.makefile("r", encoding="utf-8")
        raw = reader.readline()
        if not raw:
            return
        command = json.loads(raw)
        if not isinstance(command, dict) or command.get("type") != "resume":
            return
        request_id = command.get("request_id") if isinstance(command.get("request_id"), str) else ""
        conversation_id = (
            command.get("conversation_id") if isinstance(command.get("conversation_id"), str) else ""
        )
        resume_token = command.get("resume_token") if isinstance(command.get("resume_token"), str) else ""
        timeout = float(command.get("timeout", 0))
        if not request_id or not conversation_id or not resume_token or timeout <= 0:
            _send_envelope(
                connection,
                writer_lock,
                {"type": "error", "request_id": request_id, "error": "WKWEBVIEW_SHARED_BROKER_INPUT_INVALID"},
            )
            return

        def on_event(event: dict[str, Any]) -> None:
            _send_envelope(
                connection,
                writer_lock,
                {"type": "event", "request_id": request_id, "event": event},
            )

        result = broker.resume(
            conversation_id=conversation_id,
            resume_token=resume_token,
            offset=int(command.get("offset", 0)),
            timeout=timeout,
            on_text_event=on_event,
        )
        _send_envelope(
            connection,
            writer_lock,
            {"type": "result", "request_id": request_id, "result": result},
        )
    except Exception as error:
        try:
            _send_envelope(
                connection,
                writer_lock,
                {"type": "error", "request_id": request_id, "error": f"{type(error).__name__}:{error}"},
            )
        except OSError:
            pass
    finally:
        connection.close()
        with active_lock:
            active_state["count"] -= 1
            active_state["last_activity"] = time.monotonic()


def serve(socket_path: Path, helper: Path, *, idle_timeout: float) -> int:
    # Import lazily so the provider can import the client class without a cycle.
    from .wkwebview_provider import _WKSharedResumeBroker

    socket_path = Path(socket_path)
    runtime_dir = socket_path.parent
    runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime_dir, 0o700)
    socket_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    broker = _WKSharedResumeBroker(Path(helper), idle_timeout=1.0)
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
                name="wk-system-resume-client",
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
    return serve(Path(args.socket), Path(args.helper), idle_timeout=max(0.5, args.idle_timeout))


if __name__ == "__main__":
    raise SystemExit(main())
