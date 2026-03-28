"""Entrypoint for `gotta session scan`."""

from __future__ import annotations

import argparse
import json

from ..parse import explicit_session_ref, require_started_session, session_dirs_for_read
from .payload import scan_payload
from .render import render_scan_text


def cmd_scan(args: argparse.Namespace) -> int:
    dirs = session_dirs_for_read(args)
    require_started_session(dirs)
    session_ref = explicit_session_ref(args)
    payload = scan_payload(
        dirs,
        query=str(args.query or ""),
        plugin=str(args.plugin or ""),
        actor=str(args.actor or ""),
        locator=str(args.locator or ""),
        kind=str(args.kind or ""),
        match_mode=str(args.match or "literal"),
        case_sensitive=bool(args.case_sensitive),
        context=max(int(args.context or 0), 0),
        snippet_limit=max(int(args.snippets or 0), 0) or 1,
        limit=max(int(args.limit or 0), 0),
        offset=max(int(args.offset or 0), 0),
        include_all=bool(args.all),
        session_ref=session_ref,
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(render_scan_text(payload))
    return 0
