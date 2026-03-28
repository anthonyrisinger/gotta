"""Text rendering for `gotta session scan`."""

from __future__ import annotations

from collections.abc import Mapping

from ..core import paging_summary_line, visibility_summary


def render_scan_text(payload: Mapping[str, object]) -> str:
    lines = [
        f"scan: {string(payload.get('manifestPath'))}",
        f"query: {string(payload.get('query'))}",
        (
            f"matches: {integer(payload.get('entryCount'))} artifact(s); "
            f"showing {integer(payload.get('shownCount'))}; "
            f"match={string(payload.get('matchMode'))}; "
            f"case_sensitive={str(bool(payload.get('caseSensitive'))).lower()}"
        ),
        paging_summary_line(
            label="page",
            total_count=integer(payload.get("totalCount")),
            shown_count=integer(payload.get("shownCount")),
            offset=integer(payload.get("offset")),
            next_offset=(
                integer(payload.get("nextOffset"))
                if payload.get("nextOffset") is not None
                else None
            ),
        ),
    ]
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        lines.append("no projected materialized artifact matched the current scan")
        return "\n".join(lines)
    for value in entries:
        if not isinstance(value, Mapping):
            continue
        entry = value
        lines.extend(render_entry(entry))
    return "\n".join(lines)


def render_entry(entry: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    plugins = ", ".join(string_items(entry.get("plugins")))
    actors = ", ".join(string_items(entry.get("actors")))
    locator = (
        string(entry.get("canonical_locator") or entry.get("locator")) or "unknown"
    )
    preferred_name = string(entry.get("preferred_name")) or "data"
    lines.append(
        f"- [{string(entry.get('artifactKind')) or 'artifact'}; hits {integer(entry.get('hitCount'))}] "
        f"{locator} -> {preferred_name}"
    )
    if plugins:
        lines.append(f"  plugins: {plugins}")
    if actors:
        lines.append(f"  actors: {actors}")
    visibility = visibility_summary(dict(entry))
    if visibility:
        lines.append(f"  visibility: {visibility}")
    stored_parts = [
        f"`{string(entry.get('artifactLocator'))}`"
        if string(entry.get("artifactLocator"))
        else "",
        f"`{string(entry.get('contentLocator'))}`"
        if string(entry.get("contentLocator"))
        else "",
    ]
    lines.append("  stored: " + ", ".join(part for part in stored_parts if part))
    lines.append(
        f"  follow: `{string(entry.get('artifactFollowCommand') or entry.get('followCommand'))}`"
    )
    snippets = entry.get("snippets")
    if not isinstance(snippets, list):
        return lines
    for value in snippets:
        if not isinstance(value, Mapping):
            continue
        snippet = value
        lines.append(
            f"  snippet {integer(snippet.get('startLine'))}-{integer(snippet.get('endLine'))}"
            f" (hits {', '.join(str(number) for number in int_items(snippet.get('hitLines')))})"
        )
        snippet_lines = snippet.get("lines")
        if not isinstance(snippet_lines, list):
            continue
        for line_value in snippet_lines:
            if not isinstance(line_value, Mapping):
                continue
            lines.append(
                f"    {integer(line_value.get('number')):>5} | {string(line_value.get('text'))}"
            )
    return lines


def string(value: object) -> str:
    return str(value or "").strip()


def string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := string(item))]


def int_items(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [integer(item) for item in value]


def integer(value: object) -> int:
    try:
        return int(str(value or 0))
    except ValueError:
        return 0
