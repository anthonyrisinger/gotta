"""Parsing and scope resolution for `gotta session`."""

from __future__ import annotations

import argparse

from gotta.content import (
    CommonOptions,
    ContentError,
    ResolvedDirs,
    resolve_dirs,
    session_is_initialized,
    state_env_path,
)
from gotta.session import charter as session_charter
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope

from .core import TIMELINE_MODE_HELP, shared_session_dirs_from_ref


def session_access_mode(argv: list[str]) -> str:
    positionals = session_charter.argv_positionals(
        argv,
        valued_flags=(
            "--session",
            "--actor",
            "--content-dir",
            "--output",
            "--limit",
            "--offset",
        ),
    )
    subcommand = positionals[0] if positionals else "show"
    return "write" if subcommand in {"init", "bind"} else "read"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gotta session",
        description="Inspect the active session-rooted gotta context.",
    )
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init")
    bind = sub.add_parser("bind")
    show = sub.add_parser("show")
    doctor = sub.add_parser("doctor")
    manifest = sub.add_parser("manifest")
    timeline = sub.add_parser(
        "timeline",
        description=(
            "Inspect session chronology. Default mode is acquired order; "
            "explicit source-authored chronology is available through "
            "`--mode created`, `--mode updated`, or `--mode best-effort`."
        ),
    )
    graph = sub.add_parser("graph")
    analyze = sub.add_parser("analyze")
    scan = sub.add_parser(
        "scan",
        description=(
            "Search materialized text across the session corpus without "
            "dropping to shell-native grep tools."
        ),
    )
    leads = sub.add_parser(
        "leads",
        description=(
            "Inspect explicit, followable leads mined from already-materialized "
            "artifact content. Leads are shown best-first using observed "
            "signals such as native-ness, materialization, recurrence, and "
            "source context."
        ),
    )

    for parser_ in (
        init,
        show,
        doctor,
        manifest,
        timeline,
        graph,
        analyze,
        scan,
        leads,
    ):
        parser_.add_argument("--session", help="session root")
        parser_.add_argument("--actor", help="actor within the current session")
        parser_.add_argument(
            "--content-dir", help="explicit content directory override"
        )

    bind.add_argument(
        "session_id",
        nargs="?",
        help="session reference: shared session id, exact session root, or <session>/<actor>",
    )
    bind.add_argument(
        "--output", choices=["summary", "json", "path"], default="summary"
    )

    init.add_argument(
        "--output",
        "--print",
        dest="print_format",
        choices=["env", "json", "path", "sh"],
        default="path",
    )
    show.add_argument(
        "--output",
        "--print",
        dest="print_format",
        choices=["env", "json", "path", "sh"],
        default="path",
    )
    doctor.add_argument(
        "--output",
        "--print",
        dest="print_format",
        choices=["summary", "json"],
        default="json",
    )
    manifest.add_argument("--plugin")
    manifest.add_argument("--locator")
    manifest.add_argument(
        "--filter",
        help="case-insensitive regex applied to manifest rows",
    )
    manifest.add_argument("--limit", type=int, default=100)
    manifest.add_argument("--offset", type=int, default=0)
    manifest.add_argument("--all", action="store_true")
    manifest.add_argument("--output", choices=["json", "text"], default="text")
    timeline.add_argument(
        "--filter",
        help="case-insensitive regex applied to timeline events",
    )
    timeline.add_argument("--limit", type=int, default=100)
    timeline.add_argument("--offset", type=int, default=0)
    timeline.add_argument("--all", action="store_true")
    timeline.add_argument("--output", choices=["json", "text"], default="text")
    timeline.add_argument(
        "--mode",
        default="acquired",
        metavar="MODE",
        help=TIMELINE_MODE_HELP,
    )
    graph.add_argument(
        "--output",
        choices=["mermaid", "json", "text"],
        default="mermaid",
        help="render the content graph as Mermaid, text, or structured JSON",
    )
    graph.add_argument(
        "--filter",
        help="case-insensitive regex applied to graph nodes and edges",
    )
    analyze.add_argument(
        "--output",
        choices=["text", "mermaid", "markdown", "json"],
        default="text",
        help="render a text summary, Mermaid graph, Markdown bundle, or structured JSON to stdout",
    )
    analyze.add_argument(
        "--mode",
        choices=["lineage", "semantic", "all"],
        default="all",
        help="write lineage content, semantic content, or both; raw Mermaid output requires lineage or semantic mode",
    )
    analyze.add_argument(
        "--focus",
        help="narrow the analysis view to one keyword, locator, artifact name, or checksum prefix",
    )
    analyze.add_argument(
        "--limit",
        type=int,
        default=8,
        help="maximum items to show per overview/focus section",
    )
    scan.add_argument(
        "query",
        help="literal text or regex to search for in stored materialized text",
    )
    scan.add_argument("--plugin")
    scan.add_argument("--locator")
    scan.add_argument("--kind", choices=["discovery", "evidence"])
    scan.add_argument("--match", choices=["literal", "regex"], default="literal")
    scan.add_argument("--case-sensitive", action="store_true")
    scan.add_argument("--context", type=int, default=2)
    scan.add_argument("--snippets", type=int, default=3)
    scan.add_argument("--limit", type=int, default=20)
    scan.add_argument("--offset", type=int, default=0)
    scan.add_argument("--all", action="store_true")
    scan.add_argument("--output", choices=["json", "text"], default="text")
    leads.add_argument("target", nargs="?")
    leads.add_argument(
        "--filter",
        help="case-insensitive regex applied to derived lead records",
    )
    leads.add_argument("--limit", type=int, default=100)
    leads.add_argument("--offset", type=int, default=0)
    leads.add_argument("--all", action="store_true")
    leads.add_argument("--output", choices=["json", "text"], default="text")
    return parser


def options_from_args(args: argparse.Namespace) -> CommonOptions:
    return CommonOptions(
        session_dir=getattr(args, "session", None),
        content_dir=getattr(args, "content_dir", None),
        actor=getattr(args, "actor", None),
    )


def explicit_session_ref(args: argparse.Namespace) -> str:
    return str(getattr(args, "session", None) or "").strip()


def explicit_actor_ref(args: argparse.Namespace) -> str:
    return str(getattr(args, "actor", None) or "").strip()


def session_dirs_for_read(args: argparse.Namespace) -> ResolvedDirs:
    session_ref = explicit_session_ref(args)
    if session_ref:
        shared = shared_session_dirs_from_ref(session_ref)
        if shared is not None:
            shared_root, _session_id = shared
            group_root = session_registry._group_session_root(shared_root)
            if group_root != shared_root or (group_root / "actors").is_dir():
                return ResolvedDirs(
                    session_dir=group_root.resolve(),
                    content_dir=(group_root / "content").resolve(),
                )
        return resolve_dirs(
            CommonOptions(
                session_dir=session_ref,
                content_dir=getattr(args, "content_dir", None),
            ),
            create=False,
        )
    return resolve_dirs(
        CommonOptions(
            session_dir=getattr(args, "session", None),
            content_dir=getattr(args, "content_dir", None),
        ),
        create=False,
    )


def session_scope_started(dirs: ResolvedDirs) -> bool:
    if session_is_initialized(dirs.session_dir):
        return True
    session_root = session_registry._group_session_root(dirs.session_dir)
    if session_root != dirs.session_dir.resolve() and session_is_initialized(
        session_root
    ):
        return True
    if (session_root / "actors").is_dir() and dirs.content_dir.exists():
        return bool(session_scope._selected_actor_ids(session_root))
    return False


def require_started_session(dirs: ResolvedDirs) -> None:
    state_file = state_env_path(dirs.session_dir)
    if not session_scope_started(dirs):
        raise ContentError(
            "start or bind a session first with `gotta ...`. Stable interactive "
            "contexts adopt and scaffold their deterministic session on first "
            "session-aware use. Use `gotta session init` only when you "
            "intentionally want to scaffold one exact root; "
            f"missing {state_file}"
        )
