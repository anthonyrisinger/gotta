"""Rank lead edges and aggregated lead sources."""

from __future__ import annotations

from .canon import low_signal_url_penalty

FIRST_PARTY_LEAD_PROVIDERS = {
    "jira",
    "confluence",
    "slack",
    "github",
    "gdocs",
    "gdrive",
    "gsheets",
    "artifact",
    "content",
}
RELATION_PRIORITY = {
    "mentions": 3,
    "links_to": 2,
    "suggests_search": 1,
}


def is_first_party_target(*, provider: str, kind: str) -> bool:
    return provider in FIRST_PARTY_LEAD_PROVIDERS or kind != "url"


def edge_best_first_sort_key(item: dict[str, object]) -> tuple[object, ...]:
    raw_examples = [str(value) for value in item.get("rawExamples") or [] if str(value)]
    query = raw_examples[0] if raw_examples else str(item.get("targetLocator") or "")
    source_rank = int(item.get("sourceRank") or 0)
    return (
        not bool(item.get("firstParty")),
        not bool(item.get("materialized")),
        _lead_signal_penalty(item),
        bool(item.get("searchSeed")),
        bool(item.get("sourceSearchLike")),
        -_relation_priority(str(item.get("relation") or "")),
        -int(item.get("occurrenceCount") or 0),
        source_rank
        if bool(item.get("sourceSearchLike")) and source_rank > 0
        else 1_000_000,
        *_search_seed_signal_sort_key(query),
        str(item.get("targetLocator") or "").casefold(),
    )


def lead_source_best_first_sort_key(item: dict[str, object]) -> tuple[object, ...]:
    artifact_count = int(item.get("artifactCount") or 0)
    search_like_source_count = int(item.get("searchLikeSourceCount") or 0)
    query = str(item.get("exampleRaw") or item.get("locator") or "")
    best_search_rank = int(item.get("bestSearchRank") or 0)
    return (
        not bool(item.get("firstParty")),
        not bool(item.get("materialized")),
        _lead_signal_penalty(item),
        bool(item.get("searchSeed")),
        bool(artifact_count and search_like_source_count >= artifact_count),
        bool(search_like_source_count),
        -_relation_set_priority(
            [str(value) for value in item.get("relationKinds") or [] if str(value)]
        ),
        -artifact_count,
        -int(item.get("occurrenceCount") or 0),
        best_search_rank if best_search_rank > 0 else 1_000_000,
        *_search_seed_signal_sort_key(query),
        str(item.get("locator") or "").casefold(),
    )


def _relation_priority(value: str) -> int:
    return RELATION_PRIORITY.get(value, 0)


def _relation_set_priority(values: set[str] | list[str]) -> int:
    return max((_relation_priority(str(value)) for value in values), default=0)


def _search_seed_signal_sort_key(query: str) -> tuple[object, ...]:
    tokens = [token for token in query.split() if token]
    uppercase_count = sum(1 for token in tokens if token.isupper())
    titlecase_count = sum(
        1 for token in tokens if token[:1].isupper() and not token.isupper()
    )
    long_lowercase_count = sum(
        1 for token in tokens if not token[:1].isupper() and len(token.casefold()) >= 8
    )
    return (
        -uppercase_count,
        -(titlecase_count + long_lowercase_count),
        -len(tokens),
        query.casefold(),
    )


def _lead_signal_penalty(item: dict[str, object]) -> int:
    if bool(item.get("materialized")):
        return 0
    locator = str(item.get("locator") or item.get("targetLocator") or "").strip()
    if not locator.startswith(("http://", "https://")):
        return 0
    provider = str(item.get("provider") or "")
    kind = str(item.get("kind") or "")
    if provider in FIRST_PARTY_LEAD_PROVIDERS and kind != "url":
        return 0
    return low_signal_url_penalty(locator)
