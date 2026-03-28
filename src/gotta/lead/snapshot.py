"""Snapshot helpers for lead extraction and rendering."""

from __future__ import annotations

from gotta.content.model import ContentSnapshot
from gotta.content.path import artifact_locator

from .canon import provider_for_locator


def snapshot_display_name(snapshot: ContentSnapshot) -> str:
    preferred = str(snapshot.metadata.get("preferred_name", "")).strip()
    if preferred:
        return preferred
    if snapshot.names:
        return snapshot.names[0]
    return "data"


def snapshot_locator(snapshot: ContentSnapshot) -> str:
    return (
        str(
            snapshot.metadata.get("canonical_locator", "")
            or snapshot.metadata.get("locator", "")
        ).strip()
        or "unknown"
    )


def snapshot_artifact_locator(snapshot: ContentSnapshot) -> str:
    return artifact_locator(snapshot_display_name(snapshot), snapshot.digest)


def snapshot_last_fetched_at(snapshot: ContentSnapshot) -> str:
    if not snapshot.events:
        return ""
    return snapshot.events[-1].timestamp


def snapshot_sort_key(snapshot: ContentSnapshot) -> tuple[str, str]:
    return (snapshot_last_fetched_at(snapshot), snapshot.digest)


def is_search_like_locator(locator: str) -> bool:
    text = locator.strip().casefold()
    if not text:
        return False
    return text.startswith(
        (
            "slack:search ",
            "github:search ",
            "jira:search ",
            "confluence:search ",
            "gdocs:search ",
            "gdrive:search ",
            "gsheets:search ",
            "search ",
            "jql ",
            "cql ",
        )
    )


def snapshot_is_search_like(snapshot: ContentSnapshot) -> bool:
    metadata = snapshot.metadata
    for key in ("canonical_locator", "locator"):
        if is_search_like_locator(str(metadata.get(key) or "")):
            return True
    subcommand = str(metadata.get("subcommand") or "").strip().casefold()
    if subcommand in {"search", "jql", "cql"}:
        return True
    argv = metadata.get("argv")
    if isinstance(argv, list) and argv:
        first = str(argv[0] or "").strip().casefold()
        if first in {"search", "jql", "cql"}:
            return True
    return False


def snapshot_provider(snapshot: ContentSnapshot) -> str:
    locator = snapshot_locator(snapshot)
    provider = provider_for_locator(locator)
    if provider != "external":
        return provider
    return str(snapshot.metadata.get("plugin") or "").strip()


def snapshot_subcommand(snapshot: ContentSnapshot) -> str:
    raw_subcommand = str(snapshot.metadata.get("subcommand") or "").strip().casefold()
    if raw_subcommand in {"search", "jql", "cql"}:
        return raw_subcommand
    locator = snapshot_locator(snapshot).casefold()
    if ":search " in locator:
        return "search"
    if ":jql " in locator:
        return "jql"
    if ":cql " in locator:
        return "cql"
    argv = snapshot.metadata.get("argv")
    if isinstance(argv, list) and argv:
        first = str(argv[0] or "").strip().casefold()
        if first in {"search", "jql", "cql"}:
            return first
    return raw_subcommand
