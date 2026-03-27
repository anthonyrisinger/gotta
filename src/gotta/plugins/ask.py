#!/usr/bin/env python3
"""Dispatch to installed ask-family subplugins."""

from __future__ import annotations

import sys
from typing import Any

from gotta.builtin import ASK_PLUGIN_GROUP, available_plugins, get_plugin
from gotta.helptext import is_long_help_request


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def available_asks() -> list[str]:
    return available_plugins(group=ASK_PLUGIN_GROUP)


def ask_spec(name: str):
    return get_plugin(name, group=ASK_PLUGIN_GROUP)


def print_usage() -> int:
    print("usage: gotta ask <surface> [args...]")
    print("")
    print("use `gotta ask --help-all` for recursive ask-surface help")
    print("")
    surfaces = available_asks()
    print("installed ask surfaces:")
    if not surfaces:
        print("  - none installed")
        print("")
        print(
            "install an ask-family extension that registers the `gotta.ask` "
            "entry-point group to enable `gotta ask <surface> ...`"
        )
        return 0
    for surface in surfaces:
        spec = ask_spec(surface)
        description = spec.description if spec else ""
        if description:
            print(f"  - {surface:<10} {description}")
            continue
        print(f"  - {surface}")
    return 0


def should_materialize(argv: list[str]) -> bool:
    if not argv or any(
        arg in {"-h", "--help", "--help-all", "help-all"} for arg in argv
    ):
        return False
    spec = ask_spec(argv[0])
    if spec and spec.should_materialize is not None:
        return bool(spec.should_materialize(argv[1:]))
    return True


def invocation_locator(argv: list[str]) -> str:
    if not argv:
        return "ask"
    surface = argv[0]
    spec = ask_spec(surface)
    if spec and spec.invocation_locator is not None:
        detail = spec.invocation_locator(argv[1:])
        if not detail or detail == surface:
            return f"ask {surface}"
        return f"ask {surface} {detail}"
    if len(argv) == 1:
        return f"ask {surface}"
    return f"ask {' '.join(argv)}"


def canonical_locator(argv: list[str]) -> str:
    if not argv:
        return "ask"
    surface = argv[0]
    spec = ask_spec(surface)
    if spec and spec.canonical_locator is not None:
        return spec.canonical_locator(argv[1:])
    if len(argv) == 1:
        return f"ask:{surface}"
    return f"ask:{surface}:{' '.join(argv[1:]).strip()}"


def preferred_name(argv: list[str], options: Any) -> str:
    if not argv:
        return options.save_as or "ask.txt"
    spec = ask_spec(argv[0])
    if spec and spec.preferred_name is not None:
        return spec.preferred_name(argv[1:], options)
    return options.save_as or f"{argv[0]}.txt"


def content_type(argv: list[str], name: str) -> str:
    if not argv:
        return "text/plain"
    spec = ask_spec(argv[0])
    if spec and spec.content_type is not None:
        return spec.content_type(argv[1:], name)
    return "text/plain"


def main(argv: list[str]) -> int:
    if not argv or (len(argv) == 1 and argv[0] in {"-h", "--help"}):
        return print_usage()
    if is_long_help_request(argv):
        print("## gotta ask")
        print("")
        print("usage: gotta ask <surface> [args...]")
        print("")
        surfaces = available_asks()
        print("installed ask surfaces:")
        if not surfaces:
            print("  - none installed")
            print("")
            print(
                "Install an ask-family extension that registers the "
                "`gotta.ask` entry-point group to enable this surface."
            )
            return 0
        for surface in surfaces:
            spec = ask_spec(surface)
            description = spec.description if spec else ""
            if description:
                print(f"  - {surface:<10} {description}")
            else:
                print(f"  - {surface}")
        for surface in surfaces:
            spec = ask_spec(surface)
            if spec is None:
                continue
            print("")
            print(f"## gotta ask {surface}")
            print("")
            result = int(spec.runner(["--help-all"]))
            if result != 0:
                return result
        return 0

    if argv[0] == "help":
        if len(argv) == 1:
            return print_usage()
        if len(argv) == 2 and argv[1] == "all":
            return main(["--help-all"])
        argv = [argv[1], "--help", *argv[2:]]

    surface = argv[0]
    spec = ask_spec(surface)
    if spec is None:
        available = available_asks()
        if not available:
            return die(
                f"unknown gotta ask surface: {surface}. no ask surfaces are installed"
            )
        surfaces = ", ".join(available)
        return die(
            f"unknown gotta ask surface: {surface}. installed ask surfaces: {surfaces}"
        )
    return int(spec.runner(argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
