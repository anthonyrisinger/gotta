"""Lead payload synthesis for `gotta session`."""

from __future__ import annotations

from gotta.content.filesystem import FileSystemLedgerStore
from gotta.content.model import ResolvedDirs
from gotta.content.path import content_locator
from gotta.lead.aggregate import aggregate_lead_sources
from gotta.lead.edge import build_lead_edge_records
from gotta.lead.rank import edge_best_first_sort_key
from gotta.lead.resolve import resolve_lead_snapshots
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


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def leads_payload(
    dirs: ResolvedDirs,
    *,
    target: str = "",
    filter_query: str = "",
    limit: int = 100,
    offset: int = 0,
    include_all: bool = False,
    session_ref: str = "",
) -> dict[str, object]:
    snapshots = FileSystemLedgerStore.for_content_dir(dirs.content_dir).scan_artifacts()
    session_manifest = manifest_entries(dirs)
    selected = resolve_lead_snapshots(target, snapshots, session_manifest)
    selected_digests = {snapshot.digest for snapshot in selected}
    all_edge_records = build_lead_edge_records(
        snapshots,
        session_manifest,
        classify_kind=lead_kind,
    )
    edge_records = [
        edge
        for edge in all_edge_records
        if str(edge.get("sourceChecksum", "")) in selected_digests
    ]
    lead_sources = aggregate_lead_sources(edge_records)
    for lead in lead_sources:
        locator = str(lead.get("locator") or "").strip()
        if locator:
            lead["followCommand"] = follow_command(locator, session_ref=session_ref)
    filter_text = match_filter_text(filter_query)
    filter_pattern = compile_filter_pattern(filter_text)
    if filter_pattern is not None:
        lead_sources = [
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
    paged_sources, paging = paginate_items(
        lead_sources,
        limit=limit,
        offset=offset,
        include_all=include_all,
    )
    selected_lead_locators = {
        str(lead.get("locator") or "").strip()
        for lead in lead_sources
        if str(lead.get("locator") or "").strip()
    }
    selected_edges_by_checksum: dict[str, list[dict[str, object]]] = {}
    for edge in edge_records:
        if (
            selected_lead_locators
            and str(edge.get("targetLocator") or "").strip()
            not in selected_lead_locators
        ):
            continue
        selected_edges_by_checksum.setdefault(str(edge["sourceChecksum"]), []).append(
            edge
        )
    artifacts: list[dict[str, object]] = []
    if lead_sources:
        for snapshot in selected:
            metadata = snapshot.artifact.metadata
            edges = sorted(
                selected_edges_by_checksum.get(snapshot.digest, []),
                key=edge_best_first_sort_key,
            )
            if filter_text and not edges:
                continue
            for edge in edges:
                target_locator = str(edge.get("targetLocator") or "").strip()
                if target_locator:
                    edge["followCommand"] = follow_command(
                        target_locator,
                        session_ref=session_ref,
                    )
            artifacts.append(
                {
                    "checksum": snapshot.digest,
                    "preferredName": snapshot_display_name(snapshot),
                    "artifactKind": artifact_kind(metadata.get("artifact_kind")),
                    "sourceLocator": snapshot_locator(snapshot),
                    "artifactLocator": snapshot_artifact_locator(snapshot),
                    "contentLocator": content_locator(snapshot.digest),
                    "lastFetchedAt": snapshot_last_fetched_at(snapshot),
                    "leadCount": len(edges),
                    "leads": edges[: max(limit, 0)],
                    **resolved_visibility_metadata(
                        dict(metadata),
                        provider=str(metadata.get("plugin") or ""),
                        plugin=str(metadata.get("plugin") or ""),
                        subcommand=str(metadata.get("subcommand") or ""),
                        locator=str(snapshot_locator(snapshot)),
                    ),
                }
            )
    best_overall = lead_sources[:LEADS_BEST_OVERALL_LIMIT]
    best_locators = {str(item.get("locator") or "").strip() for item in best_overall}
    provider_highlights: list[dict[str, object]] = []
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
    top_providers = top_count_records(
        [str(source.get("provider") or "").strip() for source in lead_sources],
        key="provider",
    )
    top_relations = top_count_records(
        [
            str(relation).strip()
            for source in lead_sources
            for relation in _string_items(source.get("relationKinds"))
            if str(relation).strip()
        ],
        key="relation",
    )
    next_step = (
        topology_next_step(
            discovery_count=discovery_count, evidence_count=evidence_count
        )
        if empty and not filter_text and not selected
        else no_leads_next_step(has_artifacts=bool(selected))
        if not lead_sources and not filter_text and selected
        else (
            "No leads matched the current filter. Use `gotta session scan <query>` when you need corpus-wide search instead of field-level lead filtering."
            if not lead_sources and filter_text
            else ""
        )
    )
    return {
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
        **paging,
        "materializedLeadCount": materialized_count,
        "unmaterializedLeadCount": len(lead_sources) - materialized_count,
        "leadSources": paged_sources,
        "bestOverall": best_overall,
        "providerHighlights": provider_highlights,
        "artifacts": artifacts,
        "empty": empty,
        "nextStep": next_step,
    }
