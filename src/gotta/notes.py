"""Canonical actor notes state and readable projections."""

from __future__ import annotations

import json
import os
from pathlib import Path

from gotta.compat import UTC, datetime
from gotta.actor import (
    actor_session_root,
    resolve_actor_identity,
    requested_disposition_label,
    supervisor_stop_message,
    supervisor_stop_pending,
    writer_role,
)
from gotta.content import SESSION_ACTOR_ENV, current_actor
from gotta.friction import OOPS_CHANNEL, visible_channel_records
from gotta.logs import visible_log_records
from gotta.projection import append_jsonl, read_jsonl_records, write_projection_if_changed


ACTOR_NOTES_LOG_NAME = "notes.jsonl"


def actor_notes_log_path(work_dir: Path, actor_name: str) -> Path:
    return actor_session_root(work_dir, actor_name) / "state" / ACTOR_NOTES_LOG_NAME


def actor_notes_surface_path(work_dir: Path, actor_name: str) -> Path:
    return actor_session_root(work_dir, actor_name) / "NOTES.md"


def actor_notes_records(work_dir: Path, actor_name: str) -> list[dict[str, object]]:
    return read_jsonl_records(actor_notes_log_path(work_dir, actor_name))


def visible_actor_notes_records(work_dir: Path, actor_name: str) -> list[dict[str, object]]:
    visible: list[dict[str, object]] = []
    for record in actor_notes_records(work_dir, actor_name):
        author = str(record.get("author") or "").strip()
        if writer_role(work_dir, actor_name, writer=author or actor_name) == "foreign":
            continue
        visible.append(record)
    return visible


def _has_nonempty_note_from(records: list[dict[str, object]], author: str) -> bool:
    return any(
        str(record.get("author") or "").strip() == author
        and str(record.get("message") or "").strip()
        for record in records
    )


def _has_nonempty_actor_record(records: list[dict[str, object]], actor: str) -> bool:
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
    visible_logs = visible_log_records(actor_root)
    visible_oops = visible_channel_records(actor_root, OOPS_CHANNEL)
    if _has_nonempty_note_from(visible_notes, normalized_actor):
        return "present"
    if (
        _has_nonempty_actor_record(visible_logs, normalized_actor)
        or _has_nonempty_actor_record(visible_oops, normalized_actor)
        or _has_actor_evidence(work_dir, normalized_actor)
    ):
        return "pulse"
    if any(str(record.get("message") or "").strip() for record in visible_notes) or any(
        str(record.get("message") or "").strip() for record in visible_logs
    ) or any(str(record.get("message") or "").strip() for record in visible_oops):
        return "setup"
    return "missing"


def actor_notes_ready(work_dir: Path, actor_name: str) -> bool:
    return any(
        str(record.get("message") or "").strip()
        for record in visible_actor_notes_records(work_dir, actor_name)
    )


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
    payload = {
        "timestamp": timestamp or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "author": author.strip() or _author_name(),
        "actor": actor_name,
        "message": message,
    }
    append_jsonl(actor_notes_log_path(work_dir, actor_name), payload)
    return payload


def actor_notes_payload(
    work_dir: Path,
    actor_name: str,
    *,
    label: str,
    status_payload: dict[str, object],
) -> dict[str, object]:
    records = sorted(
        visible_actor_notes_records(work_dir, actor_name),
        key=lambda item: str(item.get("timestamp") or ""),
    )
    return {
        "actor": actor_name,
        "label": label,
        "notes": str(actor_notes_surface_path(work_dir, actor_name)),
        "notes_log": str(actor_notes_log_path(work_dir, actor_name)),
        "status": status_payload,
        "entry_count": len(records),
        "entries": records,
    }


def render_actor_notes_markdown(
    work_dir: Path,
    actor_name: str,
    *,
    label: str,
    status_payload: dict[str, object],
) -> str:
    lines = [
        f"# {label} Notes",
        "",
        f"> Generated automatically from `state/{ACTOR_NOTES_LOG_NAME}`.",
        "> This is a human-readable projection; the structured actor notes log is canonical.",
        "> Prefer `gotta notes append ...` on the active actor root; add `--actor <actor>` only when targeting another bound actor intentionally.",
        "> Shared live pulse still lands in `LOGS.md` and the session evidence web.",
        "",
    ]
    if supervisor_stop_pending(status_payload):
        lines.extend(
            [
                "> [!WARNING]",
                f"> {supervisor_stop_message(actor_name, status_payload=status_payload)}",
                "",
            ]
        )
    lines.extend(
        [
            "## Live Actor State",
            "",
            f"- status: `{status_payload.get('status', 'pending')}`",
            f"- voice: `{status_payload.get('voice', 'missing')}`",
            f"- notes_status: `{status_payload.get('notes_status', 'empty')}`",
            f"- artifact_count: {int(status_payload.get('artifact_count') or 0)}",
        ]
    )
    if status_payload.get("requested_pending"):
        requested_status = str(
            status_payload.get("requested_label")
            or requested_disposition_label(status_payload)
        ).strip()
        requested_summary = str(status_payload.get("requested_summary") or "").strip()
        detail = (
            f"{requested_status}: {requested_summary}"
            if requested_summary
            else requested_status
        )
        lines.append(f"- pending_disposition: {detail}")
    if status_payload.get("evidence_note"):
        lines.append(f"- evidence: {status_payload['evidence_note']}")
    if status_payload.get("signoff_summary"):
        lines.append(f"- signoff: {status_payload['signoff_summary']}")
    elif status_payload.get("summary"):
        lines.append(f"- summary: {status_payload['summary']}")
    if status_payload.get("next_step"):
        lines.append(f"- next_step: {status_payload['next_step']}")
    lines.extend(["", "## Entries", ""])
    records = sorted(
        visible_actor_notes_records(work_dir, actor_name),
        key=lambda item: str(item.get("timestamp") or ""),
    )
    if not records:
        lines.extend(
            [
                "- none yet",
                "",
                "Live actor evidence can still appear in session manifest, timeline, leads, and graph before notes are written.",
            ]
        )
        return "\n".join(lines) + "\n"
    for record in records:
        timestamp = str(record.get("timestamp") or "unknown-time")
        author = str(record.get("author") or actor_name)
        message = str(record.get("message") or "").strip() or "empty note"
        message_lines = message.splitlines() or ["empty note"]
        lines.append(f"- `{timestamp}` [{author}] {message_lines[0]}")
        for continuation in message_lines[1:]:
            lines.append(f"  {continuation}")
    return "\n".join(lines) + "\n"


def sync_actor_notes_projection(
    work_dir: Path,
    actor_name: str,
    *,
    label: str,
    status_payload: dict[str, object],
) -> None:
    write_projection_if_changed(
        actor_notes_surface_path(work_dir, actor_name),
        render_actor_notes_markdown(
            work_dir,
            actor_name,
            label=label,
            status_payload=status_payload,
        ),
    )
