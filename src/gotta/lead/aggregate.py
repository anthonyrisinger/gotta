"""Aggregate lead edges into source summaries."""

from __future__ import annotations

from gotta.source import best_visibility_metadata

from .rank import lead_source_best_first_sort_key


def aggregate_lead_sources(
    edge_records: list[dict[str, object]],
) -> list[dict[str, object]]:
    aggregated: dict[str, dict[str, object]] = {}
    for edge in edge_records:
        locator = str(edge["targetLocator"])
        state = aggregated.setdefault(
            locator,
            {
                "locator": locator,
                "provider": str(edge["provider"]),
                "kind": str(edge["kind"]),
                "followCommand": str(edge["followCommand"]),
                "firstParty": bool(edge.get("firstParty")),
                "searchSeed": bool(edge.get("searchSeed")),
                "materialized": bool(edge["materialized"]),
                "occurrenceCount": 0,
                "relationKinds": set(),
                "artifactLocators": set(),
                "contentLocators": set(),
                "sourceChecksums": set(),
                "searchLikeChecksums": set(),
                "searchOriginKeys": set(),
                "searchOrigins": [],
                "exampleRaw": "",
                "contexts": [],
                "visibility": {},
            },
        )
        state["firstParty"] = bool(state["firstParty"] or edge.get("firstParty"))
        state["searchSeed"] = bool(state["searchSeed"] or edge.get("searchSeed"))
        state["materialized"] = bool(state["materialized"] or edge["materialized"])
        state["occurrenceCount"] = int(state["occurrenceCount"]) + int(
            edge["occurrenceCount"]
        )
        state["relationKinds"].add(str(edge["relation"]))
        state["sourceChecksums"].add(str(edge["sourceChecksum"]))
        if bool(edge.get("sourceSearchLike")):
            state["searchLikeChecksums"].add(str(edge["sourceChecksum"]))
            search_rank = int(edge.get("sourceRank") or 0)
            if search_rank > 0:
                search_origin = {
                    "provider": str(edge.get("sourceProvider") or ""),
                    "subcommand": str(edge.get("sourceSubcommand") or ""),
                    "rank": search_rank,
                    "artifactLocator": str(edge.get("sourceArtifactLocator") or ""),
                    "sourceLocator": str(edge.get("sourceLocator") or ""),
                }
                origin_key = (
                    search_origin["provider"],
                    search_origin["subcommand"],
                    search_origin["rank"],
                    search_origin["artifactLocator"],
                    search_origin["sourceLocator"],
                )
                if origin_key not in state["searchOriginKeys"]:
                    state["searchOriginKeys"].add(origin_key)
                    state["searchOrigins"].append(search_origin)
        if not state["exampleRaw"]:
            raws = [str(value) for value in edge.get("rawExamples") or [] if str(value)]
            if raws:
                state["exampleRaw"] = raws[0]
        for artifact_locator_value in edge.get("targetArtifactLocators") or []:
            state["artifactLocators"].add(str(artifact_locator_value))
        for content_locator_value in edge.get("targetContentLocators") or []:
            state["contentLocators"].add(str(content_locator_value))
        for snippet in edge.get("contexts") or []:
            value = str(snippet).strip()
            if value and value not in state["contexts"] and len(state["contexts"]) < 3:
                state["contexts"].append(value)
        state["visibility"] = best_visibility_metadata(
            state.get("visibility", {}),
            edge,
        )
    rendered: list[dict[str, object]] = []
    for locator, state in aggregated.items():
        relation_kinds = {str(value) for value in state["relationKinds"]}
        artifact_count = len(state["sourceChecksums"])
        search_like_source_count = len(state["searchLikeChecksums"])
        search_origins = sorted(
            [origin for origin in state["searchOrigins"] if isinstance(origin, dict)],
            key=lambda item: (
                int(item.get("rank") or 0)
                if int(item.get("rank") or 0) > 0
                else 1_000_000,
                str(item.get("provider") or "").casefold(),
                str(item.get("subcommand") or "").casefold(),
                str(item.get("artifactLocator") or "").casefold(),
            ),
        )
        rendered.append(
            {
                "locator": locator,
                "provider": str(state["provider"]),
                "kind": str(state["kind"]),
                "followCommand": str(state["followCommand"]),
                "firstParty": bool(state["firstParty"]),
                "searchSeed": bool(state["searchSeed"]),
                "materialized": bool(state["materialized"]),
                "occurrenceCount": int(state["occurrenceCount"]),
                "artifactCount": artifact_count,
                "relationKinds": sorted(relation_kinds),
                "artifactLocators": sorted(
                    str(value) for value in state["artifactLocators"]
                ),
                "contentLocators": sorted(
                    str(value) for value in state["contentLocators"]
                ),
                "exampleRaw": str(state["exampleRaw"]),
                "contexts": list(state["contexts"]),
                "bestSearchRank": int(search_origins[0].get("rank") or 0)
                if search_origins
                else 0,
                "searchLikeSourceCount": search_like_source_count,
                "searchOrigins": search_origins[:5],
                **best_visibility_metadata(state.get("visibility", {})),
            }
        )
    return sorted(rendered, key=lead_source_best_first_sort_key)
