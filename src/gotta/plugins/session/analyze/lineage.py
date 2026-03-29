"""Lineage payload builders for session analysis."""

from __future__ import annotations

from collections import Counter
from typing import Any

from gotta.content.filesystem import FileSystemLedgerStore
from gotta.content.model import ContentSnapshot
from gotta.content.path import content_locator
from gotta.lead.aggregate import aggregate_lead_sources
from gotta.lead.edge import build_lead_edge_records
from gotta.lead.snapshot import (
    snapshot_artifact_locator,
    snapshot_display_name,
    snapshot_locator,
)
from gotta.source.visibility import best_visibility_metadata

from ..core import (
    artifact_kind,
    follow_command,
    lead_kind,
    provider_name,
    rendered_actor,
    render_variant,
    render_variant_label,
    resolved_visibility_metadata,
    resource_label,
    session_read_command,
    topology_next_step,
)
from ..manifest.record import manifest_entries
from .focus import focus_match_threshold, ordered_focus_scan_entries


def _revision_edges(snapshots: list[ContentSnapshot]) -> list[dict[str, str]]:
    tracks: dict[tuple[str, tuple[str, str]], list[dict[str, str]]] = {}
    for snapshot in snapshots:
        metadata = snapshot.artifact.metadata
        canonical = str(
            metadata.get("canonical_locator", "") or metadata.get("locator", "")
        ).strip()
        if not canonical:
            continue
        variant = render_variant(snapshot)
        for event in snapshot.events:
            tracks.setdefault((canonical, variant), []).append(
                {
                    "timestamp": event.timestamp,
                    "digest": snapshot.digest,
                    "preferred_name": str(
                        metadata.get("preferred_name", "") or event.alias_name
                    ),
                    "plugin": str(metadata.get("plugin", "") or "unknown"),
                    "actor": rendered_actor(
                        metadata.get("actor"),
                        session_root=snapshot.layout.artifact_dir.parent.parent,
                    ),
                    "rendering": render_variant_label(variant),
                }
            )
    edges: list[dict[str, str]] = []
    for (locator, _variant), items in sorted(tracks.items()):
        prior_item: dict[str, str] | None = None
        for item in sorted(
            items, key=lambda current: (current["timestamp"], current["digest"])
        ):
            if prior_item is None:
                prior_item = item
                continue
            if item["digest"] == prior_item["digest"]:
                prior_item = item
                continue
            edges.append(
                {
                    "locator": locator,
                    "preferredName": item["preferred_name"]
                    or prior_item["preferred_name"],
                    "from": prior_item["digest"],
                    "to": item["digest"],
                    "fromTimestamp": prior_item["timestamp"],
                    "toTimestamp": item["timestamp"],
                    "plugin": item["plugin"] or prior_item["plugin"],
                    "actor": item["actor"] or prior_item["actor"],
                    "rendering": item["rendering"] or prior_item["rendering"],
                }
            )
            prior_item = item
    return edges


def lineage_payload(dirs, *, session_ref: str = "") -> dict[str, Any]:
    snapshots = FileSystemLedgerStore.for_content_dir(dirs.content_dir).scan_artifacts()
    snapshot_by_digest = {snapshot.digest: snapshot for snapshot in snapshots}
    entries: list[dict[str, Any]] = [dict(entry) for entry in manifest_entries(dirs)]
    source_map: dict[str, dict[str, Any]] = {}
    edge_plugins: dict[tuple[str, str], list[str]] = {}
    edge_actors: dict[tuple[str, str], set[str]] = {}
    content_details: dict[str, dict[str, set[str]]] = {}

    for entry in entries:
        source = str(
            entry.get("canonical_locator") or entry.get("locator") or "unknown"
        )
        checksum = str(entry.get("checksum") or "")
        if not checksum:
            continue
        source_state = source_map.setdefault(
            source,
            {
                "content": set(),
                "locators": set(),
                "plugins": set(),
                "actors": set(),
                "artifact_kinds": set(),
                "entries": 0,
                "variants": set(),
                "visibility": {},
            },
        )
        source_state["content"].add(checksum)
        locator = str(entry.get("locator") or source)
        source_state["locators"].add(locator)
        plugin = str(entry.get("plugin") or "unknown")
        actor = rendered_actor(entry.get("actor"), session_root=dirs.session_dir)
        source_state["plugins"].add(plugin)
        source_state["actors"].add(actor)
        kind = artifact_kind(entry.get("artifact_kind"))
        if kind:
            source_state["artifact_kinds"].add(kind)
        source_state["entries"] = int(source_state["entries"]) + 1
        source_state["visibility"] = best_visibility_metadata(
            source_state.get("visibility", {}),
            resolved_visibility_metadata(
                entry,
                provider=str(plugin),
                plugin=str(plugin),
                subcommand=str(entry.get("subcommand") or ""),
                locator=str(source),
            ),
        )
        snapshot = snapshot_by_digest.get(str(checksum))
        if snapshot is not None:
            source_state["variants"].add(render_variant(snapshot))
        edge_plugins.setdefault((source, checksum), []).append(plugin)
        edge_actors.setdefault((source, checksum), set()).add(actor)
        detail = content_details.setdefault(
            checksum,
            {
                "providers": set(),
                "actors": set(),
                "resource_hints": set(),
            },
        )
        detail["providers"].add(
            provider_name(source, plugins=[plugin], fallback=plugin)
        )
        detail["actors"].add(actor)
        source_kind, source_label = resource_label(source)
        if source_kind and source_label:
            detail["resource_hints"].add(f"{source_kind}:{source_label}")
        else:
            detail["resource_hints"].add(source)

    name_counts = Counter(snapshot_display_name(snapshot) for snapshot in snapshots)

    content: list[dict[str, Any]] = []
    for snapshot in snapshots:
        metadata = snapshot.artifact.metadata
        content.append(
            {
                "checksum": snapshot.digest,
                "preferredName": snapshot_display_name(snapshot),
                "artifactKind": artifact_kind(metadata.get("artifact_kind")),
                "contentLocator": content_locator(snapshot.digest),
                "artifactLocator": snapshot_artifact_locator(snapshot),
                "followCommand": session_read_command(
                    snapshot_artifact_locator(snapshot),
                    session_ref=session_ref,
                ),
                "nameCollision": name_counts[snapshot_display_name(snapshot)] > 1,
                "nameCount": len(snapshot.aliases),
                "fetchCount": len(snapshot.events),
                "names": [alias.name for alias in snapshot.aliases],
                "firstFetchedAt": snapshot.events[0].timestamp
                if snapshot.events
                else "",
                "lastFetchedAt": snapshot.events[-1].timestamp
                if snapshot.events
                else "",
                "providers": sorted(
                    content_details.get(snapshot.digest, {}).get("providers", set())
                ),
                "actors": sorted(
                    content_details.get(snapshot.digest, {}).get("actors", set())
                ),
                "resourceHints": sorted(
                    content_details.get(snapshot.digest, {}).get(
                        "resource_hints", set()
                    )
                ),
                **resolved_visibility_metadata(
                    dict(metadata),
                    provider=str(metadata.get("plugin") or ""),
                    plugin=str(metadata.get("plugin") or ""),
                    subcommand=str(metadata.get("subcommand") or ""),
                    locator=str(snapshot_locator(snapshot)),
                ),
            }
        )
    sources = [
        {
            "locator": locator,
            "contentCount": len(state["content"]),
            "entryCount": int(state["entries"]),
            "artifactKind": (
                next(iter(state["artifact_kinds"]))
                if len(state["artifact_kinds"]) == 1
                else ""
            ),
            "artifactKinds": sorted(str(value) for value in state["artifact_kinds"]),
            "plugins": sorted(str(value) for value in state["plugins"]),
            "actors": sorted(str(value) for value in state["actors"]),
            "locators": sorted(str(value) for value in state["locators"]),
            "collision": False,
            "duplicateMaterialization": len(state["content"]) > 1
            and len(state["variants"]) <= 1,
            "variant": len(state["variants"]) > 1,
            "variantCount": len(state["variants"]),
            "variants": [
                render_variant_label(variant) for variant in sorted(state["variants"])
            ],
            **best_visibility_metadata(state.get("visibility", {})),
        }
        for locator, state in sorted(source_map.items())
    ]
    source_edges = [
        {
            "source": source,
            "checksum": checksum,
            "plugins": sorted(plugins),
            "actors": sorted(edge_actors.get((source, checksum), set())),
            "count": len(plugins),
        }
        for (source, checksum), plugins in sorted(edge_plugins.items())
    ]
    revision_edges = _revision_edges(snapshots)
    lead_edges = build_lead_edge_records(
        snapshots,
        entries,
        classify_kind=lead_kind,
    )
    lead_sources = aggregate_lead_sources(lead_edges)
    for lead in lead_sources:
        locator = str(lead.get("locator") or "").strip()
        if locator:
            lead["followCommand"] = follow_command(locator, session_ref=session_ref)
    collisions = [source["locator"] for source in sources if source["collision"]]
    duplicate_materializations = [
        source["locator"]
        for source in sources
        if source.get("duplicateMaterialization")
    ]
    variants = [source["locator"] for source in sources if source.get("variant")]
    name_collisions = sorted(
        name for name, count in name_counts.items() if count > 1 and name
    )
    materialized_lead_count = sum(
        1 for source in lead_sources if bool(source["materialized"])
    )
    empty = (
        not sources
        and not content
        and not source_edges
        and not revision_edges
        and not lead_edges
    )
    discovery_count = sum(
        1 for item in content if item.get("artifactKind") == "discovery"
    )
    evidence_count = sum(
        1 for item in content if item.get("artifactKind") == "evidence"
    )
    return {
        "sessionDir": str(dirs.session_dir),
        "contentDir": str(dirs.content_dir),
        "manifestPath": str(dirs.content_dir / "manifest.jsonl"),
        "manifestEntryCount": len(entries),
        "contentCount": len(content),
        "sourceCount": len(sources),
        "sourceEdgeCount": len(source_edges),
        "revisionEdgeCount": len(revision_edges),
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "collisionCount": len(collisions),
        "collisions": collisions,
        "duplicateMaterializationCount": len(duplicate_materializations),
        "duplicateMaterializations": duplicate_materializations,
        "variantCount": len(variants),
        "variants": variants,
        "nameCollisionCount": len(name_collisions),
        "nameCollisions": name_collisions,
        "leadSourceCount": len(lead_sources),
        "materializedLeadSourceCount": materialized_lead_count,
        "unmaterializedLeadSourceCount": len(lead_sources) - materialized_lead_count,
        "leadEdgeCount": len(lead_edges),
        "empty": empty,
        "nextStep": topology_next_step(
            discovery_count=discovery_count,
            evidence_count=evidence_count,
        ),
        "sources": sources,
        "content": content,
        "sourceEdges": source_edges,
        "revisionEdges": revision_edges,
        "leadSources": lead_sources,
        "leadEdges": lead_edges,
    }


def _lineage_source_candidate(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "source",
        "label": str(item.get("locator") or ""),
        "locator": str(item.get("locator") or ""),
        "artifactKind": str(item.get("artifactKind") or ""),
        "materialized": True,
        "followCommand": str(item.get("followCommand") or ""),
    }


def _lineage_content_candidate(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "content",
        "label": str(item.get("preferredName") or ""),
        "checksum": str(item.get("checksum") or ""),
        "artifactLocator": str(item.get("artifactLocator") or ""),
        "contentLocator": str(item.get("contentLocator") or ""),
        "artifactKind": str(item.get("artifactKind") or ""),
        "materialized": True,
        "followCommand": str(item.get("followCommand") or ""),
    }


def _lineage_lead_candidate(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "lead",
        "label": str(item.get("locator") or ""),
        "locator": str(item.get("locator") or ""),
        "artifactKind": str(item.get("artifactKind") or ""),
        "materialized": bool(item.get("materialized")),
        "followCommand": str(item.get("followCommand") or ""),
    }


def _lineage_focus_score(item: dict[str, Any], query: str) -> tuple[int, int, str]:
    query_lower = query.lower()
    candidates = [
        str(item.get("label") or ""),
        str(item.get("locator") or ""),
        str(item.get("artifactLocator") or ""),
        str(item.get("contentLocator") or ""),
        str(item.get("checksum") or ""),
    ]
    lowered = [candidate.lower() for candidate in candidates if candidate]
    score = 0
    if any(candidate == query_lower for candidate in lowered):
        score = 5
    elif any(candidate.startswith(query_lower) for candidate in lowered):
        score = 4
    elif any(f":{query_lower}" in candidate for candidate in lowered):
        score = 3
    elif any(query_lower in candidate for candidate in lowered):
        score = 2
    materialized = 1 if bool(item.get("materialized")) else 0
    label = str(item.get("label") or item.get("locator") or "")
    return (score, materialized, label.lower())


def _empty_lineage_focus_payload(
    payload: dict[str, Any],
    *,
    query: str,
    next_step: str,
) -> dict[str, Any]:
    return {
        "sessionDir": payload["sessionDir"],
        "contentDir": payload["contentDir"],
        "focus": query,
        "matched": False,
        "empty": True,
        "nextStep": next_step,
        "root": {},
        "neighbors": [],
        "sources": [],
        "content": [],
        "sourceEdges": [],
        "revisionEdges": [],
        "leadSources": [],
        "leadEdges": [],
        "discoveryArtifactCount": 0,
        "evidenceArtifactCount": 0,
        "anchors": [],
        "matchedCount": 0,
    }


def lineage_focus_payload(
    payload: dict[str, Any],
    *,
    focus: str,
    limit: int,
    scan_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    query = focus.strip()
    if not query:
        return _empty_lineage_focus_payload(
            payload,
            query="",
            next_step="Provide a focus keyword, locator, artifact name, or checksum prefix.",
        )
    sources = [dict(item) for item in payload.get("sources") or []]
    content_items = [dict(item) for item in payload.get("content") or []]
    lead_sources = [dict(item) for item in payload.get("leadSources") or []]
    source_index = {str(item.get("locator") or ""): item for item in sources}
    content_index = {str(item.get("checksum") or ""): item for item in content_items}
    lead_index = {str(item.get("locator") or ""): item for item in lead_sources}
    candidates = [
        *(_lineage_source_candidate(item) for item in sources),
        *(_lineage_content_candidate(item) for item in content_items),
        *(
            _lineage_lead_candidate(item)
            for item in lead_sources
            if str(item.get("locator") or "") not in source_index
        ),
    ]
    matches = sorted(
        (
            candidate
            for candidate in candidates
            if _lineage_focus_score(candidate, query)[0] > 0
        ),
        key=lambda candidate: _lineage_focus_score(candidate, query),
        reverse=True,
    )
    scan_entries = ordered_focus_scan_entries(
        scan_payload,
        limit=max(limit * 2, 8),
    )
    seed_cap = max(4, min(max(limit, 1), 12))
    no_match_step = (
        f"No analyzed lineage anchor or projected artifact matched `{query}`. Try a canonical locator, "
        "artifact name, checksum prefix, or a tighter target from session scan, leads, or manifest."
    )
    if not matches and not scan_entries:
        return _empty_lineage_focus_payload(
            payload,
            query=query,
            next_step=no_match_step,
        )
    best_score = _lineage_focus_score(matches[0], query)[0] if matches else 0
    threshold = focus_match_threshold(best_score)
    seeds: list[dict[str, Any]] = []
    seen_seed_keys: set[tuple[str, str]] = set()

    def add_seed(candidate: dict[str, Any]) -> None:
        kind = str(candidate.get("kind") or "")
        if kind == "source":
            key = ("source", str(candidate.get("locator") or ""))
        elif kind == "content":
            key = ("content", str(candidate.get("checksum") or ""))
        else:
            key = ("lead", str(candidate.get("locator") or ""))
        if not key[1] or key in seen_seed_keys:
            return
        seen_seed_keys.add(key)
        seeds.append(candidate)

    for candidate in matches:
        if _lineage_focus_score(candidate, query)[0] < threshold:
            break
        add_seed(dict(candidate))
        if len(seeds) >= seed_cap:
            break
    for entry in scan_entries:
        checksum = str(entry.get("checksum") or "").strip()
        locator = str(
            entry.get("canonical_locator") or entry.get("locator") or ""
        ).strip()
        if checksum and checksum in content_index:
            add_seed(_lineage_content_candidate(content_index[checksum]))
        if locator and locator in source_index:
            add_seed(_lineage_source_candidate(source_index[locator]))
        if len(seeds) >= seed_cap:
            break

    if not seeds:
        return _empty_lineage_focus_payload(
            payload,
            query=query,
            next_step=no_match_step,
        )

    root = dict(seeds[0])
    selected_sources: set[str] = set()
    selected_content: set[str] = set()
    selected_leads: set[str] = set()
    matched_sources = {
        str(candidate.get("locator") or "")
        for candidate in matches
        if str(candidate.get("kind") or "") == "source"
    }
    matched_leads = {
        str(candidate.get("locator") or "")
        for candidate in matches
        if str(candidate.get("kind") or "") == "lead"
    }
    for candidate in seeds:
        if candidate["kind"] == "source":
            selected_sources.add(str(candidate.get("locator") or ""))
        elif candidate["kind"] == "content":
            selected_content.add(str(candidate.get("checksum") or ""))
        else:
            selected_leads.add(str(candidate.get("locator") or ""))

    def expand_source_and_revision_edges() -> None:
        for edge in payload.get("sourceEdges") or []:
            source = str(edge.get("source") or "")
            checksum = str(edge.get("checksum") or "")
            if source in selected_sources:
                selected_content.add(checksum)
            if checksum in selected_content:
                selected_sources.add(source)
        for edge in payload.get("revisionEdges") or []:
            from_checksum = str(edge.get("from") or "")
            to_checksum = str(edge.get("to") or "")
            if from_checksum in selected_content:
                selected_content.add(to_checksum)
            if to_checksum in selected_content:
                selected_content.add(from_checksum)

    expand_source_and_revision_edges()
    for edge in payload.get("leadEdges") or []:
        source_checksum = str(edge.get("sourceChecksum") or "")
        target_locator = str(edge.get("targetLocator") or "")
        target_is_source = target_locator in source_index
        target_matches_focus = (
            target_locator in matched_sources or target_locator in matched_leads
        )
        if source_checksum in selected_content and (
            target_locator in selected_sources
            or target_locator in selected_leads
            or target_matches_focus
        ):
            if target_is_source:
                selected_sources.add(target_locator)
            else:
                selected_leads.add(target_locator)
        if target_locator in selected_sources or target_locator in selected_leads:
            selected_content.add(source_checksum)
    expand_source_and_revision_edges()

    neighbor_candidates: list[dict[str, Any]] = []
    for locator in sorted(selected_sources):
        if ("source", locator) in seen_seed_keys:
            continue
        source_item = source_index.get(locator)
        if source_item is None:
            continue
        neighbor_candidates.append(
            {
                "kind": "source",
                "label": locator,
                "relation": "materialized source",
                "followCommand": str(source_item.get("followCommand") or ""),
                "artifactKind": str(source_item.get("artifactKind") or ""),
                "materialized": True,
            }
        )
    for checksum in sorted(selected_content):
        if ("content", checksum) in seen_seed_keys:
            continue
        content_item = content_index.get(checksum)
        if content_item is None:
            continue
        neighbor_candidates.append(
            {
                "kind": "content",
                "label": str(content_item.get("preferredName") or checksum),
                "relation": "stored artifact",
                "followCommand": str(content_item.get("followCommand") or ""),
                "artifactKind": str(content_item.get("artifactKind") or ""),
                "materialized": True,
            }
        )
    for locator in sorted(selected_leads):
        if ("lead", locator) in seen_seed_keys:
            continue
        lead_item = lead_index.get(locator)
        if lead_item is None:
            continue
        neighbor_candidates.append(
            {
                "kind": "lead",
                "label": locator,
                "relation": "followable lead",
                "followCommand": str(lead_item.get("followCommand") or ""),
                "artifactKind": str(lead_item.get("artifactKind") or ""),
                "materialized": bool(lead_item.get("materialized")),
            }
        )
    ordered_neighbors = sorted(
        neighbor_candidates,
        key=lambda item: (
            1 if bool(item.get("materialized")) else 0,
            1 if str(item.get("kind") or "") == "content" else 0,
            str(item.get("label") or "").lower(),
        ),
        reverse=True,
    )[: max(limit, 0)]

    neighbor_source_labels = {
        str(item.get("label") or "")
        for item in ordered_neighbors
        if str(item.get("kind") or "") == "source"
    }
    neighbor_content_labels = {
        str(item.get("label") or "")
        for item in ordered_neighbors
        if str(item.get("kind") or "") == "content"
    }
    neighbor_content_checksums = {
        checksum
        for checksum, item in content_index.items()
        if str(item.get("preferredName") or checksum) in neighbor_content_labels
    }
    neighbor_lead_labels = {
        str(item.get("label") or "")
        for item in ordered_neighbors
        if str(item.get("kind") or "") == "lead"
    }
    seed_source_labels = {value for kind, value in seen_seed_keys if kind == "source"}
    seed_content_checksums = {
        value for kind, value in seen_seed_keys if kind == "content"
    }
    seed_lead_labels = {value for kind, value in seen_seed_keys if kind == "lead"}
    selected_sources = {
        locator
        for locator in selected_sources
        if locator in seed_source_labels or locator in neighbor_source_labels
    }
    selected_content = {
        checksum
        for checksum in selected_content
        if checksum in seed_content_checksums or checksum in neighbor_content_checksums
    }
    selected_leads = {
        locator
        for locator in selected_leads
        if locator in seed_lead_labels or locator in neighbor_lead_labels
    }

    selected_source_items = [
        item for item in sources if str(item.get("locator") or "") in selected_sources
    ]
    selected_content_items = [
        item
        for item in content_items
        if str(item.get("checksum") or "") in selected_content
    ]
    selected_lead_items = [
        item
        for item in lead_sources
        if str(item.get("locator") or "") in selected_leads
        and str(item.get("locator") or "") not in selected_sources
    ]
    selected_source_edges = [
        edge
        for edge in payload.get("sourceEdges") or []
        if str(edge.get("source") or "") in selected_sources
        and str(edge.get("checksum") or "") in selected_content
    ]
    selected_revision_edges = [
        edge
        for edge in payload.get("revisionEdges") or []
        if str(edge.get("from") or "") in selected_content
        and str(edge.get("to") or "") in selected_content
    ]
    selected_lead_edges = [
        edge
        for edge in payload.get("leadEdges") or []
        if str(edge.get("sourceChecksum") or "") in selected_content
        and str(edge.get("targetLocator") or "")
        in selected_leads.union(selected_sources)
    ]
    discovery_count = sum(
        1
        for item in selected_content_items
        if str(item.get("artifactKind") or "") == "discovery"
    )
    evidence_count = sum(
        1
        for item in selected_content_items
        if str(item.get("artifactKind") or "") == "evidence"
    )
    return {
        "sessionDir": payload["sessionDir"],
        "contentDir": payload["contentDir"],
        "manifestPath": payload["manifestPath"],
        "focus": query,
        "matched": True,
        "empty": False,
        "nextStep": "",
        "root": root,
        "anchors": seeds[1:],
        "matchedCount": len(seeds),
        "neighbors": ordered_neighbors,
        "sources": selected_source_items,
        "content": selected_content_items,
        "sourceEdges": selected_source_edges,
        "revisionEdges": selected_revision_edges,
        "leadSources": selected_lead_items,
        "leadEdges": selected_lead_edges,
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "contentCount": len(selected_content_items),
        "sourceCount": len(selected_source_items),
        "sourceEdgeCount": len(selected_source_edges),
        "revisionEdgeCount": len(selected_revision_edges),
        "leadSourceCount": len(selected_lead_items),
        "leadEdgeCount": len(selected_lead_edges),
        "collisionCount": 0,
        "collisions": [],
        "duplicateMaterializationCount": 0,
        "duplicateMaterializations": [],
        "variantCount": 0,
        "variants": [],
    }
