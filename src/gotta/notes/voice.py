"""Actor note voice and readiness synthesis."""

from __future__ import annotations

import json
from pathlib import Path

from gotta.actor import actor_session_root, resolve_actor_identity
from gotta.friction import FrictionRecord, OOPS_CHANNEL, visible_channel_records

from .file import ActorNoteRecord, visible_actor_notes_records


def _has_nonempty_note_from(records: list[ActorNoteRecord], author: str) -> bool:
    return any(
        str(record.get("author") or "").strip() == author
        and str(record.get("message") or "").strip()
        for record in records
    )


def _has_nonempty_actor_record(records: list[FrictionRecord], actor: str) -> bool:
    return any(
        str(record.get("actor") or "").strip() == actor
        and str(record.get("message") or "").strip()
        for record in records
    )


def _has_actor_evidence(work_dir: Path, actor: str) -> bool:
    manifest_path = work_dir / "content" / "manifest.jsonl"
    if not manifest_path.exists():
        return False
    for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("actor") or "").strip() == actor:
            return True
    return False


def actor_voice(work_dir: Path, actor_name: str) -> str:
    normalized_actor = resolve_actor_identity(work_dir, actor_name)
    actor_root = actor_session_root(work_dir, actor_name)
    visible_notes = visible_actor_notes_records(work_dir, actor_name)
    visible_oops = visible_channel_records(actor_root, OOPS_CHANNEL)
    if _has_nonempty_note_from(visible_notes, normalized_actor):
        return "present"
    if _has_nonempty_actor_record(
        visible_oops, normalized_actor
    ) or _has_actor_evidence(work_dir, normalized_actor):
        return "pulse"
    if any(str(record.get("message") or "").strip() for record in visible_notes) or any(
        str(record.get("message") or "").strip() for record in visible_oops
    ):
        return "setup"
    return "missing"


def actor_notes_status(work_dir: Path, actor_name: str) -> str:
    voice = actor_voice(work_dir, actor_name)
    if voice == "present":
        return "present"
    if voice == "setup":
        return "setup"
    return "empty"


def actor_notes_ready(work_dir: Path, actor_name: str) -> bool:
    return actor_notes_status(work_dir, actor_name) == "present"
