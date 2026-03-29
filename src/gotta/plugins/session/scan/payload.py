"""Payload synthesis for `gotta session scan`."""

from __future__ import annotations

from collections.abc import Mapping
import re

from gotta import stored
from gotta.content.backend import scan_content_snapshots
from gotta.content.model import ContentSnapshot, ResolvedDirs
from gotta.content.path import content_locator

from ..core import (
    artifact_human_locator,
    artifact_kind,
    follow_command,
    paginate_items,
    resolved_visibility_metadata,
    session_read_command,
)
from ..manifest.record import (
    aggregate_manifest_entries,
    filter_manifest_entries,
    manifest_entries,
)
from .model import ScanEntry, ScanPayload, ScanVisibility
from .match import compile_pattern, hit_lines
from .snippet import build_snippets


def scan_payload(
    dirs: ResolvedDirs,
    *,
    query: str,
    plugin: str = "",
    actor: str = "",
    locator: str = "",
    kind: str = "",
    match_mode: str = "literal",
    case_sensitive: bool = False,
    context: int = 2,
    snippet_limit: int = 3,
    limit: int = 20,
    offset: int = 0,
    include_all: bool = False,
    session_ref: str = "",
) -> ScanPayload:
    pattern = compile_pattern(
        query,
        match_mode=match_mode,
        case_sensitive=case_sensitive,
    )
    raw_entries = scan_entries(
        manifest_entries(dirs),
        plugin=plugin,
        actor=actor,
        locator=locator,
        kind=kind,
    )
    entries = raw_entries
    snapshots = {
        snapshot.digest: snapshot
        for snapshot in scan_content_snapshots(
            dirs.content_dir,
            session_dir=dirs.session_dir,
        )
    }
    matches: list[ScanEntry] = []
    for entry in entries:
        matched = matched_entry(
            entry,
            snapshots=snapshots,
            query=query,
            case_sensitive=case_sensitive,
            pattern=pattern,
            context=max(context, 0),
            snippet_limit=max(snippet_limit, 0) or 1,
            session_ref=session_ref,
        )
        if matched is not None:
            matches.append(matched)
    ordered = sorted(
        matches,
        key=lambda entry: string(
            entry.get("canonical_locator") or entry.get("locator")
        ),
    )
    ordered = sorted(
        ordered,
        key=lambda entry: string(entry.get("lastFetchedAt") or entry.get("fetched_at")),
        reverse=True,
    )
    ordered = sorted(
        ordered,
        key=lambda entry: integer(entry.get("hitCount")),
        reverse=True,
    )
    paged, paging = paginate_items(
        ordered,
        limit=limit,
        offset=offset,
        include_all=include_all,
    )
    payload: ScanPayload = {
        "sessionDir": str(dirs.session_dir),
        "contentDir": str(dirs.content_dir),
        "manifestPath": str(dirs.content_dir / "manifest.jsonl"),
        "query": query,
        "matchMode": match_mode,
        "caseSensitive": bool(case_sensitive),
        "context": max(context, 0),
        "snippetLimit": max(snippet_limit, 0) or 1,
        "entryCount": len(ordered),
        "pluginFilter": plugin,
        "actorFilter": actor,
        "locatorFilter": locator,
        "kindFilter": kind,
        "offset": paging_int(paging.get("offset")),
        "limit": paging_limit(paging.get("limit")),
        "totalCount": paging_int(paging.get("totalCount")),
        "shownCount": paging_int(paging.get("shownCount")),
        "nextOffset": paging_next_offset(paging.get("nextOffset")),
        "truncated": paging_bool(paging.get("truncated")),
        "entries": paged,
    }
    return payload


def scan_entries(
    entries: list[dict[str, object]],
    *,
    plugin: str = "",
    actor: str = "",
    locator: str = "",
    kind: str = "",
) -> list[ScanEntry]:
    filtered = [
        manifest_scan_entry(entry)
        for entry in aggregate_manifest_entries(
            filter_manifest_entries(
                list(entries),
                plugin=plugin,
                actor=actor,
                locator=locator,
            )
        )
    ]
    kind_filter = kind.strip().lower()
    if not kind_filter:
        return filtered
    return [entry for entry in filtered if entry.get("artifactKind", "") == kind_filter]


def manifest_scan_entry(entry: Mapping[str, object]) -> ScanEntry:
    checksum = string(entry.get("checksum"))
    plugin = string(entry.get("plugin"))
    actor = string(entry.get("actor"))
    locator = string(entry.get("locator"))
    canonical_locator = string(entry.get("canonical_locator")) or locator
    artifact_kind_value = artifact_kind(
        entry.get("artifact_kind") or entry.get("artifactKind")
    )
    plugins = string_list(entry.get("plugins"))
    actors = string_list(entry.get("actors"))
    locators = string_list(entry.get("locators"))
    artifact_kinds = string_list(entry.get("artifactKinds"))
    scan_entry: ScanEntry = {
        "checksum": checksum,
        "plugin": plugin,
        "actor": actor,
        "subcommand": string(entry.get("subcommand")),
        "locator": locator,
        "canonical_locator": canonical_locator,
        "preferred_name": string(entry.get("preferred_name")),
        "fetched_at": string(entry.get("fetched_at")),
        "fetchCount": integer(entry.get("fetchCount")),
        "firstFetchedAt": string(entry.get("firstFetchedAt")),
        "lastFetchedAt": string(entry.get("lastFetchedAt") or entry.get("fetched_at")),
        "plugins": plugins or ([plugin] if plugin else []),
        "actors": actors or ([actor] if actor else []),
        "locators": locators or ([canonical_locator] if canonical_locator else []),
        "artifactKinds": (
            artifact_kinds or ([artifact_kind_value] if artifact_kind_value else [])
        ),
        "artifactKind": artifact_kind_value,
    }
    apply_visibility(scan_entry, visibility_metadata(entry))
    return scan_entry


def matched_entry(
    entry: ScanEntry,
    *,
    snapshots: dict[str, ContentSnapshot],
    query: str,
    case_sensitive: bool,
    pattern: re.Pattern[str] | None,
    context: int,
    snippet_limit: int,
    session_ref: str,
) -> ScanEntry | None:
    checksum = entry.get("checksum", "")
    snapshot = snapshots.get(checksum)
    if snapshot is None:
        return None
    lines = scan_display_text(snapshot).splitlines()
    hits = hit_lines(
        lines,
        query=query,
        case_sensitive=case_sensitive,
        pattern=pattern,
    )
    if not hits:
        return None
    preferred_name = entry.get("preferred_name", "") or "data"
    canonical_locator = entry.get("canonical_locator") or entry.get("locator") or ""
    artifact_locator = artifact_human_locator(preferred_name, checksum)
    snippets = build_snippets(
        lines,
        hits=hits,
        context=context,
        limit=snippet_limit,
    )
    matched: ScanEntry = {
        **entry,
        "artifactLocator": artifact_locator,
        "contentLocator": content_locator(checksum),
        "followCommand": follow_command(
            canonical_locator,
            checksum=checksum,
            session_ref=session_ref,
        ),
        "contentFollowCommand": follow_command(
            "",
            checksum=checksum,
            session_ref=session_ref,
        ),
        "artifactFollowCommand": (
            session_read_command(artifact_locator, session_ref=session_ref)
            if checksum
            else ""
        ),
        "hitCount": len(hits),
        "snippetCount": len(snippets),
        "displayLineCount": len(lines),
        "snippets": snippets,
    }
    apply_visibility(
        matched,
        resolved_scan_visibility(
            entry,
            provider=entry.get("plugin", ""),
            subcommand=entry.get("subcommand", ""),
            locator=canonical_locator,
        ),
    )
    return matched


def scan_display_text(snapshot: ContentSnapshot) -> str:
    rendered = stored.stored_display(snapshot.layout.blob_path)
    return rendered.data.decode("utf-8", errors="replace")


def string(value: object) -> str:
    return str(value or "").strip()


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := string(item))]


def integer(value: object) -> int:
    try:
        return int(str(value or 0))
    except ValueError:
        return 0


def paging_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def paging_limit(value: object) -> int | None:
    return value if isinstance(value, int) else None


def paging_next_offset(value: object) -> int | None:
    return value if isinstance(value, int) else None


def paging_bool(value: object) -> bool:
    return value if isinstance(value, bool) else False


def visibility_metadata(entry: Mapping[str, object]) -> ScanVisibility:
    visibility: ScanVisibility = {}
    visibility_level = entry.get("visibility_level")
    visibility_boundary = entry.get("visibility_boundary")
    visibility_confidence = entry.get("visibility_confidence")
    if isinstance(visibility_level, str):
        visibility["visibility_level"] = visibility_level
    if isinstance(visibility_boundary, str):
        visibility["visibility_boundary"] = visibility_boundary
    if isinstance(visibility_confidence, str):
        visibility["visibility_confidence"] = visibility_confidence
    basis = entry.get("visibility_basis")
    if isinstance(basis, list):
        visibility["visibility_basis"] = string_list(basis)
    return visibility


def resolved_scan_visibility(
    entry: ScanEntry,
    *,
    provider: str,
    subcommand: str,
    locator: str,
) -> ScanVisibility:
    visibility = resolved_visibility_metadata(
        entry,
        provider=provider,
        plugin=provider,
        subcommand=subcommand,
        locator=locator,
    )
    return visibility_metadata(visibility)


def apply_visibility(entry: ScanEntry, visibility: ScanVisibility) -> None:
    if "visibility_level" in visibility:
        entry["visibility_level"] = visibility["visibility_level"]
    if "visibility_boundary" in visibility:
        entry["visibility_boundary"] = visibility["visibility_boundary"]
    if "visibility_confidence" in visibility:
        entry["visibility_confidence"] = visibility["visibility_confidence"]
    if "visibility_basis" in visibility:
        entry["visibility_basis"] = visibility["visibility_basis"]
