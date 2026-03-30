"""Build lead edges from session content snapshots."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Mapping

from gotta.content.model import ContentSnapshot
from gotta.content.path import content_locator
from gotta.source.visibility import (
    best_visibility_metadata,
    classify_visibility_metadata,
)

from .model import LeadEdge
from .cache import lead_mentions_for_snapshot
from .rank import edge_best_first_sort_key, is_first_party_target
from .snapshot import (
    snapshot_artifact_locator,
    snapshot_display_name,
    snapshot_is_search_like,
    snapshot_locator,
    snapshot_provider,
    snapshot_sort_key,
    snapshot_subcommand,
)


def materialized_source_index(
    manifest_entries: Sequence[Mapping[str, object]],
    snapshot_by_digest: dict[str, ContentSnapshot],
) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for entry in manifest_entries:
        locator = str(
            entry.get("canonical_locator", "") or entry.get("locator", "")
        ).strip()
        checksum = str(entry.get("checksum", "")).strip()
        if not locator or not checksum or checksum not in snapshot_by_digest:
            continue
        index.setdefault(locator, set()).add(checksum)
    return index


def materialized_target_locators(
    locator: str,
    source_index: dict[str, set[str]],
    snapshot_by_digest: dict[str, ContentSnapshot],
) -> tuple[list[str], list[str]]:
    digests = sorted(source_index.get(locator, set()))
    artifact_locators = [
        snapshot_artifact_locator(snapshot_by_digest[digest])
        for digest in digests
        if digest in snapshot_by_digest
    ]
    content_locators = [content_locator(digest) for digest in digests]
    return artifact_locators, content_locators


def materialized_target_visibility(
    locator: str,
    source_index: dict[str, set[str]],
    snapshot_by_digest: dict[str, ContentSnapshot],
) -> dict[str, object]:
    return best_visibility_metadata(
        *[
            snapshot_by_digest[digest].artifact.metadata
            for digest in sorted(source_index.get(locator, set()))
            if digest in snapshot_by_digest
        ]
    )


def build_lead_edges(
    snapshots: list[ContentSnapshot],
    manifest_entries: Sequence[Mapping[str, object]],
    *,
    classify_kind,
) -> list[LeadEdge]:
    snapshot_by_digest = {snapshot.digest: snapshot for snapshot in snapshots}
    source_index = materialized_source_index(manifest_entries, snapshot_by_digest)
    rendered: list[LeadEdge] = []
    for snapshot in sorted(snapshots, key=snapshot_sort_key, reverse=True):
        source_locator = snapshot_locator(snapshot)
        source_search_like = snapshot_is_search_like(snapshot)
        source_provider = snapshot_provider(snapshot)
        source_subcommand = snapshot_subcommand(snapshot)
        edge_state: dict[str, LeadEdge] = {}
        for mention in lead_mentions_for_snapshot(snapshot):
            target_locator = mention.canonical_locator.strip()
            if not target_locator or target_locator == source_locator:
                continue
            state = edge_state.setdefault(
                target_locator,
                {
                    "sourceChecksum": snapshot.digest,
                    "sourceLocator": source_locator or content_locator(snapshot.digest),
                    "sourceArtifactLocator": snapshot_artifact_locator(snapshot),
                    "sourceContentLocator": content_locator(snapshot.digest),
                    "sourcePreferredName": snapshot_display_name(snapshot),
                    "targetLocator": target_locator,
                    "provider": mention.provider,
                    "kind": classify_kind(target_locator, mention.provider),
                    "relation": mention.relation,
                    "followCommand": mention.follow_command,
                    "rawExamples": [],
                    "contexts": [],
                    "occurrenceCount": 0,
                    "sourceSearchLike": source_search_like,
                    "sourceProvider": source_provider,
                    "sourceSubcommand": source_subcommand,
                    "sourceRank": mention.ordinal if source_search_like else 0,
                    "materialized": False,
                    "targetArtifactLocators": [],
                    "targetContentLocators": [],
                    "firstParty": False,
                    "searchSeed": False,
                },
            )
            state["occurrenceCount"] = int(state["occurrenceCount"]) + 1
            mention_ordinal = int(mention.ordinal or 0) if source_search_like else 0
            if mention_ordinal and (
                not int(state.get("sourceRank") or 0)
                or mention_ordinal < int(state.get("sourceRank") or 0)
            ):
                state["sourceRank"] = mention_ordinal
            if (
                mention.raw not in state["rawExamples"]
                and len(state["rawExamples"]) < 3
            ):
                state["rawExamples"].append(mention.raw)
            if (
                mention.snippet
                and mention.snippet not in state["contexts"]
                and len(state["contexts"]) < 3
            ):
                state["contexts"].append(mention.snippet)
        for state in edge_state.values():
            artifact_locators, content_locators = materialized_target_locators(
                str(state["targetLocator"]),
                source_index,
                snapshot_by_digest,
            )
            target_visibility = best_visibility_metadata(
                classify_visibility_metadata(
                    {},
                    provider=str(state["provider"]),
                    locator=str(state["targetLocator"]),
                ),
                materialized_target_visibility(
                    str(state["targetLocator"]),
                    source_index,
                    snapshot_by_digest,
                ),
            )
            state["materialized"] = bool(artifact_locators or content_locators)
            state["targetArtifactLocators"] = artifact_locators
            state["targetContentLocators"] = content_locators
            state["firstParty"] = is_first_party_target(
                provider=str(state["provider"]),
                kind=str(state["kind"]),
            )
            state["searchSeed"] = str(state["kind"]).endswith("-search")
            for key in (
                "visibility_level",
                "visibility_boundary",
                "visibility_confidence",
                "visibility_basis",
            ):
                value = target_visibility.get(key)
                if value:
                    state[key] = value
            rendered.append(state)
    return sorted(
        rendered,
        key=lambda item: (
            str(item["sourcePreferredName"]).casefold(),
            *edge_best_first_sort_key(item),
        ),
    )
