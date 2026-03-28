"""Lead data structures and cache identity."""

from __future__ import annotations

from dataclasses import dataclass

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
