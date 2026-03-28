"""Low-level append helpers for session activity state."""

from __future__ import annotations

import json
import os
from pathlib import Path

from gotta.content.file import PRIVATE_FILE_MODE, ensure_private_dir


def _append_chunk(path: Path, chunk: str) -> None:
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


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    _append_chunk(path, json.dumps(payload, sort_keys=True) + "\n")
