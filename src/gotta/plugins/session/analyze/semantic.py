"""Semantic focus payload builders for session analysis."""

from __future__ import annotations

from typing import Any

from .focus import focus_match_threshold, ordered_focus_scan_entries


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
