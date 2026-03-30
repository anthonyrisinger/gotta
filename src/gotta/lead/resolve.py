"""Resolve lead targets to stored snapshots."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from typing import Mapping

from gotta.content.model import ContentError, ContentSnapshot
from gotta.content.path import content_locator

from .aggregate import aggregate_lead_sources
from .edge import build_lead_edges
from .model import LeadResolution
from .snapshot import (
    snapshot_artifact_locator,
    snapshot_display_name,
    snapshot_sort_key,
)


def resolve_lead_snapshots(
    target: str,
    snapshots: list[ContentSnapshot],
    manifest_entries: Sequence[Mapping[str, object]],
) -> list[ContentSnapshot]:
    ordered = sorted(snapshots, key=snapshot_sort_key, reverse=True)
    requested = target.strip()
    if not requested:
        return ordered
    by_digest = {snapshot.digest: snapshot for snapshot in ordered}
    if requested.startswith("content:"):
        requested = requested.removeprefix("content:").strip()
    if requested and all(char in "0123456789abcdef" for char in requested.casefold()):
        matches = [
            snapshot for snapshot in ordered if snapshot.digest.startswith(requested)
        ]
        if not matches:
            raise ContentError(
                f"no stored artifact matched checksum or content locator `{target}`"
            )
        if len(matches) > 1:
            suggestions = ", ".join(
                content_locator(snapshot.digest) for snapshot in matches[:5]
            )
            raise ContentError(
                f"ambiguous content locator `{target}`; disambiguate with one of: {suggestions}"
            )
        return matches
    artifact_matches = [
        snapshot
        for snapshot in ordered
        if snapshot_artifact_locator(snapshot) == requested
    ]
    if artifact_matches:
        return artifact_matches
    name_matches = [
        snapshot
        for snapshot in ordered
        if requested == snapshot_display_name(snapshot)
        or any(alias.name == requested for alias in snapshot.aliases)
    ]
    if len(name_matches) == 1:
        return name_matches
    if len(name_matches) > 1:
        suggestions = ", ".join(
            snapshot_artifact_locator(snapshot) for snapshot in name_matches[:5]
        )
        raise ContentError(
            f"ambiguous stored artifact name `{target}`; disambiguate with one of: {suggestions}"
        )
    locator_matches: set[str] = set()
    for entry in manifest_entries:
        locator = str(
            entry.get("canonical_locator", "") or entry.get("locator", "")
        ).strip()
        if locator == requested:
            checksum = str(entry.get("checksum", "")).strip()
            if checksum and checksum in by_digest:
                locator_matches.add(checksum)
    if locator_matches:
        return [by_digest[digest] for digest in sorted(locator_matches, reverse=True)]
    raise ContentError(
        "unsupported session leads target. Use an emitted artifact locator, "
        "content locator, stored artifact name, digest prefix, or a materialized source locator."
    )


def resolve_lead_resolution(
    target: str,
    snapshots: list[ContentSnapshot],
    manifest_entries: Sequence[Mapping[str, object]],
    *,
    classify_kind: Callable[[str, str], str],
) -> LeadResolution:
    selected_snapshots = resolve_lead_snapshots(target, snapshots, manifest_entries)
    selected_digests = {snapshot.digest for snapshot in selected_snapshots}
    edge_records = [
        edge
        for edge in build_lead_edges(
            snapshots,
            manifest_entries,
            classify_kind=classify_kind,
        )
        if edge["sourceChecksum"] in selected_digests
    ]
    lead_sources = aggregate_lead_sources(edge_records)
    return LeadResolution(
        selected_snapshots=selected_snapshots,
        edge_records=edge_records,
        lead_sources=lead_sources,
    )
