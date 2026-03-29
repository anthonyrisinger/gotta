"""Text rendering for `gotta session scan`."""

from __future__ import annotations

from ..core import paging_summary_line, visibility_summary
from .model import ScanEntry, ScanPayload


def render_scan_text(payload: ScanPayload) -> str:
    lines = [
        f"scan: {payload['manifestPath']}",
        f"query: {payload['query']}",
        (
            f"matches: {payload['entryCount']} artifact(s); "
            f"showing {payload['shownCount']}; "
            f"match={payload['matchMode']}; "
            f"case_sensitive={str(payload['caseSensitive']).lower()}"
        ),
        paging_summary_line(
            label="page",
            total_count=payload["totalCount"],
            shown_count=payload["shownCount"],
            offset=payload["offset"],
            next_offset=payload["nextOffset"],
        ),
    ]
    if not payload["entries"]:
        lines.append("no projected materialized artifact matched the current scan")
        return "\n".join(lines)
    for entry in payload["entries"]:
        lines.extend(render_entry(entry))
    return "\n".join(lines)


def render_entry(entry: ScanEntry) -> list[str]:
    lines: list[str] = []
    plugins = ", ".join(entry.get("plugins", []))
    actors = ", ".join(entry.get("actors", []))
    locator = entry.get("canonical_locator") or entry.get("locator") or "unknown"
    preferred_name = entry.get("preferred_name", "") or "data"
    lines.append(
        f"- [{entry.get('artifactKind') or 'artifact'}; hits {entry.get('hitCount', 0)}] "
        f"{locator} -> {preferred_name}"
    )
    if plugins:
        lines.append(f"  plugins: {plugins}")
    if actors:
        lines.append(f"  actors: {actors}")
    visibility = visibility_summary(entry)
    if visibility:
        lines.append(f"  visibility: {visibility}")
    stored_parts = [
        f"`{entry.get('artifactLocator')}`" if entry.get("artifactLocator") else "",
        f"`{entry.get('contentLocator')}`" if entry.get("contentLocator") else "",
    ]
    lines.append("  stored: " + ", ".join(part for part in stored_parts if part))
    lines.append(
        f"  follow: `{entry.get('artifactFollowCommand') or entry.get('followCommand') or ''}`"
    )
    for snippet in entry.get("snippets", []):
        lines.append(
            f"  snippet {snippet['startLine']}-{snippet['endLine']}"
            f" (hits {', '.join(str(number) for number in snippet['hitLines'])})"
        )
        for line_value in snippet["lines"]:
            lines.append(f"    {line_value['number']:>5} | {line_value['text']}")
    return lines
