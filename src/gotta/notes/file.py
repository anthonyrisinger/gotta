"""Canonical actor note storage helpers."""

from __future__ import annotations

import os
from pathlib import Path

from gotta.actor import actor_session_root, writer_role
from gotta.compat import UTC, datetime
from gotta.content.context import current_actor
from gotta.content.env import SESSION_ACTOR_ENV
from gotta.projection import append_jsonl, read_jsonl_records


ACTOR_NOTES_LOG_NAME = "notes.jsonl"


def actor_notes_log_path(work_dir: Path, actor_name: str) -> Path:
    return actor_session_root(work_dir, actor_name) / "state" / ACTOR_NOTES_LOG_NAME


def actor_notes_records(work_dir: Path, actor_name: str) -> list[dict[str, object]]:
    return read_jsonl_records(actor_notes_log_path(work_dir, actor_name))


def visible_actor_notes_records(
    work_dir: Path, actor_name: str
) -> list[dict[str, object]]:
    visible: list[dict[str, object]] = []
    for record in actor_notes_records(work_dir, actor_name):
        author = str(record.get("author") or "").strip()
        if writer_role(work_dir, actor_name, writer=author or actor_name) == "foreign":
            continue
        visible.append(record)
    return visible


def _author_name() -> str:
    default_speaker = os.environ.get(SESSION_ACTOR_ENV, "").strip()
    return current_actor(default_actor=default_speaker)


def append_actor_note(
    work_dir: Path,
    actor_name: str,
    *,
    message: str,
    author: str = "",
    timestamp: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "timestamp": timestamp or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "author": author.strip() or _author_name(),
        "actor": actor_name,
        "message": message,
    }
    append_jsonl(actor_notes_log_path(work_dir, actor_name), payload)
    return payload
