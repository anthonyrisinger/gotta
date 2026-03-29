"""Source-authored timeline event helpers."""

from __future__ import annotations

import shlex

from gotta.content.backend import scan_content_snapshots
from gotta.content.model import ContentSnapshot
from gotta.content.path import content_locator

from ..core import (
    AGGREGATE_SOURCE_SUBCOMMANDS,
    artifact_human_locator,
    artifact_kind,
    follow_command,
    match_any,
    rendered_actor,
    resolved_visibility_metadata,
)
from .stamp import _source_timestamps


def _strip_read_view_flags(locator: str) -> str:
    try:
        parts = shlex.split(locator)
    except ValueError:
        parts = locator.split()
    cleaned: list[str] = []
    index = 0
    while index < len(parts):
        token = parts[index]
        if token in {"--head", "--tail", "--section"}:
            index += 2
            continue
        if any(
            token.startswith(f"{flag}=") for flag in ("--head", "--tail", "--section")
        ):
            index += 1
            continue
        cleaned.append(token)
        index += 1
    return " ".join(cleaned).strip()


def aggregate_source_snapshot(snapshot: ContentSnapshot) -> bool:
    metadata = snapshot.artifact.metadata
    subcommand = str(metadata.get("subcommand") or "").strip().lower()
    if subcommand in AGGREGATE_SOURCE_SUBCOMMANDS:
        return True
    locator = str(
        metadata.get("canonical_locator", "") or metadata.get("locator", "")
    ).strip()
    if any(
        token in locator
        for token in (
            ":search ",
            ":status",
            ":workspaces",
            ":sql ",
            ":schema",
            ":list-channels",
            ":list-users",
        )
    ):
        return True
    if locator.startswith(
        (
            "search ",
            "status",
            "workspaces",
            "sql ",
            "schema",
            "list-channels",
            "list-users",
        )
    ):
        return True
    return False


def _source_timestamp_for_mode(
    snapshot: ContentSnapshot, mode: str
) -> tuple[str | None, str]:
    if aggregate_source_snapshot(snapshot):
        return None, ""
    timestamps = _source_timestamps(snapshot)
    if mode == "created":
        value = timestamps.get("source_created_at", "")
        return (value or None, "source_created_at" if value else "")
    if mode == "updated":
        value = timestamps.get("source_updated_at", "")
        return (value or None, "source_updated_at" if value else "")
    if mode == "best-effort":
        for key in ("source_created_at", "source_published_at", "source_updated_at"):
            value = timestamps.get(key, "")
            if value:
                return value, key
        return None, ""
    return None, ""


def counts_as_source_coverage_gap(snapshot: ContentSnapshot) -> bool:
    if aggregate_source_snapshot(snapshot):
        return False
    metadata = snapshot.artifact.metadata
    plugin = str(metadata.get("plugin", "")).strip() or "unknown-plugin"
    locator = str(
        metadata.get("canonical_locator", "") or metadata.get("locator", "")
    ).strip()
    if plugin == "read":
        base = _strip_read_view_flags(locator)
        if base.startswith(("artifact:", "content:")):
            return False
        if ":" not in base and not base.startswith(("http://", "https://")):
            return False
    if (
        plugin == "slack"
        and locator.startswith("slack:sql ")
        and ("PRAGMA table_info" in locator or "sqlite_master" in locator)
    ):
        return False
    return True


def source_timeline_events(
    dirs,
    *,
    mode: str,
    session_ref: str,
    filter_pattern,
    local_events: list[dict[str, object]],
) -> tuple[list[dict[str, object]], int]:
    snapshots = scan_content_snapshots(
        dirs.content_dir,
        session_dir=dirs.session_dir,
    )
    events: list[dict[str, object]] = []
    coverage_gap_count = 0
    for snapshot in snapshots:
        metadata = snapshot.artifact.metadata
        locator = str(
            metadata.get("canonical_locator", "") or metadata.get("locator", "")
        ).strip()
        source_payload = {
            "plugin": str(metadata.get("plugin", "")).strip() or "unknown-plugin",
            "actor": rendered_actor(
                metadata.get("actor"),
                session_root=dirs.session_dir,
            ),
            "target_actor": str(metadata.get("target_actor") or "").strip(),
            "locator": locator,
            "preferred_name": (
                snapshot.artifact.preferred_name.strip()
                or (snapshot.aliases[0].name if snapshot.aliases else "data")
            ),
            "event_kind": "source",
        }
        source_time, source_field = _source_timestamp_for_mode(snapshot, mode)
        if not source_time:
            if counts_as_source_coverage_gap(snapshot) and (
                filter_pattern is None
                or match_any(
                    filter_pattern,
                    source_payload.get("actor"),
                    source_payload.get("target_actor"),
                    source_payload.get("plugin"),
                    source_payload.get("locator"),
                    source_payload.get("preferred_name"),
                    source_payload.get("event_kind"),
                )
            ):
                coverage_gap_count += 1
            continue
        fetched_at = snapshot.events[-1].timestamp if snapshot.events else ""
        source_timestamps = _source_timestamps(snapshot)
        events.append(
            {
                "mode": "source",
                "source_time": source_time,
                "source_time_field": source_field,
                "source_created_at": source_timestamps.get("source_created_at", ""),
                "source_updated_at": source_timestamps.get("source_updated_at", ""),
                "source_published_at": source_timestamps.get("source_published_at", ""),
                "checksum": snapshot.digest,
                "artifactKind": artifact_kind(metadata.get("artifact_kind")),
                "content_locator": content_locator(snapshot.digest),
                "artifact_locator": artifact_human_locator(
                    snapshot.artifact.preferred_name.strip() or "data",
                    snapshot.digest,
                ),
                "fetched_at": fetched_at,
                "follow_command": follow_command(
                    locator,
                    checksum=snapshot.digest,
                    session_ref=session_ref,
                ),
                **source_payload,
                **resolved_visibility_metadata(
                    dict(metadata),
                    provider=str(metadata.get("plugin") or ""),
                    plugin=str(metadata.get("plugin") or ""),
                    subcommand=str(metadata.get("subcommand") or ""),
                    locator=locator,
                ),
            }
        )
    if mode == "best-effort":
        events.extend(local_events)
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
                item.get("source_time_field"),
            )
        ]
    ordered = sorted(
        events,
        key=lambda item: (
            str(item.get("source_time") or ""),
            str(item.get("locator") or ""),
            str(item.get("checksum") or ""),
        ),
    )
    return ordered, coverage_gap_count
