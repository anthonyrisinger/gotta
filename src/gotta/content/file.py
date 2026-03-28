from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from gotta.content.model import ContentError
from gotta.content.path import sanitize_name

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _chmod_best_effort(path: Path, mode: int) -> None:
    try:
        if path.exists() and not path.is_symlink():
            path.chmod(mode)
    except OSError:
        pass


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(path, PRIVATE_DIR_MODE)
    return path


def write_bytes_atomic(path: Path, data: bytes) -> Path:
    ensure_private_dir(path.parent)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    _chmod_best_effort(temp_path, PRIVATE_FILE_MODE)
    temp_path.replace(path)
    _chmod_best_effort(path, PRIVATE_FILE_MODE)
    return path


def write_text_atomic(path: Path, text: str) -> Path:
    return write_bytes_atomic(path, text.encode("utf-8"))


def write_text_if_changed(path: Path, text: str) -> Path:
    ensure_private_dir(path.parent)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return path
        except OSError:
            pass
    write_text_atomic(path, text)
    return path


def append_jsonl_line(path: Path, payload: dict[str, Any]) -> None:
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, PRIVATE_FILE_MODE)
    try:
        os.write(fd, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    _chmod_best_effort(path, PRIVATE_FILE_MODE)


def timestamp_log_path(logs_dir: Path, timestamp: str) -> Path:
    candidate = logs_dir / timestamp
    if not candidate.exists() and not candidate.is_symlink():
        return candidate
    index = 1
    while True:
        candidate = logs_dir / f"{timestamp}--{index:02d}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        index += 1


def ensure_name_link(names_dir: Path, data_path: Path, preferred_name: str) -> Path:
    ensure_private_dir(names_dir)
    name_link = names_dir / sanitize_name(preferred_name)
    if name_link.exists() or name_link.is_symlink():
        if not name_link.is_symlink():
            raise ContentError(
                f"integrity error: expected symlink at {name_link}, found regular file"
            )
        target = os.readlink(name_link)
        expected = os.path.relpath(data_path, start=names_dir)
        if target != expected:
            raise ContentError(
                f"integrity error: {name_link} points to {target!r} instead of {expected!r}"
            )
        return name_link
    name_link.symlink_to(os.path.relpath(data_path, start=names_dir))
    return name_link


def read_json_object(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContentError(f"invalid metadata file at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContentError(f"invalid metadata file at {path}: expected object")
    return payload
