from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ContentError(RuntimeError):
    """Raised when the shared content contract cannot be satisfied."""


@dataclass(frozen=True)
class CommonOptions:
    session_dir: str | None = None
    content_dir: str | None = None
    session_id: str | None = None
    actor: str | None = None
    save_as: str | None = None


@dataclass(frozen=True)
class ResolvedDirs:
    session_dir: Path
    content_dir: Path


@dataclass(frozen=True)
class ContextBinding:
    context_id: str
    context_source: str
    binding_id: str


@dataclass(frozen=True)
class Materialization:
    content_dir: Path
    data_path: Path
    meta_path: Path
    names_dir: Path
    logs_dir: Path
    name_link: Path
    fetch_link: Path
    digest: str
    artifact_kind: str


@dataclass(frozen=True)
class ContentEvent:
    timestamp: str
    link_name: str
    link_path: Path
    log_path: Path


@dataclass(frozen=True)
class ContentSnapshot:
    digest: str
    content_dir: Path
    data_path: Path
    meta_path: Path | None
    names_dir: Path
    logs_dir: Path
    names: list[str]
    events: list[ContentEvent]
    metadata: dict[str, Any]
