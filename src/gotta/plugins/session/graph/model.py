"""Typed graph payload shapes."""

from __future__ import annotations

from typing import TypedDict


class Visibility(TypedDict, total=False):
    visibility_level: str
    visibility_boundary: str
    visibility_confidence: str
    visibility_basis: list[str]


class GraphSource(Visibility):
    locator: str
    followCommand: str
    contentCount: int
    artifactKind: str
    artifactKinds: list[str]
    collision: bool
    variant: bool
    variantCount: int
    variants: list[str]


class GraphContent(Visibility):
    checksum: str
    preferredName: str
    artifactKind: str
    contentLocator: str
    artifactLocator: str
    followCommand: str
    sourceCount: int
    collision: bool


class GraphEdge(TypedDict):
    source: str
    checksum: str
    plugin: str
    count: int


class GraphProviderCountRecord(TypedDict):
    provider: str
    count: int


class GraphArtifactKindCountRecord(TypedDict):
    artifactKind: str
    count: int


class GraphPayload(TypedDict):
    sessionDir: str
    contentDir: str
    manifestPath: str
    filter: str
    sourceCount: int
    contentCount: int
    edgeCount: int
    discoveryArtifactCount: int
    evidenceArtifactCount: int
    topProviders: list[GraphProviderCountRecord]
    topArtifactKinds: list[GraphArtifactKindCountRecord]
    empty: bool
    nextStep: str
    sources: list[GraphSource]
    content: list[GraphContent]
    edges: list[GraphEdge]
