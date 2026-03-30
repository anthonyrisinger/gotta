"""Semantic payload builders for session analysis."""

from __future__ import annotations

from ..core import provider_name, query_label, resource_label, topology_next_step
from .focus import select_semantic_focus
from .model import (
    AnalyzeScanPayload,
    LineagePayload,
    SemanticEdge,
    SemanticFocusPayload,
    SemanticNode,
    SemanticPayload,
)


def semantic_payload(lineage: LineagePayload) -> SemanticPayload:
    nodes: dict[str, SemanticNode] = {}
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
    semantic_edges: list[SemanticEdge] = [
        {"source": source, "target": target, "label": label}
        for source, target, label in sorted(edges)
    ]
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
        "edges": semantic_edges,
    }


def _empty_semantic_focus_payload(
    semantic: SemanticPayload,
    *,
    query: str,
    next_step: str,
) -> SemanticFocusPayload:
    return {
        "sessionDir": semantic["sessionDir"],
        "contentDir": semantic["contentDir"],
        "focus": query,
        "matched": False,
        "empty": True,
        "nextStep": next_step,
        "nodeCount": 0,
        "edgeCount": 0,
        "nodes": [],
        "edges": [],
        "neighbors": [],
        "anchors": [],
        "matchedCount": 0,
    }


def semantic_focus_payload(
    lineage: LineagePayload,
    semantic: SemanticPayload,
    *,
    focus: str,
    limit: int,
    scan_payload: AnalyzeScanPayload | None = None,
) -> SemanticFocusPayload:
    query = focus.strip()
    if not query:
        return _empty_semantic_focus_payload(
            semantic,
            query="",
            next_step="Provide a focus keyword, locator, artifact name, or checksum prefix.",
        )
    no_match_step = (
        f"No analyzed node or projected artifact matched `{query}`. Try a canonical locator, "
        "artifact name, checksum prefix, or a tighter keyword from session scan, leads, or manifest."
    )
    selection = select_semantic_focus(
        lineage,
        semantic,
        query=query,
        limit=limit,
        scan_payload=scan_payload,
    )
    if selection is None:
        return _empty_semantic_focus_payload(
            semantic,
            query=query,
            next_step=no_match_step,
        )
    return {
        "sessionDir": semantic["sessionDir"],
        "contentDir": semantic["contentDir"],
        "focus": query,
        "matched": True,
        "empty": False,
        "nextStep": "",
        "nodeCount": len(selection.nodes),
        "edgeCount": len(selection.edges),
        "root": selection.root,
        "anchors": selection.anchors,
        "matchedCount": len(selection.anchors) + 1,
        "neighbors": selection.neighbors,
        "nodes": selection.nodes,
        "edges": selection.edges,
        "suppressedStructuralEdgeCount": selection.suppressed_structural_edge_count,
    }
