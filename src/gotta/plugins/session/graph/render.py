"""Text rendering for `gotta session graph`."""

from __future__ import annotations

from ..core import (
    GRAPH_TEXT_PREVIEW_LIMIT,
    append_count_section,
    append_preview_heading,
    filter_suffix,
)
from .model import GraphContent, GraphEdge, GraphPayload, GraphSource


def _preview_source_lines(source: GraphSource) -> list[str]:
    bits = [f"{source['contentCount']} content"]
    if source["artifactKinds"]:
        bits.append(",".join(source["artifactKinds"]))
    if source["variant"]:
        bits.append(f"variants {source['variantCount']}")
    return [
        f"  - {source['locator']} ({'; '.join(bits)})",
        f"    follow: `{source['followCommand']}`",
    ]


def _preview_content_lines(item: GraphContent) -> list[str]:
    bits = [item["checksum"][:12], f"{item['sourceCount']} sources"]
    if item["artifactKind"]:
        bits.append(item["artifactKind"])
    stored_parts = [
        part
        for part in (
            f"`{item['artifactLocator']}`" if item["artifactLocator"] else "",
            f"`{item['contentLocator']}`" if item["contentLocator"] else "",
        )
        if part
    ]
    return [
        f"  - {item['preferredName']} ({'; '.join(bits)})",
        "    stored: " + ", ".join(stored_parts),
    ]


def _preview_edge_lines(edge: GraphEdge) -> list[str]:
    label = (
        edge["plugin"] if edge["count"] <= 1 else f"{edge['plugin']} x{edge['count']}"
    )
    return [f"  - {edge['source']} -> {edge['checksum'][:12]} ({label})"]


def render_graph_text(payload: GraphPayload) -> str:
    lines = [
        f"session: {payload['sessionDir']}",
        f"manifest: {payload['manifestPath']}",
        (
            "graph: "
            f"{payload['sourceCount']} sources, "
            f"{payload['contentCount']} content nodes, "
            f"{payload['edgeCount']} edges"
            f"{filter_suffix(payload['filter'])}"
        ),
        (
            "artifacts: "
            f"discovery {payload['discoveryArtifactCount']}, "
            f"evidence {payload['evidenceArtifactCount']}"
        ),
    ]
    if payload["nextStep"]:
        lines.append(f"next: {payload['nextStep']}")
    append_count_section(
        lines,
        heading="top providers",
        records=payload["topProviders"],
        key="provider",
    )
    append_count_section(
        lines,
        heading="top artifact kinds",
        records=payload["topArtifactKinds"],
        key="artifactKind",
    )
    if payload["sources"]:
        preview_sources = payload["sources"][:GRAPH_TEXT_PREVIEW_LIMIT]
        append_preview_heading(
            lines,
            heading="strongest sources",
            shown=len(preview_sources),
            total=len(payload["sources"]),
        )
        for source in preview_sources:
            lines.extend(_preview_source_lines(source))
        if len(payload["sources"]) > len(preview_sources):
            lines.append(
                "  - ... "
                f"{len(payload['sources']) - len(preview_sources)} "
                "additional sources hidden in text view"
            )
    if payload["content"]:
        preview_content = payload["content"][:GRAPH_TEXT_PREVIEW_LIMIT]
        append_preview_heading(
            lines,
            heading="materialized anchors",
            shown=len(preview_content),
            total=len(payload["content"]),
        )
        for item in preview_content:
            lines.extend(_preview_content_lines(item))
    if payload["edges"]:
        preview_edges = payload["edges"][:GRAPH_TEXT_PREVIEW_LIMIT]
        append_preview_heading(
            lines,
            heading="sample edges",
            shown=len(preview_edges),
            total=len(payload["edges"]),
        )
        for edge in preview_edges:
            lines.extend(_preview_edge_lines(edge))
        if len(payload["edges"]) > len(preview_edges):
            lines.append(
                "  - ... "
                f"{len(payload['edges']) - len(preview_edges)} "
                "additional edges hidden in text view"
            )
    return "\n".join(lines)
