"""Entrypoint for the `gotta session` plugin."""

from __future__ import annotations

from gotta import binding as binding_helpers
from gotta.content.model import ContentError
from gotta.helptext import is_long_help_request, print_long_help

from .analyze.main import cmd_analyze
from .graph.main import cmd_graph
from .init import cmd_init
from .lead.main import cmd_leads
from .manifest.main import cmd_manifest
from .parse import build_parser
from .doctor import cmd_doctor
from .scan.main import cmd_scan
from .show import cmd_show
from .timeline.main import cmd_timeline


def main(argv: list[str]) -> int:
    parser = build_parser()
    if is_long_help_request(argv):
        return print_long_help(parser)
    args = parser.parse_args(argv)
    try:
        if (
            getattr(args, "command", None) == "analyze"
            and getattr(args, "output", None) == "mermaid"
            and getattr(args, "mode", None) == "all"
        ):
            parser.error(
                "`--output mermaid` requires `--mode lineage` or `--mode semantic`; "
                "use `--output text|json` for combined output"
            )
        command = args.command or "show"
        if command == "bind":
            return binding_helpers.bind_current_context(
                session_ref=getattr(args, "session_id", None),
                output=getattr(args, "output", "summary"),
            )
        if command == "show":
            return cmd_show(args)
        if command == "init":
            return cmd_init(args)
        if command == "doctor":
            return cmd_doctor(args)
        if command == "manifest":
            return cmd_manifest(args)
        if command == "timeline":
            return cmd_timeline(args)
        if command == "graph":
            return cmd_graph(args)
        if command == "leads":
            return cmd_leads(args)
        if command == "analyze":
            return cmd_analyze(args)
        if command == "scan":
            return cmd_scan(args)
    except ContentError as exc:
        if "missing shared content context" in str(exc) and getattr(
            args, "command", None
        ) in {
            None,
            "show",
            "doctor",
            "manifest",
            "timeline",
            "graph",
            "leads",
            "analyze",
            "scan",
        }:
            parser.exit(
                status=2,
                message=(
                    "start or bind a session first with `gotta ...`. Stable "
                    "interactive contexts adopt and scaffold their deterministic "
                    "session on first session-aware use. Use `gotta session "
                    "init` only when you intentionally want to scaffold one exact "
                    "root.\n"
                ),
            )
        parser.exit(status=2, message=f"{exc}\n")
    return 2
