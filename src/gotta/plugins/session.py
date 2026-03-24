"""Shared session inspection helpers for gotta."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shlex
import urllib.parse
import uuid

from gotta.compat import UTC, datetime
from gotta import binding as binding_helpers
from gotta import session as sessionlib
from gotta import topology
from gotta.content import (
    ContentError,
    ContentSnapshot,
    CommonOptions,
    activity_events,
    activity_log_path,
    artifact_locator,
    content_locator,
    current_context_binding,
    env_mapping,
    resolve_dirs,
    scan_content_store,
    SESSION_ACTIVATION_ENV,
    SESSION_CREATED_ENV,
    SESSION_REPO_ENV,
    session_identity,
    session_is_initialized,
    shared_session_root,
    sh_quote,
    state_env_path,
    load_state_env_at_root,
    write_text_atomic,
    write_session_state,
)
from gotta.helptext import is_long_help_request, print_long_help
from gotta.leads import (
    aggregate_lead_sources,
    build_lead_edge_records,
    edge_best_first_sort_key,
    resolve_lead_snapshots,
    snapshot_artifact_locator,
    snapshot_display_name,
    snapshot_last_fetched_at,
    snapshot_locator,
)
from gotta.notes import actor_notes_ready
from gotta.source import (
    best_visibility_metadata,
    classify_visibility_metadata,
    normalize_visibility_metadata,
)

TIMELINE_MODE_ALIASES = {
    "acquisition": "acquired",
    "acquired": "acquired",
    "observed": "acquired",
    "best-effort": "best-effort",
    "created": "created",
    "updated": "updated",
}

TIMELINE_MODE_HELP = "chronology mode: acquired, created, updated, or best-effort"

TIMELINE_MODE_DESCRIPTIONS = {
    "acquired": (
        "Session activity chronology ordered by acquisition/recorded time across "
        "materialized source artifacts and native local mutations."
    ),
    "created": (
        "Source-authored chronology ordered strictly by persisted created/authored timestamps; "
        "events without created-time metadata are omitted and counted as coverage gaps."
    ),
    "updated": (
        "Source-authored chronology ordered strictly by persisted updated/modified timestamps; "
        "events without updated-time metadata are omitted and counted as coverage gaps."
    ),
    "best-effort": (
        "Best-effort source chronology using persisted source-created time first, then published, "
        "then updated when older fields are unavailable; native local mutations also appear with "
        "explicit local timestamp provenance instead of being treated as source-authored events."
    ),
}
AGGREGATE_SOURCE_SUBCOMMANDS = {
    "search",
    "status",
    "workspaces",
    "sql",
    "schema",
    "list-channels",
    "list-users",
    "cql",
    "jql",
}


def _fallback_actor(session_root: Path) -> str:
    return session_identity(session_root) or "unknown"


def _rendered_actor(raw: object, *, session_root: Path) -> str:
    value = str(raw or "").strip()
    return value or _fallback_actor(session_root)


def _state_file(root: Path) -> Path:
    return state_env_path(root)


def session_access_mode(argv: list[str]) -> str:
    positionals = sessionlib.argv_positionals(
        argv,
        valued_flags=(
            "--session",
            "--actor",
            "--content-dir",
            "--output",
            "--limit",
            "--offset",
        ),
    )
    subcommand = positionals[0] if positionals else "show"
    return "write" if subcommand in {"init", "bind"} else "read"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gotta session",
        description="Inspect the active session-rooted gotta context.",
    )
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init")
    bind = sub.add_parser("bind")
    show = sub.add_parser("show")
    doctor = sub.add_parser("doctor")
    manifest = sub.add_parser("manifest")
    timeline = sub.add_parser(
        "timeline",
        description=(
            "Inspect session chronology. Default mode is acquired order; "
            "explicit source-authored chronology is available through "
            "`--mode created`, `--mode updated`, or `--mode best-effort`."
        ),
    )
    graph = sub.add_parser("graph")
    analyze = sub.add_parser("analyze")
    scan = sub.add_parser(
        "scan",
        description=(
            "Search projected text across the materialized session corpus without "
            "dropping to shell-native grep tools."
        ),
    )
    leads = sub.add_parser(
        "leads",
        description=(
            "Inspect explicit, followable leads mined from already-materialized "
            "artifact content. Leads are shown best-first using observed "
            "signals such as native-ness, materialization, recurrence, and "
            "source context."
        ),
    )

    for parser_ in (init, show, doctor, manifest, timeline, graph, analyze, scan, leads):
        parser_.add_argument("--session", help="session root")
        parser_.add_argument("--actor", help="actor within the current session")
        parser_.add_argument("--content-dir", help="explicit content directory override")

    bind.add_argument(
        "session_id",
        nargs="?",
        help="session reference: shared session id, exact session root, or <session>/<actor>",
    )
    bind.add_argument("--output", choices=["summary", "json", "path"], default="summary")

    init.add_argument(
        "--output",
        "--print",
        dest="print_format",
        choices=["env", "json", "path", "sh"],
        default="path",
    )
    show.add_argument(
        "--output",
        "--print",
        dest="print_format",
        choices=["env", "json", "path", "sh"],
        default="path",
    )
    doctor.add_argument(
        "--output",
        "--print",
        dest="print_format",
        choices=["summary", "json"],
        default="json",
    )
    manifest.add_argument("--plugin")
    manifest.add_argument("--locator")
    manifest.add_argument("--limit", type=int, default=100)
    manifest.add_argument("--offset", type=int, default=0)
    manifest.add_argument("--all", action="store_true")
    manifest.add_argument("--output", choices=["json", "text"], default="text")
    manifest.add_argument("--stdout", action="store_true", help=argparse.SUPPRESS)
    timeline.add_argument("--limit", type=int, default=100)
    timeline.add_argument("--offset", type=int, default=0)
    timeline.add_argument("--all", action="store_true")
    timeline.add_argument("--output", choices=["json", "text"], default="text")
    timeline.add_argument("--stdout", action="store_true", help=argparse.SUPPRESS)
    timeline.add_argument(
        "--mode",
        default="acquired",
        metavar="MODE",
        help=TIMELINE_MODE_HELP,
    )
    graph.add_argument(
        "--output",
        choices=["mermaid", "json"],
        default="mermaid",
        help="render the content graph as Mermaid or structured JSON",
    )
    analyze.add_argument(
        "--output",
        choices=["text", "mermaid", "json"],
        default="text",
        help="render an analysis overview, Mermaid graph, or structured JSON",
    )
    analyze.add_argument(
        "--mode",
        choices=["lineage", "semantic", "all"],
        default="all",
        help="write lineage content, semantic content, or both",
    )
    analyze.add_argument(
        "--focus",
        help="narrow the analysis view to one keyword, locator, artifact name, or checksum prefix",
    )
    analyze.add_argument(
        "--limit",
        type=int,
        default=8,
        help="maximum items to show per overview/focus section",
    )
    analyze.add_argument("--stdout", action="store_true")
    scan.add_argument("query", help="literal text or regex to search for in stored projections")
    scan.add_argument("--plugin")
    scan.add_argument("--locator")
    scan.add_argument("--kind", choices=["discovery", "evidence"])
    scan.add_argument("--match", choices=["literal", "regex"], default="literal")
    scan.add_argument("--case-sensitive", action="store_true")
    scan.add_argument("--context", type=int, default=2)
    scan.add_argument("--snippets", type=int, default=3)
    scan.add_argument("--limit", type=int, default=20)
    scan.add_argument("--offset", type=int, default=0)
    scan.add_argument("--all", action="store_true")
    scan.add_argument("--output", choices=["json", "text"], default="text")
    leads.add_argument("target", nargs="?")
    leads.add_argument("--limit", type=int, default=100)
    leads.add_argument("--offset", type=int, default=0)
    leads.add_argument("--all", action="store_true")
    leads.add_argument("--output", choices=["json", "text"], default="text")
    return parser


def _options_from_args(
    args: argparse.Namespace
) -> CommonOptions:
    return CommonOptions(
        session_dir=getattr(args, "session", None),
        content_dir=getattr(args, "content_dir", None),
        actor=getattr(args, "actor", None),
    )

def _require_started_session(dirs) -> None:
    state_file = _state_file(dirs.session_dir)
    if not session_is_initialized(dirs.session_dir):
        raise ContentError(
            "start or bind a session first with `gotta ...` or bootstrap one with "
            "`gotta session init`; "
            f"missing {state_file}"
        )


def _print_dirs(args: argparse.Namespace, payload: dict[str, str]) -> int:
    print_format = getattr(args, "print_format", "path")
    if print_format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if print_format == "path":
        print(payload["GOTTA_SESSION_DIR"])
        return 0
    if print_format == "env":
        for key, value in payload.items():
            print(f"{key}={value}")
        return 0
    lines = [f"export {key}={sh_quote(value)}" for key, value in payload.items()]
    print("\n".join(lines))
    return 0


def _paginate_items(
    items: list[dict[str, object]] | list[dict[str, str]],
    *,
    limit: int,
    offset: int,
    include_all: bool,
    default_tail_window: bool = False,
) -> tuple[list[dict[str, object]] | list[dict[str, str]], dict[str, object]]:
    total_count = len(items)
    normalized_limit = max(limit, 0)
    explicit_offset = max(offset, 0)
    applied_offset = explicit_offset
    if default_tail_window and not include_all and explicit_offset == 0 and normalized_limit > 0:
        applied_offset = max(total_count - normalized_limit, 0)
    if include_all:
        paged = items[applied_offset:]
        applied_limit: int | None = None
    else:
        paged = items[applied_offset : applied_offset + normalized_limit]
        applied_limit = normalized_limit
    shown_count = len(paged)
    next_offset = applied_offset + shown_count
    truncated = next_offset < total_count
    return paged, {
        "offset": applied_offset,
        "limit": applied_limit,
        "totalCount": total_count,
        "shownCount": shown_count,
        "nextOffset": next_offset if truncated else None,
        "truncated": truncated,
    }


def _paging_summary_line(
    *,
    label: str,
    total_count: int,
    shown_count: int,
    offset: int,
    next_offset: int | None,
) -> str:
    parts = [f"{label}: {total_count} total", f"showing {shown_count}", f"offset {offset}"]
    if next_offset is not None:
        parts.append(f"next {next_offset}")
    return "; ".join(parts)


def _binding_detail(record: dict[str, object]) -> str:
    context_id = str(record.get("contextId") or "").strip() or "unknown"
    context_source = str(record.get("contextSource") or "").strip() or "unknown"
    session_root = str(record.get("sessionRoot") or "").strip() or "unknown"
    return (
        f"{record.get('bindingId') or 'unknown'} -> {session_root} "
        f"({context_source}, {context_id})"
    )


def _doctor_payload(dirs) -> dict[str, object]:
    session_env = env_mapping(dirs)
    state = load_state_env_at_root(dirs.session_dir)
    runtime = current_context_binding()
    runtime_root = topology.resolve_binding(runtime.binding_id)
    bindings = topology.binding_records_for_session(dirs.session_dir)
    target_session_id = str(session_env.get("GOTTA_SESSION_ID") or "")
    target_shared_root = shared_session_root(target_session_id).resolve()
    session_payload = {
        "sessionId": target_session_id,
        "actor": session_identity(dirs.session_dir),
        "sessionRoot": str(dirs.session_dir),
        "contentRoot": str(dirs.content_dir),
        "initialized": bool(session_is_initialized(dirs.session_dir)),
        "repo": state.get(SESSION_REPO_ENV, ""),
        "createdAt": state.get(SESSION_CREATED_ENV, ""),
    }
    runtime_payload = {
        "present": bool(runtime.context_id),
        "contextId": runtime.context_id,
        "contextSource": runtime.context_source,
        "bindingId": runtime.binding_id,
    }
    session_in_shared_topology = (
        topology.parse_grouped_session_root(dirs.session_dir.resolve()) is not None
        or topology.parse_shared_session_root(dirs.session_dir.resolve()) is not None
    )
    matching_runtime_binding = (
        runtime_root is not None
        and topology.shared_session_id(runtime_root) == target_session_id
    )
    bindings_match_target = all(
        topology.normalize_session_id(str(record.get("sessionId") or "")) == target_session_id
        and topology.shared_session_id(Path(str(record.get("sessionRoot") or target_shared_root)))
        == target_session_id
        for record in bindings
    )
    topology_consistent = (
        dirs.session_dir.exists()
        and dirs.content_dir.exists()
        and session_is_initialized(dirs.session_dir)
        and topology.shared_session_id(dirs.session_dir) == target_session_id
        and (
            not session_in_shared_topology
            or dirs.content_dir.resolve() == (target_shared_root / "content").resolve()
        )
        and bool(bindings)
        and bindings_match_target
    )
    checks = {
        "runtimeContextPresent": {
            "status": "ok" if runtime_payload["present"] else "missing",
            "detail": (
                f"{runtime.context_source}: {runtime.context_id}"
                if runtime_payload["present"]
                else "no active runtime context"
            ),
        },
        "durableBindingsPresent": {
            "status": "ok" if bindings else "missing",
            "detail": (
                ", ".join(_binding_detail(record) for record in bindings)
                if bindings
                else "no durable bindings target this session"
            ),
        },
        "runtimeBindingMatchesTarget": {
            "status": (
                "ok"
                if matching_runtime_binding
                else "mismatch"
                if runtime_payload["present"]
                else "unknown"
            ),
            "detail": (
                f"{runtime.binding_id} targets session "
                f"{topology.shared_session_id(runtime_root)} at {runtime_root}"
                if runtime_root is not None
                else "the active runtime binding has no durable target"
            )
            if runtime_payload["present"]
            else "no active runtime context",
        },
        "sessionTopologyConsistent": {
            "status": "ok" if topology_consistent else "broken",
            "detail": (
                "session root, content root, and binding records agree"
                if topology_consistent
                else "session root, content root, and durable binding records do not fully agree"
            ),
        },
    }
    return {
        "runtime": runtime_payload,
        "session": session_payload,
        "bindings": bindings,
        "checks": checks,
    }


def _print_doctor_summary(payload: dict[str, object]) -> int:
    runtime = payload["runtime"]
    session_payload = payload["session"]
    print(f"session: {session_payload['sessionRoot']}")
    print(f"session_id: {session_payload['sessionId']}")
    print(f"actor: {session_payload['actor']}")
    print(f"content: {session_payload['contentRoot']}")
    print(
        "runtime: "
        f"{runtime['contextSource'] or 'unknown'} "
        f"{runtime['contextId'] or 'unknown'} "
        f"({runtime['bindingId'] or 'unknown'})"
    )
    print(f"bindings: {len(payload['bindings'])}")
    for name in (
        "runtimeContextPresent",
        "durableBindingsPresent",
        "runtimeBindingMatchesTarget",
        "sessionTopologyConsistent",
    ):
        check = payload["checks"][name]
        print(f"- {name}: {check['status']} - {check['detail']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    dirs = resolve_dirs(_options_from_args(args), create=False)
    _require_started_session(dirs)
    return _print_dirs(args, env_mapping(dirs))


def cmd_bind(args: argparse.Namespace) -> int:
    return binding_helpers.bind_current_context(
        session_ref=getattr(args, "session_id", None),
        output=getattr(args, "output", "summary"),
    )


def cmd_init(args: argparse.Namespace) -> int:
    dirs = resolve_dirs(_options_from_args(args), create=True)
    current = dirs.session_dir.resolve()
    write_session_state(
        dirs,
        {
            SESSION_CREATED_ENV: load_state_env_at_root(current).get(SESSION_CREATED_ENV, "")
            or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            SESSION_ACTIVATION_ENV: "manual",
        },
    )
    dirs.session_dir.joinpath("bin").mkdir(parents=True, exist_ok=True)
    from gotta import session as sessionlib

    sessionlib.scaffold_session(current)
    return _print_dirs(args, env_mapping(dirs))


def cmd_doctor(args: argparse.Namespace) -> int:
    dirs = resolve_dirs(_options_from_args(args), create=False)
    _require_started_session(dirs)
    payload = _doctor_payload(dirs)
    if getattr(args, "print_format", "json") == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    return _print_doctor_summary(payload)


def _manifest_entries(dirs) -> list[dict[str, object]]:
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


def _filter_manifest_entries(
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


def _manifest_entry_sort_key(entry: dict[str, object]) -> tuple[str, str, str]:
    return (
        entry.get("fetched_at", ""),
        entry.get("canonical_locator", "") or entry.get("locator", ""),
        entry.get("checksum", ""),
    )


def _manifest_identity_locator(entry: dict[str, object]) -> str:
    checksum = str(entry.get("checksum", "")).strip()
    locator = str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip()
    if locator:
        return locator
    return content_locator(checksum) if checksum else ""


def _aggregate_manifest_entries(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for entry in entries:
        checksum = str(entry.get("checksum", "")).strip()
        locator = _manifest_identity_locator(entry)
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
        if _manifest_entry_sort_key(entry) >= _manifest_entry_sort_key(latest):
            state["latest"] = entry
        state["fetchCount"] = int(state["fetchCount"]) + 1
        if fetched_at and (not state["firstFetchedAt"] or fetched_at < state["firstFetchedAt"]):
            state["firstFetchedAt"] = fetched_at
        if fetched_at and (not state["lastFetchedAt"] or fetched_at > state["lastFetchedAt"]):
            state["lastFetchedAt"] = fetched_at
        plugin = str(entry.get("plugin", "")).strip()
        actor = str(entry.get("actor", "")).strip()
        raw_locator = str(entry.get("locator", "")).strip()
        kind = _artifact_kind(entry.get("artifact_kind") or entry.get("artifactKind"))
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
            _resolved_visibility_metadata(
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
        latest["canonical_locator"] = str(latest.get("canonical_locator", "")).strip() or locator
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


def _follow_command(locator: str, *, checksum: str = "") -> str:
    target = locator.strip() or (content_locator(checksum.strip()) if checksum.strip() else "unknown")
    return f"gotta read {sh_quote(target)}"


def _artifact_human_locator(preferred_name: str, checksum: str) -> str:
    if not checksum.strip():
        return ""
    return artifact_locator(preferred_name or "data", checksum)


def _visibility_summary(payload: dict[str, object] | dict[str, str]) -> str:
    visibility = normalize_visibility_metadata(payload)
    if not visibility:
        return ""
    return (
        f"{visibility['visibility_level']} "
        f"({visibility['visibility_boundary']}, {visibility['visibility_confidence']})"
    )


def _resolved_visibility_metadata(
    payload: dict[str, object] | dict[str, str],
    *,
    provider: str = "",
    plugin: str = "",
    subcommand: str = "",
    locator: str = "",
) -> dict[str, object]:
    existing = normalize_visibility_metadata(payload)
    classification_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"visibility_level", "visibility_boundary", "visibility_confidence", "visibility_basis"}
    }
    classified = classify_visibility_metadata(
        classification_payload,
        provider=provider,
        plugin=plugin,
        subcommand=subcommand,
        locator=locator,
    )
    return best_visibility_metadata(existing, classified)


def _artifact_kind(raw: object) -> str:
    value = str(raw or "").strip().lower()
    return value if value in {"discovery", "evidence"} else ""


def _artifact_kind_counts(records: list[dict[str, object]]) -> tuple[int, int]:
    discovery = 0
    evidence = 0
    for record in records:
        kind = _artifact_kind(record.get("artifact_kind") or record.get("artifactKind"))
        if kind == "discovery":
            discovery += 1
        elif kind == "evidence":
            evidence += 1
    return discovery, evidence


def _artifact_kind_label(record: dict[str, object]) -> str:
    return _artifact_kind(record.get("artifact_kind") or record.get("artifactKind"))


def _topology_next_step(*, discovery_count: int, evidence_count: int) -> str:
    if discovery_count <= 0 and evidence_count <= 0:
        return (
            "No materialized artifacts yet. That is normal before first retrieval. "
            "Materialize one strong anchor with a provider-native search/get command "
            "or `gotta read <locator>`, then revisit manifest, timeline, leads, graph, "
            "and analyze."
        )
    if evidence_count <= 0 and discovery_count > 0:
        return (
            "Discovery artifacts are present, but no evidence artifacts exist yet. "
            "Follow one strong locator with a provider-native get command or "
            "`gotta read <locator>` to land evidence in the shared session web."
        )
    return ""


def _manifest_payload(
    dirs,
    *,
    plugin: str = "",
    actor: str = "",
    locator: str = "",
    limit: int = 20,
    offset: int = 0,
    include_all: bool = False,
) -> dict[str, object]:
    raw_entries = _filter_manifest_entries(
        _manifest_entries(dirs),
        plugin=plugin,
        actor=actor,
        locator=locator,
    )
    entries = _aggregate_manifest_entries(raw_entries)
    ordered = sorted(entries, key=_manifest_entry_sort_key, reverse=True)
    discovery_count, evidence_count = _artifact_kind_counts(ordered)
    paged, paging = _paginate_items(
        ordered,
        limit=limit,
        offset=offset,
        include_all=include_all,
    )
    rendered_entries = [
        {
            **entry,
            "artifactKind": _artifact_kind(entry.get("artifact_kind")),
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
            "artifact_locator": _artifact_human_locator(
                str(entry.get("preferred_name", "")).strip() or "data",
                str(entry.get("checksum", "")).strip(),
            ),
            "follow_command": _follow_command(
                str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip(),
                checksum=str(entry.get("checksum", "")).strip(),
            ),
            "content_follow_command": _follow_command(
                "",
                checksum=str(entry.get("checksum", "")).strip(),
            )
            if str(entry.get("checksum", "")).strip()
            else "",
            "artifact_follow_command": (
                f"gotta read {sh_quote(_artifact_human_locator(str(entry.get('preferred_name', '')).strip() or 'data', str(entry.get('checksum', '')).strip()))}"
                if str(entry.get("checksum", "")).strip()
                else ""
            ),
            **_resolved_visibility_metadata(
                entry,
                provider=str(entry.get("plugin") or ""),
                plugin=str(entry.get("plugin") or ""),
                subcommand=str(entry.get("subcommand") or ""),
                locator=str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip(),
            ),
        }
        for entry in paged
    ]
    return {
        "sessionDir": str(dirs.session_dir),
        "contentDir": str(dirs.content_dir),
        "manifestPath": str(dirs.content_dir / "manifest.jsonl"),
        "entryCount": len(entries),
        "fetchRecordCount": len(raw_entries),
        **paging,
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "pluginFilter": plugin,
        "actorFilter": actor,
        "locatorFilter": locator,
        "entries": rendered_entries,
    }


def _scan_entries(
    entries: list[dict[str, object]],
    *,
    plugin: str = "",
    actor: str = "",
    locator: str = "",
    kind: str = "",
) -> list[dict[str, object]]:
    filtered = _filter_manifest_entries(
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
        if _artifact_kind(entry.get("artifact_kind") or entry.get("artifactKind")) == kind_filter
    ]


def _scan_hit_lines(
    lines: list[str],
    *,
    query: str,
    match_mode: str,
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


def _scan_display_text(snapshot: ContentSnapshot) -> str:
    from gotta.plugins import read as read_plugin

    display, _language = read_plugin.stored_display(snapshot.data_path)
    return display.decode("utf-8", errors="replace")


def _scan_payload(
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
) -> dict[str, object]:
    regex = _scan_regex(
        query,
        match_mode=match_mode,
        case_sensitive=case_sensitive,
    )
    raw_entries = _scan_entries(
        _manifest_entries(dirs),
        plugin=plugin,
        actor=actor,
        locator=locator,
        kind=kind,
    )
    entries = _aggregate_manifest_entries(raw_entries)
    snapshots = {snapshot.digest: snapshot for snapshot in scan_content_store(dirs.content_dir)}
    matches: list[dict[str, object]] = []
    for entry in entries:
        checksum = str(entry.get("checksum", "")).strip()
        snapshot = snapshots.get(checksum)
        if snapshot is None:
            continue
        text = _scan_display_text(snapshot)
        lines = text.splitlines()
        hits = _scan_hit_lines(
            lines,
            query=query,
            match_mode=match_mode,
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
        match_record = {
            **entry,
            "artifactKind": _artifact_kind(entry.get("artifact_kind")),
            "artifactLocator": _artifact_human_locator(
                str(entry.get("preferred_name", "")).strip() or "data",
                checksum,
            ),
            "contentLocator": content_locator(checksum),
            "followCommand": _follow_command(
                str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip(),
                checksum=checksum,
            ),
            "contentFollowCommand": _follow_command("", checksum=checksum),
            "artifactFollowCommand": (
                f"gotta read {sh_quote(_artifact_human_locator(str(entry.get('preferred_name', '')).strip() or 'data', checksum))}"
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
            **_resolved_visibility_metadata(
                entry,
                provider=str(entry.get("plugin") or ""),
                plugin=str(entry.get("plugin") or ""),
                subcommand=str(entry.get("subcommand") or ""),
                locator=str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip(),
            ),
        }
        matches.append(match_record)
    ordered = sorted(
        matches,
        key=lambda entry: str(entry.get("canonical_locator", "") or entry.get("locator", "")),
    )
    ordered = sorted(
        ordered,
        key=lambda entry: str(entry.get("lastFetchedAt") or entry.get("fetched_at") or ""),
        reverse=True,
    )
    ordered = sorted(
        ordered,
        key=lambda entry: int(entry.get("hitCount") or 0),
        reverse=True,
    )
    paged, paging = _paginate_items(
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


def _parse_source_timestamp(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if re.fullmatch(r"\d{10}\.\d{6}", value):
        seconds, micros = value.split(".", 1)
        dt = datetime.fromtimestamp(int(seconds), tz=UTC).replace(microsecond=int(micros))
        return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _source_timestamps(snapshot: ContentSnapshot) -> dict[str, str]:
    metadata = snapshot.metadata
    aliases = (
        ("source_published_at", "source_published_at"),
        ("source_updated_at", "source_updated_at"),
        ("source_created_at", "source_created_at"),
        ("published_at", "source_published_at"),
        ("updated_at", "source_updated_at"),
        ("updated", "source_updated_at"),
        ("modifiedTime", "source_updated_at"),
        ("createdTime", "source_created_at"),
        ("authored_at", "source_created_at"),
        ("author_date", "source_created_at"),
        ("created", "source_created_at"),
        ("timestamp", "source_created_at"),
    )
    timestamps: dict[str, str] = {}
    for key, normalized in aliases:
        parsed = _parse_source_timestamp(str(metadata.get(key) or ""))
        if parsed and normalized not in timestamps:
            timestamps[normalized] = parsed
    return timestamps


def _normalize_timeline_mode(mode: str) -> str:
    normalized = TIMELINE_MODE_ALIASES.get(mode.strip().lower(), "")
    if normalized:
        return normalized
    choices = ", ".join(sorted({"acquired", "best-effort", "created", "updated"}))
    raise SystemExit(f"invalid timeline mode: {mode}. expected one of: {choices}")


def _source_timestamp_for_mode(snapshot: ContentSnapshot, mode: str) -> tuple[str | None, str]:
    if _is_aggregate_source_snapshot(snapshot):
        return None, ""
    timestamps = _source_timestamps(snapshot)
    if mode == "created":
        value = timestamps.get("source_created_at", "")
        return (value or None, "source_created_at" if value else "")
    if mode == "updated":
        value = timestamps.get("source_updated_at", "")
        return (value or None, "source_updated_at" if value else "")
    if mode == "best-effort":
        for key in ("source_created_at", "source_published_at", "source_updated_at"):
            value = timestamps.get(key, "")
            if value:
                return value, key
        return None, ""
    return None, ""


def _strip_read_view_flags(locator: str) -> str:
    try:
        parts = shlex.split(locator)
    except ValueError:
        parts = locator.split()
    cleaned: list[str] = []
    index = 0
    while index < len(parts):
        token = parts[index]
        if token in {"--head", "--tail", "--section"}:
            index += 2
            continue
        if any(token.startswith(f"{flag}=") for flag in ("--head", "--tail", "--section")):
            index += 1
            continue
        cleaned.append(token)
        index += 1
    return " ".join(cleaned).strip()


def _counts_as_source_coverage_gap(snapshot: ContentSnapshot) -> bool:
    if _is_aggregate_source_snapshot(snapshot):
        return False
    plugin = str(snapshot.metadata.get("plugin", "")).strip() or "unknown-plugin"
    locator = snapshot_locator(snapshot)
    if plugin == "read":
        base = _strip_read_view_flags(locator)
        if base.startswith(("artifact:", "content:")):
            return False
        if not re.match(r"^(?:https?://|[a-z][a-z0-9+.-]*:)", base):
            return False
    if plugin == "slack" and locator.startswith("slack:sql "):
        if re.search(r"\bPRAGMA\s+table_info\b|\bsqlite_master\b", locator, re.IGNORECASE):
            return False
    return True


LOCAL_TIMELINE_FILES = (
    "WANT.md",
    "GOAL.md",
    "BRIEF.md",
)


def _iso_utc_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _is_aggregate_source_snapshot(snapshot: ContentSnapshot) -> bool:
    subcommand = str(snapshot.metadata.get("subcommand") or "").strip().lower()
    if subcommand in AGGREGATE_SOURCE_SUBCOMMANDS:
        return True
    locator = snapshot_locator(snapshot)
    if re.search(
        r":(?:search|status|workspaces|sql|schema|list-channels|list-users)\b",
        locator,
    ):
        return True
    if re.search(
        r"^(?:search|status|workspaces|sql|schema|list-channels|list-users)\b",
        locator,
    ):
        return True
    return False


def _meaningful_local_surface(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.name == "NOTES.md":
        return actor_notes_ready(path.parents[2], path.parent.name)
    if path.name != "NOTES.md":
        return True
    return False


def _local_activity_timeline_events(dirs) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    seen_locators: set[str] = set()
    for raw in activity_events(dirs.session_dir):
        timestamp = str(raw.get("timestamp") or "").strip()
        if not timestamp:
            continue
        locator = str(raw.get("locator") or "").strip() or "unknown"
        seen_locators.add(locator)
        events.append(
            {
                "mode": "local",
                "source_time": timestamp,
                "source_time_field": str(raw.get("time_field") or "session_recorded_at"),
                "source_created_at": "",
                "source_updated_at": "",
                "source_published_at": "",
                "fetched_at": timestamp,
                "plugin": str(raw.get("plugin") or "session").strip() or "session",
                "actor": _rendered_actor(raw.get("actor"), session_root=dirs.session_dir),
                "target_actor": str(raw.get("target_actor") or "").strip(),
                "locator": locator,
                "preferred_name": str(raw.get("preferred_name") or locator).strip() or locator,
                "checksum": "",
                "artifactKind": "",
                "content_locator": "",
                "artifact_locator": "",
                "follow_command": str(raw.get("follow_command") or "").strip(),
                "detail": str(raw.get("detail") or "").strip(),
                "event_kind": "local",
                **classify_visibility_metadata(
                    {},
                    provider="gotta",
                    plugin=str(raw.get("plugin") or "session").strip() or "session",
                    locator=locator,
                ),
            }
        )
    candidates: list[tuple[str, Path, str, str]] = [
        ("session", dirs.session_dir / relative, relative, f"gotta read {relative!r}")
        for relative in LOCAL_TIMELINE_FILES
    ]
    for sibling in sorted(dirs.session_dir.parent.glob("*/NOTES.md")):
        if sibling.parent.resolve() == dirs.session_dir.resolve():
            continue
        actor = sibling.parent.name
        locator = f"actor:{actor}:notes"
        follow = f"gotta read 'NOTES.md' --actor {actor}"
        candidates.append(("actor", sibling, locator, follow))
    for plugin, path, locator, follow_command in candidates:
        if locator in seen_locators or not _meaningful_local_surface(path):
            continue
        timestamp = _iso_utc_from_timestamp(path.stat().st_mtime)
        actor = path.parent.name if plugin == "actor" else _fallback_actor(dirs.session_dir)
        events.append(
            {
                "mode": "local",
                "source_time": timestamp,
                "source_time_field": "filesystem_mtime",
                "source_created_at": "",
                "source_updated_at": "",
                "source_published_at": "",
                "fetched_at": timestamp,
                "plugin": plugin,
                "actor": actor,
                "target_actor": "",
                "locator": locator,
                "preferred_name": path.name,
                "checksum": "",
                "artifactKind": "",
                "content_locator": "",
                "artifact_locator": "",
                "follow_command": follow_command,
                "detail": "local surface snapshot",
                "event_kind": "local",
                **classify_visibility_metadata(
                    {},
                    provider="gotta",
                    plugin=plugin,
                    locator=locator,
                ),
            }
        )
    return events


def _timeline_payload(
    dirs,
    *,
    limit: int = 100,
    offset: int = 0,
    include_all: bool = False,
    mode: str = "acquired",
) -> dict[str, object]:
    normalized_mode = _normalize_timeline_mode(mode)
    local_events = _local_activity_timeline_events(dirs)
    if normalized_mode != "acquired":
        snapshots = scan_content_store(dirs.content_dir)
        events: list[dict[str, object]] = []
        coverage_gap_count = 0
        for snapshot in snapshots:
            locator = snapshot_locator(snapshot)
            source_time, source_field = _source_timestamp_for_mode(snapshot, normalized_mode)
            if not source_time:
                if _counts_as_source_coverage_gap(snapshot):
                    coverage_gap_count += 1
                continue
            fetched_at = snapshot.events[-1].timestamp if snapshot.events else ""
            source_timestamps = _source_timestamps(snapshot)
            events.append(
                {
                    "mode": "source",
                    "source_time": source_time,
                    "source_time_field": source_field,
                    "source_created_at": source_timestamps.get("source_created_at", ""),
                    "source_updated_at": source_timestamps.get("source_updated_at", ""),
                    "source_published_at": source_timestamps.get("source_published_at", ""),
                    "plugin": str(snapshot.metadata.get("plugin", "")).strip() or "unknown-plugin",
                    "actor": _rendered_actor(
                        snapshot.metadata.get("actor"),
                        session_root=dirs.session_dir,
                    ),
                    "target_actor": str(snapshot.metadata.get("target_actor") or "").strip(),
                    "locator": locator,
                    "preferred_name": snapshot_display_name(snapshot),
                    "checksum": snapshot.digest,
                    "artifactKind": _artifact_kind(snapshot.metadata.get("artifact_kind")),
                    "content_locator": content_locator(snapshot.digest),
                    "artifact_locator": _artifact_human_locator(
                        snapshot_display_name(snapshot),
                        snapshot.digest,
                    ),
                    "fetched_at": fetched_at,
                    "follow_command": _follow_command(locator, checksum=snapshot.digest),
                    "event_kind": "source",
                    **_resolved_visibility_metadata(
                        dict(snapshot.metadata),
                        provider=str(snapshot.metadata.get("plugin") or ""),
                        plugin=str(snapshot.metadata.get("plugin") or ""),
                        subcommand=str(snapshot.metadata.get("subcommand") or ""),
                        locator=locator,
                    ),
                }
            )
        if normalized_mode == "best-effort":
            events.extend(local_events)
        ordered = sorted(
            events,
            key=lambda item: (
                str(item.get("source_time") or ""),
                str(item.get("locator") or ""),
                str(item.get("checksum") or ""),
            ),
        )
        paged, paging = _paginate_items(
            ordered,
            limit=limit,
            offset=offset,
            include_all=include_all,
            default_tail_window=True,
        )
        discovery_count, evidence_count = _artifact_kind_counts(paged)
        return {
            "sessionDir": str(dirs.session_dir),
            "contentDir": str(dirs.content_dir),
            "manifestPath": str(dirs.content_dir / "manifest.jsonl"),
            "activityPath": str(activity_log_path(dirs.session_dir)),
            "mode": normalized_mode,
            "modeDescription": TIMELINE_MODE_DESCRIPTIONS[normalized_mode],
            "coverageGapCount": coverage_gap_count,
            "eventCount": paging["totalCount"],
            **paging,
            "discoveryArtifactCount": discovery_count,
            "evidenceArtifactCount": evidence_count,
            "events": paged,
        }
    manifest_events = [
        {
            "mode": "acquired",
            "fetched_at": str(entry.get("fetched_at", "")).strip(),
            "plugin": str(entry.get("plugin", "")).strip() or "unknown-plugin",
            "actor": _rendered_actor(entry.get("actor"), session_root=dirs.session_dir),
            "target_actor": str(entry.get("target_actor", "")).strip(),
            "locator": str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip() or "unknown",
            "preferred_name": str(entry.get("preferred_name", "")).strip() or "data",
            "checksum": str(entry.get("checksum", "")).strip(),
            "artifactKind": _artifact_kind(entry.get("artifact_kind")),
            "content_locator": content_locator(str(entry.get("checksum", "")).strip())
            if str(entry.get("checksum", "")).strip()
            else "",
            "artifact_locator": _artifact_human_locator(
                str(entry.get("preferred_name", "")).strip() or "data",
                str(entry.get("checksum", "")).strip(),
            ),
            "fetch_link": str(entry.get("fetch_link", "")).strip(),
            "follow_command": _follow_command(
                str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip(),
                checksum=str(entry.get("checksum", "")).strip(),
            ),
            "event_kind": "source",
            **_resolved_visibility_metadata(
                entry,
                provider=str(entry.get("plugin") or ""),
                plugin=str(entry.get("plugin") or ""),
                subcommand=str(entry.get("subcommand") or ""),
                locator=str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip(),
            ),
        }
        for entry in _manifest_entries(dirs)
    ]
    events = sorted(
        [*manifest_events, *local_events],
        key=lambda item: (
            str(item.get("fetched_at") or ""),
            str(item.get("locator") or ""),
            str(item.get("checksum") or ""),
        ),
    )
    paged, paging = _paginate_items(
        events,
        limit=limit,
        offset=offset,
        include_all=include_all,
        default_tail_window=True,
    )
    discovery_count, evidence_count = _artifact_kind_counts(paged)
    return {
        "sessionDir": str(dirs.session_dir),
        "contentDir": str(dirs.content_dir),
        "manifestPath": str(dirs.content_dir / "manifest.jsonl"),
        "activityPath": str(activity_log_path(dirs.session_dir)),
        "mode": normalized_mode,
        "modeDescription": TIMELINE_MODE_DESCRIPTIONS[normalized_mode],
        "coverageGapCount": 0,
        "eventCount": paging["totalCount"],
        **paging,
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "events": paged,
    }


def _mermaid_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _mermaid_label(value: str) -> str:
    escaped = (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return escaped.replace("\n", "<br/>")


def _empty_topology_next_step() -> str:
    return _topology_next_step(discovery_count=0, evidence_count=0)


def _no_leads_next_step(*, has_artifacts: bool) -> str:
    if not has_artifacts:
        return _empty_topology_next_step()
    return (
        "No explicit followable leads were mined from the current artifact set. Continue "
        "from the strongest materialized anchors with provider-native search/read surfaces, "
        "or inspect manifest and timeline to choose the next anchor."
    )


def _graph_payload(dirs) -> dict[str, object]:
    entries = _manifest_entries(dirs)
    snapshot_by_digest = {
        snapshot.digest: snapshot for snapshot in scan_content_store(dirs.content_dir)
    }
    source_to_content: dict[str, set[str]] = {}
    content_to_sources: dict[str, set[str]] = {}
    edge_counts: dict[tuple[str, str, str], int] = {}
    content_names: dict[str, str] = {}
    source_variants: dict[str, set[tuple[str, str]]] = {}
    source_artifact_kinds: dict[str, set[str]] = {}
    source_visibility: dict[str, dict[str, object]] = {}
    for entry in entries:
        source = entry.get("canonical_locator") or entry.get("locator") or "unknown"
        checksum = entry.get("checksum") or ""
        plugin = entry.get("plugin") or "unknown"
        if not checksum:
            continue
        source_to_content.setdefault(source, set()).add(checksum)
        content_to_sources.setdefault(checksum, set()).add(source)
        edge_key = (source, checksum, plugin)
        edge_counts[edge_key] = edge_counts.get(edge_key, 0) + 1
        content_names.setdefault(checksum, entry.get("preferred_name") or "data")
        kind = _artifact_kind(entry.get("artifact_kind"))
        if kind:
            source_artifact_kinds.setdefault(source, set()).add(kind)
        source_visibility[source] = best_visibility_metadata(
            source_visibility.get(source, {}),
            _resolved_visibility_metadata(
                entry,
                provider=str(plugin),
                plugin=str(plugin),
                subcommand=str(entry.get("subcommand") or ""),
                locator=str(source),
            ),
        )
        snapshot = snapshot_by_digest.get(str(checksum))
        if snapshot is not None:
            source_variants.setdefault(source, set()).add(_render_variant(snapshot))
    sources = [
        {
            "locator": locator,
            "followCommand": _follow_command(locator),
            "contentCount": len(checksums),
            "artifactKind": (
                next(iter(source_artifact_kinds.get(locator, set())))
                if len(source_artifact_kinds.get(locator, set())) == 1
                else ""
            ),
            "artifactKinds": sorted(str(value) for value in source_artifact_kinds.get(locator, set())),
            "collision": False,
            "variant": len(source_variants.get(locator, set())) > 1,
            "variantCount": len(source_variants.get(locator, set())),
            "variants": [
                _render_variant_label(variant)
                for variant in sorted(source_variants.get(locator, set()))
            ],
            **source_visibility.get(locator, {}),
        }
        for locator, checksums in sorted(source_to_content.items())
    ]
    content = [
        {
            "checksum": checksum,
            "preferredName": content_names.get(checksum, "data"),
            "artifactKind": _artifact_kind(
                snapshot_by_digest.get(checksum).metadata.get("artifact_kind")
            )
            if snapshot_by_digest.get(checksum) is not None
            else "",
            "contentLocator": content_locator(checksum),
            "artifactLocator": _artifact_human_locator(content_names.get(checksum, "data"), checksum),
            "followCommand": f"gotta read {sh_quote(_artifact_human_locator(content_names.get(checksum, 'data'), checksum))}",
            "sourceCount": len(locators),
            "collision": len(locators) > 1,
            **(
                _resolved_visibility_metadata(
                    dict(snapshot_by_digest.get(checksum).metadata),
                    provider=str(snapshot_by_digest.get(checksum).metadata.get("plugin") or ""),
                    plugin=str(snapshot_by_digest.get(checksum).metadata.get("plugin") or ""),
                    subcommand=str(snapshot_by_digest.get(checksum).metadata.get("subcommand") or ""),
                    locator=str(next(iter(locators), "")),
                )
                if snapshot_by_digest.get(checksum) is not None
                else {}
            ),
        }
        for checksum, locators in sorted(content_to_sources.items())
    ]
    edges = [
        {
            "source": source,
            "checksum": checksum,
            "plugin": plugin,
            "count": count,
        }
        for (source, checksum, plugin), count in sorted(edge_counts.items())
    ]
    empty = not sources and not content and not edges
    discovery_count = sum(1 for item in content if item.get("artifactKind") == "discovery")
    evidence_count = sum(1 for item in content if item.get("artifactKind") == "evidence")
    return {
        "sessionDir": str(dirs.session_dir),
        "contentDir": str(dirs.content_dir),
        "manifestPath": str(dirs.content_dir / "manifest.jsonl"),
        "sourceCount": len(sources),
        "contentCount": len(content),
        "edgeCount": len(edges),
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "empty": empty,
        "nextStep": _topology_next_step(
            discovery_count=discovery_count,
            evidence_count=evidence_count,
        ),
        "sources": sources,
        "content": content,
        "edges": edges,
    }


def _render_mermaid(payload: dict[str, object]) -> str:
    lines = [
        "---",
        "title: gotta content graph",
        "---",
        "flowchart LR",
    ]
    if payload.get("empty"):
        lines.append(f'  empty["{_mermaid_label(str(payload.get("nextStep") or _empty_topology_next_step()))}"]')
        lines.extend(
            [
                "  class empty emptyState",
                "  classDef emptyState fill:#f7fafc,stroke:#4a5568,color:#1a202c;",
                "",
            ]
        )
        return "\n".join(lines)
    if payload.get("nextStep"):
        lines.append(f'  note["{_mermaid_label(str(payload["nextStep"]))}"]')
        lines.append("  class note emptyState")
    for source in payload["sources"]:
        locator = str(source["locator"])
        node_id = _mermaid_id("src", locator)
        label_parts = [locator]
        if source.get("variant"):
            label_parts.append(f"renderings: {int(source.get('variantCount') or 0)}")
        label = _mermaid_label("\n".join(label_parts))
        lines.append(f'  {node_id}["{label}"]')
        if source["collision"]:
            lines.append(f"  class {node_id} collision")
        elif source.get("variant"):
            lines.append(f"  class {node_id} variant")
        else:
            lines.append(f"  class {node_id} source")
    for content in payload["content"]:
        checksum = str(content["checksum"])
        preferred = str(content["preferredName"])
        node_id = _mermaid_id("art", checksum)
        short = checksum[:12]
        label = _mermaid_label(f"{preferred}\n{short}")
        lines.append(f'  {node_id}["{label}"]')
        if content["collision"]:
            lines.append(f"  class {node_id} collision")
        else:
            lines.append(f"  class {node_id} content")
    for edge in payload["edges"]:
        source_id = _mermaid_id("src", str(edge["source"]))
        content_id = _mermaid_id("art", str(edge["checksum"]))
        plugin = str(edge["plugin"])
        count = int(edge["count"])
        label = plugin if count == 1 else f"{plugin} x{count}"
        lines.append(f"  {source_id} -->|{label}| {content_id}")
    lines.extend(
        [
            "  classDef source fill:#eef8ee,stroke:#2d7a2d,color:#173d17;",
            "  classDef content fill:#eef4ff,stroke:#2a62c7,color:#173058;",
            "  classDef variant fill:#fff7e6,stroke:#b7791f,color:#5a3b09;",
            "  classDef collision fill:#fff1f1,stroke:#c73434,color:#6b1111;",
            "  classDef emptyState fill:#f7fafc,stroke:#4a5568,color:#1a202c;",
            "",
        ]
    )
    return "\n".join(lines)


def _argv_output(argv: object) -> str:
    if not isinstance(argv, list):
        return ""
    for index, item in enumerate(argv):
        token = str(item)
        if token.startswith("--output="):
            return token.split("=", 1)[1].strip()
        if token == "--output" and index + 1 < len(argv):
            return str(argv[index + 1]).strip()
    return ""


def _render_variant(snapshot: ContentSnapshot) -> tuple[str, str]:
    raw_subcommand = str(snapshot.metadata.get("subcommand", "")).strip()
    locator = str(
        snapshot.metadata.get("canonical_locator", "")
        or snapshot.metadata.get("locator", "")
    ).strip()
    if raw_subcommand in {"search", "jql", "cql", "get", "status", "sql", "sync"}:
        subcommand = raw_subcommand
    elif ":search " in locator:
        subcommand = "search"
    elif ":jql " in locator:
        subcommand = "jql"
    elif ":cql " in locator:
        subcommand = "cql"
    else:
        subcommand = "default"
    output = _argv_output(snapshot.metadata.get("argv"))
    if not output:
        name = str(snapshot.metadata.get("preferred_name", "")).strip().lower()
        extension = Path(name).suffix.lower()
        output = {
            ".summary": "summary",
            ".md": "markdown",
            ".json": "json",
            ".txt": "text",
            ".html": "html",
            ".csv": "csv",
        }.get(extension, "")
    content_type = str(snapshot.metadata.get("content_type", "")).strip().lower()
    flavor = output or content_type or "default"
    return (subcommand, flavor)


def _render_variant_label(variant: tuple[str, str]) -> str:
    subcommand, flavor = variant
    if subcommand == "default":
        return flavor
    return f"{subcommand}/{flavor}"


def _analysis_output_paths(session_dir: Path) -> tuple[Path, Path, Path]:
    return (
        session_dir / "graph.mmd",
        session_dir / "graph.json",
        session_dir / "summary.json",
    )


def _semantic_output_paths(session_dir: Path) -> tuple[Path, Path]:
    return (
        session_dir / "semantic-graph.mmd",
        session_dir / "semantic-graph.json",
    )


def _wrapped_markdown_path(mermaid_path: Path) -> Path:
    return mermaid_path.with_name(mermaid_path.name + ".md")


def _write_mermaid_artifact(path: Path, mermaid: str) -> None:
    write_text_atomic(path, mermaid + "\n")
    title = path.stem
    lines = mermaid.splitlines()
    if len(lines) >= 3 and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            if lines[i].startswith("title:"):
                title = lines[i].split(":", 1)[1].strip() or title
            i += 1
        body_lines = lines[i + 1 :] if i < len(lines) else lines
    else:
        body_lines = lines
    body = "\n".join(body_lines).rstrip() + "\n"
    wrapped = f"# {title}\n\n```mermaid\n{body}```\n"
    write_text_atomic(_wrapped_markdown_path(path), wrapped)


def _revision_edges(snapshots: list[ContentSnapshot]) -> list[dict[str, str]]:
    tracks: dict[tuple[str, tuple[str, str]], list[dict[str, str]]] = {}
    for snapshot in snapshots:
        canonical = str(
            snapshot.metadata.get("canonical_locator", "")
            or snapshot.metadata.get("locator", "")
        ).strip()
        if not canonical:
            continue
        variant = _render_variant(snapshot)
        for event in snapshot.events:
            tracks.setdefault((canonical, variant), []).append(
                {
                    "timestamp": event.timestamp,
                    "digest": snapshot.digest,
                    "preferred_name": str(
                        snapshot.metadata.get("preferred_name", "") or event.link_name
                    ),
                    "plugin": str(snapshot.metadata.get("plugin", "") or "unknown"),
                    "actor": _rendered_actor(
                        snapshot.metadata.get("actor"),
                        session_root=snapshot.content_dir.parent.parent,
                    ),
                    "rendering": _render_variant_label(variant),
                }
            )
    edges: list[dict[str, str]] = []
    for (locator, _variant), items in sorted(tracks.items()):
        prior_item: dict[str, str] | None = None
        for item in sorted(items, key=lambda current: (current["timestamp"], current["digest"])):
            if prior_item is None:
                prior_item = item
                continue
            if item["digest"] == prior_item["digest"]:
                prior_item = item
                continue
            edges.append(
                {
                    "locator": locator,
                    "preferredName": item["preferred_name"] or prior_item["preferred_name"],
                    "from": prior_item["digest"],
                    "to": item["digest"],
                    "fromTimestamp": prior_item["timestamp"],
                    "toTimestamp": item["timestamp"],
                    "plugin": item["plugin"] or prior_item["plugin"],
                    "actor": item["actor"] or prior_item["actor"],
                    "rendering": item["rendering"] or prior_item["rendering"],
                }
            )
            prior_item = item
    return edges


def _analysis_payload(dirs) -> dict[str, object]:
    snapshots = scan_content_store(dirs.content_dir)
    snapshot_by_digest = {snapshot.digest: snapshot for snapshot in snapshots}
    manifest_entries = _manifest_entries(dirs)
    source_map: dict[str, dict[str, object]] = {}
    edge_plugins: dict[tuple[str, str], list[str]] = {}
    edge_actors: dict[tuple[str, str], set[str]] = {}
    content_details: dict[str, dict[str, set[str]]] = {}

    for entry in manifest_entries:
        source = entry.get("canonical_locator") or entry.get("locator") or "unknown"
        checksum = entry.get("checksum") or ""
        if not checksum:
            continue
        source_state = source_map.setdefault(
            source,
            {
                "content": set(),
                "locators": set(),
                "plugins": set(),
                "actors": set(),
                "artifact_kinds": set(),
                "entries": 0,
                "variants": set(),
                "visibility": {},
            },
        )
        source_state["content"].add(checksum)
        locator = entry.get("locator") or source
        source_state["locators"].add(locator)
        plugin = entry.get("plugin") or "unknown"
        actor = _rendered_actor(entry.get("actor"), session_root=dirs.session_dir)
        source_state["plugins"].add(plugin)
        source_state["actors"].add(actor)
        kind = _artifact_kind(entry.get("artifact_kind"))
        if kind:
            source_state["artifact_kinds"].add(kind)
        source_state["entries"] = int(source_state["entries"]) + 1
        source_state["visibility"] = best_visibility_metadata(
            source_state.get("visibility", {}),
            _resolved_visibility_metadata(
                entry,
                provider=str(plugin),
                plugin=str(plugin),
                subcommand=str(entry.get("subcommand") or ""),
                locator=str(source),
            ),
        )
        snapshot = snapshot_by_digest.get(str(checksum))
        if snapshot is not None:
            source_state["variants"].add(_render_variant(snapshot))
        edge_plugins.setdefault((source, checksum), []).append(plugin)
        edge_actors.setdefault((source, checksum), set()).add(actor)
        detail = content_details.setdefault(
            checksum,
            {
                "providers": set(),
                "actors": set(),
                "resource_hints": set(),
            },
        )
        detail["providers"].add(_provider_name(source, plugins=[plugin], fallback=plugin))
        detail["actors"].add(actor)
        resource_kind, resource_label = _resource_label(source)
        if resource_kind and resource_label:
            detail["resource_hints"].add(f"{resource_kind}:{resource_label}")
        else:
            detail["resource_hints"].add(source)

    name_counts = Counter(snapshot_display_name(snapshot) for snapshot in snapshots)

    content = [
        {
            "checksum": snapshot.digest,
            "preferredName": snapshot_display_name(snapshot),
            "artifactKind": _artifact_kind(snapshot.metadata.get("artifact_kind")),
            "contentLocator": content_locator(snapshot.digest),
            "artifactLocator": snapshot_artifact_locator(snapshot),
            "followCommand": f"gotta read {sh_quote(snapshot_artifact_locator(snapshot))}",
            "nameCollision": name_counts[snapshot_display_name(snapshot)] > 1,
            "nameCount": len(snapshot.names),
            "fetchCount": len(snapshot.events),
            "names": snapshot.names,
            "firstFetchedAt": snapshot.events[0].timestamp if snapshot.events else "",
            "lastFetchedAt": snapshot.events[-1].timestamp if snapshot.events else "",
            "providers": sorted(content_details.get(snapshot.digest, {}).get("providers", set())),
            "actors": sorted(content_details.get(snapshot.digest, {}).get("actors", set())),
            "resourceHints": sorted(content_details.get(snapshot.digest, {}).get("resource_hints", set())),
            **_resolved_visibility_metadata(
                dict(snapshot.metadata),
                provider=str(snapshot.metadata.get("plugin") or ""),
                plugin=str(snapshot.metadata.get("plugin") or ""),
                subcommand=str(snapshot.metadata.get("subcommand") or ""),
                locator=str(snapshot_locator(snapshot)),
            ),
        }
        for snapshot in snapshots
    ]
    sources = [
        {
            "locator": locator,
            "contentCount": len(state["content"]),
            "entryCount": int(state["entries"]),
            "artifactKind": (
                next(iter(state["artifact_kinds"]))
                if len(state["artifact_kinds"]) == 1
                else ""
            ),
            "artifactKinds": sorted(str(value) for value in state["artifact_kinds"]),
            "plugins": sorted(str(value) for value in state["plugins"]),
            "actors": sorted(str(value) for value in state["actors"]),
            "locators": sorted(str(value) for value in state["locators"]),
            "collision": False,
            "duplicateMaterialization": len(state["content"]) > 1 and len(state["variants"]) <= 1,
            "variant": len(state["variants"]) > 1,
            "variantCount": len(state["variants"]),
            "variants": [
                _render_variant_label(variant)
                for variant in sorted(state["variants"])
            ],
            **best_visibility_metadata(state.get("visibility", {})),
        }
        for locator, state in sorted(source_map.items())
    ]
    source_edges = [
        {
            "source": source,
            "checksum": checksum,
            "plugins": sorted(plugins),
            "actors": sorted(edge_actors.get((source, checksum), set())),
            "count": len(plugins),
        }
        for (source, checksum), plugins in sorted(edge_plugins.items())
    ]
    revision_edges = _revision_edges(snapshots)
    lead_edges = build_lead_edge_records(
        snapshots,
        manifest_entries,
        classify_kind=_lead_kind,
    )
    lead_sources = aggregate_lead_sources(lead_edges)
    collisions = [source["locator"] for source in sources if source["collision"]]
    duplicate_materializations = [
        source["locator"] for source in sources if source.get("duplicateMaterialization")
    ]
    variants = [source["locator"] for source in sources if source.get("variant")]
    name_collisions = sorted(
        name for name, count in name_counts.items() if count > 1 and name
    )
    materialized_lead_count = sum(
        1 for source in lead_sources if bool(source["materialized"])
    )
    empty = not sources and not content and not source_edges and not revision_edges and not lead_edges
    discovery_count = sum(1 for item in content if item.get("artifactKind") == "discovery")
    evidence_count = sum(1 for item in content if item.get("artifactKind") == "evidence")
    return {
        "sessionDir": str(dirs.session_dir),
        "contentDir": str(dirs.content_dir),
        "manifestPath": str(dirs.content_dir / "manifest.jsonl"),
        "manifestEntryCount": len(manifest_entries),
        "contentCount": len(content),
        "sourceCount": len(sources),
        "sourceEdgeCount": len(source_edges),
        "revisionEdgeCount": len(revision_edges),
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "collisionCount": len(collisions),
        "collisions": collisions,
        "duplicateMaterializationCount": len(duplicate_materializations),
        "duplicateMaterializations": duplicate_materializations,
        "variantCount": len(variants),
        "variants": variants,
        "nameCollisionCount": len(name_collisions),
        "nameCollisions": name_collisions,
        "leadSourceCount": len(lead_sources),
        "materializedLeadSourceCount": materialized_lead_count,
        "unmaterializedLeadSourceCount": len(lead_sources) - materialized_lead_count,
        "leadEdgeCount": len(lead_edges),
        "empty": empty,
        "nextStep": _topology_next_step(
            discovery_count=discovery_count,
            evidence_count=evidence_count,
        ),
        "sources": sources,
        "content": content,
        "sourceEdges": source_edges,
        "revisionEdges": revision_edges,
        "leadSources": lead_sources,
        "leadEdges": lead_edges,
    }


def _analysis_mermaid_id(prefix: str, value: str) -> str:
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"{prefix}:{value}").hex[:12]
    return f"{prefix}_{digest}"


def _provider_name(
    locator: str,
    *,
    plugins: list[str] | None = None,
    fallback: str = "unknown",
) -> str:
    preferred_plugins = [plugin for plugin in (plugins or []) if plugin and plugin != "read"]
    if preferred_plugins:
        return preferred_plugins[0]
    if plugins:
        first = str(plugins[0]).strip()
        if first:
            return first
    if ":" in locator:
        prefix = locator.split(":", 1)[0].strip()
        if re.fullmatch(r"[a-z][a-z0-9_-]*", prefix):
            return prefix
    return fallback


def _query_label(locator: str) -> str:
    match = re.search(r"(?:^|:)(search|jql|cql)\s+(.+)$", locator)
    if match:
        return f"{match.group(1)} {match.group(2).strip()}"
    return ""


def _resource_label(locator: str) -> tuple[str, str]:
    provider = _provider_name(locator)
    rest = locator.split(":", 1)[1] if ":" in locator else locator
    if provider == "jira" and re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", rest):
        return ("jira-issue", rest)
    if provider == "confluence" and rest.isdigit():
        return ("confluence-page", rest)
    if provider == "gdocs" and rest:
        return ("google-doc", rest)
    if provider == "gdrive" and rest:
        return ("google-drive-file", rest)
    if provider == "slack":
        parts = rest.split(":")
        if len(parts) >= 3 and parts[0] == "thread":
            return ("slack-thread", f"{parts[1]}:{parts[2]}")
        if len(parts) >= 2 and parts[0] == "channel":
            return ("slack-channel", parts[1])
    if provider == "github":
        match = re.search(r"github\.com/([^/]+/[^/]+)", rest)
        if match:
            return ("github-repo", match.group(1))
    return ("", "")


def _lead_kind(locator: str, provider: str) -> str:
    if re.search(r"(?:^|:)(search|jql|cql)\s+.+$", locator):
        return f"{provider or 'external'}-search"
    if locator.startswith("artifact:"):
        return "artifact"
    if locator.startswith("content:"):
        return "content"
    resource_kind, _ = _resource_label(locator)
    if resource_kind:
        return resource_kind
    if provider == "github" and locator.startswith("https://github.com/"):
        path = urllib.parse.urlparse(locator).path
        if re.search(r"/pull/\d+(?:/|$)", path):
            return "github-pull"
        if re.search(r"/issues/\d+(?:/|$)", path):
            return "github-issue"
        if re.search(r"/commit/[A-Fa-f0-9]+(?:/|$)", path):
            return "github-commit"
        if re.search(r"/commits(?:/|$)", path):
            return "github-commit-history"
        return "github-repo"
    if provider == "web":
        return "url"
    return f"{provider or 'external'}-reference"


def _leads_payload(
    dirs,
    *,
    target: str = "",
    limit: int = 100,
    offset: int = 0,
    include_all: bool = False,
) -> dict[str, object]:
    snapshots = scan_content_store(dirs.content_dir)
    manifest_entries = _manifest_entries(dirs)
    selected = resolve_lead_snapshots(target, snapshots, manifest_entries)
    selected_digests = {snapshot.digest for snapshot in selected}
    all_edge_records = build_lead_edge_records(
        snapshots,
        manifest_entries,
        classify_kind=_lead_kind,
    )
    edge_records = [
        edge
        for edge in all_edge_records
        if str(edge.get("sourceChecksum", "")) in selected_digests
    ]
    lead_sources = aggregate_lead_sources(edge_records)
    paged_sources, paging = _paginate_items(
        lead_sources,
        limit=limit,
        offset=offset,
        include_all=include_all,
    )
    selected_edges_by_checksum: dict[str, list[dict[str, object]]] = {}
    for edge in edge_records:
        selected_edges_by_checksum.setdefault(str(edge["sourceChecksum"]), []).append(edge)
    artifacts = []
    for snapshot in selected:
        edges = sorted(
            selected_edges_by_checksum.get(snapshot.digest, []),
            key=edge_best_first_sort_key,
        )
        artifacts.append(
            {
                "checksum": snapshot.digest,
                "preferredName": snapshot_display_name(snapshot),
                "artifactKind": _artifact_kind(snapshot.metadata.get("artifact_kind")),
                "sourceLocator": snapshot_locator(snapshot),
                "artifactLocator": snapshot_artifact_locator(snapshot),
                "contentLocator": content_locator(snapshot.digest),
                "lastFetchedAt": snapshot_last_fetched_at(snapshot),
                "leadCount": len(edges),
                "leads": edges[: max(limit, 0)],
                **_resolved_visibility_metadata(
                    dict(snapshot.metadata),
                    provider=str(snapshot.metadata.get("plugin") or ""),
                    plugin=str(snapshot.metadata.get("plugin") or ""),
                    subcommand=str(snapshot.metadata.get("subcommand") or ""),
                    locator=str(snapshot_locator(snapshot)),
                ),
            }
        )
    materialized_count = sum(1 for source in lead_sources if bool(source["materialized"]))
    discovery_count = sum(1 for item in artifacts if item.get("artifactKind") == "discovery")
    evidence_count = sum(1 for item in artifacts if item.get("artifactKind") == "evidence")
    empty = not selected
    next_step = (
        _topology_next_step(discovery_count=discovery_count, evidence_count=evidence_count)
        if empty
        else _no_leads_next_step(has_artifacts=bool(selected))
        if not lead_sources
        else ""
    )
    return {
        "sessionDir": str(dirs.session_dir),
        "contentDir": str(dirs.content_dir),
        "target": target.strip(),
        "limit": max(limit, 0),
        "artifactCount": len(selected),
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "leadCount": len(lead_sources),
        **paging,
        "materializedLeadCount": materialized_count,
        "unmaterializedLeadCount": len(lead_sources) - materialized_count,
        "leadSources": paged_sources,
        "artifacts": artifacts,
        "empty": empty,
        "nextStep": next_step,
    }


def _semantic_payload(dirs) -> dict[str, object]:
    lineage = _analysis_payload(dirs)
    nodes: dict[str, dict[str, object]] = {}
    edges: set[tuple[str, str, str]] = set()

    def add_node(node_id: str, *, label: str, kind: str, group: str) -> None:
        nodes.setdefault(
            node_id,
            {
                "id": node_id,
                "label": label,
                "kind": kind,
                "group": group,
                "materialized": False,
                "discovered": False,
            },
        )

    def add_edge(source: str, target: str, label: str) -> None:
        edges.add((source, target, label))

    for source in lineage["sources"]:
        locator = str(source["locator"])
        provider = _provider_name(
            locator,
            plugins=[str(value) for value in source.get("plugins") or [] if str(value)],
        )
        provider_id = f"provider:{provider}"
        source_id = f"source:{locator}"
        add_node(provider_id, label=provider, kind="provider", group=provider)
        add_node(source_id, label=locator, kind="source", group=provider)
        nodes[source_id]["materialized"] = True
        nodes[source_id]["artifactKind"] = str(source.get("artifactKind") or "")
        nodes[source_id]["artifactKinds"] = list(source.get("artifactKinds") or [])
        add_edge(provider_id, source_id, "source")

        query_label = _query_label(locator)
        if query_label:
            query_id = f"query:{provider}:{query_label}"
            add_node(query_id, label=query_label, kind="query", group=provider)
            add_edge(provider_id, query_id, "query")
            add_edge(query_id, source_id, "drives")

        resource_kind, resource_label = _resource_label(locator)
        if resource_kind and resource_label:
            resource_id = f"resource:{provider}:{resource_label}"
            add_node(
                resource_id,
                label=resource_label,
                kind=resource_kind,
                group=provider,
            )
            add_edge(provider_id, resource_id, "resource")
            add_edge(resource_id, source_id, "resolved_by")

    for content in lineage["content"]:
        checksum = str(content["checksum"])
        content_id = f"content:{checksum}"
        add_node(
            content_id,
            label=str(content["preferredName"]),
            kind="content",
            group="content",
        )
        nodes[content_id]["materialized"] = True
        nodes[content_id]["artifactKind"] = str(content.get("artifactKind") or "")

    for edge in lineage["sourceEdges"]:
        source_id = f"source:{edge['source']}"
        content_id = f"content:{edge['checksum']}"
        add_edge(source_id, content_id, ",".join(str(value) for value in edge["plugins"]))

    for edge in lineage["revisionEdges"]:
        from_id = f"content:{edge['from']}"
        to_id = f"content:{edge['to']}"
        add_edge(from_id, to_id, f"revision:{edge['locator']}")
    for lead_source in lineage.get("leadSources") or []:
        locator = str(lead_source["locator"])
        provider = str(lead_source["provider"] or _provider_name(locator))
        provider_id = f"provider:{provider}"
        source_id = f"source:{locator}"
        add_node(provider_id, label=provider, kind="provider", group=provider)
        add_node(source_id, label=locator, kind="source", group=provider)
        nodes[source_id]["materialized"] = bool(
            nodes[source_id].get("materialized") or lead_source.get("materialized")
        )
        nodes[source_id]["discovered"] = True
        if not nodes[source_id].get("artifactKinds"):
            nodes[source_id]["artifactKinds"] = []
        kinds = {
            str(value)
            for value in nodes[source_id].get("artifactKinds") or []
            if str(value)
        }
        lead_kind = str(lead_source.get("artifactKind") or "")
        if lead_kind:
            kinds.add(lead_kind)
        nodes[source_id]["artifactKinds"] = sorted(kinds)
        nodes[source_id]["artifactKind"] = (
            nodes[source_id]["artifactKinds"][0]
            if len(nodes[source_id]["artifactKinds"]) == 1
            else ""
        )
        add_edge(provider_id, source_id, "source")
        resource_kind, resource_label = _resource_label(locator)
        if resource_kind and resource_label:
            resource_id = f"resource:{provider}:{resource_label}"
            add_node(
                resource_id,
                label=resource_label,
                kind=resource_kind,
                group=provider,
            )
            add_edge(provider_id, resource_id, "resource")
            add_edge(resource_id, source_id, "resolved_by")
    for edge in lineage.get("leadEdges") or []:
        from_id = f"content:{edge['sourceChecksum']}"
        to_id = f"source:{edge['targetLocator']}"
        relation = str(edge.get("relation") or "links_to")
        count = int(edge.get("occurrenceCount") or 0)
        add_edge(from_id, to_id, relation if count <= 1 else f"{relation} x{count}")

    empty = not nodes and not edges
    discovery_count = int(lineage.get("discoveryArtifactCount") or 0)
    evidence_count = int(lineage.get("evidenceArtifactCount") or 0)
    return {
        "sessionDir": lineage["sessionDir"],
        "contentDir": lineage["contentDir"],
        "manifestPath": lineage["manifestPath"],
        "nodeCount": len(nodes),
        "edgeCount": len(edges),
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "empty": empty,
        "nextStep": _topology_next_step(
            discovery_count=discovery_count,
            evidence_count=evidence_count,
        ),
        "nodes": sorted(nodes.values(), key=lambda item: (item["kind"], item["label"])),
        "edges": [
            {"source": source, "target": target, "label": label}
            for source, target, label in sorted(edges)
        ],
    }


def _render_semantic_mermaid(payload: dict[str, object]) -> str:
    lines = [
        "---",
        "title: gotta semantic graph",
        "---",
        "flowchart LR",
    ]
    if payload.get("empty"):
        lines.append(f'  empty["{_mermaid_label(str(payload.get("nextStep") or _empty_topology_next_step()))}"]')
        lines.extend(
            [
                "  class empty emptyState",
                "  classDef emptyState fill:#f7fafc,stroke:#4a5568,color:#1a202c;",
                "",
            ]
        )
        return "\n".join(lines)
    if payload.get("nextStep"):
        lines.append(f'  note["{_mermaid_label(str(payload["nextStep"]))}"]')
        lines.append("  class note emptyState")
    for node in payload["nodes"]:
        node_id = _analysis_mermaid_id("sem", str(node["id"]))
        label = _mermaid_label(str(node["label"]))
        kind = str(node["kind"]).replace("-", "_")
        if (
            str(node.get("kind")) == "source"
            and not bool(node.get("materialized"))
            and bool(node.get("discovered"))
        ):
            kind = "source_gap"
        lines.append(f'  {node_id}["{label}"]')
        lines.append(f"  class {node_id} {kind}")
    for edge in payload["edges"]:
        source_id = _analysis_mermaid_id("sem", str(edge["source"]))
        target_id = _analysis_mermaid_id("sem", str(edge["target"]))
        label = _mermaid_label(str(edge["label"]))
        lines.append(f"  {source_id} -->|{label}| {target_id}")
    lines.extend(
        [
            "  classDef provider fill:#fff7e6,stroke:#b7791f,color:#5a3b09;",
            "  classDef query fill:#f5f3ff,stroke:#6b46c1,color:#34206b;",
            "  classDef source fill:#eef8ee,stroke:#2d7a2d,color:#173d17;",
            "  classDef source_gap fill:#fffaf0,stroke:#b7791f,color:#5a3b09;",
            "  classDef content fill:#eef4ff,stroke:#2a62c7,color:#173058;",
            "  classDef jira_issue fill:#fff5f5,stroke:#c53030,color:#63171b;",
            "  classDef confluence_page fill:#fffbea,stroke:#b7791f,color:#5a3b09;",
            "  classDef google_doc fill:#effcf6,stroke:#2f855a,color:#1b4332;",
            "  classDef google_drive_file fill:#effcf6,stroke:#2f855a,color:#1b4332;",
            "  classDef slack_thread fill:#ebf8ff,stroke:#2b6cb0,color:#1a365d;",
            "  classDef slack_channel fill:#ebf8ff,stroke:#2b6cb0,color:#1a365d;",
            "  classDef github_repo fill:#f7fafc,stroke:#4a5568,color:#1a202c;",
            "  classDef emptyState fill:#f7fafc,stroke:#4a5568,color:#1a202c;",
            "",
        ]
    )
    return "\n".join(lines)


def _render_analysis_mermaid(payload: dict[str, object]) -> str:
    lines = [
        "---",
        "title: gotta session analysis",
        "---",
        "flowchart LR",
    ]
    if payload.get("empty"):
        lines.append(f'  empty["{_mermaid_label(str(payload.get("nextStep") or _empty_topology_next_step()))}"]')
        lines.extend(
            [
                "  class empty emptyState",
                "  classDef emptyState fill:#f7fafc,stroke:#4a5568,color:#1a202c;",
                "",
            ]
        )
        return "\n".join(lines)
    if payload.get("nextStep"):
        lines.append(f'  note["{_mermaid_label(str(payload["nextStep"]))}"]')
        lines.append("  class note emptyState")
    for source in payload["sources"]:
        locator = str(source["locator"])
        node_id = _analysis_mermaid_id("src", locator)
        actors = ", ".join(str(value) for value in source.get("actors") or [])
        label_parts = [locator]
        if actors:
            label_parts.append(f"actor: {actors}")
        if source.get("variant"):
            label_parts.append(f"renderings: {int(source.get('variantCount') or 0)}")
        elif source.get("duplicateMaterialization"):
            label_parts.append(f"materializations: {int(source.get('contentCount') or 0)}")
        label = _mermaid_label("\n".join(label_parts))
        lines.append(f'  {node_id}["{label}"]')
        if source["collision"]:
            lines.append(f"  class {node_id} collision")
        elif source.get("variant"):
            lines.append(f"  class {node_id} variant")
        elif source.get("duplicateMaterialization"):
            lines.append(f"  class {node_id} duplicate")
        else:
            lines.append(f"  class {node_id} source")
    for content in payload["content"]:
        checksum = str(content["checksum"])
        providers = ", ".join(str(value) for value in content.get("providers") or [])
        actors = ", ".join(str(value) for value in content.get("actors") or [])
        label_parts = [str(content["preferredName"])]
        resource_hints = [str(value) for value in content.get("resourceHints") or [] if str(value)]
        if bool(content.get("nameCollision")) and resource_hints:
            label_parts.append(resource_hints[0])
        if providers:
            label_parts.append(providers)
        if actors:
            label_parts.append(f"actor: {actors}")
        label_parts.append(checksum[:12])
        label = _mermaid_label("\n".join(label_parts))
        node_id = _analysis_mermaid_id("art", checksum)
        lines.append(f'  {node_id}["{label}"]')
        lines.append(f"  class {node_id} content")
    for edge in payload["sourceEdges"]:
        source_id = _analysis_mermaid_id("src", str(edge["source"]))
        content_id = _analysis_mermaid_id("art", str(edge["checksum"]))
        label_parts = [", ".join(edge["plugins"])]
        actors = ", ".join(str(value) for value in edge.get("actors") or [])
        if actors:
            label_parts.append(f"actor: {actors}")
        label = _mermaid_label("\n".join(part for part in label_parts if part))
        lines.append(f"  {source_id} -->|{label}| {content_id}")
    for edge in payload["revisionEdges"]:
        from_id = _analysis_mermaid_id("art", str(edge["from"]))
        to_id = _analysis_mermaid_id("art", str(edge["to"]))
        label = _mermaid_label(
            f"revision:{str(edge['locator'])}\n{str(edge.get('rendering') or '')}".rstrip()
        )
        lines.append(f"  {from_id} -->|{label}| {to_id}")
    seen_source_nodes = {str(source["locator"]) for source in payload["sources"]}
    for lead_source in payload.get("leadSources") or []:
        locator = str(lead_source["locator"])
        if locator in seen_source_nodes:
            continue
        node_id = _analysis_mermaid_id("src", locator)
        label_parts = [locator, f"lead: {str(lead_source['provider'])}"]
        if not bool(lead_source.get("materialized")):
            label_parts.append("not yet materialized")
        label = _mermaid_label("\n".join(label_parts))
        lines.append(f'  {node_id}["{label}"]')
        lines.append(f"  class {node_id} leadgap")
    for edge in payload.get("leadEdges") or []:
        content_id = _analysis_mermaid_id("art", str(edge["sourceChecksum"]))
        source_id = _analysis_mermaid_id("src", str(edge["targetLocator"]))
        relation = str(edge.get("relation") or "links_to")
        count = int(edge.get("occurrenceCount") or 0)
        label = relation if count <= 1 else f"{relation} x{count}"
        lines.append(f"  {content_id} -.->|{_mermaid_label(label)}| {source_id}")
    lines.extend(
        [
            "  classDef source fill:#eef8ee,stroke:#2d7a2d,color:#173d17;",
            "  classDef content fill:#eef4ff,stroke:#2a62c7,color:#173058;",
            "  classDef duplicate fill:#edf2f7,stroke:#4a5568,color:#1a202c;",
            "  classDef variant fill:#fff7e6,stroke:#b7791f,color:#5a3b09;",
            "  classDef collision fill:#fff1f1,stroke:#c73434,color:#6b1111;",
            "  classDef leadgap fill:#fffaf0,stroke:#b7791f,color:#5a3b09;",
            "  classDef emptyState fill:#f7fafc,stroke:#4a5568,color:#1a202c;",
            "",
        ]
    )
    return "\n".join(lines)


def _semantic_node_follow_command(
    node: dict[str, object],
    *,
    lineage: dict[str, object],
) -> str:
    kind = str(node.get("kind") or "")
    node_id = str(node.get("id") or "")
    label = str(node.get("label") or "").strip()
    if kind == "source" and label:
        return _follow_command(label)
    if kind == "content" and node_id.startswith("content:"):
        checksum = node_id.split(":", 1)[1]
        for content_item in lineage.get("content") or []:
            if str(content_item.get("checksum") or "") == checksum:
                return str(content_item.get("followCommand") or "").strip()
    return ""


def _focus_match_threshold(best_score: int) -> int:
    if best_score <= 0:
        return 0
    if best_score >= 4:
        return 2
    return best_score


def _ordered_focus_scan_entries(
    scan_payload: dict[str, object] | None,
    *,
    limit: int,
) -> list[dict[str, object]]:
    if not isinstance(scan_payload, dict):
        return []
    entries = [
        dict(entry)
        for entry in scan_payload.get("entries") or []
        if isinstance(entry, dict)
    ]
    ordered = sorted(
        entries,
        key=lambda entry: str(entry.get("lastFetchedAt") or entry.get("fetched_at") or ""),
        reverse=True,
    )
    ordered = sorted(
        ordered,
        key=lambda entry: int(entry.get("hitCount") or 0),
        reverse=True,
    )
    ordered = sorted(
        ordered,
        key=lambda entry: str(
            entry.get("artifactKind") or entry.get("artifact_kind") or ""
        )
        != "evidence",
    )
    return ordered[: max(limit, 0)]


def _lineage_source_candidate(item: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "source",
        "label": str(item.get("locator") or ""),
        "locator": str(item.get("locator") or ""),
        "artifactKind": str(item.get("artifactKind") or ""),
        "materialized": True,
        "followCommand": str(item.get("followCommand") or ""),
    }


def _lineage_content_candidate(item: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "content",
        "label": str(item.get("preferredName") or ""),
        "checksum": str(item.get("checksum") or ""),
        "artifactLocator": str(item.get("artifactLocator") or ""),
        "contentLocator": str(item.get("contentLocator") or ""),
        "artifactKind": str(item.get("artifactKind") or ""),
        "materialized": True,
        "followCommand": str(item.get("followCommand") or ""),
    }


def _lineage_lead_candidate(item: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "lead",
        "label": str(item.get("locator") or ""),
        "locator": str(item.get("locator") or ""),
        "artifactKind": str(item.get("artifactKind") or ""),
        "materialized": bool(item.get("materialized")),
        "followCommand": str(item.get("followCommand") or ""),
    }


def _analysis_focus_score(node: dict[str, object], query: str) -> tuple[int, int, int, str]:
    query_lower = query.lower()
    label = str(node.get("label") or "")
    node_id = str(node.get("id") or "")
    label_lower = label.lower()
    node_id_lower = node_id.lower()
    score = 0
    if label_lower == query_lower or node_id_lower == query_lower:
        score = 5
    elif label_lower.startswith(query_lower) or node_id_lower.startswith(query_lower):
        score = 4
    elif f":{query_lower}" in node_id_lower:
        score = 3
    elif query_lower in label_lower or query_lower in node_id_lower:
        score = 2
    materialized = 1 if bool(node.get("materialized")) else 0
    discovered = 1 if bool(node.get("discovered")) else 0
    return (score, materialized, discovered, label_lower)


def _neighbor_sort_key(
    node: dict[str, object],
    *,
    relation_labels: list[str],
) -> tuple[int, int, int, str, str]:
    interesting_relations = sum(
        1
        for label in relation_labels
        if label not in {"source", "resource", "resolved_by", "query", "drives"}
    )
    return (
        interesting_relations,
        1 if bool(node.get("materialized")) else 0,
        1 if bool(node.get("discovered")) else 0,
        str(node.get("kind") or ""),
        str(node.get("label") or "").lower(),
    )


def _semantic_focus_payload(
    lineage: dict[str, object],
    semantic: dict[str, object],
    *,
    focus: str,
    limit: int,
    scan_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    query = focus.strip()
    if not query:
        return {
            "sessionDir": semantic["sessionDir"],
            "contentDir": semantic["contentDir"],
            "focus": "",
            "matched": False,
            "empty": True,
            "nextStep": "Provide a focus keyword, locator, artifact name, or checksum prefix.",
            "nodeCount": 0,
            "edgeCount": 0,
            "nodes": [],
            "edges": [],
            "neighbors": [],
            "anchors": [],
            "matchedCount": 0,
        }
    nodes = [dict(node) for node in semantic.get("nodes") or []]
    node_index = {str(node["id"]): node for node in nodes}
    matches = sorted(
        (
            node
            for node in nodes
            if str(node.get("kind") or "") in {"source", "content"}
            and _analysis_focus_score(node, query)[0] > 0
        ),
        key=lambda node: _analysis_focus_score(node, query),
        reverse=True,
    )
    scan_entries = _ordered_focus_scan_entries(
        scan_payload,
        limit=max(limit * 2, 8),
    )
    seed_cap = max(4, min(max(limit, 1), 12))
    if not matches and not scan_entries:
        return {
            "sessionDir": semantic["sessionDir"],
            "contentDir": semantic["contentDir"],
            "focus": query,
            "matched": False,
            "empty": True,
            "nextStep": (
                f"No analyzed node or projected artifact matched `{query}`. Try a canonical locator, "
                "artifact name, checksum prefix, or a tighter keyword from session scan, leads, or manifest."
            ),
            "nodeCount": 0,
            "edgeCount": 0,
            "nodes": [],
            "edges": [],
            "neighbors": [],
            "anchors": [],
            "matchedCount": 0,
        }

    best_score = _analysis_focus_score(matches[0], query)[0] if matches else 0
    threshold = _focus_match_threshold(best_score)
    seed_ids: list[str] = []

    def add_seed(node_id: str) -> None:
        if node_id and node_id in node_index and node_id not in seed_ids:
            seed_ids.append(node_id)

    for node in matches:
        if _analysis_focus_score(node, query)[0] < threshold:
            break
        add_seed(str(node.get("id") or ""))
        if len(seed_ids) >= seed_cap:
            break
    for entry in scan_entries:
        checksum = str(entry.get("checksum") or "").strip()
        locator = str(entry.get("canonical_locator") or entry.get("locator") or "").strip()
        if checksum:
            add_seed(f"content:{checksum}")
        if locator:
            add_seed(f"source:{locator}")
        if len(seed_ids) >= seed_cap:
            break

    if not seed_ids:
        return {
            "sessionDir": semantic["sessionDir"],
            "contentDir": semantic["contentDir"],
            "focus": query,
            "matched": False,
            "empty": True,
            "nextStep": (
                f"No analyzed node or projected artifact matched `{query}`. Try a canonical locator, "
                "artifact name, checksum prefix, or a tighter keyword from session scan, leads, or manifest."
            ),
            "nodeCount": 0,
            "edgeCount": 0,
            "nodes": [],
            "edges": [],
            "neighbors": [],
            "anchors": [],
            "matchedCount": 0,
        }

    root = dict(node_index[seed_ids[0]])
    root["followCommand"] = _semantic_node_follow_command(root, lineage=lineage)
    root_id = str(root["id"])
    seed_records = []
    for node_id in seed_ids:
        node = dict(node_index[node_id])
        node["followCommand"] = _semantic_node_follow_command(node, lineage=lineage)
        seed_records.append(node)
    seed_id_set = set(seed_ids)
    structural_labels = {"source", "resource", "resolved_by", "query", "drives"}
    incident_edges = [
        dict(edge)
        for edge in semantic.get("edges") or []
        if (
            str(edge.get("source") or "") in seed_id_set
            or str(edge.get("target") or "") in seed_id_set
        )
    ]
    semantic_incident_edges = [
        edge for edge in incident_edges if str(edge.get("label") or "") not in structural_labels
    ]
    selected_edges = semantic_incident_edges or incident_edges
    selected_neighbor_ids: list[str] = []
    relation_labels_by_neighbor: dict[str, list[str]] = {}
    for edge in selected_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in seed_id_set and target in seed_id_set:
            continue
        neighbor_id = target if source in seed_id_set else source
        if not neighbor_id:
            continue
        if neighbor_id not in relation_labels_by_neighbor:
            relation_labels_by_neighbor[neighbor_id] = []
        relation_labels_by_neighbor[neighbor_id].append(str(edge.get("label") or ""))
        if neighbor_id not in selected_neighbor_ids:
            selected_neighbor_ids.append(neighbor_id)
    if len(selected_neighbor_ids) < max(2, limit // 2):
        for edge in incident_edges:
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            if source in seed_id_set and target in seed_id_set:
                continue
            neighbor_id = target if source in seed_id_set else source
            if not neighbor_id:
                continue
            if neighbor_id not in relation_labels_by_neighbor:
                relation_labels_by_neighbor[neighbor_id] = []
            relation_labels_by_neighbor[neighbor_id].append(str(edge.get("label") or ""))
            if neighbor_id not in selected_neighbor_ids:
                selected_neighbor_ids.append(neighbor_id)
    selected_neighbor_ids = [
        neighbor_id
        for neighbor_id in sorted(
            selected_neighbor_ids,
            key=lambda neighbor_id: _neighbor_sort_key(
                node_index.get(neighbor_id, {}),
                relation_labels=relation_labels_by_neighbor.get(neighbor_id, []),
            ),
            reverse=True,
        )[: max(limit, 0)]
        if neighbor_id in node_index
    ]
    selected_node_ids = {root_id, *seed_ids, *selected_neighbor_ids}
    focused_edges = [
        edge
        for edge in incident_edges
        if (
            str(edge.get("source") or "") in selected_node_ids
            and str(edge.get("target") or "") in selected_node_ids
        )
    ]
    neighbor_records = []
    for neighbor_id in selected_neighbor_ids:
        node = dict(node_index[neighbor_id])
        node["followCommand"] = _semantic_node_follow_command(node, lineage=lineage)
        relation_labels = relation_labels_by_neighbor.get(neighbor_id, [])
        neighbor_records.append(
            {
                **node,
                "relations": relation_labels,
            }
        )
    focused_nodes = [*seed_records, *neighbor_records]
    suppressed_count = max(len(incident_edges) - len(focused_edges), 0)
    return {
        "sessionDir": semantic["sessionDir"],
        "contentDir": semantic["contentDir"],
        "focus": query,
        "matched": True,
        "empty": False,
        "nextStep": "",
        "nodeCount": len(focused_nodes),
        "edgeCount": len(focused_edges),
        "root": root,
        "anchors": seed_records[1:],
        "matchedCount": len(seed_records),
        "neighbors": neighbor_records,
        "nodes": focused_nodes,
        "edges": focused_edges,
        "suppressedStructuralEdgeCount": suppressed_count,
    }


def _lineage_focus_score(item: dict[str, object], query: str) -> tuple[int, int, str]:
    query_lower = query.lower()
    candidates = [
        str(item.get("label") or ""),
        str(item.get("locator") or ""),
        str(item.get("artifactLocator") or ""),
        str(item.get("contentLocator") or ""),
        str(item.get("checksum") or ""),
    ]
    lowered = [candidate.lower() for candidate in candidates if candidate]
    score = 0
    if any(candidate == query_lower for candidate in lowered):
        score = 5
    elif any(candidate.startswith(query_lower) for candidate in lowered):
        score = 4
    elif any(f":{query_lower}" in candidate for candidate in lowered):
        score = 3
    elif any(query_lower in candidate for candidate in lowered):
        score = 2
    materialized = 1 if bool(item.get("materialized")) else 0
    label = str(item.get("label") or item.get("locator") or "")
    return (score, materialized, label.lower())


def _lineage_focus_payload(
    payload: dict[str, object],
    *,
    focus: str,
    limit: int,
    scan_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    query = focus.strip()
    if not query:
        return {
            "sessionDir": payload["sessionDir"],
            "contentDir": payload["contentDir"],
            "focus": "",
            "matched": False,
            "empty": True,
            "nextStep": "Provide a focus keyword, locator, artifact name, or checksum prefix.",
            "root": {},
            "neighbors": [],
            "sources": [],
            "content": [],
            "sourceEdges": [],
            "revisionEdges": [],
            "leadSources": [],
            "leadEdges": [],
            "discoveryArtifactCount": 0,
            "evidenceArtifactCount": 0,
            "anchors": [],
            "matchedCount": 0,
        }
    sources = [dict(item) for item in payload.get("sources") or []]
    content_items = [dict(item) for item in payload.get("content") or []]
    lead_sources = [dict(item) for item in payload.get("leadSources") or []]
    source_index = {str(item.get("locator") or ""): item for item in sources}
    content_index = {str(item.get("checksum") or ""): item for item in content_items}
    lead_index = {str(item.get("locator") or ""): item for item in lead_sources}
    candidates = [
        *(_lineage_source_candidate(item) for item in sources),
        *(_lineage_content_candidate(item) for item in content_items),
        *(
            _lineage_lead_candidate(item)
            for item in lead_sources
            if str(item.get("locator") or "") not in source_index
        ),
    ]
    matches = sorted(
        (candidate for candidate in candidates if _lineage_focus_score(candidate, query)[0] > 0),
        key=lambda candidate: _lineage_focus_score(candidate, query),
        reverse=True,
    )
    scan_entries = _ordered_focus_scan_entries(
        scan_payload,
        limit=max(limit * 2, 8),
    )
    seed_cap = max(4, min(max(limit, 1), 12))
    if not matches and not scan_entries:
        return {
            "sessionDir": payload["sessionDir"],
            "contentDir": payload["contentDir"],
            "focus": query,
            "matched": False,
            "empty": True,
            "nextStep": (
                f"No analyzed lineage anchor or projected artifact matched `{query}`. Try a canonical locator, "
                "artifact name, checksum prefix, or a tighter target from session scan, leads, or manifest."
            ),
            "root": {},
            "neighbors": [],
            "sources": [],
            "content": [],
            "sourceEdges": [],
            "revisionEdges": [],
            "leadSources": [],
            "leadEdges": [],
            "discoveryArtifactCount": 0,
            "evidenceArtifactCount": 0,
            "anchors": [],
            "matchedCount": 0,
        }
    best_score = _lineage_focus_score(matches[0], query)[0] if matches else 0
    threshold = _focus_match_threshold(best_score)
    seeds: list[dict[str, object]] = []
    seen_seed_keys: set[tuple[str, str]] = set()

    def add_seed(candidate: dict[str, object]) -> None:
        kind = str(candidate.get("kind") or "")
        if kind == "source":
            key = ("source", str(candidate.get("locator") or ""))
        elif kind == "content":
            key = ("content", str(candidate.get("checksum") or ""))
        else:
            key = ("lead", str(candidate.get("locator") or ""))
        if not key[1] or key in seen_seed_keys:
            return
        seen_seed_keys.add(key)
        seeds.append(candidate)

    for candidate in matches:
        if _lineage_focus_score(candidate, query)[0] < threshold:
            break
        add_seed(dict(candidate))
        if len(seeds) >= seed_cap:
            break
    for entry in scan_entries:
        checksum = str(entry.get("checksum") or "").strip()
        locator = str(entry.get("canonical_locator") or entry.get("locator") or "").strip()
        if checksum and checksum in content_index:
            add_seed(_lineage_content_candidate(content_index[checksum]))
        if locator and locator in source_index:
            add_seed(_lineage_source_candidate(source_index[locator]))
        if len(seeds) >= seed_cap:
            break

    if not seeds:
        return {
            "sessionDir": payload["sessionDir"],
            "contentDir": payload["contentDir"],
            "focus": query,
            "matched": False,
            "empty": True,
            "nextStep": (
                f"No analyzed lineage anchor or projected artifact matched `{query}`. Try a canonical locator, "
                "artifact name, checksum prefix, or a tighter target from session scan, leads, or manifest."
            ),
            "root": {},
            "neighbors": [],
            "sources": [],
            "content": [],
            "sourceEdges": [],
            "revisionEdges": [],
            "leadSources": [],
            "leadEdges": [],
            "discoveryArtifactCount": 0,
            "evidenceArtifactCount": 0,
            "anchors": [],
            "matchedCount": 0,
        }

    root = dict(seeds[0])
    selected_sources: set[str] = set()
    selected_content: set[str] = set()
    selected_leads: set[str] = set()
    matched_sources = {
        str(candidate.get("locator") or "")
        for candidate in matches
        if str(candidate.get("kind") or "") == "source"
    }
    matched_leads = {
        str(candidate.get("locator") or "")
        for candidate in matches
        if str(candidate.get("kind") or "") == "lead"
    }
    for candidate in seeds:
        if candidate["kind"] == "source":
            selected_sources.add(str(candidate.get("locator") or ""))
        elif candidate["kind"] == "content":
            selected_content.add(str(candidate.get("checksum") or ""))
        else:
            selected_leads.add(str(candidate.get("locator") or ""))

    def expand_source_and_revision_edges() -> None:
        for edge in payload.get("sourceEdges") or []:
            source = str(edge.get("source") or "")
            checksum = str(edge.get("checksum") or "")
            if source in selected_sources:
                selected_content.add(checksum)
            if checksum in selected_content:
                selected_sources.add(source)
        for edge in payload.get("revisionEdges") or []:
            from_checksum = str(edge.get("from") or "")
            to_checksum = str(edge.get("to") or "")
            if from_checksum in selected_content:
                selected_content.add(to_checksum)
            if to_checksum in selected_content:
                selected_content.add(from_checksum)

    expand_source_and_revision_edges()
    for edge in payload.get("leadEdges") or []:
        source_checksum = str(edge.get("sourceChecksum") or "")
        target_locator = str(edge.get("targetLocator") or "")
        target_is_source = target_locator in source_index
        target_matches_focus = (
            target_locator in matched_sources or target_locator in matched_leads
        )
        if source_checksum in selected_content and (
            target_locator in selected_sources
            or target_locator in selected_leads
            or target_matches_focus
        ):
            if target_is_source:
                selected_sources.add(target_locator)
            else:
                selected_leads.add(target_locator)
        if target_locator in selected_sources or target_locator in selected_leads:
            selected_content.add(source_checksum)
    expand_source_and_revision_edges()

    neighbor_candidates: list[dict[str, object]] = []
    for locator in sorted(selected_sources):
        if ("source", locator) in seen_seed_keys:
            continue
        source_item = source_index.get(locator)
        if source_item is None:
            continue
        neighbor_candidates.append(
            {
                "kind": "source",
                "label": locator,
                "relation": "materialized source",
                "followCommand": str(source_item.get("followCommand") or ""),
                "artifactKind": str(source_item.get("artifactKind") or ""),
                "materialized": True,
            }
        )
    for checksum in sorted(selected_content):
        if ("content", checksum) in seen_seed_keys:
            continue
        content_item = content_index.get(checksum)
        if content_item is None:
            continue
        neighbor_candidates.append(
            {
                "kind": "content",
                "label": str(content_item.get("preferredName") or checksum),
                "relation": "stored artifact",
                "followCommand": str(content_item.get("followCommand") or ""),
                "artifactKind": str(content_item.get("artifactKind") or ""),
                "materialized": True,
            }
        )
    for locator in sorted(selected_leads):
        if ("lead", locator) in seen_seed_keys:
            continue
        lead_item = lead_index.get(locator)
        if lead_item is None:
            continue
        neighbor_candidates.append(
            {
                "kind": "lead",
                "label": locator,
                "relation": "followable lead",
                "followCommand": str(lead_item.get("followCommand") or ""),
                "artifactKind": str(lead_item.get("artifactKind") or ""),
                "materialized": bool(lead_item.get("materialized")),
            }
        )
    ordered_neighbors = sorted(
        neighbor_candidates,
        key=lambda item: (
            1 if bool(item.get("materialized")) else 0,
            1 if str(item.get("kind") or "") == "content" else 0,
            str(item.get("label") or "").lower(),
        ),
        reverse=True,
    )[: max(limit, 0)]

    neighbor_source_labels = {
        str(item.get("label") or "")
        for item in ordered_neighbors
        if str(item.get("kind") or "") == "source"
    }
    neighbor_content_labels = {
        str(item.get("label") or "")
        for item in ordered_neighbors
        if str(item.get("kind") or "") == "content"
    }
    neighbor_content_checksums = {
        checksum
        for checksum, item in content_index.items()
        if str(item.get("preferredName") or checksum) in neighbor_content_labels
    }
    neighbor_lead_labels = {
        str(item.get("label") or "")
        for item in ordered_neighbors
        if str(item.get("kind") or "") == "lead"
    }
    seed_source_labels = {
        value for kind, value in seen_seed_keys if kind == "source"
    }
    seed_content_checksums = {
        value for kind, value in seen_seed_keys if kind == "content"
    }
    seed_lead_labels = {
        value for kind, value in seen_seed_keys if kind == "lead"
    }
    selected_sources = {
        locator
        for locator in selected_sources
        if locator in seed_source_labels or locator in neighbor_source_labels
    }
    selected_content = {
        checksum
        for checksum in selected_content
        if checksum in seed_content_checksums or checksum in neighbor_content_checksums
    }
    selected_leads = {
        locator
        for locator in selected_leads
        if locator in seed_lead_labels or locator in neighbor_lead_labels
    }

    selected_source_items = [item for item in sources if str(item.get("locator") or "") in selected_sources]
    selected_content_items = [
        item for item in content_items if str(item.get("checksum") or "") in selected_content
    ]
    selected_lead_items = [
        item
        for item in lead_sources
        if str(item.get("locator") or "") in selected_leads
        and str(item.get("locator") or "") not in selected_sources
    ]
    selected_source_edges = [
        edge
        for edge in payload.get("sourceEdges") or []
        if str(edge.get("source") or "") in selected_sources
        and str(edge.get("checksum") or "") in selected_content
    ]
    selected_revision_edges = [
        edge
        for edge in payload.get("revisionEdges") or []
        if str(edge.get("from") or "") in selected_content
        and str(edge.get("to") or "") in selected_content
    ]
    selected_lead_edges = [
        edge
        for edge in payload.get("leadEdges") or []
        if str(edge.get("sourceChecksum") or "") in selected_content
        and str(edge.get("targetLocator") or "") in selected_leads.union(selected_sources)
    ]
    discovery_count = sum(
        1 for item in selected_content_items if str(item.get("artifactKind") or "") == "discovery"
    )
    evidence_count = sum(
        1 for item in selected_content_items if str(item.get("artifactKind") or "") == "evidence"
    )
    return {
        "sessionDir": payload["sessionDir"],
        "contentDir": payload["contentDir"],
        "manifestPath": payload["manifestPath"],
        "focus": query,
        "matched": True,
        "empty": False,
        "nextStep": "",
        "root": root,
        "anchors": seeds[1:],
        "matchedCount": len(seeds),
        "neighbors": ordered_neighbors,
        "sources": selected_source_items,
        "content": selected_content_items,
        "sourceEdges": selected_source_edges,
        "revisionEdges": selected_revision_edges,
        "leadSources": selected_lead_items,
        "leadEdges": selected_lead_edges,
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "contentCount": len(selected_content_items),
        "sourceCount": len(selected_source_items),
        "sourceEdgeCount": len(selected_source_edges),
        "revisionEdgeCount": len(selected_revision_edges),
        "leadSourceCount": len(selected_lead_items),
        "leadEdgeCount": len(selected_lead_edges),
        "collisionCount": 0,
        "collisions": [],
        "duplicateMaterializationCount": 0,
        "duplicateMaterializations": [],
        "variantCount": 0,
        "variants": [],
    }


def _analysis_overview_payload(
    lineage: dict[str, object],
    semantic: dict[str, object],
    *,
    limit: int,
) -> dict[str, object]:
    node_groups = Counter(
        str(node.get("group") or "")
        for node in semantic.get("nodes") or []
        if str(node.get("group") or "")
    )
    node_kinds = Counter(
        str(node.get("kind") or "")
        for node in semantic.get("nodes") or []
        if str(node.get("kind") or "")
    )
    edge_labels = Counter(
        str(edge.get("label") or "")
        for edge in semantic.get("edges") or []
        if str(edge.get("label") or "")
    )
    provider_clusters = [
        {"provider": provider, "nodeCount": count}
        for provider, count in node_groups.most_common(max(limit, 0))
        if provider != "content"
    ]
    dominant_kinds = [
        {"kind": kind, "nodeCount": count}
        for kind, count in node_kinds.most_common(max(limit, 0))
    ]
    dominant_relations = [
        {"label": label, "edgeCount": count}
        for label, count in edge_labels.most_common(max(limit, 0))
    ]
    structural_edge_count = sum(
        count
        for label, count in edge_labels.items()
        if label in {"source", "resource", "resolved_by", "query", "drives"}
    )
    source_node_count = sum(
        count for kind, count in node_kinds.items() if kind in {"source", "query", "provider"}
    )
    anchors = sorted(
        [dict(item) for item in lineage.get("content") or []],
        key=lambda item: (
            1 if str(item.get("artifactKind") or "") == "evidence" else 0,
            int(item.get("fetchCount") or 0),
            str(item.get("lastFetchedAt") or ""),
            str(item.get("preferredName") or ""),
        ),
        reverse=True,
    )[: max(limit, 0)]
    queries = [
        dict(node)
        for node in semantic.get("nodes") or []
        if str(node.get("kind") or "") == "query"
    ][: max(limit, 0)]
    lead_sources = [dict(item) for item in (lineage.get("leadSources") or [])[: max(limit, 0)]]
    return {
        "sessionDir": lineage["sessionDir"],
        "contentDir": lineage["contentDir"],
        "manifestPath": lineage["manifestPath"],
        "contentCount": int(lineage.get("contentCount") or 0),
        "sourceCount": int(lineage.get("sourceCount") or 0),
        "leadSourceCount": int(lineage.get("leadSourceCount") or 0),
        "leadEdgeCount": int(lineage.get("leadEdgeCount") or 0),
        "semanticNodeCount": int(semantic.get("nodeCount") or 0),
        "semanticEdgeCount": int(semantic.get("edgeCount") or 0),
        "discoveryArtifactCount": int(lineage.get("discoveryArtifactCount") or 0),
        "evidenceArtifactCount": int(lineage.get("evidenceArtifactCount") or 0),
        "nextStep": str(lineage.get("nextStep") or semantic.get("nextStep") or ""),
        "providerClusters": provider_clusters,
        "dominantKinds": dominant_kinds,
        "dominantRelations": dominant_relations,
        "materializedAnchors": anchors,
        "querySeeds": queries,
        "bestLeads": lead_sources,
        "sourceHeavy": source_node_count * 2 >= max(int(semantic.get("nodeCount") or 0), 1),
        "structuralHeavy": structural_edge_count * 2 >= max(int(semantic.get("edgeCount") or 0), 1),
        "sourceNodeCount": source_node_count,
        "structuralEdgeCount": structural_edge_count,
    }


def _render_analysis_overview_text(payload: dict[str, object]) -> str:
    lines = [
        f"session: {payload['sessionDir']}",
        f"content: {payload['contentDir']}",
        (
            "artifacts: "
            f"{payload['contentCount']} "
            f"(discovery {payload['discoveryArtifactCount']}, "
            f"evidence {payload['evidenceArtifactCount']})"
        ),
        (
            "graph: "
            f"{payload['sourceCount']} sources, "
            f"{payload['leadSourceCount']} lead sources, "
            f"{payload['leadEdgeCount']} lead edges"
        ),
        (
            "semantic: "
            f"{payload['semanticNodeCount']} nodes, "
            f"{payload['semanticEdgeCount']} edges"
        ),
    ]
    shape_parts: list[str] = []
    if bool(payload.get("sourceHeavy")):
        shape_parts.append(
            f"source-heavy ({int(payload['sourceNodeCount'])}/{int(payload['semanticNodeCount'])} source/query/provider nodes)"
        )
    if bool(payload.get("structuralHeavy")):
        shape_parts.append(
            f"structural-edge-heavy ({int(payload['structuralEdgeCount'])}/{int(payload['semanticEdgeCount'])} structural edges)"
        )
    if shape_parts:
        lines.append("shape: " + "; ".join(shape_parts))
    if payload.get("nextStep"):
        lines.append(f"next: {payload['nextStep']}")
    if payload["providerClusters"]:
        lines.append("provider clusters:")
        for cluster in payload["providerClusters"]:
            lines.append(f"  - {cluster['provider']}: {cluster['nodeCount']} nodes")
    if payload["dominantKinds"]:
        lines.append("dominant node kinds:")
        for item in payload["dominantKinds"]:
            lines.append(f"  - {item['kind']}: {item['nodeCount']}")
    if payload["dominantRelations"]:
        lines.append("dominant relations:")
        for item in payload["dominantRelations"]:
            lines.append(f"  - {item['label']}: {item['edgeCount']}")
    if payload["materializedAnchors"]:
        lines.append("materialized anchors:")
        for anchor in payload["materializedAnchors"]:
            providers = ", ".join(str(value) for value in anchor.get("providers") or [])
            visibility = _visibility_summary(anchor)
            lines.append(
                f"  - [{anchor.get('artifactKind') or 'artifact'}] {anchor['preferredName']}"
            )
            if providers:
                lines.append(f"    providers: {providers}")
            if visibility:
                lines.append(f"    visibility: {visibility}")
            lines.append(f"    follow: `{anchor['followCommand']}`")
    if payload["querySeeds"]:
        lines.append("query seeds:")
        for node in payload["querySeeds"]:
            lines.append(f"  - {node['label']}")
    if payload["bestLeads"]:
        lines.append("best leads:")
        for lead in payload["bestLeads"]:
            relation = ", ".join(str(value) for value in lead.get("relationKinds") or [] if str(value))
            lines.append(
                f"  - [{'; '.join(_lead_signal_labels(lead, aggregated=True))}] "
                f"{lead['locator']} ({lead['provider']}, {relation or 'lead'})"
            )
            lines.append(f"    follow: `{lead['followCommand']}`")
    lines.append(
        "focus: use `gotta session analyze --focus <locator|keyword> --session <session> --stdout` "
        "to inspect one local neighborhood instead of dumping the full graph."
    )
    return "\n".join(lines)


def _render_analysis_focus_text(payload: dict[str, object]) -> str:
    lines = [
        f"session: {payload['sessionDir']}",
        f"focus: {payload['focus'] or '(empty)'}",
    ]
    if not payload.get("matched"):
        if payload.get("nextStep"):
            lines.append(f"next: {payload['nextStep']}")
        return "\n".join(lines)
    root = payload["root"]
    lines.append(
        f"matched: {root['label']} ({root['kind']}, {root['group']})"
    )
    if int(payload.get("matchedCount") or 0) > 1:
        lines.append(
            f"signal: {int(payload['matchedCount'])} anchors matched this focus; "
            "showing the strongest root plus nearby corroborating anchors"
        )
    state_bits = []
    if root.get("artifactKind"):
        state_bits.append(f"artifact_kind={root['artifactKind']}")
    if bool(root.get("materialized")):
        state_bits.append("materialized")
    if bool(root.get("discovered")) and not bool(root.get("materialized")):
        state_bits.append("discovered-only")
    if state_bits:
        lines.append("state: " + ", ".join(state_bits))
    if root.get("followCommand"):
        lines.append(f"follow: `{root['followCommand']}`")
    anchors = payload.get("anchors") or []
    if anchors:
        lines.append("also matched:")
        for anchor in anchors:
            lines.append(
                f"  - {anchor['label']} ({anchor['kind']}, {anchor['group']})"
            )
            if anchor.get("followCommand"):
                lines.append(f"    follow: `{anchor['followCommand']}`")
    if int(payload.get("suppressedStructuralEdgeCount") or 0) > 0:
        lines.append(
            "signal: "
            f"suppressed {payload['suppressedStructuralEdgeCount']} lower-signal structural edges "
            "to keep this neighborhood readable"
        )
    if payload["neighbors"]:
        lines.append("neighbors:")
        for neighbor in payload["neighbors"]:
            relation = ", ".join(str(value) for value in neighbor.get("relations") or [] if str(value))
            lines.append(
                f"  - {neighbor['label']} ({neighbor['kind']}, {neighbor['group']}; {relation or 'adjacent'})"
            )
            bits = []
            if neighbor.get("artifactKind"):
                bits.append(f"artifact_kind={neighbor['artifactKind']}")
            if bool(neighbor.get("materialized")):
                bits.append("materialized")
            if bool(neighbor.get("discovered")) and not bool(neighbor.get("materialized")):
                bits.append("discovered-only")
            if bits:
                lines.append("    state: " + ", ".join(bits))
            if neighbor.get("followCommand"):
                lines.append(f"    follow: `{neighbor['followCommand']}`")
    else:
        lines.append("neighbors: none")
    return "\n".join(lines)


def _render_lineage_focus_text(payload: dict[str, object]) -> str:
    lines = [
        f"session: {payload['sessionDir']}",
        f"focus: {payload['focus'] or '(empty)'}",
    ]
    if not payload.get("matched"):
        if payload.get("nextStep"):
            lines.append(f"next: {payload['nextStep']}")
        return "\n".join(lines)
    root = payload["root"]
    lines.append(f"matched: {root['label']} ({root['kind']})")
    if int(payload.get("matchedCount") or 0) > 1:
        lines.append(
            f"signal: {int(payload['matchedCount'])} anchors matched this focus; "
            "showing the strongest root plus nearby corroborating anchors"
        )
    state_bits = []
    if root.get("artifactKind"):
        state_bits.append(f"artifact_kind={root['artifactKind']}")
    if bool(root.get("materialized")):
        state_bits.append("materialized")
    else:
        state_bits.append("discovered-only")
    if state_bits:
        lines.append("state: " + ", ".join(state_bits))
    if root.get("followCommand"):
        lines.append(f"follow: `{root['followCommand']}`")
    anchors = payload.get("anchors") or []
    if anchors:
        lines.append("also matched:")
        for anchor in anchors:
            lines.append(f"  - {anchor['label']} ({anchor['kind']})")
            if anchor.get("followCommand"):
                lines.append(f"    follow: `{anchor['followCommand']}`")
    if payload["neighbors"]:
        lines.append("neighbors:")
        for neighbor in payload["neighbors"]:
            lines.append(
                f"  - {neighbor['label']} ({neighbor['kind']}; {neighbor['relation']})"
            )
            bits = []
            if neighbor.get("artifactKind"):
                bits.append(f"artifact_kind={neighbor['artifactKind']}")
            bits.append("materialized" if bool(neighbor.get("materialized")) else "discovered-only")
            lines.append("    state: " + ", ".join(bits))
            if neighbor.get("followCommand"):
                lines.append(f"    follow: `{neighbor['followCommand']}`")
    else:
        lines.append("neighbors: none")
    return "\n".join(lines)


def _lead_signal_labels(lead: dict[str, object], *, aggregated: bool) -> list[str]:
    labels = [
        "native" if bool(lead.get("firstParty")) else "web",
        "materialized" if bool(lead.get("materialized")) else "unmaterialized",
    ]
    if bool(lead.get("searchSeed")):
        labels.append("search seed")
    if aggregated:
        labels.append(f"artifacts {int(lead.get('artifactCount') or 0)}")
    labels.append(f"mentions {int(lead.get('occurrenceCount') or 0)}")
    search_like_source_count = int(lead.get("searchLikeSourceCount") or 0)
    artifact_count = int(lead.get("artifactCount") or 0)
    if search_like_source_count:
        if artifact_count and search_like_source_count >= artifact_count:
            labels.append("search/listing only")
        else:
            labels.append("partly search/listing driven")
    return labels


def _stored_target_locators(lead: dict[str, object]) -> list[str]:
    return [
        str(value)
        for value in (
            list(lead.get("artifactLocators") or [])
            + list(lead.get("contentLocators") or [])
            + list(lead.get("targetArtifactLocators") or [])
            + list(lead.get("targetContentLocators") or [])
        )
        if str(value)
    ]


def _render_search_origins(lead: dict[str, object]) -> str:
    origins = [
        origin
        for origin in lead.get("searchOrigins") or []
        if isinstance(origin, dict)
    ]
    parts: list[str] = []
    for origin in origins[:3]:
        provider = str(origin.get("provider") or "unknown")
        subcommand = str(origin.get("subcommand") or "search")
        rank = int(origin.get("rank") or 0)
        label = f"{provider}/{subcommand}"
        if rank > 0:
            label += f" #{rank}"
        parts.append(label)
    return ", ".join(parts)


def cmd_graph(args: argparse.Namespace) -> int:
    dirs = resolve_dirs(_options_from_args(args), create=False)
    _require_started_session(dirs)
    payload = _graph_payload(dirs)
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(_render_mermaid(payload))
    return 0


def cmd_leads(args: argparse.Namespace) -> int:
    dirs = resolve_dirs(_options_from_args(args), create=False)
    _require_started_session(dirs)
    payload = _leads_payload(
        dirs,
        target=args.target or "",
        limit=max(args.limit, 0),
        offset=max(args.offset, 0),
        include_all=bool(args.all),
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"session: {payload['sessionDir']}")
    print(f"target: {payload['target'] or '(session-wide)'}")
    print(
        "artifacts: "
        f"{payload['artifactCount']} "
        f"(discovery {payload['discoveryArtifactCount']}, "
        f"evidence {payload['evidenceArtifactCount']})"
    )
    print(
        "leads: "
        f"{payload['leadCount']} total (showing {payload['shownCount']}; "
        f"materialized {payload['materializedLeadCount']}, "
        f"unmaterialized {payload['unmaterializedLeadCount']})"
    )
    print(
        _paging_summary_line(
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
    if payload["nextStep"]:
        print(f"next: {payload['nextStep']}")
    if payload["leadSources"]:
        print("best leads:")
        for lead in payload["leadSources"]:
            relation = ", ".join(str(value) for value in lead.get("relationKinds") or [] if str(value))
            print(
                f"  - [{'; '.join(_lead_signal_labels(lead, aggregated=True))}] "
                f"{lead['locator']} ({lead['provider']}, {relation or 'lead'})"
            )
            visibility = _visibility_summary(lead)
            if visibility:
                print(f"    visibility: {visibility}")
            print(f"    follow: `{lead['followCommand']}`")
            stored_targets = _stored_target_locators(lead)
            if stored_targets:
                print(
                    "    stored_target: "
                    + ", ".join(f"`{value}`" for value in stored_targets)
                )
            search_origins = _render_search_origins(lead)
            if search_origins:
                print(f"    search_origin: {search_origins}")
            contexts = [str(value) for value in lead.get("contexts") or [] if str(value)]
            if contexts:
                print(f"    context: {contexts[0]}")
    if payload["artifacts"] and (payload["target"] or payload["artifactCount"] == 1):
        print("source context:")
        for artifact in payload["artifacts"]:
            print(f"- {artifact['preferredName']} ({str(artifact['checksum'])[:12]})")
            print(f"  source: `{artifact['sourceLocator'] or 'unknown'}`")
            if artifact.get("artifactKind"):
                print(f"  artifact_kind: {artifact['artifactKind']}")
            artifact_visibility = _visibility_summary(artifact)
            if artifact_visibility:
                print(f"  visibility: {artifact_visibility}")
            print(
                f"  stored: `{artifact['artifactLocator']}`, `{artifact['contentLocator']}`"
            )
            if artifact["lastFetchedAt"]:
                print(f"  fetched: {artifact['lastFetchedAt']}")
            if not artifact["leads"]:
                print("  leads: none")
                continue
            for lead in artifact["leads"]:
                print(
                    f"  - [{'; '.join(_lead_signal_labels(lead, aggregated=False))}] "
                    f"{lead['targetLocator']} ({lead['provider']}, {lead['relation']})"
                )
                visibility = _visibility_summary(lead)
                if visibility:
                    print(f"    visibility: {visibility}")
                print(f"    follow: `{lead['followCommand']}`")
                stored_targets = _stored_target_locators(lead)
                if stored_targets:
                    print(
                        "    stored_target: "
                        + ", ".join(f"`{value}`" for value in stored_targets)
                    )
                if bool(lead.get("sourceSearchLike")):
                    provider = str(lead.get("sourceProvider") or "unknown")
                    subcommand = str(lead.get("sourceSubcommand") or "search")
                    rank = int(lead.get("sourceRank") or 0)
                    origin = f"{provider}/{subcommand}"
                    if rank > 0:
                        origin += f" #{rank}"
                    print(f"    source_origin: {origin}")
                contexts = [str(value) for value in lead.get("contexts") or [] if str(value)]
                if contexts:
                    print(f"    context: {contexts[0]}")
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    dirs = resolve_dirs(_options_from_args(args), create=False)
    _require_started_session(dirs)
    payload = _manifest_payload(
        dirs,
        plugin=args.plugin or "",
        actor=args.actor or "",
        locator=args.locator or "",
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
    )
    print(
        _paging_summary_line(
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
    print("follow: pass any emitted locator directly to `gotta read <locator>`")
    for entry in payload["entries"]:
        fetched_at = str(entry.get("fetched_at", "")).strip() or "unknown-time"
        plugin_list = [str(value).strip() for value in list(entry.get("plugins") or []) if str(value).strip()]
        actor_list = [str(value).strip() for value in list(entry.get("actors") or []) if str(value).strip()]
        plugin = ", ".join(plugin_list) or str(entry.get("plugin", "")).strip() or "unknown-plugin"
        actor = (
            ", ".join(actor_list)
            or _rendered_actor(entry.get("actor"), session_root=dirs.session_dir)
        )
        locator = str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip() or "unknown"
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
        artifact_kind = str(entry.get("artifactKind") or "").strip()
        if artifact_kind:
            print(f"  artifact_kind: {artifact_kind}")
        visibility = _visibility_summary(entry)
        if visibility:
            print(f"  visibility: {visibility}")
        if entry.get("artifact_locator") or entry.get("content_locator"):
            print(
                "  "
                + "stored: "
                + ", ".join(
                    part
                    for part in (
                        f"`{entry.get('artifact_locator')}`" if entry.get("artifact_locator") else "",
                        f"`{entry.get('content_locator')}`" if entry.get("content_locator") else "",
                    )
                    if part
                )
            )
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    dirs = resolve_dirs(_options_from_args(args), create=False)
    _require_started_session(dirs)
    payload = _timeline_payload(
        dirs,
        limit=max(args.limit, 0),
        offset=max(args.offset, 0),
        include_all=bool(args.all),
        mode=args.mode,
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"timeline: {payload['manifestPath']}")
    print(f"activity: {payload['activityPath']}")
    print(f"mode: {payload['mode']} ({payload['modeDescription']})")
    print(f"coverage_gaps: {payload.get('coverageGapCount', 0)}")
    print(
        "events: "
        f"{payload['eventCount']} total "
        f"(discovery {payload['discoveryArtifactCount']}, "
        f"evidence {payload['evidenceArtifactCount']})"
    )
    print(
        _paging_summary_line(
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
    for event in payload["events"]:
        actor_label = event["actor"]
        if event.get("target_actor") and event["target_actor"] != actor_label:
            actor_label = f"{actor_label}->{event['target_actor']}"
        checksum = event["checksum"][:12] if event["checksum"] else "unknown"
        if payload["mode"] != "acquired":
            print(
                f"- {event.get('source_time') or 'unknown-time'} "
                f"[{event['plugin']}/{actor_label}] "
                f"{event['locator']} -> {event['preferred_name']} ({checksum}) "
                f"(from {event.get('source_time_field') or 'unknown-field'})"
            )
            if event.get("artifactKind"):
                print(f"  artifact_kind: {event['artifactKind']}")
            visibility = _visibility_summary(event)
            if visibility:
                print(f"  visibility: {visibility}")
            if event.get("artifact_locator") or event.get("content_locator"):
                print(
                    "  "
                    + "stored: "
                    + ", ".join(
                        part
                        for part in (
                            f"`{event.get('artifact_locator')}`" if event.get("artifact_locator") else "",
                            f"`{event.get('content_locator')}`" if event.get("content_locator") else "",
                        )
                        if part
                    )
                )
        else:
            print(
                f"- {event['fetched_at'] or 'unknown-time'} "
                f"[{event['plugin']}/{actor_label}] "
                f"{event['locator']} -> {event['preferred_name']} ({checksum})"
                f"{' (local)' if event.get('event_kind') == 'local' else ''}"
            )
            if event.get("artifactKind"):
                print(f"  artifact_kind: {event['artifactKind']}")
            visibility = _visibility_summary(event)
            if visibility:
                print(f"  visibility: {visibility}")
            if event.get("artifact_locator") or event.get("content_locator"):
                print(
                    "  "
                    + "stored: "
                    + ", ".join(
                        part
                        for part in (
                            f"`{event.get('artifact_locator')}`" if event.get("artifact_locator") else "",
                            f"`{event.get('content_locator')}`" if event.get("content_locator") else "",
                        )
                        if part
                    )
                )
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    dirs = resolve_dirs(_options_from_args(args), create=False)
    _require_started_session(dirs)
    graph_path, json_path, summary_path = _analysis_output_paths(dirs.session_dir)
    semantic_graph_path, semantic_json_path = _semantic_output_paths(dirs.session_dir)
    payload = _analysis_payload(dirs)
    mermaid = _render_analysis_mermaid(payload)
    semantic_payload = _semantic_payload(dirs)
    semantic_mermaid = _render_semantic_mermaid(semantic_payload)
    summary = {
        "sessionDir": payload["sessionDir"],
        "contentDir": payload["contentDir"],
        "manifestPath": payload["manifestPath"],
        "graphMermaidPath": str(graph_path),
        "graphMermaidMarkdownPath": str(_wrapped_markdown_path(graph_path)),
        "graphJsonPath": str(json_path),
        "semanticGraphMermaidPath": str(semantic_graph_path),
        "semanticGraphMermaidMarkdownPath": str(_wrapped_markdown_path(semantic_graph_path)),
        "semanticGraphJsonPath": str(semantic_json_path),
        "summaryPath": str(summary_path),
        "contentCount": payload["contentCount"],
        "sourceCount": payload["sourceCount"],
        "sourceEdgeCount": payload["sourceEdgeCount"],
        "revisionEdgeCount": payload["revisionEdgeCount"],
        "discoveryArtifactCount": payload["discoveryArtifactCount"],
        "evidenceArtifactCount": payload["evidenceArtifactCount"],
        "collisionCount": payload["collisionCount"],
        "collisions": payload["collisions"],
        "duplicateMaterializationCount": payload["duplicateMaterializationCount"],
        "duplicateMaterializations": payload["duplicateMaterializations"],
        "variantCount": payload["variantCount"],
        "variants": payload["variants"],
        "leadSourceCount": payload["leadSourceCount"],
        "leadEdgeCount": payload["leadEdgeCount"],
        "materializedLeadSourceCount": payload["materializedLeadSourceCount"],
        "unmaterializedLeadSourceCount": payload["unmaterializedLeadSourceCount"],
        "semanticNodeCount": semantic_payload["nodeCount"],
        "semanticEdgeCount": semantic_payload["edgeCount"],
    }
    if args.mode in {"lineage", "all"}:
        _write_mermaid_artifact(graph_path, mermaid)
        write_text_atomic(
            json_path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )
    if args.mode in {"semantic", "all"}:
        _write_mermaid_artifact(semantic_graph_path, semantic_mermaid)
        write_text_atomic(
            semantic_json_path,
            json.dumps(semantic_payload, indent=2, sort_keys=True) + "\n",
        )
    write_text_atomic(summary_path, json.dumps(summary, indent=2, sort_keys=True) + "\n")

    if args.stdout:
        focus_payload = None
        if str(getattr(args, "focus", "") or "").strip():
            focus_query = str(args.focus)
            focus_limit = max(int(getattr(args, "limit", 8) or 0), 0)
            focus_scan_payload = _scan_payload(
                dirs,
                query=focus_query,
                limit=max(focus_limit * 2, 12),
                include_all=True,
            )
            if args.mode == "lineage":
                focus_payload = _lineage_focus_payload(
                    payload,
                    focus=focus_query,
                    limit=focus_limit,
                    scan_payload=focus_scan_payload,
                )
            else:
                focus_payload = _semantic_focus_payload(
                    payload,
                    semantic_payload,
                    focus=focus_query,
                    limit=focus_limit,
                    scan_payload=focus_scan_payload,
                )
        if args.output == "text":
            if focus_payload is not None:
                if args.mode == "lineage":
                    print(_render_lineage_focus_text(focus_payload))
                else:
                    print(_render_analysis_focus_text(focus_payload))
                return 0
            overview = _analysis_overview_payload(
                payload,
                semantic_payload,
                limit=max(int(getattr(args, "limit", 8) or 0), 0),
            )
            print(_render_analysis_overview_text(overview))
            return 0
        if args.mode == "semantic":
            if args.output == "mermaid" and focus_payload is not None:
                print(_render_semantic_mermaid(focus_payload))
                return 0
            if args.output == "mermaid":
                print(semantic_mermaid)
                return 0
            if args.output == "json":
                print(
                    json.dumps(
                        focus_payload if focus_payload is not None else semantic_payload,
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
        if args.output == "mermaid" and focus_payload is not None:
            if args.mode == "lineage":
                print(_render_analysis_mermaid(focus_payload))
            else:
                print(_render_semantic_mermaid(focus_payload))
            return 0
        if args.output == "mermaid":
            print(mermaid)
            return 0
        if args.output == "json":
            print(
                json.dumps(
                    focus_payload if focus_payload is not None else payload,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    dirs = resolve_dirs(_options_from_args(args), create=False)
    _require_started_session(dirs)
    payload = _scan_payload(
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
        _paging_summary_line(
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
        plugins = ", ".join(str(value) for value in entry.get("plugins") or [] if str(value))
        actors = ", ".join(str(value) for value in entry.get("actors") or [] if str(value))
        locator_value = str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip() or "unknown"
        preferred_name = str(entry.get("preferred_name", "")).strip() or "data"
        print(
            f"- [{entry.get('artifactKind') or 'artifact'}; hits {entry.get('hitCount') or 0}] "
            f"{locator_value} -> {preferred_name}"
        )
        if plugins:
            print(f"  plugins: {plugins}")
        if actors:
            print(f"  actors: {actors}")
        visibility = _visibility_summary(entry)
        if visibility:
            print(f"  visibility: {visibility}")
        print(
            "  stored: "
            + ", ".join(
                part
                for part in (
                    f"`{entry.get('artifactLocator')}`" if entry.get("artifactLocator") else "",
                    f"`{entry.get('contentLocator')}`" if entry.get("contentLocator") else "",
                )
                if part
            )
        )
        print(f"  follow: `{entry.get('artifactFollowCommand') or entry.get('followCommand')}`")
        for snippet in entry.get("snippets") or []:
            print(
                f"  snippet {snippet.get('startLine')}-{snippet.get('endLine')}"
                f" (hits {', '.join(str(value) for value in snippet.get('hitLines') or [])})"
            )
            for line in snippet.get("lines") or []:
                print(f"    {line['number']:>5} | {line['text']}")
    return 0


def main(argv: list[str]) -> int:
    parser = build_parser()
    if is_long_help_request(argv):
        return print_long_help(parser)
    try:
        args = parser.parse_args(argv)
        command = args.command or "show"
        if command == "bind":
            return cmd_bind(args)
        if command == "show":
            return cmd_show(args)
        if command == "init":
            return cmd_init(args)
        if command == "doctor":
            return cmd_doctor(args)
        if command == "manifest":
            return cmd_manifest(args)
        if command == "timeline":
            return cmd_timeline(args)
        if command == "graph":
            return cmd_graph(args)
        if command == "leads":
            return cmd_leads(args)
        if command == "analyze":
            return cmd_analyze(args)
        if command == "scan":
            return cmd_scan(args)
    except ContentError as exc:
        if (
            "missing shared content context" in str(exc)
            and getattr(args, "command", None)
            in {None, "show", "doctor", "manifest", "timeline", "graph", "leads", "analyze", "scan"}
        ):
            parser.exit(
                status=2,
                message="start or bind a session first with `gotta ...` or `gotta session init`\n",
            )
        parser.exit(status=2, message=f"{exc}\n")
    return 2
