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
from ..manifest import manifest_entries


def acquired_timeline_events(
    dirs,
    *,
    session_ref: str,
    filter_pattern,
    local_events: list[dict[str, object]],
) -> list[dict[str, object]]:
    manifest_events = [
        {
            "mode": "acquired",
            "fetched_at": str(entry.get("fetched_at", "")).strip(),
            "plugin": str(entry.get("plugin", "")).strip() or "unknown-plugin",
            "actor": rendered_actor(entry.get("actor"), session_root=dirs.session_dir),
            "target_actor": str(entry.get("target_actor", "")).strip(),
            "locator": str(
                entry.get("canonical_locator", "") or entry.get("locator", "")
            ).strip()
            or "unknown",
            "preferred_name": str(entry.get("preferred_name", "")).strip() or "data",
            "checksum": str(entry.get("checksum", "")).strip(),
            "artifactKind": artifact_kind(entry.get("artifact_kind")),
            "content_locator": content_locator(str(entry.get("checksum", "")).strip())
            if str(entry.get("checksum", "")).strip()
            else "",
            "artifact_locator": artifact_human_locator(
                str(entry.get("preferred_name", "")).strip() or "data",
                str(entry.get("checksum", "")).strip(),
            ),
            "fetch_link": str(entry.get("fetch_link", "")).strip(),
            "follow_command": follow_command(
                str(
                    entry.get("canonical_locator", "") or entry.get("locator", "")
                ).strip(),
                checksum=str(entry.get("checksum", "")).strip(),
                session_ref=session_ref,
            ),
            "event_kind": "source",
            **resolved_visibility_metadata(
                entry,
                provider=str(entry.get("plugin") or ""),
                plugin=str(entry.get("plugin") or ""),
                subcommand=str(entry.get("subcommand") or ""),
                locator=str(
                    entry.get("canonical_locator", "") or entry.get("locator", "")
                ).strip(),
            ),
        }
        for entry in manifest_entries(dirs)
    ]
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
