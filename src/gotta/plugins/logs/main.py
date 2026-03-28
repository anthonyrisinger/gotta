"""Entrypoint for `gotta logs`."""

from __future__ import annotations

from gotta.helptext import format_long_help, is_long_help_request

from .parse import build_parser
from .show import show_logs
from .write import append_logs, extend_logs


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
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
    action = args.action or "show"
    if action == "show":
        return show_logs(args)
    if action == "extend":
        return extend_logs(args)
    return append_logs(args)
