"""Canonical structured session log state and Markdown projection."""

from __future__ import annotations

from pathlib import Path

from gotta.compat import UTC, datetime
from gotta.content import load_state_env_at_root, write_text_atomic
from gotta.projection import append_chunk, append_jsonl, read_jsonl_records


LOGS_LOG_NAME = "logs.jsonl"


def logs_state_path(work_dir: Path) -> Path:
    return work_dir / "state" / LOGS_LOG_NAME


def logs_surface_path(work_dir: Path) -> Path:
    return work_dir / "LOGS.md"

def log_records(work_dir: Path) -> list[dict[str, object]]:
    return read_jsonl_records(logs_state_path(work_dir))


def logs_payload(work_dir: Path, *, limit: int = 0) -> dict[str, object]:
    records = sorted(log_records(work_dir), key=lambda item: str(item.get("timestamp") or ""))
    entries = records[-limit:] if limit > 0 else records
    return {
        "logs": str(logs_surface_path(work_dir)),
        "logs_log": str(logs_state_path(work_dir)),
        "entry_count": len(records),
        "entries": entries,
    }


def _repo_display_name(work_dir: Path) -> str:
    state = load_state_env_at_root(work_dir)
    repo_path = str(state.get("GOTTA_WORK_REPO") or "").strip()
    if not repo_path:
        return "Session"
    return Path(repo_path).name.capitalize() or "Session"


def render_logs_markdown(work_dir: Path, records: list[dict[str, object]]) -> str:
    lines = [
        "# Logs",
        "",
        f"> Generated automatically from `state/{LOGS_LOG_NAME}`.",
        "> This is a human-readable projection; the structured `logs` log is canonical.",
        "> Prefer `gotta logs ...` for routine mutation and inspection.",
        "",
        f"This is the durable execution log for the {_repo_display_name(work_dir)} session.",
        "",
        "## Entries",
        "",
    ]
    ordered = sorted(records, key=lambda item: str(item.get("timestamp") or ""))
    for record in ordered:
        timestamp = str(record.get("timestamp") or "unknown-time")
        message = str(record.get("message") or "").strip() or "unspecified log entry"
        message_lines = message.splitlines() or ["unspecified log entry"]
        lines.append(f"- `{timestamp}` {message_lines[0]}")
        for continuation in message_lines[1:]:
            lines.append(f"  {continuation}")
    return "\n".join(lines) + "\n"


def render_log_record_markdown(record: dict[str, object]) -> str:
    timestamp = str(record.get("timestamp") or "unknown-time")
    message = str(record.get("message") or "").strip() or "unspecified log entry"
    message_lines = message.splitlines() or ["unspecified log entry"]
    lines = [f"- `{timestamp}` {message_lines[0]}"]
    for continuation in message_lines[1:]:
        lines.append(f"  {continuation}")
    return "\n".join(lines) + "\n"


def sync_logs_projection(work_dir: Path) -> None:
    write_text_atomic(
        logs_surface_path(work_dir),
        render_logs_markdown(work_dir, log_records(work_dir)),
    )


def append_log_record(
    work_dir: Path,
    *,
    message: str,
    actor: str = "primary",
    timestamp: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "timestamp": timestamp or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message": message,
        "actor": actor,
        "channel": "logs",
    }
    log_path = logs_state_path(work_dir)
    surface_path = logs_surface_path(work_dir)
    append_jsonl(log_path, payload)
    if surface_path.exists():
        append_chunk(surface_path, render_log_record_markdown(payload))
    else:
        sync_logs_projection(work_dir)
    return payload


def recent_log_lines(work_dir: Path, *, limit: int) -> list[str]:
    records = sorted(log_records(work_dir), key=lambda item: str(item.get("timestamp") or ""))
    entries: list[str] = []
    for record in records[-limit:]:
        timestamp = str(record.get("timestamp") or "unknown-time")
        message = str(record.get("message") or "").strip() or "unspecified log entry"
        first_line = message.splitlines()[0] if message.splitlines() else message
        entries.append(f"- `{timestamp}` {first_line}")
    return entries
