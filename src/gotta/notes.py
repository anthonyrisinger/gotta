"""Canonical peer notes state and readable projections."""

from __future__ import annotations

from pathlib import Path

from gotta.compat import UTC, datetime
from gotta.actors import PRIMARY_ACTOR, resolve_actor_context
from gotta.peer import (
    peer_session_root,
    requested_disposition_label,
    supervisor_stop_message,
    supervisor_stop_pending,
)
from gotta.projection import append_jsonl, read_jsonl_records, write_projection_if_changed


PEER_NOTES_LOG_NAME = "notes.jsonl"


def peer_notes_log_path(work_dir: Path, peer_name: str) -> Path:
    return peer_session_root(work_dir, peer_name) / "state" / PEER_NOTES_LOG_NAME


def peer_notes_surface_path(work_dir: Path, peer_name: str) -> Path:
    return peer_session_root(work_dir, peer_name) / "NOTES.md"


def peer_notes_records(work_dir: Path, peer_name: str) -> list[dict[str, object]]:
    return read_jsonl_records(peer_notes_log_path(work_dir, peer_name))


def peer_notes_ready(work_dir: Path, peer_name: str) -> bool:
    return any(str(record.get("message") or "").strip() for record in peer_notes_records(work_dir, peer_name))


def _author_name() -> str:
    speaker = resolve_actor_context().speaker
    return str(speaker or PRIMARY_ACTOR).strip() or PRIMARY_ACTOR


def append_peer_note(
    work_dir: Path,
    peer_name: str,
    *,
    message: str,
    author: str = "",
    timestamp: str | None = None,
) -> dict[str, object]:
    payload = {
        "timestamp": timestamp or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "author": author.strip() or _author_name(),
        "peer": peer_name,
        "message": message,
    }
    append_jsonl(peer_notes_log_path(work_dir, peer_name), payload)
    return payload


def peer_notes_payload(
    work_dir: Path,
    peer_name: str,
    *,
    label: str,
    status_payload: dict[str, object],
) -> dict[str, object]:
    records = sorted(
        peer_notes_records(work_dir, peer_name),
        key=lambda item: str(item.get("timestamp") or ""),
    )
    return {
        "peer": peer_name,
        "label": label,
        "notes": str(peer_notes_surface_path(work_dir, peer_name)),
        "notes_log": str(peer_notes_log_path(work_dir, peer_name)),
        "status": status_payload,
        "entry_count": len(records),
        "entries": records,
    }


def render_peer_notes_markdown(
    work_dir: Path,
    peer_name: str,
    *,
    label: str,
    status_payload: dict[str, object],
) -> str:
    lines = [
        f"# {label} Notes",
        "",
        f"> Generated automatically from `state/{PEER_NOTES_LOG_NAME}`.",
        "> This is a human-readable projection; the structured peer notes log is canonical.",
        "> Prefer `gotta notes append <peer> ...` for mutation.",
        "> Shared live pulse still lands in `LOGS.md` and the session evidence web.",
        "",
    ]
    if supervisor_stop_pending(status_payload):
        lines.extend(
            [
                "> [!WARNING]",
                f"> {supervisor_stop_message(peer_name, status_payload=status_payload)}",
                "",
            ]
        )
    lines.extend(
        [
            "## Live Peer State",
            "",
            f"- status: `{status_payload.get('status', 'pending')}`",
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
        peer_notes_records(work_dir, peer_name),
        key=lambda item: str(item.get("timestamp") or ""),
    )
    if not records:
        lines.extend(
            [
                "- none yet",
                "",
                "Live peer evidence can still appear in session manifest, timeline, leads, and graph before notes are written.",
            ]
        )
        return "\n".join(lines) + "\n"
    for record in records:
        timestamp = str(record.get("timestamp") or "unknown-time")
        author = str(record.get("author") or peer_name)
        message = str(record.get("message") or "").strip() or "empty note"
        message_lines = message.splitlines() or ["empty note"]
        lines.append(f"- `{timestamp}` [{author}] {message_lines[0]}")
        for continuation in message_lines[1:]:
            lines.append(f"  {continuation}")
    return "\n".join(lines) + "\n"


def sync_peer_notes_projection(
    work_dir: Path,
    peer_name: str,
    *,
    label: str,
    status_payload: dict[str, object],
) -> None:
    write_projection_if_changed(
        peer_notes_surface_path(work_dir, peer_name),
        render_peer_notes_markdown(
            work_dir,
            peer_name,
            label=label,
            status_payload=status_payload,
        ),
    )
