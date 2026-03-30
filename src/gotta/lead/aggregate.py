"""Aggregate lead edges into source summaries."""

from __future__ import annotations

from dataclasses import dataclass, field

from gotta.source.visibility import best_visibility_metadata

from .model import LeadEdge, LeadSearchOrigin, LeadSourceSummary
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

    @classmethod
    def from_edge(cls, edge: LeadEdge) -> _AggregatedLeadSourceState:
        return cls(
            locator=edge["targetLocator"],
            provider=edge["provider"],
            kind=edge["kind"],
            follow_command=edge["followCommand"],
            first_party=edge["firstParty"],
            search_seed=edge["searchSeed"],
            materialized=edge["materialized"],
        )

    def absorb(self, edge: LeadEdge) -> None:
        self.first_party = bool(self.first_party or edge["firstParty"])
        self.search_seed = bool(self.search_seed or edge["searchSeed"])
        self.materialized = bool(self.materialized or edge["materialized"])
        self.occurrence_count += int(edge["occurrenceCount"])
        self.relation_kinds.add(edge["relation"])
        self.source_checksums.add(edge["sourceChecksum"])
        self._record_search_origin(edge)
        self._record_examples(edge)
        self._record_targets(edge)
        self._record_contexts(edge)
        self.visibility = best_visibility_metadata(self.visibility, edge)

    def render_summary(self) -> LeadSourceSummary:
        search_origins = self._sorted_search_origins()
        rendered_item: LeadSourceSummary = {
            "locator": self.locator,
            "provider": self.provider,
            "kind": self.kind,
            "followCommand": self.follow_command,
            "firstParty": self.first_party,
            "searchSeed": self.search_seed,
            "materialized": self.materialized,
            "occurrenceCount": self.occurrence_count,
            "artifactCount": len(self.source_checksums),
            "relationKinds": sorted(str(value) for value in self.relation_kinds),
            "artifactLocators": sorted(str(value) for value in self.artifact_locators),
            "contentLocators": sorted(str(value) for value in self.content_locators),
            "exampleRaw": self.example_raw,
            "contexts": list(self.contexts),
            "bestSearchRank": int(search_origins[0].get("rank") or 0)
            if search_origins
            else 0,
            "searchLikeSourceCount": len(self.search_like_checksums),
            "searchOrigins": search_origins[:5],
        }
        for key, value in best_visibility_metadata(self.visibility).items():
            if value:
                rendered_item[key] = value
        return rendered_item

    def _record_search_origin(self, edge: LeadEdge) -> None:
        if not edge["sourceSearchLike"]:
            return
        self.search_like_checksums.add(edge["sourceChecksum"])
        search_rank = int(edge["sourceRank"] or 0)
        if search_rank <= 0:
            return
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
        if origin_key in self.search_origin_keys:
            return
        self.search_origin_keys.add(origin_key)
        self.search_origins.append(search_origin)

    def _record_examples(self, edge: LeadEdge) -> None:
        if self.example_raw:
            return
        raws = [str(value) for value in edge["rawExamples"] if str(value)]
        if raws:
            self.example_raw = raws[0]

    def _record_targets(self, edge: LeadEdge) -> None:
        for artifact_locator_value in edge["targetArtifactLocators"]:
            self.artifact_locators.add(str(artifact_locator_value))
        for content_locator_value in edge["targetContentLocators"]:
            self.content_locators.add(str(content_locator_value))

    def _record_contexts(self, edge: LeadEdge) -> None:
        for snippet in edge["contexts"]:
            value = str(snippet).strip()
            if value and value not in self.contexts and len(self.contexts) < 3:
                self.contexts.append(value)

    def _sorted_search_origins(self) -> list[LeadSearchOrigin]:
        return sorted(
            list(self.search_origins),
            key=lambda item: (
                int(item.get("rank") or 0)
                if int(item.get("rank") or 0) > 0
                else 1_000_000,
                str(item.get("provider") or "").casefold(),
                str(item.get("subcommand") or "").casefold(),
                str(item.get("artifactLocator") or "").casefold(),
            ),
        )


def aggregate_lead_sources(
    edge_records: list[LeadEdge],
) -> list[LeadSourceSummary]:
    aggregated: dict[str, _AggregatedLeadSourceState] = {}
    for edge in edge_records:
        locator = edge["targetLocator"]
        state = aggregated.setdefault(
            locator, _AggregatedLeadSourceState.from_edge(edge)
        )
        state.absorb(edge)
    return sorted(
        (state.render_summary() for state in aggregated.values()),
        key=lead_source_best_first_sort_key,
    )
