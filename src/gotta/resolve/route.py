"""Provider-owned locator routing helpers."""

from __future__ import annotations

from typing import Any

from gotta.builtin import PluginSpec, available_plugins, get_plugin
from gotta.content.model import CommonOptions
from gotta.content.path import sanitize_name
from gotta.resolve.model import ReadRequest, ReadTarget
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


def _routed_plugins() -> list[PluginSpec]:
    return sorted(
        [
            spec
            for spec in (get_plugin(name) for name in available_plugins())
            if spec
            and spec.name not in {"read", "session"}
            and spec.route_target is not None
        ],
        key=lambda item: (item.route_priority, item.name),
    )


def discover_plugin_route(target: str) -> tuple[str, list[str]] | None:
    for plugin in _routed_plugins():
        try:
            argv = plugin.route_target(target) if plugin.route_target else None
        except ValueError:
            argv = None
        if argv is not None:
            return plugin.name, argv
    return None


def _resolved_routed_target(
    request: ReadRequest,
    target: str,
    plugin: str,
    plugin_argv: list[str],
    options: CommonOptions | Any,
    *,
    save_as: str,
) -> ReadTarget:
    spec = get_plugin(plugin)
    canonical = (
        spec.canonical_locator(plugin_argv)
        if spec and spec.canonical_locator
        else target
    )
    preferred = save_as or (
        spec.preferred_name(plugin_argv, options)
        if spec and spec.preferred_name
        else f"{sanitize_name(target) or plugin}.txt"
    )
    return ReadTarget(
        request=request,
        kind="routed",
        path=None,
        routed_plugin=plugin,
        routed_argv=plugin_argv,
        canonical_locator=canonical,
        preferred_name=preferred,
        should_materialize=True,
    )


def resolve_routed_target(
    request: ReadRequest,
    target: str,
    options: CommonOptions | Any,
    *,
    save_as: str,
) -> ReadTarget | None:
    if request.routed_plugin and request.routed_argv:
        return _resolved_routed_target(
            request,
            target,
            request.routed_plugin,
            list(request.routed_argv),
            options,
            save_as=save_as,
        )
    routed = discover_plugin_route(target)
    if routed is None:
        return None
    plugin, plugin_argv = routed
    return _resolved_routed_target(
        request,
        target,
        plugin,
        plugin_argv,
        options,
        save_as=save_as,
    )


def partition_routed_target_tokens(
    tokens: list[str],
) -> tuple[str, str, tuple[str, ...]] | None:
    best: tuple[int, int, str, str, tuple[str, ...]] | None = None
    token_count = len(tokens)
    for start in range(token_count):
        for end in range(token_count, start, -1):
            candidate = " ".join(
                part.strip() for part in tokens[start:end] if part.strip()
            ).strip()
            if not candidate:
                continue
            routed = discover_plugin_route(candidate)
            if routed is None:
                continue
            plugin, plugin_argv = routed
            provider_argv = tuple(tokens[:start] + plugin_argv + tokens[end:])
            score = (end - start, -len(tokens[:start] + tokens[end:]))
            if best is None or score > (best[0], best[1]):
                best = (score[0], score[1], plugin, candidate, provider_argv)
    if best is None:
        return None
    return best[2], best[3], best[4]
