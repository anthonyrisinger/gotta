"""Mermaid rendering for `gotta session graph`."""

from __future__ import annotations

from ..core import empty_topology_next_step, mermaid_id, mermaid_label
from .model import GraphPayload


def render_mermaid(payload: GraphPayload) -> str:
    lines = [
        "---",
        "title: gotta content graph",
        "---",
        "flowchart LR",
    ]
    if payload["empty"]:
        lines.append(
            f'  empty["{mermaid_label(payload["nextStep"] or empty_topology_next_step())}"]'
        )
        lines.extend(
            [
                "  class empty emptyState",
                "  classDef emptyState fill:#f7fafc,stroke:#4a5568,color:#1a202c;",
                "",
            ]
        )
        return "\n".join(lines)
    if payload["nextStep"]:
        lines.append(f'  note["{mermaid_label(payload["nextStep"])}"]')
        lines.append("  class note emptyState")
    for source in payload["sources"]:
        locator = source["locator"]
        node_id = mermaid_id("src", locator)
        label_parts = [locator]
        if source["variant"]:
            label_parts.append(f"renderings: {source['variantCount']}")
        label = mermaid_label("\n".join(label_parts))
        lines.append(f'  {node_id}["{label}"]')
        if source["collision"]:
            lines.append(f"  class {node_id} collision")
        elif source["variant"]:
            lines.append(f"  class {node_id} variant")
        else:
            lines.append(f"  class {node_id} source")
    for item in payload["content"]:
        checksum = item["checksum"]
        node_id = mermaid_id("art", checksum)
        label = mermaid_label(f"{item['preferredName']}\n{checksum[:12]}")
        lines.append(f'  {node_id}["{label}"]')
        if item["collision"]:
            lines.append(f"  class {node_id} collision")
        else:
            lines.append(f"  class {node_id} content")
    for edge in payload["edges"]:
        source_id = mermaid_id("src", edge["source"])
        content_id = mermaid_id("art", edge["checksum"])
        label = (
            edge["plugin"]
            if edge["count"] == 1
            else f"{edge['plugin']} x{edge['count']}"
        )
        lines.append(f"  {source_id} -->|{label}| {content_id}")
    lines.extend(
        [
            "  classDef source fill:#eef8ee,stroke:#2d7a2d,color:#173d17;",
            "  classDef content fill:#eef4ff,stroke:#2a62c7,color:#173058;",
            "  classDef variant fill:#fff7e6,stroke:#b7791f,color:#5a3b09;",
            "  classDef collision fill:#fff1f1,stroke:#c73434,color:#6b1111;",
            "  classDef emptyState fill:#f7fafc,stroke:#4a5568,color:#1a202c;",
            "",
        ]
    )
    return "\n".join(lines)
