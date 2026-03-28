"""Overview payload builders for session analysis."""

from __future__ import annotations

from collections import Counter
from typing import Any

from gotta.content.model import ContentSnapshot
from gotta.content.path import content_locator
from gotta.content.store import scan_content_store
from gotta.lead.aggregate import aggregate_lead_sources
from gotta.lead.edge import build_lead_edge_records
from gotta.lead.snapshot import (
    snapshot_artifact_locator,
    snapshot_display_name,
    snapshot_locator,
)
from gotta.source import best_visibility_metadata

from ..core import (
    artifact_kind,
    follow_command,
    lead_kind,
    provider_name,
    query_label,
    rendered_actor,
    render_variant,
    render_variant_label,
    resolved_visibility_metadata,
    resource_label,
    session_read_command,
    topology_next_step,
)
from ..manifest import manifest_entries


def _revision_edges(snapshots: list[ContentSnapshot]) -> list[dict[str, str]]:
    tracks: dict[tuple[str, tuple[str, str]], list[dict[str, str]]] = {}
    for snapshot in snapshots:
        canonical = str(
            snapshot.metadata.get("canonical_locator", "")
            or snapshot.metadata.get("locator", "")
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
                        snapshot.metadata.get("preferred_name", "") or event.link_name
                    ),
                    "plugin": str(snapshot.metadata.get("plugin", "") or "unknown"),
                    "actor": rendered_actor(
                        snapshot.metadata.get("actor"),
                        session_root=snapshot.content_dir.parent.parent,
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


def analysis_payload(dirs, *, session_ref: str = "") -> dict[str, Any]:
    snapshots = scan_content_store(dirs.content_dir)
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

    content = [
        {
            "checksum": snapshot.digest,
            "preferredName": snapshot_display_name(snapshot),
            "artifactKind": artifact_kind(snapshot.metadata.get("artifact_kind")),
            "contentLocator": content_locator(snapshot.digest),
            "artifactLocator": snapshot_artifact_locator(snapshot),
            "followCommand": session_read_command(
                snapshot_artifact_locator(snapshot),
                session_ref=session_ref,
            ),
            "nameCollision": name_counts[snapshot_display_name(snapshot)] > 1,
            "nameCount": len(snapshot.names),
            "fetchCount": len(snapshot.events),
            "names": snapshot.names,
            "firstFetchedAt": snapshot.events[0].timestamp if snapshot.events else "",
            "lastFetchedAt": snapshot.events[-1].timestamp if snapshot.events else "",
            "providers": sorted(
                content_details.get(snapshot.digest, {}).get("providers", set())
            ),
            "actors": sorted(
                content_details.get(snapshot.digest, {}).get("actors", set())
            ),
            "resourceHints": sorted(
                content_details.get(snapshot.digest, {}).get("resource_hints", set())
            ),
            **resolved_visibility_metadata(
                dict(snapshot.metadata),
                provider=str(snapshot.metadata.get("plugin") or ""),
                plugin=str(snapshot.metadata.get("plugin") or ""),
                subcommand=str(snapshot.metadata.get("subcommand") or ""),
                locator=str(snapshot_locator(snapshot)),
            ),
        }
        for snapshot in snapshots
    ]
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


def semantic_payload(dirs, *, session_ref: str = "") -> dict[str, Any]:
    lineage = analysis_payload(dirs, session_ref=session_ref)
    nodes: dict[str, dict[str, Any]] = {}
    edges: set[tuple[str, str, str]] = set()

    def add_node(node_id: str, *, label: str, kind: str, group: str) -> None:
        nodes.setdefault(
            node_id,
            {
                "id": node_id,
                "label": label,
                "kind": kind,
                "group": group,
                "materialized": False,
                "discovered": False,
            },
        )

    def add_edge(source: str, target: str, label: str) -> None:
        edges.add((source, target, label))

    for source in lineage["sources"]:
        locator = str(source["locator"])
        provider = provider_name(
            locator,
            plugins=[str(value) for value in source.get("plugins") or [] if str(value)],
        )
        provider_id = f"provider:{provider}"
        source_id = f"source:{locator}"
        add_node(provider_id, label=provider, kind="provider", group=provider)
        add_node(source_id, label=locator, kind="source", group=provider)
        nodes[source_id]["materialized"] = True
        nodes[source_id]["artifactKind"] = str(source.get("artifactKind") or "")
        nodes[source_id]["artifactKinds"] = list(source.get("artifactKinds") or [])
        add_edge(provider_id, source_id, "source")

        query = query_label(locator)
        if query:
            query_id = f"query:{provider}:{query}"
            add_node(query_id, label=query, kind="query", group=provider)
            add_edge(provider_id, query_id, "query")
            add_edge(query_id, source_id, "drives")

        source_kind, source_label = resource_label(locator)
        if source_kind and source_label:
            resource_id = f"resource:{provider}:{source_label}"
            add_node(
                resource_id,
                label=source_label,
                kind=source_kind,
                group=provider,
            )
            add_edge(provider_id, resource_id, "resource")
            add_edge(resource_id, source_id, "resolved_by")

    for item in lineage["content"]:
        checksum = str(item["checksum"])
        content_id = f"content:{checksum}"
        add_node(
            content_id,
            label=str(item["preferredName"]),
            kind="content",
            group="content",
        )
        nodes[content_id]["materialized"] = True
        nodes[content_id]["artifactKind"] = str(item.get("artifactKind") or "")

    for edge in lineage["sourceEdges"]:
        source_id = f"source:{edge['source']}"
        content_id = f"content:{edge['checksum']}"
        add_edge(
            source_id, content_id, ",".join(str(value) for value in edge["plugins"])
        )

    for edge in lineage["revisionEdges"]:
        from_id = f"content:{edge['from']}"
        to_id = f"content:{edge['to']}"
        add_edge(from_id, to_id, f"revision:{edge['locator']}")

    for lead_source in lineage.get("leadSources") or []:
        locator = str(lead_source["locator"])
        provider = str(lead_source["provider"] or provider_name(locator))
        provider_id = f"provider:{provider}"
        source_id = f"source:{locator}"
        add_node(provider_id, label=provider, kind="provider", group=provider)
        add_node(source_id, label=locator, kind="source", group=provider)
        nodes[source_id]["materialized"] = bool(
            nodes[source_id].get("materialized") or lead_source.get("materialized")
        )
        nodes[source_id]["discovered"] = True
        if not nodes[source_id].get("artifactKinds"):
            nodes[source_id]["artifactKinds"] = []
        kinds = {
            str(value)
            for value in nodes[source_id].get("artifactKinds") or []
            if str(value)
        }
        source_kind = str(lead_source.get("artifactKind") or "")
        if source_kind:
            kinds.add(source_kind)
        nodes[source_id]["artifactKinds"] = sorted(kinds)
        nodes[source_id]["artifactKind"] = (
            nodes[source_id]["artifactKinds"][0]
            if len(nodes[source_id]["artifactKinds"]) == 1
            else ""
        )
        add_edge(provider_id, source_id, "source")
        resource_kind_name, resource_name = resource_label(locator)
        if resource_kind_name and resource_name:
            resource_id = f"resource:{provider}:{resource_name}"
            add_node(
                resource_id,
                label=resource_name,
                kind=resource_kind_name,
                group=provider,
            )
            add_edge(provider_id, resource_id, "resource")
            add_edge(resource_id, source_id, "resolved_by")

    for edge in lineage.get("leadEdges") or []:
        from_id = f"content:{edge['sourceChecksum']}"
        to_id = f"source:{edge['targetLocator']}"
        relation = str(edge.get("relation") or "links_to")
        count = int(edge.get("occurrenceCount") or 0)
        add_edge(from_id, to_id, relation if count <= 1 else f"{relation} x{count}")

    empty = not nodes and not edges
    discovery_count = int(lineage.get("discoveryArtifactCount") or 0)
    evidence_count = int(lineage.get("evidenceArtifactCount") or 0)
    return {
        "sessionDir": lineage["sessionDir"],
        "contentDir": lineage["contentDir"],
        "manifestPath": lineage["manifestPath"],
        "nodeCount": len(nodes),
        "edgeCount": len(edges),
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "empty": empty,
        "nextStep": topology_next_step(
            discovery_count=discovery_count,
            evidence_count=evidence_count,
        ),
        "nodes": sorted(nodes.values(), key=lambda item: (item["kind"], item["label"])),
        "edges": [
            {"source": source, "target": target, "label": label}
            for source, target, label in sorted(edges)
        ],
    }


def analysis_overview_payload(
    lineage: dict[str, Any],
    semantic: dict[str, Any],
    *,
    limit: int,
) -> dict[str, Any]:
    node_groups = Counter(
        str(node.get("group") or "")
        for node in semantic.get("nodes") or []
        if str(node.get("group") or "")
    )
    node_kinds = Counter(
        str(node.get("kind") or "")
        for node in semantic.get("nodes") or []
        if str(node.get("kind") or "")
    )
    edge_labels = Counter(
        str(edge.get("label") or "")
        for edge in semantic.get("edges") or []
        if str(edge.get("label") or "")
    )
    provider_clusters = [
        {"provider": provider, "nodeCount": count}
        for provider, count in node_groups.most_common(max(limit, 0))
        if provider != "content"
    ]
    dominant_kinds = [
        {"kind": kind, "nodeCount": count}
        for kind, count in node_kinds.most_common(max(limit, 0))
    ]
    dominant_relations = [
        {"label": label, "edgeCount": count}
        for label, count in edge_labels.most_common(max(limit, 0))
    ]
    structural_edge_count = sum(
        count
        for label, count in edge_labels.items()
        if label in {"source", "resource", "resolved_by", "query", "drives"}
    )
    source_node_count = sum(
        count
        for kind, count in node_kinds.items()
        if kind in {"source", "query", "provider"}
    )
    anchors = sorted(
        [dict(item) for item in lineage.get("content") or []],
        key=lambda item: (
            1 if str(item.get("artifactKind") or "") == "evidence" else 0,
            int(item.get("fetchCount") or 0),
            str(item.get("lastFetchedAt") or ""),
            str(item.get("preferredName") or ""),
        ),
        reverse=True,
    )[: max(limit, 0)]
    queries = [
        dict(node)
        for node in semantic.get("nodes") or []
        if str(node.get("kind") or "") == "query"
    ][: max(limit, 0)]
    lead_sources = [
        dict(item) for item in (lineage.get("leadSources") or [])[: max(limit, 0)]
    ]
    return {
        "sessionDir": lineage["sessionDir"],
        "contentDir": lineage["contentDir"],
        "manifestPath": lineage["manifestPath"],
        "contentCount": int(lineage.get("contentCount") or 0),
        "sourceCount": int(lineage.get("sourceCount") or 0),
        "leadSourceCount": int(lineage.get("leadSourceCount") or 0),
        "leadEdgeCount": int(lineage.get("leadEdgeCount") or 0),
        "semanticNodeCount": int(semantic.get("nodeCount") or 0),
        "semanticEdgeCount": int(semantic.get("edgeCount") or 0),
        "discoveryArtifactCount": int(lineage.get("discoveryArtifactCount") or 0),
        "evidenceArtifactCount": int(lineage.get("evidenceArtifactCount") or 0),
        "nextStep": str(lineage.get("nextStep") or semantic.get("nextStep") or ""),
        "providerClusters": provider_clusters,
        "dominantKinds": dominant_kinds,
        "dominantRelations": dominant_relations,
        "materializedAnchors": anchors,
        "querySeeds": queries,
        "bestLeads": lead_sources,
        "sourceHeavy": source_node_count * 2
        >= max(int(semantic.get("nodeCount") or 0), 1),
        "structuralHeavy": structural_edge_count * 2
        >= max(int(semantic.get("edgeCount") or 0), 1),
        "sourceNodeCount": source_node_count,
        "structuralEdgeCount": structural_edge_count,
    }


def combined_analysis_payload(
    *,
    focus: str,
    lineage: dict[str, Any],
    semantic: dict[str, Any],
) -> dict[str, Any]:
    return {
        "mode": "all",
        "focus": focus,
        "lineage": lineage,
        "semantic": semantic,
    }
