"""Source-authored timeline event helpers."""

from __future__ import annotations

import shlex

from gotta.content.model import ContentSnapshot
from gotta.content.path import content_locator
from gotta.content.store import scan_content_store

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
    subcommand = str(snapshot.metadata.get("subcommand") or "").strip().lower()
    if subcommand in AGGREGATE_SOURCE_SUBCOMMANDS:
        return True
    locator = str(
        snapshot.metadata.get("canonical_locator", "")
        or snapshot.metadata.get("locator", "")
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
    plugin = str(snapshot.metadata.get("plugin", "")).strip() or "unknown-plugin"
    locator = str(
        snapshot.metadata.get("canonical_locator", "")
        or snapshot.metadata.get("locator", "")
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
    snapshots = scan_content_store(dirs.content_dir)
    events: list[dict[str, object]] = []
    coverage_gap_count = 0
    for snapshot in snapshots:
        locator = str(
            snapshot.metadata.get("canonical_locator", "")
            or snapshot.metadata.get("locator", "")
        ).strip()
        source_payload = {
            "plugin": str(snapshot.metadata.get("plugin", "")).strip()
            or "unknown-plugin",
            "actor": rendered_actor(
                snapshot.metadata.get("actor"),
                session_root=dirs.session_dir,
            ),
            "target_actor": str(snapshot.metadata.get("target_actor") or "").strip(),
            "locator": locator,
            "preferred_name": (
                str(snapshot.metadata.get("preferred_name") or "").strip()
                or (snapshot.names[0] if snapshot.names else "data")
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
                "artifactKind": artifact_kind(snapshot.metadata.get("artifact_kind")),
                "content_locator": content_locator(snapshot.digest),
                "artifact_locator": artifact_human_locator(
                    str(snapshot.metadata.get("preferred_name") or "").strip()
                    or "data",
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
                    dict(snapshot.metadata),
                    provider=str(snapshot.metadata.get("plugin") or ""),
                    plugin=str(snapshot.metadata.get("plugin") or ""),
                    subcommand=str(snapshot.metadata.get("subcommand") or ""),
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
