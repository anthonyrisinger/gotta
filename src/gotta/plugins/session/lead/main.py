"""CLI entrypoint for `gotta session leads`."""

from __future__ import annotations

import argparse
import json

from ..parse import explicit_session_ref, require_started_session, session_dirs_for_read
from .payload import leads_payload
from .render import render_leads_text


def cmd_leads(args: argparse.Namespace) -> int:
    dirs = session_dirs_for_read(args)
    require_started_session(dirs)
    payload = leads_payload(
        dirs,
        target=args.target or "",
        filter_query=str(getattr(args, "filter", "") or ""),
        session_ref=explicit_session_ref(args),
        limit=max(args.limit, 0),
        offset=max(args.offset, 0),
        include_all=bool(args.all),
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(render_leads_text(payload))
    return 0
