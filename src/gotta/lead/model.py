"""Lead data structures and cross-package payload records."""

from __future__ import annotations

from dataclasses import dataclass

from gotta.content.model import ContentSnapshot
from typing import TypedDict

LEADS_CACHE_NAME = "leads.json"
LEADS_CACHE_VERSION = 8


@dataclass(frozen=True, slots=True)
class LeadMention:
    raw: str
    canonical_locator: str
    provider: str
    relation: str
    follow_command: str
    snippet: str
    ordinal: int


class LeadVisibility(TypedDict, total=False):
    visibility_level: str
    visibility_boundary: str
    visibility_confidence: str
    visibility_basis: list[str]


class LeadSearchOrigin(TypedDict):
    provider: str
    subcommand: str
    rank: int
    artifactLocator: str
    sourceLocator: str


class LeadEdge(LeadVisibility):
    sourceChecksum: str
    sourceLocator: str
    sourceArtifactLocator: str
    sourceContentLocator: str
    sourcePreferredName: str
    targetLocator: str
    provider: str
    kind: str
    relation: str
    followCommand: str
    rawExamples: list[str]
    contexts: list[str]
    occurrenceCount: int
    sourceSearchLike: bool
    sourceProvider: str
    sourceSubcommand: str
    sourceRank: int
    materialized: bool
    targetArtifactLocators: list[str]
    targetContentLocators: list[str]
    firstParty: bool
    searchSeed: bool


class LeadSourceSummary(LeadVisibility):
    locator: str
    provider: str
    kind: str
    followCommand: str
    firstParty: bool
    searchSeed: bool
    materialized: bool
    occurrenceCount: int
    artifactCount: int
    relationKinds: list[str]
    artifactLocators: list[str]
    contentLocators: list[str]
    exampleRaw: str
    contexts: list[str]
    bestSearchRank: int
    searchLikeSourceCount: int
    searchOrigins: list[LeadSearchOrigin]


@dataclass(frozen=True, slots=True)
class LeadResolution:
    selected_snapshots: list[ContentSnapshot]
    edge_records: list[LeadEdge]
    lead_sources: list[LeadSourceSummary]
