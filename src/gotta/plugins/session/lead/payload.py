"""Lead payload synthesis for `gotta session`."""

from __future__ import annotations

from typing import cast

from gotta.content.backend import scan_content_snapshots
from gotta.content.model import ResolvedDirs
from gotta.content.path import content_locator
from gotta.lead.model import LeadEdge, LeadResolution, LeadSourceSummary
from gotta.lead.rank import edge_best_first_sort_key
from gotta.lead.resolve import resolve_lead_resolution
from gotta.lead.snapshot import (
    snapshot_artifact_locator,
    snapshot_display_name,
    snapshot_last_fetched_at,
    snapshot_locator,
)

from ..core import (
    LEADS_BEST_OVERALL_LIMIT,
    LEADS_PROVIDER_HIGHLIGHT_LIMIT,
    artifact_kind,
    compile_filter_pattern,
    follow_command,
    lead_kind,
    match_any,
    match_filter_text,
    no_leads_next_step,
    paginate_items,
    resolved_visibility_metadata,
    top_count_records,
    topology_next_step,
)
from ..manifest.record import manifest_entries
from .model import LeadArtifact, LeadsPayload, ProviderCountRecord, RelationCountRecord


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _provider_count_records(values: list[str]) -> list[ProviderCountRecord]:
    rendered: list[ProviderCountRecord] = []
    for record in top_count_records(values, key="provider"):
        count = record.get("count")
        provider_record: ProviderCountRecord = {
            "provider": str(record.get("provider") or ""),
            "count": count if isinstance(count, int) else 0,
        }
        rendered.append(provider_record)
    return rendered


def _relation_count_records(values: list[str]) -> list[RelationCountRecord]:
    rendered: list[RelationCountRecord] = []
    for record in top_count_records(values, key="relation"):
        count = record.get("count")
        relation_record: RelationCountRecord = {
            "relation": str(record.get("relation") or ""),
            "count": count if isinstance(count, int) else 0,
        }
        rendered.append(relation_record)
    return rendered


def _paging_int(paging: dict[str, object], key: str) -> int:
    value = paging.get(key)
    return value if isinstance(value, int) else 0


def _paging_next_offset(paging: dict[str, object]) -> int | None:
    value = paging.get("nextOffset")
    return value if isinstance(value, int) or value is None else None


def _paging_bool(paging: dict[str, object], key: str) -> bool:
    value = paging.get(key)
    return value if isinstance(value, bool) else False


def _resolved_lead_source(
    lead: LeadSourceSummary,
    *,
    session_ref: str,
) -> LeadSourceSummary:
    rendered = cast(LeadSourceSummary, dict(lead))
    locator = str(rendered.get("locator") or "").strip()
    if locator:
        rendered["followCommand"] = follow_command(locator, session_ref=session_ref)
    return rendered


def _resolved_lead_sources(
    lead_sources: list[LeadSourceSummary],
    *,
    session_ref: str,
) -> list[LeadSourceSummary]:
    return [
        _resolved_lead_source(lead, session_ref=session_ref) for lead in lead_sources
    ]


def _filtered_lead_sources(
    lead_sources: list[LeadSourceSummary],
    *,
    filter_query: str,
) -> tuple[list[LeadSourceSummary], str]:
    filter_text = match_filter_text(filter_query)
    filter_pattern = compile_filter_pattern(filter_text)
    if filter_pattern is None:
        return lead_sources, filter_text
    filtered = [
        lead
        for lead in lead_sources
        if match_any(
            filter_pattern,
            lead.get("locator"),
            lead.get("followCommand"),
            lead.get("provider"),
            lead.get("kind"),
            lead.get("exampleRaw"),
            lead.get("contexts"),
            lead.get("relationKinds"),
            lead.get("artifactLocators"),
            lead.get("contentLocators"),
            lead.get("searchOrigins"),
        )
    ]
    return filtered, filter_text


def _resolved_lead_edge(edge: LeadEdge, *, session_ref: str) -> LeadEdge:
    rendered = cast(LeadEdge, dict(edge))
    target_locator = str(rendered.get("targetLocator") or "").strip()
    if target_locator:
        rendered["followCommand"] = follow_command(
            target_locator,
            session_ref=session_ref,
        )
    return rendered


def _selected_edges_by_checksum(
    resolution: LeadResolution,
    *,
    lead_sources: list[LeadSourceSummary],
    session_ref: str,
) -> dict[str, list[LeadEdge]]:
    selected_lead_locators = {
        str(lead.get("locator") or "").strip()
        for lead in lead_sources
        if str(lead.get("locator") or "").strip()
    }
    selected: dict[str, list[LeadEdge]] = {}
    for edge in resolution.edge_records:
        target_locator = str(edge.get("targetLocator") or "").strip()
        if selected_lead_locators and target_locator not in selected_lead_locators:
            continue
        selected.setdefault(edge["sourceChecksum"], []).append(
            _resolved_lead_edge(edge, session_ref=session_ref)
        )
    return selected


def _lead_artifacts(
    resolution: LeadResolution,
    *,
    edges_by_checksum: dict[str, list[LeadEdge]],
    limit: int,
) -> list[LeadArtifact]:
    artifacts: list[LeadArtifact] = []
    for snapshot in resolution.selected_snapshots:
        metadata = snapshot.artifact.metadata
        edges = sorted(
            edges_by_checksum.get(snapshot.digest, []),
            key=edge_best_first_sort_key,
        )
        artifact_visibility = resolved_visibility_metadata(
            dict(metadata),
            provider=str(metadata.get("plugin") or ""),
            plugin=str(metadata.get("plugin") or ""),
            subcommand=str(metadata.get("subcommand") or ""),
            locator=str(snapshot_locator(snapshot)),
        )
        artifact: LeadArtifact = {
            "checksum": snapshot.digest,
            "preferredName": snapshot_display_name(snapshot),
            "artifactKind": artifact_kind(metadata.get("artifact_kind")),
            "sourceLocator": snapshot_locator(snapshot),
            "artifactLocator": snapshot_artifact_locator(snapshot),
            "contentLocator": content_locator(snapshot.digest),
            "lastFetchedAt": snapshot_last_fetched_at(snapshot),
            "leadCount": len(edges),
            "leads": edges[: max(limit, 0)],
        }
        visibility_level = artifact_visibility.get("visibility_level")
        if isinstance(visibility_level, str) and visibility_level:
            artifact["visibility_level"] = visibility_level
        visibility_boundary = artifact_visibility.get("visibility_boundary")
        if isinstance(visibility_boundary, str) and visibility_boundary:
            artifact["visibility_boundary"] = visibility_boundary
        visibility_confidence = artifact_visibility.get("visibility_confidence")
        if isinstance(visibility_confidence, str) and visibility_confidence:
            artifact["visibility_confidence"] = visibility_confidence
        visibility_basis = artifact_visibility.get("visibility_basis")
        if isinstance(visibility_basis, list) and visibility_basis:
            artifact["visibility_basis"] = [
                str(value) for value in visibility_basis if str(value)
            ]
        artifacts.append(artifact)
    return artifacts


def _provider_highlights(
    lead_sources: list[LeadSourceSummary],
) -> tuple[list[LeadSourceSummary], list[LeadSourceSummary]]:
    best_overall = lead_sources[:LEADS_BEST_OVERALL_LIMIT]
    best_locators = {str(item.get("locator") or "").strip() for item in best_overall}
    provider_highlights: list[LeadSourceSummary] = []
    highlighted_providers = {
        str(item.get("provider") or "").strip()
        for item in best_overall
        if str(item.get("provider") or "").strip()
    }
    for lead in lead_sources:
        provider = str(lead.get("provider") or "").strip()
        locator = str(lead.get("locator") or "").strip()
        if (
            not provider
            or not locator
            or locator in best_locators
            or provider in highlighted_providers
        ):
            continue
        provider_highlights.append(lead)
        highlighted_providers.add(provider)
        if len(provider_highlights) >= LEADS_PROVIDER_HIGHLIGHT_LIMIT:
            break
    return best_overall, provider_highlights


def _next_step(
    *,
    artifacts: list[LeadArtifact],
    lead_sources: list[LeadSourceSummary],
    filter_text: str,
    resolution: LeadResolution,
    discovery_count: int,
    evidence_count: int,
) -> str:
    empty = not artifacts and not lead_sources
    return (
        topology_next_step(
            discovery_count=discovery_count, evidence_count=evidence_count
        )
        if empty and not filter_text and not resolution.selected_snapshots
        else no_leads_next_step(has_artifacts=bool(resolution.selected_snapshots))
        if not lead_sources and not filter_text and resolution.selected_snapshots
        else (
            "No leads matched the current filter. Use `gotta session scan <query>` when you need corpus-wide search instead of field-level lead filtering."
            if not lead_sources and filter_text
            else ""
        )
    )


def leads_payload(
    dirs: ResolvedDirs,
    *,
    target: str = "",
    filter_query: str = "",
    limit: int = 100,
    offset: int = 0,
    include_all: bool = False,
    session_ref: str = "",
) -> LeadsPayload:
    snapshots = scan_content_snapshots(
        dirs.content_dir,
        session_dir=dirs.session_dir,
    )
    session_manifest = manifest_entries(dirs)
    resolution = resolve_lead_resolution(
        target,
        snapshots,
        session_manifest,
        classify_kind=lead_kind,
    )
    lead_sources, filter_text = _filtered_lead_sources(
        _resolved_lead_sources(resolution.lead_sources, session_ref=session_ref),
        filter_query=filter_query,
    )
    paged_sources, paging = paginate_items(
        lead_sources,
        limit=limit,
        offset=offset,
        include_all=include_all,
    )
    selected_edges_by_checksum = _selected_edges_by_checksum(
        resolution,
        lead_sources=lead_sources,
        session_ref=session_ref,
    )
    artifacts = _lead_artifacts(
        resolution,
        edges_by_checksum=selected_edges_by_checksum,
        limit=limit,
    )
    if filter_text:
        artifacts = [artifact for artifact in artifacts if artifact["leads"]]
    best_overall, provider_highlights = _provider_highlights(lead_sources)
    materialized_count = sum(
        1 for source in lead_sources if bool(source["materialized"])
    )
    discovery_count = sum(
        1 for item in artifacts if item.get("artifactKind") == "discovery"
    )
    evidence_count = sum(
        1 for item in artifacts if item.get("artifactKind") == "evidence"
    )
    empty = not artifacts and not lead_sources
    top_providers = _provider_count_records(
        [str(source.get("provider") or "").strip() for source in lead_sources],
    )
    top_relations = _relation_count_records(
        [
            str(relation).strip()
            for source in lead_sources
            for relation in _string_items(source.get("relationKinds"))
            if str(relation).strip()
        ],
    )
    next_step = _next_step(
        artifacts=artifacts,
        lead_sources=lead_sources,
        filter_text=filter_text,
        resolution=resolution,
        discovery_count=discovery_count,
        evidence_count=evidence_count,
    )
    payload_offset: int = _paging_int(paging, "offset")
    payload_total: int = _paging_int(paging, "totalCount")
    payload_shown: int = _paging_int(paging, "shownCount")
    payload_next: int | None = _paging_next_offset(paging)
    payload_truncated: bool = _paging_bool(paging, "truncated")
    payload: LeadsPayload = {
        "sessionDir": str(dirs.session_dir),
        "contentDir": str(dirs.content_dir),
        "target": target.strip(),
        "filter": filter_text,
        "limit": max(limit, 0),
        "artifactCount": len(artifacts),
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "topProviders": top_providers,
        "topRelations": top_relations,
        "leadCount": len(lead_sources),
        "totalCount": payload_total,
        "shownCount": payload_shown,
        "offset": payload_offset,
        "nextOffset": payload_next,
        "truncated": payload_truncated,
        "materializedLeadCount": materialized_count,
        "unmaterializedLeadCount": len(lead_sources) - materialized_count,
        "leadSources": paged_sources,
        "bestOverall": best_overall,
        "providerHighlights": provider_highlights,
        "artifacts": artifacts,
        "empty": empty,
        "nextStep": next_step,
    }
    return payload
