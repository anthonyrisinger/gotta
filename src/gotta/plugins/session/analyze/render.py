"""Renderers for session analysis payloads."""

from __future__ import annotations

from typing import cast

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
from .model import (
    AnalysisOverviewPayload,
    LineageFocusPayload,
    LineagePayload,
    SemanticFocusPayload,
    SemanticPayload,
)


def _mapping(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _mapping_list_field(item: object, key: str) -> list[dict[str, object]]:
    value = _mapping(item).get(key)
    if not isinstance(value, list):
        return []
    return [_mapping(entry) for entry in value]


def _text_field(item: object, key: str) -> str:
    value = _mapping(item).get(key)
    return str(value or "")


def _bool_field(item: object, key: str) -> bool:
    return bool(_mapping(item).get(key))


def _int_field(item: object, key: str) -> int:
    value = _mapping(item).get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        return 0


def _string_list_field(item: object, key: str) -> list[str]:
    value = _mapping(item).get(key)
    if not isinstance(value, list):
        return []
    return [str(entry) for entry in value if str(entry)]


def render_semantic_mermaid(payload: SemanticPayload | SemanticFocusPayload) -> str:
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
        lines.append(f'  note["{mermaid_label(_text_field(payload, "nextStep"))}"]')
        lines.append("  class note emptyState")
    for node in _mapping_list_field(payload, "nodes"):
        node_id = analysis_mermaid_id("sem", _text_field(node, "id"))
        label = mermaid_label(_text_field(node, "label"))
        kind = _text_field(node, "kind").replace("-", "_")
        if (
            _text_field(node, "kind") == "source"
            and not _bool_field(node, "materialized")
            and _bool_field(node, "discovered")
        ):
            kind = "source_gap"
        lines.append(f'  {node_id}["{label}"]')
        lines.append(f"  class {node_id} {kind}")
    for edge in _mapping_list_field(payload, "edges"):
        source_id = analysis_mermaid_id("sem", _text_field(edge, "source"))
        target_id = analysis_mermaid_id("sem", _text_field(edge, "target"))
        label = mermaid_label(_text_field(edge, "label"))
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


def render_analysis_mermaid(payload: LineagePayload | LineageFocusPayload) -> str:
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
        lines.append(f'  note["{mermaid_label(_text_field(payload, "nextStep"))}"]')
        lines.append("  class note emptyState")
    for source in _mapping_list_field(payload, "sources"):
        locator = _text_field(source, "locator")
        node_id = analysis_mermaid_id("src", locator)
        actors = ", ".join(_string_list_field(source, "actors"))
        label_parts = [locator]
        if actors:
            label_parts.append(f"actor: {actors}")
        if _bool_field(source, "variant"):
            label_parts.append(f"renderings: {_int_field(source, 'variantCount')}")
        elif _bool_field(source, "duplicateMaterialization"):
            label_parts.append(
                f"materializations: {_int_field(source, 'contentCount')}"
            )
        label = mermaid_label("\n".join(label_parts))
        lines.append(f'  {node_id}["{label}"]')
        if _bool_field(source, "collision"):
            lines.append(f"  class {node_id} collision")
        elif _bool_field(source, "variant"):
            lines.append(f"  class {node_id} variant")
        elif _bool_field(source, "duplicateMaterialization"):
            lines.append(f"  class {node_id} duplicate")
        else:
            lines.append(f"  class {node_id} source")
    for content in _mapping_list_field(payload, "content"):
        checksum = _text_field(content, "checksum")
        providers = ", ".join(_string_list_field(content, "providers"))
        actors = ", ".join(_string_list_field(content, "actors"))
        label_parts = [_text_field(content, "preferredName")]
        resource_hints = [
            value for value in _string_list_field(content, "resourceHints") if value
        ]
        if _bool_field(content, "nameCollision") and resource_hints:
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
    for edge in _mapping_list_field(payload, "sourceEdges"):
        source_id = analysis_mermaid_id("src", _text_field(edge, "source"))
        content_id = analysis_mermaid_id("art", _text_field(edge, "checksum"))
        label_parts = [", ".join(_string_list_field(edge, "plugins"))]
        actors = ", ".join(_string_list_field(edge, "actors"))
        if actors:
            label_parts.append(f"actor: {actors}")
        label = mermaid_label("\n".join(part for part in label_parts if part))
        lines.append(f"  {source_id} -->|{label}| {content_id}")
    for edge in _mapping_list_field(payload, "revisionEdges"):
        from_id = analysis_mermaid_id("art", _text_field(edge, "from"))
        to_id = analysis_mermaid_id("art", _text_field(edge, "to"))
        label = mermaid_label(
            f"revision:{_text_field(edge, 'locator')}\n{_text_field(edge, 'rendering')}".rstrip()
        )
        lines.append(f"  {from_id} -->|{label}| {to_id}")
    seen_source_nodes = {
        _text_field(source, "locator")
        for source in _mapping_list_field(payload, "sources")
    }
    for lead_source in _mapping_list_field(payload, "leadSources"):
        locator = _text_field(lead_source, "locator")
        if locator in seen_source_nodes:
            continue
        node_id = analysis_mermaid_id("src", locator)
        label_parts = [locator, f"lead: {_text_field(lead_source, 'provider')}"]
        if not _bool_field(lead_source, "materialized"):
            label_parts.append("not yet materialized")
        label = mermaid_label("\n".join(label_parts))
        lines.append(f'  {node_id}["{label}"]')
        lines.append(f"  class {node_id} leadgap")
    for edge in _mapping_list_field(payload, "leadEdges"):
        content_id = analysis_mermaid_id("art", _text_field(edge, "sourceChecksum"))
        source_id = analysis_mermaid_id("src", _text_field(edge, "targetLocator"))
        relation = _text_field(edge, "relation") or "links_to"
        count = _int_field(edge, "occurrenceCount")
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


def render_analysis_overview_text(payload: AnalysisOverviewPayload) -> str:
    lines = [
        f"session: {_text_field(payload, 'sessionDir')}",
        f"content: {_text_field(payload, 'contentDir')}",
        (
            "artifacts: "
            f"{_int_field(payload, 'contentCount')} "
            f"(discovery {_int_field(payload, 'discoveryArtifactCount')}, "
            f"evidence {_int_field(payload, 'evidenceArtifactCount')})"
        ),
        (
            "graph: "
            f"{_int_field(payload, 'sourceCount')} sources, "
            f"{_int_field(payload, 'leadSourceCount')} lead sources, "
            f"{_int_field(payload, 'leadEdgeCount')} lead edges"
        ),
        (
            "semantic: "
            f"{_int_field(payload, 'semanticNodeCount')} nodes, "
            f"{_int_field(payload, 'semanticEdgeCount')} edges"
        ),
    ]
    shape_parts: list[str] = []
    if bool(payload.get("sourceHeavy")):
        shape_parts.append(
            f"source-heavy ({_int_field(payload, 'sourceNodeCount')}/{_int_field(payload, 'semanticNodeCount')} source/query/provider nodes)"
        )
    if bool(payload.get("structuralHeavy")):
        shape_parts.append(
            f"structural-edge-heavy ({_int_field(payload, 'structuralEdgeCount')}/{_int_field(payload, 'semanticEdgeCount')} structural edges)"
        )
    if shape_parts:
        lines.append("shape: " + "; ".join(shape_parts))
    if payload.get("nextStep"):
        lines.append(f"next: {_text_field(payload, 'nextStep')}")
    provider_clusters = _mapping_list_field(payload, "providerClusters")
    if provider_clusters:
        lines.append("provider clusters:")
        for cluster in provider_clusters:
            lines.append(
                f"  - {_text_field(cluster, 'provider')}: {_int_field(cluster, 'nodeCount')} nodes"
            )
    dominant_kinds = _mapping_list_field(payload, "dominantKinds")
    if dominant_kinds:
        lines.append("dominant node kinds:")
        for item in dominant_kinds:
            lines.append(
                f"  - {_text_field(item, 'kind')}: {_int_field(item, 'nodeCount')}"
            )
    dominant_relations = _mapping_list_field(payload, "dominantRelations")
    if dominant_relations:
        lines.append("dominant relations:")
        for item in dominant_relations:
            lines.append(
                f"  - {_text_field(item, 'label')}: {_int_field(item, 'edgeCount')}"
            )
    anchors = _mapping_list_field(payload, "materializedAnchors")
    if anchors:
        lines.append("materialized anchors:")
        for anchor in anchors:
            providers = ", ".join(_string_list_field(anchor, "providers"))
            visibility = visibility_summary(anchor)
            lines.append(
                f"  - [{_text_field(anchor, 'artifactKind') or 'artifact'}] {_text_field(anchor, 'preferredName')}"
            )
            if providers:
                lines.append(f"    providers: {providers}")
            if visibility:
                lines.append(f"    visibility: {visibility}")
            lines.append(f"    follow: `{_text_field(anchor, 'followCommand')}`")
    query_seeds = _mapping_list_field(payload, "querySeeds")
    if query_seeds:
        lines.append("query seeds:")
        for node in query_seeds:
            lines.append(f"  - {_text_field(node, 'label')}")
    best_leads = _mapping_list_field(payload, "bestLeads")
    if best_leads:
        lines.append("best leads:")
        for lead in best_leads:
            relation = ", ".join(_string_list_field(lead, "relationKinds"))
            lines.append(
                f"  - [{'; '.join(lead_signal_labels(lead, aggregated=True))}] "
                f"{_text_field(lead, 'locator')} ({_text_field(lead, 'provider')}, {relation or 'lead'})"
            )
            lines.append(f"    follow: `{_text_field(lead, 'followCommand')}`")
    lines.append(
        "focus: use `gotta session analyze --focus <locator|keyword> --session <session>` "
        "to inspect one local neighborhood instead of dumping the full graph."
    )
    return "\n".join(lines)


def render_lineage_overview_text(
    payload: LineagePayload,
    *,
    limit: int,
) -> str:
    lines = [
        f"session: {_text_field(payload, 'sessionDir')}",
        f"content: {_text_field(payload, 'contentDir')}",
        (
            "artifacts: "
            f"{_int_field(payload, 'contentCount')} "
            f"(discovery {_int_field(payload, 'discoveryArtifactCount')}, "
            f"evidence {_int_field(payload, 'evidenceArtifactCount')})"
        ),
        (
            "graph: "
            f"{_int_field(payload, 'sourceCount')} sources, "
            f"{_int_field(payload, 'leadSourceCount')} lead sources, "
            f"{_int_field(payload, 'leadEdgeCount')} lead edges"
        ),
    ]
    if payload.get("nextStep"):
        lines.append(f"next: {_text_field(payload, 'nextStep')}")
    sources = _mapping_list_field(payload, "sources")[: max(limit, 0)]
    if sources:
        lines.append("materialized sources:")
        for source in sources:
            lines.append(
                f"  - {_text_field(source, 'locator')} "
                f"({_int_field(source, 'contentCount')} materializations)"
            )
    lead_sources = _mapping_list_field(payload, "leadSources")
    if lead_sources:
        lines.append("best leads:")
        for lead in lead_sources[: max(limit, 0)]:
            relation = ", ".join(_string_list_field(lead, "relationKinds"))
            lines.append(
                f"  - [{'; '.join(lead_signal_labels(lead, aggregated=True))}] "
                f"{_text_field(lead, 'locator')} ({_text_field(lead, 'provider')}, {relation or 'lead'})"
            )
            lines.append(f"    follow: `{_text_field(lead, 'followCommand')}`")
    return "\n".join(lines)


def render_semantic_overview_text(payload: AnalysisOverviewPayload) -> str:
    lines = [
        f"session: {_text_field(payload, 'sessionDir')}",
        f"content: {_text_field(payload, 'contentDir')}",
        (
            "artifacts: "
            f"{_int_field(payload, 'contentCount')} "
            f"(discovery {_int_field(payload, 'discoveryArtifactCount')}, "
            f"evidence {_int_field(payload, 'evidenceArtifactCount')})"
        ),
        (
            "semantic: "
            f"{_int_field(payload, 'semanticNodeCount')} nodes, "
            f"{_int_field(payload, 'semanticEdgeCount')} edges"
        ),
    ]
    shape_parts: list[str] = []
    if bool(payload.get("sourceHeavy")):
        shape_parts.append(
            f"source-heavy ({_int_field(payload, 'sourceNodeCount')}/{_int_field(payload, 'semanticNodeCount')} source/query/provider nodes)"
        )
    if bool(payload.get("structuralHeavy")):
        shape_parts.append(
            f"structural-edge-heavy ({_int_field(payload, 'structuralEdgeCount')}/{_int_field(payload, 'semanticEdgeCount')} structural edges)"
        )
    if shape_parts:
        lines.append("shape: " + "; ".join(shape_parts))
    if payload.get("nextStep"):
        lines.append(f"next: {_text_field(payload, 'nextStep')}")
    provider_clusters = _mapping_list_field(payload, "providerClusters")
    if provider_clusters:
        lines.append("provider clusters:")
        for cluster in provider_clusters:
            lines.append(
                f"  - {_text_field(cluster, 'provider')}: {_int_field(cluster, 'nodeCount')} nodes"
            )
    dominant_kinds = _mapping_list_field(payload, "dominantKinds")
    if dominant_kinds:
        lines.append("dominant node kinds:")
        for item in dominant_kinds:
            lines.append(
                f"  - {_text_field(item, 'kind')}: {_int_field(item, 'nodeCount')}"
            )
    dominant_relations = _mapping_list_field(payload, "dominantRelations")
    if dominant_relations:
        lines.append("dominant relations:")
        for item in dominant_relations:
            lines.append(
                f"  - {_text_field(item, 'label')}: {_int_field(item, 'edgeCount')}"
            )
    query_seeds = _mapping_list_field(payload, "querySeeds")
    if query_seeds:
        lines.append("query seeds:")
        for node in query_seeds:
            lines.append(f"  - {_text_field(node, 'label')}")
    return "\n".join(lines)


def render_analysis_focus_text(payload: SemanticFocusPayload) -> str:
    lines = [
        f"session: {_text_field(payload, 'sessionDir')}",
        f"focus: {_text_field(payload, 'focus') or '(empty)'}",
    ]
    if not payload.get("matched"):
        if payload.get("nextStep"):
            lines.append(f"next: {_text_field(payload, 'nextStep')}")
        return "\n".join(lines)
    root = _mapping(payload.get("root"))
    lines.append(
        f"matched: {_text_field(root, 'label')} ({_text_field(root, 'kind')}, {_text_field(root, 'group')})"
    )
    if _int_field(payload, "matchedCount") > 1:
        lines.append(
            f"signal: {_int_field(payload, 'matchedCount')} anchors matched this focus; "
            "showing the strongest root plus nearby corroborating anchors"
        )
    state_bits = []
    if _text_field(root, "artifactKind"):
        state_bits.append(f"artifact_kind={_text_field(root, 'artifactKind')}")
    if _bool_field(root, "materialized"):
        state_bits.append("materialized")
    if _bool_field(root, "discovered") and not _bool_field(root, "materialized"):
        state_bits.append("discovered-only")
    if state_bits:
        lines.append("state: " + ", ".join(state_bits))
    if _text_field(root, "followCommand"):
        lines.append(f"follow: `{_text_field(root, 'followCommand')}`")
    anchors = _mapping_list_field(payload, "anchors")
    if anchors:
        lines.append("also matched:")
        for anchor in anchors:
            lines.append(
                f"  - {_text_field(anchor, 'label')} ({_text_field(anchor, 'kind')}, {_text_field(anchor, 'group')})"
            )
            if _text_field(anchor, "followCommand"):
                lines.append(f"    follow: `{_text_field(anchor, 'followCommand')}`")
    if _int_field(payload, "suppressedStructuralEdgeCount") > 0:
        lines.append(
            "signal: "
            f"suppressed {_int_field(payload, 'suppressedStructuralEdgeCount')} lower-signal structural edges "
            "to keep this neighborhood readable"
        )
    neighbors = _mapping_list_field(payload, "neighbors")
    if neighbors:
        lines.append("neighbors:")
        for neighbor in neighbors:
            relation = ", ".join(_string_list_field(neighbor, "relations"))
            lines.append(
                f"  - {_text_field(neighbor, 'label')} ({_text_field(neighbor, 'kind')}, {_text_field(neighbor, 'group')}; {relation or 'adjacent'})"
            )
            bits = []
            if _text_field(neighbor, "artifactKind"):
                bits.append(f"artifact_kind={_text_field(neighbor, 'artifactKind')}")
            if _bool_field(neighbor, "materialized"):
                bits.append("materialized")
            if _bool_field(neighbor, "discovered") and not _bool_field(
                neighbor, "materialized"
            ):
                bits.append("discovered-only")
            if bits:
                lines.append("    state: " + ", ".join(bits))
            if _text_field(neighbor, "followCommand"):
                lines.append(f"    follow: `{_text_field(neighbor, 'followCommand')}`")
    else:
        lines.append("neighbors: none")
    return "\n".join(lines)


def render_lineage_focus_text(payload: LineageFocusPayload) -> str:
    lines = [
        f"session: {_text_field(payload, 'sessionDir')}",
        f"focus: {_text_field(payload, 'focus') or '(empty)'}",
    ]
    if not payload.get("matched"):
        if payload.get("nextStep"):
            lines.append(f"next: {_text_field(payload, 'nextStep')}")
        return "\n".join(lines)
    root = _mapping(payload.get("root"))
    lines.append(f"matched: {_text_field(root, 'label')} ({_text_field(root, 'kind')})")
    if _int_field(payload, "matchedCount") > 1:
        lines.append(
            f"signal: {_int_field(payload, 'matchedCount')} anchors matched this focus; "
            "showing the strongest root plus nearby corroborating anchors"
        )
    state_bits = []
    if _text_field(root, "artifactKind"):
        state_bits.append(f"artifact_kind={_text_field(root, 'artifactKind')}")
    if _bool_field(root, "materialized"):
        state_bits.append("materialized")
    else:
        state_bits.append("discovered-only")
    if state_bits:
        lines.append("state: " + ", ".join(state_bits))
    if _text_field(root, "followCommand"):
        lines.append(f"follow: `{_text_field(root, 'followCommand')}`")
    anchors = _mapping_list_field(payload, "anchors")
    if anchors:
        lines.append("also matched:")
        for anchor in anchors:
            lines.append(
                f"  - {_text_field(anchor, 'label')} ({_text_field(anchor, 'kind')})"
            )
            if _text_field(anchor, "followCommand"):
                lines.append(f"    follow: `{_text_field(anchor, 'followCommand')}`")
    neighbors = _mapping_list_field(payload, "neighbors")
    if neighbors:
        lines.append("neighbors:")
        for neighbor in neighbors:
            lines.append(
                f"  - {_text_field(neighbor, 'label')} ({_text_field(neighbor, 'kind')}; {_text_field(neighbor, 'relation')})"
            )
            bits = []
            if _text_field(neighbor, "artifactKind"):
                bits.append(f"artifact_kind={_text_field(neighbor, 'artifactKind')}")
            bits.append(
                "materialized"
                if _bool_field(neighbor, "materialized")
                else "discovered-only"
            )
            lines.append("    state: " + ", ".join(bits))
            if _text_field(neighbor, "followCommand"):
                lines.append(f"    follow: `{_text_field(neighbor, 'followCommand')}`")
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


def render_lineage_overview_markdown(payload: LineagePayload, *, limit: int) -> str:
    lines = [
        "# gotta session analyze",
        "",
        f"Session: `{_text_field(payload, 'sessionDir')}`",
        "",
        (
            f"Artifacts: {_int_field(payload, 'contentCount')} total "
            f"(discovery {_int_field(payload, 'discoveryArtifactCount')}, "
            f"evidence {_int_field(payload, 'evidenceArtifactCount')})"
        ),
        (
            f"Lineage: {_int_field(payload, 'sourceCount')} sources, "
            f"{_int_field(payload, 'leadSourceCount')} lead sources, "
            f"{_int_field(payload, 'leadEdgeCount')} lead edges"
        ),
        "",
    ]
    next_step = _text_field(payload, "nextStep").strip()
    if next_step:
        lines.extend(["## Synthesis", "", next_step, ""])
    sources = [
        f"{_text_field(source, 'locator')} ({_int_field(source, 'contentCount')} materializations)"
        for source in _mapping_list_field(payload, "sources")[: max(limit, 0)]
    ]
    _render_markdown_list(lines, "Materialized Sources", sources)
    leads_preview = []
    for lead in _mapping_list_field(payload, "leadSources")[: max(limit, 0)]:
        relation = ", ".join(_string_list_field(lead, "relationKinds"))
        label = (
            f"[{'; '.join(lead_signal_labels(lead, aggregated=True))}] "
            f"{_text_field(lead, 'locator')} ({_text_field(lead, 'provider')}, {relation or 'lead'})"
        )
        follow = _text_field(lead, "followCommand").strip()
        if follow:
            label += f" via `{follow}`"
        leads_preview.append(label)
    _render_markdown_list(lines, "Lead Preview", leads_preview)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def render_semantic_overview_markdown(payload: AnalysisOverviewPayload) -> str:
    lines = [
        "# gotta session analyze",
        "",
        f"Session: `{_text_field(payload, 'sessionDir')}`",
        "",
        (
            f"Artifacts: {_int_field(payload, 'contentCount')} total "
            f"(discovery {_int_field(payload, 'discoveryArtifactCount')}, "
            f"evidence {_int_field(payload, 'evidenceArtifactCount')})"
        ),
        (
            f"Semantic: {_int_field(payload, 'semanticNodeCount')} nodes, "
            f"{_int_field(payload, 'semanticEdgeCount')} edges"
        ),
        "",
    ]
    shape_parts: list[str] = []
    if bool(payload.get("sourceHeavy")):
        shape_parts.append(
            f"source-heavy ({_int_field(payload, 'sourceNodeCount')}/{_int_field(payload, 'semanticNodeCount')} source/query/provider nodes)"
        )
    if bool(payload.get("structuralHeavy")):
        shape_parts.append(
            f"structural-edge-heavy ({_int_field(payload, 'structuralEdgeCount')}/{_int_field(payload, 'semanticEdgeCount')} structural edges)"
        )
    if shape_parts:
        lines.extend(["## Shape", "", "; ".join(shape_parts), ""])
    next_step = _text_field(payload, "nextStep").strip()
    if next_step:
        lines.extend(["## Synthesis", "", next_step, ""])
    provider_clusters = [
        f"{_text_field(cluster, 'provider')}: {_int_field(cluster, 'nodeCount')} nodes"
        for cluster in _mapping_list_field(payload, "providerClusters")[
            :SUMMARY_BUCKET_LIMIT
        ]
    ]
    _render_markdown_list(lines, "Provider Clusters", provider_clusters)
    dominant_kinds = [
        f"{_text_field(item, 'kind')}: {_int_field(item, 'nodeCount')}"
        for item in _mapping_list_field(payload, "dominantKinds")[:SUMMARY_BUCKET_LIMIT]
    ]
    _render_markdown_list(lines, "Dominant Node Kinds", dominant_kinds)
    dominant_relations = [
        f"{_text_field(item, 'label')}: {_int_field(item, 'edgeCount')} edges"
        for item in _mapping_list_field(payload, "dominantRelations")[
            :SUMMARY_BUCKET_LIMIT
        ]
    ]
    _render_markdown_list(lines, "Dominant Relations", dominant_relations)
    query_seeds = [
        _text_field(node, "label").strip()
        for node in _mapping_list_field(payload, "querySeeds")[:SUMMARY_BUCKET_LIMIT]
        if _text_field(node, "label").strip()
    ]
    _render_markdown_list(lines, "Query Seeds", query_seeds)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def render_lineage_focus_markdown_section(payload: LineageFocusPayload) -> list[str]:
    lines: list[str] = ["## Lineage Focus", ""]
    if not payload.get("matched"):
        lines.append("- Match: none")
        next_step = str(payload.get("nextStep") or "").strip()
        if next_step:
            lines.append(f"- Next: {next_step}")
        lines.append("")
        return lines
    root = _mapping(payload.get("root"))
    lines.append(
        f"- Matched: `{_text_field(root, 'label')}` ({_text_field(root, 'kind')})"
    )
    if _int_field(payload, "matchedCount") > 1:
        lines.append(
            f"- Signal: {_int_field(payload, 'matchedCount')} anchors matched this focus; showing the strongest root plus nearby corroborating anchors"
        )
    state_bits = []
    if _text_field(root, "artifactKind"):
        state_bits.append(f"artifact_kind={_text_field(root, 'artifactKind')}")
    state_bits.append(
        "materialized" if _bool_field(root, "materialized") else "discovered-only"
    )
    if state_bits:
        lines.append(f"- State: {', '.join(state_bits)}")
    follow = _text_field(root, "followCommand").strip()
    if follow:
        lines.append(f"- Follow: `{follow}`")
    anchors = _mapping_list_field(payload, "anchors")[
        :ANALYZE_FOCUS_MATCH_PREVIEW_LIMIT
    ]
    if anchors:
        lines.extend(["", "### Also Matched", ""])
        for anchor in anchors:
            entry = f"`{_text_field(anchor, 'label')}` ({_text_field(anchor, 'kind')})"
            follow = _text_field(anchor, "followCommand").strip()
            if follow:
                entry += f" via `{follow}`"
            lines.append(f"- {entry}")
        hidden_anchors = max(
            len(_mapping_list_field(payload, "anchors")) - len(anchors),
            0,
        )
        if hidden_anchors > 0:
            lines.append(f"- ... {hidden_anchors} additional matched anchors hidden")
    neighbors = _mapping_list_field(payload, "neighbors")[
        :ANALYZE_FOCUS_NEIGHBOR_PREVIEW_LIMIT
    ]
    lines.extend(["", "### Neighbors", ""])
    if neighbors:
        for neighbor in neighbors:
            bits = []
            if neighbor.get("artifactKind"):
                bits.append(f"artifact_kind={_text_field(neighbor, 'artifactKind')}")
            bits.append(
                "materialized"
                if _bool_field(neighbor, "materialized")
                else "discovered-only"
            )
            entry = (
                f"`{_text_field(neighbor, 'label')}` ({_text_field(neighbor, 'kind')}; {_text_field(neighbor, 'relation')}; "
                f"{', '.join(bits)})"
            )
            follow = _text_field(neighbor, "followCommand").strip()
            if follow:
                entry += f" via `{follow}`"
            lines.append(f"- {entry}")
        hidden_neighbors = max(
            len(_mapping_list_field(payload, "neighbors")) - len(neighbors),
            0,
        )
        if hidden_neighbors > 0:
            lines.append(f"- ... {hidden_neighbors} additional neighbors hidden")
    else:
        lines.append("- None")
    lines.append("")
    return lines


def render_semantic_focus_markdown_section(payload: SemanticFocusPayload) -> list[str]:
    lines: list[str] = ["## Semantic Focus", ""]
    if not payload.get("matched"):
        lines.append("- Match: none")
        next_step = str(payload.get("nextStep") or "").strip()
        if next_step:
            lines.append(f"- Next: {next_step}")
        lines.append("")
        return lines
    root = _mapping(payload.get("root"))
    lines.append(
        f"- Matched: `{_text_field(root, 'label')}` ({_text_field(root, 'kind')}, {_text_field(root, 'group')})"
    )
    if _int_field(payload, "matchedCount") > 1:
        lines.append(
            f"- Signal: {_int_field(payload, 'matchedCount')} anchors matched this focus; showing the strongest root plus nearby corroborating anchors"
        )
    state_bits = []
    if _text_field(root, "artifactKind"):
        state_bits.append(f"artifact_kind={_text_field(root, 'artifactKind')}")
    if _bool_field(root, "materialized"):
        state_bits.append("materialized")
    if _bool_field(root, "discovered") and not _bool_field(root, "materialized"):
        state_bits.append("discovered-only")
    if state_bits:
        lines.append(f"- State: {', '.join(state_bits)}")
    follow = _text_field(root, "followCommand").strip()
    if follow:
        lines.append(f"- Follow: `{follow}`")
    anchors = _mapping_list_field(payload, "anchors")[
        :ANALYZE_FOCUS_MATCH_PREVIEW_LIMIT
    ]
    if anchors:
        lines.extend(["", "### Also Matched", ""])
        for anchor in anchors:
            entry = f"`{_text_field(anchor, 'label')}` ({_text_field(anchor, 'kind')}, {_text_field(anchor, 'group')})"
            follow = _text_field(anchor, "followCommand").strip()
            if follow:
                entry += f" via `{follow}`"
            lines.append(f"- {entry}")
        hidden_anchors = max(
            len(_mapping_list_field(payload, "anchors")) - len(anchors),
            0,
        )
        if hidden_anchors > 0:
            lines.append(f"- ... {hidden_anchors} additional matched anchors hidden")
    suppressed = _int_field(payload, "suppressedStructuralEdgeCount")
    if suppressed > 0:
        lines.append("")
        lines.append(
            f"- Signal: suppressed {suppressed} lower-signal structural edges to keep this neighborhood readable"
        )
    neighbors = _mapping_list_field(payload, "neighbors")[
        :ANALYZE_FOCUS_NEIGHBOR_PREVIEW_LIMIT
    ]
    lines.extend(["", "### Neighbors", ""])
    if neighbors:
        for neighbor in neighbors:
            relation = ", ".join(_string_list_field(neighbor, "relations"))
            bits = []
            if _text_field(neighbor, "artifactKind"):
                bits.append(f"artifact_kind={_text_field(neighbor, 'artifactKind')}")
            if _bool_field(neighbor, "materialized"):
                bits.append("materialized")
            if _bool_field(neighbor, "discovered") and not _bool_field(
                neighbor, "materialized"
            ):
                bits.append("discovered-only")
            entry = (
                f"`{_text_field(neighbor, 'label')}` ({_text_field(neighbor, 'kind')}, {_text_field(neighbor, 'group')}; "
                f"{relation or 'adjacent'}"
            )
            if bits:
                entry += f"; {', '.join(bits)}"
            entry += ")"
            follow = _text_field(neighbor, "followCommand").strip()
            if follow:
                entry += f" via `{follow}`"
            lines.append(f"- {entry}")
        hidden_neighbors = max(
            len(_mapping_list_field(payload, "neighbors")) - len(neighbors),
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
    lineage: LineageFocusPayload,
    semantic: SemanticFocusPayload,
) -> str:
    focus = str(lineage.get("focus") or semantic.get("focus") or "").strip()
    lines = [
        "# gotta session analyze",
        "",
        f"Session: `{_text_field(lineage, 'sessionDir')}`",
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
    overview: AnalysisOverviewPayload,
    *,
    lineage: LineagePayload,
    limit: int,
) -> str:
    lines = [
        "# gotta session analyze",
        "",
        f"Session: `{_text_field(overview, 'sessionDir')}`",
        "",
        (
            f"Artifacts: {_int_field(overview, 'contentCount')} total "
            f"(discovery {_int_field(overview, 'discoveryArtifactCount')}, "
            f"evidence {_int_field(overview, 'evidenceArtifactCount')})"
        ),
        (
            f"Lineage: {_int_field(overview, 'sourceCount')} sources, "
            f"{_int_field(overview, 'leadSourceCount')} lead sources, "
            f"{_int_field(overview, 'leadEdgeCount')} lead edges"
        ),
        (
            f"Semantic: {_int_field(overview, 'semanticNodeCount')} nodes, "
            f"{_int_field(overview, 'semanticEdgeCount')} edges"
        ),
        "",
    ]
    next_step = _text_field(overview, "nextStep").strip()
    if next_step:
        lines.extend(["## Synthesis", "", next_step, ""])
    provider_clusters = [
        f"{_text_field(item, 'provider')}: {_int_field(item, 'nodeCount')} nodes"
        for item in _mapping_list_field(overview, "providerClusters")[
            :SUMMARY_BUCKET_LIMIT
        ]
    ]
    _render_markdown_list(lines, "Provider Clusters", provider_clusters)
    dominant_relations = [
        f"{_text_field(item, 'label')}: {_int_field(item, 'edgeCount')} edges"
        for item in _mapping_list_field(overview, "dominantRelations")[
            :SUMMARY_BUCKET_LIMIT
        ]
    ]
    _render_markdown_list(lines, "Dominant Relations", dominant_relations)
    anchors = []
    for anchor in _mapping_list_field(overview, "materializedAnchors")[
        : max(limit, ANALYZE_ANCHOR_PREVIEW_LIMIT)
    ]:
        follow = _text_field(anchor, "followCommand").strip()
        if follow:
            anchors.append(
                f"[{_text_field(anchor, 'artifactKind') or 'artifact'}] {_text_field(anchor, 'preferredName')} via `{follow}`"
            )
        else:
            anchors.append(
                f"[{_text_field(anchor, 'artifactKind') or 'artifact'}] {_text_field(anchor, 'preferredName')}"
            )
    _render_markdown_list(lines, "Anchor Shortlist", anchors)
    lineage_preview = [
        f"{_text_field(item, 'locator')} ({_int_field(item, 'contentCount')} materializations)"
        for item in _mapping_list_field(lineage, "sources")[
            : max(limit, ANALYZE_ANCHOR_PREVIEW_LIMIT)
        ]
    ]
    _render_markdown_list(lines, "Lineage Preview", lineage_preview)
    semantic_preview = [
        f"{_text_field(item, 'label')} ({_text_field(item, 'kind')})"
        for item in _mapping_list_field(overview, "querySeeds")[
            : max(limit, ANALYZE_ANCHOR_PREVIEW_LIMIT)
        ]
    ]
    _render_markdown_list(lines, "Semantic Preview", semantic_preview)
    leads_preview = []
    for lead in _mapping_list_field(overview, "bestLeads")[
        : max(limit, ANALYZE_ANCHOR_PREVIEW_LIMIT)
    ]:
        follow = _text_field(lead, "followCommand").strip()
        relation = ", ".join(_string_list_field(lead, "relationKinds"))
        label = f"{_text_field(lead, 'locator')} ({_text_field(lead, 'provider')}, {relation or 'lead'})"
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
