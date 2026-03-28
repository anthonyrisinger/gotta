from __future__ import annotations

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
    ContentEvent,
    ContentSnapshot,
    Materialization,
    ResolvedDirs,
)
from gotta.content.path import is_sha256_digest, sanitize_name
from gotta.content.scope import session_identity
from gotta.content.stamp import iso_utc


def _append_manifest(dirs: ResolvedDirs, entry: dict[str, Any]) -> None:
    append_jsonl_line(dirs.content_dir / "manifest.jsonl", entry)


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
    data_path = content_dir / "data"
    meta_path = content_dir / "meta.json"
    names_dir = content_dir / "names"
    logs_dir = content_dir / "logs"
    ensure_private_dir(names_dir)
    ensure_private_dir(logs_dir)

    if not data_path.exists():
        write_bytes_atomic(data_path, data)

    name_link = ensure_name_link(names_dir, data_path, preferred_name)
    ts = timestamp or iso_utc()
    fetch_link = timestamp_log_path(logs_dir, ts)
    fetch_link.symlink_to(os.path.relpath(name_link, start=logs_dir))

    existing = _read_existing_meta(meta_path)
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
        meta_path,
        json.dumps(update, indent=2, sort_keys=True) + "\n",
    )
    _append_manifest(
        dirs,
        {
            "locator": metadata.get("locator", ""),
            "canonical_locator": metadata.get("canonical_locator", ""),
            "canonical_path": str(data_path),
            "fetched_at": ts,
            "fetcher": metadata.get("tool", "gotta"),
            "plugin": metadata.get("plugin", ""),
            "provider": metadata.get("provider", ""),
            "artifact_kind": metadata.get("artifact_kind", ""),
            "actor": metadata.get("actor") or session_identity(dirs.session_dir),
            "actor_dir": metadata.get("actor_dir", ""),
            "session_root": metadata.get("session_root", ""),
            "visibility_level": metadata.get("visibility_level", ""),
            "visibility_boundary": metadata.get("visibility_boundary", ""),
            "visibility_confidence": metadata.get("visibility_confidence", ""),
            "visibility_basis": metadata.get("visibility_basis", []),
            "checksum": digest,
            "preferred_name": sanitize_name(preferred_name),
            "fetch_link": str(fetch_link),
        },
    )
    import gotta.lead.cache as lead_cache

    lead_cache.maybe_write_lead_cache(content_dir)

    return Materialization(
        content_dir=content_dir,
        data_path=data_path,
        meta_path=meta_path,
        names_dir=names_dir,
        logs_dir=logs_dir,
        name_link=name_link,
        fetch_link=fetch_link,
        digest=digest,
        artifact_kind=str(metadata.get("artifact_kind", "") or "").strip(),
    )


def _collect_directory_snapshot(content_dir: Path) -> ContentSnapshot | None:
    digest = content_dir.name
    if not content_dir.is_dir() or not is_sha256_digest(digest):
        return None
    data_path = content_dir / "data"
    if not data_path.exists():
        return None
    meta_path = content_dir / "meta.json"
    names_dir = content_dir / "names"
    logs_dir = content_dir / "logs"
    names = (
        sorted(path.name for path in names_dir.iterdir() if path.is_symlink())
        if names_dir.exists()
        else []
    )
    events: list[ContentEvent] = []
    if logs_dir.exists():
        for path in sorted(logs_dir.iterdir(), key=lambda item: item.name):
            if not path.is_symlink():
                continue
            target = path.parent / os.readlink(path)
            events.append(
                ContentEvent(
                    timestamp=path.name.split("--", 1)[0],
                    link_name=target.name,
                    link_path=target.resolve(),
                    log_path=path,
                )
            )
    return ContentSnapshot(
        digest=digest,
        content_dir=content_dir,
        data_path=data_path,
        meta_path=meta_path if meta_path.exists() else None,
        names_dir=names_dir,
        logs_dir=logs_dir,
        names=names,
        events=events,
        metadata=read_json_object(meta_path if meta_path.exists() else None),
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
