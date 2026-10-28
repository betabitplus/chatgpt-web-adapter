from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .types import ConversationRef

# ChatGPT image generation stores each picture as a tool message part
# {"content_type": "image_asset_pointer", "asset_pointer": "sediment://file_..."}
# with its exact byte size. The file id inside the pointer is the product's own
# identity for the picture; the size is the integrity check for the download.
_POINTER_PREFIXES = ("sediment://", "file-service://")
_FILE_ID_RE = re.compile(r"^file[-_][A-Za-z0-9]+$")
_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"RIFF": ".webp",
}


class GeneratedImageError(RuntimeError):
    def __init__(self, reason_code: str, *, conversation_id: str | None = None):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.conversation_id = conversation_id


@dataclass(frozen=True)
class GeneratedImageRef:
    file_id: str
    message_id: str
    width: int | None
    height: int | None
    size_bytes: int | None


@dataclass(frozen=True)
class DownloadedImage:
    path: str
    file_id: str
    message_id: str
    width: int | None
    height: int | None
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _file_id_from_pointer(pointer: Any) -> str | None:
    if not isinstance(pointer, str):
        return None
    for prefix in _POINTER_PREFIXES:
        if pointer.startswith(prefix):
            candidate = pointer[len(prefix) :].strip()
            return candidate if _FILE_ID_RE.match(candidate) else None
    return None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _current_branch(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Messages of the current branch, oldest first."""

    mapping = payload.get("mapping")
    current = payload.get("current_node")
    if not isinstance(mapping, dict) or not isinstance(current, str):
        return []
    branch: list[dict[str, Any]] = []
    seen: set[str] = set()
    while isinstance(current, str) and current and current not in seen:
        seen.add(current)
        node = mapping.get(current)
        if not isinstance(node, dict):
            break
        message = node.get("message")
        if isinstance(message, dict):
            branch.append(message)
        current = node.get("parent")
    branch.reverse()
    return branch


def find_generated_images(
    payload: dict[str, Any],
    *,
    all_turns: bool = False,
) -> list[GeneratedImageRef]:
    """Generated pictures on the current branch.

    By default only the last turn counts: messages after the latest user message,
    so pictures the user attached (they live in the user message) never qualify.
    """

    branch = _current_branch(payload)
    if not all_turns:
        last_user = max(
            (
                index
                for index, message in enumerate(branch)
                if (message.get("author") or {}).get("role") == "user"
            ),
            default=-1,
        )
        branch = branch[last_user + 1 :]
    found: list[GeneratedImageRef] = []
    seen: set[str] = set()
    for message in branch:
        if (message.get("author") or {}).get("role") == "user":
            continue
        content = message.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("content_type") != "image_asset_pointer":
                continue
            file_id = _file_id_from_pointer(part.get("asset_pointer"))
            if file_id is None or file_id in seen:
                continue
            seen.add(file_id)
            found.append(
                GeneratedImageRef(
                    file_id=file_id,
                    message_id=str(message.get("id") or ""),
                    width=_int_or_none(part.get("width")),
                    height=_int_or_none(part.get("height")),
                    size_bytes=_int_or_none(part.get("size_bytes")),
                )
            )
    return found


def _turn_is_final(payload: dict[str, Any]) -> bool:
    branch = _current_branch(payload)
    if not branch:
        return False
    last = branch[-1]
    if (last.get("author") or {}).get("role") != "assistant":
        return False
    if last.get("recipient") not in {None, "all"}:
        return False
    metadata = last.get("metadata") or {}
    return bool(
        last.get("end_turn") is True
        or isinstance(metadata.get("finish_details"), dict)
        or last.get("status") == "finished_successfully"
    )


def _downloader(runtime: Any) -> Callable[..., tuple[bytes, dict[str, Any]]]:
    canonical = getattr(runtime, "canonical", None)
    provider = getattr(canonical, "provider", None)
    download = getattr(provider, "download_file", None)
    if callable(download):
        return download
    raise GeneratedImageError("GENERATED_IMAGE_DOWNLOAD_UNSUPPORTED_TRANSPORT")


def _suffix_for(raw: bytes) -> str:
    for magic, suffix in _IMAGE_MAGIC.items():
        if raw.startswith(magic):
            return suffix
    raise GeneratedImageError("GENERATED_IMAGE_NOT_AN_IMAGE")


def _write_new(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def _target_path(output_dir: Path, stem: str, suffix: str) -> Path:
    candidate = output_dir / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = output_dir / f"{stem}_{index}{suffix}"
        index += 1
    return candidate


def wait_for_generated_images(
    runtime: Any,
    conversation: Any,
    *,
    timeout: float = 300.0,
    poll_interval: float = 5.0,
    all_turns: bool = False,
) -> tuple[dict[str, Any], list[GeneratedImageRef]]:
    """Poll the canonical conversation until the last turn is final."""

    ref = ConversationRef.from_any(conversation)
    deadline = time.monotonic() + timeout
    while True:
        payload = runtime.get_conversation_payload(ref)
        images = find_generated_images(payload, all_turns=all_turns)
        if _turn_is_final(payload):
            return payload, images
        if time.monotonic() >= deadline:
            raise GeneratedImageError(
                "GENERATED_IMAGE_TURN_NOT_FINAL",
                conversation_id=ref.conversation_id,
            )
        time.sleep(poll_interval)


def download_generated_images(
    runtime: Any,
    conversation: Any,
    output_dir: str | Path,
    *,
    name: str = "image",
    all_turns: bool = False,
    wait_timeout: float = 300.0,
    download_timeout: float = 120.0,
) -> list[DownloadedImage]:
    """Download the pictures ChatGPT generated in the conversation's last turn.

    Each file is fetched by the product's own file id and kept only when its byte
    count equals the size the conversation records for it. Existing files are
    never overwritten: a numeric suffix is added instead.
    """

    ref = ConversationRef.from_any(conversation)
    _payload, images = wait_for_generated_images(
        runtime,
        ref,
        timeout=wait_timeout,
        all_turns=all_turns,
    )
    if not images:
        raise GeneratedImageError(
            "GENERATED_IMAGE_NOT_FOUND",
            conversation_id=ref.conversation_id,
        )
    download = _downloader(runtime)
    target_dir = Path(output_dir)
    results: list[DownloadedImage] = []
    for index, image in enumerate(images, start=1):
        raw, _metadata = download(image.file_id, timeout=download_timeout)
        if image.size_bytes is not None and len(raw) != image.size_bytes:
            raise GeneratedImageError(
                "GENERATED_IMAGE_SIZE_MISMATCH",
                conversation_id=ref.conversation_id,
            )
        suffix = _suffix_for(raw)
        stem = name if len(images) == 1 else f"{name}_{index}"
        path = _target_path(target_dir, stem, suffix)
        _write_new(path, raw)
        results.append(
            DownloadedImage(
                path=str(path),
                file_id=image.file_id,
                message_id=image.message_id,
                width=image.width,
                height=image.height,
                size_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    return results


def generate_images(
    runtime: Any,
    prompt: str,
    output_dir: str | Path,
    *,
    media: Sequence[Any] | None = None,
    conversation: Any = None,
    name: str = "image",
    wait_timeout: float = 300.0,
    **send_kwargs: Any,
) -> dict[str, Any]:
    """Send one turn (prompt plus optional input pictures) and save its pictures.

    Continue refining by passing the returned conversation_id as ``conversation``.
    """

    kwargs = dict(send_kwargs)
    if media:
        kwargs["media"] = list(media)
    if conversation is not None:
        kwargs["conversation"] = conversation
    execution = runtime.send_text_observed(prompt, **kwargs)
    response = execution.response
    conversation_id = response.conversation.conversation_id
    if not conversation_id:
        raise GeneratedImageError("GENERATED_IMAGE_CONVERSATION_ID_MISSING")
    images = download_generated_images(
        runtime,
        conversation_id,
        output_dir,
        name=name,
        wait_timeout=wait_timeout,
    )
    return {
        "conversation_id": conversation_id,
        "message_id": response.conversation.message_id,
        "text": response.text,
        "images": [image.to_dict() for image in images],
    }


__all__ = [
    "DownloadedImage",
    "GeneratedImageError",
    "GeneratedImageRef",
    "download_generated_images",
    "find_generated_images",
    "generate_images",
    "wait_for_generated_images",
]
