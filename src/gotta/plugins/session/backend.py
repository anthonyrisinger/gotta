"""Derived backend contracts for session view surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict, runtime_checkable

from gotta.content.model import ArtifactRecord, ResolvedDirs

if TYPE_CHECKING:
    from .analyze.model import (
        AnalysisOverviewPayload,
        AnalyzeScanPayload,
        CombinedAnalysisPayload,
        LineageFocusPayload,
        LineagePayload,
        SemanticFocusPayload,
        SemanticPayload,
    )
    from .graph.model import GraphPayload
    from .lead.model import LeadsPayload


class DerivedBackendHealth(TypedDict):
    backend: str
    status: Literal["healthy"]
    detail: str


class DerivedBackendStaleness(TypedDict):
    backend: str
    stale: bool
    detail: str


@runtime_checkable
class DerivedIndexBackend(Protocol):
    def ingest_artifact(self, artifact: ArtifactRecord) -> None: ...

    def ingest_state_record(self, channel: str, payload: Mapping[str, Any]) -> None: ...

    def rebuild(self, dirs: ResolvedDirs) -> None: ...

    def health(self, dirs: ResolvedDirs) -> DerivedBackendHealth: ...

    def staleness(self, dirs: ResolvedDirs) -> DerivedBackendStaleness: ...


@runtime_checkable
class GraphIndexBackend(DerivedIndexBackend, Protocol):
    def query_graph(
        self,
        dirs: ResolvedDirs,
        *,
        filter_query: str = "",
        session_ref: str = "",
    ) -> GraphPayload: ...


@runtime_checkable
class LeadIndexBackend(DerivedIndexBackend, Protocol):
    def query_leads(
        self,
        dirs: ResolvedDirs,
        *,
        target: str = "",
        filter_query: str = "",
        limit: int = 100,
        offset: int = 0,
        include_all: bool = False,
        session_ref: str = "",
    ) -> LeadsPayload: ...


@runtime_checkable
class SemanticIndexBackend(DerivedIndexBackend, Protocol):
    def query_lineage(
        self,
        dirs: ResolvedDirs,
        *,
        session_ref: str = "",
    ) -> LineagePayload: ...

    def query_semantic(self, lineage: LineagePayload) -> SemanticPayload: ...

    def query_overview(
        self,
        lineage: LineagePayload,
        semantic: SemanticPayload,
        *,
        limit: int,
    ) -> AnalysisOverviewPayload: ...

    def query_lineage_focus(
        self,
        lineage: LineagePayload,
        *,
        focus: str,
        limit: int,
        scan_payload: AnalyzeScanPayload | None = None,
    ) -> LineageFocusPayload: ...

    def query_semantic_focus(
        self,
        lineage: LineagePayload,
        semantic: SemanticPayload,
        *,
        focus: str,
        limit: int,
        scan_payload: AnalyzeScanPayload | None = None,
    ) -> SemanticFocusPayload: ...

    def query_combined(
        self,
        *,
        focus: str,
        lineage: LineagePayload | LineageFocusPayload,
        semantic: SemanticPayload | SemanticFocusPayload,
    ) -> CombinedAnalysisPayload: ...


class _OnDemandDerivedBackend:
    backend_name: str
    backend_detail: str

    def ingest_artifact(self, artifact: ArtifactRecord) -> None:
        return None

    def ingest_state_record(self, channel: str, payload: Mapping[str, Any]) -> None:
        return None

    def rebuild(self, dirs: ResolvedDirs) -> None:
        return None

    def health(self, dirs: ResolvedDirs) -> DerivedBackendHealth:
        return {
            "backend": self.backend_name,
            "status": "healthy",
            "detail": self.backend_detail,
        }

    def staleness(self, dirs: ResolvedDirs) -> DerivedBackendStaleness:
        return {
            "backend": self.backend_name,
            "stale": False,
            "detail": (
                "This backend derives results directly from current ledger truth on "
                "every query."
            ),
        }


class _InProcessGraphIndexBackend(_OnDemandDerivedBackend):
    backend_name = "graph-in-process"
    backend_detail = (
        "The graph backend derives session graph results directly from the current "
        "ledger."
    )

    def query_graph(
        self,
        dirs: ResolvedDirs,
        *,
        filter_query: str = "",
        session_ref: str = "",
    ) -> GraphPayload:
        from .graph.payload import graph_payload

        return graph_payload(dirs, filter_query=filter_query, session_ref=session_ref)


class _InProcessLeadIndexBackend(_OnDemandDerivedBackend):
    backend_name = "lead-in-process"
    backend_detail = (
        "The lead backend derives lead results directly from the current ledger."
    )

    def query_leads(
        self,
        dirs: ResolvedDirs,
        *,
        target: str = "",
        filter_query: str = "",
        limit: int = 100,
        offset: int = 0,
        include_all: bool = False,
        session_ref: str = "",
    ) -> LeadsPayload:
        from .lead.payload import leads_payload

        return leads_payload(
            dirs,
            target=target,
            filter_query=filter_query,
            limit=limit,
            offset=offset,
            include_all=include_all,
            session_ref=session_ref,
        )


class _InProcessSemanticIndexBackend(_OnDemandDerivedBackend):
    backend_name = "semantic-in-process"
    backend_detail = (
        "The semantic backend derives lineage and semantic results directly from "
        "the current ledger."
    )

    def query_lineage(
        self,
        dirs: ResolvedDirs,
        *,
        session_ref: str = "",
    ) -> LineagePayload:
        from .analyze.lineage import lineage_payload

        return lineage_payload(dirs, session_ref=session_ref)

    def query_semantic(self, lineage: LineagePayload) -> SemanticPayload:
        from .analyze.semantic import semantic_payload

        return semantic_payload(lineage)

    def query_overview(
        self,
        lineage: LineagePayload,
        semantic: SemanticPayload,
        *,
        limit: int,
    ) -> AnalysisOverviewPayload:
        from .analyze.overview import analysis_overview_payload

        return analysis_overview_payload(lineage, semantic, limit=limit)

    def query_lineage_focus(
        self,
        lineage: LineagePayload,
        *,
        focus: str,
        limit: int,
        scan_payload: AnalyzeScanPayload | None = None,
    ) -> LineageFocusPayload:
        from .analyze.lineage import lineage_focus_payload

        return lineage_focus_payload(
            lineage,
            focus=focus,
            limit=limit,
            scan_payload=scan_payload,
        )

    def query_semantic_focus(
        self,
        lineage: LineagePayload,
        semantic: SemanticPayload,
        *,
        focus: str,
        limit: int,
        scan_payload: AnalyzeScanPayload | None = None,
    ) -> SemanticFocusPayload:
        from .analyze.semantic import semantic_focus_payload

        return semantic_focus_payload(
            lineage,
            semantic,
            focus=focus,
            limit=limit,
            scan_payload=scan_payload,
        )

    def query_combined(
        self,
        *,
        focus: str,
        lineage: LineagePayload | LineageFocusPayload,
        semantic: SemanticPayload | SemanticFocusPayload,
    ) -> CombinedAnalysisPayload:
        from .analyze.overview import combined_analysis_payload

        return combined_analysis_payload(
            focus=focus,
            lineage=lineage,
            semantic=semantic,
        )


_GRAPH_INDEX_BACKEND: GraphIndexBackend = _InProcessGraphIndexBackend()
_LEAD_INDEX_BACKEND: LeadIndexBackend = _InProcessLeadIndexBackend()
_SEMANTIC_INDEX_BACKEND: SemanticIndexBackend = _InProcessSemanticIndexBackend()


def default_graph_index_backend() -> GraphIndexBackend:
    return _GRAPH_INDEX_BACKEND


def default_lead_index_backend() -> LeadIndexBackend:
    return _LEAD_INDEX_BACKEND


def default_semantic_index_backend() -> SemanticIndexBackend:
    return _SEMANTIC_INDEX_BACKEND


__all__ = [
    "DerivedBackendHealth",
    "DerivedBackendStaleness",
    "DerivedIndexBackend",
    "GraphIndexBackend",
    "LeadIndexBackend",
    "SemanticIndexBackend",
    "default_graph_index_backend",
    "default_lead_index_backend",
    "default_semantic_index_backend",
]
