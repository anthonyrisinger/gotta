"""Canonical actor note storage helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TypedDict

from gotta.actor import actor_session_root, writer_role
from gotta.compat import UTC, datetime
from gotta.content.context import current_actor
from gotta.content.env import SESSION_ACTOR_ENV
from gotta.projection import append_jsonl, read_jsonl_records


ACTOR_NOTES_LOG_NAME = "notes.jsonl"


class ActorNoteRecord(TypedDict):
    timestamp: str
    author: str
    actor: str
    message: str


def actor_notes_log_path(work_dir: Path, actor_name: str) -> Path:
    return actor_session_root(work_dir, actor_name) / "state" / ACTOR_NOTES_LOG_NAME


def _normalize_actor_note_record(value: object) -> ActorNoteRecord | None:
    if not isinstance(value, dict):
        return None
    return {
        "timestamp": str(value.get("timestamp") or "").strip(),
        "author": str(value.get("author") or "").strip(),
        "actor": str(value.get("actor") or "").strip(),
        "message": str(value.get("message") or ""),
    }


def actor_notes_records(work_dir: Path, actor_name: str) -> list[ActorNoteRecord]:
    records: list[ActorNoteRecord] = []
    for record in read_jsonl_records(actor_notes_log_path(work_dir, actor_name)):
        normalized = _normalize_actor_note_record(record)
        if normalized is not None:
            records.append(normalized)
    return records


def visible_actor_notes_records(
    work_dir: Path, actor_name: str
) -> list[ActorNoteRecord]:
    visible: list[ActorNoteRecord] = []
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
) -> ActorNoteRecord:
    payload: ActorNoteRecord = {
        "timestamp": timestamp or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "author": author.strip() or _author_name(),
        "actor": actor_name,
        "message": message,
    }
    append_jsonl(actor_notes_log_path(work_dir, actor_name), dict(payload))
    return payload
