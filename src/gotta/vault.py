"""Shared helpers for private on-disk auth and state blobs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable


def write_secret_text_atomic(
    path: Path,
    value: str,
    *,
    ensure_dir: Callable[[], None] | None = None,
) -> Path:
    if ensure_dir is not None:
        ensure_dir()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        os.chmod(temp_path, 0o600)
        handle.write(value.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(path)
    os.chmod(path, 0o600)
    return path


def write_secret_json_atomic(
    path: Path,
    payload: dict[str, Any],
    *,
    ensure_dir: Callable[[], None] | None = None,
    indent: int | None = None,
    sort_keys: bool = False,
    trailing_newline: bool = False,
) -> Path:
    text = json.dumps(payload, indent=indent, sort_keys=sort_keys)
    if trailing_newline:
        text += "\n"
    return write_secret_text_atomic(path, text, ensure_dir=ensure_dir)


def load_secret_json_object(
    path: Path,
    *,
    allow_trailing_unmatched_closing_braces: bool = False,
) -> tuple[dict[str, Any], bool]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(str(exc)) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        if allow_trailing_unmatched_closing_braces:
            recovered = _recover_trailing_closing_braces_object(text)
            if recovered is not None:
                return recovered, True
        raise ValueError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise ValueError("expected JSON object")
    return payload, False


def _recover_trailing_closing_braces_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    decoder = json.JSONDecoder()
    try:
        payload, end = decoder.raw_decode(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    remainder = stripped[end:]
    if not remainder:
        return payload
    if remainder.replace("}", "").strip():
        return None
    return payload
