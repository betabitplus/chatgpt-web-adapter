from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any, Iterator, Sequence

from .browser_native_provider import (
    BrowserNativeBridgeStatus,
    BrowserNativeRuntimeTabReleaseResult,
    BrowserNativeTurnResult,
)
from .exceptions import RequestError
from .product_model_profile_pr8_10 import (
    PROFILE_TO_PRODUCT_MODE,
    normalize_model_profile,
)
from .status import _status_from_payload
from .types import ChatConversation, ConversationRef

_HELPER_BUNDLE_ID = "local.gptty.webkit-authority"
_HELPER_DIRNAME = "wkwebview-authority"
_RESULT_PREFIX = "WK_RESULT "
_EVENT_PREFIX = "WK_EVENT "


class WKWebViewTurnProvider:
    """macOS browser-owned turn provider backed by a minimal WKWebView app.

    The provider owns the protected product write while the existing CWA runtime
    retains canonical reconciliation, finality, and product semantics.
    """

    browser_authority_backend = "wkwebview"
    streaming_source = "WKWEBVIEW_PASSIVE_SSE"
    revision_safe_streaming_supported = True
    supports_attachment_paths = True
    supports_model_slug = False
    temporary_chat_supported = False

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        turn_timeout: float = 150.0,
        build_timeout: float = 30.0,
    ) -> None:
        if turn_timeout <= 0:
            raise ValueError("turn_timeout must be positive")
        if build_timeout <= 0:
            raise ValueError("build_timeout must be positive")
        self.state_dir = (
            Path(state_dir).expanduser()
            if state_dir is not None
            else Path.home() / "Library" / "Application Support" / "chatgpt-web-adapter"
        )
        self.turn_timeout = float(turn_timeout)
        self.build_timeout = float(build_timeout)
        self._profile_context = threading.local()
        self._authority_context = threading.local()
        self._canonical_context = threading.local()
        self._build_lock = threading.Lock()
        self._stopped_lock = threading.Lock()
        self._stopped_conversations: set[str] = set()

    @property
    def helper_root(self) -> Path:
        return self.state_dir / _HELPER_DIRNAME

    @property
    def helper_app(self) -> Path:
        return self.helper_root / "WKChatGPTAuthority.app"

    @property
    def helper_binary(self) -> Path:
        return self.helper_app / "Contents" / "MacOS" / "WKChatGPTAuthority"

    def _source_paths(self) -> tuple[Path, Path]:
        package_root = resources.files("chatgpt_web_adapter")
        source = Path(str(package_root.joinpath("wkwebview_helper", "WKChatGPTAuthority.m")))
        plist = Path(str(package_root.joinpath("wkwebview_helper", "Info.plist")))
        return source, plist

    @staticmethod
    def _source_digest(source: Path, plist: Path) -> str:
        digest = hashlib.sha256()
        digest.update(source.read_bytes())
        digest.update(plist.read_bytes())
        return digest.hexdigest()

    def _ensure_helper(self) -> Path:
        if sys.platform != "darwin":
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_UNAVAILABLE: macOS is required",
                request_stage="wkwebview_authority_build",
            )
        source, plist = self._source_paths()
        if not source.is_file() or not plist.is_file():
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_SOURCE_MISSING",
                request_stage="wkwebview_authority_build",
            )
        if shutil.which("clang") is None or shutil.which("codesign") is None:
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_TOOLCHAIN_MISSING: clang/codesign required",
                request_stage="wkwebview_authority_build",
            )

        expected = self._source_digest(source, plist)
        stamp = self.helper_root / "source.sha256"
        with self._build_lock:
            if self.helper_binary.is_file() and stamp.is_file():
                try:
                    if stamp.read_text(encoding="utf-8").strip() == expected:
                        return self.helper_binary
                except OSError:
                    pass

            macos_dir = self.helper_app / "Contents" / "MacOS"
            macos_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(plist, self.helper_app / "Contents" / "Info.plist")
            command = [
                "clang",
                "-fobjc-arc",
                "-framework",
                "Cocoa",
                "-framework",
                "WebKit",
                str(source),
                "-o",
                str(self.helper_binary),
            ]
            self._run_build(command)
            self._run_build(["codesign", "--force", "--deep", "--sign", "-", str(self.helper_app)])
            stamp.write_text(expected + "\n", encoding="utf-8")
        return self.helper_binary

    def _run_build(self, command: list[str]) -> None:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.build_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_BUILD_FAILED: {error}",
                request_stage="wkwebview_authority_build",
            ) from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "build failed").strip()
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_BUILD_FAILED: {detail[-1500:]}",
                request_stage="wkwebview_authority_build",
            )

    def status(self) -> BrowserNativeBridgeStatus:
        try:
            binary = self._ensure_helper()
        except Exception:
            return BrowserNativeBridgeStatus(False, False)
        return BrowserNativeBridgeStatus(
            available=binary.is_file(),
            extension_connected=binary.is_file(),
            host_pid=None,
            extension_id="wkwebview-authority",
            runtime_tab_id=None,
        )

    def build_canonical_client(self, source_client: Any) -> Any:
        from .wkwebview_canonical import WKWebViewCanonicalClient

        return WKWebViewCanonicalClient(source_client, self)

    @staticmethod
    def _decode_helper_json(
        payload: dict[str, Any],
        *,
        request_stage: str,
        error_prefix: str,
    ) -> dict[str, Any]:
        status = payload.get("status")
        if not isinstance(status, int) or status < 200 or status >= 300:
            raise RequestError(
                f"{error_prefix}_HTTP_STATUS:{status}",
                request_stage=request_stage,
                status_code=status if isinstance(status, int) else None,
            )
        encoded = payload.get("body_base64")
        if not isinstance(encoded, str):
            raise RequestError(
                f"{error_prefix}_BODY_MISSING",
                request_stage=request_stage,
            )
        try:
            raw = base64.b64decode(encoded, validate=True)
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RequestError(
                f"{error_prefix}_JSON_INVALID",
                request_stage=request_stage,
            ) from error
        if not isinstance(parsed, dict):
            raise RequestError(
                f"{error_prefix}_JSON_OBJECT_REQUIRED",
                request_stage=request_stage,
            )
        return parsed

    def read_conversation_payload(
        self,
        conversation_id: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        ref = ConversationRef(conversation_id)
        total_timeout = float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")
        binary = self._ensure_helper()
        payload = self._run_helper(
            [
                str(binary),
                "--canonical-conversation",
                ref.conversation_id,
                "--timeout",
                f"{total_timeout:.3f}",
            ],
            timeout=total_timeout,
        )
        parsed = self._decode_helper_json(
            payload,
            request_stage="wkwebview_canonical_read",
            error_prefix="WKWEBVIEW_CANONICAL",
        )
        cache = getattr(self._canonical_context, "current_nodes", None)
        if not isinstance(cache, dict):
            cache = {}
            self._canonical_context.current_nodes = cache
        current_node = parsed.get("current_node")
        cache[ref.conversation_id] = (
            current_node if isinstance(current_node, str) and current_node else None
        )
        return parsed

    def read_catalog_payload(
        self,
        catalog: str,
        *,
        offset: int = 0,
        limit: int = 100,
        is_archived: bool = False,
        is_starred: bool = False,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        normalized = catalog.strip().lower() if isinstance(catalog, str) else ""
        if normalized not in {"conversations", "models"}:
            raise ValueError("catalog must be conversations or models")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative int")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an int between 1 and 100")
        total_timeout = float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")
        binary = self._ensure_helper()
        command = [
            str(binary),
            "--catalog",
            normalized,
            "--offset",
            str(offset),
            "--limit",
            str(limit),
            "--timeout",
            f"{total_timeout:.3f}",
        ]
        if is_archived:
            command.append("--archived")
        if is_starred:
            command.append("--starred")
        payload = self._run_helper(command, timeout=total_timeout)
        return self._decode_helper_json(
            payload,
            request_stage="wkwebview_catalog_read",
            error_prefix="WKWEBVIEW_CATALOG",
        )

    def _cached_current_node(self, conversation_id: str | None) -> str | None:
        if not conversation_id:
            return None
        cache = getattr(self._canonical_context, "current_nodes", None)
        if not isinstance(cache, dict):
            return None
        value = cache.get(conversation_id)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _current_branch_contains_user_text(payload: dict[str, Any], text: str) -> bool:
        mapping = payload.get("mapping")
        current = payload.get("current_node")
        if not isinstance(mapping, dict) or not isinstance(current, str):
            return False
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            node = mapping.get(current)
            if not isinstance(node, dict):
                return False
            message = node.get("message")
            if isinstance(message, dict):
                author = message.get("author")
                content = message.get("content")
                parts = content.get("parts") if isinstance(content, dict) else None
                if (
                    isinstance(author, dict)
                    and author.get("role") == "user"
                    and isinstance(parts, list)
                ):
                    rendered = "\n".join(part for part in parts if isinstance(part, str))
                    if rendered.strip() == text.strip():
                        return True
            parent = node.get("parent")
            current = parent if isinstance(parent, str) else ""
        return False

    def _wait_for_canonical_write_commit(
        self,
        *,
        conversation_id: str,
        text: str,
        baseline_current_node: str | None,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(2.0, min(float(timeout), 30.0))
        while time.monotonic() < deadline:
            try:
                payload = self.read_conversation_payload(
                    conversation_id,
                    timeout=min(10.0, max(1.0, deadline - time.monotonic())),
                )
            except Exception:
                time.sleep(0.25)
                continue
            current_node = payload.get("current_node")
            node_changed = (
                baseline_current_node is None
                or (
                    isinstance(current_node, str)
                    and current_node
                    and current_node != baseline_current_node
                )
            )
            if node_changed and self._current_branch_contains_user_text(payload, text):
                return payload
            time.sleep(0.25)
        raise RequestError(
            "WKWEBVIEW_WRITE_CANONICAL_COMMIT_NOT_PROVEN",
            request_stage="wkwebview_authority_postwrite",
        )

    @contextmanager
    def require_profile(self, profile: str) -> Iterator[str]:
        normalized = normalize_model_profile(profile)
        if getattr(self._profile_context, "profile", None) is not None:
            raise RuntimeError("nested model-profile requirements are not supported")
        self._profile_context.profile = normalized
        try:
            yield normalized
        finally:
            if hasattr(self._profile_context, "profile"):
                del self._profile_context.profile

    def set_browser_authority_lease(self, lease_id: str) -> None:
        if not isinstance(lease_id, str) or not lease_id.strip():
            raise ValueError("browser authority lease_id is required")
        self._authority_context.lease_id = lease_id.strip()

    def clear_browser_authority_lease(self) -> None:
        if hasattr(self._authority_context, "lease_id"):
            del self._authority_context.lease_id

    def _current_browser_authority_lease_id(self) -> str | None:
        value = getattr(self._authority_context, "lease_id", None)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _normalize_attachment_paths(
        attachment_paths: Sequence[str | Path] | None,
    ) -> tuple[str, ...]:
        if attachment_paths is None:
            return ()
        if isinstance(attachment_paths, (str, bytes, bytearray, Path)):
            raise TypeError("attachment_paths must be a sequence of local paths")
        normalized: list[str] = []
        for index, raw_path in enumerate(attachment_paths):
            if not isinstance(raw_path, (str, Path)):
                raise TypeError(f"attachment_paths[{index}] must be str or Path")
            try:
                path = Path(raw_path).expanduser().resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ValueError(f"attachment_paths[{index}] is unavailable") from error
            if not path.is_file():
                raise ValueError(f"attachment_paths[{index}] must reference a regular file")
            normalized.append(str(path))
        return tuple(normalized)

    def _helper_command(
        self,
        *,
        conversation_id: str | None,
        text: str | None,
        timeout: float,
        attachment_paths: Sequence[str] = (),
        expected_current_node: str | None = None,
        stop_only: bool = False,
    ) -> list[str]:
        binary = self._ensure_helper()
        url = (
            f"https://chatgpt.com/c/{conversation_id}"
            if conversation_id
            else "https://chatgpt.com/"
        )
        command = [str(binary), "--url", url, "--timeout", f"{timeout:.3f}"]
        if text is not None:
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            command += ["--prompt-base64", encoded]
        if isinstance(expected_current_node, str) and expected_current_node.strip():
            command += ["--expected-current-node", expected_current_node.strip()]
        for path in attachment_paths:
            command += ["--attach", path]
        profile = getattr(self._profile_context, "profile", None)
        if isinstance(profile, str):
            command += ["--profile", PROFILE_TO_PRODUCT_MODE[profile]]
        if stop_only:
            command.append("--stop-only")
        return command

    def _run_helper(self, command: list[str], *, timeout: float) -> dict[str, Any]:
        env = os.environ.copy()
        env.setdefault("NSUnbufferedIO", "YES")
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=max(1.0, timeout + 5.0),
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_TIMEOUT",
                request_stage="wkwebview_authority_turn",
            ) from error
        except OSError as error:
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_LAUNCH_FAILED: {error}",
                request_stage="wkwebview_authority_turn",
            ) from error

        payload = None
        for line in reversed(completed.stdout.splitlines()):
            if line.startswith(_RESULT_PREFIX):
                try:
                    candidate = json.loads(line[len(_RESULT_PREFIX) :])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    payload = candidate
                    break
        if payload is None:
            detail = (completed.stderr or completed.stdout or "no helper result").strip()
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_NO_RESULT: {detail[-2000:]}",
                request_stage="wkwebview_authority_turn",
            )
        if payload.get("ok") is not True:
            raise RequestError(
                str(payload.get("error") or "WKWEBVIEW_AUTHORITY_TURN_FAILED"),
                request_stage="wkwebview_authority_turn",
            )
        return payload

    def _run_helper_streaming(
        self,
        command: list[str],
        *,
        timeout: float,
        on_text_event: Any,
    ) -> dict[str, Any]:
        if not callable(on_text_event):
            raise TypeError("on_text_event must be callable")
        env = os.environ.copy()
        env.setdefault("NSUnbufferedIO", "YES")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as error:
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_LAUNCH_FAILED: {error}",
                request_stage="wkwebview_authority_turn",
            ) from error

        deadline = time.monotonic() + max(1.0, timeout + 5.0)
        payload: dict[str, Any] | None = None
        try:
            assert process.stdout is not None
            while time.monotonic() < deadline:
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    time.sleep(0.02)
                    continue
                if line.startswith(_EVENT_PREFIX):
                    try:
                        event = json.loads(line[len(_EVENT_PREFIX) :])
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") not in {
                        "assistant_text_snapshot",
                        "assistant_text_delta",
                        "assistant_text_revision",
                    }:
                        continue
                    try:
                        on_text_event(event)
                    except Exception:
                        pass
                    continue
                if not line.startswith(_RESULT_PREFIX):
                    continue
                try:
                    candidate = json.loads(line[len(_RESULT_PREFIX) :])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    payload = candidate
                    break
        finally:
            if process.poll() is None:
                self._terminate_observer_process(process)

        if payload is None:
            detail = "no helper result"
            if process.stderr is not None:
                try:
                    stderr = process.stderr.read().strip()
                except Exception:
                    stderr = ""
                if stderr:
                    detail = stderr
            if time.monotonic() >= deadline:
                detail = "streaming helper timed out"
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_NO_RESULT: {detail[-2000:]}",
                request_stage="wkwebview_authority_turn",
            )
        if payload.get("ok") is not True:
            raise RequestError(
                str(payload.get("error") or "WKWEBVIEW_AUTHORITY_TURN_FAILED"),
                request_stage="wkwebview_authority_turn",
            )
        return payload

    @staticmethod
    def _terminate_observer_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)

    def observe_turn(
        self,
        *,
        conversation_id: str,
        turn_exchange_id: str | None,
        browser_authority_lease_id: str,
        timeout: float,
        on_event: Any = None,
    ) -> dict[str, Any]:
        """Observe one committed turn through one lightweight WebKit context.

        The helper stays on ``robots.txt`` and emits authenticated canonical
        snapshots. It owns no product write authority; shared CWA code remains
        responsible for interpreting snapshots and reconciling canonical finality.
        """

        ref = ConversationRef(conversation_id)
        if not isinstance(browser_authority_lease_id, str) or not browser_authority_lease_id.strip():
            raise ValueError("browser_authority_lease_id is required")
        total_timeout = float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")

        binary = self._ensure_helper()
        command = [
            str(binary),
            "--observe-conversation",
            ref.conversation_id,
            "--poll-interval",
            "1.000",
            "--timeout",
            f"{total_timeout:.3f}",
        ]
        env = os.environ.copy()
        env.setdefault("NSUnbufferedIO", "YES")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as error:
            raise RequestError(
                f"WKWEBVIEW_CANONICAL_OBSERVER_LAUNCH_FAILED: {error}",
                request_stage="browser_native_observe_turn",
            ) from error

        deadline = time.monotonic() + total_timeout
        last_message_id: str | None = None
        last_finish_reason: str | None = None
        try:
            assert process.stdout is not None
            while time.monotonic() < deadline:
                if self.stop_requested_for(ref.conversation_id):
                    return {
                        "ok": True,
                        "conversationId": ref.conversation_id,
                        "turnExchangeId": turn_exchange_id,
                        "messageId": last_message_id,
                        "finishReason": "stopped",
                    }

                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                if not line.startswith(_EVENT_PREFIX):
                    continue
                try:
                    raw_event = json.loads(line[len(_EVENT_PREFIX) :])
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw_event, dict) or raw_event.get("type") != "canonical_payload":
                    continue
                try:
                    payload = self._decode_helper_json(
                        raw_event,
                        request_stage="browser_native_observe_turn",
                        error_prefix="WKWEBVIEW_CANONICAL_OBSERVER",
                    )
                except RequestError as error:
                    if error.status_code in {401, 403}:
                        raise
                    continue

                current_node = payload.get("current_node")
                cache = getattr(self._canonical_context, "current_nodes", None)
                if not isinstance(cache, dict):
                    cache = {}
                    self._canonical_context.current_nodes = cache
                cache[ref.conversation_id] = (
                    current_node if isinstance(current_node, str) and current_node else None
                )

                if on_event is not None:
                    try:
                        on_event({"type": "canonical_payload_snapshot", "payload": payload})
                    except Exception:
                        pass

                status = _status_from_payload(payload)
                last_message_id = status.message_id or last_message_id
                last_finish_reason = status.finish_reason or last_finish_reason
                if status.status == "completed":
                    return {
                        "ok": True,
                        "conversationId": ref.conversation_id,
                        "turnExchangeId": turn_exchange_id,
                        "messageId": last_message_id,
                        "finishReason": last_finish_reason or "stop",
                    }
        finally:
            self._terminate_observer_process(process)

        raise RequestError(
            "PASSIVE_OBSERVER_STREAM_ENDED_WITHOUT_TERMINAL",
            request_stage="browser_native_observe_turn",
        )

    def _send_text_impl(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str | None = None,
        timeout: float | None = None,
        attachment_paths: Sequence[str | Path] | None = None,
        model_slug: str | None = None,
        on_text_event: Any = None,
    ) -> BrowserNativeTurnResult:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text is required")
        total_timeout = self.turn_timeout if timeout is None else float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")
        conversation_id = None
        baseline_current_node = None
        if conversation is not None:
            conversation_id = ConversationRef.from_any(conversation).conversation_id
            self.clear_stop_requested_for(conversation_id)
            baseline_current_node = self._cached_current_node(conversation_id)
            if baseline_current_node is None:
                prewrite_payload = self.read_conversation_payload(
                    conversation_id,
                    timeout=min(15.0, total_timeout),
                )
                value = prewrite_payload.get("current_node")
                baseline_current_node = value if isinstance(value, str) and value else None
        attachments = self._normalize_attachment_paths(attachment_paths)
        command = self._helper_command(
            conversation_id=conversation_id,
            text=text,
            timeout=total_timeout,
            attachment_paths=attachments,
            expected_current_node=baseline_current_node,
        )
        if on_text_event is not None:
            command += ["--observe-stream", "--stream-probe-until-end"]
        started = time.monotonic()
        payload = (
            self._run_helper_streaming(
                command,
                timeout=total_timeout,
                on_text_event=on_text_event,
            )
            if on_text_event is not None
            else self._run_helper(command, timeout=total_timeout)
        )
        result_conversation_id = payload.get("conversation_id")
        if not isinstance(result_conversation_id, str) or not result_conversation_id.strip():
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_CONVERSATION_ID_UNRESOLVED",
                request_stage="wkwebview_authority_turn",
            )
        response_status = payload.get("response_status", 200)
        if not isinstance(response_status, int) or not (200 <= response_status < 300):
            raise RequestError(
                f"WKWEBVIEW_AUTHORITY_HTTP_STATUS:{response_status}",
                request_stage="wkwebview_authority_turn",
                status_code=response_status if isinstance(response_status, int) else None,
            )
        attachment_count = payload.get("attachment_count", 0)
        if not isinstance(attachment_count, int) or isinstance(attachment_count, bool):
            attachment_count = 0
        if attachments and attachment_count != len(attachments):
            raise RequestError(
                "WKWEBVIEW_AUTHORITY_ATTACHMENT_COUNT_MISMATCH",
                request_stage="wkwebview_authority_turn",
            )
        self._wait_for_canonical_write_commit(
            conversation_id=result_conversation_id.strip(),
            text=text,
            baseline_current_node=baseline_current_node,
            timeout=min(30.0, total_timeout),
        )
        elapsed_ms = payload.get("elapsed_ms")
        if not isinstance(elapsed_ms, int):
            elapsed_ms = int((time.monotonic() - started) * 1000)
        return BrowserNativeTurnResult(
            conversation_id=result_conversation_id.strip(),
            turn_exchange_id=(
                payload.get("turn_exchange_id")
                if isinstance(payload.get("turn_exchange_id"), str)
                else None
            ),
            response_status=response_status,
            response_mime_type="text/event-stream",
            final_url=payload.get("final_url") if isinstance(payload.get("final_url"), str) else None,
            tab_id=None,
            tab_was_active=False,
            elapsed_ms=elapsed_ms,
            runtime_reloaded=True,
            runtime_reload_ms=payload.get("load_elapsed_ms")
            if isinstance(payload.get("load_elapsed_ms"), int)
            else None,
            runtime_tab_preexisting=False,
            runtime_tab_created_for_turn=True,
            tab_active_after=False,
            tab_activated_during_turn=False,
            foreground_activation_observed=False,
            browser_authority_lease_id=self._current_browser_authority_lease_id(),
            attachment_count=attachment_count,
            passive_observer_armed=on_text_event is None,
        )

    def send_text(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str | None = None,
        timeout: float | None = None,
        attachment_paths: Sequence[str | Path] | None = None,
        model_slug: str | None = None,
    ) -> BrowserNativeTurnResult:
        return self._send_text_impl(
            text,
            conversation=conversation,
            timeout=timeout,
            attachment_paths=attachment_paths,
            model_slug=model_slug,
        )

    def send_text_streaming(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str | None = None,
        timeout: float | None = None,
        attachment_paths: Sequence[str | Path] | None = None,
        model_slug: str | None = None,
        on_text_event: Any,
    ) -> BrowserNativeTurnResult:
        return self._send_text_impl(
            text,
            conversation=conversation,
            timeout=timeout,
            attachment_paths=attachment_paths,
            model_slug=model_slug,
            on_text_event=on_text_event,
        )

    def send_text_with_stale_ui_recovery(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str,
        timeout: float | None = None,
        canonical_completed_at_ms: int,
        attachment_paths: Sequence[str | Path] | None = None,
        model_slug: str | None = None,
    ) -> BrowserNativeTurnResult:
        # Every WKWebView turn starts from a fresh route, so there is no retained
        # stale composer state to repair. The canonical completion proof is still
        # owned by the caller and this remains a single write attempt.
        if isinstance(canonical_completed_at_ms, bool) or canonical_completed_at_ms <= 0:
            raise ValueError("canonical_completed_at_ms must be a positive integer")
        return self.send_text(
            text,
            conversation=conversation,
            timeout=timeout,
            attachment_paths=attachment_paths,
            model_slug=model_slug,
        )

    def send_text_with_stale_ui_recovery_streaming(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str,
        timeout: float | None = None,
        canonical_completed_at_ms: int,
        attachment_paths: Sequence[str | Path] | None = None,
        model_slug: str | None = None,
        on_text_event: Any,
    ) -> BrowserNativeTurnResult:
        if isinstance(canonical_completed_at_ms, bool) or canonical_completed_at_ms <= 0:
            raise ValueError("canonical_completed_at_ms must be a positive integer")
        return self.send_text_streaming(
            text,
            conversation=conversation,
            timeout=timeout,
            attachment_paths=attachment_paths,
            model_slug=model_slug,
            on_text_event=on_text_event,
        )

    def stop_generation(
        self,
        conversation_id: str | None = None,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if conversation_id is None:
            raise ValueError("WKWebView stop_generation requires a conversation id")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be a non-empty string")
        total_timeout = float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")
        conversation_id = conversation_id.strip()
        command = self._helper_command(
            conversation_id=conversation_id,
            text=None,
            timeout=total_timeout,
            stop_only=True,
        )
        payload = self._run_helper(command, timeout=total_timeout)
        stopped = payload.get("stopped") is True
        if stopped:
            self._mark_conversation_stopped(conversation_id)
        return {
            "ok": True,
            "stopped": stopped,
            "conversationId": conversation_id,
            "provider": "wkwebview",
        }

    def release_runtime_tab(
        self,
        *,
        expected_runtime_tab_id: int | None,
        browser_authority_lease_id: str,
        timeout: float = 10.0,
    ) -> BrowserNativeRuntimeTabReleaseResult:
        if not isinstance(browser_authority_lease_id, str) or not browser_authority_lease_id.strip():
            raise ValueError("browser_authority_lease_id is required")
        # One-shot helper processes are already gone when the write result is
        # returned. Report an already-absent authority to the existing lease model.
        return BrowserNativeRuntimeTabReleaseResult(
            released=False,
            already_absent=True,
            runtime_tab_id=None,
            browser_authority_lease_id=browser_authority_lease_id.strip(),
        )

    def _mark_conversation_stopped(self, conversation_id: str) -> None:
        with self._stopped_lock:
            self._stopped_conversations.add(conversation_id)

    def stop_requested_for(self, conversation_id: str) -> bool:
        with self._stopped_lock:
            return conversation_id in self._stopped_conversations

    def clear_stop_requested_for(self, conversation_id: str) -> None:
        with self._stopped_lock:
            self._stopped_conversations.discard(conversation_id)


__all__ = ["WKWebViewTurnProvider"]
