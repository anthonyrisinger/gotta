"""Entrypoint for `gotta session analyze`."""

from __future__ import annotations

import argparse
import json
from typing import Any

from ..parse import explicit_session_ref, require_started_session, session_dirs_for_read
from ..scan import scan_payload
from .lineage import lineage_focus_payload
from .overview import (
    analysis_overview_payload,
    analysis_payload,
    combined_analysis_payload,
    semantic_payload,
)
from .render import (
    render_analysis_focus_text,
    render_analysis_mermaid,
    render_analysis_overview_markdown,
    render_analysis_overview_text,
    render_combined_focus_markdown,
    render_lineage_focus_markdown_section,
    render_lineage_focus_text,
    render_lineage_overview_markdown,
    render_lineage_overview_text,
    render_semantic_focus_markdown_section,
    render_semantic_mermaid,
    render_semantic_overview_markdown,
    render_semantic_overview_text,
    render_single_focus_markdown,
    render_text_bundle,
)
from .semantic import semantic_focus_payload


def cmd_analyze(args: argparse.Namespace) -> int:
    dirs = session_dirs_for_read(args)
    require_started_session(dirs)
    session_ref = explicit_session_ref(args)
    lineage_payload = analysis_payload(dirs, session_ref=session_ref)
    lineage_mermaid = render_analysis_mermaid(lineage_payload)
    semantic_graph = semantic_payload(dirs, session_ref=session_ref)
    semantic_mermaid = render_semantic_mermaid(semantic_graph)
    focus_query = str(getattr(args, "focus", "") or "").strip()
    focus_limit = max(int(getattr(args, "limit", 8) or 0), 0)
    overview = analysis_overview_payload(
        lineage_payload,
        semantic_graph,
        limit=focus_limit,
    )
    lineage_focus: dict[str, Any] | None = None
    semantic_focus: dict[str, Any] | None = None
    if focus_query:
        focus_scan = scan_payload(
            dirs,
            query=focus_query,
            limit=max(focus_limit * 2, 12),
            include_all=True,
            session_ref=session_ref,
        )
        lineage_focus = lineage_focus_payload(
            lineage_payload,
            focus=focus_query,
            limit=focus_limit,
            scan_payload=focus_scan,
        )
        semantic_focus = semantic_focus_payload(
            lineage_payload,
            semantic_graph,
            focus=focus_query,
            limit=focus_limit,
            scan_payload=focus_scan,
        )

    if args.output == "text":
        if args.mode == "lineage":
            print(
                render_lineage_focus_text(lineage_focus)
                if lineage_focus is not None
                else render_lineage_overview_text(lineage_payload, limit=focus_limit)
            )
        elif args.mode == "semantic":
            print(
                render_analysis_focus_text(semantic_focus)
                if semantic_focus is not None
                else render_semantic_overview_text(overview)
            )
        elif focus_query:
            assert lineage_focus is not None
            assert semantic_focus is not None
            print(
                render_text_bundle(
                    [
                        ("Lineage", render_lineage_focus_text(lineage_focus)),
                        ("Semantic", render_analysis_focus_text(semantic_focus)),
                    ]
                )
            )
        else:
            print(render_analysis_overview_text(overview))
    elif args.output == "json":
        if args.mode == "lineage":
            print(
                json.dumps(
                    lineage_focus if lineage_focus is not None else lineage_payload,
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.mode == "semantic":
            print(
                json.dumps(
                    semantic_focus if semantic_focus is not None else semantic_graph,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(
                json.dumps(
                    combined_analysis_payload(
                        focus=focus_query,
                        lineage=(
                            lineage_focus
                            if lineage_focus is not None
                            else lineage_payload
                        ),
                        semantic=(
                            semantic_focus
                            if semantic_focus is not None
                            else semantic_graph
                        ),
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
    elif args.output == "markdown":
        if args.mode == "lineage":
            print(
                render_single_focus_markdown(
                    session_dir=str(lineage_focus["sessionDir"]),
                    focus=str(lineage_focus.get("focus") or ""),
                    section_lines=render_lineage_focus_markdown_section(lineage_focus),
                )
                if lineage_focus is not None
                else render_lineage_overview_markdown(
                    lineage_payload, limit=focus_limit
                ),
                end="",
            )
        elif args.mode == "semantic":
            print(
                render_single_focus_markdown(
                    session_dir=str(semantic_focus["sessionDir"]),
                    focus=str(semantic_focus.get("focus") or ""),
                    section_lines=render_semantic_focus_markdown_section(
                        semantic_focus
                    ),
                )
                if semantic_focus is not None
                else render_semantic_overview_markdown(overview),
                end="",
            )
        elif focus_query:
            assert lineage_focus is not None
            assert semantic_focus is not None
            print(
                render_combined_focus_markdown(
                    lineage=lineage_focus,
                    semantic=semantic_focus,
                ),
                end="",
            )
        else:
            print(
                render_analysis_overview_markdown(
                    overview,
                    lineage=lineage_payload,
                    limit=focus_limit,
                ),
                end="",
            )
    else:
        if args.mode == "lineage":
            print(
                render_analysis_mermaid(lineage_focus)
                if lineage_focus is not None
                else lineage_mermaid
            )
        else:
            print(
                render_semantic_mermaid(semantic_focus)
                if semantic_focus is not None
                else semantic_mermaid
            )
    return 0
