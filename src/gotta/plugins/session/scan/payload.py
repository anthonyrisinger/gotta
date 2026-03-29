"""Payload synthesis for `gotta session scan`."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re

from gotta import stored
from gotta.content.filesystem import FileSystemLedgerStore
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
) -> dict[str, object]:
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
    entries = object_entries(aggregate_manifest_entries(raw_entries))
    snapshots = {
        snapshot.digest: snapshot
        for snapshot in FileSystemLedgerStore.for_content_dir(
            dirs.content_dir
        ).scan_artifacts()
    }
    matches: list[dict[str, object]] = []
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
    return {
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
        **paging,
        "entries": paged,
    }


def scan_entries(
    entries: list[dict[str, object]],
    *,
    plugin: str = "",
    actor: str = "",
    locator: str = "",
    kind: str = "",
) -> list[dict[str, object]]:
    filtered = object_entries(
        filter_manifest_entries(
            entries,
            plugin=plugin,
            actor=actor,
            locator=locator,
        )
    )
    kind_filter = kind.strip().lower()
    if not kind_filter:
        return filtered
    return [
        entry
        for entry in filtered
        if artifact_kind(entry.get("artifact_kind") or entry.get("artifactKind"))
        == kind_filter
    ]


def matched_entry(
    entry: dict[str, object],
    *,
    snapshots: dict[str, ContentSnapshot],
    query: str,
    case_sensitive: bool,
    pattern: re.Pattern[str] | None,
    context: int,
    snippet_limit: int,
    session_ref: str,
) -> dict[str, object] | None:
    checksum = string(entry.get("checksum"))
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
    preferred_name = string(entry.get("preferred_name")) or "data"
    canonical_locator = string(entry.get("canonical_locator") or entry.get("locator"))
    artifact_locator = artifact_human_locator(preferred_name, checksum)
    snippets = build_snippets(
        lines,
        hits=hits,
        context=context,
        limit=snippet_limit,
    )
    return {
        **entry,
        "artifactKind": artifact_kind(entry.get("artifact_kind")),
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
        "plugins": string_list(entry.get("plugins")),
        "actors": string_list(entry.get("actors")),
        "locators": string_list(entry.get("locators")),
        "artifactKinds": string_list(entry.get("artifactKinds")),
        "hitCount": len(hits),
        "snippetCount": len(snippets),
        "displayLineCount": len(lines),
        "snippets": snippets,
        **resolved_visibility_metadata(
            entry,
            provider=string(entry.get("plugin")),
            plugin=string(entry.get("plugin")),
            subcommand=string(entry.get("subcommand")),
            locator=canonical_locator,
        ),
    }


def scan_display_text(snapshot: ContentSnapshot) -> str:
    rendered = stored.stored_display(snapshot.layout.blob_path)
    return rendered.data.decode("utf-8", errors="replace")


def object_entries(entries: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    return [{str(key): value for key, value in entry.items()} for entry in entries]


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
