"""Overview payload builders for session analysis."""

from __future__ import annotations

from collections import Counter

from .model import (
    AnalysisOverviewPayload,
    CombinedAnalysisPayload,
    LineagePayload,
    SemanticPayload,
)


def analysis_overview_payload(
    lineage: LineagePayload,
    semantic: SemanticPayload,
    *,
    limit: int,
) -> AnalysisOverviewPayload:
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
    lineage: LineagePayload,
    semantic: SemanticPayload,
) -> CombinedAnalysisPayload:
    return {
        "mode": "all",
        "focus": focus,
        "lineage": lineage,
        "semantic": semantic,
    }
