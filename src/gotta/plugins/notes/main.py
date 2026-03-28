"""Entrypoint for `gotta notes`."""

from __future__ import annotations

from gotta.helptext import format_long_help, is_long_help_request

from .parse import build_parser, normalize_argv
from .show import show_notes
from .write import append_note


def main(argv: list[str] | None = None) -> int:
    argv = normalize_argv(list(argv or []))
    if is_long_help_request(argv):
        print(format_long_help(build_parser()))
        return 0
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if int(exc.code or 0) == 0:
            return 0
        raise
    if (args.action or "show") == "show":
        return show_notes(args)
    return append_note(args)
