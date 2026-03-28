"""Source metadata rendering helpers."""

from __future__ import annotations

from typing import Any, Mapping

from .visibility import normalize_visibility_metadata


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


def render_visibility_metadata_lines(metadata: Mapping[str, Any]) -> list[str]:
    visibility = normalize_visibility_metadata(metadata)
    if not visibility:
        return []
    return [
        "- Visibility: "
        f"{visibility['visibility_level']} "
        f"({visibility['visibility_boundary']}, {visibility['visibility_confidence']})"
    ]
