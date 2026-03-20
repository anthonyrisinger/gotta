"""Canonical session-rooted speed-bump logging for gotta."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from gotta.compat import UTC, datetime
from gotta.content import append_activity_event, current_actor, session_identity, write_text_atomic
from gotta.projection import append_chunk, append_jsonl, read_jsonl_records


@dataclass(frozen=True, slots=True)
class FrictionChannel:
    noun: str
    title: str
    stem: str
    description: str
    canonical_line: str
    use_cases: tuple[str, ...]
    default_message: str


OOPS_CHANNEL = FrictionChannel(
    noun="oops",
    title="Oops",
    stem="oops",
    description="session speed bumps, friction, and suspected gotta bugs",
    canonical_line=(
        "Canonical truth lives in `state/oops.jsonl`; this file is the human-readable"
    ),
    use_cases=(
        "literally any operator-visible speed bump",
        "command boundary awkwardness",
        "weak defaults",
        "missing traversal",
        "surprising output shape",
        "auth or resolution weirdness",
        "non-obvious next steps",
        "reproducible wrong behavior",
        "state drift after acknowledged success",
        "emitted command or locator does not work as emitted",
        "native surface contradicts its own help or contract",
        "retrieval or rendering bug with a known source object",
    ),
    default_message="unspecified speed bump",
)

def channel_log_path(session_dir: Path, channel: FrictionChannel) -> Path:
    return session_dir / "state" / f"{channel.stem}.jsonl"


def channel_surface_path(session_dir: Path, channel: FrictionChannel) -> Path:
    return session_dir / f"{channel.title.upper()}.md"


def channel_records(session_dir: Path, channel: FrictionChannel) -> list[dict[str, object]]:
    return read_jsonl_records(channel_log_path(session_dir, channel))


def filtered_records(
    records: list[dict[str, object]],
    *,
    surface: str = "",
    command: str = "",
    kind: str = "",
    severity: str = "",
) -> list[dict[str, object]]:
    return [
        record
        for record in records
        if (not surface or str(record.get("surface") or "") == surface)
        and (not command or str(record.get("command") or "") == command)
        and (not kind or str(record.get("kind") or "") == kind)
        and (not severity or str(record.get("severity") or "") == severity)
    ]


def render_channel_markdown(
    records: list[dict[str, object]],
    channel: FrictionChannel,
) -> str:
    lines = [
        f"# {channel.title}",
        "",
        f"> Generated automatically from `state/{channel.stem}.jsonl`.",
        f"> This is a human-readable projection; the structured `{channel.stem}` log is canonical.",
        "",
        f"Capture {channel.description} as it happens.",
        "",
        channel.canonical_line,
        f"projection of that structured `{channel.stem}` log.",
        "",
        "Use this file for:",
        "",
    ]
    lines.extend(f"- {use_case}" for use_case in channel.use_cases)
    lines.extend(["", "## Entries", ""])
    ordered = sorted(records, key=lambda item: str(item.get("timestamp") or ""))
    for record in ordered:
        timestamp = str(record.get("timestamp") or "unknown-time")
        message = str(record.get("message") or "").strip() or channel.default_message
        qualifiers = [
            str(record.get("severity") or "").strip(),
            str(record.get("kind") or "").strip(),
            str(record.get("surface") or "").strip(),
        ]
        tag = " ".join(part for part in qualifiers if part)
        suffix = f" [{tag}]" if tag else ""
        message_lines = message.splitlines() or [channel.default_message]
        lines.append(f"- `{timestamp}` {message_lines[0]}{suffix}")
        for continuation in message_lines[1:]:
            lines.append(f"  {continuation}")
    return "\n".join(lines) + "\n"


def render_channel_record_markdown(
    record: dict[str, object],
    channel: FrictionChannel,
) -> str:
    timestamp = str(record.get("timestamp") or "unknown-time")
    message = str(record.get("message") or "").strip() or channel.default_message
    qualifiers = [
        str(record.get("severity") or "").strip(),
        str(record.get("kind") or "").strip(),
        str(record.get("surface") or "").strip(),
    ]
    tag = " ".join(part for part in qualifiers if part)
    suffix = f" [{tag}]" if tag else ""
    message_lines = message.splitlines() or [channel.default_message]
    lines = [f"- `{timestamp}` {message_lines[0]}{suffix}"]
    for continuation in message_lines[1:]:
        lines.append(f"  {continuation}")
    return "\n".join(lines) + "\n"


def sync_channel_projection(session_dir: Path, channel: FrictionChannel) -> None:
    write_text_atomic(
        channel_surface_path(session_dir, channel),
        render_channel_markdown(channel_records(session_dir, channel), channel),
    )


def append_channel_record(
    session_dir: Path,
    channel: FrictionChannel,
    *,
    message: str,
    surface: str = "",
    command: str = "",
    kind: str = "",
    affordance: str = "",
    workaround: str = "",
    severity: str = "medium",
    reproducibility: str = "unknown",
    resolution_state: str = "open",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "timestamp": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actor": current_actor(default_actor=session_identity(session_dir)),
        "message": message,
        "surface": surface,
        "command": command,
        "kind": kind,
        "affordance": affordance,
        "workaround": workaround,
        "severity": severity,
        "reproducibility": reproducibility,
        "resolution_state": resolution_state,
        "sessionDir": str(session_dir),
        "channel": channel.stem,
    }
    log_path = channel_log_path(session_dir, channel)
    surface_path = channel_surface_path(session_dir, channel)
    append_jsonl(log_path, payload)
    append_activity_event(
        session_dir,
        {
            "plugin": channel.stem,
            "surface": channel.stem,
            "action": "append",
            "locator": str(log_path.relative_to(session_dir)),
            "preferred_name": surface_path.name,
            "follow_command": f"gotta read {surface_path.name!r}",
            "detail": message,
            "time_field": "session_recorded_at",
        },
    )
    if surface_path.exists():
        append_chunk(surface_path, render_channel_record_markdown(payload, channel))
    else:
        sync_channel_projection(session_dir, channel)
    return payload


def recent_channel_lines(
    session_dir: Path,
    channel: FrictionChannel,
    *,
    limit: int,
) -> list[str]:
    records = sorted(
        channel_records(session_dir, channel), key=lambda item: str(item.get("timestamp") or "")
    )
    entries = []
    for record in records[-limit:]:
        timestamp = str(record.get("timestamp") or "unknown-time")
        message = str(record.get("message") or "").strip() or channel.default_message
        first_line = message.splitlines()[0] if message.splitlines() else message
        entries.append(f"- `{timestamp}` {first_line}")
    return entries


def channel_summary(records: list[dict[str, object]]) -> dict[str, object]:
    severity_counts = Counter(str(record.get("severity") or "unknown") for record in records)
    kind_counts = Counter(str(record.get("kind") or "general") for record in records)
    surface_counts = Counter(str(record.get("surface") or "unspecified") for record in records)
    resolution_counts = Counter(
        str(record.get("resolution_state") or "open") for record in records
    )
    reproducibility_counts = Counter(
        str(record.get("reproducibility") or "unknown") for record in records
    )
    affordance_counts = Counter(
        str(record.get("affordance") or "unspecified") for record in records
    )
    return {
        "entry_count": len(records),
        "severity_counts": dict(sorted(severity_counts.items())),
        "kind_counts": dict(sorted(kind_counts.items())),
        "surface_counts": dict(sorted(surface_counts.items())),
        "resolution_counts": dict(sorted(resolution_counts.items())),
        "reproducibility_counts": dict(sorted(reproducibility_counts.items())),
        "affordance_counts": dict(sorted(affordance_counts.items())),
    }


def oops_log_path(session_dir: Path) -> Path:
    return channel_log_path(session_dir, OOPS_CHANNEL)


def oops_surface_path(session_dir: Path) -> Path:
    return channel_surface_path(session_dir, OOPS_CHANNEL)


def oops_records(session_dir: Path) -> list[dict[str, object]]:
    return channel_records(session_dir, OOPS_CHANNEL)


def filtered_oops_records(
    records: list[dict[str, object]],
    *,
    surface: str = "",
    command: str = "",
    kind: str = "",
    severity: str = "",
) -> list[dict[str, object]]:
    return filtered_records(
        records,
        surface=surface,
        command=command,
        kind=kind,
        severity=severity,
    )


def render_oops_markdown(records: list[dict[str, object]]) -> str:
    return render_channel_markdown(records, OOPS_CHANNEL)


def sync_oops_projection(session_dir: Path) -> None:
    sync_channel_projection(session_dir, OOPS_CHANNEL)


def append_oops_record(
    session_dir: Path,
    *,
    message: str,
    surface: str = "",
    command: str = "",
    kind: str = "",
    affordance: str = "",
    workaround: str = "",
    severity: str = "medium",
    reproducibility: str = "unknown",
    resolution_state: str = "open",
) -> dict[str, object]:
    return append_channel_record(
        session_dir,
        OOPS_CHANNEL,
        message=message,
        surface=surface,
        command=command,
        kind=kind,
        affordance=affordance,
        workaround=workaround,
        severity=severity,
        reproducibility=reproducibility,
        resolution_state=resolution_state,
    )


def recent_oops_lines(session_dir: Path, *, limit: int) -> list[str]:
    return recent_channel_lines(session_dir, OOPS_CHANNEL, limit=limit)


def oops_summary(records: list[dict[str, object]]) -> dict[str, object]:
    return channel_summary(records)
