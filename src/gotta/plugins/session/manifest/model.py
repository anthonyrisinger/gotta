"""Typed payload and record shapes for `gotta session manifest`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict


class ManifestVisibility(TypedDict, total=False):
    visibility_level: str
    visibility_boundary: str
    visibility_confidence: str
    visibility_basis: list[str]


class ManifestRecord(ManifestVisibility, total=False):
    plugin: str
    actor: str
    target_actor: str
    subcommand: str
    locator: str
    canonical_locator: str
    preferred_name: str
    checksum: str
    fetched_at: str
    artifact_kind: str
    artifactKind: str
    fetchCount: int
    firstFetchedAt: str
    lastFetchedAt: str
    plugins: list[str]
    actors: list[str]
    locators: list[str]
    artifactKinds: list[str]


class ManifestPayloadEntry(ManifestVisibility):
    plugin: str
    actor: str
    target_actor: str
    subcommand: str
    locator: str
    canonical_locator: str
    preferred_name: str
    checksum: str
    fetched_at: str
    artifactKind: str
    fetchCount: int
    firstFetchedAt: str
    lastFetchedAt: str
    plugins: list[str]
    actors: list[str]
    locators: list[str]
    artifactKinds: list[str]
    artifact_locator: str
    content_locator: str
    follow_command: str
    content_follow_command: str
    artifact_follow_command: str


class ManifestPluginCountRecord(TypedDict):
    plugin: str
    count: int


class ManifestActorCountRecord(TypedDict):
    actor: str
    count: int


class ManifestPayload(TypedDict):
    sessionDir: str
    contentDir: str
    manifestPath: str
    sessionRef: str
    entryCount: int
    fetchRecordCount: int
    offset: int
    limit: int | None
    totalCount: int
    shownCount: int
    nextOffset: int | None
    truncated: bool
    discoveryArtifactCount: int
    evidenceArtifactCount: int
    topPlugins: list[ManifestPluginCountRecord]
    topActors: list[ManifestActorCountRecord]
    pluginFilter: str
    actorFilter: str
    locatorFilter: str
    filter: str
    entries: list[ManifestPayloadEntry]


def manifest_visibility(value: Mapping[str, object]) -> ManifestVisibility:
    visibility: ManifestVisibility = {}
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


def apply_manifest_visibility(
    entry: ManifestRecord | ManifestPayloadEntry,
    visibility: ManifestVisibility,
) -> None:
    if "visibility_level" in visibility:
        entry["visibility_level"] = visibility["visibility_level"]
    if "visibility_boundary" in visibility:
        entry["visibility_boundary"] = visibility["visibility_boundary"]
    if "visibility_confidence" in visibility:
        entry["visibility_confidence"] = visibility["visibility_confidence"]
    if "visibility_basis" in visibility:
        entry["visibility_basis"] = visibility["visibility_basis"]
