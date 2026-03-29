"""Aggregate lead edges into source summaries."""

from __future__ import annotations

from dataclasses import dataclass, field

from gotta.source.visibility import best_visibility_metadata

from .model import LeadEdgeRecord, LeadSearchOrigin, LeadSourceSummary
from .rank import lead_source_best_first_sort_key


@dataclass(slots=True)
class _AggregatedLeadSourceState:
    locator: str
    provider: str
    kind: str
    follow_command: str
    first_party: bool
    search_seed: bool
    materialized: bool
    occurrence_count: int = 0
    relation_kinds: set[str] = field(default_factory=set)
    artifact_locators: set[str] = field(default_factory=set)
    content_locators: set[str] = field(default_factory=set)
    source_checksums: set[str] = field(default_factory=set)
    search_like_checksums: set[str] = field(default_factory=set)
    search_origin_keys: set[tuple[str, str, int, str, str]] = field(default_factory=set)
    search_origins: list[LeadSearchOrigin] = field(default_factory=list)
    example_raw: str = ""
    contexts: list[str] = field(default_factory=list)
    visibility: dict[str, object] = field(default_factory=dict)


def aggregate_lead_sources(
    edge_records: list[LeadEdgeRecord],
) -> list[LeadSourceSummary]:
    aggregated: dict[str, _AggregatedLeadSourceState] = {}
    for edge in edge_records:
        locator = edge["targetLocator"]
        state = aggregated.setdefault(
            locator,
            _AggregatedLeadSourceState(
                locator=locator,
                provider=edge["provider"],
                kind=edge["kind"],
                follow_command=edge["followCommand"],
                first_party=edge["firstParty"],
                search_seed=edge["searchSeed"],
                materialized=edge["materialized"],
            ),
        )
        state.first_party = bool(state.first_party or edge["firstParty"])
        state.search_seed = bool(state.search_seed or edge["searchSeed"])
        state.materialized = bool(state.materialized or edge["materialized"])
        state.occurrence_count += int(edge["occurrenceCount"])
        state.relation_kinds.add(edge["relation"])
        state.source_checksums.add(edge["sourceChecksum"])
        if edge["sourceSearchLike"]:
            state.search_like_checksums.add(edge["sourceChecksum"])
            search_rank = int(edge["sourceRank"] or 0)
            if search_rank > 0:
                search_origin: LeadSearchOrigin = {
                    "provider": edge["sourceProvider"],
                    "subcommand": edge["sourceSubcommand"],
                    "rank": search_rank,
                    "artifactLocator": edge["sourceArtifactLocator"],
                    "sourceLocator": edge["sourceLocator"],
                }
                origin_key = (
                    search_origin["provider"],
                    search_origin["subcommand"],
                    search_origin["rank"],
                    search_origin["artifactLocator"],
                    search_origin["sourceLocator"],
                )
                if origin_key not in state.search_origin_keys:
                    state.search_origin_keys.add(origin_key)
                    state.search_origins.append(search_origin)
        if not state.example_raw:
            raws = [str(value) for value in edge["rawExamples"] if str(value)]
            if raws:
                state.example_raw = raws[0]
        for artifact_locator_value in edge["targetArtifactLocators"]:
            state.artifact_locators.add(str(artifact_locator_value))
        for content_locator_value in edge["targetContentLocators"]:
            state.content_locators.add(str(content_locator_value))
        for snippet in edge["contexts"]:
            value = str(snippet).strip()
            if value and value not in state.contexts and len(state.contexts) < 3:
                state.contexts.append(value)
        state.visibility = best_visibility_metadata(
            state.visibility,
            edge,
        )
    rendered: list[LeadSourceSummary] = []
    for locator, state in aggregated.items():
        relation_kinds = {str(value) for value in state.relation_kinds}
        artifact_count = len(state.source_checksums)
        search_like_source_count = len(state.search_like_checksums)
        search_origins: list[LeadSearchOrigin] = sorted(
            list(state.search_origins),
            key=lambda item: (
                int(item.get("rank") or 0)
                if int(item.get("rank") or 0) > 0
                else 1_000_000,
                str(item.get("provider") or "").casefold(),
                str(item.get("subcommand") or "").casefold(),
                str(item.get("artifactLocator") or "").casefold(),
            ),
        )
        visibility = best_visibility_metadata(state.visibility)
        rendered_item: LeadSourceSummary = {
            "locator": locator,
            "provider": state.provider,
            "kind": state.kind,
            "followCommand": state.follow_command,
            "firstParty": state.first_party,
            "searchSeed": state.search_seed,
            "materialized": state.materialized,
            "occurrenceCount": state.occurrence_count,
            "artifactCount": artifact_count,
            "relationKinds": sorted(relation_kinds),
            "artifactLocators": sorted(str(value) for value in state.artifact_locators),
            "contentLocators": sorted(str(value) for value in state.content_locators),
            "exampleRaw": state.example_raw,
            "contexts": list(state.contexts),
            "bestSearchRank": int(search_origins[0].get("rank") or 0)
            if search_origins
            else 0,
            "searchLikeSourceCount": search_like_source_count,
            "searchOrigins": search_origins[:5],
        }
        for key in (
            "visibility_level",
            "visibility_boundary",
            "visibility_confidence",
            "visibility_basis",
        ):
            value = visibility.get(key)
            if value:
                rendered_item[key] = value
        rendered.append(rendered_item)
    return sorted(rendered, key=lead_source_best_first_sort_key)
