"""Payload orchestration for session timeline surfaces."""

from __future__ import annotations

from gotta.content.activity import activity_log_path

from ..core import (
    TIMELINE_MODE_DESCRIPTIONS,
    artifact_kind_counts,
    compile_filter_pattern,
    match_filter_text,
    paginate_items,
    top_count_records,
)
from .acquired import acquired_timeline_events
from .local import local_activity_timeline_events
from .model import (
    TimelineActorCountRecord,
    TimelineEvent,
    TimelinePayload,
    TimelinePluginCountRecord,
)
from .source import source_timeline_events
from .stamp import normalize_timeline_mode


def timeline_payload(
    dirs,
    *,
    limit: int = 100,
    offset: int = 0,
    include_all: bool = False,
    mode: str = "acquired",
    filter_query: str = "",
    session_ref: str = "",
) -> TimelinePayload:
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
        events, coverage_gap_count = source_timeline_events(
            dirs,
            mode=normalized_mode,
            session_ref=session_ref,
            filter_pattern=filter_pattern,
            local_events=local_events,
        )
    else:
        events = acquired_timeline_events(
            dirs,
            session_ref=session_ref,
            filter_pattern=filter_pattern,
            local_events=local_events,
        )
        coverage_gap_count = 0
    paged, paging = paginate_items(
        events,
        limit=limit,
        offset=offset,
        include_all=include_all,
        default_tail_window=True,
    )
    discovery_count, evidence_count = artifact_kind_counts(events)
    top_plugins = plugin_count_records(events)
    top_actors = actor_count_records(events)
    payload: TimelinePayload = {
        "sessionDir": str(dirs.session_dir),
        "contentDir": str(dirs.content_dir),
        "manifestPath": str(dirs.content_dir / "manifest.jsonl"),
        "activityPath": primary_activity_path,
        "activityPaths": activity_paths,
        "mode": normalized_mode,
        "modeDescription": TIMELINE_MODE_DESCRIPTIONS[normalized_mode],
        "coverageGapCount": coverage_gap_count,
        "eventCount": paging_int(paging.get("totalCount")),
        "offset": paging_int(paging.get("offset")),
        "limit": paging_limit(paging.get("limit")),
        "totalCount": paging_int(paging.get("totalCount")),
        "shownCount": paging_int(paging.get("shownCount")),
        "nextOffset": paging_next_offset(paging.get("nextOffset")),
        "truncated": paging_bool(paging.get("truncated")),
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "topPlugins": top_plugins,
        "topActors": top_actors,
        "filter": filter_text,
        "events": paged,
    }
    return payload


def plugin_count_records(
    events: list[TimelineEvent],
) -> list[TimelinePluginCountRecord]:
    records: list[TimelinePluginCountRecord] = []
    for record in top_count_records(
        [str(item.get("plugin") or "").strip() for item in events],
        key="plugin",
    ):
        plugin = record.get("plugin")
        count = record.get("count")
        if isinstance(plugin, str) and isinstance(count, int):
            records.append({"plugin": plugin, "count": count})
    return records


def actor_count_records(events: list[TimelineEvent]) -> list[TimelineActorCountRecord]:
    records: list[TimelineActorCountRecord] = []
    for record in top_count_records(
        [str(item.get("actor") or "").strip() for item in events],
        key="actor",
    ):
        actor = record.get("actor")
        count = record.get("count")
        if isinstance(actor, str) and isinstance(count, int):
            records.append({"actor": actor, "count": count})
    return records


def paging_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def paging_limit(value: object) -> int | None:
    return value if isinstance(value, int) else None


def paging_next_offset(value: object) -> int | None:
    return value if isinstance(value, int) else None


def paging_bool(value: object) -> bool:
    return value if isinstance(value, bool) else False
