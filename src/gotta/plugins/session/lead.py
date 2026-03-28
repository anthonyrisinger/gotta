"""Lead surface for `gotta session`."""

from __future__ import annotations

import argparse
import json

from gotta.content import content_locator, scan_content_store
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

from .core import (
    LEADS_BEST_OVERALL_LIMIT,
    LEADS_PROVIDER_HIGHLIGHT_LIMIT,
    append_count_section,
    artifact_kind,
    compile_filter_pattern,
    filter_suffix,
    follow_command,
    lead_kind,
    lead_signal_labels,
    match_any,
    match_filter_text,
    no_leads_next_step,
    paging_summary_line,
    paginate_items,
    render_search_origins,
    resolved_visibility_metadata,
    stored_target_locators,
    top_count_records,
    topology_next_step,
    visibility_summary,
)
from .manifest import manifest_entries
from .parse import explicit_session_ref, require_started_session, session_dirs_for_read

_artifact_kind = artifact_kind
_append_count_section = append_count_section
_explicit_session_ref = explicit_session_ref
_manifest_entries = manifest_entries
_compile_filter_pattern = compile_filter_pattern
_filter_suffix = filter_suffix
_follow_command = follow_command
_lead_kind = lead_kind
_lead_signal_labels = lead_signal_labels
_match_any = match_any
_match_filter_text = match_filter_text
_no_leads_next_step = no_leads_next_step
_paging_summary_line = paging_summary_line
_paginate_items = paginate_items
_render_search_origins = render_search_origins
_resolved_visibility_metadata = resolved_visibility_metadata
_stored_target_locators = stored_target_locators
_top_count_records = top_count_records
_topology_next_step = topology_next_step
_visibility_summary = visibility_summary
_require_started_session = require_started_session
_session_dirs_for_read = session_dirs_for_read


def _leads_payload(
    dirs,
    *,
    target: str = "",
    filter_query: str = "",
    limit: int = 100,
    offset: int = 0,
    include_all: bool = False,
    session_ref: str = "",
) -> dict[str, object]:
    snapshots = scan_content_store(dirs.content_dir)
    manifest_entries = _manifest_entries(dirs)
    selected = resolve_lead_snapshots(target, snapshots, manifest_entries)
    selected_digests = {snapshot.digest for snapshot in selected}
    all_edge_records = build_lead_edge_records(
        snapshots,
        manifest_entries,
        classify_kind=_lead_kind,
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
            lead["followCommand"] = _follow_command(locator, session_ref=session_ref)
    filter_text = _match_filter_text(filter_query)
    filter_pattern = _compile_filter_pattern(filter_text)
    if filter_pattern is not None:
        lead_sources = [
            lead
            for lead in lead_sources
            if _match_any(
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
    paged_sources, paging = _paginate_items(
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
    artifacts = []
    if lead_sources:
        for snapshot in selected:
            edges = sorted(
                selected_edges_by_checksum.get(snapshot.digest, []),
                key=edge_best_first_sort_key,
            )
            if filter_text and not edges:
                continue
            for edge in edges:
                target_locator = str(edge.get("targetLocator") or "").strip()
                if target_locator:
                    edge["followCommand"] = _follow_command(
                        target_locator,
                        session_ref=session_ref,
                    )
            artifacts.append(
                {
                    "checksum": snapshot.digest,
                    "preferredName": snapshot_display_name(snapshot),
                    "artifactKind": _artifact_kind(
                        snapshot.metadata.get("artifact_kind")
                    ),
                    "sourceLocator": snapshot_locator(snapshot),
                    "artifactLocator": snapshot_artifact_locator(snapshot),
                    "contentLocator": content_locator(snapshot.digest),
                    "lastFetchedAt": snapshot_last_fetched_at(snapshot),
                    "leadCount": len(edges),
                    "leads": edges[: max(limit, 0)],
                    **_resolved_visibility_metadata(
                        dict(snapshot.metadata),
                        provider=str(snapshot.metadata.get("plugin") or ""),
                        plugin=str(snapshot.metadata.get("plugin") or ""),
                        subcommand=str(snapshot.metadata.get("subcommand") or ""),
                        locator=str(snapshot_locator(snapshot)),
                    ),
                }
            )
    best_overall = lead_sources[:LEADS_BEST_OVERALL_LIMIT]
    best_locators = {str(item.get("locator") or "").strip() for item in best_overall}
    provider_highlights: list[dict[str, object]] = []
    highlighted_providers: set[str] = {
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
    top_providers = _top_count_records(
        [str(source.get("provider") or "").strip() for source in lead_sources],
        key="provider",
    )
    top_relations = _top_count_records(
        [
            str(relation).strip()
            for source in lead_sources
            for relation in list(source.get("relationKinds") or [])
            if str(relation).strip()
        ],
        key="relation",
    )
    next_step = (
        _topology_next_step(
            discovery_count=discovery_count, evidence_count=evidence_count
        )
        if empty and not filter_text and not selected
        else _no_leads_next_step(has_artifacts=bool(selected))
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


def cmd_leads(args: argparse.Namespace) -> int:
    dirs = _session_dirs_for_read(args)
    _require_started_session(dirs)
    session_ref = _explicit_session_ref(args)
    payload = _leads_payload(
        dirs,
        target=args.target or "",
        filter_query=str(getattr(args, "filter", "") or ""),
        session_ref=session_ref,
        limit=max(args.limit, 0),
        offset=max(args.offset, 0),
        include_all=bool(args.all),
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"session: {payload['sessionDir']}")
    print(f"target: {payload['target'] or '(session-wide)'}")
    print(
        "artifacts: "
        f"{payload['artifactCount']} "
        f"(discovery {payload['discoveryArtifactCount']}, "
        f"evidence {payload['evidenceArtifactCount']})"
    )
    print(
        "leads: "
        f"{payload['leadCount']} total (showing {payload['shownCount']}; "
        f"materialized {payload['materializedLeadCount']}, "
        f"unmaterialized {payload['unmaterializedLeadCount']})"
        f"{_filter_suffix(payload.get('filter'))}"
    )
    print(
        _paging_summary_line(
            label="page",
            total_count=int(payload["totalCount"]),
            shown_count=int(payload["shownCount"]),
            offset=int(payload["offset"]),
            next_offset=(
                int(payload["nextOffset"])
                if payload.get("nextOffset") is not None
                else None
            ),
        )
    )
    if int(payload["shownCount"]) == 0 and int(payload["totalCount"]) > 0:
        print("page: no results in this page window")
    top_providers_lines: list[str] = []
    _append_count_section(
        top_providers_lines,
        heading="top providers",
        records=list(payload.get("topProviders") or []),
        key="provider",
    )
    if top_providers_lines:
        print("\n".join(top_providers_lines))
    top_relation_lines: list[str] = []
    _append_count_section(
        top_relation_lines,
        heading="top relations",
        records=list(payload.get("topRelations") or []),
        key="relation",
    )
    if top_relation_lines:
        print("\n".join(top_relation_lines))
    if payload["nextStep"]:
        print(f"next: {payload['nextStep']}")
    if payload["bestOverall"]:
        print("best leads:")
        for lead in payload["bestOverall"]:
            relation = ", ".join(
                str(value) for value in lead.get("relationKinds") or [] if str(value)
            )
            print(
                f"  - [{'; '.join(_lead_signal_labels(lead, aggregated=True))}] "
                f"{lead['locator']} ({lead['provider']}, {relation or 'lead'})"
            )
            visibility = _visibility_summary(lead)
            if visibility:
                print(f"    visibility: {visibility}")
            print(f"    follow: `{lead['followCommand']}`")
            stored_targets = _stored_target_locators(lead)
            if stored_targets:
                print(
                    "    stored_target: "
                    + ", ".join(f"`{value}`" for value in stored_targets)
                )
            search_origins = _render_search_origins(lead)
            if search_origins:
                print(f"    search_origin: {search_origins}")
            contexts = [
                str(value) for value in lead.get("contexts") or [] if str(value)
            ]
            if contexts:
                print(f"    context: {contexts[0]}")
    if payload["providerHighlights"]:
        print("provider highlights:")
        for lead in payload["providerHighlights"]:
            relation = ", ".join(
                str(value) for value in lead.get("relationKinds") or [] if str(value)
            )
            print(
                f"  - [{'; '.join(_lead_signal_labels(lead, aggregated=True))}] "
                f"{lead['locator']} ({lead['provider']}, {relation or 'lead'})"
            )
            print(f"    follow: `{lead['followCommand']}`")
    if payload["artifacts"] and (payload["target"] or payload["artifactCount"] == 1):
        print("source context:")
        for artifact in payload["artifacts"]:
            print(f"- {artifact['preferredName']} ({str(artifact['checksum'])[:12]})")
            print(f"  source: `{artifact['sourceLocator'] or 'unknown'}`")
            if artifact.get("artifactKind"):
                print(f"  artifact_kind: {artifact['artifactKind']}")
            artifact_visibility = _visibility_summary(artifact)
            if artifact_visibility:
                print(f"  visibility: {artifact_visibility}")
            print(
                f"  stored: `{artifact['artifactLocator']}`, `{artifact['contentLocator']}`"
            )
            if artifact["lastFetchedAt"]:
                print(f"  fetched: {artifact['lastFetchedAt']}")
            if not artifact["leads"]:
                print("  leads: none")
                continue
            for lead in artifact["leads"]:
                print(
                    f"  - [{'; '.join(_lead_signal_labels(lead, aggregated=False))}] "
                    f"{lead['targetLocator']} ({lead['provider']}, {lead['relation']})"
                )
                visibility = _visibility_summary(lead)
                if visibility:
                    print(f"    visibility: {visibility}")
                print(f"    follow: `{lead['followCommand']}`")
                stored_targets = _stored_target_locators(lead)
                if stored_targets:
                    print(
                        "    stored_target: "
                        + ", ".join(f"`{value}`" for value in stored_targets)
                    )
                if bool(lead.get("sourceSearchLike")):
                    provider = str(lead.get("sourceProvider") or "unknown")
                    subcommand = str(lead.get("sourceSubcommand") or "search")
                    rank = int(lead.get("sourceRank") or 0)
                    origin = f"{provider}/{subcommand}"
                    if rank > 0:
                        origin += f" #{rank}"
                    print(f"    source_origin: {origin}")
                contexts = [
                    str(value) for value in lead.get("contexts") or [] if str(value)
                ]
                if contexts:
                    print(f"    context: {contexts[0]}")
    return 0


leads_payload = _leads_payload
