from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from chatgpt_web_adapter.generated_images import (
    GeneratedImageError,
    download_generated_images,
    find_generated_images,
)

CONVERSATION_ID = "6ae21820-60a8-83eb-a0b6-4747b539f566"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _node(node_id, parent, role, parts, *, content_type="text", recipient="all", **extra):
    return node_id, {
        "id": node_id,
        "parent": parent,
        "message": {
            "id": node_id,
            "author": {"role": role},
            "recipient": recipient,
            "content": {"content_type": content_type, "parts": parts},
            **extra,
        },
    }


def _image_part(file_id, size):
    return {
        "content_type": "image_asset_pointer",
        "asset_pointer": f"sediment://{file_id}",
        "width": 1536,
        "height": 1024,
        "size_bytes": size,
    }


def _payload(*, final=True, size=len(PNG)):
    nodes = dict(
        [
            _node("u1", None, "user", ["first prompt"]),
            _node(
                "t1",
                "u1",
                "tool",
                [_image_part("file_00000000aaaa", size)],
                content_type="multimodal_text",
            ),
            _node("a1", "t1", "assistant", ["done"], end_turn=True),
            _node(
                "u2",
                "a1",
                "user",
                [_image_part("file_00000000uuuu", 10), "refine"],
                content_type="multimodal_text",
            ),
            _node(
                "t2",
                "u2",
                "tool",
                [_image_part("file_00000000bbbb", size)],
                content_type="multimodal_text",
            ),
        ]
    )
    last = _node(
        "a2",
        "t2",
        "assistant",
        ["done again"],
        **({"end_turn": True} if final else {"status": "in_progress"}),
    )
    nodes[last[0]] = last[1]
    return {"mapping": nodes, "current_node": "a2"}


class _Provider:
    def __init__(self, blobs):
        self.blobs = blobs
        self.calls = []

    def download_file(self, file_id, *, timeout):
        self.calls.append(file_id)
        return self.blobs[file_id], {"status": "success"}


class _Runtime:
    def __init__(self, payloads, blobs):
        self.payloads = list(payloads)
        self.canonical = type("Canonical", (), {})()
        self.canonical.provider = _Provider(blobs)

    def get_conversation_payload(self, ref):
        assert ref.conversation_id == CONVERSATION_ID
        return self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]


def test_last_turn_only_and_user_uploads_ignored():
    images = find_generated_images(_payload())
    assert [image.file_id for image in images] == ["file_00000000bbbb"]
    everything = find_generated_images(_payload(), all_turns=True)
    assert [image.file_id for image in everything] == [
        "file_00000000aaaa",
        "file_00000000bbbb",
    ]


def test_download_waits_for_final_turn_and_checks_size(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    runtime = _Runtime(
        [_payload(final=False), _payload()],
        {"file_00000000bbbb": PNG},
    )
    (tmp_path / "shot.png").write_bytes(b"keep")
    [image] = download_generated_images(runtime, CONVERSATION_ID, tmp_path, name="shot")
    assert Path(image.path).name == "shot_2.png"
    assert Path(image.path).read_bytes() == PNG
    assert (tmp_path / "shot.png").read_bytes() == b"keep"
    assert image.sha256 == hashlib.sha256(PNG).hexdigest()
    assert runtime.canonical.provider.calls == ["file_00000000bbbb"]


def test_size_mismatch_writes_nothing(tmp_path: Path):
    runtime = _Runtime([_payload(size=len(PNG) + 1)], {"file_00000000bbbb": PNG})
    with pytest.raises(GeneratedImageError, match="SIZE_MISMATCH"):
        download_generated_images(runtime, CONVERSATION_ID, tmp_path)
    assert list(tmp_path.iterdir()) == []
