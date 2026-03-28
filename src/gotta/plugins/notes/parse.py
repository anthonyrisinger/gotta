"""Argument parsing for `gotta notes`."""

from __future__ import annotations

import argparse

from gotta.session import charter as session_charter


def _add_root_args(parser: argparse.ArgumentParser) -> None:
    session_charter.add_target_args(parser)


def build_parser(command_name: str = "gotta notes") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description=(
            "Inspect or mutate canonical actor notes inside the active session "
            "surface. Notes are the canonical actor-authored narration surface, "
            "and short one-line notes are valid."
        ),
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["show", "append"],
        help="show one or all actor note surfaces, or append a actor note",
    )
    parser.add_argument("value", nargs="*")
    _add_root_args(parser)
    parser.add_argument(
        "--from-file",
        help="read actor note text from a UTF-8 file instead of inline text; use '-' for stdin",
    )
    parser.add_argument(
        "--stdin",
        dest="use_stdin",
        action="store_true",
        help="read actor note text from stdin explicitly",
    )
    parser.add_argument("--output", choices=["json", "text"], default="text")
    return parser


def session_access_mode(argv: list[str]) -> str:
    positionals = session_charter.argv_positionals(
        argv,
        valued_flags=(
            "--session",
            "--actor",
            "--from-file",
            "--output",
        ),
    )
    action = positionals[0] if positionals else "show"
    return "write" if action == "append" else "read"


def normalize_argv(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    first = argv[0]
    if first in {"show", "append"} or first.startswith("-"):
        return argv
    return ["show", *argv]
