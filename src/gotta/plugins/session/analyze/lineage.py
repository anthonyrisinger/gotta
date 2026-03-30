"""Lineage payload builders for session analysis."""

from __future__ import annotations

from collections import Counter

from gotta.content.backend import scan_content_snapshots
from gotta.content.model import ContentSnapshot
from gotta.content.path import content_locator
from gotta.lead.aggregate import aggregate_lead_sources
from gotta.lead.edge import build_lead_edges
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
from .focus import select_lineage_focus
from .model import (
    AnalyzeScanPayload,
    LineageContent,
    LineageFocusPayload,
    LineagePayload,
    LineageRevisionEdge,
    LineageSource,
    LineageSourceEdge,
)


def _revision_edges(snapshots: list[ContentSnapshot]) -> list[LineageRevisionEdge]:
    tracks: dict[tuple[str, tuple[str, str]], list[LineageRevisionEdge]] = {}
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
    edges: list[LineageRevisionEdge] = []
    for (locator, _variant), items in sorted(tracks.items()):
        prior_item: LineageRevisionEdge | None = None
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


def lineage_payload(dirs, *, session_ref: str = "") -> LineagePayload:
    snapshots = scan_content_snapshots(
        dirs.content_dir,
        session_dir=dirs.session_dir,
    )
    snapshot_by_digest = {snapshot.digest: snapshot for snapshot in snapshots}
    entries = [dict(entry) for entry in manifest_entries(dirs)]
    source_map: dict[str, dict[str, object]] = {}
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

    content: list[LineageContent] = []
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
    sources: list[LineageSource] = [
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
    source_edges: list[LineageSourceEdge] = [
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
    lead_edges = build_lead_edges(
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


def _empty_lineage_focus_payload(
    payload: LineagePayload,
    *,
    query: str,
    next_step: str,
) -> LineageFocusPayload:
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
    payload: LineagePayload,
    *,
    focus: str,
    limit: int,
    scan_payload: AnalyzeScanPayload | None = None,
) -> LineageFocusPayload:
    query = focus.strip()
    if not query:
        return _empty_lineage_focus_payload(
            payload,
            query="",
            next_step="Provide a focus keyword, locator, artifact name, or checksum prefix.",
        )
    no_match_step = (
        f"No analyzed lineage anchor or projected artifact matched `{query}`. Try a canonical locator, "
        "artifact name, checksum prefix, or a tighter target from session scan, leads, or manifest."
    )
    selection = select_lineage_focus(
        payload,
        query=query,
        limit=limit,
        scan_payload=scan_payload,
    )
    if selection is None:
        return _empty_lineage_focus_payload(
            payload,
            query=query,
            next_step=no_match_step,
        )
    return {
        "sessionDir": payload["sessionDir"],
        "contentDir": payload["contentDir"],
        "manifestPath": payload["manifestPath"],
        "focus": query,
        "matched": True,
        "empty": False,
        "nextStep": "",
        "root": selection.root,
        "anchors": selection.anchors,
        "matchedCount": len(selection.anchors) + 1,
        "neighbors": selection.neighbors,
        "sources": selection.sources,
        "content": selection.content,
        "sourceEdges": selection.source_edges,
        "revisionEdges": selection.revision_edges,
        "leadSources": selection.lead_sources,
        "leadEdges": selection.lead_edges,
        "discoveryArtifactCount": selection.discovery_artifact_count,
        "evidenceArtifactCount": selection.evidence_artifact_count,
        "contentCount": len(selection.content),
        "sourceCount": len(selection.sources),
        "sourceEdgeCount": len(selection.source_edges),
        "revisionEdgeCount": len(selection.revision_edges),
        "leadSourceCount": len(selection.lead_sources),
        "leadEdgeCount": len(selection.lead_edges),
        "collisionCount": 0,
        "collisions": [],
        "duplicateMaterializationCount": 0,
        "duplicateMaterializations": [],
        "variantCount": 0,
        "variants": [],
    }
