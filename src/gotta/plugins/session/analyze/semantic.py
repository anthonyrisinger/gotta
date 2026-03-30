"""Semantic payload builders for session analysis."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core import provider_name, query_label, resource_label, topology_next_step
from .focus import select_semantic_focus
from .model import (
    AnalyzeScanPayload,
    LeadEdgeSummary,
    LeadSourceSummary,
    LineageContent,
    LineagePayload,
    LineageRevisionEdge,
    LineageSource,
    LineageSourceEdge,
    SemanticEdge,
    SemanticFocusPayload,
    SemanticNode,
    SemanticPayload,
)


@dataclass(slots=True)
class _SemanticNodeState:
    node_id: str
    label: str
    kind: str
    group: str
    materialized: bool = False
    discovered: bool = False
    artifact_kind: str = ""
    artifact_kinds: set[str] = field(default_factory=set)

    def record_artifact_kind(self, artifact_kind: str) -> None:
        if artifact_kind:
            self.artifact_kinds.add(artifact_kind)
        self.artifact_kind = (
            sorted(self.artifact_kinds)[0] if len(self.artifact_kinds) == 1 else ""
        )

    def render(self) -> SemanticNode:
        rendered: SemanticNode = {
            "id": self.node_id,
            "label": self.label,
            "kind": self.kind,
            "group": self.group,
            "materialized": self.materialized,
            "discovered": self.discovered,
            "artifactKind": self.artifact_kind,
            "artifactKinds": sorted(self.artifact_kinds),
        }
        return rendered


@dataclass(slots=True)
class _SemanticGraphState:
    nodes: dict[str, _SemanticNodeState] = field(default_factory=dict)
    edges: set[tuple[str, str, str]] = field(default_factory=set)

    def node(
        self, node_id: str, *, label: str, kind: str, group: str
    ) -> _SemanticNodeState:
        return self.nodes.setdefault(
            node_id,
            _SemanticNodeState(
                node_id=node_id,
                label=label,
                kind=kind,
                group=group,
            ),
        )

    def edge(self, source: str, target: str, label: str) -> None:
        self.edges.add((source, target, label))

    def add_lineage_source(self, source: LineageSource) -> None:
        locator = str(source.get("locator") or "")
        if not locator:
            return
        provider = provider_name(
            locator,
            plugins=[str(value) for value in source.get("plugins") or [] if str(value)],
        )
        provider_id = f"provider:{provider}"
        source_id = f"source:{locator}"
        self.node(provider_id, label=provider, kind="provider", group=provider)
        source_node = self.node(source_id, label=locator, kind="source", group=provider)
        source_node.materialized = True
        for artifact_kind in source.get("artifactKinds") or []:
            source_node.record_artifact_kind(str(artifact_kind))
        source_node.record_artifact_kind(str(source.get("artifactKind") or ""))
        self.edge(provider_id, source_id, "source")

        query = query_label(locator)
        if query:
            query_id = f"query:{provider}:{query}"
            self.node(query_id, label=query, kind="query", group=provider)
            self.edge(provider_id, query_id, "query")
            self.edge(query_id, source_id, "drives")

        resource_kind, resource_name = resource_label(locator)
        if resource_kind and resource_name:
            resource_id = f"resource:{provider}:{resource_name}"
            self.node(
                resource_id,
                label=resource_name,
                kind=resource_kind,
                group=provider,
            )
            self.edge(provider_id, resource_id, "resource")
            self.edge(resource_id, source_id, "resolved_by")

    def add_content(self, item: LineageContent) -> None:
        checksum = str(item.get("checksum") or "")
        if not checksum:
            return
        content_node = self.node(
            f"content:{checksum}",
            label=str(item.get("preferredName") or checksum),
            kind="content",
            group="content",
        )
        content_node.materialized = True
        content_node.record_artifact_kind(str(item.get("artifactKind") or ""))

    def add_source_edge(self, edge: LineageSourceEdge) -> None:
        source = str(edge.get("source") or "")
        checksum = str(edge.get("checksum") or "")
        if not source or not checksum:
            return
        label = ",".join(str(value) for value in edge.get("plugins") or [])
        self.edge(f"source:{source}", f"content:{checksum}", label)

    def add_revision_edge(self, edge: LineageRevisionEdge) -> None:
        prior = str(edge.get("from") or "")
        current = str(edge.get("to") or "")
        locator = str(edge.get("locator") or "")
        if not prior or not current:
            return
        self.edge(f"content:{prior}", f"content:{current}", f"revision:{locator}")

    def add_lead_source(self, lead_source: LeadSourceSummary) -> None:
        locator = str(lead_source.get("locator") or "")
        if not locator:
            return
        provider = str(lead_source.get("provider") or provider_name(locator))
        provider_id = f"provider:{provider}"
        source_id = f"source:{locator}"
        self.node(provider_id, label=provider, kind="provider", group=provider)
        source_node = self.node(source_id, label=locator, kind="source", group=provider)
        source_node.materialized = bool(
            source_node.materialized or lead_source.get("materialized")
        )
        source_node.discovered = True
        source_node.record_artifact_kind(str(lead_source.get("artifactKind") or ""))
        self.edge(provider_id, source_id, "source")

        resource_kind, resource_name = resource_label(locator)
        if resource_kind and resource_name:
            resource_id = f"resource:{provider}:{resource_name}"
            self.node(
                resource_id,
                label=resource_name,
                kind=resource_kind,
                group=provider,
            )
            self.edge(provider_id, resource_id, "resource")
            self.edge(resource_id, source_id, "resolved_by")

    def add_lead_edge(self, edge: LeadEdgeSummary) -> None:
        source_checksum = str(edge.get("sourceChecksum") or "")
        target_locator = str(edge.get("targetLocator") or "")
        if not source_checksum or not target_locator:
            return
        relation = str(edge.get("relation") or "links_to")
        count = int(edge.get("occurrenceCount") or 0)
        self.edge(
            f"content:{source_checksum}",
            f"source:{target_locator}",
            relation if count <= 1 else f"{relation} x{count}",
        )

    def render_nodes(self) -> list[SemanticNode]:
        return sorted(
            [state.render() for state in self.nodes.values()],
            key=lambda item: (
                str(item.get("kind") or ""),
                str(item.get("label") or ""),
            ),
        )

    def render_edges(self) -> list[SemanticEdge]:
        return [
            {"source": source, "target": target, "label": label}
            for source, target, label in sorted(self.edges)
        ]


def _payload_text(payload: LineagePayload | SemanticPayload, key: str) -> str:
    return str(payload.get(key) or "")


def _lineage_sources(payload: LineagePayload) -> list[LineageSource]:
    return list(payload.get("sources") or [])


def _lineage_content(payload: LineagePayload) -> list[LineageContent]:
    return list(payload.get("content") or [])


def _lineage_source_edges(payload: LineagePayload) -> list[LineageSourceEdge]:
    return list(payload.get("sourceEdges") or [])


def _lineage_revision_edges(payload: LineagePayload) -> list[LineageRevisionEdge]:
    return list(payload.get("revisionEdges") or [])


def _lineage_lead_sources(payload: LineagePayload) -> list[LeadSourceSummary]:
    return list(payload.get("leadSources") or [])


def _lineage_lead_edges(payload: LineagePayload) -> list[LeadEdgeSummary]:
    return list(payload.get("leadEdges") or [])


def semantic_payload(lineage: LineagePayload) -> SemanticPayload:
    graph = _SemanticGraphState()
    for source in _lineage_sources(lineage):
        graph.add_lineage_source(source)
    for item in _lineage_content(lineage):
        graph.add_content(item)
    for edge in _lineage_source_edges(lineage):
        graph.add_source_edge(edge)
    for edge in _lineage_revision_edges(lineage):
        graph.add_revision_edge(edge)
    for lead_source in _lineage_lead_sources(lineage):
        graph.add_lead_source(lead_source)
    for edge in _lineage_lead_edges(lineage):
        graph.add_lead_edge(edge)

    empty = not graph.nodes and not graph.edges
    discovery_count = int(lineage.get("discoveryArtifactCount") or 0)
    evidence_count = int(lineage.get("evidenceArtifactCount") or 0)
    semantic_nodes = graph.render_nodes()
    semantic_edges = graph.render_edges()
    return {
        "sessionDir": _payload_text(lineage, "sessionDir"),
        "contentDir": _payload_text(lineage, "contentDir"),
        "manifestPath": _payload_text(lineage, "manifestPath"),
        "nodeCount": len(semantic_nodes),
        "edgeCount": len(semantic_edges),
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "empty": empty,
        "nextStep": topology_next_step(
            discovery_count=discovery_count,
            evidence_count=evidence_count,
        ),
        "nodes": semantic_nodes,
        "edges": semantic_edges,
    }


def _empty_semantic_focus_payload(
    semantic: SemanticPayload,
    *,
    query: str,
    next_step: str,
) -> SemanticFocusPayload:
    return {
        "sessionDir": _payload_text(semantic, "sessionDir"),
        "contentDir": _payload_text(semantic, "contentDir"),
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
        "sessionDir": _payload_text(semantic, "sessionDir"),
        "contentDir": _payload_text(semantic, "contentDir"),
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
