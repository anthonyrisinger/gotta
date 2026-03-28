"""Graph surface for `gotta session`."""

from __future__ import annotations

import argparse
import json

from gotta.content.store import scan_content_store

from .core import (
    GRAPH_TEXT_PREVIEW_LIMIT,
    append_count_section,
    append_preview_heading,
    artifact_human_locator,
    artifact_kind,
    compile_filter_pattern,
    empty_topology_next_step,
    filter_suffix,
    follow_command,
    match_any,
    match_filter_text,
    mermaid_id,
    mermaid_label,
    provider_name,
    render_variant,
    render_variant_label,
    resolved_visibility_metadata,
    session_read_command,
    top_count_records,
    topology_next_step,
)
from .manifest import manifest_entries
from .parse import explicit_session_ref, require_started_session, session_dirs_for_read


def graph_payload(
    dirs,
    *,
    filter_query: str = "",
    session_ref: str = "",
) -> dict[str, object]:
    entries = manifest_entries(dirs)
    snapshot_by_digest = {
        snapshot.digest: snapshot for snapshot in scan_content_store(dirs.content_dir)
    }
    source_to_content: dict[str, set[str]] = {}
    content_to_sources: dict[str, set[str]] = {}
    edge_counts: dict[tuple[str, str, str], int] = {}
    content_names: dict[str, str] = {}
    source_variants: dict[str, set[tuple[str, str]]] = {}
    source_artifact_kinds: dict[str, set[str]] = {}
    source_visibility: dict[str, dict[str, object]] = {}
    source_plugins: dict[str, set[str]] = {}
    source_actors: dict[str, set[str]] = {}
    content_plugins: dict[str, set[str]] = {}
    content_actors: dict[str, set[str]] = {}
    for entry in entries:
        source = entry.get("canonical_locator") or entry.get("locator") or "unknown"
        checksum = entry.get("checksum") or ""
        plugin = entry.get("plugin") or "unknown"
        actor = str(entry.get("actor") or "").strip()
        if not checksum:
            continue
        source_to_content.setdefault(source, set()).add(checksum)
        content_to_sources.setdefault(checksum, set()).add(source)
        edge_key = (source, checksum, plugin)
        edge_counts[edge_key] = edge_counts.get(edge_key, 0) + 1
        content_names.setdefault(checksum, entry.get("preferred_name") or "data")
        kind = artifact_kind(entry.get("artifact_kind"))
        if kind:
            source_artifact_kinds.setdefault(source, set()).add(kind)
        source_plugins.setdefault(str(source), set()).add(str(plugin))
        if actor:
            source_actors.setdefault(str(source), set()).add(actor)
            content_actors.setdefault(str(checksum), set()).add(actor)
        content_plugins.setdefault(str(checksum), set()).add(str(plugin))
        source_visibility[source] = resolved_visibility_metadata(
            source_visibility.get(source, {}),
            provider=str(plugin),
            plugin=str(plugin),
            subcommand=str(entry.get("subcommand") or ""),
            locator=str(source),
        )
        snapshot = snapshot_by_digest.get(str(checksum))
        if snapshot is not None:
            source_variants.setdefault(source, set()).add(render_variant(snapshot))
    sources = [
        {
            "locator": locator,
            "followCommand": follow_command(locator, session_ref=session_ref),
            "contentCount": len(checksums),
            "artifactKind": (
                next(iter(source_artifact_kinds.get(locator, set())))
                if len(source_artifact_kinds.get(locator, set())) == 1
                else ""
            ),
            "artifactKinds": sorted(
                str(value) for value in source_artifact_kinds.get(locator, set())
            ),
            "collision": False,
            "variant": len(source_variants.get(locator, set())) > 1,
            "variantCount": len(source_variants.get(locator, set())),
            "variants": [
                render_variant_label(variant)
                for variant in sorted(source_variants.get(locator, set()))
            ],
            **source_visibility.get(locator, {}),
        }
        for locator, checksums in sorted(source_to_content.items())
    ]
    content = [
        {
            "checksum": checksum,
            "preferredName": content_names.get(checksum, "data"),
            "artifactKind": artifact_kind(
                snapshot_by_digest.get(checksum).metadata.get("artifact_kind")
            )
            if snapshot_by_digest.get(checksum) is not None
            else "",
            "contentLocator": f"content:{checksum}",
            "artifactLocator": artifact_human_locator(
                content_names.get(checksum, "data"), checksum
            ),
            "followCommand": session_read_command(
                artifact_human_locator(content_names.get(checksum, "data"), checksum),
                session_ref=session_ref,
            ),
            "sourceCount": len(locators),
            "collision": len(locators) > 1,
            **(
                resolved_visibility_metadata(
                    dict(snapshot_by_digest.get(checksum).metadata),
                    provider=str(
                        snapshot_by_digest.get(checksum).metadata.get("plugin") or ""
                    ),
                    plugin=str(
                        snapshot_by_digest.get(checksum).metadata.get("plugin") or ""
                    ),
                    subcommand=str(
                        snapshot_by_digest.get(checksum).metadata.get("subcommand")
                        or ""
                    ),
                    locator=str(next(iter(locators), "")),
                )
                if snapshot_by_digest.get(checksum) is not None
                else {}
            ),
        }
        for checksum, locators in sorted(content_to_sources.items())
    ]
    edges = [
        {
            "source": source,
            "checksum": checksum,
            "plugin": plugin,
            "count": count,
        }
        for (source, checksum, plugin), count in sorted(edge_counts.items())
    ]
    filter_text = match_filter_text(filter_query)
    filter_pattern = compile_filter_pattern(filter_text)
    if filter_pattern is not None:
        matched_sources = {
            str(source["locator"])
            for source in sources
            if match_any(
                filter_pattern,
                source["locator"],
                source.get("artifactKind"),
                source.get("artifactKinds"),
                source.get("variants"),
                source_plugins.get(str(source["locator"]), set()),
                source_actors.get(str(source["locator"]), set()),
            )
        }
        matched_content = {
            str(item["checksum"])
            for item in content
            if match_any(
                filter_pattern,
                item["checksum"],
                item["preferredName"],
                item["contentLocator"],
                item["artifactLocator"],
                item.get("artifactKind"),
                content_plugins.get(str(item["checksum"]), set()),
                content_actors.get(str(item["checksum"]), set()),
            )
        }
        matched_edges = {
            (str(edge["source"]), str(edge["checksum"]), str(edge["plugin"]))
            for edge in edges
            if match_any(
                filter_pattern,
                edge["source"],
                edge["checksum"],
                edge["plugin"],
                content_names.get(str(edge["checksum"]), ""),
            )
        }
        kept_sources = matched_sources.union(
            source for source, _checksum, _plugin in matched_edges
        )
        kept_content = matched_content.union(
            checksum for _source, checksum, _plugin in matched_edges
        )
        sources = [item for item in sources if str(item["locator"]) in kept_sources]
        content = [item for item in content if str(item["checksum"]) in kept_content]
        edges = [
            edge
            for edge in edges
            if str(edge["source"]) in kept_sources
            and str(edge["checksum"]) in kept_content
        ]
    empty = not sources and not content and not edges
    discovery_count = sum(
        1 for item in content if item.get("artifactKind") == "discovery"
    )
    evidence_count = sum(
        1 for item in content if item.get("artifactKind") == "evidence"
    )
    top_providers = top_count_records(
        [provider_name(str(item["locator"])) for item in sources],
        key="provider",
    )
    top_artifact_kinds = top_count_records(
        [str(item.get("artifactKind") or "").strip() for item in content],
        key="artifactKind",
    )
    next_step = topology_next_step(
        discovery_count=discovery_count,
        evidence_count=evidence_count,
    )
    if filter_text and empty:
        next_step = (
            f"No graph nodes matched {filter_text!r}. Clear `--filter` or choose a "
            "different locator, actor, plugin, or keyword."
        )
    return {
        "sessionDir": str(dirs.session_dir),
        "contentDir": str(dirs.content_dir),
        "manifestPath": str(dirs.content_dir / "manifest.jsonl"),
        "filter": filter_text,
        "sourceCount": len(sources),
        "contentCount": len(content),
        "edgeCount": len(edges),
        "discoveryArtifactCount": discovery_count,
        "evidenceArtifactCount": evidence_count,
        "topProviders": top_providers,
        "topArtifactKinds": top_artifact_kinds,
        "empty": empty,
        "nextStep": next_step,
        "sources": sources,
        "content": content,
        "edges": edges,
    }


def render_mermaid(payload: dict[str, object]) -> str:
    lines = [
        "---",
        "title: gotta content graph",
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
        node_id = mermaid_id("src", locator)
        label_parts = [locator]
        if source.get("variant"):
            label_parts.append(f"renderings: {int(source.get('variantCount') or 0)}")
        label = mermaid_label("\n".join(label_parts))
        lines.append(f'  {node_id}["{label}"]')
        if source["collision"]:
            lines.append(f"  class {node_id} collision")
        elif source.get("variant"):
            lines.append(f"  class {node_id} variant")
        else:
            lines.append(f"  class {node_id} source")
    for item in payload["content"]:
        checksum = str(item["checksum"])
        preferred = str(item["preferredName"])
        node_id = mermaid_id("art", checksum)
        short = checksum[:12]
        label = mermaid_label(f"{preferred}\n{short}")
        lines.append(f'  {node_id}["{label}"]')
        if item["collision"]:
            lines.append(f"  class {node_id} collision")
        else:
            lines.append(f"  class {node_id} content")
    for edge in payload["edges"]:
        source_id = mermaid_id("src", str(edge["source"]))
        content_id = mermaid_id("art", str(edge["checksum"]))
        plugin = str(edge["plugin"])
        count = int(edge["count"])
        label = plugin if count == 1 else f"{plugin} x{count}"
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


def render_graph_text(payload: dict[str, object]) -> str:
    lines = [
        f"session: {payload['sessionDir']}",
        f"manifest: {payload['manifestPath']}",
        (
            "graph: "
            f"{payload['sourceCount']} sources, "
            f"{payload['contentCount']} content nodes, "
            f"{payload['edgeCount']} edges"
            f"{filter_suffix(payload.get('filter'))}"
        ),
        (
            "artifacts: "
            f"discovery {payload['discoveryArtifactCount']}, "
            f"evidence {payload['evidenceArtifactCount']}"
        ),
    ]
    if payload.get("nextStep"):
        lines.append(f"next: {payload['nextStep']}")
    append_count_section(
        lines,
        heading="top providers",
        records=list(payload.get("topProviders") or []),
        key="provider",
    )
    append_count_section(
        lines,
        heading="top artifact kinds",
        records=list(payload.get("topArtifactKinds") or []),
        key="artifactKind",
    )
    if payload.get("sources"):
        sources = list(payload.get("sources") or [])
        preview_sources = sources[:GRAPH_TEXT_PREVIEW_LIMIT]
        append_preview_heading(
            lines,
            heading="strongest sources",
            shown=len(preview_sources),
            total=len(sources),
        )
        for source in preview_sources:
            bits = [f"{int(source.get('contentCount') or 0)} content"]
            if source.get("artifactKinds"):
                bits.append(
                    ",".join(str(value) for value in source.get("artifactKinds") or [])
                )
            if bool(source.get("variant")):
                bits.append(f"variants {int(source.get('variantCount') or 0)}")
            lines.append(f"  - {source['locator']} ({'; '.join(bits)})")
            lines.append(f"    follow: `{source['followCommand']}`")
        if len(sources) > len(preview_sources):
            lines.append(
                f"  - ... {len(sources) - len(preview_sources)} additional sources hidden in text view"
            )
    if payload.get("content"):
        content_items = list(payload.get("content") or [])
        preview_content = content_items[:GRAPH_TEXT_PREVIEW_LIMIT]
        append_preview_heading(
            lines,
            heading="materialized anchors",
            shown=len(preview_content),
            total=len(content_items),
        )
        for item in preview_content:
            bits = [
                str(item["checksum"])[:12],
                f"{int(item.get('sourceCount') or 0)} sources",
            ]
            if item.get("artifactKind"):
                bits.append(str(item["artifactKind"]))
            lines.append(f"  - {item['preferredName']} ({'; '.join(bits)})")
            lines.append(
                "    stored: "
                + ", ".join(
                    part
                    for part in (
                        f"`{item.get('artifactLocator')}`"
                        if item.get("artifactLocator")
                        else "",
                        f"`{item.get('contentLocator')}`"
                        if item.get("contentLocator")
                        else "",
                    )
                    if part
                )
            )
    if payload.get("edges"):
        edges = list(payload.get("edges") or [])
        preview_edges = edges[:GRAPH_TEXT_PREVIEW_LIMIT]
        append_preview_heading(
            lines,
            heading="sample edges",
            shown=len(preview_edges),
            total=len(edges),
        )
        for edge in preview_edges:
            label = str(edge["plugin"])
            count = int(edge.get("count") or 0)
            if count > 1:
                label = f"{label} x{count}"
            lines.append(f"  - {edge['source']} -> {edge['checksum'][:12]} ({label})")
        if len(edges) > len(preview_edges):
            lines.append(
                f"  - ... {len(edges) - len(preview_edges)} additional edges hidden in text view"
            )
    return "\n".join(lines)


def cmd_graph(args: argparse.Namespace) -> int:
    dirs = session_dirs_for_read(args)
    require_started_session(dirs)
    session_ref = explicit_session_ref(args)
    payload = graph_payload(
        dirs,
        filter_query=str(getattr(args, "filter", "") or ""),
        session_ref=session_ref,
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.output == "text":
        print(render_graph_text(payload))
        return 0
    print(render_mermaid(payload))
    return 0
