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
