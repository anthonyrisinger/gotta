"""Typed payload shapes for session analysis."""

from __future__ import annotations

from typing import Literal, TypedDict


class AnalyzeVisibility(TypedDict, total=False):
    visibility_level: str
    visibility_boundary: str
    visibility_confidence: str
    visibility_basis: list[str]


class AnalyzeScanEntry(TypedDict, total=False):
    checksum: str
    canonical_locator: str
    locator: str
    lastFetchedAt: str
    fetched_at: str
    hitCount: int
    artifactKind: str
    artifact_kind: str


class AnalyzeScanPayload(TypedDict, total=False):
    entries: list[AnalyzeScanEntry]


class LineageSource(AnalyzeVisibility, total=False):
    locator: str
    contentCount: int
    entryCount: int
    artifactKind: str
    artifactKinds: list[str]
    plugins: list[str]
    actors: list[str]
    locators: list[str]
    collision: bool
    duplicateMaterialization: bool
    variant: bool
    variantCount: int
    variants: list[str]


class LineageContent(AnalyzeVisibility, total=False):
    checksum: str
    preferredName: str
    artifactKind: str
    contentLocator: str
    artifactLocator: str
    followCommand: str
    nameCollision: bool
    nameCount: int
    fetchCount: int
    names: list[str]
    firstFetchedAt: str
    lastFetchedAt: str
    providers: list[str]
    actors: list[str]
    resourceHints: list[str]


class LineageSourceEdge(TypedDict, total=False):
    source: str
    checksum: str
    plugins: list[str]
    actors: list[str]
    count: int


LineageRevisionEdge = TypedDict(
    "LineageRevisionEdge",
    {
        "locator": str,
        "preferredName": str,
        "from": str,
        "to": str,
        "fromTimestamp": str,
        "toTimestamp": str,
        "plugin": str,
        "actor": str,
        "rendering": str,
    },
    total=False,
)


class LeadSourceSummary(AnalyzeVisibility, total=False):
    locator: str
    provider: str
    materialized: bool
    occurrenceCount: int
    artifactCount: int
    artifactKind: str
    relationKinds: list[str]
    followCommand: str


class LeadEdgeSummary(AnalyzeVisibility, total=False):
    sourceChecksum: str
    targetLocator: str
    relation: str
    occurrenceCount: int
    materialized: bool


class LineageCandidate(TypedDict, total=False):
    kind: Literal["source", "content", "lead"]
    label: str
    locator: str
    checksum: str
    artifactLocator: str
    contentLocator: str
    artifactKind: str
    materialized: bool
    followCommand: str


class LineageNeighbor(TypedDict, total=False):
    kind: Literal["source", "content", "lead"]
    label: str
    relation: str
    artifactKind: str
    materialized: bool
    followCommand: str


class LineagePayload(TypedDict, total=False):
    sessionDir: str
    contentDir: str
    manifestPath: str
    manifestEntryCount: int
    contentCount: int
    sourceCount: int
    sourceEdgeCount: int
    revisionEdgeCount: int
    discoveryArtifactCount: int
    evidenceArtifactCount: int
    collisionCount: int
    collisions: list[str]
    duplicateMaterializationCount: int
    duplicateMaterializations: list[str]
    variantCount: int
    variants: list[str]
    nameCollisionCount: int
    nameCollisions: list[str]
    leadSourceCount: int
    materializedLeadSourceCount: int
    unmaterializedLeadSourceCount: int
    leadEdgeCount: int
    empty: bool
    nextStep: str
    sources: list[LineageSource]
    content: list[LineageContent]
    sourceEdges: list[LineageSourceEdge]
    revisionEdges: list[LineageRevisionEdge]
    leadSources: list[LeadSourceSummary]
    leadEdges: list[LeadEdgeSummary]


class LineageFocusPayload(TypedDict, total=False):
    sessionDir: str
    contentDir: str
    manifestPath: str
    focus: str
    matched: bool
    empty: bool
    nextStep: str
    root: LineageCandidate
    anchors: list[LineageCandidate]
    matchedCount: int
    neighbors: list[LineageNeighbor]
    sources: list[LineageSource]
    content: list[LineageContent]
    sourceEdges: list[LineageSourceEdge]
    revisionEdges: list[LineageRevisionEdge]
    leadSources: list[LeadSourceSummary]
    leadEdges: list[LeadEdgeSummary]
    discoveryArtifactCount: int
    evidenceArtifactCount: int
    contentCount: int
    sourceCount: int
    sourceEdgeCount: int
    revisionEdgeCount: int
    leadSourceCount: int
    leadEdgeCount: int
    collisionCount: int
    collisions: list[str]
    duplicateMaterializationCount: int
    duplicateMaterializations: list[str]
    variantCount: int
    variants: list[str]


class SemanticNode(TypedDict, total=False):
    id: str
    label: str
    kind: str
    group: str
    materialized: bool
    discovered: bool
    artifactKind: str
    artifactKinds: list[str]
    followCommand: str


class SemanticNeighbor(SemanticNode, total=False):
    relations: list[str]


class SemanticEdge(TypedDict):
    source: str
    target: str
    label: str


class SemanticPayload(TypedDict, total=False):
    sessionDir: str
    contentDir: str
    manifestPath: str
    nodeCount: int
    edgeCount: int
    discoveryArtifactCount: int
    evidenceArtifactCount: int
    empty: bool
    nextStep: str
    nodes: list[SemanticNode]
    edges: list[SemanticEdge]


class SemanticFocusPayload(TypedDict, total=False):
    sessionDir: str
    contentDir: str
    focus: str
    matched: bool
    empty: bool
    nextStep: str
    nodeCount: int
    edgeCount: int
    root: SemanticNode
    anchors: list[SemanticNode]
    matchedCount: int
    neighbors: list[SemanticNeighbor]
    nodes: list[SemanticNode | SemanticNeighbor]
    edges: list[SemanticEdge]
    suppressedStructuralEdgeCount: int


class ProviderCluster(TypedDict):
    provider: str
    nodeCount: int


class DominantKind(TypedDict):
    kind: str
    nodeCount: int


class DominantRelation(TypedDict):
    label: str
    edgeCount: int


class AnalysisOverviewPayload(TypedDict, total=False):
    sessionDir: str
    contentDir: str
    manifestPath: str
    contentCount: int
    sourceCount: int
    leadSourceCount: int
    leadEdgeCount: int
    semanticNodeCount: int
    semanticEdgeCount: int
    discoveryArtifactCount: int
    evidenceArtifactCount: int
    nextStep: str
    providerClusters: list[ProviderCluster]
    dominantKinds: list[DominantKind]
    dominantRelations: list[DominantRelation]
    materializedAnchors: list[LineageContent]
    querySeeds: list[SemanticNode]
    bestLeads: list[LeadSourceSummary]
    sourceHeavy: bool
    structuralHeavy: bool
    sourceNodeCount: int
    structuralEdgeCount: int


class CombinedAnalysisPayload(TypedDict):
    mode: Literal["all"]
    focus: str
    lineage: LineagePayload | LineageFocusPayload
    semantic: SemanticPayload | SemanticFocusPayload
