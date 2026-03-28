"""Scan surface for `gotta session`."""

from __future__ import annotations

import argparse
import json
import re

from gotta import stored
from gotta.content.path import content_locator
from gotta.content.store import scan_content_store

from .core import (
    artifact_human_locator,
    artifact_kind,
    follow_command,
    paging_summary_line,
    paginate_items,
    resolved_visibility_metadata,
    session_read_command,
    visibility_summary,
)
from .manifest import (
    aggregate_manifest_entries,
    filter_manifest_entries,
    manifest_entries,
)
from .parse import explicit_session_ref, require_started_session, session_dirs_for_read


def _scan_entries(
    entries: list[dict[str, object]],
    *,
    plugin: str = "",
    actor: str = "",
    locator: str = "",
    kind: str = "",
) -> list[dict[str, object]]:
    filtered = filter_manifest_entries(
        entries,
        plugin=plugin,
        actor=actor,
        locator=locator,
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


def _scan_hit_lines(
    lines: list[str],
    *,
    query: str,
    case_sensitive: bool,
    regex: re.Pattern[str] | None = None,
) -> list[int]:
    if regex is not None:
        return [index for index, line in enumerate(lines) if regex.search(line)]
    needle = query if case_sensitive else query.casefold()
    return [
        index
        for index, line in enumerate(lines)
        if needle in (line if case_sensitive else line.casefold())
    ]


def _scan_regex(
    query: str,
    *,
    match_mode: str,
    case_sensitive: bool,
) -> re.Pattern[str] | None:
    if match_mode != "regex":
        return None
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(query, flags)
    except re.error as exc:
        raise SystemExit(f"invalid scan pattern: {exc}") from exc


def _scan_snippets(
    lines: list[str],
    *,
    hits: list[int],
    context: int,
    limit: int,
) -> list[dict[str, object]]:
    snippets: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for hit in hits:
        start = max(hit - context, 0)
        end = min(hit + context, len(lines) - 1)
        if current is not None and start <= int(current["endLine"]) + 1:
            current["endLine"] = max(int(current["endLine"]), end + 1)
            current_hits = list(current["hitLines"])
            hit_line = hit + 1
            if hit_line not in current_hits:
                current_hits.append(hit_line)
                current["hitLines"] = current_hits
            continue
        if current is not None:
            snippets.append(current)
            if len(snippets) >= limit:
                return snippets
        current = {
            "startLine": start + 1,
            "endLine": end + 1,
            "hitLines": [hit + 1],
        }
    if current is not None and len(snippets) < limit:
        snippets.append(current)
    for snippet in snippets:
        start = int(snippet["startLine"]) - 1
        end = int(snippet["endLine"])
        snippet["lines"] = [
            {
                "number": index + 1,
                "text": lines[index],
            }
            for index in range(start, end)
        ]
    return snippets


def scan_display_text(snapshot) -> str:
    rendered = stored.stored_display(snapshot.data_path)
    return rendered.data.decode("utf-8", errors="replace")


def scan_payload(
    dirs,
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
    regex = _scan_regex(
        query,
        match_mode=match_mode,
        case_sensitive=case_sensitive,
    )
    raw_entries = _scan_entries(
        manifest_entries(dirs),
        plugin=plugin,
        actor=actor,
        locator=locator,
        kind=kind,
    )
    entries = aggregate_manifest_entries(raw_entries)
    snapshots = {
        snapshot.digest: snapshot for snapshot in scan_content_store(dirs.content_dir)
    }
    matches: list[dict[str, object]] = []
    for entry in entries:
        checksum = str(entry.get("checksum", "")).strip()
        snapshot = snapshots.get(checksum)
        if snapshot is None:
            continue
        text = scan_display_text(snapshot)
        lines = text.splitlines()
        hits = _scan_hit_lines(
            lines,
            query=query,
            case_sensitive=case_sensitive,
            regex=regex,
        )
        if not hits:
            continue
        snippets = _scan_snippets(
            lines,
            hits=hits,
            context=max(context, 0),
            limit=max(snippet_limit, 0) or 1,
        )
        matches.append(
            {
                **entry,
                "artifactKind": artifact_kind(entry.get("artifact_kind")),
                "artifactLocator": artifact_human_locator(
                    str(entry.get("preferred_name", "")).strip() or "data",
                    checksum,
                ),
                "contentLocator": content_locator(checksum),
                "followCommand": follow_command(
                    str(
                        entry.get("canonical_locator", "") or entry.get("locator", "")
                    ).strip(),
                    checksum=checksum,
                    session_ref=session_ref,
                ),
                "contentFollowCommand": follow_command(
                    "", checksum=checksum, session_ref=session_ref
                ),
                "artifactFollowCommand": (
                    session_read_command(
                        artifact_human_locator(
                            str(entry.get("preferred_name", "")).strip() or "data",
                            checksum,
                        ),
                        session_ref=session_ref,
                    )
                    if checksum
                    else ""
                ),
                "plugins": list(entry.get("plugins") or []),
                "actors": list(entry.get("actors") or []),
                "locators": list(entry.get("locators") or []),
                "artifactKinds": list(entry.get("artifactKinds") or []),
                "hitCount": len(hits),
                "snippetCount": len(snippets),
                "displayLineCount": len(lines),
                "snippets": snippets,
                **resolved_visibility_metadata(
                    entry,
                    provider=str(entry.get("plugin") or ""),
                    plugin=str(entry.get("plugin") or ""),
                    subcommand=str(entry.get("subcommand") or ""),
                    locator=str(
                        entry.get("canonical_locator", "") or entry.get("locator", "")
                    ).strip(),
                ),
            }
        )
    ordered = sorted(
        matches,
        key=lambda entry: str(
            entry.get("canonical_locator", "") or entry.get("locator", "")
        ),
    )
    ordered = sorted(
        ordered,
        key=lambda entry: str(
            entry.get("lastFetchedAt") or entry.get("fetched_at") or ""
        ),
        reverse=True,
    )
    ordered = sorted(
        ordered,
        key=lambda entry: int(entry.get("hitCount") or 0),
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


def cmd_scan(args: argparse.Namespace) -> int:
    dirs = session_dirs_for_read(args)
    require_started_session(dirs)
    session_ref = explicit_session_ref(args)
    payload = scan_payload(
        dirs,
        query=str(args.query or ""),
        plugin=str(args.plugin or ""),
        actor=str(args.actor or ""),
        locator=str(args.locator or ""),
        kind=str(args.kind or ""),
        match_mode=str(args.match or "literal"),
        case_sensitive=bool(args.case_sensitive),
        context=max(int(args.context or 0), 0),
        snippet_limit=max(int(args.snippets or 0), 0) or 1,
        limit=max(int(args.limit or 0), 0),
        offset=max(int(args.offset or 0), 0),
        include_all=bool(args.all),
        session_ref=session_ref,
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"scan: {payload['manifestPath']}")
    print(f"query: {payload['query']}")
    print(
        f"matches: {payload['entryCount']} artifact(s); "
        f"showing {payload['shownCount']}; "
        f"match={payload['matchMode']}; "
        f"case_sensitive={str(payload['caseSensitive']).lower()}"
    )
    print(
        paging_summary_line(
            label="page",
            total_count=int(payload["totalCount"]),
            shown_count=int(payload["shownCount"]),
            offset=int(payload["offset"]),
            next_offset=(
                int(payload["nextOffset"])
                if payload.get("nextOffset") is not None
                else None
            ),
        )
    )
    if not payload["entries"]:
        print("no projected materialized artifact matched the current scan")
        return 0
    for entry in payload["entries"]:
        plugins = ", ".join(
            str(value) for value in entry.get("plugins") or [] if str(value)
        )
        actors = ", ".join(
            str(value) for value in entry.get("actors") or [] if str(value)
        )
        locator_value = (
            str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip()
            or "unknown"
        )
        preferred_name = str(entry.get("preferred_name", "")).strip() or "data"
        print(
            f"- [{entry.get('artifactKind') or 'artifact'}; hits {entry.get('hitCount') or 0}] "
            f"{locator_value} -> {preferred_name}"
        )
        if plugins:
            print(f"  plugins: {plugins}")
        if actors:
            print(f"  actors: {actors}")
        visibility = visibility_summary(entry)
        if visibility:
            print(f"  visibility: {visibility}")
        print(
            "  stored: "
            + ", ".join(
                part
                for part in (
                    f"`{entry.get('artifactLocator')}`"
                    if entry.get("artifactLocator")
                    else "",
                    f"`{entry.get('contentLocator')}`"
                    if entry.get("contentLocator")
                    else "",
                )
                if part
            )
        )
        print(
            f"  follow: `{entry.get('artifactFollowCommand') or entry.get('followCommand')}`"
        )
        for snippet in entry.get("snippets") or []:
            print(
                f"  snippet {snippet.get('startLine')}-{snippet.get('endLine')}"
                f" (hits {', '.join(str(value) for value in snippet.get('hitLines') or [])})"
            )
            for line in snippet.get("lines") or []:
                print(f"    {line['number']:>5} | {line['text']}")
    return 0
