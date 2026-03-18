"""Shared locator-routing helpers for provider-owned read delegation."""

from __future__ import annotations

import shlex
import urllib.parse


def split_locator_tail(tail: str) -> list[str]:
    try:
        parts = shlex.split(tail)
    except ValueError:
        parts = [tail]
    return [part for part in parts if part]


def strip_http_url_fragment(target: str) -> str:
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.fragment:
        return target
    return urllib.parse.urlunparse(parsed._replace(fragment=""))


def query_route(
    subcommand: str,
    tail: str,
    *,
    valued_flags: tuple[str, ...] = (),
    boolean_flags: tuple[str, ...] = (),
) -> list[str] | None:
    parts = split_locator_tail(tail)
    if not parts:
        return None
    argv = [subcommand]
    query_parts: list[str] = []
    index = 0
    parse_flags = True
    while index < len(parts):
        token = parts[index]
        if parse_flags and token == "--":
            parse_flags = False
            index += 1
            continue
        if parse_flags and token in boolean_flags:
            argv.append(token)
            index += 1
            continue
        if parse_flags and token in valued_flags:
            if index + 1 >= len(parts):
                return None
            argv.extend([token, parts[index + 1]])
            index += 2
            continue
        if parse_flags:
            matched_flag = next(
                (flag for flag in valued_flags if token.startswith(f"{flag}=")),
                "",
            )
            if matched_flag:
                argv.extend([matched_flag, token.split("=", 1)[1]])
                index += 1
                continue
        query_parts.append(token)
        index += 1
    query = " ".join(part for part in query_parts if part).strip()
    if not query:
        return None
    return [*argv, query]
