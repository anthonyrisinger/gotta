from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from gotta.content.file import (
    append_jsonl_line,
    ensure_name_link,
    ensure_private_dir,
    read_json_object,
    timestamp_log_path,
    write_bytes_atomic,
    write_text_atomic,
)
from gotta.content.model import (
    AliasRecord,
    ArtifactRecord,
    ContentSnapshot,
    ManifestEntry,
    Materialization,
    MaterializationEvent,
    ResolvedDirs,
    StoredArtifactLayout,
)
from gotta.content.path import is_sha256_digest, sanitize_name
from gotta.content.scope import session_identity
from gotta.content.stamp import iso_utc
from gotta.content.store import BlobStore, LedgerStore


@dataclass(frozen=True, slots=True)
class FileSystemBlobStore(BlobStore):
    content_dir: Path

    def layout(self, digest: str) -> StoredArtifactLayout:
        artifact_dir = self.content_dir / digest
        return StoredArtifactLayout(
            artifact_dir=artifact_dir,
            blob_path=artifact_dir / "data",
            metadata_path=artifact_dir / "meta.json",
            alias_dir=artifact_dir / "names",
            event_dir=artifact_dir / "logs",
        )

    def ensure_layout(self, digest: str) -> StoredArtifactLayout:
        layout = self.layout(digest)
        ensure_private_dir(layout.alias_dir)
        ensure_private_dir(layout.event_dir)
        return layout

    def write_blob(self, digest: str, data: bytes) -> StoredArtifactLayout:
        layout = self.ensure_layout(digest)
        if not layout.blob_path.exists():
            write_bytes_atomic(layout.blob_path, data)
        return layout

    def iter_layouts(self) -> list[StoredArtifactLayout]:
        if not self.content_dir.exists():
            return []
        return [
            self.layout(path.name)
            for path in sorted(self.content_dir.iterdir(), key=lambda item: item.name)
            if path.is_dir() and is_sha256_digest(path.name)
        ]


def _merge_meta(
    existing: dict[str, Any] | None, update: dict[str, Any]
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if existing:
        payload.update(existing)
    payload.update(update)
    if existing and "created_at" in existing:
        payload["created_at"] = existing["created_at"]
    return payload


def _read_existing_meta(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    return read_json_object(path)


def _artifact_record(
    digest: str,
    metadata: dict[str, Any],
    *,
    byte_count: int,
    preferred_name: str,
    original_name: str,
    created_at: str,
    fetched_at: str,
) -> ArtifactRecord:
    return ArtifactRecord(
        digest=digest,
        artifact_kind=str(metadata.get("artifact_kind", "") or "").strip(),
        byte_count=byte_count,
        preferred_name=preferred_name,
        original_name=original_name,
        created_at=created_at,
        fetched_at=fetched_at,
        metadata=metadata,
    )


@dataclass(frozen=True, slots=True)
class FileSystemLedgerStore(LedgerStore):
    content_dir: Path
    session_dir: Path | None = None

    @classmethod
    def for_dirs(cls, dirs: ResolvedDirs) -> FileSystemLedgerStore:
        return cls(content_dir=dirs.content_dir, session_dir=dirs.session_dir)

    @classmethod
    def for_content_dir(
        cls, content_dir: Path, *, session_dir: Path | None = None
    ) -> FileSystemLedgerStore:
        return cls(content_dir=content_dir, session_dir=session_dir)

    def append_manifest_entry(self, entry: ManifestEntry) -> None:
        append_jsonl_line(self.content_dir / "manifest.jsonl", asdict(entry))

    def materialize_bytes(
        self,
        data: bytes,
        *,
        preferred_name: str,
        metadata: dict[str, Any],
        timestamp: str | None = None,
    ) -> Materialization:
        digest = hashlib.sha256(data).hexdigest()
        layout = FileSystemBlobStore(self.content_dir).write_blob(digest, data)

        alias = ensure_name_link(layout.alias_dir, layout.blob_path, preferred_name)
        ts = timestamp or iso_utc()
        event_path = timestamp_log_path(layout.event_dir, ts)
        event_path.symlink_to(os.path.relpath(alias, start=layout.event_dir))

        existing = _read_existing_meta(layout.metadata_path)
        merged = _merge_meta(
            existing,
            {
                "hash": digest,
                "algorithm": "sha256",
                "bytes": len(data),
                "preferred_name": sanitize_name(preferred_name),
                "original_name": preferred_name,
                **metadata,
            },
        )
        if not merged.get("created_at"):
            merged["created_at"] = ts
        merged["fetched_at"] = ts
        assert layout.metadata_path is not None
        write_text_atomic(
            layout.metadata_path,
            json.dumps(merged, indent=2, sort_keys=True) + "\n",
        )

        normalized_name = sanitize_name(preferred_name)
        self.append_manifest_entry(
            ManifestEntry(
                locator=str(metadata.get("locator", "") or ""),
                canonical_locator=str(metadata.get("canonical_locator", "") or ""),
                canonical_path=str(layout.blob_path),
                fetched_at=ts,
                fetcher=str(metadata.get("tool", "gotta") or "gotta"),
                plugin=str(metadata.get("plugin", "") or ""),
                provider=str(metadata.get("provider", "") or ""),
                artifact_kind=str(metadata.get("artifact_kind", "") or ""),
                actor=str(metadata.get("actor") or self._session_identity()),
                actor_dir=str(metadata.get("actor_dir", "") or ""),
                session_root=str(metadata.get("session_root", "") or ""),
                visibility_level=str(metadata.get("visibility_level", "") or ""),
                visibility_boundary=str(metadata.get("visibility_boundary", "") or ""),
                visibility_confidence=str(
                    metadata.get("visibility_confidence", "") or ""
                ),
                visibility_basis=list(metadata.get("visibility_basis", []) or []),
                checksum=digest,
                preferred_name=normalized_name,
                fetch_link=str(event_path),
            )
        )

        import gotta.lead.cache as lead_cache

        lead_cache.maybe_write_lead_cache(layout.artifact_dir)

        artifact = _artifact_record(
            digest,
            merged,
            byte_count=len(data),
            preferred_name=normalized_name,
            original_name=preferred_name,
            created_at=str(merged.get("created_at", "") or ts),
            fetched_at=ts,
        )
        return Materialization(
            artifact=artifact,
            layout=layout,
            alias=AliasRecord(name=alias.name, path=alias),
            event=MaterializationEvent(
                timestamp=ts,
                alias_name=alias.name,
                alias_path=alias,
                blob_path=layout.blob_path,
                event_path=event_path,
            ),
        )

    def scan_artifacts(self) -> list[ContentSnapshot]:
        snapshots: dict[str, ContentSnapshot] = {}
        for layout in FileSystemBlobStore(self.content_dir).iter_layouts():
            snapshot = self._snapshot_for_layout(layout)
            if snapshot is not None:
                snapshots[snapshot.digest] = snapshot
        return [snapshots[digest] for digest in sorted(snapshots)]

    def _session_identity(self) -> str:
        if self.session_dir is None:
            return ""
        return session_identity(self.session_dir)

    def _snapshot_for_layout(
        self, layout: StoredArtifactLayout
    ) -> ContentSnapshot | None:
        digest = layout.artifact_dir.name
        if not layout.blob_path.exists():
            return None
        metadata = read_json_object(
            layout.metadata_path
            if layout.metadata_path and layout.metadata_path.exists()
            else None
        )
        aliases = (
            [
                AliasRecord(name=path.name, path=path)
                for path in sorted(
                    layout.alias_dir.iterdir(), key=lambda item: item.name
                )
                if path.is_symlink()
            ]
            if layout.alias_dir.exists()
            else []
        )
        events: list[MaterializationEvent] = []
        if layout.event_dir.exists():
            alias_by_name = {alias.name: alias for alias in aliases}
            for path in sorted(layout.event_dir.iterdir(), key=lambda item: item.name):
                if not path.is_symlink():
                    continue
                target = path.parent / os.readlink(path)
                alias_name = target.name
                alias = alias_by_name.get(alias_name, AliasRecord(alias_name, target))
                events.append(
                    MaterializationEvent(
                        timestamp=path.name.split("--", 1)[0],
                        alias_name=alias.name,
                        alias_path=alias.path,
                        blob_path=target.resolve(),
                        event_path=path,
                    )
                )
        preferred = str(metadata.get("preferred_name", "") or "").strip()
        original = str(metadata.get("original_name", "") or "").strip()
        artifact = _artifact_record(
            digest,
            metadata,
            byte_count=int(metadata.get("bytes", layout.blob_path.stat().st_size) or 0),
            preferred_name=preferred or (aliases[0].name if aliases else digest),
            original_name=original
            or preferred
            or (aliases[0].name if aliases else digest),
            created_at=str(metadata.get("created_at", "") or ""),
            fetched_at=str(metadata.get("fetched_at", "") or ""),
        )
        return ContentSnapshot(
            artifact=artifact,
            layout=StoredArtifactLayout(
                artifact_dir=layout.artifact_dir,
                blob_path=layout.blob_path,
                metadata_path=layout.metadata_path
                if layout.metadata_path and layout.metadata_path.exists()
                else None,
                alias_dir=layout.alias_dir,
                event_dir=layout.event_dir,
            ),
            aliases=aliases,
            events=events,
        )


__all__ = [
    "FileSystemBlobStore",
    "FileSystemLedgerStore",
]
