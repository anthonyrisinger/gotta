from __future__ import annotations

from dataclasses import asdict
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


def _append_manifest(dirs: ResolvedDirs, entry: ManifestEntry) -> None:
    append_jsonl_line(dirs.content_dir / "manifest.jsonl", asdict(entry))


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


def _read_existing_meta(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return read_json_object(path)


def _artifact_layout(content_dir: Path) -> StoredArtifactLayout:
    return StoredArtifactLayout(
        artifact_dir=content_dir,
        blob_path=content_dir / "data",
        metadata_path=content_dir / "meta.json",
        alias_dir=content_dir / "names",
        event_dir=content_dir / "logs",
    )


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


def materialize_bytes(
    data: bytes,
    *,
    dirs: ResolvedDirs,
    preferred_name: str,
    metadata: dict[str, Any],
    timestamp: str | None = None,
) -> Materialization:
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    content_dir = dirs.content_dir / digest
    layout = _artifact_layout(content_dir)
    ensure_private_dir(layout.alias_dir)
    ensure_private_dir(layout.event_dir)

    if not layout.blob_path.exists():
        write_bytes_atomic(layout.blob_path, data)

    name_link = ensure_name_link(layout.alias_dir, layout.blob_path, preferred_name)
    ts = timestamp or iso_utc()
    fetch_link = timestamp_log_path(layout.event_dir, ts)
    fetch_link.symlink_to(os.path.relpath(name_link, start=layout.event_dir))

    existing = _read_existing_meta(layout.metadata_path)
    update = _merge_meta(
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
    if not update.get("created_at"):
        update["created_at"] = ts
    update["fetched_at"] = ts
    write_text_atomic(
        layout.metadata_path,
        json.dumps(update, indent=2, sort_keys=True) + "\n",
    )
    preferred = sanitize_name(preferred_name)
    _append_manifest(
        dirs,
        ManifestEntry(
            locator=str(metadata.get("locator", "") or ""),
            canonical_locator=str(metadata.get("canonical_locator", "") or ""),
            canonical_path=str(layout.blob_path),
            fetched_at=ts,
            fetcher=str(metadata.get("tool", "gotta") or "gotta"),
            plugin=str(metadata.get("plugin", "") or ""),
            provider=str(metadata.get("provider", "") or ""),
            artifact_kind=str(metadata.get("artifact_kind", "") or ""),
            actor=str(metadata.get("actor") or session_identity(dirs.session_dir)),
            actor_dir=str(metadata.get("actor_dir", "") or ""),
            session_root=str(metadata.get("session_root", "") or ""),
            visibility_level=str(metadata.get("visibility_level", "") or ""),
            visibility_boundary=str(metadata.get("visibility_boundary", "") or ""),
            visibility_confidence=str(metadata.get("visibility_confidence", "") or ""),
            visibility_basis=list(metadata.get("visibility_basis", []) or []),
            checksum=digest,
            preferred_name=preferred,
            fetch_link=str(fetch_link),
        ),
    )
    import gotta.lead.cache as lead_cache

    lead_cache.maybe_write_lead_cache(layout.artifact_dir)

    artifact = _artifact_record(
        digest,
        update,
        byte_count=len(data),
        preferred_name=preferred,
        original_name=preferred_name,
        created_at=str(update.get("created_at", "") or ts),
        fetched_at=ts,
    )
    alias = AliasRecord(name=name_link.name, path=name_link)
    event = MaterializationEvent(
        timestamp=ts,
        alias_name=alias.name,
        alias_path=alias.path,
        blob_path=layout.blob_path,
        event_path=fetch_link,
    )

    return Materialization(
        artifact=artifact,
        layout=layout,
        alias=alias,
        event=event,
    )


def _collect_directory_snapshot(content_dir: Path) -> ContentSnapshot | None:
    digest = content_dir.name
    if not content_dir.is_dir() or not is_sha256_digest(digest):
        return None
    layout = _artifact_layout(content_dir)
    if not layout.blob_path.exists():
        return None
    metadata = read_json_object(
        layout.metadata_path if layout.metadata_path.exists() else None
    )
    aliases = (
        [
            AliasRecord(name=path.name, path=path)
            for path in sorted(layout.alias_dir.iterdir(), key=lambda item: item.name)
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
        original_name=original or preferred or (aliases[0].name if aliases else digest),
        created_at=str(metadata.get("created_at", "") or ""),
        fetched_at=str(metadata.get("fetched_at", "") or ""),
    )
    return ContentSnapshot(
        artifact=artifact,
        layout=StoredArtifactLayout(
            artifact_dir=layout.artifact_dir,
            blob_path=layout.blob_path,
            metadata_path=layout.metadata_path
            if layout.metadata_path.exists()
            else None,
            alias_dir=layout.alias_dir,
            event_dir=layout.event_dir,
        ),
        aliases=aliases,
        events=events,
    )


def scan_content_store(content_dir: Path) -> list[ContentSnapshot]:
    snapshots: dict[str, ContentSnapshot] = {}
    if not content_dir.exists():
        return []
    for path in sorted(content_dir.iterdir(), key=lambda item: item.name):
        snapshot = _collect_directory_snapshot(path)
        if snapshot is not None:
            snapshots[snapshot.digest] = snapshot
    return [snapshots[digest] for digest in sorted(snapshots)]
