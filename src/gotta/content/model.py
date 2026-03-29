from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ContentError(RuntimeError):
    """Raised when the shared content contract cannot be satisfied."""


ArtifactMetadata = dict[str, Any]


@dataclass(frozen=True, slots=True)
class CommonOptions:
    session_dir: str | None = None
    content_dir: str | None = None
    session_id: str | None = None
    actor: str | None = None
    save_as: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedDirs:
    session_dir: Path
    content_dir: Path


@dataclass(frozen=True, slots=True)
class ContextBinding:
    context_id: str
    context_source: str
    binding_id: str


@dataclass(frozen=True, slots=True)
class StoredArtifactLayout:
    artifact_dir: Path
    blob_path: Path
    metadata_path: Path | None
    alias_dir: Path
    event_dir: Path


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    digest: str
    artifact_kind: str
    byte_count: int
    preferred_name: str
    original_name: str
    created_at: str
    fetched_at: str
    metadata: ArtifactMetadata


@dataclass(frozen=True, slots=True)
class AliasRecord:
    name: str
    path: Path


@dataclass(frozen=True, slots=True)
class MaterializationEvent:
    timestamp: str
    alias_name: str
    alias_path: Path
    blob_path: Path
    event_path: Path

    @property
    def link_name(self) -> str:
        return self.alias_name

    @property
    def link_path(self) -> Path:
        return self.blob_path

    @property
    def log_path(self) -> Path:
        return self.event_path


ContentEvent = MaterializationEvent


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    locator: str
    canonical_locator: str
    canonical_path: str
    fetched_at: str
    fetcher: str
    plugin: str
    provider: str
    artifact_kind: str
    actor: str
    actor_dir: str
    session_root: str
    visibility_level: str
    visibility_boundary: str
    visibility_confidence: str
    visibility_basis: list[str]
    checksum: str
    preferred_name: str
    fetch_link: str


@dataclass(frozen=True, slots=True)
class Materialization:
    artifact: ArtifactRecord
    layout: StoredArtifactLayout
    alias: AliasRecord
    event: MaterializationEvent

    @property
    def content_dir(self) -> Path:
        return self.layout.artifact_dir

    @property
    def data_path(self) -> Path:
        return self.layout.blob_path

    @property
    def meta_path(self) -> Path:
        assert self.layout.metadata_path is not None
        return self.layout.metadata_path

    @property
    def names_dir(self) -> Path:
        return self.layout.alias_dir

    @property
    def logs_dir(self) -> Path:
        return self.layout.event_dir

    @property
    def name_link(self) -> Path:
        return self.alias.path

    @property
    def fetch_link(self) -> Path:
        return self.event.event_path

    @property
    def digest(self) -> str:
        return self.artifact.digest

    @property
    def artifact_kind(self) -> str:
        return self.artifact.artifact_kind


@dataclass(frozen=True, slots=True)
class ContentSnapshot:
    artifact: ArtifactRecord
    layout: StoredArtifactLayout
    aliases: list[AliasRecord]
    events: list[MaterializationEvent]

    @property
    def digest(self) -> str:
        return self.artifact.digest

    @property
    def content_dir(self) -> Path:
        return self.layout.artifact_dir

    @property
    def data_path(self) -> Path:
        return self.layout.blob_path

    @property
    def meta_path(self) -> Path | None:
        return self.layout.metadata_path

    @property
    def names_dir(self) -> Path:
        return self.layout.alias_dir

    @property
    def logs_dir(self) -> Path:
        return self.layout.event_dir

    @property
    def names(self) -> list[str]:
        return [alias.name for alias in self.aliases]

    @property
    def metadata(self) -> ArtifactMetadata:
        return self.artifact.metadata
