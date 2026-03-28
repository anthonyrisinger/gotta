from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from gotta.content.context import current_actor
from gotta.content.env import state_dir_path
from gotta.content.file import append_jsonl_line
from gotta.content.scope import session_identity
from gotta.content.stamp import iso_utc

ACTIVITY_LOG_NAME = "activity.jsonl"


def activity_log_path(root: Path) -> Path:
    return state_dir_path(root) / ACTIVITY_LOG_NAME


def append_activity_event(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    event = dict(payload)
    event.setdefault("timestamp", iso_utc())
    event.setdefault("actor", current_actor(default_actor=session_identity(root)))
    append_jsonl_line(activity_log_path(root), event)
    return event


def activity_events(root: Path) -> list[dict[str, Any]]:
    path = activity_log_path(root)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
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
