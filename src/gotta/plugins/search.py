#!/usr/bin/env python3
"""Top-level remote discovery routing for gotta."""

from __future__ import annotations

import os
import signal
import sys

from gotta.dispatch import (
    SUPPRESS_RECEIPTS_ENV,
    load_plugin_runner,
    system_exit_status,
)
from gotta.helptext import is_long_help_request
from gotta.searching import SearchRouteError, resolve_search_route


def build_usage() -> str:
    return """usage: gotta search '<provider>:<query>'

Route plain-text remote discovery through the provider-native search surface.

Examples:
  gotta search jira:Architecture
  gotta search 'slack:ABC reboot'
  gotta search 'github:SomeFunction ownership'
  gotta search 'github:search SomeFunction ownership'
  gotta search 'jira:retry --literal-token'

Notes:
  Top-level `gotta search` takes exactly one provider-qualified plain-text query string.
  Quote the full `<provider>:<query>` argument when the query contains spaces or flag-shaped text.
  Explicit aliases like `github:search foo` are tolerated, but `github:foo` is canonical.
  Use provider-exact surfaces for structured flags:
    gotta slack search ...
    gotta github search ...
  Raw provider query languages stay provider-native:
    gotta jira jql ...
    gotta confluence cql ...
    gotta granola search-transcript ...
"""


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def canonical_locator(argv: list[str]) -> str:
    if not argv or any(token in {"-h", "--help", "--help-all"} for token in argv):
        return "search:help"
    route = resolve_search_route(argv)
    query = " ".join(route.provider_argv[1:]).strip()
    return f"{route.provider}:search {query}".strip()


def preferred_name(argv: list[str], options: object) -> str:
    if not argv or any(token in {"-h", "--help", "--help-all"} for token in argv):
        return "search.txt"
    route = resolve_search_route(argv)
    from gotta.builtin import get_plugin

    spec = get_plugin(route.provider)
    if spec and spec.preferred_name is not None:
        return spec.preferred_name(route.provider_argv, options)
    return f"{route.provider}-search.md"


def content_type(argv: list[str], name: str) -> str:
    if not argv or any(token in {"-h", "--help", "--help-all"} for token in argv):
        return "text/plain"
    route = resolve_search_route(argv)
    from gotta.builtin import get_plugin

    spec = get_plugin(route.provider)
    if spec and spec.content_type is not None:
        return spec.content_type(route.provider_argv, name)
    return "text/markdown"


def capture(argv: list[str], options: object):
    route = resolve_search_route(argv)
    from gotta.builtin import get_plugin
    from gotta.capture import Capture
    from gotta.dispatch import capture_stdout

    spec = get_plugin(route.provider)
    if spec and spec.capture is not None:
        return spec.capture(route.provider_argv, options)
    previous = os.environ.get(SUPPRESS_RECEIPTS_ENV)
    os.environ[SUPPRESS_RECEIPTS_ENV] = "1"
    try:
        with capture_stdout() as captured:
            code = load_plugin_runner(route.provider)(route.provider_argv)
    finally:
        if previous is None:
            os.environ.pop(SUPPRESS_RECEIPTS_ENV, None)
        else:
            os.environ[SUPPRESS_RECEIPTS_ENV] = previous
    if code != 0:
        detail = captured.getvalue().decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"`{route.provider}` search capture failed")
    return Capture(data=captured.getvalue())


def project(argv: list[str], capture):
    route = resolve_search_route(argv)
    from gotta.builtin import get_plugin

    spec = get_plugin(route.provider)
    if spec and spec.project is not None:
        return spec.project(route.provider_argv, capture)
    return capture.data


def main(argv: list[str]) -> int:
    if is_long_help_request(argv):
        print(build_usage(), end="")
        return 0
    if any(token in {"-h", "--help"} for token in argv):
        print(build_usage(), end="")
        return 0
    try:
        route = resolve_search_route(argv)
    except SearchRouteError as exc:
        return die(str(exc))
    previous = os.environ.get(SUPPRESS_RECEIPTS_ENV)
    os.environ[SUPPRESS_RECEIPTS_ENV] = "1"
    try:
        runner = load_plugin_runner(route.provider)
        try:
            return int(runner(route.provider_argv))
        except SystemExit as exc:
            return system_exit_status(exc)
    finally:
        if previous is None:
            os.environ.pop(SUPPRESS_RECEIPTS_ENV, None)
        else:
            os.environ[SUPPRESS_RECEIPTS_ENV] = previous


if __name__ == "__main__":
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    raise SystemExit(main(sys.argv[1:]))
