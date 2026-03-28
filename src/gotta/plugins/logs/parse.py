"""Argument parsing for `gotta logs`."""

from __future__ import annotations

import argparse

from gotta.session import charter as session_charter


def _add_root_args(parser: argparse.ArgumentParser) -> None:
    session_charter.add_target_args(parser)


def build_parser(command_name: str = "gotta logs") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description=(
            "Inspect or mutate the canonical session procedural trace. "
            "Shared-session read paths show all bound actor logs by default. "
            "Use notes for actor-authored narration; use logs for chronology and "
            "system/runtime trace."
        ),
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["show", "append", "extend"],
        help="show entries, append one entry, or extend with many entries",
    )
    parser.add_argument(
        "value",
        nargs="*",
        help="inline log text for append/extend",
    )
    _add_root_args(parser)
    parser.add_argument(
        "--from-file",
        help="read log text from a UTF-8 file instead of inline text; use '-' for stdin",
    )
    parser.add_argument(
        "--stdin",
        dest="use_stdin",
        action="store_true",
        help="read log text from stdin explicitly",
    )
    parser.add_argument("--output", choices=["json", "text"], default="text")
    parser.add_argument("--limit", type=int, default=0)
    return parser


def session_access_mode(argv: list[str]) -> str:
    positionals = session_charter.argv_positionals(
        argv,
        valued_flags=(
            "--session",
            "--actor",
            "--from-file",
            "--output",
            "--limit",
        ),
    )
    action = positionals[0] if positionals else "show"
    return "write" if action in {"append", "extend"} else "read"
