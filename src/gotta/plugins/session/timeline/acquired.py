"""Acquired-order timeline event helpers."""

from __future__ import annotations

from gotta.content.path import content_locator

from ..core import (
    artifact_human_locator,
    artifact_kind,
    follow_command,
    match_any,
    rendered_actor,
    resolved_visibility_metadata,
)
from ..manifest.record import manifest_entries
from .model import TimelineEvent, apply_visibility, timeline_visibility


def acquired_timeline_events(
    dirs,
    *,
    session_ref: str,
    filter_pattern,
    local_events: list[TimelineEvent],
) -> list[TimelineEvent]:
    manifest_events: list[TimelineEvent] = []
    for entry in manifest_entries(dirs):
        locator = (
            str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip()
            or "unknown"
        )
        checksum = str(entry.get("checksum", "")).strip()
        event: TimelineEvent = {
            "mode": "acquired",
            "source_time": "",
            "source_time_field": "",
            "source_created_at": "",
            "source_updated_at": "",
            "source_published_at": "",
            "fetched_at": str(entry.get("fetched_at", "")).strip(),
            "plugin": str(entry.get("plugin", "")).strip() or "unknown-plugin",
            "actor": rendered_actor(entry.get("actor"), session_root=dirs.session_dir),
            "target_actor": str(entry.get("target_actor", "")).strip(),
            "locator": locator,
            "preferred_name": str(entry.get("preferred_name", "")).strip() or "data",
            "checksum": checksum,
            "artifactKind": artifact_kind(entry.get("artifact_kind")),
            "content_locator": content_locator(checksum) if checksum else "",
            "artifact_locator": artifact_human_locator(
                str(entry.get("preferred_name", "")).strip() or "data",
                checksum,
            ),
            "follow_command": follow_command(
                locator,
                checksum=checksum,
                session_ref=session_ref,
            ),
            "detail": "",
            "surface": "",
            "event_kind": "source",
        }
        apply_visibility(
            event,
            timeline_visibility(
                resolved_visibility_metadata(
                    entry,
                    provider=str(entry.get("plugin") or ""),
                    plugin=str(entry.get("plugin") or ""),
                    subcommand=str(entry.get("subcommand") or ""),
                    locator=locator,
                )
            ),
        )
        manifest_events.append(event)
    events = sorted(
        [*manifest_events, *local_events],
        key=lambda item: (
            str(item.get("fetched_at") or ""),
            str(item.get("locator") or ""),
            str(item.get("checksum") or ""),
        ),
    )
    if filter_pattern is not None:
        events = [
            item
            for item in events
            if match_any(
                filter_pattern,
                item.get("actor"),
                item.get("target_actor"),
                item.get("plugin"),
                item.get("locator"),
                item.get("preferred_name"),
                item.get("detail"),
                item.get("surface"),
                item.get("event_kind"),
            )
        ]
    return events
