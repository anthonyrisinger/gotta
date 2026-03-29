"""Manifest payload synthesis."""

from __future__ import annotations

import re

from gotta.content.model import ResolvedDirs
from gotta.content.path import content_locator

from ..core import (
    artifact_human_locator,
    artifact_kind,
    artifact_kind_counts,
    compile_filter_pattern,
    follow_command,
    match_any,
    match_filter_text,
    paginate_items,
    resolved_visibility_metadata,
    session_read_command,
    top_count_records,
)
from .record import (
    aggregate_manifest_entries,
    filter_manifest_entries,
    manifest_entries,
    manifest_entry_sort_key,
)
from .model import (
    ManifestActorCountRecord,
    ManifestPayload,
    ManifestPayloadEntry,
    ManifestPluginCountRecord,
    ManifestRecord,
    apply_manifest_visibility,
    manifest_visibility,
)


def _string(value: object) -> str:
    return str(value or "").strip()


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        text = _string(item)
        if text:
            values.append(text)
    return values


def _manifest_entry_matches(
    entry: ManifestRecord,
    pattern: re.Pattern[str] | None,
) -> bool:
    return match_any(
        pattern,
        entry.get("canonical_locator"),
        entry.get("locator"),
        entry.get("preferred_name"),
        entry.get("plugin"),
        entry.get("plugins"),
        entry.get("actor"),
        entry.get("actors"),
        entry.get("locators"),
        entry.get("artifactKinds"),
        entry.get("artifactKind"),
        entry.get("artifact_locator"),
        entry.get("content_locator"),
        entry.get("visibility_basis"),
    )


def _rendered_manifest_entry(
    entry: ManifestRecord,
    *,
    session_ref: str,
) -> ManifestPayloadEntry:
    checksum = _string(entry.get("checksum"))
    preferred_name = _string(entry.get("preferred_name")) or "data"
    canonical_locator = _string(entry.get("canonical_locator")) or _string(
        entry.get("locator")
    )
    rendered: ManifestPayloadEntry = {
        "plugin": _string(entry.get("plugin")),
        "actor": _string(entry.get("actor")),
        "target_actor": _string(entry.get("target_actor")),
        "subcommand": _string(entry.get("subcommand")),
        "locator": _string(entry.get("locator")),
        "canonical_locator": canonical_locator,
        "preferred_name": preferred_name,
        "checksum": checksum,
        "fetched_at": _string(entry.get("fetched_at")),
        "artifactKind": artifact_kind(entry.get("artifact_kind")),
        "content_locator": content_locator(checksum) if checksum else "",
        "fetchCount": int(_string(entry.get("fetchCount")) or 0),
        "firstFetchedAt": _string(entry.get("firstFetchedAt")),
        "lastFetchedAt": _string(entry.get("lastFetchedAt")),
        "plugins": _string_list(entry.get("plugins")),
        "actors": _string_list(entry.get("actors")),
        "locators": _string_list(entry.get("locators")),
        "artifactKinds": _string_list(entry.get("artifactKinds")),
        "artifact_locator": artifact_human_locator(preferred_name, checksum),
        "follow_command": follow_command(
            canonical_locator,
            checksum=checksum,
            session_ref=session_ref,
        ),
        "content_follow_command": (
            follow_command("", checksum=checksum, session_ref=session_ref)
            if checksum
            else ""
        ),
        "artifact_follow_command": (
            session_read_command(
                artifact_human_locator(preferred_name, checksum),
                session_ref=session_ref,
            )
            if checksum
            else ""
        ),
    }
    apply_manifest_visibility(
        rendered,
        manifest_visibility(
            resolved_visibility_metadata(
                entry,
                provider=_string(entry.get("plugin")),
                plugin=_string(entry.get("plugin")),
                subcommand=_string(entry.get("subcommand")),
                locator=canonical_locator,
            )
        ),
    )
    return rendered


def manifest_payload(
    dirs: ResolvedDirs,
    *,
    plugin: str = "",
    actor: str = "",
    locator: str = "",
    filter_query: str = "",
    limit: int = 20,
    offset: int = 0,
    include_all: bool = False,
    session_ref: str = "",
) -> ManifestPayload:
    raw_entries = filter_manifest_entries(
        manifest_entries(dirs),
        plugin=plugin,
        actor=actor,
        locator=locator,
    )
    entries = aggregate_manifest_entries(raw_entries)
    filter_text = match_filter_text(filter_query)
    filter_pattern = compile_filter_pattern(filter_text)
    if filter_pattern is not None:
        entries = [
            entry for entry in entries if _manifest_entry_matches(entry, filter_pattern)
        ]
    ordered = sorted(entries, key=manifest_entry_sort_key, reverse=True)
    discovery_count, evidence_count = artifact_kind_counts(ordered)
    paged, paging = paginate_items(
        ordered,
        limit=limit,
        offset=offset,
        include_all=include_all,
    )
    rendered_entries = [
        _rendered_manifest_entry(entry, session_ref=session_ref) for entry in paged
    ]
    fetch_record_count = sum(
        int(_string(entry.get("fetchCount")) or 0) for entry in ordered
    )
    top_plugins = manifest_plugin_count_records(
        [
            plugin_name
            for entry in ordered
            for plugin_name in _string_list(entry.get("plugins"))
        ]
    )
    top_actors = manifest_actor_count_records(
        [
            actor_name
            for entry in ordered
            for actor_name in _string_list(entry.get("actors"))
        ]
    )
    payload: ManifestPayload = {
        "sessionDir": str(dirs.session_dir),
        "contentDir": str(dirs.content_dir),
        "manifestPath": str(dirs.content_dir / "manifest.jsonl"),
        "sessionRef": session_ref,
        "entryCount": len(entries),
        "fetchRecordCount": fetch_record_count,
        "offset": paging_int(paging.get("offset")),
        "limit": paging_limit(paging.get("limit")),
        "totalCount": paging_int(paging.get("totalCount")),
        "shownCount": paging_int(paging.get("shownCount")),
        "nextOffset": paging_next_offset(paging.get("nextOffset")),
        "truncated": paging_bool(paging.get("truncated")),
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "topPlugins": top_plugins,
        "topActors": top_actors,
        "pluginFilter": plugin,
        "actorFilter": actor,
        "locatorFilter": locator,
        "filter": filter_text,
        "entries": rendered_entries,
    }
    return payload


def manifest_plugin_count_records(
    values: list[str],
) -> list[ManifestPluginCountRecord]:
    records: list[ManifestPluginCountRecord] = []
    for record in top_count_records(values, key="plugin"):
        plugin = record.get("plugin")
        count = record.get("count")
        if isinstance(plugin, str) and isinstance(count, int):
            records.append({"plugin": plugin, "count": count})
    return records


def manifest_actor_count_records(values: list[str]) -> list[ManifestActorCountRecord]:
    records: list[ManifestActorCountRecord] = []
    for record in top_count_records(values, key="actor"):
        actor = record.get("actor")
        count = record.get("count")
        if isinstance(actor, str) and isinstance(count, int):
            records.append({"actor": actor, "count": count})
    return records


def paging_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def paging_limit(value: object) -> int | None:
    return value if isinstance(value, int) else None


def paging_next_offset(value: object) -> int | None:
    return value if isinstance(value, int) else None


def paging_bool(value: object) -> bool:
    return value if isinstance(value, bool) else False
