"""Overview payload builders for session analysis."""

from __future__ import annotations

from collections import Counter

from .model import (
    AnalysisOverviewPayload,
    CombinedAnalysisPayload,
    DominantKind,
    DominantRelation,
    LeadSourceSummary,
    LineageContent,
    LineageFocusPayload,
    LineagePayload,
    ProviderCluster,
    SemanticFocusPayload,
    SemanticNode,
    SemanticPayload,
)


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


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
    provider_clusters: list[ProviderCluster] = [
        {"provider": provider, "nodeCount": count}
        for provider, count in node_groups.most_common(max(limit, 0))
        if provider != "content"
    ]
    dominant_kinds: list[DominantKind] = [
        {"kind": kind, "nodeCount": count}
        for kind, count in node_kinds.most_common(max(limit, 0))
    ]
    dominant_relations: list[DominantRelation] = [
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
        [item for item in lineage.get("content") or []],
        key=lambda item: (
            1 if str(item.get("artifactKind") or "") == "evidence" else 0,
            _int(item.get("fetchCount")),
            str(item.get("lastFetchedAt") or ""),
            str(item.get("preferredName") or ""),
        ),
        reverse=True,
    )[: max(limit, 0)]
    materialized_anchors: list[LineageContent] = anchors
    queries: list[SemanticNode] = [
        node
        for node in semantic.get("nodes") or []
        if str(node.get("kind") or "") == "query"
    ][: max(limit, 0)]
    lead_sources: list[LeadSourceSummary] = [
        item for item in (lineage.get("leadSources") or [])[: max(limit, 0)]
    ]
    return {
        "sessionDir": str(lineage.get("sessionDir") or ""),
        "contentDir": str(lineage.get("contentDir") or ""),
        "manifestPath": str(lineage.get("manifestPath") or ""),
        "contentCount": _int(lineage.get("contentCount")),
        "sourceCount": _int(lineage.get("sourceCount")),
        "leadSourceCount": _int(lineage.get("leadSourceCount")),
        "leadEdgeCount": _int(lineage.get("leadEdgeCount")),
        "semanticNodeCount": _int(semantic.get("nodeCount")),
        "semanticEdgeCount": _int(semantic.get("edgeCount")),
        "discoveryArtifactCount": _int(lineage.get("discoveryArtifactCount")),
        "evidenceArtifactCount": _int(lineage.get("evidenceArtifactCount")),
        "nextStep": str(lineage.get("nextStep") or semantic.get("nextStep") or ""),
        "providerClusters": provider_clusters,
        "dominantKinds": dominant_kinds,
        "dominantRelations": dominant_relations,
        "materializedAnchors": materialized_anchors,
        "querySeeds": queries,
        "bestLeads": lead_sources,
        "sourceHeavy": source_node_count * 2 >= max(_int(semantic.get("nodeCount")), 1),
        "structuralHeavy": structural_edge_count * 2
        >= max(_int(semantic.get("edgeCount")), 1),
        "sourceNodeCount": source_node_count,
        "structuralEdgeCount": structural_edge_count,
    }


def combined_analysis_payload(
    *,
    focus: str,
    lineage: LineagePayload | LineageFocusPayload,
    semantic: SemanticPayload | SemanticFocusPayload,
) -> CombinedAnalysisPayload:
    return {
        "mode": "all",
        "focus": focus,
        "lineage": lineage,
        "semantic": semantic,
    }
