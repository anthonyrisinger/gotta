"""Actor note payload and Markdown rendering helpers."""

from __future__ import annotations

from pathlib import Path

from gotta.actor import (
    requested_disposition_label,
    supervisor_stop_message,
    supervisor_stop_pending,
)

from .file import (
    ACTOR_NOTES_LOG_NAME,
    actor_notes_log_path,
    visible_actor_notes_records,
)


def _artifact_count(status_payload: dict[str, object]) -> int:
    try:
        return int(str(status_payload.get("artifact_count") or 0))
    except ValueError:
        return 0


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
        "state_path": str(actor_notes_log_path(work_dir, actor_name)),
        "locator": f"notes:actor:{actor_name}",
        "follow_command": f"gotta notes --actor {actor_name}",
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
        "> Rendered on demand from canonical state.",
        "> The structured actor notes log is canonical.",
        "> Notes are the canonical actor-authored narration surface; short one-line notes are valid.",
        "> Prefer `gotta notes append ...` on the active actor root; add `--actor <actor>` only when targeting another bound actor intentionally.",
        "> Use notes for alive, first-anchor, evidence-wave, and signoff narration. `gotta logs` remains procedural/system trace.",
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
            f"- artifact_count: {_artifact_count(status_payload)}",
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
