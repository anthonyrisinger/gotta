"""Text rendering for session timeline payloads."""

from __future__ import annotations

from ..core import (
    TIMELINE_TEXT_PREVIEW_LIMIT,
    append_count_section,
    filter_suffix,
    paging_summary_line,
    visibility_summary,
)


def _int_value(value: object, *, default: int = 0) -> int:
    try:
        return int(str(value or default))
    except ValueError:
        return default


def _object_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def render_timeline_text(payload: dict[str, object]) -> str:
    total_count = _int_value(payload.get("totalCount"))
    shown_count = _int_value(payload.get("shownCount"))
    offset = _int_value(payload.get("offset"))
    next_offset = (
        _int_value(payload.get("nextOffset"))
        if payload.get("nextOffset") is not None
        else None
    )
    top_plugins = _object_records(payload.get("topPlugins"))
    top_actors = _object_records(payload.get("topActors"))
    events = _object_records(payload.get("events"))
    lines = [f"timeline: {payload['manifestPath']}"]
    activity_paths = _string_items(payload.get("activityPaths"))
    if len(activity_paths) > 1:
        lines.append(f"activity: {len(activity_paths)} actor activity logs")
    else:
        lines.append(f"activity: {payload['activityPath']}")
    lines.append(f"mode: {payload['mode']} ({payload['modeDescription']})")
    lines.append(f"coverage_gaps: {payload.get('coverageGapCount', 0)}")
    lines.append(
        "events: "
        f"{payload['eventCount']} total "
        f"(discovery {payload['discoveryArtifactCount']}, "
        f"evidence {payload['evidenceArtifactCount']})"
        f"{filter_suffix(payload.get('filter'))}"
    )
    lines.append(
        paging_summary_line(
            label="page",
            total_count=total_count,
            shown_count=shown_count,
            offset=offset,
            next_offset=next_offset,
        )
    )
    if shown_count == 0 and total_count > 0:
        lines.append("page: no results in this page window")
    top_plugins_lines: list[str] = []
    append_count_section(
        top_plugins_lines,
        heading="top plugins",
        records=top_plugins,
        key="plugin",
    )
    lines.extend(top_plugins_lines)
    top_actors_lines: list[str] = []
    append_count_section(
        top_actors_lines,
        heading="top actors",
        records=top_actors,
        key="actor",
    )
    lines.extend(top_actors_lines)

    preview_events = events[:TIMELINE_TEXT_PREVIEW_LIMIT]
    if events:
        if len(events) <= TIMELINE_TEXT_PREVIEW_LIMIT:
            lines.append("events preview:")
        else:
            lines.append(
                f"events preview (showing {len(preview_events)} of {len(events)}):"
            )
    for event in preview_events:
        actor_label = str(event.get("actor") or "")
        target_actor = str(event.get("target_actor") or "")
        if target_actor and target_actor != actor_label:
            actor_label = f"{actor_label}->{target_actor}"
        checksum = str(event.get("checksum") or "")[:12] or "unknown"
        if payload["mode"] != "acquired":
            lines.append(
                f"- {event.get('source_time') or 'unknown-time'} "
                f"[{event['plugin']}/{actor_label}] "
                f"{event['locator']} -> {event['preferred_name']} ({checksum}) "
                f"(from {event.get('source_time_field') or 'unknown-field'})"
            )
        else:
            local_suffix = " (local)" if event.get("event_kind") == "local" else ""
            lines.append(
                f"- {event['fetched_at'] or 'unknown-time'} "
                f"[{event['plugin']}/{actor_label}] "
                f"{event['locator']} -> {event['preferred_name']} ({checksum})"
                f"{local_suffix}"
            )
        if event.get("artifactKind"):
            lines.append(f"  artifact_kind: {event['artifactKind']}")
        visibility = visibility_summary(event)
        if visibility:
            lines.append(f"  visibility: {visibility}")
        stored_parts = [
            part
            for part in (
                f"`{event.get('artifact_locator')}`"
                if event.get("artifact_locator")
                else "",
                f"`{event.get('content_locator')}`"
                if event.get("content_locator")
                else "",
            )
            if part
        ]
        if stored_parts:
            lines.append("  stored: " + ", ".join(stored_parts))
    hidden = len(events) - len(preview_events)
    if hidden > 0:
        lines.append(f"  - ... {hidden} additional events hidden in text view")
    return "\n".join(lines)
