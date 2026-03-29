"""Manifest text rendering."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from ..core import (
    MANIFEST_TEXT_PREVIEW_LIMIT,
    append_count_section,
    filter_suffix,
    paging_summary_line,
    rendered_actor,
    visibility_summary,
)
from .model import ManifestPayload, ManifestPayloadEntry


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


def _count_section_lines(
    *,
    heading: str,
    key: str,
    records: Sequence[Mapping[str, object]],
) -> list[str]:
    lines: list[str] = []
    append_count_section(
        lines,
        heading=heading,
        records=records,
        key=key,
    )
    return lines


def _preview_heading(entries: list[ManifestPayloadEntry]) -> str:
    if len(entries) <= MANIFEST_TEXT_PREVIEW_LIMIT:
        return "entries preview:"
    preview_count = len(entries[:MANIFEST_TEXT_PREVIEW_LIMIT])
    return f"entries preview (showing {preview_count} of {len(entries)}):"


def _preview_entry_lines(
    entry: ManifestPayloadEntry,
    *,
    session_root: Path,
) -> list[str]:
    fetched_at = _string(entry.get("fetched_at")) or "unknown-time"
    plugin_values = entry.get("plugins", [])
    actor_values = entry.get("actors", [])
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
    payload: ManifestPayload,
    *,
    session_root: Path,
) -> str:
    lines = [f"manifest: {payload['manifestPath']}"]
    lines.append(
        "entries: "
        f"{payload['entryCount']} canonical "
        f"(from {payload['fetchRecordCount']} fetches; "
        f"showing {payload['shownCount']}; "
        f"discovery {payload['discoveryArtifactCount']}, "
        f"evidence {payload['evidenceArtifactCount']})"
        f"{filter_suffix(payload['filter'])}"
    )
    lines.append(
        paging_summary_line(
            label="page",
            total_count=payload["totalCount"],
            shown_count=payload["shownCount"],
            offset=payload["offset"],
            next_offset=payload["nextOffset"],
        )
    )
    if payload["shownCount"] == 0 and payload["totalCount"] > 0:
        lines.append("page: no results in this page window")

    lines.extend(
        _count_section_lines(
            heading="top plugins",
            key="plugin",
            records=list(payload["topPlugins"]),
        )
    )
    lines.extend(
        _count_section_lines(
            heading="top actors",
            key="actor",
            records=list(payload["topActors"]),
        )
    )

    session_ref = payload["sessionRef"]
    if session_ref:
        lines.append(
            f"follow: use emitted locators with `gotta read --session {session_ref} <locator>`"
        )

    entries = payload["entries"]
    preview_entries = entries[:MANIFEST_TEXT_PREVIEW_LIMIT]
    if entries:
        lines.append(_preview_heading(entries))
    for entry in preview_entries:
        lines.extend(_preview_entry_lines(entry, session_root=session_root))
    hidden = len(entries) - len(preview_entries)
    if hidden > 0:
        lines.append(f"  - ... {hidden} additional entries hidden in text view")
    return "\n".join(lines)
