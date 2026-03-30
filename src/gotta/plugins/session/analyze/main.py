"""Entrypoint for `gotta session analyze`."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json

from ..backend import SemanticIndexBackend, default_semantic_index_backend
from ..parse import explicit_session_ref, require_started_session, session_dirs_for_read
from ..scan.payload import scan_payload
from .model import (
    AnalysisOverviewPayload,
    CombinedAnalysisPayload,
    LineageFocusPayload,
    LineagePayload,
    SemanticFocusPayload,
    SemanticPayload,
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


@dataclass(frozen=True)
class _AnalyzeState:
    backend: SemanticIndexBackend
    mode: str
    output: str
    focus_query: str
    focus_limit: int
    lineage_graph: LineagePayload
    semantic_graph: SemanticPayload
    overview: AnalysisOverviewPayload
    lineage_focus: LineageFocusPayload | None
    semantic_focus: SemanticFocusPayload | None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> _AnalyzeState:
        dirs = session_dirs_for_read(args)
        require_started_session(dirs)
        session_ref = explicit_session_ref(args)
        backend = default_semantic_index_backend()
        lineage_graph = backend.query_lineage(dirs, session_ref=session_ref)
        semantic_graph = backend.query_semantic(lineage_graph)
        focus_query = str(getattr(args, "focus", "") or "").strip()
        focus_limit = max(int(getattr(args, "limit", 8) or 0), 0)
        overview = backend.query_overview(
            lineage_graph,
            semantic_graph,
            limit=focus_limit,
        )
        lineage_focus: LineageFocusPayload | None = None
        semantic_focus: SemanticFocusPayload | None = None
        if focus_query:
            focus_scan = scan_payload(
                dirs,
                query=focus_query,
                limit=max(focus_limit * 2, 12),
                include_all=True,
                session_ref=session_ref,
            )
            lineage_focus = backend.query_lineage_focus(
                lineage_graph,
                focus=focus_query,
                limit=focus_limit,
                scan_payload=focus_scan,
            )
            semantic_focus = backend.query_semantic_focus(
                lineage_graph,
                semantic_graph,
                focus=focus_query,
                limit=focus_limit,
                scan_payload=focus_scan,
            )
        return cls(
            backend=backend,
            mode=args.mode,
            output=args.output,
            focus_query=focus_query,
            focus_limit=focus_limit,
            lineage_graph=lineage_graph,
            semantic_graph=semantic_graph,
            overview=overview,
            lineage_focus=lineage_focus,
            semantic_focus=semantic_focus,
        )

    def execute(self) -> int:
        if self.output == "text":
            self._emit_text()
        elif self.output == "json":
            self._emit_json()
        elif self.output == "markdown":
            self._emit_markdown()
        else:
            self._emit_mermaid()
        return 0

    def _emit_text(self) -> None:
        if self.mode == "lineage":
            print(
                render_lineage_focus_text(self.lineage_focus)
                if self.lineage_focus is not None
                else render_lineage_overview_text(
                    self.lineage_graph,
                    limit=self.focus_limit,
                )
            )
            return
        if self.mode == "semantic":
            print(
                render_analysis_focus_text(self.semantic_focus)
                if self.semantic_focus is not None
                else render_semantic_overview_text(self.overview)
            )
            return
        if self.focus_query:
            assert self.lineage_focus is not None
            assert self.semantic_focus is not None
            print(
                render_text_bundle(
                    [
                        ("Lineage", render_lineage_focus_text(self.lineage_focus)),
                        ("Semantic", render_analysis_focus_text(self.semantic_focus)),
                    ]
                )
            )
            return
        print(render_analysis_overview_text(self.overview))

    def _emit_json(self) -> None:
        payload: (
            LineagePayload
            | LineageFocusPayload
            | SemanticPayload
            | SemanticFocusPayload
            | CombinedAnalysisPayload
        )
        if self.mode == "lineage":
            payload = self.lineage_focus or self.lineage_graph
        elif self.mode == "semantic":
            payload = self.semantic_focus or self.semantic_graph
        else:
            payload = self._combined_payload()
        print(json.dumps(payload, indent=2, sort_keys=True))

    def _emit_markdown(self) -> None:
        if self.mode == "lineage":
            print(self._lineage_markdown(), end="")
            return
        if self.mode == "semantic":
            print(self._semantic_markdown(), end="")
            return
        if self.focus_query:
            assert self.lineage_focus is not None
            assert self.semantic_focus is not None
            print(
                render_combined_focus_markdown(
                    lineage=self.lineage_focus,
                    semantic=self.semantic_focus,
                ),
                end="",
            )
            return
        print(
            render_analysis_overview_markdown(
                self.overview,
                lineage=self.lineage_graph,
                limit=self.focus_limit,
            ),
            end="",
        )

    def _emit_mermaid(self) -> None:
        if self.mode == "lineage":
            print(
                render_analysis_mermaid(
                    self.lineage_focus or self.lineage_graph,
                )
            )
            return
        print(render_semantic_mermaid(self.semantic_focus or self.semantic_graph))

    def _combined_payload(self) -> CombinedAnalysisPayload:
        return self.backend.query_combined(
            focus=self.focus_query,
            lineage=self.lineage_focus or self.lineage_graph,
            semantic=self.semantic_focus or self.semantic_graph,
        )

    def _lineage_markdown(self) -> str:
        if self.lineage_focus is None:
            return render_lineage_overview_markdown(
                self.lineage_graph,
                limit=self.focus_limit,
            )
        return render_single_focus_markdown(
            session_dir=str(self.lineage_focus.get("sessionDir") or ""),
            focus=str(self.lineage_focus.get("focus") or ""),
            section_lines=render_lineage_focus_markdown_section(self.lineage_focus),
        )

    def _semantic_markdown(self) -> str:
        if self.semantic_focus is None:
            return render_semantic_overview_markdown(self.overview)
        return render_single_focus_markdown(
            session_dir=str(self.semantic_focus.get("sessionDir") or ""),
            focus=str(self.semantic_focus.get("focus") or ""),
            section_lines=render_semantic_focus_markdown_section(self.semantic_focus),
        )


def cmd_analyze(args: argparse.Namespace) -> int:
    return _AnalyzeState.from_args(args).execute()
