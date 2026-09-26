from __future__ import annotations

import hashlib
import importlib.metadata
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .artifact_io import write_private_artifact_text

ARTIFACT_MANIFEST_SCHEMA = 2
LEGACY_ARTIFACT_MANIFEST_SCHEMA = 1
SUPPORTED_ARTIFACT_MANIFEST_SCHEMAS = frozenset(
    {LEGACY_ARTIFACT_MANIFEST_SCHEMA, ARTIFACT_MANIFEST_SCHEMA}
)

SNAPSHOT_ARTIFACT_KIND = "conversation_snapshot"
EXPORT_ARTIFACT_KIND = "conversation_export"
SNAPSHOT_CONTRACT = "curated_current_branch_context_v1"
EXPORT_CONTRACT = "normalized_current_branch_export_v1"

CURRENT_BRANCH_REPRESENTATION = "current_branch"
CANONICAL_VISIBLE_GRAPH_REPRESENTATION = "canonical_visible_graph"
DIAGNOSTIC_RAW_SNAPSHOT_REPRESENTATION = "diagnostic_raw_snapshot"
ARTIFACT_REPRESENTATIONS = frozenset(
    {
        CURRENT_BRANCH_REPRESENTATION,
        CANONICAL_VISIBLE_GRAPH_REPRESENTATION,
        DIAGNOSTIC_RAW_SNAPSHOT_REPRESENTATION,
    }
)

ARTIFACT_PRODUCER = "chatgpt-web-adapter"
ARTIFACT_SOURCE = "chatgpt-canonical-read"
ARTIFACT_STORAGE_PRIVACY = "owner_only"
ARTIFACT_STORAGE_CREATION = "private_exclusive"
ARTIFACT_STORAGE_COMPLETION_MARKER = "manifest_last"
_PACKAGE_NAME = "chatgpt-web-adapter"


@dataclass(frozen=True)
class ArtifactFileEntry:
    role: str
    path: str
    media_type: str
    representation: str
    bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": self.path,
            "media_type": self.media_type,
            "representation": self.representation,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ArtifactProvenance:
    producer: str
    producer_version: str
    source: str
    fetched_at: str
    source_revision: str | None
    projection_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "producer": self.producer,
            "producer_version": self.producer_version,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "source_revision": self.source_revision,
            "projection_version": self.projection_version,
        }


@dataclass(frozen=True)
class ArtifactStoragePolicy:
    privacy: str = ARTIFACT_STORAGE_PRIVACY
    creation: str = ARTIFACT_STORAGE_CREATION
    completion_marker: str = ARTIFACT_STORAGE_COMPLETION_MARKER

    def to_dict(self) -> dict[str, str]:
        return {
            "privacy": self.privacy,
            "creation": self.creation,
            "completion_marker": self.completion_marker,
        }


@dataclass(frozen=True)
class StableArtifactManifest:
    artifact_kind: str
    contract: str
    conversation_id: str
    index: int
    files: tuple[ArtifactFileEntry, ...]
    representations: tuple[str, ...]
    content_sha256: str
    provenance: ArtifactProvenance
    storage: ArtifactStoragePolicy
    format: str | None = None
    schema: int = ARTIFACT_MANIFEST_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "artifact_kind": self.artifact_kind,
            "contract": self.contract,
            "conversation_id": self.conversation_id,
            "index": self.index,
            "format": self.format,
            "representations": list(self.representations),
            "content_sha256": self.content_sha256,
            "provenance": self.provenance.to_dict(),
            "storage": self.storage.to_dict(),
            "files": [entry.to_dict() for entry in self.files],
        }


def artifact_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def artifact_source_revision(value: Any) -> str | None:
    """Return a revision marker already present on the supplied object.

    This helper deliberately performs no network read.  A caller that has a canonical
    payload or catalog record can preserve its update/current-node marker; otherwise
    the manifest records a null source revision alongside fetched_at.
    """

    for key in (
        "update_time",
        "updateTime",
        "revision",
        "current_node",
        "currentNode",
        "message_id",
    ):
        candidate = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
        if isinstance(candidate, bool) or candidate is None:
            continue
        if isinstance(candidate, (str, int, float)):
            normalized = str(candidate).strip()
            if normalized:
                return normalized
    return None


def artifact_file_entry(
    path: str | Path,
    *,
    role: str,
    media_type: str,
    representation: str,
) -> ArtifactFileEntry:
    artifact_path = Path(path)
    normalized_representation = _required_representation(representation)
    payload = artifact_path.read_bytes()
    return ArtifactFileEntry(
        role=role,
        path=artifact_path.name,
        media_type=media_type,
        representation=normalized_representation,
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def build_artifact_manifest(
    *,
    artifact_kind: str,
    contract: str,
    conversation_id: str,
    index: int,
    files: Iterable[ArtifactFileEntry],
    format: str | None = None,
    fetched_at: str | None = None,
    source_revision: str | None = None,
    producer_version: str | None = None,
    projection_version: str | None = None,
    source: str = ARTIFACT_SOURCE,
) -> StableArtifactManifest:
    normalized_files = tuple(files)
    if not normalized_files:
        raise ValueError("artifact manifest must contain at least one file")
    if not isinstance(index, int) or isinstance(index, bool) or index <= 0:
        raise ValueError("artifact manifest index must be a positive integer")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError("artifact manifest conversation_id is required")
    if not isinstance(artifact_kind, str) or not artifact_kind.strip():
        raise ValueError("artifact manifest artifact_kind is required")
    if not isinstance(contract, str) or not contract.strip():
        raise ValueError("artifact manifest contract is required")

    normalized_contract = contract.strip()
    representations = tuple(
        dict.fromkeys(_required_representation(item.representation) for item in normalized_files)
    )
    provenance = ArtifactProvenance(
        producer=ARTIFACT_PRODUCER,
        producer_version=(
            producer_version.strip()
            if isinstance(producer_version, str) and producer_version.strip()
            else _runtime_package_version()
        ),
        source=_required_text(source, "artifact provenance source"),
        fetched_at=(
            fetched_at.strip()
            if isinstance(fetched_at, str) and fetched_at.strip()
            else artifact_timestamp()
        ),
        source_revision=_optional_text(source_revision),
        projection_version=(
            projection_version.strip()
            if isinstance(projection_version, str) and projection_version.strip()
            else normalized_contract
        ),
    )
    return StableArtifactManifest(
        artifact_kind=artifact_kind.strip(),
        contract=normalized_contract,
        conversation_id=conversation_id.strip(),
        index=index,
        files=normalized_files,
        representations=representations,
        content_sha256=artifact_content_sha256(normalized_files),
        provenance=provenance,
        storage=ArtifactStoragePolicy(),
        format=format.strip().lower() if isinstance(format, str) and format.strip() else None,
    )


def render_artifact_manifest(manifest: StableArtifactManifest) -> str:
    return (
        json.dumps(
            manifest.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_artifact_manifest(
    path: str | Path,
    manifest: StableArtifactManifest,
) -> Path:
    return write_private_artifact_text(path, render_artifact_manifest(manifest))


def artifact_content_sha256(files: Iterable[ArtifactFileEntry]) -> str:
    files = tuple(files)
    payload = json.dumps(
        [entry.to_dict() for entry in files],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _runtime_package_version() -> str:
    try:
        return importlib.metadata.version(_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _required_representation(value: Any) -> str:
    normalized = _required_text(value, "artifact representation")
    if normalized not in ARTIFACT_REPRESENTATIONS:
        raise ValueError(f"unsupported artifact representation: {normalized!r}")
    return normalized


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
