"""Typed payload shapes for `gotta session leads`."""

from __future__ import annotations

from typing import TypedDict

from gotta.lead.model import LeadEdgeRecord, LeadSourceSummary, LeadVisibility


class ProviderCountRecord(TypedDict):
    provider: str
    count: int


class RelationCountRecord(TypedDict):
    relation: str
    count: int


class LeadArtifact(LeadVisibility):
    checksum: str
    preferredName: str
    artifactKind: str
    sourceLocator: str
    artifactLocator: str
    contentLocator: str
    lastFetchedAt: str
    leadCount: int
    leads: list[LeadEdgeRecord]


class LeadsPayload(TypedDict):
    sessionDir: str
    contentDir: str
    target: str
    filter: str
    limit: int
    artifactCount: int
    discoveryArtifactCount: int
    evidenceArtifactCount: int
    topProviders: list[ProviderCountRecord]
    topRelations: list[RelationCountRecord]
    leadCount: int
    totalCount: int
    shownCount: int
    offset: int
    nextOffset: int | None
    truncated: bool
    materializedLeadCount: int
    unmaterializedLeadCount: int
    leadSources: list[LeadSourceSummary]
    bestOverall: list[LeadSourceSummary]
    providerHighlights: list[LeadSourceSummary]
    artifacts: list[LeadArtifact]
    empty: bool
    nextStep: str
