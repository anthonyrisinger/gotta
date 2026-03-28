"""Text rendering for `gotta logs`."""

from __future__ import annotations


def _record_lines(record: dict[str, object], *, default_actor: str) -> list[str]:
    timestamp = str(record.get("timestamp") or "unknown-time")
    actor = str(record.get("actor") or default_actor)
    message = str(record.get("message") or "").strip() or "unspecified log entry"
    message_lines = message.splitlines() or ["unspecified log entry"]
    lines = [f"- `{timestamp}` [{actor}] {message_lines[0]}"]
    lines.extend(f"  {continuation}" for continuation in message_lines[1:])
    return lines


def render_follow_text(
    *,
    follow_command: str,
    entry_count: int,
    entries: list[dict[str, object]],
    default_actor: str,
) -> str:
    lines = [f"logs: {follow_command}", f"entries: {entry_count}"]
    for record in entries:
        lines.extend(_record_lines(record, default_actor=default_actor))
    return "\n".join(lines)


def render_session_text(
    *,
    actor_count: int,
    entry_count: int,
    entries: list[dict[str, object]],
) -> str:
    lines = [
        f"logs: session-wide across {actor_count} actor(s)",
        f"entries: {entry_count}",
    ]
    for record in entries:
        lines.extend(_record_lines(record, default_actor="unknown-actor"))
    return "\n".join(lines)
