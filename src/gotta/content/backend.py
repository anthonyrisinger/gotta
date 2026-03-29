from __future__ import annotations

from pathlib import Path
from typing import Any

from gotta.content.filesystem import FileSystemBlobStore, FileSystemLedgerStore
from gotta.content.model import ContentSnapshot, Materialization, ResolvedDirs
from gotta.content.store import BlobStore, LedgerStore


def default_blob_store(content_dir: Path) -> BlobStore:
    return FileSystemBlobStore(content_dir)


def default_ledger_store(
    *, content_dir: Path, session_dir: Path | None = None
) -> LedgerStore:
    return FileSystemLedgerStore.for_content_dir(content_dir, session_dir=session_dir)


def default_ledger_store_for_dirs(dirs: ResolvedDirs) -> LedgerStore:
    return FileSystemLedgerStore.for_dirs(dirs)


def scan_content_snapshots(
    content_dir: Path, *, session_dir: Path | None = None
) -> list[ContentSnapshot]:
    return default_ledger_store(
        content_dir=content_dir,
        session_dir=session_dir,
    ).scan_artifacts()


def materialize_artifact_bytes(
    data: bytes,
    *,
    dirs: ResolvedDirs,
    preferred_name: str,
    metadata: dict[str, Any],
    timestamp: str | None = None,
) -> Materialization:
    return default_ledger_store_for_dirs(dirs).materialize_bytes(
        data,
        preferred_name=preferred_name,
        metadata=dict(metadata),
        timestamp=timestamp,
    )


__all__ = [
    "default_blob_store",
    "default_ledger_store",
    "default_ledger_store_for_dirs",
    "scan_content_snapshots",
    "materialize_artifact_bytes",
]
