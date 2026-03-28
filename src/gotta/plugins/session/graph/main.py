"""Command surface for `gotta session graph`."""

from __future__ import annotations

import argparse
import json

from ..parse import explicit_session_ref, require_started_session, session_dirs_for_read
from .mermaid import render_mermaid
from .payload import graph_payload
from .render import render_graph_text


def cmd_graph(args: argparse.Namespace) -> int:
    dirs = session_dirs_for_read(args)
    require_started_session(dirs)
    payload = graph_payload(
        dirs,
        filter_query=str(getattr(args, "filter", "") or ""),
        session_ref=explicit_session_ref(args),
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.output == "text":
        print(render_graph_text(payload))
        return 0
    print(render_mermaid(payload))
    return 0
