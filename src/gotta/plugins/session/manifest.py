"""Manifest surface for `gotta session`."""

from __future__ import annotations

import argparse
import json

from gotta.content.path import content_locator
from gotta.source import best_visibility_metadata

from .core import (
    MANIFEST_TEXT_PREVIEW_LIMIT,
    append_count_section,
    artifact_human_locator,
    artifact_kind,
    artifact_kind_counts,
    compile_filter_pattern,
    filter_suffix,
    follow_command,
    match_any,
    match_filter_text,
    paging_summary_line,
    paginate_items,
    rendered_actor,
    resolved_visibility_metadata,
    session_read_command,
    top_count_records,
    visibility_summary,
)
from .parse import explicit_session_ref, require_started_session, session_dirs_for_read


def manifest_entries(dirs) -> list[dict[str, object]]:
    manifest_path = dirs.content_dir / "manifest.jsonl"
    if not manifest_path.exists():
        return []
    entries: list[dict[str, object]] = []
    for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        payload = json.loads(raw_line)
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


def filter_manifest_entries(
    entries: list[dict[str, object]],
    *,
    plugin: str = "",
    actor: str = "",
    locator: str = "",
) -> list[dict[str, str]]:
    plugin_filter = plugin.strip()
    actor_filter = actor.strip()
    locator_filter = locator.strip()
    filtered: list[dict[str, object]] = []
    for entry in entries:
        if plugin_filter and entry.get("plugin", "") != plugin_filter:
            continue
        if actor_filter and entry.get("actor", "") != actor_filter:
            continue
        if locator_filter:
            canonical = entry.get("canonical_locator", "")
            raw = entry.get("locator", "")
            if locator_filter not in canonical and locator_filter not in raw:
                continue
        filtered.append(entry)
    return filtered


def _manifest_entry_matches(
    entry: dict[str, object],
    pattern,
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


def manifest_entry_sort_key(entry: dict[str, object]) -> tuple[str, str, str]:
    return (
        entry.get("fetched_at", ""),
        entry.get("canonical_locator", "") or entry.get("locator", ""),
        entry.get("checksum", ""),
    )


def manifest_identity_locator(entry: dict[str, object]) -> str:
    checksum = str(entry.get("checksum", "")).strip()
    locator = str(
        entry.get("canonical_locator", "") or entry.get("locator", "")
    ).strip()
    if locator:
        return locator
    return content_locator(checksum) if checksum else ""


def aggregate_manifest_entries(
    entries: list[dict[str, object]],
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for entry in entries:
        checksum = str(entry.get("checksum", "")).strip()
        locator = manifest_identity_locator(entry)
        key = (locator, checksum)
        state = grouped.setdefault(
            key,
            {
                "latest": entry,
                "fetchCount": 0,
                "firstFetchedAt": "",
                "lastFetchedAt": "",
                "plugins": set(),
                "actors": set(),
                "locators": set(),
                "artifactKinds": set(),
                "visibility": {},
            },
        )
        fetched_at = str(entry.get("fetched_at", "")).strip()
        latest = state["latest"]
        if manifest_entry_sort_key(entry) >= manifest_entry_sort_key(latest):
            state["latest"] = entry
        state["fetchCount"] = int(state["fetchCount"]) + 1
        if fetched_at and (
            not state["firstFetchedAt"] or fetched_at < state["firstFetchedAt"]
        ):
            state["firstFetchedAt"] = fetched_at
        if fetched_at and (
            not state["lastFetchedAt"] or fetched_at > state["lastFetchedAt"]
        ):
            state["lastFetchedAt"] = fetched_at
        plugin = str(entry.get("plugin", "")).strip()
        actor = str(entry.get("actor", "")).strip()
        raw_locator = str(entry.get("locator", "")).strip()
        kind = artifact_kind(entry.get("artifact_kind") or entry.get("artifactKind"))
        if plugin:
            state["plugins"].add(plugin)
        if actor:
            state["actors"].add(actor)
        if raw_locator:
            state["locators"].add(raw_locator)
        if locator:
            state["locators"].add(locator)
        if kind:
            state["artifactKinds"].add(kind)
        state["visibility"] = best_visibility_metadata(
            state.get("visibility", {}),
            resolved_visibility_metadata(
                entry,
                provider=plugin,
                plugin=plugin,
                subcommand=str(entry.get("subcommand") or ""),
                locator=locator,
            ),
        )
    aggregated: list[dict[str, object]] = []
    for (locator, _checksum), state in grouped.items():
        latest = dict(state["latest"])
        latest["canonical_locator"] = (
            str(latest.get("canonical_locator", "")).strip() or locator
        )
        latest["fetchCount"] = int(state["fetchCount"])
        latest["firstFetchedAt"] = str(state.get("firstFetchedAt") or "")
        latest["lastFetchedAt"] = str(state.get("lastFetchedAt") or "") or str(
            latest.get("fetched_at", "") or ""
        )
        latest["fetched_at"] = latest["lastFetchedAt"]
        latest["plugins"] = sorted(str(value) for value in state["plugins"])
        latest["actors"] = sorted(str(value) for value in state["actors"])
        latest["locators"] = sorted(str(value) for value in state["locators"])
        latest["artifactKinds"] = sorted(str(value) for value in state["artifactKinds"])
        latest.update(best_visibility_metadata(state.get("visibility", {})))
        aggregated.append(latest)
    return aggregated


def manifest_payload(
    dirs,
    *,
    plugin: str = "",
    actor: str = "",
    locator: str = "",
    filter_query: str = "",
    limit: int = 20,
    offset: int = 0,
    include_all: bool = False,
    session_ref: str = "",
) -> dict[str, object]:
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
        {
            **entry,
            "artifactKind": artifact_kind(entry.get("artifact_kind")),
            "content_locator": content_locator(str(entry.get("checksum", "")).strip())
            if str(entry.get("checksum", "")).strip()
            else "",
            "fetchCount": int(entry.get("fetchCount") or 0),
            "firstFetchedAt": str(entry.get("firstFetchedAt") or ""),
            "lastFetchedAt": str(entry.get("lastFetchedAt") or ""),
            "plugins": list(entry.get("plugins") or []),
            "actors": list(entry.get("actors") or []),
            "locators": list(entry.get("locators") or []),
            "artifactKinds": list(entry.get("artifactKinds") or []),
            "artifact_locator": artifact_human_locator(
                str(entry.get("preferred_name", "")).strip() or "data",
                str(entry.get("checksum", "")).strip(),
            ),
            "follow_command": follow_command(
                str(
                    entry.get("canonical_locator", "") or entry.get("locator", "")
                ).strip(),
                checksum=str(entry.get("checksum", "")).strip(),
                session_ref=session_ref,
            ),
            "content_follow_command": follow_command(
                "",
                checksum=str(entry.get("checksum", "")).strip(),
                session_ref=session_ref,
            )
            if str(entry.get("checksum", "")).strip()
            else "",
            "artifact_follow_command": (
                session_read_command(
                    artifact_human_locator(
                        str(entry.get("preferred_name", "")).strip() or "data",
                        str(entry.get("checksum", "")).strip(),
                    ),
                    session_ref=session_ref,
                )
                if str(entry.get("checksum", "")).strip()
                else ""
            ),
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
        for entry in paged
    ]
    fetch_record_count = sum(int(entry.get("fetchCount") or 0) for entry in ordered)
    top_plugins = top_count_records(
        [
            str(plugin_name).strip()
            for entry in ordered
            for plugin_name in list(entry.get("plugins") or [])
            if str(plugin_name).strip()
        ],
        key="plugin",
    )
    top_actors = top_count_records(
        [
            str(actor_name).strip()
            for entry in ordered
            for actor_name in list(entry.get("actors") or [])
            if str(actor_name).strip()
        ],
        key="actor",
    )
    return {
        "sessionDir": str(dirs.session_dir),
        "contentDir": str(dirs.content_dir),
        "manifestPath": str(dirs.content_dir / "manifest.jsonl"),
        "entryCount": len(entries),
        "fetchRecordCount": fetch_record_count,
        **paging,
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


def cmd_manifest(args: argparse.Namespace) -> int:
    dirs = session_dirs_for_read(args)
    require_started_session(dirs)
    session_ref = explicit_session_ref(args)
    payload = manifest_payload(
        dirs,
        plugin=args.plugin or "",
        actor=args.actor or "",
        locator=args.locator or "",
        filter_query=str(getattr(args, "filter", "") or ""),
        session_ref=session_ref,
        limit=args.limit,
        offset=max(args.offset, 0),
        include_all=bool(args.all),
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"manifest: {payload['manifestPath']}")
    print(
        "entries: "
        f"{payload['entryCount']} canonical (from {payload['fetchRecordCount']} fetches; "
        f"showing {payload['shownCount']}; "
        f"discovery {payload['discoveryArtifactCount']}, "
        f"evidence {payload['evidenceArtifactCount']})"
        f"{filter_suffix(payload.get('filter'))}"
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
    if int(payload["shownCount"]) == 0 and int(payload["totalCount"]) > 0:
        print("page: no results in this page window")
    top_plugins_lines: list[str] = []
    append_count_section(
        top_plugins_lines,
        heading="top plugins",
        records=list(payload.get("topPlugins") or []),
        key="plugin",
    )
    if top_plugins_lines:
        print("\n".join(top_plugins_lines))
    top_actors_lines: list[str] = []
    append_count_section(
        top_actors_lines,
        heading="top actors",
        records=list(payload.get("topActors") or []),
        key="actor",
    )
    if top_actors_lines:
        print("\n".join(top_actors_lines))
    if session_ref:
        print(
            f"follow: use emitted locators with `gotta read --session {session_ref} <locator>`"
        )
    preview_entries = list(payload["entries"])[:MANIFEST_TEXT_PREVIEW_LIMIT]
    if payload["entries"]:
        print(
            "entries preview:"
            if len(payload["entries"]) <= MANIFEST_TEXT_PREVIEW_LIMIT
            else f"entries preview (showing {len(preview_entries)} of {len(payload['entries'])}):"
        )
    for entry in preview_entries:
        fetched_at = str(entry.get("fetched_at", "")).strip() or "unknown-time"
        plugin_list = [
            str(value).strip()
            for value in list(entry.get("plugins") or [])
            if str(value).strip()
        ]
        actor_list = [
            str(value).strip()
            for value in list(entry.get("actors") or [])
            if str(value).strip()
        ]
        plugin = (
            ", ".join(plugin_list)
            or str(entry.get("plugin", "")).strip()
            or "unknown-plugin"
        )
        actor = ", ".join(actor_list) or rendered_actor(
            entry.get("actor"), session_root=dirs.session_dir
        )
        locator = (
            str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip()
            or "unknown"
        )
        preferred_name = str(entry.get("preferred_name", "")).strip() or "data"
        checksum = str(entry.get("checksum", "")).strip()
        short = checksum[:12] if checksum else "unknown"
        print(
            f"- {fetched_at} [{plugin}/{actor}] {locator} -> {preferred_name} ({short})"
        )
        if int(entry.get("fetchCount") or 0) > 1:
            print(
                "  fetches: "
                f"{int(entry.get('fetchCount') or 0)} "
                f"(first {entry.get('firstFetchedAt') or 'unknown-time'}; "
                f"last {entry.get('lastFetchedAt') or 'unknown-time'})"
            )
        kind = str(entry.get("artifactKind") or "").strip()
        if kind:
            print(f"  artifact_kind: {kind}")
        visibility = visibility_summary(entry)
        if visibility:
            print(f"  visibility: {visibility}")
        if entry.get("artifact_locator") or entry.get("content_locator"):
            print(
                "  "
                + "stored: "
                + ", ".join(
                    part
                    for part in (
                        f"`{entry.get('artifact_locator')}`"
                        if entry.get("artifact_locator")
                        else "",
                        f"`{entry.get('content_locator')}`"
                        if entry.get("content_locator")
                        else "",
                    )
                    if part
                )
            )
    hidden = len(payload["entries"]) - len(preview_entries)
    if hidden > 0:
        print(f"  - ... {hidden} additional entries hidden in text view")
    return 0
