"""Command surface for `gotta session manifest`."""

from __future__ import annotations

import argparse
import json

from ..parse import explicit_session_ref, require_started_session, session_dirs_for_read
from .payload import manifest_payload
from .render import render_manifest_text


def cmd_manifest(args: argparse.Namespace) -> int:
    dirs = session_dirs_for_read(args)
    require_started_session(dirs)
    payload = manifest_payload(
        dirs,
        plugin=args.plugin or "",
        actor=args.actor or "",
        locator=args.locator or "",
        filter_query=str(getattr(args, "filter", "") or ""),
        session_ref=explicit_session_ref(args),
        limit=args.limit,
        offset=max(args.offset, 0),
        include_all=bool(args.all),
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(render_manifest_text(payload, session_root=dirs.session_dir))
    return 0
