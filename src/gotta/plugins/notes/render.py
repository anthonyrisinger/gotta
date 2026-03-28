"""Text rendering for session-wide `gotta notes` views."""

from __future__ import annotations


def render_session_text(
    *,
    actor_count: int,
    entry_count: int,
    entries: list[dict[str, object]],
) -> str:
    lines = [
        f"notes: session-wide across {actor_count} actor(s)",
        f"entries: {entry_count}",
    ]
    if not entries:
        lines.append("- none yet")
        return "\n".join(lines)
    for record in entries:
        actor_name = str(record.get("actor") or "unknown-actor")
        author = str(record.get("author") or actor_name)
        timestamp = str(record.get("timestamp") or "unknown-time")
        message = str(record.get("message") or "").strip() or "empty note"
        message_lines = message.splitlines() or ["empty note"]
        lines.append(f"- `{timestamp}` [{actor_name}/{author}] {message_lines[0]}")
        lines.extend(f"  {continuation}" for continuation in message_lines[1:])
    return "\n".join(lines)
