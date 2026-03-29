"""Shared helpers for the gotta session inspection plugin."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import hashlib
from pathlib import Path
import re
from typing import TypeVar
import urllib.parse
import uuid

from gotta.content.model import ContentSnapshot
from gotta.content.path import artifact_locator, content_locator, sh_quote
from gotta.content.scope import (
    resolve_session_reference,
    session_identity,
    session_shared_id,
    shared_session_root,
)
from gotta.source.visibility import (
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

SUMMARY_BUCKET_LIMIT = 5
GRAPH_TEXT_PREVIEW_LIMIT = 8
MANIFEST_TEXT_PREVIEW_LIMIT = 12
TIMELINE_TEXT_PREVIEW_LIMIT = 20
LEADS_BEST_OVERALL_LIMIT = 6
LEADS_PROVIDER_HIGHLIGHT_LIMIT = 4
ANALYZE_ANCHOR_PREVIEW_LIMIT = 4
ANALYZE_FOCUS_MATCH_PREVIEW_LIMIT = 4
ANALYZE_FOCUS_NEIGHBOR_PREVIEW_LIMIT = 8
_ItemT = TypeVar("_ItemT")

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

LOCAL_TIMELINE_FILES = ("WANT.md", "GOAL.md")


def _fallback_actor(session_root: Path) -> str:
    return session_identity(session_root) or "unknown"


def rendered_actor(raw: object, *, session_root: Path) -> str:
    value = str(raw or "").strip()
    return value or _fallback_actor(session_root)


def match_filter_text(raw: object) -> str:
    return str(raw or "").strip()


def compile_filter_pattern(raw_query: object) -> re.Pattern[str] | None:
    query = match_filter_text(raw_query)
    if not query:
        return None
    try:
        return re.compile(query, re.IGNORECASE)
    except re.error as exc:
        raise SystemExit(f"invalid filter pattern: {exc}") from exc


def iter_match_strings(value: object):
    if value is None:
        return
    if isinstance(value, dict):
        for nested in value.values():
            yield from iter_match_strings(nested)
        return
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from iter_match_strings(nested)
        return
    text = str(value).strip()
    if text:
        yield text


def match_any(pattern: re.Pattern[str] | None, *values: object) -> bool:
    if pattern is None:
        return True
    for value in values:
        for text in iter_match_strings(value):
            if pattern.search(text):
                return True
    return False


def filter_suffix(raw_query: object) -> str:
    query = match_filter_text(raw_query)
    return f"; filter {query!r}" if query else ""


def _list_value(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _int_value(value: object) -> int:
    return value if isinstance(value, int) else 0


def paginate_items(
    items: Sequence[_ItemT],
    *,
    limit: int,
    offset: int,
    include_all: bool,
    default_tail_window: bool = False,
) -> tuple[list[_ItemT], dict[str, object]]:
    total_count = len(items)
    normalized_limit = max(limit, 0)
    explicit_offset = max(offset, 0)
    applied_offset = explicit_offset
    if (
        default_tail_window
        and not include_all
        and explicit_offset == 0
        and normalized_limit > 0
    ):
        applied_offset = max(total_count - normalized_limit, 0)
    if include_all:
        paged = list(items[applied_offset:])
        applied_limit: int | None = None
    else:
        paged = list(items[applied_offset : applied_offset + normalized_limit])
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


def paging_summary_line(
    *,
    label: str,
    total_count: int,
    shown_count: int,
    offset: int,
    next_offset: int | None,
) -> str:
    parts = [
        f"{label}: {total_count} total",
        f"showing {shown_count}",
        f"offset {offset}",
    ]
    if next_offset is not None:
        parts.append(f"next {next_offset}")
    return "; ".join(parts)


def session_read_command(target: str, *, session_ref: str = "") -> str:
    parts = ["gotta read"]
    if session_ref:
        parts.append(f"--session {sh_quote(session_ref)}")
    parts.append(sh_quote(target))
    return " ".join(parts)


def follow_command(locator: str, *, checksum: str = "", session_ref: str = "") -> str:
    target = locator.strip() or (content_locator(checksum.strip()) if checksum else "")
    return session_read_command(target or "unknown", session_ref=session_ref)


def artifact_human_locator(preferred_name: str, checksum: str) -> str:
    if not checksum.strip():
        return ""
    return artifact_locator(preferred_name or "data", checksum)


def visibility_summary(payload: Mapping[str, object]) -> str:
    visibility = normalize_visibility_metadata(dict(payload))
    if not visibility:
        return ""
    return (
        f"{visibility['visibility_level']} "
        f"({visibility['visibility_boundary']}, {visibility['visibility_confidence']})"
    )


def resolved_visibility_metadata(
    payload: Mapping[str, object],
    *,
    provider: str = "",
    plugin: str = "",
    subcommand: str = "",
    locator: str = "",
) -> dict[str, object]:
    existing = normalize_visibility_metadata(dict(payload))
    classification_payload = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "visibility_level",
            "visibility_boundary",
            "visibility_confidence",
            "visibility_basis",
        }
    }
    classified = classify_visibility_metadata(
        classification_payload,
        provider=provider,
        plugin=plugin,
        subcommand=subcommand,
        locator=locator,
    )
    return best_visibility_metadata(existing, classified)


def artifact_kind(raw: object) -> str:
    value = str(raw or "").strip().lower()
    return value if value in {"discovery", "evidence"} else ""


def artifact_kind_counts(records: Sequence[Mapping[str, object]]) -> tuple[int, int]:
    discovery = 0
    evidence = 0
    for record in records:
        kind = artifact_kind(record.get("artifact_kind") or record.get("artifactKind"))
        if kind == "discovery":
            discovery += 1
        elif kind == "evidence":
            evidence += 1
    return discovery, evidence


def top_count_records(
    values: list[str],
    *,
    key: str,
    limit: int = SUMMARY_BUCKET_LIMIT,
) -> list[dict[str, object]]:
    counter = Counter(value for value in values if value)
    return [{key: label, "count": count} for label, count in counter.most_common(limit)]


def append_count_section(
    lines: list[str],
    *,
    heading: str,
    records: Sequence[Mapping[str, object]],
    key: str,
) -> None:
    if not records:
        return
    lines.append(f"{heading}:")
    for record in records:
        lines.append(f"  - {record[key]}: {_int_value(record.get('count'))}")


def append_preview_heading(
    lines: list[str],
    *,
    heading: str,
    shown: int,
    total: int,
) -> None:
    if total <= 0:
        return
    if shown >= total:
        lines.append(f"{heading}:")
        return
    lines.append(f"{heading} (showing {shown} of {total}):")


def topology_next_step(*, discovery_count: int, evidence_count: int) -> str:
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


def empty_topology_next_step() -> str:
    return topology_next_step(discovery_count=0, evidence_count=0)


def no_leads_next_step(*, has_artifacts: bool) -> str:
    if not has_artifacts:
        return empty_topology_next_step()
    return (
        "No explicit followable leads were mined from the current artifact set. Continue "
        "from the strongest materialized anchors with provider-native search/read surfaces, "
        "or inspect manifest and timeline to choose the next anchor."
    )


def mermaid_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def mermaid_label(value: str) -> str:
    escaped = (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return escaped.replace("\n", "<br/>")


def analysis_mermaid_id(prefix: str, value: str) -> str:
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"{prefix}:{value}").hex[:12]
    return f"{prefix}_{digest}"


def argv_output(argv: object) -> str:
    if not isinstance(argv, list):
        return ""
    for index, item in enumerate(argv):
        token = str(item)
        if token.startswith("--output="):
            return token.split("=", 1)[1].strip()
        if token == "--output" and index + 1 < len(argv):
            return str(argv[index + 1]).strip()
    return ""


def render_variant(snapshot: ContentSnapshot) -> tuple[str, str]:
    metadata = snapshot.artifact.metadata
    raw_subcommand = str(metadata.get("subcommand", "")).strip()
    locator = str(
        metadata.get("canonical_locator", "") or metadata.get("locator", "")
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
    output = argv_output(metadata.get("argv"))
    if not output:
        name = str(metadata.get("preferred_name", "")).strip().lower()
        extension = Path(name).suffix.lower()
        output = {
            ".summary": "summary",
            ".md": "markdown",
            ".json": "json",
            ".txt": "text",
            ".html": "html",
            ".csv": "csv",
        }.get(extension, "")
    content_type = str(metadata.get("content_type", "")).strip().lower()
    flavor = output or content_type or "default"
    return (subcommand, flavor)


def render_variant_label(variant: tuple[str, str]) -> str:
    subcommand, flavor = variant
    if subcommand == "default":
        return flavor
    return f"{subcommand}/{flavor}"


def provider_name(
    locator: str,
    *,
    plugins: list[str] | None = None,
    fallback: str = "unknown",
) -> str:
    preferred_plugins = [
        plugin for plugin in (plugins or []) if plugin and plugin != "read"
    ]
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


def query_label(locator: str) -> str:
    match = re.search(r"(?:^|:)(search|jql|cql)\s+(.+)$", locator)
    if match:
        return f"{match.group(1)} {match.group(2).strip()}"
    return ""


def resource_label(locator: str) -> tuple[str, str]:
    provider = provider_name(locator)
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
        match = re.search(r"github\\.com/([^/]+/[^/]+)", rest)
        if match:
            return ("github-repo", match.group(1))
    return ("", "")


def lead_kind(locator: str, provider: str) -> str:
    if re.search(r"(?:^|:)(search|jql|cql)\s+.+$", locator):
        return f"{provider or 'external'}-search"
    if locator.startswith("artifact:"):
        return "artifact"
    if locator.startswith("content:"):
        return "content"
    resource_kind, _ = resource_label(locator)
    if resource_kind:
        return resource_kind
    if provider == "github" and locator.startswith("https://github.com/"):
        path = urllib.parse.urlparse(locator).path
        if re.search(r"/pull/\\d+(?:/|$)", path):
            return "github-pull"
        if re.search(r"/issues/\\d+(?:/|$)", path):
            return "github-issue"
        if re.search(r"/commit/[A-Fa-f0-9]+(?:/|$)", path):
            return "github-commit"
        if re.search(r"/commits(?:/|$)", path):
            return "github-commit-history"
        return "github-repo"
    if provider == "web":
        return "url"
    return f"{provider or 'external'}-reference"


def lead_signal_labels(lead: Mapping[str, object], *, aggregated: bool) -> list[str]:
    labels = [
        "native" if bool(lead.get("firstParty")) else "web",
        "materialized" if bool(lead.get("materialized")) else "unmaterialized",
    ]
    if bool(lead.get("searchSeed")):
        labels.append("search seed")
    if aggregated:
        labels.append(f"artifacts {_int_value(lead.get('artifactCount'))}")
    labels.append(f"mentions {_int_value(lead.get('occurrenceCount'))}")
    search_like_source_count = _int_value(lead.get("searchLikeSourceCount"))
    artifact_count = _int_value(lead.get("artifactCount"))
    if search_like_source_count:
        if artifact_count and search_like_source_count >= artifact_count:
            labels.append("search/listing only")
        else:
            labels.append("partly search/listing driven")
    return labels


def stored_target_locators(lead: Mapping[str, object]) -> list[str]:
    return [
        str(value)
        for value in (
            _list_value(lead.get("artifactLocators"))
            + _list_value(lead.get("contentLocators"))
            + _list_value(lead.get("targetArtifactLocators"))
            + _list_value(lead.get("targetContentLocators"))
        )
        if str(value)
    ]


def render_search_origins(lead: Mapping[str, object]) -> str:
    origins = [
        origin
        for origin in _list_value(lead.get("searchOrigins"))
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


def shared_session_dirs_from_ref(session_ref: str):
    normalized = str(session_ref or "").strip()
    if not normalized:
        return None
    shared_root = resolve_session_reference(normalized, allow_missing=False)
    if (
        shared_root is None
        and "/" not in normalized
        and not Path(normalized).expanduser().is_absolute()
    ):
        candidate = shared_session_root(normalized)
        if candidate.exists() or candidate.is_symlink():
            shared_root = candidate.resolve()
    if shared_root is None:
        return None
    return shared_root, session_shared_id(shared_root)
