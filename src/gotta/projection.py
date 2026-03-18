"""Shared helpers for canonical JSONL state and readable projections."""

from __future__ import annotations

import json
import os
from pathlib import Path

from gotta.content import write_text_atomic


def append_chunk(path: Path, chunk: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, chunk.encode("utf-8"))
    finally:
        os.close(fd)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return
        except OSError:
            pass
    write_text_atomic(path, text)
