"""Snippet window helpers for `gotta session scan`."""

from __future__ import annotations

from typing import TypedDict


class SnippetLine(TypedDict):
    number: int
    text: str


class Snippet(TypedDict):
    startLine: int
    endLine: int
    hitLines: list[int]
    lines: list[SnippetLine]


class _Window(TypedDict):
    startLine: int
    endLine: int
    hitLines: list[int]


def build_snippets(
    lines: list[str],
    *,
    hits: list[int],
    context: int,
    limit: int,
) -> list[Snippet]:
    windows: list[_Window] = []
    current: _Window | None = None
    for hit in hits:
        start_line = max(hit - context, 0) + 1
        end_line = min(hit + context, len(lines) - 1) + 1
        if current is not None and start_line <= current["endLine"] + 1:
            current["endLine"] = max(current["endLine"], end_line)
            hit_line = hit + 1
            if hit_line not in current["hitLines"]:
                current["hitLines"].append(hit_line)
            continue
        if current is not None:
            windows.append(current)
            if len(windows) >= limit:
                return _render_snippets(lines, windows)
        current = {
            "startLine": start_line,
            "endLine": end_line,
            "hitLines": [hit + 1],
        }
    if current is not None and len(windows) < limit:
        windows.append(current)
    return _render_snippets(lines, windows)


def _render_snippets(lines: list[str], windows: list[_Window]) -> list[Snippet]:
    rendered: list[Snippet] = []
    for window in windows:
        start = window["startLine"] - 1
        end = window["endLine"]
        rendered.append(
            {
                "startLine": window["startLine"],
                "endLine": window["endLine"],
                "hitLines": list(window["hitLines"]),
                "lines": [
                    {
                        "number": index + 1,
                        "text": lines[index],
                    }
                    for index in range(start, end)
                ],
            }
        )
    return rendered
