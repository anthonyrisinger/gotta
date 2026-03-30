"""Canonical structured session log state and on-demand rendering."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from gotta.actor import session_actor, writer_role
from gotta.compat import UTC, datetime
from gotta.content.env import SESSION_REPO_ENV, load_state_env_at_root
from gotta.content.scope import session_identity
from gotta.projection import append_jsonl, read_jsonl_records


LOGS_LOG_NAME = "logs.jsonl"


class LogRecord(TypedDict):
    timestamp: str
    message: str
    actor: str
    channel: str


class LogsPayload(TypedDict):
    state_path: str
    locator: str
    follow_command: str
    entry_count: int
    entries: list[LogRecord]


def logs_state_path(work_dir: Path) -> Path:
    return work_dir / "state" / LOGS_LOG_NAME


def _normalize_log_record(value: object) -> LogRecord | None:
    if not isinstance(value, dict):
        return None
    return {
        "timestamp": str(value.get("timestamp") or "").strip(),
        "message": str(value.get("message") or ""),
        "actor": str(value.get("actor") or "").strip(),
        "channel": str(value.get("channel") or "logs").strip() or "logs",
    }


def log_records(work_dir: Path) -> list[LogRecord]:
    records: list[LogRecord] = []
    for record in read_jsonl_records(logs_state_path(work_dir)):
        normalized = _normalize_log_record(record)
        if normalized is not None:
            records.append(normalized)
    return records


def visible_log_records(work_dir: Path) -> list[LogRecord]:
    target_actor = (
        session_actor(work_dir) if work_dir.resolve().parent.name == "actors" else ""
    )
    records = log_records(work_dir)
    if not target_actor:
        return records
    return [
        record
        for record in records
        if writer_role(work_dir, target_actor, writer=str(record.get("actor") or ""))
        != "foreign"
    ]


def logs_payload(work_dir: Path, *, limit: int = 0) -> LogsPayload:
    records = sorted(
        visible_log_records(work_dir), key=lambda item: str(item.get("timestamp") or "")
    )
    entries = records[-limit:] if limit > 0 else records
    return {
        "state_path": str(logs_state_path(work_dir)),
        "locator": "logs:session",
        "follow_command": "gotta logs",
        "entry_count": len(records),
        "entries": entries,
    }


def _repo_display_name(work_dir: Path) -> str:
    state = load_state_env_at_root(work_dir)
    repo_path = str(state.get(SESSION_REPO_ENV) or "").strip()
    if not repo_path:
        return "Session"
    return Path(repo_path).name.capitalize() or "Session"


def render_logs_markdown(work_dir: Path, records: list[LogRecord]) -> str:
    lines = [
        "# Logs",
        "",
        f"> Generated automatically from `state/{LOGS_LOG_NAME}`.",
        "> Rendered on demand from canonical state.",
        "> The structured `logs` log is canonical.",
        "> This surface is procedural/system trace. Prefer `gotta notes ...` for actor-authored narration.",
        "> Prefer `gotta logs ...` for chronology, runtime trace, and other procedural inspection.",
        "",
        f"This is the procedural execution trace for the {_repo_display_name(work_dir)} session.",
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


def append_log_record(
    work_dir: Path,
    *,
    message: str,
    actor: str = "",
    timestamp: str | None = None,
) -> LogRecord:
    payload: LogRecord = {
        "timestamp": timestamp or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message": message,
        "actor": actor or session_identity(work_dir),
        "channel": "logs",
    }
    log_path = logs_state_path(work_dir)
    append_jsonl(log_path, dict(payload))
    return payload


def recent_log_lines(work_dir: Path, *, limit: int) -> list[str]:
    records = sorted(
        visible_log_records(work_dir), key=lambda item: str(item.get("timestamp") or "")
    )
    entries: list[str] = []
    for record in records[-limit:]:
        timestamp = str(record.get("timestamp") or "unknown-time")
        message = str(record.get("message") or "").strip() or "unspecified log entry"
        first_line = message.splitlines()[0] if message.splitlines() else message
        entries.append(f"- `{timestamp}` {first_line}")
    return entries
