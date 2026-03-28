"""Timeline surface for `gotta session`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex

from gotta.compat import UTC, datetime
from gotta.actor import session_actor
from gotta.content.activity import activity_events, activity_log_path
from gotta.content.model import ContentSnapshot
from gotta.content.path import content_locator
from gotta.content.store import scan_content_store
from gotta.source import classify_visibility_metadata
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope

from .core import (
    AGGREGATE_SOURCE_SUBCOMMANDS,
    LOCAL_TIMELINE_FILES,
    TIMELINE_MODE_DESCRIPTIONS,
    TIMELINE_TEXT_PREVIEW_LIMIT,
    append_count_section,
    artifact_human_locator,
    artifact_kind,
    artifact_kind_counts,
    compile_filter_pattern,
    filter_suffix,
    follow_command,
    match_any,
    match_filter_text,
    paging_summary_line,
    paginate_items,
    rendered_actor,
    resolved_visibility_metadata,
    top_count_records,
    visibility_summary,
)
from .manifest import manifest_entries
from .parse import explicit_session_ref, require_started_session, session_dirs_for_read


def _parse_source_timestamp(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if re.fullmatch(r"\d{10}\.\d{6}", value):
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
    normalized = {
        "acquisition": "acquired",
        "acquired": "acquired",
        "observed": "acquired",
        "best-effort": "best-effort",
        "created": "created",
        "updated": "updated",
    }.get(mode.strip().lower(), "")
    if normalized:
        return normalized
    choices = ", ".join(sorted({"acquired", "best-effort", "created", "updated"}))
    raise SystemExit(f"invalid timeline mode: {mode}. expected one of: {choices}")


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
        if not re.match(r"^(?:https?://|[a-z][a-z0-9+.-]*:)", base):
            return False
    if plugin == "slack" and locator.startswith("slack:sql "):
        if re.search(
            r"\bPRAGMA\s+table_info\b|\bsqlite_master\b", locator, re.IGNORECASE
        ):
            return False
    return True


def _iso_utc_from_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def aggregate_source_snapshot(snapshot: ContentSnapshot) -> bool:
    subcommand = str(snapshot.metadata.get("subcommand") or "").strip().lower()
    if subcommand in AGGREGATE_SOURCE_SUBCOMMANDS:
        return True
    locator = str(
        snapshot.metadata.get("canonical_locator", "")
        or snapshot.metadata.get("locator", "")
    ).strip()
    if re.search(
        r":(?:search|status|workspaces|sql|schema|list-channels|list-users)\b",
        locator,
    ):
        return True
    if re.search(
        r"^(?:search|status|workspaces|sql|schema|list-channels|list-users)\b",
        locator,
    ):
        return True
    return False


def _meaningful_local_surface(path: Path) -> bool:
    return path.is_file()


def _timeline_activity_roots(dirs) -> list[Path]:
    grouped_root = session_registry._group_session_root(dirs.session_dir)
    if grouped_root != dirs.session_dir.resolve() or (grouped_root / "actors").is_dir():
        actor_ids = list(session_scope._selected_actor_ids(grouped_root))
        if actor_ids:
            return [
                session_registry._actor_session_dir(grouped_root, actor_id)
                for actor_id in actor_ids
            ]
    return [dirs.session_dir.resolve()]


def local_activity_timeline_events(dirs) -> tuple[list[dict[str, object]], list[str]]:
    events: list[dict[str, object]] = []
    activity_paths: list[str] = []
    seen_locators: set[tuple[str, str]] = set()
    seen_activity_roots: set[Path] = set()
    for activity_root in _timeline_activity_roots(dirs):
        resolved_root = activity_root.resolve()
        if resolved_root in seen_activity_roots:
            continue
        seen_activity_roots.add(resolved_root)
        activity_paths.append(str(activity_log_path(resolved_root)))
        for raw in activity_events(resolved_root):
            timestamp = str(raw.get("timestamp") or "").strip()
            if not timestamp:
                continue
            locator = str(raw.get("locator") or "").strip() or "unknown"
            seen_locators.add((str(resolved_root), locator))
            events.append(
                {
                    "mode": "local",
                    "source_time": timestamp,
                    "source_time_field": str(
                        raw.get("time_field") or "session_recorded_at"
                    ),
                    "source_created_at": "",
                    "source_updated_at": "",
                    "source_published_at": "",
                    "fetched_at": timestamp,
                    "plugin": str(raw.get("plugin") or "session").strip() or "session",
                    "actor": rendered_actor(
                        raw.get("actor"), session_root=resolved_root
                    ),
                    "target_actor": str(raw.get("target_actor") or "").strip(),
                    "locator": locator,
                    "preferred_name": str(raw.get("preferred_name") or locator).strip()
                    or locator,
                    "checksum": "",
                    "artifactKind": "",
                    "content_locator": "",
                    "artifact_locator": "",
                    "follow_command": str(raw.get("follow_command") or "").strip(),
                    "detail": str(raw.get("detail") or "").strip(),
                    "event_kind": "local",
                    **classify_visibility_metadata(
                        {},
                        provider="gotta",
                        plugin=str(raw.get("plugin") or "session").strip() or "session",
                        locator=locator,
                    ),
                }
            )
    candidates: list[tuple[str, Path, str, str, str]] = []
    for activity_root in _timeline_activity_roots(dirs):
        resolved_root = activity_root.resolve()
        actor = (
            session_actor(resolved_root)
            if resolved_root.parent.name == "actors"
            else ""
        )
        for relative in LOCAL_TIMELINE_FILES:
            surface = "want" if relative == "WANT.md" else "goal"
            locator = f"{surface}:actor:{actor}" if actor else f"{surface}:session"
            follow = f"gotta {surface} --actor {actor}" if actor else f"gotta {surface}"
            candidates.append(
                (
                    "actor" if actor else "session",
                    resolved_root / relative,
                    locator,
                    follow,
                    locator,
                )
            )
    for plugin, path, locator, follow_path, preferred_name in candidates:
        candidate_scope = str(path.parent.resolve())
        if (candidate_scope, locator) in seen_locators or not _meaningful_local_surface(
            path
        ):
            continue
        timestamp = _iso_utc_from_timestamp(path.stat().st_mtime)
        actor = (
            session_actor(path.parent)
            if plugin == "actor"
            else rendered_actor("", session_root=dirs.session_dir)
        )
        events.append(
            {
                "mode": "local",
                "source_time": timestamp,
                "source_time_field": "filesystem_mtime",
                "source_created_at": "",
                "source_updated_at": "",
                "source_published_at": "",
                "fetched_at": timestamp,
                "plugin": plugin,
                "actor": actor,
                "target_actor": "",
                "locator": locator,
                "preferred_name": preferred_name,
                "checksum": "",
                "artifactKind": "",
                "content_locator": "",
                "artifact_locator": "",
                "follow_command": follow_path,
                "detail": "local surface snapshot",
                "event_kind": "local",
                **classify_visibility_metadata(
                    {},
                    provider="gotta",
                    plugin=plugin,
                    locator=locator,
                ),
            }
        )
    return events, activity_paths


def timeline_payload(
    dirs,
    *,
    limit: int = 100,
    offset: int = 0,
    include_all: bool = False,
    mode: str = "acquired",
    filter_query: str = "",
    session_ref: str = "",
) -> dict[str, object]:
    normalized_mode = normalize_timeline_mode(mode)
    local_events, activity_paths = local_activity_timeline_events(dirs)
    primary_activity_path = (
        activity_paths[0]
        if activity_paths
        else str(activity_log_path(dirs.session_dir))
    )
    filter_text = match_filter_text(filter_query)
    filter_pattern = compile_filter_pattern(filter_text)
    if normalized_mode != "acquired":
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
                "target_actor": str(
                    snapshot.metadata.get("target_actor") or ""
                ).strip(),
                "locator": locator,
                "preferred_name": str(
                    snapshot.metadata.get("preferred_name") or ""
                ).strip()
                or snapshot.names[0]
                if snapshot.names
                else "data",
                "event_kind": "source",
            }
            source_time, source_field = _source_timestamp_for_mode(
                snapshot, normalized_mode
            )
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
                    "source_published_at": source_timestamps.get(
                        "source_published_at", ""
                    ),
                    "checksum": snapshot.digest,
                    "artifactKind": artifact_kind(
                        snapshot.metadata.get("artifact_kind")
                    ),
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
        if normalized_mode == "best-effort":
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
        paged, paging = paginate_items(
            ordered,
            limit=limit,
            offset=offset,
            include_all=include_all,
            default_tail_window=True,
        )
        discovery_count, evidence_count = artifact_kind_counts(ordered)
        top_plugins = top_count_records(
            [str(item.get("plugin") or "").strip() for item in ordered],
            key="plugin",
        )
        top_actors = top_count_records(
            [str(item.get("actor") or "").strip() for item in ordered],
            key="actor",
        )
        return {
            "sessionDir": str(dirs.session_dir),
            "contentDir": str(dirs.content_dir),
            "manifestPath": str(dirs.content_dir / "manifest.jsonl"),
            "activityPath": primary_activity_path,
            "activityPaths": activity_paths,
            "mode": normalized_mode,
            "modeDescription": TIMELINE_MODE_DESCRIPTIONS[normalized_mode],
            "coverageGapCount": coverage_gap_count,
            "eventCount": paging["totalCount"],
            **paging,
            "discoveryArtifactCount": discovery_count,
            "evidenceArtifactCount": evidence_count,
            "topPlugins": top_plugins,
            "topActors": top_actors,
            "filter": filter_text,
            "events": paged,
        }
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
    paged, paging = paginate_items(
        events,
        limit=limit,
        offset=offset,
        include_all=include_all,
        default_tail_window=True,
    )
    discovery_count, evidence_count = artifact_kind_counts(events)
    top_plugins = top_count_records(
        [str(item.get("plugin") or "").strip() for item in events],
        key="plugin",
    )
    top_actors = top_count_records(
        [str(item.get("actor") or "").strip() for item in events],
        key="actor",
    )
    return {
        "sessionDir": str(dirs.session_dir),
        "contentDir": str(dirs.content_dir),
        "manifestPath": str(dirs.content_dir / "manifest.jsonl"),
        "activityPath": primary_activity_path,
        "activityPaths": activity_paths,
        "mode": normalized_mode,
        "modeDescription": TIMELINE_MODE_DESCRIPTIONS[normalized_mode],
        "coverageGapCount": 0,
        "eventCount": paging["totalCount"],
        **paging,
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "topPlugins": top_plugins,
        "topActors": top_actors,
        "filter": filter_text,
        "events": paged,
    }


def cmd_timeline(args: argparse.Namespace) -> int:
    dirs = session_dirs_for_read(args)
    require_started_session(dirs)
    session_ref = explicit_session_ref(args)
    payload = timeline_payload(
        dirs,
        limit=max(args.limit, 0),
        offset=max(args.offset, 0),
        include_all=bool(args.all),
        mode=args.mode,
        filter_query=str(getattr(args, "filter", "") or ""),
        session_ref=session_ref,
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"timeline: {payload['manifestPath']}")
    activity_paths = list(payload.get("activityPaths") or [])
    if len(activity_paths) > 1:
        print(f"activity: {len(activity_paths)} actor activity logs")
    else:
        print(f"activity: {payload['activityPath']}")
    print(f"mode: {payload['mode']} ({payload['modeDescription']})")
    print(f"coverage_gaps: {payload.get('coverageGapCount', 0)}")
    print(
        "events: "
        f"{payload['eventCount']} total "
        f"(discovery {payload['discoveryArtifactCount']}, "
        f"evidence {payload['evidenceArtifactCount']})"
        f"{filter_suffix(payload.get('filter'))}"
    )
    print(
        paging_summary_line(
            label="page",
            total_count=int(payload["totalCount"]),
            shown_count=int(payload["shownCount"]),
            offset=int(payload["offset"]),
            next_offset=(
                int(payload["nextOffset"])
                if payload.get("nextOffset") is not None
                else None
            ),
        )
    )
    if int(payload["shownCount"]) == 0 and int(payload["totalCount"]) > 0:
        print("page: no results in this page window")
    top_plugins_lines: list[str] = []
    append_count_section(
        top_plugins_lines,
        heading="top plugins",
        records=list(payload.get("topPlugins") or []),
        key="plugin",
    )
    if top_plugins_lines:
        print("\n".join(top_plugins_lines))
    top_actors_lines: list[str] = []
    append_count_section(
        top_actors_lines,
        heading="top actors",
        records=list(payload.get("topActors") or []),
        key="actor",
    )
    if top_actors_lines:
        print("\n".join(top_actors_lines))
    preview_events = list(payload["events"])[:TIMELINE_TEXT_PREVIEW_LIMIT]
    if payload["events"]:
        print(
            "events preview:"
            if len(payload["events"]) <= TIMELINE_TEXT_PREVIEW_LIMIT
            else f"events preview (showing {len(preview_events)} of {len(payload['events'])}):"
        )
    for event in preview_events:
        actor_label = event["actor"]
        if event.get("target_actor") and event["target_actor"] != actor_label:
            actor_label = f"{actor_label}->{event['target_actor']}"
        checksum = event["checksum"][:12] if event["checksum"] else "unknown"
        if payload["mode"] != "acquired":
            print(
                f"- {event.get('source_time') or 'unknown-time'} "
                f"[{event['plugin']}/{actor_label}] "
                f"{event['locator']} -> {event['preferred_name']} ({checksum}) "
                f"(from {event.get('source_time_field') or 'unknown-field'})"
            )
            if event.get("artifactKind"):
                print(f"  artifact_kind: {event['artifactKind']}")
            visibility = visibility_summary(event)
            if visibility:
                print(f"  visibility: {visibility}")
            if event.get("artifact_locator") or event.get("content_locator"):
                print(
                    "  "
                    + "stored: "
                    + ", ".join(
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
                    )
                )
        else:
            print(
                f"- {event['fetched_at'] or 'unknown-time'} "
                f"[{event['plugin']}/{actor_label}] "
                f"{event['locator']} -> {event['preferred_name']} ({checksum})"
                f"{' (local)' if event.get('event_kind') == 'local' else ''}"
            )
            if event.get("artifactKind"):
                print(f"  artifact_kind: {event['artifactKind']}")
            visibility = visibility_summary(event)
            if visibility:
                print(f"  visibility: {visibility}")
            if event.get("artifact_locator") or event.get("content_locator"):
                print(
                    "  "
                    + "stored: "
                    + ", ".join(
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
                    )
                )
    hidden = len(payload["events"]) - len(preview_events)
    if hidden > 0:
        print(f"  - ... {hidden} additional events hidden in text view")
    return 0
