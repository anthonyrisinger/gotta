"""Analysis surfaces for `gotta session`."""

from __future__ import annotations

import argparse
from collections import Counter
import json

from gotta.source import best_visibility_metadata
from gotta.content.model import ContentSnapshot
from gotta.content.path import content_locator
from gotta.content.store import scan_content_store
from gotta.lead.aggregate import aggregate_lead_sources
from gotta.lead.edge import build_lead_edge_records
from gotta.lead.snapshot import (
    snapshot_artifact_locator,
    snapshot_display_name,
    snapshot_locator,
)

from .core import (
    ANALYZE_ANCHOR_PREVIEW_LIMIT,
    ANALYZE_FOCUS_MATCH_PREVIEW_LIMIT,
    ANALYZE_FOCUS_NEIGHBOR_PREVIEW_LIMIT,
    SUMMARY_BUCKET_LIMIT,
    analysis_mermaid_id,
    artifact_kind,
    empty_topology_next_step,
    follow_command,
    lead_kind,
    lead_signal_labels,
    mermaid_label,
    provider_name,
    query_label,
    rendered_actor,
    render_variant,
    render_variant_label,
    resolved_visibility_metadata,
    resource_label,
    session_read_command,
    topology_next_step,
    visibility_summary,
)
from .manifest import manifest_entries
from .parse import explicit_session_ref, require_started_session, session_dirs_for_read
from .scan import scan_payload

_analysis_mermaid_id = analysis_mermaid_id
_artifact_kind = artifact_kind
_empty_topology_next_step = empty_topology_next_step
_explicit_session_ref = explicit_session_ref
_follow_command = follow_command
_lead_kind = lead_kind
_lead_signal_labels = lead_signal_labels
_manifest_entries = manifest_entries
_mermaid_label = mermaid_label
_provider_name = provider_name
_query_label = query_label
_rendered_actor = rendered_actor
_render_variant = render_variant
_render_variant_label = render_variant_label
_resolved_visibility_metadata = resolved_visibility_metadata
_resource_label = resource_label
_require_started_session = require_started_session
_scan_payload = scan_payload
_session_dirs_for_read = session_dirs_for_read
_session_read_command = session_read_command
_topology_next_step = topology_next_step
_visibility_summary = visibility_summary


def _revision_edges(snapshots: list[ContentSnapshot]) -> list[dict[str, str]]:
    tracks: dict[tuple[str, tuple[str, str]], list[dict[str, str]]] = {}
    for snapshot in snapshots:
        canonical = str(
            snapshot.metadata.get("canonical_locator", "")
            or snapshot.metadata.get("locator", "")
        ).strip()
        if not canonical:
            continue
        variant = _render_variant(snapshot)
        for event in snapshot.events:
            tracks.setdefault((canonical, variant), []).append(
                {
                    "timestamp": event.timestamp,
                    "digest": snapshot.digest,
                    "preferred_name": str(
                        snapshot.metadata.get("preferred_name", "") or event.link_name
                    ),
                    "plugin": str(snapshot.metadata.get("plugin", "") or "unknown"),
                    "actor": _rendered_actor(
                        snapshot.metadata.get("actor"),
                        session_root=snapshot.content_dir.parent.parent,
                    ),
                    "rendering": _render_variant_label(variant),
                }
            )
    edges: list[dict[str, str]] = []
    for (locator, _variant), items in sorted(tracks.items()):
        prior_item: dict[str, str] | None = None
        for item in sorted(
            items, key=lambda current: (current["timestamp"], current["digest"])
        ):
            if prior_item is None:
                prior_item = item
                continue
            if item["digest"] == prior_item["digest"]:
                prior_item = item
                continue
            edges.append(
                {
                    "locator": locator,
                    "preferredName": item["preferred_name"]
                    or prior_item["preferred_name"],
                    "from": prior_item["digest"],
                    "to": item["digest"],
                    "fromTimestamp": prior_item["timestamp"],
                    "toTimestamp": item["timestamp"],
                    "plugin": item["plugin"] or prior_item["plugin"],
                    "actor": item["actor"] or prior_item["actor"],
                    "rendering": item["rendering"] or prior_item["rendering"],
                }
            )
            prior_item = item
    return edges


def _analysis_payload(dirs, *, session_ref: str = "") -> dict[str, object]:
    snapshots = scan_content_store(dirs.content_dir)
    snapshot_by_digest = {snapshot.digest: snapshot for snapshot in snapshots}
    manifest_entries = _manifest_entries(dirs)
    source_map: dict[str, dict[str, object]] = {}
    edge_plugins: dict[tuple[str, str], list[str]] = {}
    edge_actors: dict[tuple[str, str], set[str]] = {}
    content_details: dict[str, dict[str, set[str]]] = {}

    for entry in manifest_entries:
        source = entry.get("canonical_locator") or entry.get("locator") or "unknown"
        checksum = entry.get("checksum") or ""
        if not checksum:
            continue
        source_state = source_map.setdefault(
            source,
            {
                "content": set(),
                "locators": set(),
                "plugins": set(),
                "actors": set(),
                "artifact_kinds": set(),
                "entries": 0,
                "variants": set(),
                "visibility": {},
            },
        )
        source_state["content"].add(checksum)
        locator = entry.get("locator") or source
        source_state["locators"].add(locator)
        plugin = entry.get("plugin") or "unknown"
        actor = _rendered_actor(entry.get("actor"), session_root=dirs.session_dir)
        source_state["plugins"].add(plugin)
        source_state["actors"].add(actor)
        kind = _artifact_kind(entry.get("artifact_kind"))
        if kind:
            source_state["artifact_kinds"].add(kind)
        source_state["entries"] = int(source_state["entries"]) + 1
        source_state["visibility"] = best_visibility_metadata(
            source_state.get("visibility", {}),
            _resolved_visibility_metadata(
                entry,
                provider=str(plugin),
                plugin=str(plugin),
                subcommand=str(entry.get("subcommand") or ""),
                locator=str(source),
            ),
        )
        snapshot = snapshot_by_digest.get(str(checksum))
        if snapshot is not None:
            source_state["variants"].add(_render_variant(snapshot))
        edge_plugins.setdefault((source, checksum), []).append(plugin)
        edge_actors.setdefault((source, checksum), set()).add(actor)
        detail = content_details.setdefault(
            checksum,
            {
                "providers": set(),
                "actors": set(),
                "resource_hints": set(),
            },
        )
        detail["providers"].add(
            _provider_name(source, plugins=[plugin], fallback=plugin)
        )
        detail["actors"].add(actor)
        resource_kind, resource_label = _resource_label(source)
        if resource_kind and resource_label:
            detail["resource_hints"].add(f"{resource_kind}:{resource_label}")
        else:
            detail["resource_hints"].add(source)

    name_counts = Counter(snapshot_display_name(snapshot) for snapshot in snapshots)

    content = [
        {
            "checksum": snapshot.digest,
            "preferredName": snapshot_display_name(snapshot),
            "artifactKind": _artifact_kind(snapshot.metadata.get("artifact_kind")),
            "contentLocator": content_locator(snapshot.digest),
            "artifactLocator": snapshot_artifact_locator(snapshot),
            "followCommand": _session_read_command(
                snapshot_artifact_locator(snapshot),
                session_ref=session_ref,
            ),
            "nameCollision": name_counts[snapshot_display_name(snapshot)] > 1,
            "nameCount": len(snapshot.names),
            "fetchCount": len(snapshot.events),
            "names": snapshot.names,
            "firstFetchedAt": snapshot.events[0].timestamp if snapshot.events else "",
            "lastFetchedAt": snapshot.events[-1].timestamp if snapshot.events else "",
            "providers": sorted(
                content_details.get(snapshot.digest, {}).get("providers", set())
            ),
            "actors": sorted(
                content_details.get(snapshot.digest, {}).get("actors", set())
            ),
            "resourceHints": sorted(
                content_details.get(snapshot.digest, {}).get("resource_hints", set())
            ),
            **_resolved_visibility_metadata(
                dict(snapshot.metadata),
                provider=str(snapshot.metadata.get("plugin") or ""),
                plugin=str(snapshot.metadata.get("plugin") or ""),
                subcommand=str(snapshot.metadata.get("subcommand") or ""),
                locator=str(snapshot_locator(snapshot)),
            ),
        }
        for snapshot in snapshots
    ]
    sources = [
        {
            "locator": locator,
            "contentCount": len(state["content"]),
            "entryCount": int(state["entries"]),
            "artifactKind": (
                next(iter(state["artifact_kinds"]))
                if len(state["artifact_kinds"]) == 1
                else ""
            ),
            "artifactKinds": sorted(str(value) for value in state["artifact_kinds"]),
            "plugins": sorted(str(value) for value in state["plugins"]),
            "actors": sorted(str(value) for value in state["actors"]),
            "locators": sorted(str(value) for value in state["locators"]),
            "collision": False,
            "duplicateMaterialization": len(state["content"]) > 1
            and len(state["variants"]) <= 1,
            "variant": len(state["variants"]) > 1,
            "variantCount": len(state["variants"]),
            "variants": [
                _render_variant_label(variant) for variant in sorted(state["variants"])
            ],
            **best_visibility_metadata(state.get("visibility", {})),
        }
        for locator, state in sorted(source_map.items())
    ]
    source_edges = [
        {
            "source": source,
            "checksum": checksum,
            "plugins": sorted(plugins),
            "actors": sorted(edge_actors.get((source, checksum), set())),
            "count": len(plugins),
        }
        for (source, checksum), plugins in sorted(edge_plugins.items())
    ]
    revision_edges = _revision_edges(snapshots)
    lead_edges = build_lead_edge_records(
        snapshots,
        manifest_entries,
        classify_kind=_lead_kind,
    )
    lead_sources = aggregate_lead_sources(lead_edges)
    for lead in lead_sources:
        locator = str(lead.get("locator") or "").strip()
        if locator:
            lead["followCommand"] = _follow_command(locator, session_ref=session_ref)
    collisions = [source["locator"] for source in sources if source["collision"]]
    duplicate_materializations = [
        source["locator"]
        for source in sources
        if source.get("duplicateMaterialization")
    ]
    variants = [source["locator"] for source in sources if source.get("variant")]
    name_collisions = sorted(
        name for name, count in name_counts.items() if count > 1 and name
    )
    materialized_lead_count = sum(
        1 for source in lead_sources if bool(source["materialized"])
    )
    empty = (
        not sources
        and not content
        and not source_edges
        and not revision_edges
        and not lead_edges
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
        "manifestEntryCount": len(manifest_entries),
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
        "nextStep": _topology_next_step(
            discovery_count=discovery_count,
            evidence_count=evidence_count,
        ),
        "sources": sources,
        "content": content,
        "sourceEdges": source_edges,
        "revisionEdges": revision_edges,
        "leadSources": lead_sources,
        "leadEdges": lead_edges,
    }


def _semantic_payload(dirs, *, session_ref: str = "") -> dict[str, object]:
    lineage = _analysis_payload(dirs, session_ref=session_ref)
    nodes: dict[str, dict[str, object]] = {}
    edges: set[tuple[str, str, str]] = set()

    def add_node(node_id: str, *, label: str, kind: str, group: str) -> None:
        nodes.setdefault(
            node_id,
            {
                "id": node_id,
                "label": label,
                "kind": kind,
                "group": group,
                "materialized": False,
                "discovered": False,
            },
        )

    def add_edge(source: str, target: str, label: str) -> None:
        edges.add((source, target, label))

    for source in lineage["sources"]:
        locator = str(source["locator"])
        provider = _provider_name(
            locator,
            plugins=[str(value) for value in source.get("plugins") or [] if str(value)],
        )
        provider_id = f"provider:{provider}"
        source_id = f"source:{locator}"
        add_node(provider_id, label=provider, kind="provider", group=provider)
        add_node(source_id, label=locator, kind="source", group=provider)
        nodes[source_id]["materialized"] = True
        nodes[source_id]["artifactKind"] = str(source.get("artifactKind") or "")
        nodes[source_id]["artifactKinds"] = list(source.get("artifactKinds") or [])
        add_edge(provider_id, source_id, "source")

        query_label = _query_label(locator)
        if query_label:
            query_id = f"query:{provider}:{query_label}"
            add_node(query_id, label=query_label, kind="query", group=provider)
            add_edge(provider_id, query_id, "query")
            add_edge(query_id, source_id, "drives")

        resource_kind, resource_label = _resource_label(locator)
        if resource_kind and resource_label:
            resource_id = f"resource:{provider}:{resource_label}"
            add_node(
                resource_id,
                label=resource_label,
                kind=resource_kind,
                group=provider,
            )
            add_edge(provider_id, resource_id, "resource")
            add_edge(resource_id, source_id, "resolved_by")

    for content in lineage["content"]:
        checksum = str(content["checksum"])
        content_id = f"content:{checksum}"
        add_node(
            content_id,
            label=str(content["preferredName"]),
            kind="content",
            group="content",
        )
        nodes[content_id]["materialized"] = True
        nodes[content_id]["artifactKind"] = str(content.get("artifactKind") or "")

    for edge in lineage["sourceEdges"]:
        source_id = f"source:{edge['source']}"
        content_id = f"content:{edge['checksum']}"
        add_edge(
            source_id, content_id, ",".join(str(value) for value in edge["plugins"])
        )

    for edge in lineage["revisionEdges"]:
        from_id = f"content:{edge['from']}"
        to_id = f"content:{edge['to']}"
        add_edge(from_id, to_id, f"revision:{edge['locator']}")
    for lead_source in lineage.get("leadSources") or []:
        locator = str(lead_source["locator"])
        provider = str(lead_source["provider"] or _provider_name(locator))
        provider_id = f"provider:{provider}"
        source_id = f"source:{locator}"
        add_node(provider_id, label=provider, kind="provider", group=provider)
        add_node(source_id, label=locator, kind="source", group=provider)
        nodes[source_id]["materialized"] = bool(
            nodes[source_id].get("materialized") or lead_source.get("materialized")
        )
        nodes[source_id]["discovered"] = True
        if not nodes[source_id].get("artifactKinds"):
            nodes[source_id]["artifactKinds"] = []
        kinds = {
            str(value)
            for value in nodes[source_id].get("artifactKinds") or []
            if str(value)
        }
        lead_kind = str(lead_source.get("artifactKind") or "")
        if lead_kind:
            kinds.add(lead_kind)
        nodes[source_id]["artifactKinds"] = sorted(kinds)
        nodes[source_id]["artifactKind"] = (
            nodes[source_id]["artifactKinds"][0]
            if len(nodes[source_id]["artifactKinds"]) == 1
            else ""
        )
        add_edge(provider_id, source_id, "source")
        resource_kind, resource_label = _resource_label(locator)
        if resource_kind and resource_label:
            resource_id = f"resource:{provider}:{resource_label}"
            add_node(
                resource_id,
                label=resource_label,
                kind=resource_kind,
                group=provider,
            )
            add_edge(provider_id, resource_id, "resource")
            add_edge(resource_id, source_id, "resolved_by")
    for edge in lineage.get("leadEdges") or []:
        from_id = f"content:{edge['sourceChecksum']}"
        to_id = f"source:{edge['targetLocator']}"
        relation = str(edge.get("relation") or "links_to")
        count = int(edge.get("occurrenceCount") or 0)
        add_edge(from_id, to_id, relation if count <= 1 else f"{relation} x{count}")

    empty = not nodes and not edges
    discovery_count = int(lineage.get("discoveryArtifactCount") or 0)
    evidence_count = int(lineage.get("evidenceArtifactCount") or 0)
    return {
        "sessionDir": lineage["sessionDir"],
        "contentDir": lineage["contentDir"],
        "manifestPath": lineage["manifestPath"],
        "nodeCount": len(nodes),
        "edgeCount": len(edges),
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "empty": empty,
        "nextStep": _topology_next_step(
            discovery_count=discovery_count,
            evidence_count=evidence_count,
        ),
        "nodes": sorted(nodes.values(), key=lambda item: (item["kind"], item["label"])),
        "edges": [
            {"source": source, "target": target, "label": label}
            for source, target, label in sorted(edges)
        ],
    }


def _render_semantic_mermaid(payload: dict[str, object]) -> str:
    lines = [
        "---",
        "title: gotta semantic graph",
        "---",
        "flowchart LR",
    ]
    if payload.get("empty"):
        lines.append(
            f'  empty["{_mermaid_label(str(payload.get("nextStep") or _empty_topology_next_step()))}"]'
        )
        lines.extend(
            [
                "  class empty emptyState",
                "  classDef emptyState fill:#f7fafc,stroke:#4a5568,color:#1a202c;",
                "",
            ]
        )
        return "\n".join(lines)
    if payload.get("nextStep"):
        lines.append(f'  note["{_mermaid_label(str(payload["nextStep"]))}"]')
        lines.append("  class note emptyState")
    for node in payload["nodes"]:
        node_id = _analysis_mermaid_id("sem", str(node["id"]))
        label = _mermaid_label(str(node["label"]))
        kind = str(node["kind"]).replace("-", "_")
        if (
            str(node.get("kind")) == "source"
            and not bool(node.get("materialized"))
            and bool(node.get("discovered"))
        ):
            kind = "source_gap"
        lines.append(f'  {node_id}["{label}"]')
        lines.append(f"  class {node_id} {kind}")
    for edge in payload["edges"]:
        source_id = _analysis_mermaid_id("sem", str(edge["source"]))
        target_id = _analysis_mermaid_id("sem", str(edge["target"]))
        label = _mermaid_label(str(edge["label"]))
        lines.append(f"  {source_id} -->|{label}| {target_id}")
    lines.extend(
        [
            "  classDef provider fill:#fff7e6,stroke:#b7791f,color:#5a3b09;",
            "  classDef query fill:#f5f3ff,stroke:#6b46c1,color:#34206b;",
            "  classDef source fill:#eef8ee,stroke:#2d7a2d,color:#173d17;",
            "  classDef source_gap fill:#fffaf0,stroke:#b7791f,color:#5a3b09;",
            "  classDef content fill:#eef4ff,stroke:#2a62c7,color:#173058;",
            "  classDef jira_issue fill:#fff5f5,stroke:#c53030,color:#63171b;",
            "  classDef confluence_page fill:#fffbea,stroke:#b7791f,color:#5a3b09;",
            "  classDef google_doc fill:#effcf6,stroke:#2f855a,color:#1b4332;",
            "  classDef google_drive_file fill:#effcf6,stroke:#2f855a,color:#1b4332;",
            "  classDef slack_thread fill:#ebf8ff,stroke:#2b6cb0,color:#1a365d;",
            "  classDef slack_channel fill:#ebf8ff,stroke:#2b6cb0,color:#1a365d;",
            "  classDef github_repo fill:#f7fafc,stroke:#4a5568,color:#1a202c;",
            "  classDef emptyState fill:#f7fafc,stroke:#4a5568,color:#1a202c;",
            "",
        ]
    )
    return "\n".join(lines)


def _render_analysis_mermaid(payload: dict[str, object]) -> str:
    lines = [
        "---",
        "title: gotta session analysis",
        "---",
        "flowchart LR",
    ]
    if payload.get("empty"):
        lines.append(
            f'  empty["{_mermaid_label(str(payload.get("nextStep") or _empty_topology_next_step()))}"]'
        )
        lines.extend(
            [
                "  class empty emptyState",
                "  classDef emptyState fill:#f7fafc,stroke:#4a5568,color:#1a202c;",
                "",
            ]
        )
        return "\n".join(lines)
    if payload.get("nextStep"):
        lines.append(f'  note["{_mermaid_label(str(payload["nextStep"]))}"]')
        lines.append("  class note emptyState")
    for source in payload["sources"]:
        locator = str(source["locator"])
        node_id = _analysis_mermaid_id("src", locator)
        actors = ", ".join(str(value) for value in source.get("actors") or [])
        label_parts = [locator]
        if actors:
            label_parts.append(f"actor: {actors}")
        if source.get("variant"):
            label_parts.append(f"renderings: {int(source.get('variantCount') or 0)}")
        elif source.get("duplicateMaterialization"):
            label_parts.append(
                f"materializations: {int(source.get('contentCount') or 0)}"
            )
        label = _mermaid_label("\n".join(label_parts))
        lines.append(f'  {node_id}["{label}"]')
        if source["collision"]:
            lines.append(f"  class {node_id} collision")
        elif source.get("variant"):
            lines.append(f"  class {node_id} variant")
        elif source.get("duplicateMaterialization"):
            lines.append(f"  class {node_id} duplicate")
        else:
            lines.append(f"  class {node_id} source")
    for content in payload["content"]:
        checksum = str(content["checksum"])
        providers = ", ".join(str(value) for value in content.get("providers") or [])
        actors = ", ".join(str(value) for value in content.get("actors") or [])
        label_parts = [str(content["preferredName"])]
        resource_hints = [
            str(value) for value in content.get("resourceHints") or [] if str(value)
        ]
        if bool(content.get("nameCollision")) and resource_hints:
            label_parts.append(resource_hints[0])
        if providers:
            label_parts.append(providers)
        if actors:
            label_parts.append(f"actor: {actors}")
        label_parts.append(checksum[:12])
        label = _mermaid_label("\n".join(label_parts))
        node_id = _analysis_mermaid_id("art", checksum)
        lines.append(f'  {node_id}["{label}"]')
        lines.append(f"  class {node_id} content")
    for edge in payload["sourceEdges"]:
        source_id = _analysis_mermaid_id("src", str(edge["source"]))
        content_id = _analysis_mermaid_id("art", str(edge["checksum"]))
        label_parts = [", ".join(edge["plugins"])]
        actors = ", ".join(str(value) for value in edge.get("actors") or [])
        if actors:
            label_parts.append(f"actor: {actors}")
        label = _mermaid_label("\n".join(part for part in label_parts if part))
        lines.append(f"  {source_id} -->|{label}| {content_id}")
    for edge in payload["revisionEdges"]:
        from_id = _analysis_mermaid_id("art", str(edge["from"]))
        to_id = _analysis_mermaid_id("art", str(edge["to"]))
        label = _mermaid_label(
            f"revision:{str(edge['locator'])}\n{str(edge.get('rendering') or '')}".rstrip()
        )
        lines.append(f"  {from_id} -->|{label}| {to_id}")
    seen_source_nodes = {str(source["locator"]) for source in payload["sources"]}
    for lead_source in payload.get("leadSources") or []:
        locator = str(lead_source["locator"])
        if locator in seen_source_nodes:
            continue
        node_id = _analysis_mermaid_id("src", locator)
        label_parts = [locator, f"lead: {str(lead_source['provider'])}"]
        if not bool(lead_source.get("materialized")):
            label_parts.append("not yet materialized")
        label = _mermaid_label("\n".join(label_parts))
        lines.append(f'  {node_id}["{label}"]')
        lines.append(f"  class {node_id} leadgap")
    for edge in payload.get("leadEdges") or []:
        content_id = _analysis_mermaid_id("art", str(edge["sourceChecksum"]))
        source_id = _analysis_mermaid_id("src", str(edge["targetLocator"]))
        relation = str(edge.get("relation") or "links_to")
        count = int(edge.get("occurrenceCount") or 0)
        label = relation if count <= 1 else f"{relation} x{count}"
        lines.append(f"  {content_id} -.->|{_mermaid_label(label)}| {source_id}")
    lines.extend(
        [
            "  classDef source fill:#eef8ee,stroke:#2d7a2d,color:#173d17;",
            "  classDef content fill:#eef4ff,stroke:#2a62c7,color:#173058;",
            "  classDef duplicate fill:#edf2f7,stroke:#4a5568,color:#1a202c;",
            "  classDef variant fill:#fff7e6,stroke:#b7791f,color:#5a3b09;",
            "  classDef collision fill:#fff1f1,stroke:#c73434,color:#6b1111;",
            "  classDef leadgap fill:#fffaf0,stroke:#b7791f,color:#5a3b09;",
            "  classDef emptyState fill:#f7fafc,stroke:#4a5568,color:#1a202c;",
            "",
        ]
    )
    return "\n".join(lines)


def _semantic_node_follow_command(
    node: dict[str, object],
    *,
    lineage: dict[str, object],
) -> str:
    kind = str(node.get("kind") or "")
    node_id = str(node.get("id") or "")
    label = str(node.get("label") or "").strip()
    if kind == "source" and label:
        return _follow_command(label)
    if kind == "content" and node_id.startswith("content:"):
        checksum = node_id.split(":", 1)[1]
        for content_item in lineage.get("content") or []:
            if str(content_item.get("checksum") or "") == checksum:
                return str(content_item.get("followCommand") or "").strip()
    return ""


def _focus_match_threshold(best_score: int) -> int:
    if best_score <= 0:
        return 0
    if best_score >= 4:
        return 2
    return best_score


def _ordered_focus_scan_entries(
    scan_payload: dict[str, object] | None,
    *,
    limit: int,
) -> list[dict[str, object]]:
    if not isinstance(scan_payload, dict):
        return []
    entries = [
        dict(entry)
        for entry in scan_payload.get("entries") or []
        if isinstance(entry, dict)
    ]
    ordered = sorted(
        entries,
        key=lambda entry: str(
            entry.get("lastFetchedAt") or entry.get("fetched_at") or ""
        ),
        reverse=True,
    )
    ordered = sorted(
        ordered,
        key=lambda entry: int(entry.get("hitCount") or 0),
        reverse=True,
    )
    ordered = sorted(
        ordered,
        key=lambda entry: (
            str(entry.get("artifactKind") or entry.get("artifact_kind") or "")
            != "evidence"
        ),
    )
    return ordered[: max(limit, 0)]


def _lineage_source_candidate(item: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "source",
        "label": str(item.get("locator") or ""),
        "locator": str(item.get("locator") or ""),
        "artifactKind": str(item.get("artifactKind") or ""),
        "materialized": True,
        "followCommand": str(item.get("followCommand") or ""),
    }


def _lineage_content_candidate(item: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "content",
        "label": str(item.get("preferredName") or ""),
        "checksum": str(item.get("checksum") or ""),
        "artifactLocator": str(item.get("artifactLocator") or ""),
        "contentLocator": str(item.get("contentLocator") or ""),
        "artifactKind": str(item.get("artifactKind") or ""),
        "materialized": True,
        "followCommand": str(item.get("followCommand") or ""),
    }


def _lineage_lead_candidate(item: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "lead",
        "label": str(item.get("locator") or ""),
        "locator": str(item.get("locator") or ""),
        "artifactKind": str(item.get("artifactKind") or ""),
        "materialized": bool(item.get("materialized")),
        "followCommand": str(item.get("followCommand") or ""),
    }


def _analysis_focus_score(
    node: dict[str, object], query: str
) -> tuple[int, int, int, str]:
    query_lower = query.lower()
    label = str(node.get("label") or "")
    node_id = str(node.get("id") or "")
    label_lower = label.lower()
    node_id_lower = node_id.lower()
    score = 0
    if label_lower == query_lower or node_id_lower == query_lower:
        score = 5
    elif label_lower.startswith(query_lower) or node_id_lower.startswith(query_lower):
        score = 4
    elif f":{query_lower}" in node_id_lower:
        score = 3
    elif query_lower in label_lower or query_lower in node_id_lower:
        score = 2
    materialized = 1 if bool(node.get("materialized")) else 0
    discovered = 1 if bool(node.get("discovered")) else 0
    return (score, materialized, discovered, label_lower)


def _neighbor_sort_key(
    node: dict[str, object],
    *,
    relation_labels: list[str],
) -> tuple[int, int, int, str, str]:
    interesting_relations = sum(
        1
        for label in relation_labels
        if label not in {"source", "resource", "resolved_by", "query", "drives"}
    )
    return (
        interesting_relations,
        1 if bool(node.get("materialized")) else 0,
        1 if bool(node.get("discovered")) else 0,
        str(node.get("kind") or ""),
        str(node.get("label") or "").lower(),
    )


def _semantic_focus_payload(
    lineage: dict[str, object],
    semantic: dict[str, object],
    *,
    focus: str,
    limit: int,
    scan_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    query = focus.strip()
    if not query:
        return {
            "sessionDir": semantic["sessionDir"],
            "contentDir": semantic["contentDir"],
            "focus": "",
            "matched": False,
            "empty": True,
            "nextStep": "Provide a focus keyword, locator, artifact name, or checksum prefix.",
            "nodeCount": 0,
            "edgeCount": 0,
            "nodes": [],
            "edges": [],
            "neighbors": [],
            "anchors": [],
            "matchedCount": 0,
        }
    nodes = [dict(node) for node in semantic.get("nodes") or []]
    node_index = {str(node["id"]): node for node in nodes}
    matches = sorted(
        (
            node
            for node in nodes
            if str(node.get("kind") or "") in {"source", "content"}
            and _analysis_focus_score(node, query)[0] > 0
        ),
        key=lambda node: _analysis_focus_score(node, query),
        reverse=True,
    )
    scan_entries = _ordered_focus_scan_entries(
        scan_payload,
        limit=max(limit * 2, 8),
    )
    seed_cap = max(4, min(max(limit, 1), 12))
    if not matches and not scan_entries:
        return {
            "sessionDir": semantic["sessionDir"],
            "contentDir": semantic["contentDir"],
            "focus": query,
            "matched": False,
            "empty": True,
            "nextStep": (
                f"No analyzed node or projected artifact matched `{query}`. Try a canonical locator, "
                "artifact name, checksum prefix, or a tighter keyword from session scan, leads, or manifest."
            ),
            "nodeCount": 0,
            "edgeCount": 0,
            "nodes": [],
            "edges": [],
            "neighbors": [],
            "anchors": [],
            "matchedCount": 0,
        }

    best_score = _analysis_focus_score(matches[0], query)[0] if matches else 0
    threshold = _focus_match_threshold(best_score)
    seed_ids: list[str] = []

    def add_seed(node_id: str) -> None:
        if node_id and node_id in node_index and node_id not in seed_ids:
            seed_ids.append(node_id)

    for node in matches:
        if _analysis_focus_score(node, query)[0] < threshold:
            break
        add_seed(str(node.get("id") or ""))
        if len(seed_ids) >= seed_cap:
            break
    for entry in scan_entries:
        checksum = str(entry.get("checksum") or "").strip()
        locator = str(
            entry.get("canonical_locator") or entry.get("locator") or ""
        ).strip()
        if checksum:
            add_seed(f"content:{checksum}")
        if locator:
            add_seed(f"source:{locator}")
        if len(seed_ids) >= seed_cap:
            break

    if not seed_ids:
        return {
            "sessionDir": semantic["sessionDir"],
            "contentDir": semantic["contentDir"],
            "focus": query,
            "matched": False,
            "empty": True,
            "nextStep": (
                f"No analyzed node or projected artifact matched `{query}`. Try a canonical locator, "
                "artifact name, checksum prefix, or a tighter keyword from session scan, leads, or manifest."
            ),
            "nodeCount": 0,
            "edgeCount": 0,
            "nodes": [],
            "edges": [],
            "neighbors": [],
            "anchors": [],
            "matchedCount": 0,
        }

    root = dict(node_index[seed_ids[0]])
    root["followCommand"] = _semantic_node_follow_command(root, lineage=lineage)
    root_id = str(root["id"])
    seed_records = []
    for node_id in seed_ids:
        node = dict(node_index[node_id])
        node["followCommand"] = _semantic_node_follow_command(node, lineage=lineage)
        seed_records.append(node)
    seed_id_set = set(seed_ids)
    structural_labels = {"source", "resource", "resolved_by", "query", "drives"}
    incident_edges = [
        dict(edge)
        for edge in semantic.get("edges") or []
        if (
            str(edge.get("source") or "") in seed_id_set
            or str(edge.get("target") or "") in seed_id_set
        )
    ]
    semantic_incident_edges = [
        edge
        for edge in incident_edges
        if str(edge.get("label") or "") not in structural_labels
    ]
    selected_edges = semantic_incident_edges or incident_edges
    selected_neighbor_ids: list[str] = []
    relation_labels_by_neighbor: dict[str, list[str]] = {}
    for edge in selected_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in seed_id_set and target in seed_id_set:
            continue
        neighbor_id = target if source in seed_id_set else source
        if not neighbor_id:
            continue
        if neighbor_id not in relation_labels_by_neighbor:
            relation_labels_by_neighbor[neighbor_id] = []
        relation_labels_by_neighbor[neighbor_id].append(str(edge.get("label") or ""))
        if neighbor_id not in selected_neighbor_ids:
            selected_neighbor_ids.append(neighbor_id)
    if len(selected_neighbor_ids) < max(2, limit // 2):
        for edge in incident_edges:
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            if source in seed_id_set and target in seed_id_set:
                continue
            neighbor_id = target if source in seed_id_set else source
            if not neighbor_id:
                continue
            if neighbor_id not in relation_labels_by_neighbor:
                relation_labels_by_neighbor[neighbor_id] = []
            relation_labels_by_neighbor[neighbor_id].append(
                str(edge.get("label") or "")
            )
            if neighbor_id not in selected_neighbor_ids:
                selected_neighbor_ids.append(neighbor_id)
    selected_neighbor_ids = [
        neighbor_id
        for neighbor_id in sorted(
            selected_neighbor_ids,
            key=lambda neighbor_id: _neighbor_sort_key(
                node_index.get(neighbor_id, {}),
                relation_labels=relation_labels_by_neighbor.get(neighbor_id, []),
            ),
            reverse=True,
        )[: max(limit, 0)]
        if neighbor_id in node_index
    ]
    selected_node_ids = {root_id, *seed_ids, *selected_neighbor_ids}
    focused_edges = [
        edge
        for edge in incident_edges
        if (
            str(edge.get("source") or "") in selected_node_ids
            and str(edge.get("target") or "") in selected_node_ids
        )
    ]
    neighbor_records = []
    for neighbor_id in selected_neighbor_ids:
        node = dict(node_index[neighbor_id])
        node["followCommand"] = _semantic_node_follow_command(node, lineage=lineage)
        relation_labels = relation_labels_by_neighbor.get(neighbor_id, [])
        neighbor_records.append(
            {
                **node,
                "relations": relation_labels,
            }
        )
    focused_nodes = [*seed_records, *neighbor_records]
    suppressed_count = max(len(incident_edges) - len(focused_edges), 0)
    return {
        "sessionDir": semantic["sessionDir"],
        "contentDir": semantic["contentDir"],
        "focus": query,
        "matched": True,
        "empty": False,
        "nextStep": "",
        "nodeCount": len(focused_nodes),
        "edgeCount": len(focused_edges),
        "root": root,
        "anchors": seed_records[1:],
        "matchedCount": len(seed_records),
        "neighbors": neighbor_records,
        "nodes": focused_nodes,
        "edges": focused_edges,
        "suppressedStructuralEdgeCount": suppressed_count,
    }


def _lineage_focus_score(item: dict[str, object], query: str) -> tuple[int, int, str]:
    query_lower = query.lower()
    candidates = [
        str(item.get("label") or ""),
        str(item.get("locator") or ""),
        str(item.get("artifactLocator") or ""),
        str(item.get("contentLocator") or ""),
        str(item.get("checksum") or ""),
    ]
    lowered = [candidate.lower() for candidate in candidates if candidate]
    score = 0
    if any(candidate == query_lower for candidate in lowered):
        score = 5
    elif any(candidate.startswith(query_lower) for candidate in lowered):
        score = 4
    elif any(f":{query_lower}" in candidate for candidate in lowered):
        score = 3
    elif any(query_lower in candidate for candidate in lowered):
        score = 2
    materialized = 1 if bool(item.get("materialized")) else 0
    label = str(item.get("label") or item.get("locator") or "")
    return (score, materialized, label.lower())


def _lineage_focus_payload(
    payload: dict[str, object],
    *,
    focus: str,
    limit: int,
    scan_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    query = focus.strip()
    if not query:
        return {
            "sessionDir": payload["sessionDir"],
            "contentDir": payload["contentDir"],
            "focus": "",
            "matched": False,
            "empty": True,
            "nextStep": "Provide a focus keyword, locator, artifact name, or checksum prefix.",
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
    sources = [dict(item) for item in payload.get("sources") or []]
    content_items = [dict(item) for item in payload.get("content") or []]
    lead_sources = [dict(item) for item in payload.get("leadSources") or []]
    source_index = {str(item.get("locator") or ""): item for item in sources}
    content_index = {str(item.get("checksum") or ""): item for item in content_items}
    lead_index = {str(item.get("locator") or ""): item for item in lead_sources}
    candidates = [
        *(_lineage_source_candidate(item) for item in sources),
        *(_lineage_content_candidate(item) for item in content_items),
        *(
            _lineage_lead_candidate(item)
            for item in lead_sources
            if str(item.get("locator") or "") not in source_index
        ),
    ]
    matches = sorted(
        (
            candidate
            for candidate in candidates
            if _lineage_focus_score(candidate, query)[0] > 0
        ),
        key=lambda candidate: _lineage_focus_score(candidate, query),
        reverse=True,
    )
    scan_entries = _ordered_focus_scan_entries(
        scan_payload,
        limit=max(limit * 2, 8),
    )
    seed_cap = max(4, min(max(limit, 1), 12))
    if not matches and not scan_entries:
        return {
            "sessionDir": payload["sessionDir"],
            "contentDir": payload["contentDir"],
            "focus": query,
            "matched": False,
            "empty": True,
            "nextStep": (
                f"No analyzed lineage anchor or projected artifact matched `{query}`. Try a canonical locator, "
                "artifact name, checksum prefix, or a tighter target from session scan, leads, or manifest."
            ),
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
    best_score = _lineage_focus_score(matches[0], query)[0] if matches else 0
    threshold = _focus_match_threshold(best_score)
    seeds: list[dict[str, object]] = []
    seen_seed_keys: set[tuple[str, str]] = set()

    def add_seed(candidate: dict[str, object]) -> None:
        kind = str(candidate.get("kind") or "")
        if kind == "source":
            key = ("source", str(candidate.get("locator") or ""))
        elif kind == "content":
            key = ("content", str(candidate.get("checksum") or ""))
        else:
            key = ("lead", str(candidate.get("locator") or ""))
        if not key[1] or key in seen_seed_keys:
            return
        seen_seed_keys.add(key)
        seeds.append(candidate)

    for candidate in matches:
        if _lineage_focus_score(candidate, query)[0] < threshold:
            break
        add_seed(dict(candidate))
        if len(seeds) >= seed_cap:
            break
    for entry in scan_entries:
        checksum = str(entry.get("checksum") or "").strip()
        locator = str(
            entry.get("canonical_locator") or entry.get("locator") or ""
        ).strip()
        if checksum and checksum in content_index:
            add_seed(_lineage_content_candidate(content_index[checksum]))
        if locator and locator in source_index:
            add_seed(_lineage_source_candidate(source_index[locator]))
        if len(seeds) >= seed_cap:
            break

    if not seeds:
        return {
            "sessionDir": payload["sessionDir"],
            "contentDir": payload["contentDir"],
            "focus": query,
            "matched": False,
            "empty": True,
            "nextStep": (
                f"No analyzed lineage anchor or projected artifact matched `{query}`. Try a canonical locator, "
                "artifact name, checksum prefix, or a tighter target from session scan, leads, or manifest."
            ),
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

    root = dict(seeds[0])
    selected_sources: set[str] = set()
    selected_content: set[str] = set()
    selected_leads: set[str] = set()
    matched_sources = {
        str(candidate.get("locator") or "")
        for candidate in matches
        if str(candidate.get("kind") or "") == "source"
    }
    matched_leads = {
        str(candidate.get("locator") or "")
        for candidate in matches
        if str(candidate.get("kind") or "") == "lead"
    }
    for candidate in seeds:
        if candidate["kind"] == "source":
            selected_sources.add(str(candidate.get("locator") or ""))
        elif candidate["kind"] == "content":
            selected_content.add(str(candidate.get("checksum") or ""))
        else:
            selected_leads.add(str(candidate.get("locator") or ""))

    def expand_source_and_revision_edges() -> None:
        for edge in payload.get("sourceEdges") or []:
            source = str(edge.get("source") or "")
            checksum = str(edge.get("checksum") or "")
            if source in selected_sources:
                selected_content.add(checksum)
            if checksum in selected_content:
                selected_sources.add(source)
        for edge in payload.get("revisionEdges") or []:
            from_checksum = str(edge.get("from") or "")
            to_checksum = str(edge.get("to") or "")
            if from_checksum in selected_content:
                selected_content.add(to_checksum)
            if to_checksum in selected_content:
                selected_content.add(from_checksum)

    expand_source_and_revision_edges()
    for edge in payload.get("leadEdges") or []:
        source_checksum = str(edge.get("sourceChecksum") or "")
        target_locator = str(edge.get("targetLocator") or "")
        target_is_source = target_locator in source_index
        target_matches_focus = (
            target_locator in matched_sources or target_locator in matched_leads
        )
        if source_checksum in selected_content and (
            target_locator in selected_sources
            or target_locator in selected_leads
            or target_matches_focus
        ):
            if target_is_source:
                selected_sources.add(target_locator)
            else:
                selected_leads.add(target_locator)
        if target_locator in selected_sources or target_locator in selected_leads:
            selected_content.add(source_checksum)
    expand_source_and_revision_edges()

    neighbor_candidates: list[dict[str, object]] = []
    for locator in sorted(selected_sources):
        if ("source", locator) in seen_seed_keys:
            continue
        source_item = source_index.get(locator)
        if source_item is None:
            continue
        neighbor_candidates.append(
            {
                "kind": "source",
                "label": locator,
                "relation": "materialized source",
                "followCommand": str(source_item.get("followCommand") or ""),
                "artifactKind": str(source_item.get("artifactKind") or ""),
                "materialized": True,
            }
        )
    for checksum in sorted(selected_content):
        if ("content", checksum) in seen_seed_keys:
            continue
        content_item = content_index.get(checksum)
        if content_item is None:
            continue
        neighbor_candidates.append(
            {
                "kind": "content",
                "label": str(content_item.get("preferredName") or checksum),
                "relation": "stored artifact",
                "followCommand": str(content_item.get("followCommand") or ""),
                "artifactKind": str(content_item.get("artifactKind") or ""),
                "materialized": True,
            }
        )
    for locator in sorted(selected_leads):
        if ("lead", locator) in seen_seed_keys:
            continue
        lead_item = lead_index.get(locator)
        if lead_item is None:
            continue
        neighbor_candidates.append(
            {
                "kind": "lead",
                "label": locator,
                "relation": "followable lead",
                "followCommand": str(lead_item.get("followCommand") or ""),
                "artifactKind": str(lead_item.get("artifactKind") or ""),
                "materialized": bool(lead_item.get("materialized")),
            }
        )
    ordered_neighbors = sorted(
        neighbor_candidates,
        key=lambda item: (
            1 if bool(item.get("materialized")) else 0,
            1 if str(item.get("kind") or "") == "content" else 0,
            str(item.get("label") or "").lower(),
        ),
        reverse=True,
    )[: max(limit, 0)]

    neighbor_source_labels = {
        str(item.get("label") or "")
        for item in ordered_neighbors
        if str(item.get("kind") or "") == "source"
    }
    neighbor_content_labels = {
        str(item.get("label") or "")
        for item in ordered_neighbors
        if str(item.get("kind") or "") == "content"
    }
    neighbor_content_checksums = {
        checksum
        for checksum, item in content_index.items()
        if str(item.get("preferredName") or checksum) in neighbor_content_labels
    }
    neighbor_lead_labels = {
        str(item.get("label") or "")
        for item in ordered_neighbors
        if str(item.get("kind") or "") == "lead"
    }
    seed_source_labels = {value for kind, value in seen_seed_keys if kind == "source"}
    seed_content_checksums = {
        value for kind, value in seen_seed_keys if kind == "content"
    }
    seed_lead_labels = {value for kind, value in seen_seed_keys if kind == "lead"}
    selected_sources = {
        locator
        for locator in selected_sources
        if locator in seed_source_labels or locator in neighbor_source_labels
    }
    selected_content = {
        checksum
        for checksum in selected_content
        if checksum in seed_content_checksums or checksum in neighbor_content_checksums
    }
    selected_leads = {
        locator
        for locator in selected_leads
        if locator in seed_lead_labels or locator in neighbor_lead_labels
    }

    selected_source_items = [
        item for item in sources if str(item.get("locator") or "") in selected_sources
    ]
    selected_content_items = [
        item
        for item in content_items
        if str(item.get("checksum") or "") in selected_content
    ]
    selected_lead_items = [
        item
        for item in lead_sources
        if str(item.get("locator") or "") in selected_leads
        and str(item.get("locator") or "") not in selected_sources
    ]
    selected_source_edges = [
        edge
        for edge in payload.get("sourceEdges") or []
        if str(edge.get("source") or "") in selected_sources
        and str(edge.get("checksum") or "") in selected_content
    ]
    selected_revision_edges = [
        edge
        for edge in payload.get("revisionEdges") or []
        if str(edge.get("from") or "") in selected_content
        and str(edge.get("to") or "") in selected_content
    ]
    selected_lead_edges = [
        edge
        for edge in payload.get("leadEdges") or []
        if str(edge.get("sourceChecksum") or "") in selected_content
        and str(edge.get("targetLocator") or "")
        in selected_leads.union(selected_sources)
    ]
    discovery_count = sum(
        1
        for item in selected_content_items
        if str(item.get("artifactKind") or "") == "discovery"
    )
    evidence_count = sum(
        1
        for item in selected_content_items
        if str(item.get("artifactKind") or "") == "evidence"
    )
    return {
        "sessionDir": payload["sessionDir"],
        "contentDir": payload["contentDir"],
        "manifestPath": payload["manifestPath"],
        "focus": query,
        "matched": True,
        "empty": False,
        "nextStep": "",
        "root": root,
        "anchors": seeds[1:],
        "matchedCount": len(seeds),
        "neighbors": ordered_neighbors,
        "sources": selected_source_items,
        "content": selected_content_items,
        "sourceEdges": selected_source_edges,
        "revisionEdges": selected_revision_edges,
        "leadSources": selected_lead_items,
        "leadEdges": selected_lead_edges,
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "contentCount": len(selected_content_items),
        "sourceCount": len(selected_source_items),
        "sourceEdgeCount": len(selected_source_edges),
        "revisionEdgeCount": len(selected_revision_edges),
        "leadSourceCount": len(selected_lead_items),
        "leadEdgeCount": len(selected_lead_edges),
        "collisionCount": 0,
        "collisions": [],
        "duplicateMaterializationCount": 0,
        "duplicateMaterializations": [],
        "variantCount": 0,
        "variants": [],
    }


def _analysis_overview_payload(
    lineage: dict[str, object],
    semantic: dict[str, object],
    *,
    limit: int,
) -> dict[str, object]:
    node_groups = Counter(
        str(node.get("group") or "")
        for node in semantic.get("nodes") or []
        if str(node.get("group") or "")
    )
    node_kinds = Counter(
        str(node.get("kind") or "")
        for node in semantic.get("nodes") or []
        if str(node.get("kind") or "")
    )
    edge_labels = Counter(
        str(edge.get("label") or "")
        for edge in semantic.get("edges") or []
        if str(edge.get("label") or "")
    )
    provider_clusters = [
        {"provider": provider, "nodeCount": count}
        for provider, count in node_groups.most_common(max(limit, 0))
        if provider != "content"
    ]
    dominant_kinds = [
        {"kind": kind, "nodeCount": count}
        for kind, count in node_kinds.most_common(max(limit, 0))
    ]
    dominant_relations = [
        {"label": label, "edgeCount": count}
        for label, count in edge_labels.most_common(max(limit, 0))
    ]
    structural_edge_count = sum(
        count
        for label, count in edge_labels.items()
        if label in {"source", "resource", "resolved_by", "query", "drives"}
    )
    source_node_count = sum(
        count
        for kind, count in node_kinds.items()
        if kind in {"source", "query", "provider"}
    )
    anchors = sorted(
        [dict(item) for item in lineage.get("content") or []],
        key=lambda item: (
            1 if str(item.get("artifactKind") or "") == "evidence" else 0,
            int(item.get("fetchCount") or 0),
            str(item.get("lastFetchedAt") or ""),
            str(item.get("preferredName") or ""),
        ),
        reverse=True,
    )[: max(limit, 0)]
    queries = [
        dict(node)
        for node in semantic.get("nodes") or []
        if str(node.get("kind") or "") == "query"
    ][: max(limit, 0)]
    lead_sources = [
        dict(item) for item in (lineage.get("leadSources") or [])[: max(limit, 0)]
    ]
    return {
        "sessionDir": lineage["sessionDir"],
        "contentDir": lineage["contentDir"],
        "manifestPath": lineage["manifestPath"],
        "contentCount": int(lineage.get("contentCount") or 0),
        "sourceCount": int(lineage.get("sourceCount") or 0),
        "leadSourceCount": int(lineage.get("leadSourceCount") or 0),
        "leadEdgeCount": int(lineage.get("leadEdgeCount") or 0),
        "semanticNodeCount": int(semantic.get("nodeCount") or 0),
        "semanticEdgeCount": int(semantic.get("edgeCount") or 0),
        "discoveryArtifactCount": int(lineage.get("discoveryArtifactCount") or 0),
        "evidenceArtifactCount": int(lineage.get("evidenceArtifactCount") or 0),
        "nextStep": str(lineage.get("nextStep") or semantic.get("nextStep") or ""),
        "providerClusters": provider_clusters,
        "dominantKinds": dominant_kinds,
        "dominantRelations": dominant_relations,
        "materializedAnchors": anchors,
        "querySeeds": queries,
        "bestLeads": lead_sources,
        "sourceHeavy": source_node_count * 2
        >= max(int(semantic.get("nodeCount") or 0), 1),
        "structuralHeavy": structural_edge_count * 2
        >= max(int(semantic.get("edgeCount") or 0), 1),
        "sourceNodeCount": source_node_count,
        "structuralEdgeCount": structural_edge_count,
    }


def _render_analysis_overview_text(payload: dict[str, object]) -> str:
    lines = [
        f"session: {payload['sessionDir']}",
        f"content: {payload['contentDir']}",
        (
            "artifacts: "
            f"{payload['contentCount']} "
            f"(discovery {payload['discoveryArtifactCount']}, "
            f"evidence {payload['evidenceArtifactCount']})"
        ),
        (
            "graph: "
            f"{payload['sourceCount']} sources, "
            f"{payload['leadSourceCount']} lead sources, "
            f"{payload['leadEdgeCount']} lead edges"
        ),
        (
            "semantic: "
            f"{payload['semanticNodeCount']} nodes, "
            f"{payload['semanticEdgeCount']} edges"
        ),
    ]
    shape_parts: list[str] = []
    if bool(payload.get("sourceHeavy")):
        shape_parts.append(
            f"source-heavy ({int(payload['sourceNodeCount'])}/{int(payload['semanticNodeCount'])} source/query/provider nodes)"
        )
    if bool(payload.get("structuralHeavy")):
        shape_parts.append(
            f"structural-edge-heavy ({int(payload['structuralEdgeCount'])}/{int(payload['semanticEdgeCount'])} structural edges)"
        )
    if shape_parts:
        lines.append("shape: " + "; ".join(shape_parts))
    if payload.get("nextStep"):
        lines.append(f"next: {payload['nextStep']}")
    if payload["providerClusters"]:
        lines.append("provider clusters:")
        for cluster in payload["providerClusters"]:
            lines.append(f"  - {cluster['provider']}: {cluster['nodeCount']} nodes")
    if payload["dominantKinds"]:
        lines.append("dominant node kinds:")
        for item in payload["dominantKinds"]:
            lines.append(f"  - {item['kind']}: {item['nodeCount']}")
    if payload["dominantRelations"]:
        lines.append("dominant relations:")
        for item in payload["dominantRelations"]:
            lines.append(f"  - {item['label']}: {item['edgeCount']}")
    if payload["materializedAnchors"]:
        lines.append("materialized anchors:")
        for anchor in payload["materializedAnchors"]:
            providers = ", ".join(str(value) for value in anchor.get("providers") or [])
            visibility = _visibility_summary(anchor)
            lines.append(
                f"  - [{anchor.get('artifactKind') or 'artifact'}] {anchor['preferredName']}"
            )
            if providers:
                lines.append(f"    providers: {providers}")
            if visibility:
                lines.append(f"    visibility: {visibility}")
            lines.append(f"    follow: `{anchor['followCommand']}`")
    if payload["querySeeds"]:
        lines.append("query seeds:")
        for node in payload["querySeeds"]:
            lines.append(f"  - {node['label']}")
    if payload["bestLeads"]:
        lines.append("best leads:")
        for lead in payload["bestLeads"]:
            relation = ", ".join(
                str(value) for value in lead.get("relationKinds") or [] if str(value)
            )
            lines.append(
                f"  - [{'; '.join(_lead_signal_labels(lead, aggregated=True))}] "
                f"{lead['locator']} ({lead['provider']}, {relation or 'lead'})"
            )
            lines.append(f"    follow: `{lead['followCommand']}`")
    lines.append(
        "focus: use `gotta session analyze --focus <locator|keyword> --session <session>` "
        "to inspect one local neighborhood instead of dumping the full graph."
    )
    return "\n".join(lines)


def _render_lineage_overview_text(
    payload: dict[str, object],
    *,
    limit: int,
) -> str:
    lines = [
        f"session: {payload['sessionDir']}",
        f"content: {payload['contentDir']}",
        (
            "artifacts: "
            f"{payload['contentCount']} "
            f"(discovery {payload['discoveryArtifactCount']}, "
            f"evidence {payload['evidenceArtifactCount']})"
        ),
        (
            "graph: "
            f"{payload['sourceCount']} sources, "
            f"{payload['leadSourceCount']} lead sources, "
            f"{payload['leadEdgeCount']} lead edges"
        ),
    ]
    if payload.get("nextStep"):
        lines.append(f"next: {payload['nextStep']}")
    sources = [dict(item) for item in (payload.get("sources") or [])[: max(limit, 0)]]
    if sources:
        lines.append("materialized sources:")
        for source in sources:
            lines.append(
                f"  - {source['locator']} "
                f"({int(source.get('contentCount') or 0)} materializations)"
            )
    if payload["leadSources"]:
        lines.append("best leads:")
        for lead in (payload.get("leadSources") or [])[: max(limit, 0)]:
            relation = ", ".join(
                str(value) for value in lead.get("relationKinds") or [] if str(value)
            )
            lines.append(
                f"  - [{'; '.join(_lead_signal_labels(lead, aggregated=True))}] "
                f"{lead['locator']} ({lead['provider']}, {relation or 'lead'})"
            )
            lines.append(f"    follow: `{lead['followCommand']}`")
    return "\n".join(lines)


def _render_semantic_overview_text(payload: dict[str, object]) -> str:
    lines = [
        f"session: {payload['sessionDir']}",
        f"content: {payload['contentDir']}",
        (
            "artifacts: "
            f"{payload['contentCount']} "
            f"(discovery {payload['discoveryArtifactCount']}, "
            f"evidence {payload['evidenceArtifactCount']})"
        ),
        (
            "semantic: "
            f"{payload['semanticNodeCount']} nodes, "
            f"{payload['semanticEdgeCount']} edges"
        ),
    ]
    shape_parts: list[str] = []
    if bool(payload.get("sourceHeavy")):
        shape_parts.append(
            f"source-heavy ({int(payload['sourceNodeCount'])}/{int(payload['semanticNodeCount'])} source/query/provider nodes)"
        )
    if bool(payload.get("structuralHeavy")):
        shape_parts.append(
            f"structural-edge-heavy ({int(payload['structuralEdgeCount'])}/{int(payload['semanticEdgeCount'])} structural edges)"
        )
    if shape_parts:
        lines.append("shape: " + "; ".join(shape_parts))
    if payload.get("nextStep"):
        lines.append(f"next: {payload['nextStep']}")
    if payload["providerClusters"]:
        lines.append("provider clusters:")
        for cluster in payload["providerClusters"]:
            lines.append(f"  - {cluster['provider']}: {cluster['nodeCount']} nodes")
    if payload["dominantKinds"]:
        lines.append("dominant node kinds:")
        for item in payload["dominantKinds"]:
            lines.append(f"  - {item['kind']}: {item['nodeCount']}")
    if payload["dominantRelations"]:
        lines.append("dominant relations:")
        for item in payload["dominantRelations"]:
            lines.append(f"  - {item['label']}: {item['edgeCount']}")
    if payload["querySeeds"]:
        lines.append("query seeds:")
        for node in payload["querySeeds"]:
            lines.append(f"  - {node['label']}")
    return "\n".join(lines)


def _render_analysis_focus_text(payload: dict[str, object]) -> str:
    lines = [
        f"session: {payload['sessionDir']}",
        f"focus: {payload['focus'] or '(empty)'}",
    ]
    if not payload.get("matched"):
        if payload.get("nextStep"):
            lines.append(f"next: {payload['nextStep']}")
        return "\n".join(lines)
    root = payload["root"]
    lines.append(f"matched: {root['label']} ({root['kind']}, {root['group']})")
    if int(payload.get("matchedCount") or 0) > 1:
        lines.append(
            f"signal: {int(payload['matchedCount'])} anchors matched this focus; "
            "showing the strongest root plus nearby corroborating anchors"
        )
    state_bits = []
    if root.get("artifactKind"):
        state_bits.append(f"artifact_kind={root['artifactKind']}")
    if bool(root.get("materialized")):
        state_bits.append("materialized")
    if bool(root.get("discovered")) and not bool(root.get("materialized")):
        state_bits.append("discovered-only")
    if state_bits:
        lines.append("state: " + ", ".join(state_bits))
    if root.get("followCommand"):
        lines.append(f"follow: `{root['followCommand']}`")
    anchors = payload.get("anchors") or []
    if anchors:
        lines.append("also matched:")
        for anchor in anchors:
            lines.append(f"  - {anchor['label']} ({anchor['kind']}, {anchor['group']})")
            if anchor.get("followCommand"):
                lines.append(f"    follow: `{anchor['followCommand']}`")
    if int(payload.get("suppressedStructuralEdgeCount") or 0) > 0:
        lines.append(
            "signal: "
            f"suppressed {payload['suppressedStructuralEdgeCount']} lower-signal structural edges "
            "to keep this neighborhood readable"
        )
    if payload["neighbors"]:
        lines.append("neighbors:")
        for neighbor in payload["neighbors"]:
            relation = ", ".join(
                str(value) for value in neighbor.get("relations") or [] if str(value)
            )
            lines.append(
                f"  - {neighbor['label']} ({neighbor['kind']}, {neighbor['group']}; {relation or 'adjacent'})"
            )
            bits = []
            if neighbor.get("artifactKind"):
                bits.append(f"artifact_kind={neighbor['artifactKind']}")
            if bool(neighbor.get("materialized")):
                bits.append("materialized")
            if bool(neighbor.get("discovered")) and not bool(
                neighbor.get("materialized")
            ):
                bits.append("discovered-only")
            if bits:
                lines.append("    state: " + ", ".join(bits))
            if neighbor.get("followCommand"):
                lines.append(f"    follow: `{neighbor['followCommand']}`")
    else:
        lines.append("neighbors: none")
    return "\n".join(lines)


def _render_lineage_focus_text(payload: dict[str, object]) -> str:
    lines = [
        f"session: {payload['sessionDir']}",
        f"focus: {payload['focus'] or '(empty)'}",
    ]
    if not payload.get("matched"):
        if payload.get("nextStep"):
            lines.append(f"next: {payload['nextStep']}")
        return "\n".join(lines)
    root = payload["root"]
    lines.append(f"matched: {root['label']} ({root['kind']})")
    if int(payload.get("matchedCount") or 0) > 1:
        lines.append(
            f"signal: {int(payload['matchedCount'])} anchors matched this focus; "
            "showing the strongest root plus nearby corroborating anchors"
        )
    state_bits = []
    if root.get("artifactKind"):
        state_bits.append(f"artifact_kind={root['artifactKind']}")
    if bool(root.get("materialized")):
        state_bits.append("materialized")
    else:
        state_bits.append("discovered-only")
    if state_bits:
        lines.append("state: " + ", ".join(state_bits))
    if root.get("followCommand"):
        lines.append(f"follow: `{root['followCommand']}`")
    anchors = payload.get("anchors") or []
    if anchors:
        lines.append("also matched:")
        for anchor in anchors:
            lines.append(f"  - {anchor['label']} ({anchor['kind']})")
            if anchor.get("followCommand"):
                lines.append(f"    follow: `{anchor['followCommand']}`")
    if payload["neighbors"]:
        lines.append("neighbors:")
        for neighbor in payload["neighbors"]:
            lines.append(
                f"  - {neighbor['label']} ({neighbor['kind']}; {neighbor['relation']})"
            )
            bits = []
            if neighbor.get("artifactKind"):
                bits.append(f"artifact_kind={neighbor['artifactKind']}")
            bits.append(
                "materialized"
                if bool(neighbor.get("materialized"))
                else "discovered-only"
            )
            lines.append("    state: " + ", ".join(bits))
            if neighbor.get("followCommand"):
                lines.append(f"    follow: `{neighbor['followCommand']}`")
    else:
        lines.append("neighbors: none")
    return "\n".join(lines)


def _render_text_bundle(sections: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    for title, body in sections:
        body_text = str(body).strip()
        if not body_text:
            continue
        if lines:
            lines.append("")
        lines.append(f"## {title}")
        lines.append("")
        lines.append(body_text)
    return "\n".join(lines)


def _render_markdown_list(lines: list[str], heading: str, entries: list[str]) -> None:
    if not entries:
        return
    lines.extend([f"## {heading}", ""])
    lines.extend([f"- {entry}" for entry in entries])
    lines.append("")


def _render_lineage_overview_markdown(payload: dict[str, object], *, limit: int) -> str:
    lines = [
        "# gotta session analyze",
        "",
        f"Session: `{payload['sessionDir']}`",
        "",
        (
            f"Artifacts: {payload['contentCount']} total "
            f"(discovery {payload['discoveryArtifactCount']}, "
            f"evidence {payload['evidenceArtifactCount']})"
        ),
        (
            f"Lineage: {payload['sourceCount']} sources, "
            f"{payload['leadSourceCount']} lead sources, "
            f"{payload['leadEdgeCount']} lead edges"
        ),
        "",
    ]
    next_step = str(payload.get("nextStep") or "").strip()
    if next_step:
        lines.extend(["## Synthesis", "", next_step, ""])
    sources = [
        f"{source['locator']} ({int(source.get('contentCount') or 0)} materializations)"
        for source in list(payload.get("sources") or [])[: max(limit, 0)]
    ]
    _render_markdown_list(lines, "Materialized Sources", sources)
    leads_preview = []
    for lead in list(payload.get("leadSources") or [])[: max(limit, 0)]:
        relation = ", ".join(
            str(value) for value in lead.get("relationKinds") or [] if str(value)
        )
        label = (
            f"[{'; '.join(_lead_signal_labels(lead, aggregated=True))}] "
            f"{lead['locator']} ({lead['provider']}, {relation or 'lead'})"
        )
        follow = str(lead.get("followCommand") or "").strip()
        if follow:
            label += f" via `{follow}`"
        leads_preview.append(label)
    _render_markdown_list(lines, "Lead Preview", leads_preview)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def _render_semantic_overview_markdown(payload: dict[str, object]) -> str:
    lines = [
        "# gotta session analyze",
        "",
        f"Session: `{payload['sessionDir']}`",
        "",
        (
            f"Artifacts: {payload['contentCount']} total "
            f"(discovery {payload['discoveryArtifactCount']}, "
            f"evidence {payload['evidenceArtifactCount']})"
        ),
        (
            f"Semantic: {payload['semanticNodeCount']} nodes, "
            f"{payload['semanticEdgeCount']} edges"
        ),
        "",
    ]
    shape_parts: list[str] = []
    if bool(payload.get("sourceHeavy")):
        shape_parts.append(
            f"source-heavy ({int(payload['sourceNodeCount'])}/{int(payload['semanticNodeCount'])} source/query/provider nodes)"
        )
    if bool(payload.get("structuralHeavy")):
        shape_parts.append(
            f"structural-edge-heavy ({int(payload['structuralEdgeCount'])}/{int(payload['semanticEdgeCount'])} structural edges)"
        )
    if shape_parts:
        lines.extend(["## Shape", "", "; ".join(shape_parts), ""])
    next_step = str(payload.get("nextStep") or "").strip()
    if next_step:
        lines.extend(["## Synthesis", "", next_step, ""])
    provider_clusters = [
        f"{cluster['provider']}: {cluster['nodeCount']} nodes"
        for cluster in list(payload.get("providerClusters") or [])[
            :SUMMARY_BUCKET_LIMIT
        ]
    ]
    _render_markdown_list(lines, "Provider Clusters", provider_clusters)
    dominant_kinds = [
        f"{item['kind']}: {item['nodeCount']}"
        for item in list(payload.get("dominantKinds") or [])[:SUMMARY_BUCKET_LIMIT]
    ]
    _render_markdown_list(lines, "Dominant Node Kinds", dominant_kinds)
    dominant_relations = [
        f"{item['label']}: {item['edgeCount']} edges"
        for item in list(payload.get("dominantRelations") or [])[:SUMMARY_BUCKET_LIMIT]
    ]
    _render_markdown_list(lines, "Dominant Relations", dominant_relations)
    query_seeds = [
        str(node.get("label") or "").strip()
        for node in list(payload.get("querySeeds") or [])[:SUMMARY_BUCKET_LIMIT]
        if str(node.get("label") or "").strip()
    ]
    _render_markdown_list(lines, "Query Seeds", query_seeds)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def _render_lineage_focus_markdown_section(payload: dict[str, object]) -> list[str]:
    lines: list[str] = ["## Lineage Focus", ""]
    if not payload.get("matched"):
        lines.append("- Match: none")
        next_step = str(payload.get("nextStep") or "").strip()
        if next_step:
            lines.append(f"- Next: {next_step}")
        lines.append("")
        return lines
    root = payload["root"]
    lines.append(f"- Matched: `{root['label']}` ({root['kind']})")
    if int(payload.get("matchedCount") or 0) > 1:
        lines.append(
            f"- Signal: {int(payload['matchedCount'])} anchors matched this focus; showing the strongest root plus nearby corroborating anchors"
        )
    state_bits = []
    if root.get("artifactKind"):
        state_bits.append(f"artifact_kind={root['artifactKind']}")
    state_bits.append(
        "materialized" if bool(root.get("materialized")) else "discovered-only"
    )
    if state_bits:
        lines.append(f"- State: {', '.join(state_bits)}")
    follow = str(root.get("followCommand") or "").strip()
    if follow:
        lines.append(f"- Follow: `{follow}`")
    anchors = list(payload.get("anchors") or [])[:ANALYZE_FOCUS_MATCH_PREVIEW_LIMIT]
    if anchors:
        lines.extend(["", "### Also Matched", ""])
        for anchor in anchors:
            entry = f"`{anchor['label']}` ({anchor['kind']})"
            follow = str(anchor.get("followCommand") or "").strip()
            if follow:
                entry += f" via `{follow}`"
            lines.append(f"- {entry}")
        hidden_anchors = max(
            int(len(payload.get("anchors") or [])) - len(anchors),
            0,
        )
        if hidden_anchors > 0:
            lines.append(f"- ... {hidden_anchors} additional matched anchors hidden")
    neighbors = list(payload.get("neighbors") or [])[
        :ANALYZE_FOCUS_NEIGHBOR_PREVIEW_LIMIT
    ]
    lines.extend(["", "### Neighbors", ""])
    if neighbors:
        for neighbor in neighbors:
            bits = []
            if neighbor.get("artifactKind"):
                bits.append(f"artifact_kind={neighbor['artifactKind']}")
            bits.append(
                "materialized"
                if bool(neighbor.get("materialized"))
                else "discovered-only"
            )
            entry = (
                f"`{neighbor['label']}` ({neighbor['kind']}; {neighbor['relation']}; "
                f"{', '.join(bits)})"
            )
            follow = str(neighbor.get("followCommand") or "").strip()
            if follow:
                entry += f" via `{follow}`"
            lines.append(f"- {entry}")
        hidden_neighbors = max(
            int(len(payload.get("neighbors") or [])) - len(neighbors),
            0,
        )
        if hidden_neighbors > 0:
            lines.append(f"- ... {hidden_neighbors} additional neighbors hidden")
    else:
        lines.append("- None")
    lines.append("")
    return lines


def _render_semantic_focus_markdown_section(payload: dict[str, object]) -> list[str]:
    lines: list[str] = ["## Semantic Focus", ""]
    if not payload.get("matched"):
        lines.append("- Match: none")
        next_step = str(payload.get("nextStep") or "").strip()
        if next_step:
            lines.append(f"- Next: {next_step}")
        lines.append("")
        return lines
    root = payload["root"]
    lines.append(f"- Matched: `{root['label']}` ({root['kind']}, {root['group']})")
    if int(payload.get("matchedCount") or 0) > 1:
        lines.append(
            f"- Signal: {int(payload['matchedCount'])} anchors matched this focus; showing the strongest root plus nearby corroborating anchors"
        )
    state_bits = []
    if root.get("artifactKind"):
        state_bits.append(f"artifact_kind={root['artifactKind']}")
    if bool(root.get("materialized")):
        state_bits.append("materialized")
    if bool(root.get("discovered")) and not bool(root.get("materialized")):
        state_bits.append("discovered-only")
    if state_bits:
        lines.append(f"- State: {', '.join(state_bits)}")
    follow = str(root.get("followCommand") or "").strip()
    if follow:
        lines.append(f"- Follow: `{follow}`")
    anchors = list(payload.get("anchors") or [])[:ANALYZE_FOCUS_MATCH_PREVIEW_LIMIT]
    if anchors:
        lines.extend(["", "### Also Matched", ""])
        for anchor in anchors:
            entry = f"`{anchor['label']}` ({anchor['kind']}, {anchor['group']})"
            follow = str(anchor.get("followCommand") or "").strip()
            if follow:
                entry += f" via `{follow}`"
            lines.append(f"- {entry}")
        hidden_anchors = max(
            int(len(payload.get("anchors") or [])) - len(anchors),
            0,
        )
        if hidden_anchors > 0:
            lines.append(f"- ... {hidden_anchors} additional matched anchors hidden")
    suppressed = int(payload.get("suppressedStructuralEdgeCount") or 0)
    if suppressed > 0:
        lines.append("")
        lines.append(
            f"- Signal: suppressed {suppressed} lower-signal structural edges to keep this neighborhood readable"
        )
    neighbors = list(payload.get("neighbors") or [])[
        :ANALYZE_FOCUS_NEIGHBOR_PREVIEW_LIMIT
    ]
    lines.extend(["", "### Neighbors", ""])
    if neighbors:
        for neighbor in neighbors:
            relation = ", ".join(
                str(value) for value in neighbor.get("relations") or [] if str(value)
            )
            bits = []
            if neighbor.get("artifactKind"):
                bits.append(f"artifact_kind={neighbor['artifactKind']}")
            if bool(neighbor.get("materialized")):
                bits.append("materialized")
            if bool(neighbor.get("discovered")) and not bool(
                neighbor.get("materialized")
            ):
                bits.append("discovered-only")
            entry = (
                f"`{neighbor['label']}` ({neighbor['kind']}, {neighbor['group']}; "
                f"{relation or 'adjacent'}"
            )
            if bits:
                entry += f"; {', '.join(bits)}"
            entry += ")"
            follow = str(neighbor.get("followCommand") or "").strip()
            if follow:
                entry += f" via `{follow}`"
            lines.append(f"- {entry}")
        hidden_neighbors = max(
            int(len(payload.get("neighbors") or [])) - len(neighbors),
            0,
        )
        if hidden_neighbors > 0:
            lines.append(f"- ... {hidden_neighbors} additional neighbors hidden")
    else:
        lines.append("- None")
    lines.append("")
    return lines


def _render_combined_focus_markdown(
    *, lineage: dict[str, object], semantic: dict[str, object]
) -> str:
    focus = str(lineage.get("focus") or semantic.get("focus") or "").strip()
    lines = [
        "# gotta session analyze",
        "",
        f"Session: `{lineage['sessionDir']}`",
        "",
        f"Focus: `{focus or '(empty)'}`",
        "",
    ]
    lines.extend(_render_lineage_focus_markdown_section(lineage))
    lines.extend(_render_semantic_focus_markdown_section(semantic))
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def _render_single_focus_markdown(
    *,
    session_dir: str,
    focus: str,
    section_lines: list[str],
) -> str:
    lines = [
        "# gotta session analyze",
        "",
        f"Session: `{session_dir}`",
        "",
        f"Focus: `{focus or '(empty)'}`",
        "",
    ]
    lines.extend(section_lines)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def _render_analysis_overview_markdown(
    overview: dict[str, object],
    *,
    lineage: dict[str, object],
    limit: int,
) -> str:
    lines = [
        "# gotta session analyze",
        "",
        f"Session: `{overview['sessionDir']}`",
        "",
        (
            f"Artifacts: {overview['contentCount']} total "
            f"(discovery {overview['discoveryArtifactCount']}, "
            f"evidence {overview['evidenceArtifactCount']})"
        ),
        (
            f"Lineage: {overview['sourceCount']} sources, "
            f"{overview['leadSourceCount']} lead sources, "
            f"{overview['leadEdgeCount']} lead edges"
        ),
        (
            f"Semantic: {overview['semanticNodeCount']} nodes, "
            f"{overview['semanticEdgeCount']} edges"
        ),
        "",
    ]
    next_step = str(overview.get("nextStep") or "").strip()
    if next_step:
        lines.extend(["## Synthesis", "", next_step, ""])
    provider_clusters = [
        f"{item['provider']}: {item['nodeCount']} nodes"
        for item in list(overview.get("providerClusters") or [])[:SUMMARY_BUCKET_LIMIT]
    ]
    _render_markdown_list(lines, "Provider Clusters", provider_clusters)
    dominant_relations = [
        f"{item['label']}: {item['edgeCount']} edges"
        for item in list(overview.get("dominantRelations") or [])[:SUMMARY_BUCKET_LIMIT]
    ]
    _render_markdown_list(lines, "Dominant Relations", dominant_relations)
    anchors = []
    for anchor in list(overview.get("materializedAnchors") or [])[
        : max(limit, ANALYZE_ANCHOR_PREVIEW_LIMIT)
    ]:
        follow = str(anchor.get("followCommand") or "").strip()
        if follow:
            anchors.append(
                f"[{anchor.get('artifactKind') or 'artifact'}] {anchor['preferredName']} via `{follow}`"
            )
        else:
            anchors.append(
                f"[{anchor.get('artifactKind') or 'artifact'}] {anchor['preferredName']}"
            )
    _render_markdown_list(lines, "Anchor Shortlist", anchors)
    lineage_preview = [
        f"{item['locator']} ({int(item.get('contentCount') or 0)} materializations)"
        for item in list(lineage.get("sources") or [])[
            : max(limit, ANALYZE_ANCHOR_PREVIEW_LIMIT)
        ]
    ]
    _render_markdown_list(lines, "Lineage Preview", lineage_preview)
    semantic_preview = [
        f"{item['label']} ({item['kind']})"
        for item in list(overview.get("querySeeds") or [])[
            : max(limit, ANALYZE_ANCHOR_PREVIEW_LIMIT)
        ]
    ]
    _render_markdown_list(lines, "Semantic Preview", semantic_preview)
    leads_preview = []
    for lead in list(overview.get("bestLeads") or [])[
        : max(limit, ANALYZE_ANCHOR_PREVIEW_LIMIT)
    ]:
        follow = str(lead.get("followCommand") or "").strip()
        relation = ", ".join(
            str(value) for value in lead.get("relationKinds") or [] if str(value)
        )
        label = f"{lead['locator']} ({lead['provider']}, {relation or 'lead'})"
        if follow:
            label += f" via `{follow}`"
        leads_preview.append(label)
    _render_markdown_list(lines, "Lead Preview", leads_preview)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def _render_markdown_bundle(
    sections: list[tuple[str, str]],
    *,
    title: str = "gotta session analyze",
) -> str:
    lines = [f"# {title}", ""]
    for index, (section_title, body) in enumerate(sections):
        graph_text = str(body).rstrip()
        if index:
            lines.append("")
        lines.append(f"## {section_title}")
        lines.append("")
        lines.append("```mermaid")
        lines.append(graph_text)
        lines.append("```")
    return "\n".join(lines)


def _combined_analysis_payload(
    *,
    focus: str,
    lineage: dict[str, object],
    semantic: dict[str, object],
) -> dict[str, object]:
    return {
        "mode": "all",
        "focus": focus,
        "lineage": lineage,
        "semantic": semantic,
    }


def cmd_analyze(args: argparse.Namespace) -> int:
    dirs = _session_dirs_for_read(args)
    _require_started_session(dirs)
    session_ref = _explicit_session_ref(args)
    payload = _analysis_payload(dirs, session_ref=session_ref)
    mermaid = _render_analysis_mermaid(payload)
    semantic_payload = _semantic_payload(dirs, session_ref=session_ref)
    semantic_mermaid = _render_semantic_mermaid(semantic_payload)
    focus_query = str(getattr(args, "focus", "") or "").strip()
    focus_limit = max(int(getattr(args, "limit", 8) or 0), 0)
    overview = _analysis_overview_payload(
        payload,
        semantic_payload,
        limit=focus_limit,
    )
    lineage_focus_payload = None
    semantic_focus_payload = None
    if focus_query:
        focus_scan_payload = _scan_payload(
            dirs,
            query=focus_query,
            limit=max(focus_limit * 2, 12),
            include_all=True,
            session_ref=session_ref,
        )
        lineage_focus_payload = _lineage_focus_payload(
            payload,
            focus=focus_query,
            limit=focus_limit,
            scan_payload=focus_scan_payload,
        )
        semantic_focus_payload = _semantic_focus_payload(
            payload,
            semantic_payload,
            focus=focus_query,
            limit=focus_limit,
            scan_payload=focus_scan_payload,
        )

    if args.output == "text":
        if args.mode == "lineage":
            print(
                _render_lineage_focus_text(lineage_focus_payload)
                if lineage_focus_payload is not None
                else _render_lineage_overview_text(payload, limit=focus_limit)
            )
        elif args.mode == "semantic":
            print(
                _render_analysis_focus_text(semantic_focus_payload)
                if semantic_focus_payload is not None
                else _render_semantic_overview_text(overview)
            )
        elif focus_query:
            print(
                _render_text_bundle(
                    [
                        ("Lineage", _render_lineage_focus_text(lineage_focus_payload)),
                        (
                            "Semantic",
                            _render_analysis_focus_text(semantic_focus_payload),
                        ),
                    ]
                )
            )
        else:
            print(_render_analysis_overview_text(overview))
    elif args.output == "json":
        if args.mode == "lineage":
            print(
                json.dumps(
                    lineage_focus_payload
                    if lineage_focus_payload is not None
                    else payload,
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.mode == "semantic":
            print(
                json.dumps(
                    semantic_focus_payload
                    if semantic_focus_payload is not None
                    else semantic_payload,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(
                json.dumps(
                    _combined_analysis_payload(
                        focus=focus_query,
                        lineage=(
                            lineage_focus_payload
                            if lineage_focus_payload is not None
                            else payload
                        ),
                        semantic=(
                            semantic_focus_payload
                            if semantic_focus_payload is not None
                            else semantic_payload
                        ),
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
    elif args.output == "markdown":
        if args.mode == "lineage":
            print(
                _render_single_focus_markdown(
                    session_dir=str(lineage_focus_payload["sessionDir"]),
                    focus=str(lineage_focus_payload.get("focus") or ""),
                    section_lines=_render_lineage_focus_markdown_section(
                        lineage_focus_payload
                    ),
                )
                if lineage_focus_payload is not None
                else _render_lineage_overview_markdown(payload, limit=focus_limit),
                end="",
            )
        elif args.mode == "semantic":
            print(
                _render_single_focus_markdown(
                    session_dir=str(semantic_focus_payload["sessionDir"]),
                    focus=str(semantic_focus_payload.get("focus") or ""),
                    section_lines=_render_semantic_focus_markdown_section(
                        semantic_focus_payload
                    ),
                )
                if semantic_focus_payload is not None
                else _render_semantic_overview_markdown(overview),
                end="",
            )
        elif focus_query:
            print(
                _render_combined_focus_markdown(
                    lineage=lineage_focus_payload,
                    semantic=semantic_focus_payload,
                ),
                end="",
            )
        else:
            print(
                _render_analysis_overview_markdown(
                    overview,
                    lineage=payload,
                    limit=focus_limit,
                ),
                end="",
            )
    else:
        if args.mode == "lineage":
            print(
                _render_analysis_mermaid(lineage_focus_payload)
                if lineage_focus_payload is not None
                else mermaid
            )
        else:
            print(
                _render_semantic_mermaid(semantic_focus_payload)
                if semantic_focus_payload is not None
                else semantic_mermaid
            )
    return 0


analysis_payload = _analysis_payload
semantic_payload = _semantic_payload
render_analysis_mermaid = _render_analysis_mermaid
render_semantic_mermaid = _render_semantic_mermaid
combined_analysis_payload = _combined_analysis_payload
analysis_overview_payload = _analysis_overview_payload
lineage_focus_payload = _lineage_focus_payload
semantic_focus_payload = _semantic_focus_payload
