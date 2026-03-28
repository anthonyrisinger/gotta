"""Renderers for session analysis payloads."""

from __future__ import annotations

from typing import Any

from ..core import (
    ANALYZE_ANCHOR_PREVIEW_LIMIT,
    ANALYZE_FOCUS_MATCH_PREVIEW_LIMIT,
    ANALYZE_FOCUS_NEIGHBOR_PREVIEW_LIMIT,
    SUMMARY_BUCKET_LIMIT,
    analysis_mermaid_id,
    empty_topology_next_step,
    lead_signal_labels,
    mermaid_label,
    visibility_summary,
)


def render_semantic_mermaid(payload: dict[str, Any]) -> str:
    lines = [
        "---",
        "title: gotta semantic graph",
        "---",
        "flowchart LR",
    ]
    if payload.get("empty"):
        lines.append(
            f'  empty["{mermaid_label(str(payload.get("nextStep") or empty_topology_next_step()))}"]'
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
        lines.append(f'  note["{mermaid_label(str(payload["nextStep"]))}"]')
        lines.append("  class note emptyState")
    for node in payload["nodes"]:
        node_id = analysis_mermaid_id("sem", str(node["id"]))
        label = mermaid_label(str(node["label"]))
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
        source_id = analysis_mermaid_id("sem", str(edge["source"]))
        target_id = analysis_mermaid_id("sem", str(edge["target"]))
        label = mermaid_label(str(edge["label"]))
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


def render_analysis_mermaid(payload: dict[str, Any]) -> str:
    lines = [
        "---",
        "title: gotta session analysis",
        "---",
        "flowchart LR",
    ]
    if payload.get("empty"):
        lines.append(
            f'  empty["{mermaid_label(str(payload.get("nextStep") or empty_topology_next_step()))}"]'
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
        lines.append(f'  note["{mermaid_label(str(payload["nextStep"]))}"]')
        lines.append("  class note emptyState")
    for source in payload["sources"]:
        locator = str(source["locator"])
        node_id = analysis_mermaid_id("src", locator)
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
        label = mermaid_label("\n".join(label_parts))
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
        label = mermaid_label("\n".join(label_parts))
        node_id = analysis_mermaid_id("art", checksum)
        lines.append(f'  {node_id}["{label}"]')
        lines.append(f"  class {node_id} content")
    for edge in payload["sourceEdges"]:
        source_id = analysis_mermaid_id("src", str(edge["source"]))
        content_id = analysis_mermaid_id("art", str(edge["checksum"]))
        label_parts = [", ".join(edge["plugins"])]
        actors = ", ".join(str(value) for value in edge.get("actors") or [])
        if actors:
            label_parts.append(f"actor: {actors}")
        label = mermaid_label("\n".join(part for part in label_parts if part))
        lines.append(f"  {source_id} -->|{label}| {content_id}")
    for edge in payload["revisionEdges"]:
        from_id = analysis_mermaid_id("art", str(edge["from"]))
        to_id = analysis_mermaid_id("art", str(edge["to"]))
        label = mermaid_label(
            f"revision:{str(edge['locator'])}\n{str(edge.get('rendering') or '')}".rstrip()
        )
        lines.append(f"  {from_id} -->|{label}| {to_id}")
    seen_source_nodes = {str(source["locator"]) for source in payload["sources"]}
    for lead_source in payload.get("leadSources") or []:
        locator = str(lead_source["locator"])
        if locator in seen_source_nodes:
            continue
        node_id = analysis_mermaid_id("src", locator)
        label_parts = [locator, f"lead: {str(lead_source['provider'])}"]
        if not bool(lead_source.get("materialized")):
            label_parts.append("not yet materialized")
        label = mermaid_label("\n".join(label_parts))
        lines.append(f'  {node_id}["{label}"]')
        lines.append(f"  class {node_id} leadgap")
    for edge in payload.get("leadEdges") or []:
        content_id = analysis_mermaid_id("art", str(edge["sourceChecksum"]))
        source_id = analysis_mermaid_id("src", str(edge["targetLocator"]))
        relation = str(edge.get("relation") or "links_to")
        count = int(edge.get("occurrenceCount") or 0)
        label = relation if count <= 1 else f"{relation} x{count}"
        lines.append(f"  {content_id} -.->|{mermaid_label(label)}| {source_id}")
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


def render_analysis_overview_text(payload: dict[str, Any]) -> str:
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
            visibility = visibility_summary(anchor)
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
                f"  - [{'; '.join(lead_signal_labels(lead, aggregated=True))}] "
                f"{lead['locator']} ({lead['provider']}, {relation or 'lead'})"
            )
            lines.append(f"    follow: `{lead['followCommand']}`")
    lines.append(
        "focus: use `gotta session analyze --focus <locator|keyword> --session <session>` "
        "to inspect one local neighborhood instead of dumping the full graph."
    )
    return "\n".join(lines)


def render_lineage_overview_text(
    payload: dict[str, Any],
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
                f"  - [{'; '.join(lead_signal_labels(lead, aggregated=True))}] "
                f"{lead['locator']} ({lead['provider']}, {relation or 'lead'})"
            )
            lines.append(f"    follow: `{lead['followCommand']}`")
    return "\n".join(lines)


def render_semantic_overview_text(payload: dict[str, Any]) -> str:
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


def render_analysis_focus_text(payload: dict[str, Any]) -> str:
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


def render_lineage_focus_text(payload: dict[str, Any]) -> str:
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


def render_text_bundle(sections: list[tuple[str, str]]) -> str:
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


def render_lineage_overview_markdown(payload: dict[str, Any], *, limit: int) -> str:
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
            f"[{'; '.join(lead_signal_labels(lead, aggregated=True))}] "
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


def render_semantic_overview_markdown(payload: dict[str, Any]) -> str:
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


def render_lineage_focus_markdown_section(payload: dict[str, Any]) -> list[str]:
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


def render_semantic_focus_markdown_section(payload: dict[str, Any]) -> list[str]:
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


def render_combined_focus_markdown(
    *,
    lineage: dict[str, Any],
    semantic: dict[str, Any],
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
    lines.extend(render_lineage_focus_markdown_section(lineage))
    lines.extend(render_semantic_focus_markdown_section(semantic))
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def render_single_focus_markdown(
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


def render_analysis_overview_markdown(
    overview: dict[str, Any],
    *,
    lineage: dict[str, Any],
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


def render_markdown_bundle(
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
