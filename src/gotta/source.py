"""Shared source metadata normalization and aggregation helpers."""

from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Any


_SLACK_WHOLE_TS_RE = re.compile(r"^\d{16}$")
_SLACK_FRACTIONAL_TS_RE = re.compile(r"^\d{10}\.\d{6}$")
_ISOISH_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T\s]\S+)?$")

_CREATED_KEYS = {
    "authored",
    "created",
    "createdat",
    "created_at",
    "createdtime",
    "firstts",
    "first_ts",
    "threadts",
    "thread_ts",
    "ts",
}
_UPDATED_KEYS = {
    "lastmodified",
    "lastmodifiedat",
    "last_modified",
    "last_modified_at",
    "latestts",
    "latest_ts",
    "modified",
    "modifiedat",
    "modified_at",
    "modifiedtime",
    "pushedat",
    "pushed_at",
    "updated",
    "updatedat",
    "updated_at",
}
_PUBLISHED_KEYS = {
    "published",
    "publishedat",
    "published_at",
}


def slack_timestamp_to_iso(raw: object) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if _SLACK_WHOLE_TS_RE.fullmatch(value):
        seconds = int(value[:10])
        micros = int(value[10:])
        return (
            datetime.fromtimestamp(seconds, tz=UTC)
            .replace(microsecond=micros)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    if _SLACK_FRACTIONAL_TS_RE.fullmatch(value):
        seconds, micros = value.split(".", 1)
        return (
            datetime.fromtimestamp(int(seconds), tz=UTC)
            .replace(microsecond=int(micros))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    return ""


def normalize_source_timestamp(value: object) -> str:
    slack_value = slack_timestamp_to_iso(value)
    if slack_value:
        return slack_value
    text = str(value or "").strip()
    if not text:
        return ""
    if _ISOISH_RE.fullmatch(text):
        return text
    return ""


def _classify_timestamp_path(path: tuple[str, ...]) -> str:
    if not path:
        return ""
    key = path[-1].casefold()
    if key in _PUBLISHED_KEYS:
        return "published"
    if key in _UPDATED_KEYS:
        return "updated"
    if key in _CREATED_KEYS:
        return "created"
    if key == "date" and len(path) >= 2 and path[-2].casefold() == "author":
        return "created"
    return ""


def _collect_candidates(
    payload: Any,
    *,
    path: tuple[str, ...] = (),
    created: list[str],
    updated: list[str],
    published: list[str],
) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            _collect_candidates(
                value,
                path=(*path, str(key)),
                created=created,
                updated=updated,
                published=published,
            )
        return
    if isinstance(payload, list):
        for item in payload:
            _collect_candidates(
                item,
                path=path,
                created=created,
                updated=updated,
                published=published,
            )
        return
    kind = _classify_timestamp_path(path)
    if not kind:
        return
    normalized = normalize_source_timestamp(payload)
    if not normalized:
        return
    if kind == "created":
        created.append(normalized)
        return
    if kind == "updated":
        updated.append(normalized)
        return
    published.append(normalized)


def derive_source_metadata_from_payload(payload: Any) -> dict[str, str]:
    created: list[str] = []
    updated: list[str] = []
    published: list[str] = []
    _collect_candidates(payload, created=created, updated=updated, published=published)
    metadata: dict[str, str] = {}
    if published:
        metadata["source_published_at"] = min(published)
    if created:
        metadata["source_created_at"] = min(created)
    if updated:
        metadata["source_updated_at"] = max(updated)
    return metadata


def render_source_metadata_lines(metadata: dict[str, str]) -> list[str]:
    lines: list[str] = []
    created = metadata.get("source_created_at", "").strip()
    updated = metadata.get("source_updated_at", "").strip()
    published = metadata.get("source_published_at", "").strip()
    if created:
        lines.append(f"- Created: {created}")
    if updated:
        lines.append(f"- Updated: {updated}")
    if published:
        lines.append(f"- Published: {published}")
    return lines
