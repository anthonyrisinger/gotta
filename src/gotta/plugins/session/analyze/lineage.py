"""Lineage payload builders for session analysis."""

from __future__ import annotations

from collections.abc import Mapping
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from gotta.content.backend import scan_content_snapshots
from gotta.content.model import ContentSnapshot
from gotta.content.path import content_locator
from gotta.lead.aggregate import aggregate_lead_sources
from gotta.lead.edge import build_lead_edges
from gotta.lead.model import LeadEdge as MaterializedLeadEdge
from gotta.lead.model import LeadSourceSummary as MaterializedLeadSourceSummary
from gotta.lead.snapshot import (
    snapshot_artifact_locator,
    snapshot_display_name,
    snapshot_locator,
)
from gotta.source.visibility import best_visibility_metadata

from ..core import (
    artifact_kind,
    follow_command,
    lead_kind,
    provider_name,
    rendered_actor,
    render_variant,
    render_variant_label,
    resolved_visibility_metadata,
    resource_label,
    session_read_command,
    topology_next_step,
)
from ..manifest.record import manifest_entries
from .focus import select_lineage_focus
from .model import (
    AnalyzeScanPayload,
    AnalyzeVisibility,
    LeadEdgeSummary,
    LineageContent,
    LineageFocusPayload,
    LineagePayload,
    LineageRevisionEdge,
    LeadSourceSummary,
    LineageSource,
    LineageSourceEdge,
)

_VISIBILITY_LEVEL_KEY = "visibility_level"
_VISIBILITY_BOUNDARY_KEY = "visibility_boundary"
_VISIBILITY_CONFIDENCE_KEY = "visibility_confidence"
_VISIBILITY_BASIS_KEY = "visibility_basis"


@dataclass(frozen=True, slots=True)
class _RevisionTrackEvent:
    timestamp: str
    digest: str
    preferred_name: str
    plugin: str
    actor: str
    rendering: str


@dataclass(slots=True)
class _SourceState:
    content: set[str] = field(default_factory=set)
    locators: set[str] = field(default_factory=set)
    plugins: set[str] = field(default_factory=set)
    actors: set[str] = field(default_factory=set)
    artifact_kinds: set[str] = field(default_factory=set)
    entries: int = 0
    variants: set[tuple[str, str]] = field(default_factory=set)
    visibility: dict[str, object] = field(default_factory=dict)

    def record(
        self,
        *,
        checksum: str,
        locator: str,
        plugin: str,
        actor: str,
        source_artifact_kind: str,
        variant: tuple[str, str] | None,
        visibility: Mapping[str, object],
    ) -> None:
        self.content.add(checksum)
        self.locators.add(locator)
        self.plugins.add(plugin)
        self.actors.add(actor)
        if source_artifact_kind:
            self.artifact_kinds.add(source_artifact_kind)
        self.entries += 1
        self.visibility = dict(best_visibility_metadata(self.visibility, visibility))
        if variant is not None:
            self.variants.add(variant)

    def render(self, locator: str) -> LineageSource:
        rendered: LineageSource = {
            "locator": locator,
            "contentCount": len(self.content),
            "entryCount": self.entries,
            "artifactKind": (
                sorted(self.artifact_kinds)[0] if len(self.artifact_kinds) == 1 else ""
            ),
            "artifactKinds": sorted(self.artifact_kinds),
            "plugins": sorted(self.plugins),
            "actors": sorted(self.actors),
            "locators": sorted(self.locators),
            "collision": False,
            "duplicateMaterialization": len(self.content) > 1
            and len(self.variants) <= 1,
            "variant": len(self.variants) > 1,
            "variantCount": len(self.variants),
            "variants": [
                render_variant_label(variant) for variant in sorted(self.variants)
            ],
        }
        _apply_visibility(rendered, _visibility_metadata(self.visibility))
        return rendered


@dataclass(slots=True)
class _ContentDetailState:
    providers: set[str] = field(default_factory=set)
    actors: set[str] = field(default_factory=set)
    resource_hints: set[str] = field(default_factory=set)

    def record(self, *, source: str, plugin: str, actor: str) -> None:
        self.providers.add(provider_name(source, plugins=[plugin], fallback=plugin))
        self.actors.add(actor)
        source_kind, source_label = resource_label(source)
        if source_kind and source_label:
            self.resource_hints.add(f"{source_kind}:{source_label}")
            return
        self.resource_hints.add(source)


@dataclass(slots=True)
class _LineageBuildState:
    session_dir: Path
    source_states: dict[str, _SourceState] = field(default_factory=dict)
    edge_plugins: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    edge_actors: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    content_details: dict[str, _ContentDetailState] = field(default_factory=dict)

    def record_manifest_entry(
        self,
        entry: Mapping[str, object],
        *,
        snapshot_by_digest: Mapping[str, ContentSnapshot],
    ) -> None:
        source = str(
            entry.get("canonical_locator") or entry.get("locator") or "unknown"
        )
        checksum = str(entry.get("checksum") or "")
        if not checksum:
            return
        locator = str(entry.get("locator") or source)
        plugin = str(entry.get("plugin") or "unknown")
        actor = rendered_actor(entry.get("actor"), session_root=self.session_dir)
        source_artifact_kind = artifact_kind(entry.get("artifact_kind"))
        visibility = resolved_visibility_metadata(
            entry,
            provider=plugin,
            plugin=plugin,
            subcommand=str(entry.get("subcommand") or ""),
            locator=source,
        )
        snapshot = snapshot_by_digest.get(checksum)
        variant = render_variant(snapshot) if snapshot is not None else None
        self.source_states.setdefault(source, _SourceState()).record(
            checksum=checksum,
            locator=locator,
            plugin=plugin,
            actor=actor,
            source_artifact_kind=source_artifact_kind,
            variant=variant,
            visibility=visibility,
        )
        self.edge_plugins.setdefault((source, checksum), []).append(plugin)
        self.edge_actors.setdefault((source, checksum), set()).add(actor)
        self.content_details.setdefault(checksum, _ContentDetailState()).record(
            source=source,
            plugin=plugin,
            actor=actor,
        )

    def detail_for(self, checksum: str) -> _ContentDetailState | None:
        return self.content_details.get(checksum)

    def render_sources(self) -> list[LineageSource]:
        return [
            state.render(locator)
            for locator, state in sorted(self.source_states.items())
        ]

    def render_source_edges(self) -> list[LineageSourceEdge]:
        return [
            {
                "source": source,
                "checksum": checksum,
                "plugins": sorted(plugins),
                "actors": sorted(self.edge_actors.get((source, checksum), set())),
                "count": len(plugins),
            }
            for (source, checksum), plugins in sorted(self.edge_plugins.items())
        ]


def _visibility_metadata(*candidates: Mapping[str, object]) -> AnalyzeVisibility:
    metadata = best_visibility_metadata(*candidates)
    rendered: AnalyzeVisibility = {}
    level = metadata.get(_VISIBILITY_LEVEL_KEY)
    if isinstance(level, str) and level:
        rendered[_VISIBILITY_LEVEL_KEY] = level
    boundary = metadata.get(_VISIBILITY_BOUNDARY_KEY)
    if isinstance(boundary, str) and boundary:
        rendered[_VISIBILITY_BOUNDARY_KEY] = boundary
    confidence = metadata.get(_VISIBILITY_CONFIDENCE_KEY)
    if isinstance(confidence, str) and confidence:
        rendered[_VISIBILITY_CONFIDENCE_KEY] = confidence
    basis = metadata.get(_VISIBILITY_BASIS_KEY)
    if isinstance(basis, list):
        rendered[_VISIBILITY_BASIS_KEY] = [str(value) for value in basis if str(value)]
    return rendered


def _apply_visibility(
    target: LineageSource | LineageContent | LeadSourceSummary | LeadEdgeSummary,
    visibility: AnalyzeVisibility,
) -> None:
    level = visibility.get(_VISIBILITY_LEVEL_KEY)
    if level:
        target[_VISIBILITY_LEVEL_KEY] = level
    boundary = visibility.get(_VISIBILITY_BOUNDARY_KEY)
    if boundary:
        target[_VISIBILITY_BOUNDARY_KEY] = boundary
    confidence = visibility.get(_VISIBILITY_CONFIDENCE_KEY)
    if confidence:
        target[_VISIBILITY_CONFIDENCE_KEY] = confidence
    basis = visibility.get(_VISIBILITY_BASIS_KEY)
    if basis:
        target[_VISIBILITY_BASIS_KEY] = basis


def _lineage_content_item(
    snapshot: ContentSnapshot,
    *,
    detail: _ContentDetailState | None,
    name_counts: Counter[str],
    session_ref: str,
) -> LineageContent:
    metadata = snapshot.artifact.metadata
    rendered: LineageContent = {
        "checksum": snapshot.digest,
        "preferredName": snapshot_display_name(snapshot),
        "artifactKind": artifact_kind(metadata.get("artifact_kind")),
        "contentLocator": content_locator(snapshot.digest),
        "artifactLocator": snapshot_artifact_locator(snapshot),
        "followCommand": session_read_command(
            snapshot_artifact_locator(snapshot),
            session_ref=session_ref,
        ),
        "nameCollision": name_counts[snapshot_display_name(snapshot)] > 1,
        "nameCount": len(snapshot.aliases),
        "fetchCount": len(snapshot.events),
        "names": [alias.name for alias in snapshot.aliases],
        "firstFetchedAt": snapshot.events[0].timestamp if snapshot.events else "",
        "lastFetchedAt": snapshot.events[-1].timestamp if snapshot.events else "",
        "providers": sorted(detail.providers) if detail is not None else [],
        "actors": sorted(detail.actors) if detail is not None else [],
        "resourceHints": sorted(detail.resource_hints) if detail is not None else [],
    }
    _apply_visibility(
        rendered,
        _visibility_metadata(
            resolved_visibility_metadata(
                dict(metadata),
                provider=str(metadata.get("plugin") or ""),
                plugin=str(metadata.get("plugin") or ""),
                subcommand=str(metadata.get("subcommand") or ""),
                locator=str(snapshot_locator(snapshot)),
            )
        ),
    )
    return rendered


def _lead_source_summary(
    lead: MaterializedLeadSourceSummary,
    *,
    session_ref: str,
) -> LeadSourceSummary:
    locator = str(lead.get("locator") or "").strip()
    rendered: LeadSourceSummary = {
        "locator": locator,
        "provider": str(lead.get("provider") or ""),
        "materialized": bool(lead.get("materialized")),
        "occurrenceCount": int(lead.get("occurrenceCount") or 0),
        "artifactCount": int(lead.get("artifactCount") or 0),
        "artifactKind": str(lead.get("kind") or ""),
        "relationKinds": [str(value) for value in lead.get("relationKinds") or []],
        "followCommand": (
            follow_command(locator, session_ref=session_ref)
            if locator
            else str(lead.get("followCommand") or "")
        ),
    }
    _apply_visibility(rendered, _visibility_metadata(lead))
    return rendered


def _lead_edge_summary(edge: MaterializedLeadEdge) -> LeadEdgeSummary:
    rendered: LeadEdgeSummary = {
        "sourceChecksum": str(edge.get("sourceChecksum") or ""),
        "targetLocator": str(edge.get("targetLocator") or ""),
        "relation": str(edge.get("relation") or ""),
        "occurrenceCount": int(edge.get("occurrenceCount") or 0),
        "materialized": bool(edge.get("materialized")),
    }
    _apply_visibility(rendered, _visibility_metadata(edge))
    return rendered


def _payload_text(payload: LineagePayload, key: str) -> str:
    return str(payload.get(key) or "")


def _revision_edges(snapshots: list[ContentSnapshot]) -> list[LineageRevisionEdge]:
    tracks: dict[tuple[str, tuple[str, str]], list[_RevisionTrackEvent]] = {}
    for snapshot in snapshots:
        metadata = snapshot.artifact.metadata
        canonical = str(
            metadata.get("canonical_locator", "") or metadata.get("locator", "")
        ).strip()
        if not canonical:
            continue
        variant = render_variant(snapshot)
        for event in snapshot.events:
            tracks.setdefault((canonical, variant), []).append(
                _RevisionTrackEvent(
                    timestamp=event.timestamp,
                    digest=snapshot.digest,
                    preferred_name=str(
                        metadata.get("preferred_name", "") or event.alias_name
                    ),
                    plugin=str(metadata.get("plugin", "") or "unknown"),
                    actor=rendered_actor(
                        metadata.get("actor"),
                        session_root=snapshot.layout.artifact_dir.parent.parent,
                    ),
                    rendering=render_variant_label(variant),
                )
            )
    edges: list[LineageRevisionEdge] = []
    for (locator, _variant), items in sorted(tracks.items()):
        prior_item: _RevisionTrackEvent | None = None
        for item in sorted(
            items, key=lambda current: (current.timestamp, current.digest)
        ):
            if prior_item is None:
                prior_item = item
                continue
            if item.digest == prior_item.digest:
                prior_item = item
                continue
            edges.append(
                {
                    "locator": locator,
                    "preferredName": item.preferred_name or prior_item.preferred_name,
                    "from": prior_item.digest,
                    "to": item.digest,
                    "fromTimestamp": prior_item.timestamp,
                    "toTimestamp": item.timestamp,
                    "plugin": item.plugin or prior_item.plugin,
                    "actor": item.actor or prior_item.actor,
                    "rendering": item.rendering or prior_item.rendering,
                }
            )
            prior_item = item
    return edges


def lineage_payload(dirs, *, session_ref: str = "") -> LineagePayload:
    snapshots = scan_content_snapshots(
        dirs.content_dir,
        session_dir=dirs.session_dir,
    )
    snapshot_by_digest = {snapshot.digest: snapshot for snapshot in snapshots}
    entries = [dict(entry) for entry in manifest_entries(dirs)]
    build_state = _LineageBuildState(session_dir=dirs.session_dir)
    for entry in entries:
        build_state.record_manifest_entry(entry, snapshot_by_digest=snapshot_by_digest)

    name_counts = Counter(snapshot_display_name(snapshot) for snapshot in snapshots)
    content = [
        _lineage_content_item(
            snapshot,
            detail=build_state.detail_for(snapshot.digest),
            name_counts=name_counts,
            session_ref=session_ref,
        )
        for snapshot in snapshots
    ]
    sources = build_state.render_sources()
    source_edges = build_state.render_source_edges()
    revision_edges = _revision_edges(snapshots)
    lead_edges = build_lead_edges(
        snapshots,
        entries,
        classify_kind=lead_kind,
    )
    lead_sources = [
        _lead_source_summary(lead, session_ref=session_ref)
        for lead in aggregate_lead_sources(lead_edges)
    ]
    rendered_lead_edges = [_lead_edge_summary(edge) for edge in lead_edges]
    collisions = [
        str(source.get("locator") or "")
        for source in sources
        if bool(source.get("collision"))
    ]
    duplicate_materializations = [
        str(source.get("locator") or "")
        for source in sources
        if bool(source.get("duplicateMaterialization"))
    ]
    variants = [
        str(source.get("locator") or "")
        for source in sources
        if bool(source.get("variant"))
    ]
    name_collisions = sorted(
        name for name, count in name_counts.items() if count > 1 and name
    )
    materialized_lead_count = sum(
        1 for source in lead_sources if bool(source.get("materialized"))
    )
    empty = (
        not sources
        and not content
        and not source_edges
        and not revision_edges
        and not rendered_lead_edges
    )
    discovery_count = sum(
        1 for item in content if item.get("artifactKind") == "discovery"
    )
    evidence_count = sum(
        1 for item in content if item.get("artifactKind") == "evidence"
    )
    return {
        "sessionDir": str(dirs.session_dir),
        "contentDir": str(dirs.content_dir),
        "manifestPath": str(dirs.content_dir / "manifest.jsonl"),
        "manifestEntryCount": len(entries),
        "contentCount": len(content),
        "sourceCount": len(sources),
        "sourceEdgeCount": len(source_edges),
        "revisionEdgeCount": len(revision_edges),
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "collisionCount": len(collisions),
        "collisions": collisions,
        "duplicateMaterializationCount": len(duplicate_materializations),
        "duplicateMaterializations": duplicate_materializations,
        "variantCount": len(variants),
        "variants": variants,
        "nameCollisionCount": len(name_collisions),
        "nameCollisions": name_collisions,
        "leadSourceCount": len(lead_sources),
        "materializedLeadSourceCount": materialized_lead_count,
        "unmaterializedLeadSourceCount": len(lead_sources) - materialized_lead_count,
        "leadEdgeCount": len(lead_edges),
        "empty": empty,
        "nextStep": topology_next_step(
            discovery_count=discovery_count,
            evidence_count=evidence_count,
        ),
        "sources": sources,
        "content": content,
        "sourceEdges": source_edges,
        "revisionEdges": revision_edges,
        "leadSources": lead_sources,
        "leadEdges": rendered_lead_edges,
    }


def _empty_lineage_focus_payload(
    payload: LineagePayload,
    *,
    query: str,
    next_step: str,
) -> LineageFocusPayload:
    return {
        "sessionDir": _payload_text(payload, "sessionDir"),
        "contentDir": _payload_text(payload, "contentDir"),
        "focus": query,
        "matched": False,
        "empty": True,
        "nextStep": next_step,
        "root": {},
        "neighbors": [],
        "sources": [],
        "content": [],
        "sourceEdges": [],
        "revisionEdges": [],
        "leadSources": [],
        "leadEdges": [],
        "discoveryArtifactCount": 0,
        "evidenceArtifactCount": 0,
        "anchors": [],
        "matchedCount": 0,
    }


def lineage_focus_payload(
    payload: LineagePayload,
    *,
    focus: str,
    limit: int,
    scan_payload: AnalyzeScanPayload | None = None,
) -> LineageFocusPayload:
    query = focus.strip()
    if not query:
        return _empty_lineage_focus_payload(
            payload,
            query="",
            next_step="Provide a focus keyword, locator, artifact name, or checksum prefix.",
        )
    no_match_step = (
        f"No analyzed lineage anchor or projected artifact matched `{query}`. Try a canonical locator, "
        "artifact name, checksum prefix, or a tighter target from session scan, leads, or manifest."
    )
    selection = select_lineage_focus(
        payload,
        query=query,
        limit=limit,
        scan_payload=scan_payload,
    )
    if selection is None:
        return _empty_lineage_focus_payload(
            payload,
            query=query,
            next_step=no_match_step,
        )
    return {
        "sessionDir": _payload_text(payload, "sessionDir"),
        "contentDir": _payload_text(payload, "contentDir"),
        "manifestPath": _payload_text(payload, "manifestPath"),
        "focus": query,
        "matched": True,
        "empty": False,
        "nextStep": "",
        "root": selection.root,
        "anchors": selection.anchors,
        "matchedCount": len(selection.anchors) + 1,
        "neighbors": selection.neighbors,
        "sources": selection.sources,
        "content": selection.content,
        "sourceEdges": selection.source_edges,
        "revisionEdges": selection.revision_edges,
        "leadSources": selection.lead_sources,
        "leadEdges": selection.lead_edges,
        "discoveryArtifactCount": selection.discovery_artifact_count,
        "evidenceArtifactCount": selection.evidence_artifact_count,
        "contentCount": len(selection.content),
        "sourceCount": len(selection.sources),
        "sourceEdgeCount": len(selection.source_edges),
        "revisionEdgeCount": len(selection.revision_edges),
        "leadSourceCount": len(selection.lead_sources),
        "leadEdgeCount": len(selection.lead_edges),
        "collisionCount": 0,
        "collisions": [],
        "duplicateMaterializationCount": 0,
        "duplicateMaterializations": [],
        "variantCount": 0,
        "variants": [],
    }
