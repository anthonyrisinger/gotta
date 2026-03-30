"""Overview payload builders for session analysis."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

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


@dataclass(slots=True)
class _OverviewState:
    lineage: LineagePayload
    semantic: SemanticPayload
    node_groups: Counter[str]
    node_kinds: Counter[str]
    edge_labels: Counter[str]
    materialized_anchors: list[LineageContent]
    query_seeds: list[SemanticNode]
    best_leads: list[LeadSourceSummary]
    structural_edge_count: int
    source_node_count: int

    @classmethod
    def from_payloads(
        cls,
        lineage: LineagePayload,
        semantic: SemanticPayload,
    ) -> _OverviewState:
        semantic_nodes = list(semantic.get("nodes") or [])
        semantic_edges = list(semantic.get("edges") or [])
        node_groups = Counter(
            str(node.get("group") or "")
            for node in semantic_nodes
            if str(node.get("group") or "")
        )
        node_kinds = Counter(
            str(node.get("kind") or "")
            for node in semantic_nodes
            if str(node.get("kind") or "")
        )
        edge_labels = Counter(
            str(edge.get("label") or "")
            for edge in semantic_edges
            if str(edge.get("label") or "")
        )
        materialized_anchors = sorted(
            list(lineage.get("content") or []),
            key=lambda item: (
                1 if str(item.get("artifactKind") or "") == "evidence" else 0,
                _int(item.get("fetchCount")),
                str(item.get("lastFetchedAt") or ""),
                str(item.get("preferredName") or ""),
            ),
            reverse=True,
        )
        query_seeds = [
            node for node in semantic_nodes if str(node.get("kind") or "") == "query"
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
        return cls(
            lineage=lineage,
            semantic=semantic,
            node_groups=node_groups,
            node_kinds=node_kinds,
            edge_labels=edge_labels,
            materialized_anchors=materialized_anchors,
            query_seeds=query_seeds,
            best_leads=list(lineage.get("leadSources") or []),
            structural_edge_count=structural_edge_count,
            source_node_count=source_node_count,
        )

    def render(self, *, limit: int) -> AnalysisOverviewPayload:
        bounded_limit = max(limit, 0)
        provider_clusters: list[ProviderCluster] = [
            {"provider": provider, "nodeCount": count}
            for provider, count in self.node_groups.most_common(bounded_limit)
            if provider != "content"
        ]
        dominant_kinds: list[DominantKind] = [
            {"kind": kind, "nodeCount": count}
            for kind, count in self.node_kinds.most_common(bounded_limit)
        ]
        dominant_relations: list[DominantRelation] = [
            {"label": label, "edgeCount": count}
            for label, count in self.edge_labels.most_common(bounded_limit)
        ]
        semantic_node_count = _int(self.semantic.get("nodeCount"))
        semantic_edge_count = _int(self.semantic.get("edgeCount"))
        return {
            "sessionDir": str(self.lineage.get("sessionDir") or ""),
            "contentDir": str(self.lineage.get("contentDir") or ""),
            "manifestPath": str(self.lineage.get("manifestPath") or ""),
            "contentCount": _int(self.lineage.get("contentCount")),
            "sourceCount": _int(self.lineage.get("sourceCount")),
            "leadSourceCount": _int(self.lineage.get("leadSourceCount")),
            "leadEdgeCount": _int(self.lineage.get("leadEdgeCount")),
            "semanticNodeCount": semantic_node_count,
            "semanticEdgeCount": semantic_edge_count,
            "discoveryArtifactCount": _int(self.lineage.get("discoveryArtifactCount")),
            "evidenceArtifactCount": _int(self.lineage.get("evidenceArtifactCount")),
            "nextStep": str(
                self.lineage.get("nextStep") or self.semantic.get("nextStep") or ""
            ),
            "providerClusters": provider_clusters,
            "dominantKinds": dominant_kinds,
            "dominantRelations": dominant_relations,
            "materializedAnchors": self.materialized_anchors[:bounded_limit],
            "querySeeds": self.query_seeds[:bounded_limit],
            "bestLeads": self.best_leads[:bounded_limit],
            "sourceHeavy": self.source_node_count * 2 >= max(semantic_node_count, 1),
            "structuralHeavy": self.structural_edge_count * 2
            >= max(semantic_edge_count, 1),
            "sourceNodeCount": self.source_node_count,
            "structuralEdgeCount": self.structural_edge_count,
        }


def analysis_overview_payload(
    lineage: LineagePayload,
    semantic: SemanticPayload,
    *,
    limit: int,
) -> AnalysisOverviewPayload:
    return _OverviewState.from_payloads(lineage, semantic).render(limit=limit)


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
