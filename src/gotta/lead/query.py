"""Derive search-like lead queries from rendered text."""

from __future__ import annotations

import re

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


def semantic_search_locator(provider: str, query: str, *, text: str) -> str:
    if provider == "slack":
        workspace_match = re.search(
            r"https://(?P<workspace>[A-Za-z0-9._-]+)\.slack\.com/archives/",
            text,
        )
        workspace = (
            workspace_match.group("workspace").strip()
            if workspace_match is not None
            else ""
        )
        if workspace:
            return f"slack:search --workspace {workspace} {query}"
        return f"slack:search {query}"
    return f"{provider}:search {query}"


def heading_search_query(raw_line: str) -> str:
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


def line_search_query(raw_line: str) -> str:
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
