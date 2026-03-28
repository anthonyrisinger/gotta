"""Query matching helpers for `gotta session scan`."""

from __future__ import annotations

import re


def compile_pattern(
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


def hit_lines(
    lines: list[str],
    *,
    query: str,
    case_sensitive: bool,
    pattern: re.Pattern[str] | None = None,
) -> list[int]:
    if pattern is not None:
        return [index for index, line in enumerate(lines) if pattern.search(line)]
    needle = query if case_sensitive else query.casefold()
    return [
        index
        for index, line in enumerate(lines)
        if needle in (line if case_sensitive else line.casefold())
    ]
