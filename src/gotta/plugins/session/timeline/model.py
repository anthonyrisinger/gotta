"""Typed payload shapes for `gotta session timeline`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypedDict


class TimelineVisibility(TypedDict, total=False):
    visibility_level: str
    visibility_boundary: str
    visibility_confidence: str
    visibility_basis: list[str]


class TimelinePluginCountRecord(TypedDict):
    plugin: str
    count: int


class TimelineActorCountRecord(TypedDict):
    actor: str
    count: int


class TimelineEvent(TimelineVisibility):
    mode: Literal["acquired", "source", "local"]
    source_time: str
    source_time_field: str
    source_created_at: str
    source_updated_at: str
    source_published_at: str
    fetched_at: str
    plugin: str
    actor: str
    target_actor: str
    locator: str
    preferred_name: str
    checksum: str
    artifactKind: str
    content_locator: str
    artifact_locator: str
    follow_command: str
    detail: str
    surface: str
    event_kind: Literal["source", "local"]


class TimelinePayload(TypedDict):
    sessionDir: str
    contentDir: str
    manifestPath: str
    activityPath: str
    activityPaths: list[str]
    mode: str
    modeDescription: str
    coverageGapCount: int
    eventCount: int
    offset: int
    limit: int | None
    totalCount: int
    shownCount: int
    nextOffset: int | None
    truncated: bool
    discoveryArtifactCount: int
    evidenceArtifactCount: int
    topPlugins: list[TimelinePluginCountRecord]
    topActors: list[TimelineActorCountRecord]
    filter: str
    events: list[TimelineEvent]


def timeline_visibility(value: Mapping[str, object]) -> TimelineVisibility:
    visibility: TimelineVisibility = {}
    visibility_level = value.get("visibility_level")
    visibility_boundary = value.get("visibility_boundary")
    visibility_confidence = value.get("visibility_confidence")
    visibility_basis = value.get("visibility_basis")
    if isinstance(visibility_level, str):
        visibility["visibility_level"] = visibility_level
    if isinstance(visibility_boundary, str):
        visibility["visibility_boundary"] = visibility_boundary
    if isinstance(visibility_confidence, str):
        visibility["visibility_confidence"] = visibility_confidence
    if isinstance(visibility_basis, list):
        visibility["visibility_basis"] = [
            str(item).strip() for item in visibility_basis if str(item).strip()
        ]
    return visibility


def apply_visibility(event: TimelineEvent, visibility: TimelineVisibility) -> None:
    if "visibility_level" in visibility:
        event["visibility_level"] = visibility["visibility_level"]
    if "visibility_boundary" in visibility:
        event["visibility_boundary"] = visibility["visibility_boundary"]
    if "visibility_confidence" in visibility:
        event["visibility_confidence"] = visibility["visibility_confidence"]
    if "visibility_basis" in visibility:
        event["visibility_basis"] = visibility["visibility_basis"]
