"""Timestamp and mode helpers for session timeline surfaces."""

from __future__ import annotations

from gotta.compat import UTC, datetime
from gotta.content.model import ContentSnapshot

from ..core import TIMELINE_MODE_ALIASES


def _parse_source_timestamp(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if len(value) == 17 and value[10] == "." and value[:10].isdigit():
        seconds, micros = value.split(".", 1)
        dt = datetime.fromtimestamp(int(seconds), tz=UTC).replace(
            microsecond=int(micros)
        )
        return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (
        parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _source_timestamps(snapshot: ContentSnapshot) -> dict[str, str]:
    metadata = snapshot.metadata
    aliases = (
        ("source_published_at", "source_published_at"),
        ("source_updated_at", "source_updated_at"),
        ("source_created_at", "source_created_at"),
        ("published_at", "source_published_at"),
        ("updated_at", "source_updated_at"),
        ("updated", "source_updated_at"),
        ("modifiedTime", "source_updated_at"),
        ("createdTime", "source_created_at"),
        ("authored_at", "source_created_at"),
        ("author_date", "source_created_at"),
        ("created", "source_created_at"),
        ("timestamp", "source_created_at"),
    )
    timestamps: dict[str, str] = {}
    for key, normalized in aliases:
        parsed = _parse_source_timestamp(str(metadata.get(key) or ""))
        if parsed and normalized not in timestamps:
            timestamps[normalized] = parsed
    return timestamps


def normalize_timeline_mode(mode: str) -> str:
    normalized = TIMELINE_MODE_ALIASES.get(mode.strip().lower(), "")
    if normalized:
        return normalized
    choices = ", ".join(sorted({"acquired", "best-effort", "created", "updated"}))
    raise SystemExit(f"invalid timeline mode: {mode}. expected one of: {choices}")


def _iso_utc_from_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
