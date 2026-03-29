"""Typed payload shapes for `gotta session scan`."""

from __future__ import annotations

from typing import TypedDict

from .snippet import Snippet


class ScanVisibility(TypedDict, total=False):
    visibility_level: str
    visibility_boundary: str
    visibility_confidence: str
    visibility_basis: list[str]


class ScanEntry(ScanVisibility, total=False):
    checksum: str
    plugin: str
    actor: str
    subcommand: str
    locator: str
    canonical_locator: str
    preferred_name: str
    fetched_at: str
    fetchCount: int
    firstFetchedAt: str
    lastFetchedAt: str
    plugins: list[str]
    actors: list[str]
    locators: list[str]
    artifactKinds: list[str]
    artifactKind: str
    artifactLocator: str
    contentLocator: str
    followCommand: str
    contentFollowCommand: str
    artifactFollowCommand: str
    hitCount: int
    snippetCount: int
    displayLineCount: int
    snippets: list[Snippet]


class ScanPayload(TypedDict):
    sessionDir: str
    contentDir: str
    manifestPath: str
    query: str
    matchMode: str
    caseSensitive: bool
    context: int
    snippetLimit: int
    entryCount: int
    pluginFilter: str
    actorFilter: str
    locatorFilter: str
    kindFilter: str
    offset: int
    limit: int | None
    totalCount: int
    shownCount: int
    nextOffset: int | None
    truncated: bool
    entries: list[ScanEntry]
