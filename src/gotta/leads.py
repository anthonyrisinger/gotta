"""Extract and summarize explicit, followable leads from artifact text."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
import re
import urllib.parse
from typing import Any

from gotta.content import (
    ContentError,
    ContentSnapshot,
    artifact_locator,
    content_locator,
    sh_quote,
    write_text_atomic,
)
from gotta.providers import atlassian as atl
from gotta.source import (
    best_visibility_metadata,
    classify_visibility_metadata,
)

URL_RE = re.compile(r"https?://[^\s<>\"]+")
JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
CANONICAL_LOCATOR_RE = re.compile(
    r"\b(?:"
    r"jira:[A-Z][A-Z0-9]+-\d+"
    r"|confluence:\d+"
    r"|gdocs:[A-Za-z0-9_-]+"
    r"|gdrive:[A-Za-z0-9_-]+"
    r"|gsheets:[A-Za-z0-9_-]+"
    r"|slack:thread:[A-Za-z0-9]+:(?:\d{16}|\d{10}\.\d{6})"
    r"|slack:channel:[A-Za-z0-9]+"
    r"|slack:workspace:[A-Za-z0-9._-]+"
    r"|artifact:[A-Za-z0-9._-]+@[a-f0-9]{12}"
    r"|content:[a-f0-9]{64}"
    r")\b"
)
_TRAILING_PUNCTUATION = ".,;:!?)>]}`'\"`"
LEADS_CACHE_NAME = "leads.json"
LEADS_CACHE_VERSION = 5
SLACK_PERMALINK_RE = re.compile(
    r"https://[^/.]+\.slack\.com/archives/(?P<channel>[A-Z0-9]+)(?:/p(?P<pnum>[0-9]{16}))?"
)
JIRA_BROWSE_RE = re.compile(r"/browse/(?P<issue>[A-Z][A-Z0-9]+-\d+)(?:/|$)")
CONFLUENCE_PAGE_ID_QUERY_RE = re.compile(r"(?:^|&)pageId=(?P<page_id>\d+)(?:&|$)")
CONFLUENCE_PAGE_PATH_RE = re.compile(r"/pages/(?P<page_id>\d+)(?:/|$)")
CONFLUENCE_SPACE_PAGE_PATH_RE = re.compile(r"/spaces/[^/]+/pages/(?P<page_id>\d+)(?:/|$)")
GDOC_URL_RE = re.compile(r"/document/d/(?P<doc_id>[A-Za-z0-9_-]+)(?:/|$)")
GSHEET_URL_RE = re.compile(r"/spreadsheets/d/(?P<sheet_id>[A-Za-z0-9_-]+)(?:/|$)")
GDRIVE_FILE_RE = re.compile(r"/file/d/(?P<file_id>[A-Za-z0-9_-]+)(?:/|$)")
LOW_SIGNAL_WEB_HOSTS = {
    "127.0.0.1",
    "localhost",
    "www.example.com",
    "example.com",
    "img.shields.io",
}
LOW_SIGNAL_WEB_EXTENSIONS = (".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp")
LOW_SIGNAL_MEETING_HOSTS = {
    "meet.google.com",
}
LOW_SIGNAL_HOST_LABELS = {
    "admin",
    "argocd",
    "auth",
    "grafana",
    "jenkins",
    "login",
    "oauth",
    "oidc",
    "sso",
}
LOW_SIGNAL_PATH_MARKERS = (
    "/.well-known/",
    "/auth",
    "/explore",
    "/jwks",
    "/login",
    "/logout",
    "/oauth",
    "/oidc",
    "/openid",
    "/saml",
    "/signin",
    "/sso",
)
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
SEMANTIC_SEARCH_LEAD_PROVIDERS = {
    "confluence",
    "slack",
}
GENERIC_SEARCH_ANCHORS = {
    "HTML",
    "JSONL",
    "LOGS",
    "NOTES",
    "OOPS",
    "README",
    "TLDR",
    "TODO",
    "URL",
}
SEARCH_CONTEXT_WINDOW = 6
SEARCH_LEAD_LIMIT = 3
SEARCH_TERM_MIN_LENGTH = 8
RELATION_PRIORITY = {
    "mentions": 3,
    "links_to": 2,
    "suggests_search": 1,
}


@dataclass(frozen=True, slots=True)
class LeadMention:
    raw: str
    canonical_locator: str
    provider: str
    relation: str
    follow_command: str
    snippet: str
    ordinal: int


def snapshot_display_name(snapshot: ContentSnapshot) -> str:
    preferred = str(snapshot.metadata.get("preferred_name", "")).strip()
    if preferred:
        return preferred
    if snapshot.names:
        return snapshot.names[0]
    return "data"


def snapshot_locator(snapshot: ContentSnapshot) -> str:
    return (
        str(snapshot.metadata.get("canonical_locator", "") or snapshot.metadata.get("locator", "")).strip()
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


def _trim_candidate(raw: str) -> str:
    cleaned = raw.strip()
    ellipsized_url = cleaned.startswith(("http://", "https://")) and cleaned.rstrip().endswith("...")
    if ")](" in cleaned:
        left, right = cleaned.split(")](", 1)
        cleaned = right if right.startswith(("http://", "https://")) else left
    if "](" in cleaned:
        left, right = cleaned.split("](", 1)
        right = right.rstrip(")")
        cleaned = right if right.startswith(("http://", "https://")) else left
    if cleaned.startswith(("http://", "https://")) and "|" in cleaned:
        cleaned = cleaned.split("|", 1)[0].rstrip()
    while cleaned and (cleaned[-1] in _TRAILING_PUNCTUATION or cleaned[-1] == "*"):
        cleaned = cleaned[:-1]
    if cleaned.endswith("/>"):
        cleaned = cleaned[:-2]
    cleaned = cleaned.strip()
    if ellipsized_url and cleaned.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.netloc and not parsed.path and not parsed.query and not parsed.fragment:
            return ""
    return cleaned


def _provider_for_url(target: str) -> str:
    try:
        host = urllib.parse.urlparse(target).netloc.strip().lower()
    except ValueError:
        return "web"
    if "github.com" in host:
        return "github"
    if ".slack.com" in host or "enterprise.slack.com" in host:
        return "slack"
    if ".atlassian.net" in host:
        if "/wiki/" in target:
            return "confluence"
        return "jira"
    if "docs.google.com" in host:
        path = urllib.parse.urlparse(target).path
        if "/spreadsheets/" in path:
            return "gsheets"
        return "gdocs"
    if "drive.google.com" in host:
        return "gdrive"
    return "web"


def lead_cache_path(content_dir: Path) -> Path:
    return content_dir / LEADS_CACHE_NAME


def _provider_for_locator(target: str) -> str:
    if target.startswith(("http://", "https://")):
        return _provider_for_url(target)
    prefix = target.split(":", 1)[0].strip()
    if prefix in {
        "jira",
        "confluence",
        "gdocs",
        "gdrive",
        "gsheets",
        "slack",
        "artifact",
        "content",
    }:
        return prefix
    return "external"


def _is_search_like_locator(locator: str) -> bool:
    text = locator.strip().casefold()
    if not text:
        return False
    if text.startswith(
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
    ):
        return True
    return False


def _snapshot_is_search_like(snapshot: ContentSnapshot) -> bool:
    metadata = snapshot.metadata
    for key in ("canonical_locator", "locator"):
        if _is_search_like_locator(str(metadata.get(key) or "")):
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


def _canonicalize_url(target: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(target)
    except ValueError:
        return target
    host = parsed.netloc.strip().lower()
    path = parsed.path
    query = parsed.query
    if _is_low_signal_web_url(target, host=host, path=path):
        return None
    if ".slack.com" in host:
        query_thread_ts = ""
        for value in urllib.parse.parse_qs(query).get("thread_ts") or []:
            candidate = str(value or "").strip()
            if re.fullmatch(r"\d{10}\.\d{6}", candidate):
                query_thread_ts = candidate.replace(".", "")
                break
        match = SLACK_PERMALINK_RE.match(target)
        if match:
            channel = match.group("channel")
            pnum = match.group("pnum")
            if query_thread_ts:
                return f"slack:thread:{channel}:{query_thread_ts}"
            if pnum:
                return f"slack:thread:{channel}:{pnum}"
            return f"slack:channel:{channel}"
    if ".atlassian.net" in host:
        if path.startswith("/wiki") and not re.match(r"^/wiki(?:/|$)", path):
            return None
        issue_match = JIRA_BROWSE_RE.search(path)
        if issue_match:
            return f"jira:{issue_match.group('issue')}"
        if "/browse/" in path:
            return None
        page_id = atl.extract_confluence_page_id(target)
        if page_id:
            return f"confluence:{page_id}"
    if "docs.google.com" in host:
        doc_match = GDOC_URL_RE.search(path)
        if doc_match:
            return f"gdocs:{doc_match.group('doc_id')}"
        sheet_match = GSHEET_URL_RE.search(path)
        if sheet_match:
            return f"gsheets:{sheet_match.group('sheet_id')}"
    if "drive.google.com" in host:
        drive_match = GDRIVE_FILE_RE.search(path)
        if drive_match:
            return f"gdrive:{drive_match.group('file_id')}"
    return target


def _is_low_signal_web_url(target: str, *, host: str, path: str) -> bool:
    host_only = host.split(":", 1)[0]
    lowered = target.strip().lower()
    if host_only in LOW_SIGNAL_WEB_HOSTS:
        return True
    if host_only.endswith(".example.com"):
        return True
    if "[server_addr]" in lowered or "x.x.x.x" in lowered:
        return True
    if path.lower().endswith(LOW_SIGNAL_WEB_EXTENSIONS):
        return True
    return False


def _semantic_search_locator(provider: str, query: str, *, text: str) -> str:
    if provider == "slack":
        workspace_match = re.search(
            r"https://(?P<workspace>[A-Za-z0-9._-]+)\.slack\.com/archives/",
            text,
        )
        workspace = (
            workspace_match.group("workspace").strip() if workspace_match is not None else ""
        )
        if workspace:
            return f"slack:search --workspace {workspace} {query}"
        return f"slack:search {query}"
    return f"{provider}:search {query}"


def _search_tokens(raw_line: str) -> list[str]:
    tokens: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9+/-]*", raw_line):
        tokens.extend(part for part in token.replace("/", " ").split() if len(part) > 1)
    return tokens


def _is_metadata_line(raw_line: str) -> bool:
    line = raw_line.strip()
    if not line or line == "---":
        return True
    if line.startswith(
        (
            "- URL:",
            "- Page ID:",
            "- Space ID:",
            "- Created:",
            "- Updated:",
            "- Version:",
            "- Projection:",
        )
    ):
        return True
    return bool(re.match(r"^-\s+_(?:Channel|Source|Retrieval|Fidelity)_:", line))


def _search_anchor_token(token: str) -> str:
    if token.isupper():
        if len(token) >= 2 and token not in GENERIC_SEARCH_ANCHORS:
            return token
        return ""
    if token[:1].isupper() and len(token) >= 4:
        return token
    return ""


def _search_query_token(token: str) -> str:
    anchor = _search_anchor_token(token)
    if anchor:
        return anchor
    lowered = token.casefold()
    if len(lowered) >= SEARCH_TERM_MIN_LENGTH:
        return lowered
    return ""


def _heading_search_query(raw_line: str) -> str:
    match = re.match(r"^#{1,6}\s+(.+)$", raw_line.strip())
    if match is None:
        return ""
    title = re.sub(r"^[A-Za-z ]+:\s*", "", match.group(1)).strip()
    words: list[str] = []
    seen: set[str] = set()
    for token in _search_tokens(title):
        query_token = _search_query_token(token)
        if not query_token:
            continue
        normalized = query_token.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        words.append(query_token)
        if len(words) >= 3:
            break
    return " ".join(words) if len(words) >= 2 else ""


def _line_search_query(raw_line: str) -> str:
    if _is_metadata_line(raw_line):
        return ""
    if raw_line.lstrip().startswith("#"):
        return ""
    if raw_line.count(",") >= 3:
        return ""
    tokens = _search_tokens(raw_line)
    anchor_index = -1
    anchor = ""
    for index, token in enumerate(tokens):
        if token.isupper() and len(token) >= 2 and token not in GENERIC_SEARCH_ANCHORS:
            anchor_index = index
            anchor = token
            break
    if anchor_index < 0 or not anchor:
        for index, token in enumerate(tokens):
            query_token = _search_anchor_token(token)
            if not query_token:
                continue
            anchor_index = index
            anchor = query_token
            break
    if anchor_index < 0 or not anchor:
        return ""
    context: list[str] = []
    seen_tokens = {anchor.casefold()}
    for token in tokens[anchor_index + 1 : anchor_index + 1 + SEARCH_CONTEXT_WINDOW]:
        query_token = _search_query_token(token)
        if not query_token:
            continue
        normalized = query_token.casefold()
        if normalized in seen_tokens:
            continue
        seen_tokens.add(normalized)
        context.append(query_token)
        if len(context) >= 2:
            break
    if not context:
        return ""
    query_words = [anchor, *context]
    return " ".join(query_words) if len(query_words) >= 2 else ""


def _is_first_party_target(*, provider: str, kind: str) -> bool:
    return provider in FIRST_PARTY_LEAD_PROVIDERS or kind != "url"


def _relation_priority(value: str) -> int:
    return RELATION_PRIORITY.get(value, 0)


def _relation_set_priority(values: set[str] | list[str]) -> int:
    return max((_relation_priority(str(value)) for value in values), default=0)


def _search_seed_signal_sort_key(query: str) -> tuple[object, ...]:
    tokens = [token for token in query.split() if token]
    uppercase_count = sum(1 for token in tokens if token.isupper())
    titlecase_count = sum(1 for token in tokens if token[:1].isupper() and not token.isupper())
    long_lowercase_count = sum(
        1
        for token in tokens
        if not token[:1].isupper() and len(token.casefold()) >= SEARCH_TERM_MIN_LENGTH
    )
    return (
        -uppercase_count,
        -(titlecase_count + long_lowercase_count),
        -len(tokens),
        query.casefold(),
    )


def _low_signal_url_penalty(locator: str) -> int:
    try:
        parsed = urllib.parse.urlparse(locator)
    except ValueError:
        return 0
    host = parsed.netloc.strip().lower()
    if not host:
        return 0
    host_only = host.split(":", 1)[0]
    path = parsed.path.strip().lower()
    labels = {part for part in re.split(r"[.-]", host_only) if part}
    if host_only in LOW_SIGNAL_MEETING_HOSTS or host_only.endswith(".zoom.us"):
        return 3
    if any(marker in path for marker in LOW_SIGNAL_PATH_MARKERS):
        return 3
    if labels & LOW_SIGNAL_HOST_LABELS and path in {"", "/"}:
        return 2
    if path in {"", "/"}:
        return 1
    return 0


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
    return _low_signal_url_penalty(locator)


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
        source_rank if bool(item.get("sourceSearchLike")) and source_rank > 0 else 1_000_000,
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


def _serialize_mentions(mentions: list[LeadMention]) -> dict[str, Any]:
    return {
        "version": LEADS_CACHE_VERSION,
        "leadCount": len(mentions),
        "entries": [
            {
                "raw": mention.raw,
                "canonical_locator": mention.canonical_locator,
                "provider": mention.provider,
                "relation": mention.relation,
                "follow_command": mention.follow_command,
                "snippet": mention.snippet,
                "ordinal": mention.ordinal,
            }
            for mention in mentions
        ],
    }


def _deserialize_mentions(payload: dict[str, Any]) -> list[LeadMention] | None:
    if int(payload.get("version") or 0) != LEADS_CACHE_VERSION:
        return None
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return None
    mentions: list[LeadMention] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mentions.append(
            LeadMention(
                raw=str(entry.get("raw") or ""),
                canonical_locator=str(entry.get("canonical_locator") or ""),
                provider=str(entry.get("provider") or ""),
                relation=str(entry.get("relation") or ""),
                follow_command=str(entry.get("follow_command") or ""),
                snippet=str(entry.get("snippet") or ""),
                ordinal=int(entry.get("ordinal") or 0),
            )
        )
    return mentions


def _load_lead_cache_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_lead_cache(content_dir: Path, mentions: list[LeadMention]) -> Path:
    return write_text_atomic(
        lead_cache_path(content_dir),
        json.dumps(_serialize_mentions(mentions), indent=2, sort_keys=True) + "\n",
    )


def _read_lead_cache(snapshot: ContentSnapshot) -> list[LeadMention] | None:
    path = lead_cache_path(snapshot.content_dir)
    payload = _load_lead_cache_payload(path)
    if payload is None:
        return None
    return _deserialize_mentions(payload)


def _provider_for_content_dir(content_dir: Path) -> str:
    meta_path = content_dir / "meta.json"
    if not meta_path.exists():
        return ""
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    locator = str(payload.get("canonical_locator", "") or payload.get("locator", "")).strip()
    provider = _provider_for_locator(locator)
    if provider != "external":
        return provider
    return str(payload.get("plugin") or "").strip()


def extract_semantic_search_leads(
    text: str,
    *,
    provider: str,
) -> list[LeadMention]:
    if provider not in SEMANTIC_SEARCH_LEAD_PROVIDERS:
        return []
    candidates: list[LeadMention] = []
    seen_locators: set[str] = set()

    def append_query(query: str, *, snippet: str) -> None:
        normalized_query = " ".join(query.split()).strip()
        if not normalized_query:
            return
        locator = _semantic_search_locator(provider, normalized_query, text=text)
        if locator in seen_locators:
            return
        seen_locators.add(locator)
        candidates.append(
            LeadMention(
                raw=normalized_query,
                canonical_locator=locator,
                provider=provider,
                relation="suggests_search",
                follow_command=f"gotta read {sh_quote(locator)}",
                snippet=snippet.strip(),
                ordinal=len(candidates) + 1,
            )
        )

    for raw_line in text.splitlines():
        query = _line_search_query(raw_line)
        if query:
            append_query(query, snippet=" ".join(raw_line.split()))
            if len(candidates) >= SEARCH_LEAD_LIMIT:
                return candidates
    for raw_line in text.splitlines():
        query = _heading_search_query(raw_line)
        if query:
            append_query(query, snippet=" ".join(raw_line.split()))
            if len(candidates) >= SEARCH_LEAD_LIMIT:
                break
    return candidates


def _snapshot_provider(snapshot: ContentSnapshot) -> str:
    locator = snapshot_locator(snapshot)
    provider = _provider_for_locator(locator)
    if provider != "external":
        return provider
    return str(snapshot.metadata.get("plugin") or "").strip()


def _snapshot_subcommand(snapshot: ContentSnapshot) -> str:
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


def _effective_explicit_mentions(
    snapshot: ContentSnapshot,
    mentions: list[LeadMention],
) -> list[LeadMention]:
    source_locator = snapshot_locator(snapshot).strip()
    return [
        mention
        for mention in mentions
        if mention.canonical_locator.strip()
        and mention.canonical_locator.strip() != source_locator
    ]


def maybe_write_lead_cache(content_dir: Path, *, data: bytes) -> Path | None:
    path = lead_cache_path(content_dir)
    payload = _load_lead_cache_payload(path)
    if payload is not None and int(payload.get("version") or 0) == LEADS_CACHE_VERSION:
        return path
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="ignore")
    mentions = extract_explicit_leads(text)
    provider = _provider_for_content_dir(content_dir)
    source_locator = ""
    meta_path = content_dir / "meta.json"
    if meta_path.exists():
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            source_locator = str(
                payload.get("canonical_locator", "") or payload.get("locator", "")
            ).strip()
    if provider and not any(
        mention.canonical_locator.strip()
        and mention.canonical_locator.strip() != source_locator
        for mention in mentions
    ):
        mentions.extend(extract_semantic_search_leads(text, provider=provider))
    return _write_lead_cache(content_dir, mentions)


def lead_mentions_for_snapshot(snapshot: ContentSnapshot) -> list[LeadMention]:
    cached = _read_lead_cache(snapshot)
    if cached is not None:
        return cached
    try:
        text = snapshot.data_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    mentions = extract_explicit_leads(text)
    if not _effective_explicit_mentions(snapshot, mentions):
        mentions.extend(
            extract_semantic_search_leads(
                text,
                provider=_snapshot_provider(snapshot),
            )
        )
    _write_lead_cache(snapshot.content_dir, mentions)
    return mentions


@cache
def _normalize_candidate(raw: str, relation: str) -> LeadMention | None:
    target = raw.strip()
    if relation == "mentions" and JIRA_KEY_RE.fullmatch(raw):
        target = f"jira:{raw}"
    elif CANONICAL_LOCATOR_RE.fullmatch(target):
        if target.startswith("slack:thread:") and "." in target.rsplit(":", 1)[-1]:
            prefix, thread_ref = target.rsplit(":", 1)
            target = f"{prefix}:{thread_ref.replace('.', '')}"
    elif target.startswith(("http://", "https://")):
        canonical_url = _canonicalize_url(target)
        if canonical_url is None:
            return None
        target = canonical_url
    else:
        return None
    canonical = target.strip()
    if not canonical:
        return None
    provider = _provider_for_locator(canonical)
    follow = f"gotta read {sh_quote(canonical)}"
    return LeadMention(
        raw=raw,
        canonical_locator=canonical,
        provider=provider,
        relation=relation,
        follow_command=follow,
        snippet="",
        ordinal=0,
    )


def extract_explicit_leads(text: str) -> list[LeadMention]:
    leads: list[LeadMention] = []
    ordinal = 0
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        seen_raw: set[tuple[str, str]] = set()
        local_seen: set[tuple[str, str]] = set()
        candidates: list[tuple[str, str]] = []
        for match in URL_RE.finditer(raw_line):
            cleaned = _trim_candidate(match.group(0))
            if cleaned:
                candidates.append((cleaned, "links_to"))
        for match in CANONICAL_LOCATOR_RE.finditer(raw_line):
            cleaned = _trim_candidate(match.group(0))
            if cleaned:
                candidates.append((cleaned, "links_to"))
        for match in JIRA_KEY_RE.finditer(raw_line):
            cleaned = _trim_candidate(match.group(0))
            if cleaned:
                candidates.append((cleaned, "mentions"))
        for raw, relation in candidates:
            raw_key = (raw, relation)
            if raw_key in seen_raw:
                continue
            seen_raw.add(raw_key)
            normalized = _normalize_candidate(raw, relation)
            if normalized is None:
                continue
            key = (normalized.canonical_locator, relation)
            if key in local_seen:
                continue
            local_seen.add(key)
            ordinal += 1
            leads.append(
                LeadMention(
                    raw=raw,
                    canonical_locator=normalized.canonical_locator,
                    provider=normalized.provider,
                    relation=relation,
                    follow_command=normalized.follow_command,
                    snippet=line,
                    ordinal=ordinal,
                )
            )
    return leads


def materialized_source_index(
    manifest_entries: list[dict[str, object]],
    snapshot_by_digest: dict[str, ContentSnapshot],
) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for entry in manifest_entries:
        locator = str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip()
        checksum = str(entry.get("checksum", "")).strip()
        if not locator or not checksum or checksum not in snapshot_by_digest:
            continue
        index.setdefault(locator, set()).add(checksum)
    return index


def materialized_target_locators(
    locator: str,
    source_index: dict[str, set[str]],
    snapshot_by_digest: dict[str, ContentSnapshot],
) -> tuple[list[str], list[str]]:
    digests = sorted(source_index.get(locator, set()))
    artifact_locators = [
        snapshot_artifact_locator(snapshot_by_digest[digest])
        for digest in digests
        if digest in snapshot_by_digest
    ]
    content_locators = [content_locator(digest) for digest in digests]
    return artifact_locators, content_locators


def materialized_target_visibility(
    locator: str,
    source_index: dict[str, set[str]],
    snapshot_by_digest: dict[str, ContentSnapshot],
) -> dict[str, object]:
    return best_visibility_metadata(
        *[
            snapshot_by_digest[digest].metadata
            for digest in sorted(source_index.get(locator, set()))
            if digest in snapshot_by_digest
        ]
    )


def build_lead_edge_records(
    snapshots: list[ContentSnapshot],
    manifest_entries: list[dict[str, object]],
    *,
    classify_kind,
) -> list[dict[str, object]]:
    snapshot_by_digest = {snapshot.digest: snapshot for snapshot in snapshots}
    source_index = materialized_source_index(manifest_entries, snapshot_by_digest)
    rendered: list[dict[str, object]] = []
    for snapshot in sorted(snapshots, key=snapshot_sort_key, reverse=True):
        source_locator = snapshot_locator(snapshot)
        source_search_like = _snapshot_is_search_like(snapshot)
        source_provider = _snapshot_provider(snapshot)
        source_subcommand = _snapshot_subcommand(snapshot)
        edge_state: dict[str, dict[str, object]] = {}
        for mention in lead_mentions_for_snapshot(snapshot):
            target_locator = mention.canonical_locator.strip()
            if not target_locator or target_locator == source_locator:
                continue
            state = edge_state.setdefault(
                target_locator,
                {
                    "sourceChecksum": snapshot.digest,
                    "sourceLocator": source_locator or content_locator(snapshot.digest),
                    "sourceArtifactLocator": snapshot_artifact_locator(snapshot),
                    "sourceContentLocator": content_locator(snapshot.digest),
                    "sourcePreferredName": snapshot_display_name(snapshot),
                    "targetLocator": target_locator,
                    "provider": mention.provider,
                    "kind": classify_kind(target_locator, mention.provider),
                    "relation": mention.relation,
                    "followCommand": mention.follow_command,
                    "rawExamples": [],
                    "contexts": [],
                    "occurrenceCount": 0,
                    "sourceSearchLike": source_search_like,
                    "sourceProvider": source_provider,
                    "sourceSubcommand": source_subcommand,
                    "sourceRank": mention.ordinal if source_search_like else 0,
                },
            )
            state["occurrenceCount"] = int(state["occurrenceCount"]) + 1
            mention_ordinal = int(mention.ordinal or 0) if source_search_like else 0
            if mention_ordinal and (
                not int(state.get("sourceRank") or 0)
                or mention_ordinal < int(state.get("sourceRank") or 0)
            ):
                state["sourceRank"] = mention_ordinal
            if mention.raw not in state["rawExamples"] and len(state["rawExamples"]) < 3:
                state["rawExamples"].append(mention.raw)
            if mention.snippet and mention.snippet not in state["contexts"] and len(state["contexts"]) < 3:
                state["contexts"].append(mention.snippet)
        for state in edge_state.values():
            artifact_locators, content_locators = materialized_target_locators(
                str(state["targetLocator"]),
                source_index,
                snapshot_by_digest,
            )
            target_visibility = best_visibility_metadata(
                classify_visibility_metadata(
                    {},
                    provider=str(state["provider"]),
                    locator=str(state["targetLocator"]),
                ),
                materialized_target_visibility(
                    str(state["targetLocator"]),
                    source_index,
                    snapshot_by_digest,
                ),
            )
            state["materialized"] = bool(artifact_locators or content_locators)
            state["targetArtifactLocators"] = artifact_locators
            state["targetContentLocators"] = content_locators
            state["firstParty"] = _is_first_party_target(
                provider=str(state["provider"]),
                kind=str(state["kind"]),
            )
            state["searchSeed"] = str(state["kind"]).endswith("-search")
            state.update(target_visibility)
            rendered.append(state)
    return sorted(
        rendered,
        key=lambda item: (
            str(item["sourcePreferredName"]).casefold(),
            *edge_best_first_sort_key(item),
        ),
    )


def aggregate_lead_sources(edge_records: list[dict[str, object]]) -> list[dict[str, object]]:
    aggregated: dict[str, dict[str, object]] = {}
    for edge in edge_records:
        locator = str(edge["targetLocator"])
        state = aggregated.setdefault(
            locator,
            {
                "locator": locator,
                "provider": str(edge["provider"]),
                "kind": str(edge["kind"]),
                "followCommand": str(edge["followCommand"]),
                "firstParty": bool(edge.get("firstParty")),
                "searchSeed": bool(edge.get("searchSeed")),
                "materialized": bool(edge["materialized"]),
                "occurrenceCount": 0,
                "relationKinds": set(),
                "artifactLocators": set(),
                "contentLocators": set(),
                "sourceChecksums": set(),
                "searchLikeChecksums": set(),
                "searchOriginKeys": set(),
                "searchOrigins": [],
                "exampleRaw": "",
                "contexts": [],
                "visibility": {},
            },
        )
        state["firstParty"] = bool(state["firstParty"] or edge.get("firstParty"))
        state["searchSeed"] = bool(state["searchSeed"] or edge.get("searchSeed"))
        state["materialized"] = bool(state["materialized"] or edge["materialized"])
        state["occurrenceCount"] = int(state["occurrenceCount"]) + int(edge["occurrenceCount"])
        state["relationKinds"].add(str(edge["relation"]))
        state["sourceChecksums"].add(str(edge["sourceChecksum"]))
        if bool(edge.get("sourceSearchLike")):
            state["searchLikeChecksums"].add(str(edge["sourceChecksum"]))
            search_rank = int(edge.get("sourceRank") or 0)
            if search_rank > 0:
                search_origin = {
                    "provider": str(edge.get("sourceProvider") or ""),
                    "subcommand": str(edge.get("sourceSubcommand") or ""),
                    "rank": search_rank,
                    "artifactLocator": str(edge.get("sourceArtifactLocator") or ""),
                    "sourceLocator": str(edge.get("sourceLocator") or ""),
                }
                origin_key = (
                    search_origin["provider"],
                    search_origin["subcommand"],
                    search_origin["rank"],
                    search_origin["artifactLocator"],
                    search_origin["sourceLocator"],
                )
                if origin_key not in state["searchOriginKeys"]:
                    state["searchOriginKeys"].add(origin_key)
                    state["searchOrigins"].append(search_origin)
        if not state["exampleRaw"]:
            raws = [str(value) for value in edge.get("rawExamples") or [] if str(value)]
            if raws:
                state["exampleRaw"] = raws[0]
        for artifact_locator_value in edge.get("targetArtifactLocators") or []:
            state["artifactLocators"].add(str(artifact_locator_value))
        for content_locator_value in edge.get("targetContentLocators") or []:
            state["contentLocators"].add(str(content_locator_value))
        for snippet in edge.get("contexts") or []:
            value = str(snippet).strip()
            if value and value not in state["contexts"] and len(state["contexts"]) < 3:
                state["contexts"].append(value)
        state["visibility"] = best_visibility_metadata(
            state.get("visibility", {}),
            edge,
        )
    rendered: list[dict[str, object]] = []
    for locator, state in aggregated.items():
        relation_kinds = {str(value) for value in state["relationKinds"]}
        artifact_count = len(state["sourceChecksums"])
        search_like_source_count = len(state["searchLikeChecksums"])
        search_origins = sorted(
            [
                origin
                for origin in state["searchOrigins"]
                if isinstance(origin, dict)
            ],
            key=lambda item: (
                int(item.get("rank") or 0) if int(item.get("rank") or 0) > 0 else 1_000_000,
                str(item.get("provider") or "").casefold(),
                str(item.get("subcommand") or "").casefold(),
                str(item.get("artifactLocator") or "").casefold(),
            ),
        )
        rendered.append(
            {
                "locator": locator,
                "provider": str(state["provider"]),
                "kind": str(state["kind"]),
                "followCommand": str(state["followCommand"]),
                "firstParty": bool(state["firstParty"]),
                "searchSeed": bool(state["searchSeed"]),
                "materialized": bool(state["materialized"]),
                "occurrenceCount": int(state["occurrenceCount"]),
                "artifactCount": artifact_count,
                "relationKinds": sorted(relation_kinds),
                "artifactLocators": sorted(str(value) for value in state["artifactLocators"]),
                "contentLocators": sorted(str(value) for value in state["contentLocators"]),
                "exampleRaw": str(state["exampleRaw"]),
                "contexts": list(state["contexts"]),
                "bestSearchRank": int(search_origins[0].get("rank") or 0) if search_origins else 0,
                "searchLikeSourceCount": search_like_source_count,
                "searchOrigins": search_origins[:5],
                **best_visibility_metadata(state.get("visibility", {})),
            }
        )
    return sorted(rendered, key=lead_source_best_first_sort_key)


def resolve_lead_snapshots(
    target: str,
    snapshots: list[ContentSnapshot],
    manifest_entries: list[dict[str, object]],
) -> list[ContentSnapshot]:
    ordered = sorted(snapshots, key=snapshot_sort_key, reverse=True)
    requested = target.strip()
    if not requested:
        return ordered
    by_digest = {snapshot.digest: snapshot for snapshot in ordered}
    if requested.startswith("content:"):
        requested = requested.removeprefix("content:").strip()
    if requested and all(char in "0123456789abcdef" for char in requested.casefold()):
        matches = [snapshot for snapshot in ordered if snapshot.digest.startswith(requested)]
        if not matches:
            raise ContentError(
                f"no stored artifact matched checksum or content locator `{target}`"
            )
        if len(matches) > 1:
            suggestions = ", ".join(content_locator(snapshot.digest) for snapshot in matches[:5])
            raise ContentError(
                f"ambiguous content locator `{target}`; disambiguate with one of: {suggestions}"
            )
        return matches
    artifact_matches = [
        snapshot for snapshot in ordered if snapshot_artifact_locator(snapshot) == requested
    ]
    if artifact_matches:
        return artifact_matches
    name_matches = [
        snapshot
        for snapshot in ordered
        if requested == snapshot_display_name(snapshot) or requested in snapshot.names
    ]
    if len(name_matches) == 1:
        return name_matches
    if len(name_matches) > 1:
        suggestions = ", ".join(snapshot_artifact_locator(snapshot) for snapshot in name_matches[:5])
        raise ContentError(
            f"ambiguous stored artifact name `{target}`; disambiguate with one of: {suggestions}"
        )
    locator_matches: set[str] = set()
    for entry in manifest_entries:
        locator = str(entry.get("canonical_locator", "") or entry.get("locator", "")).strip()
        if locator == requested:
            checksum = str(entry.get("checksum", "")).strip()
            if checksum and checksum in by_digest:
                locator_matches.add(checksum)
    if locator_matches:
        return [by_digest[digest] for digest in sorted(locator_matches, reverse=True)]
    raise ContentError(
        "unsupported session leads target. Use an emitted artifact locator, "
        "content locator, stored artifact name, digest prefix, or a materialized source locator."
    )
