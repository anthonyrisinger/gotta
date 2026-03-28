"""Entrypoint for `gotta session timeline`."""

from __future__ import annotations

import argparse
import json

from ..parse import explicit_session_ref, require_started_session, session_dirs_for_read
from .payload import timeline_payload
from .render import render_timeline_text


def cmd_timeline(args: argparse.Namespace) -> int:
    dirs = session_dirs_for_read(args)
    require_started_session(dirs)
    payload = timeline_payload(
        dirs,
        limit=max(args.limit, 0),
        offset=max(args.offset, 0),
        include_all=bool(args.all),
        mode=args.mode,
        filter_query=str(getattr(args, "filter", "") or ""),
        session_ref=explicit_session_ref(args),
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(render_timeline_text(payload))
    return 0
