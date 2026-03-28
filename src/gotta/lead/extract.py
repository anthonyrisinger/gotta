"""Extract explicit and semantic lead mentions from rendered text."""

from __future__ import annotations

from functools import cache
import re
import urllib.parse

from gotta.content.path import sh_quote

from .canon import canonicalize_url, github_repo_reference, provider_for_locator
from .model import LeadMention
from .query import (
    SEARCH_LEAD_LIMIT,
    heading_search_query,
    line_search_query,
    semantic_search_locator,
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
    r"|slack:doc:[A-Za-z0-9]+:[A-Za-z0-9]+"
    r"|slack:workspace:[A-Za-z0-9._-]+"
    r"|artifact:[A-Za-z0-9._-]+@[a-f0-9]{12}"
    r"|content:[a-f0-9]{64}"
    r")\b"
)
MAX_SNIPPET_CHARS = 240
_TRAILING_PUNCTUATION = ".,;:!?)>]}`'\"`"
SEMANTIC_SEARCH_LEAD_PROVIDERS = {
    "confluence",
    "slack",
}


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
        locator = semantic_search_locator(provider, normalized_query, text=text)
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
                snippet=_clip_snippet(snippet, normalized_query),
                ordinal=len(candidates) + 1,
            )
        )

    for raw_line in text.splitlines():
        query = line_search_query(raw_line)
        if query:
            append_query(query, snippet=" ".join(raw_line.split()))
            if len(candidates) >= SEARCH_LEAD_LIMIT:
                return candidates
    for raw_line in text.splitlines():
        query = heading_search_query(raw_line)
        if query:
            append_query(query, snippet=" ".join(raw_line.split()))
            if len(candidates) >= SEARCH_LEAD_LIMIT:
                break
    return candidates


def filter_explicit_mentions(
    *,
    source_locator: str,
    provider: str,
    mentions: list[LeadMention],
) -> list[LeadMention]:
    filtered: list[LeadMention] = []
    for mention in mentions:
        target_locator = mention.canonical_locator.strip()
        if not target_locator or target_locator == source_locator:
            continue
        if _is_structural_github_repo_navigation(
            source_locator=source_locator,
            provider=provider,
            mention=mention,
        ):
            continue
        filtered.append(mention)
    return filtered


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
                    snippet=_clip_snippet(raw_line, raw),
                    ordinal=ordinal,
                )
            )
    return leads


def _trim_candidate(raw: str) -> str:
    cleaned = raw.strip()
    ellipsized_url = cleaned.startswith(
        ("http://", "https://")
    ) and cleaned.rstrip().endswith("...")
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
        if (
            parsed.netloc
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        ):
            return ""
    return cleaned


def _clip_snippet(raw_line: str, needle: str = "") -> str:
    line = " ".join(raw_line.split()).strip()
    if len(line) <= MAX_SNIPPET_CHARS:
        return line
    candidate = needle.strip()
    if candidate:
        index = line.casefold().find(candidate.casefold())
        if index >= 0:
            half = max((MAX_SNIPPET_CHARS - 6) // 2, 1)
            start = max(index - half, 0)
            end = min(index + len(candidate) + half, len(line))
            excerpt = line[start:end].strip()
            if start > 0:
                excerpt = f"... {excerpt}"
            if end < len(line):
                excerpt = f"{excerpt} ..."
            return excerpt
    return f"{line[: max(MAX_SNIPPET_CHARS - 4, 1)].rstrip()} ..."


def _is_structural_github_repo_navigation(
    *,
    source_locator: str,
    provider: str,
    mention: LeadMention,
) -> bool:
    if provider != "github":
        return False
    source_repo, source_kind = github_repo_reference(source_locator)
    if source_kind not in {"repo", "tree"} or not source_repo:
        return False
    target_repo, target_kind = github_repo_reference(mention.canonical_locator)
    if target_repo != source_repo or target_kind not in {"blob", "tree"}:
        return False
    snippet = mention.snippet.strip()
    return snippet.startswith("- [") or snippet.startswith("- **README:**")


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
        canonical_url = canonicalize_url(target)
        if canonical_url is None:
            return None
        target = canonical_url
    else:
        return None
    canonical = target.strip()
    if not canonical:
        return None
    provider = provider_for_locator(canonical)
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
