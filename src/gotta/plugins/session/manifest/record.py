"""Manifest record loading and aggregation."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import cast

from gotta.content.model import ResolvedDirs
from gotta.content.path import content_locator
from gotta.source.visibility import best_visibility_metadata

from ..core import artifact_kind, resolved_visibility_metadata
from .model import (
    ManifestRecord,
    ManifestVisibility,
    apply_manifest_visibility,
    manifest_visibility,
)


def _string(value: object) -> str:
    return str(value or "").strip()


def _manifest_record(value: dict[str, object]) -> ManifestRecord:
    return cast(ManifestRecord, value)


@dataclass(slots=True)
class AggregateState:
    latest: ManifestRecord
    fetch_count: int = 0
    first_fetched_at: str = ""
    last_fetched_at: str = ""
    plugins: set[str] = field(default_factory=set)
    actors: set[str] = field(default_factory=set)
    locators: set[str] = field(default_factory=set)
    artifact_kinds: set[str] = field(default_factory=set)
    visibility: ManifestVisibility = field(
        default_factory=lambda: manifest_visibility({})
    )


def manifest_entries(dirs: ResolvedDirs) -> list[ManifestRecord]:
    manifest_path = dirs.content_dir / "manifest.jsonl"
    if not manifest_path.exists():
        return []
    entries: list[ManifestRecord] = []
    for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        payload = json.loads(raw_line)
        if isinstance(payload, dict):
            normalized = {str(key): value for key, value in payload.items()}
            entries.append(_manifest_record(normalized))
    return entries


def filter_manifest_entries(
    entries: list[ManifestRecord],
    *,
    plugin: str = "",
    actor: str = "",
    locator: str = "",
) -> list[ManifestRecord]:
    plugin_filter = plugin.strip()
    actor_filter = actor.strip()
    locator_filter = locator.strip()
    filtered: list[ManifestRecord] = []
    for entry in entries:
        if plugin_filter and _string(entry.get("plugin")) != plugin_filter:
            continue
        if actor_filter and _string(entry.get("actor")) != actor_filter:
            continue
        if locator_filter:
            canonical = _string(entry.get("canonical_locator"))
            raw = _string(entry.get("locator"))
            if locator_filter not in canonical and locator_filter not in raw:
                continue
        filtered.append(entry)
    return filtered


def manifest_entry_sort_key(entry: ManifestRecord) -> tuple[str, str, str]:
    return (
        _string(entry.get("fetched_at")),
        _string(entry.get("canonical_locator")) or _string(entry.get("locator")),
        _string(entry.get("checksum")),
    )


def manifest_identity_locator(entry: ManifestRecord) -> str:
    checksum = _string(entry.get("checksum"))
    locator = _string(entry.get("canonical_locator")) or _string(entry.get("locator"))
    if locator:
        return locator
    return content_locator(checksum) if checksum else ""


def aggregate_manifest_entries(entries: list[ManifestRecord]) -> list[ManifestRecord]:
    grouped: dict[tuple[str, str], AggregateState] = {}
    for entry in entries:
        checksum = _string(entry.get("checksum"))
        locator = manifest_identity_locator(entry)
        state = grouped.setdefault((locator, checksum), AggregateState(latest=entry))
        fetched_at = _string(entry.get("fetched_at"))
        if manifest_entry_sort_key(entry) >= manifest_entry_sort_key(state.latest):
            state.latest = entry
        state.fetch_count += 1
        if fetched_at and (
            not state.first_fetched_at or fetched_at < state.first_fetched_at
        ):
            state.first_fetched_at = fetched_at
        if fetched_at and (
            not state.last_fetched_at or fetched_at > state.last_fetched_at
        ):
            state.last_fetched_at = fetched_at
        plugin = _string(entry.get("plugin"))
        actor = _string(entry.get("actor"))
        raw_locator = _string(entry.get("locator"))
        kind = artifact_kind(entry.get("artifact_kind") or entry.get("artifactKind"))
        if plugin:
            state.plugins.add(plugin)
        if actor:
            state.actors.add(actor)
        if raw_locator:
            state.locators.add(raw_locator)
        if locator:
            state.locators.add(locator)
        if kind:
            state.artifact_kinds.add(kind)
        state.visibility = manifest_visibility(
            best_visibility_metadata(
                state.visibility,
                resolved_visibility_metadata(
                    entry,
                    provider=plugin,
                    plugin=plugin,
                    subcommand=_string(entry.get("subcommand")),
                    locator=locator,
                ),
            )
        )

    aggregated: list[ManifestRecord] = []
    for (locator, _checksum), state in grouped.items():
        latest = _manifest_record(
            {str(key): value for key, value in state.latest.items()}
        )
        latest["canonical_locator"] = (
            _string(latest.get("canonical_locator")) or locator
        )
        latest["fetchCount"] = state.fetch_count
        latest["firstFetchedAt"] = state.first_fetched_at
        latest["lastFetchedAt"] = state.last_fetched_at or _string(
            latest.get("fetched_at")
        )
        latest["fetched_at"] = _string(latest.get("lastFetchedAt"))
        latest["plugins"] = sorted(state.plugins)
        latest["actors"] = sorted(state.actors)
        latest["locators"] = sorted(state.locators)
        latest["artifactKinds"] = sorted(state.artifact_kinds)
        apply_manifest_visibility(
            latest,
            manifest_visibility(best_visibility_metadata(state.visibility)),
        )
        aggregated.append(latest)
    return aggregated
