from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol, runtime_checkable

from gotta.content.model import (
    ArtifactRecord,
    ContentSnapshot,
    ManifestEntry,
    Materialization,
    StoredArtifactLayout,
)


@runtime_checkable
class BlobStore(Protocol):
    def layout(self, digest: str) -> StoredArtifactLayout: ...

    def ensure_layout(self, digest: str) -> StoredArtifactLayout: ...

    def write_blob(self, digest: str, data: bytes) -> StoredArtifactLayout: ...

    def iter_layouts(self) -> Iterable[StoredArtifactLayout]: ...


@runtime_checkable
class LedgerStore(Protocol):
    def append_manifest_entry(self, entry: ManifestEntry) -> None: ...

    def materialize_bytes(
        self,
        data: bytes,
        *,
        preferred_name: str,
        metadata: dict[str, Any],
        timestamp: str | None = None,
    ) -> Materialization: ...

    def scan_artifacts(self) -> list[ContentSnapshot]: ...


@runtime_checkable
class StateStore(Protocol):
    def append_channel_record(
        self, channel: str, payload: Mapping[str, Any]
    ) -> None: ...

    def channel_records(self, channel: str) -> list[dict[str, Any]]: ...

    def write_actor_state(self, actor: str, payload: Mapping[str, Any]) -> None: ...

    def read_actor_state(self, actor: str) -> dict[str, Any] | None: ...


@runtime_checkable
class IndexStore(Protocol):
    def ingest_artifact(self, artifact: ArtifactRecord) -> None: ...

    def ingest_state_record(self, channel: str, payload: Mapping[str, Any]) -> None: ...

    def rebuild(self) -> None: ...

    def health(self) -> Mapping[str, Any]: ...


__all__ = [
    "BlobStore",
    "LedgerStore",
    "StateStore",
    "IndexStore",
]
