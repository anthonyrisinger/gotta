"""Read-target request and resolution models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ReadRequest:
    target: str | None
    recursive: bool
    max_depth: int
    head: int
    tail: int
    section: str
    session: str = ""
    actor: str = ""
    routed_plugin: str | None = None
    routed_argv: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadTarget:
    request: ReadRequest
    kind: str
    path: Path | None
    routed_plugin: str | None
    routed_argv: list[str]
    canonical_locator: str
    preferred_name: str
    should_materialize: bool
