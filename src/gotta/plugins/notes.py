"""Top-level session-rooted peer notes surface."""

from __future__ import annotations

import argparse
import json

from gotta.helptext import format_long_help, is_long_help_request
from gotta.notes import (
    append_peer_note,
    peer_notes_log_path,
    peer_notes_payload,
    peer_notes_surface_path,
)
from gotta import session as session_plugin
from gotta.session import (
    _normalize_peer_name,
    _peer_label,
)


def _add_root_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", help="session root")


def build_parser(command_name: str = "gotta notes") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description=(
            "Inspect or mutate canonical peer notes inside the active session surface. "
            "Use this for live peer visibility instead of waiting on a separate summary artifact."
        ),
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["show", "append"],
        help="show one or all peer note surfaces, or append a peer note",
    )
    parser.add_argument("peer", nargs="?")
    parser.add_argument("value", nargs="*")
    _add_root_args(parser)
    parser.add_argument(
        "--from-file",
        help="read peer note text from a UTF-8 file instead of inline text; use '-' for stdin",
    )
    parser.add_argument(
        "--stdin",
        dest="use_stdin",
        action="store_true",
        help="read peer note text from stdin explicitly",
    )
    parser.add_argument("--output", choices=["json", "text"], default="text")
    return parser


def _normalize_args(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    first = argv[0]
    if first in {"show", "append"} or first.startswith("-"):
        return argv
    return ["show", *argv]


def _summary_payload(work_dir, *, peer_name: str) -> dict[str, object]:
    status = session_plugin._peer_status_payload(work_dir, peer_name)
    return {
        "peer": peer_name,
        "label": _peer_label(peer_name),
        "notes": str(peer_notes_surface_path(work_dir, peer_name)),
        "notes_log": str(peer_notes_log_path(work_dir, peer_name)),
        "status": str(status.get("status") or "pending"),
        "notes_status": str(status.get("notes_status") or "empty"),
        "artifact_count": int(status.get("artifact_count") or 0),
        "next_step": str(status.get("next_step") or ""),
    }


def _configured_peers(work_dir) -> list[str]:
    return list(session_plugin._selected_actor_ids(work_dir))


def _require_configured_peer(work_dir, peer_name: str) -> None:
    if peer_name not in session_plugin._selected_actor_ids(work_dir):
        raise SystemExit(
            f"{peer_name} is not configured for this session; choose them first with "
            f"`gotta peer with {_peer_label(peer_name)}`"
        )


def main(argv: list[str] | None = None) -> int:
    argv = _normalize_args(list(argv or []))
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
    session_dir = session_plugin._work_workspace_dir(
        explicit_session=getattr(args, "session", None)
    )
    work_dir = session_dir.resolve()
    action = args.action or "show"

    if action == "show":
        if args.peer:
            peer_name = _normalize_peer_name(args.peer)
            _require_configured_peer(work_dir, peer_name)
            session_plugin._ensure_peer_surface(work_dir, peer_name)
            status = session_plugin._peer_status_payload(work_dir, peer_name)
            payload = peer_notes_payload(
                work_dir,
                peer_name,
                label=_peer_label(peer_name),
                status_payload=status,
            )
            if args.output == "json":
                print(json.dumps(payload, indent=2, sort_keys=True))
                return 0
            print(peer_notes_surface_path(work_dir, peer_name).read_text(encoding="utf-8"), end="")
            return 0
        peers = _configured_peers(work_dir)
        payload = {
            peer: _summary_payload(work_dir, peer_name=peer)
            for peer in peers
        }
        if args.output == "json":
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        if not peers:
            print(
                "no peers configured for this session; choose one intentionally with "
                + session_plugin._work_with_examples(prefix="gotta peer with")
            )
            return 0
        for peer, summary in payload.items():
            print(
                f"{peer}: {summary['status']} "
                f"(notes: {summary['notes_status']}, artifacts: {summary['artifact_count']})"
            )
            print(f"  notes: {summary['notes']}")
            if summary.get("next_step"):
                print(f"  next_step: {summary['next_step']}")
        return 0

    if not args.peer:
        raise SystemExit("missing peer; use `gotta notes append <peer> ...`")
    peer_name = _normalize_peer_name(args.peer or "")
    _require_configured_peer(work_dir, peer_name)
    session_plugin._ensure_peer_surface(work_dir, peer_name)
    message = session_plugin._normalize_entry_text(
        session_plugin._read_text_source(
            session_root=session_dir,
            inline=(args.value[0] if args.value else None),
            from_file=args.from_file,
            use_stdin=args.use_stdin,
            input_name="peer note",
        ),
        input_name="peer note",
    )
    append_peer_note(work_dir, peer_name, message=message)
    session_plugin._append_peer_event(work_dir, peer_name, event="note", detail=message.splitlines()[0])
    session_plugin._peer_log_line(work_dir, peer_name, f"noted: {message.splitlines()[0]}")
    session_plugin._sync_peer_projection_surfaces(work_dir, peer_name)
    session_plugin._record_peer_projection_activity(
        work_dir,
        peer_name=peer_name,
        surface="notes",
        action="append",
        log_path=peer_notes_log_path(work_dir, peer_name),
        projection_path=peer_notes_surface_path(work_dir, peer_name),
        detail="appended peer note",
    )
    print(f"appended peer note for {peer_name}")
    return 0
