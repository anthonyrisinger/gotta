"""Manifest text rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from ..core import (
    MANIFEST_TEXT_PREVIEW_LIMIT,
    append_count_section,
    filter_suffix,
    paging_summary_line,
    rendered_actor,
    visibility_summary,
)


def _string(value: object) -> str:
    return str(value or "").strip()


def _int(value: object) -> int:
    try:
        return int(str(value or 0))
    except ValueError:
        return 0


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        text = _string(item)
        if text:
            values.append(text)
    return values


def _records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    records: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            records.append({str(key): item[key] for key in item})
    return records


def _count_section_lines(
    payload: Mapping[str, object],
    *,
    heading: str,
    key: str,
    field: str,
) -> list[str]:
    lines: list[str] = []
    append_count_section(
        lines,
        heading=heading,
        records=_records(payload.get(field)),
        key=key,
    )
    return lines


def _preview_heading(entries: list[dict[str, object]]) -> str:
    if len(entries) <= MANIFEST_TEXT_PREVIEW_LIMIT:
        return "entries preview:"
    preview_count = len(entries[:MANIFEST_TEXT_PREVIEW_LIMIT])
    return f"entries preview (showing {preview_count} of {len(entries)}):"


def _preview_entry_lines(entry: dict[str, object], *, session_root: Path) -> list[str]:
    fetched_at = _string(entry.get("fetched_at")) or "unknown-time"
    plugin_values = _string_list(entry.get("plugins"))
    actor_values = _string_list(entry.get("actors"))
    plugin = (
        ", ".join(plugin_values) or _string(entry.get("plugin")) or "unknown-plugin"
    )
    actor = ", ".join(actor_values) or rendered_actor(
        entry.get("actor"),
        session_root=session_root,
    )
    locator = (
        _string(entry.get("canonical_locator"))
        or _string(entry.get("locator"))
        or "unknown"
    )
    preferred_name = _string(entry.get("preferred_name")) or "data"
    checksum = _string(entry.get("checksum"))
    short = checksum[:12] if checksum else "unknown"
    lines = [
        f"- {fetched_at} [{plugin}/{actor}] {locator} -> {preferred_name} ({short})"
    ]
    if _int(entry.get("fetchCount")) > 1:
        lines.append(
            "  fetches: "
            f"{_int(entry.get('fetchCount'))} "
            f"(first {_string(entry.get('firstFetchedAt')) or 'unknown-time'}; "
            f"last {_string(entry.get('lastFetchedAt')) or 'unknown-time'})"
        )
    kind = _string(entry.get("artifactKind"))
    if kind:
        lines.append(f"  artifact_kind: {kind}")
    visibility = visibility_summary(dict(entry))
    if visibility:
        lines.append(f"  visibility: {visibility}")
    stored_parts: list[str] = []
    if entry.get("artifact_locator"):
        stored_parts.append(f"`{entry.get('artifact_locator')}`")
    if entry.get("content_locator"):
        stored_parts.append(f"`{entry.get('content_locator')}`")
    if stored_parts:
        lines.append("  stored: " + ", ".join(stored_parts))
    return lines


def render_manifest_text(
    payload: Mapping[str, object],
    *,
    session_root: Path,
) -> str:
    lines = [f"manifest: {_string(payload.get('manifestPath'))}"]
    lines.append(
        "entries: "
        f"{_int(payload.get('entryCount'))} canonical "
        f"(from {_int(payload.get('fetchRecordCount'))} fetches; "
        f"showing {_int(payload.get('shownCount'))}; "
        f"discovery {_int(payload.get('discoveryArtifactCount'))}, "
        f"evidence {_int(payload.get('evidenceArtifactCount'))})"
        f"{filter_suffix(payload.get('filter'))}"
    )
    lines.append(
        paging_summary_line(
            label="page",
            total_count=_int(payload.get("totalCount")),
            shown_count=_int(payload.get("shownCount")),
            offset=_int(payload.get("offset")),
            next_offset=(
                _int(payload["nextOffset"])
                if payload.get("nextOffset") is not None
                else None
            ),
        )
    )
    if _int(payload.get("shownCount")) == 0 and _int(payload.get("totalCount")) > 0:
        lines.append("page: no results in this page window")

    lines.extend(
        _count_section_lines(
            payload,
            heading="top plugins",
            key="plugin",
            field="topPlugins",
        )
    )
    lines.extend(
        _count_section_lines(
            payload,
            heading="top actors",
            key="actor",
            field="topActors",
        )
    )

    session_ref = _string(payload.get("sessionRef"))
    if session_ref:
        lines.append(
            f"follow: use emitted locators with `gotta read --session {session_ref} <locator>`"
        )

    entries = _records(payload.get("entries"))
    preview_entries = entries[:MANIFEST_TEXT_PREVIEW_LIMIT]
    if entries:
        lines.append(_preview_heading(entries))
    for entry in preview_entries:
        lines.extend(_preview_entry_lines(entry, session_root=session_root))
    hidden = len(entries) - len(preview_entries)
    if hidden > 0:
        lines.append(f"  - ... {hidden} additional entries hidden in text view")
    return "\n".join(lines)
