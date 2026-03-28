"""Semantic payload builders for session analysis."""

from __future__ import annotations

from typing import Any

from ..core import provider_name, query_label, resource_label, topology_next_step
from .focus import focus_match_threshold, ordered_focus_scan_entries


def semantic_payload(lineage: dict[str, Any]) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
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
        provider = provider_name(
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

        query = query_label(locator)
        if query:
            query_id = f"query:{provider}:{query}"
            add_node(query_id, label=query, kind="query", group=provider)
            add_edge(provider_id, query_id, "query")
            add_edge(query_id, source_id, "drives")

        source_kind, source_label = resource_label(locator)
        if source_kind and source_label:
            resource_id = f"resource:{provider}:{source_label}"
            add_node(
                resource_id,
                label=source_label,
                kind=source_kind,
                group=provider,
            )
            add_edge(provider_id, resource_id, "resource")
            add_edge(resource_id, source_id, "resolved_by")

    for item in lineage["content"]:
        checksum = str(item["checksum"])
        content_id = f"content:{checksum}"
        add_node(
            content_id,
            label=str(item["preferredName"]),
            kind="content",
            group="content",
        )
        nodes[content_id]["materialized"] = True
        nodes[content_id]["artifactKind"] = str(item.get("artifactKind") or "")

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
        provider = str(lead_source["provider"] or provider_name(locator))
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
        source_kind = str(lead_source.get("artifactKind") or "")
        if source_kind:
            kinds.add(source_kind)
        nodes[source_id]["artifactKinds"] = sorted(kinds)
        nodes[source_id]["artifactKind"] = (
            nodes[source_id]["artifactKinds"][0]
            if len(nodes[source_id]["artifactKinds"]) == 1
            else ""
        )
        add_edge(provider_id, source_id, "source")
        resource_kind_name, resource_name = resource_label(locator)
        if resource_kind_name and resource_name:
            resource_id = f"resource:{provider}:{resource_name}"
            add_node(
                resource_id,
                label=resource_name,
                kind=resource_kind_name,
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
        "nextStep": topology_next_step(
            discovery_count=discovery_count,
            evidence_count=evidence_count,
        ),
        "nodes": sorted(nodes.values(), key=lambda item: (item["kind"], item["label"])),
        "edges": [
            {"source": source, "target": target, "label": label}
            for source, target, label in sorted(edges)
        ],
    }


def _semantic_node_follow_command(
    node: dict[str, Any],
    *,
    lineage: dict[str, Any],
) -> str:
    kind = str(node.get("kind") or "")
    node_id = str(node.get("id") or "")
    label = str(node.get("label") or "").strip()
    if kind == "source" and label:
        for source_item in lineage.get("sources") or []:
            if str(source_item.get("locator") or "") == label:
                return str(source_item.get("followCommand") or "").strip()
        return ""
    if kind == "content" and node_id.startswith("content:"):
        checksum = node_id.split(":", 1)[1]
        for content_item in lineage.get("content") or []:
            if str(content_item.get("checksum") or "") == checksum:
                return str(content_item.get("followCommand") or "").strip()
    return ""


def _analysis_focus_score(
    node: dict[str, Any], query: str
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
    node: dict[str, Any],
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


def semantic_focus_payload(
    lineage: dict[str, Any],
    semantic: dict[str, Any],
    *,
    focus: str,
    limit: int,
    scan_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    scan_entries = ordered_focus_scan_entries(
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
    threshold = focus_match_threshold(best_score)
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
    root["followCommand"] = _semantic_node_follow_command(
        root,
        lineage=lineage,
    )
    root_id = str(root["id"])
    seed_records = []
    for node_id in seed_ids:
        node = dict(node_index[node_id])
        node["followCommand"] = _semantic_node_follow_command(
            node,
            lineage=lineage,
        )
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
        node["followCommand"] = _semantic_node_follow_command(
            node,
            lineage=lineage,
        )
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
