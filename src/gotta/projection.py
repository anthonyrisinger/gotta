"""Shared helpers for canonical JSONL state and readable projections."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path

from gotta.capture import Capture
from gotta.content.file import PRIVATE_FILE_MODE, ensure_private_dir, write_text_atomic


@dataclass(frozen=True, slots=True)
class Projection:
    data: bytes
    content_type: str = ""
    degradations: tuple[str, ...] = ()


def projection_bytes(
    data: bytes,
    *,
    content_type: str = "",
    degradations: tuple[str, ...] = (),
) -> Projection:
    return Projection(
        data=data,
        content_type=content_type,
        degradations=degradations,
    )


def projection_for_capture(
    capture: Capture,
    data: bytes,
    *,
    content_type: str = "",
    degradations: tuple[str, ...] = (),
) -> Projection:
    return Projection(
        data=data,
        content_type=content_type or capture.content_type,
        degradations=degradations,
    )


def append_chunk(path: Path, chunk: str) -> None:
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, PRIVATE_FILE_MODE)
    try:
        os.write(fd, chunk.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        path.chmod(PRIVATE_FILE_MODE)
    except OSError:
        pass


def append_jsonl(path: Path, payload: dict[str, object]) -> None:
    append_chunk(path, json.dumps(payload, sort_keys=True) + "\n")


def read_jsonl_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def write_projection_if_changed(path: Path, text: str) -> None:
    ensure_private_dir(path.parent)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return
        except OSError:
            pass
    write_text_atomic(path, text)


__all__ = [
    "Projection",
    "append_chunk",
    "append_jsonl",
    "projection_bytes",
    "projection_for_capture",
    "read_jsonl_records",
    "write_projection_if_changed",
]
