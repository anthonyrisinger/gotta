"""Shared focus-mode selection kernels for session analysis."""

from __future__ import annotations

from dataclasses import dataclass

from .model import (
    AnalyzeScanEntry,
    AnalyzeScanPayload,
    LeadEdgeSummary,
    LeadSourceSummary,
    LineageCandidate,
    LineageContent,
    LineageNeighbor,
    LineagePayload,
    LineageRevisionEdge,
    LineageSource,
    LineageSourceEdge,
    SemanticEdge,
    SemanticNeighbor,
    SemanticNode,
    SemanticPayload,
)


def focus_match_threshold(best_score: int) -> int:
    if best_score <= 0:
        return 0
    if best_score >= 4:
        return 2
    return best_score


def ordered_focus_scan_entries(
    scan_payload: AnalyzeScanPayload | None,
    *,
    limit: int,
) -> list[AnalyzeScanEntry]:
    if scan_payload is None:
        return []
    entries = list(scan_payload.get("entries") or [])
    ordered = sorted(
        entries,
        key=lambda entry: str(
            entry.get("lastFetchedAt") or entry.get("fetched_at") or ""
        ),
        reverse=True,
    )
    ordered = sorted(
        ordered,
        key=lambda entry: int(str(entry.get("hitCount") or 0)),
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


@dataclass(frozen=True)
class LineageFocusSelection:
    root: LineageCandidate
    anchors: list[LineageCandidate]
    neighbors: list[LineageNeighbor]
    sources: list[LineageSource]
    content: list[LineageContent]
    source_edges: list[LineageSourceEdge]
    revision_edges: list[LineageRevisionEdge]
    lead_sources: list[LeadSourceSummary]
    lead_edges: list[LeadEdgeSummary]
    discovery_artifact_count: int
    evidence_artifact_count: int


@dataclass(frozen=True)
class SemanticFocusSelection:
    root: SemanticNode
    anchors: list[SemanticNode]
    neighbors: list[SemanticNeighbor]
    nodes: list[SemanticNode | SemanticNeighbor]
    edges: list[SemanticEdge]
    suppressed_structural_edge_count: int


@dataclass(frozen=True)
class _LineageFocusIndexes:
    sources: list[LineageSource]
    content: list[LineageContent]
    lead_sources: list[LeadSourceSummary]
    source_index: dict[str, LineageSource]
    content_index: dict[str, LineageContent]
    lead_index: dict[str, LeadSourceSummary]


def _lineage_indexes(payload: LineagePayload) -> _LineageFocusIndexes:
    sources = list(payload.get("sources") or [])
    content_items = list(payload.get("content") or [])
    lead_sources = list(payload.get("leadSources") or [])
    return _LineageFocusIndexes(
        sources=sources,
        content=content_items,
        lead_sources=lead_sources,
        source_index={str(item.get("locator") or ""): item for item in sources},
        content_index={str(item.get("checksum") or ""): item for item in content_items},
        lead_index={str(item.get("locator") or ""): item for item in lead_sources},
    )


def _lineage_source_candidate(item: LineageSource) -> LineageCandidate:
    return {
        "kind": "source",
        "label": str(item.get("locator") or ""),
        "locator": str(item.get("locator") or ""),
        "artifactKind": str(item.get("artifactKind") or ""),
        "materialized": True,
        "followCommand": str(item.get("followCommand") or ""),
    }


def _lineage_content_candidate(item: LineageContent) -> LineageCandidate:
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


def _lineage_lead_candidate(item: LeadSourceSummary) -> LineageCandidate:
    return {
        "kind": "lead",
        "label": str(item.get("locator") or ""),
        "locator": str(item.get("locator") or ""),
        "artifactKind": str(item.get("artifactKind") or ""),
        "materialized": bool(item.get("materialized")),
        "followCommand": str(item.get("followCommand") or ""),
    }


def _lineage_focus_score(item: LineageCandidate, query: str) -> tuple[int, int, str]:
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


def _lineage_seed_key(candidate: LineageCandidate) -> tuple[str, str]:
    kind = str(candidate.get("kind") or "")
    if kind == "content":
        return ("content", str(candidate.get("checksum") or ""))
    if kind == "lead":
        return ("lead", str(candidate.get("locator") or ""))
    return ("source", str(candidate.get("locator") or ""))


def _lineage_matches(
    indexes: _LineageFocusIndexes,
    *,
    query: str,
) -> list[LineageCandidate]:
    candidates = [
        *(_lineage_source_candidate(item) for item in indexes.sources),
        *(_lineage_content_candidate(item) for item in indexes.content),
        *(
            _lineage_lead_candidate(item)
            for item in indexes.lead_sources
            if str(item.get("locator") or "") not in indexes.source_index
        ),
    ]
    return sorted(
        (
            candidate
            for candidate in candidates
            if _lineage_focus_score(candidate, query)[0] > 0
        ),
        key=lambda candidate: _lineage_focus_score(candidate, query),
        reverse=True,
    )


def _lineage_seed_candidates(
    indexes: _LineageFocusIndexes,
    *,
    matches: list[LineageCandidate],
    query: str,
    limit: int,
    scan_payload: AnalyzeScanPayload | None,
) -> list[LineageCandidate]:
    scan_entries = ordered_focus_scan_entries(
        scan_payload,
        limit=max(limit * 2, 8),
    )
    seed_cap = max(4, min(max(limit, 1), 12))
    best_score = _lineage_focus_score(matches[0], query)[0] if matches else 0
    threshold = focus_match_threshold(best_score)
    seeds: list[LineageCandidate] = []
    seen_seed_keys: set[tuple[str, str]] = set()

    def add_seed(candidate: LineageCandidate) -> None:
        key = _lineage_seed_key(candidate)
        if not key[1] or key in seen_seed_keys:
            return
        seen_seed_keys.add(key)
        seeds.append(candidate)

    for candidate in matches:
        if _lineage_focus_score(candidate, query)[0] < threshold:
            break
        add_seed(candidate)
        if len(seeds) >= seed_cap:
            return seeds
    for entry in scan_entries:
        checksum = str(entry.get("checksum") or "").strip()
        locator = str(
            entry.get("canonical_locator") or entry.get("locator") or ""
        ).strip()
        if checksum and checksum in indexes.content_index:
            add_seed(_lineage_content_candidate(indexes.content_index[checksum]))
        if locator and locator in indexes.source_index:
            add_seed(_lineage_source_candidate(indexes.source_index[locator]))
        if len(seeds) >= seed_cap:
            break
    return seeds


def _expand_lineage_selection(
    payload: LineagePayload,
    *,
    selected_sources: set[str],
    selected_content: set[str],
) -> None:
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


def _ordered_lineage_neighbors(
    indexes: _LineageFocusIndexes,
    *,
    selected_sources: set[str],
    selected_content: set[str],
    selected_leads: set[str],
    seen_seed_keys: set[tuple[str, str]],
    limit: int,
) -> list[LineageNeighbor]:
    neighbors: list[LineageNeighbor] = []
    for locator in sorted(selected_sources):
        if ("source", locator) in seen_seed_keys:
            continue
        source_item = indexes.source_index.get(locator)
        if source_item is None:
            continue
        neighbors.append(
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
        content_item = indexes.content_index.get(checksum)
        if content_item is None:
            continue
        neighbors.append(
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
        lead_item = indexes.lead_index.get(locator)
        if lead_item is None:
            continue
        neighbors.append(
            {
                "kind": "lead",
                "label": locator,
                "relation": "followable lead",
                "followCommand": str(lead_item.get("followCommand") or ""),
                "artifactKind": str(lead_item.get("artifactKind") or ""),
                "materialized": bool(lead_item.get("materialized")),
            }
        )
    return sorted(
        neighbors,
        key=lambda item: (
            1 if bool(item.get("materialized")) else 0,
            1 if str(item.get("kind") or "") == "content" else 0,
            str(item.get("label") or "").lower(),
        ),
        reverse=True,
    )[: max(limit, 0)]


def select_lineage_focus(
    payload: LineagePayload,
    *,
    query: str,
    limit: int,
    scan_payload: AnalyzeScanPayload | None = None,
) -> LineageFocusSelection | None:
    indexes = _lineage_indexes(payload)
    matches = _lineage_matches(indexes, query=query)
    seeds = _lineage_seed_candidates(
        indexes,
        matches=matches,
        query=query,
        limit=limit,
        scan_payload=scan_payload,
    )
    if not seeds:
        return None

    root = seeds[0]
    seen_seed_keys = {_lineage_seed_key(candidate) for candidate in seeds}
    selected_sources = {
        str(candidate.get("locator") or "")
        for candidate in seeds
        if str(candidate.get("kind") or "") == "source"
    }
    selected_content = {
        str(candidate.get("checksum") or "")
        for candidate in seeds
        if str(candidate.get("kind") or "") == "content"
    }
    selected_leads = {
        str(candidate.get("locator") or "")
        for candidate in seeds
        if str(candidate.get("kind") or "") == "lead"
    }
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

    _expand_lineage_selection(
        payload,
        selected_sources=selected_sources,
        selected_content=selected_content,
    )
    for edge in payload.get("leadEdges") or []:
        source_checksum = str(edge.get("sourceChecksum") or "")
        target_locator = str(edge.get("targetLocator") or "")
        target_is_source = target_locator in indexes.source_index
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
    _expand_lineage_selection(
        payload,
        selected_sources=selected_sources,
        selected_content=selected_content,
    )

    neighbors = _ordered_lineage_neighbors(
        indexes,
        selected_sources=selected_sources,
        selected_content=selected_content,
        selected_leads=selected_leads,
        seen_seed_keys=seen_seed_keys,
        limit=limit,
    )
    neighbor_source_labels = {
        str(item.get("label") or "")
        for item in neighbors
        if str(item.get("kind") or "") == "source"
    }
    neighbor_content_labels = {
        str(item.get("label") or "")
        for item in neighbors
        if str(item.get("kind") or "") == "content"
    }
    neighbor_content_checksums = {
        checksum
        for checksum, item in indexes.content_index.items()
        if str(item.get("preferredName") or checksum) in neighbor_content_labels
    }
    neighbor_lead_labels = {
        str(item.get("label") or "")
        for item in neighbors
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
        item
        for item in indexes.sources
        if str(item.get("locator") or "") in selected_sources
    ]
    selected_content_items = [
        item
        for item in indexes.content
        if str(item.get("checksum") or "") in selected_content
    ]
    selected_lead_items = [
        item
        for item in indexes.lead_sources
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
    return LineageFocusSelection(
        root=root,
        anchors=seeds[1:],
        neighbors=neighbors,
        sources=selected_source_items,
        content=selected_content_items,
        source_edges=selected_source_edges,
        revision_edges=selected_revision_edges,
        lead_sources=selected_lead_items,
        lead_edges=selected_lead_edges,
        discovery_artifact_count=discovery_count,
        evidence_artifact_count=evidence_count,
    )


def _semantic_node_follow_command(
    node: SemanticNode,
    *,
    lineage: LineagePayload,
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


def _semantic_focus_score(node: SemanticNode, query: str) -> tuple[int, int, int, str]:
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


def _semantic_neighbor_sort_key(
    node: SemanticNode,
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


def _semantic_matches(
    semantic: SemanticPayload,
    *,
    query: str,
) -> list[SemanticNode]:
    nodes = list(semantic.get("nodes") or [])
    return sorted(
        (
            node
            for node in nodes
            if str(node.get("kind") or "") in {"source", "content"}
            and _semantic_focus_score(node, query)[0] > 0
        ),
        key=lambda node: _semantic_focus_score(node, query),
        reverse=True,
    )


def _semantic_seed_ids(
    *,
    node_index: dict[str, SemanticNode],
    matches: list[SemanticNode],
    query: str,
    limit: int,
    scan_payload: AnalyzeScanPayload | None,
) -> list[str]:
    scan_entries = ordered_focus_scan_entries(
        scan_payload,
        limit=max(limit * 2, 8),
    )
    seed_cap = max(4, min(max(limit, 1), 12))
    best_score = _semantic_focus_score(matches[0], query)[0] if matches else 0
    threshold = focus_match_threshold(best_score)
    seed_ids: list[str] = []

    def add_seed(node_id: str) -> None:
        if node_id and node_id in node_index and node_id not in seed_ids:
            seed_ids.append(node_id)

    for node in matches:
        if _semantic_focus_score(node, query)[0] < threshold:
            break
        add_seed(str(node.get("id") or ""))
        if len(seed_ids) >= seed_cap:
            return seed_ids
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
    return seed_ids


def _semantic_incident_edges(
    semantic: SemanticPayload,
    *,
    seed_id_set: set[str],
) -> list[SemanticEdge]:
    return [
        edge
        for edge in semantic.get("edges") or []
        if (
            str(edge.get("source") or "") in seed_id_set
            or str(edge.get("target") or "") in seed_id_set
        )
    ]


def select_semantic_focus(
    lineage: LineagePayload,
    semantic: SemanticPayload,
    *,
    query: str,
    limit: int,
    scan_payload: AnalyzeScanPayload | None = None,
) -> SemanticFocusSelection | None:
    nodes = list(semantic.get("nodes") or [])
    node_index = {
        node_id: node for node in nodes if (node_id := str(node.get("id") or ""))
    }
    matches = _semantic_matches(semantic, query=query)
    seed_ids = _semantic_seed_ids(
        node_index=node_index,
        matches=matches,
        query=query,
        limit=limit,
        scan_payload=scan_payload,
    )
    if not seed_ids:
        return None

    root_id = seed_ids[0]
    root: SemanticNode = {
        **node_index[root_id],
        "followCommand": _semantic_node_follow_command(
            node_index[root_id],
            lineage=lineage,
        ),
    }
    seed_records: list[SemanticNode] = [
        {
            **node_index[node_id],
            "followCommand": _semantic_node_follow_command(
                node_index[node_id],
                lineage=lineage,
            ),
        }
        for node_id in seed_ids
    ]
    seed_id_set = set(seed_ids)
    structural_labels = {"source", "resource", "resolved_by", "query", "drives"}
    incident_edges = _semantic_incident_edges(semantic, seed_id_set=seed_id_set)
    semantic_incident_edges = [
        edge
        for edge in incident_edges
        if str(edge.get("label") or "") not in structural_labels
    ]
    selected_edges = semantic_incident_edges or incident_edges
    selected_neighbor_ids: list[str] = []
    relation_labels_by_neighbor: dict[str, list[str]] = {}

    def record_neighbors(edges: list[SemanticEdge]) -> None:
        for edge in edges:
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            if source in seed_id_set and target in seed_id_set:
                continue
            neighbor_id = target if source in seed_id_set else source
            if not neighbor_id:
                continue
            relation_labels_by_neighbor.setdefault(neighbor_id, []).append(
                str(edge.get("label") or "")
            )
            if neighbor_id not in selected_neighbor_ids:
                selected_neighbor_ids.append(neighbor_id)

    record_neighbors(selected_edges)
    if len(selected_neighbor_ids) < max(2, limit // 2):
        record_neighbors(incident_edges)

    ranked_neighbor_ids = [
        neighbor_id
        for neighbor_id in selected_neighbor_ids
        if neighbor_id in node_index
    ]
    ranked_neighbor_ids = sorted(
        ranked_neighbor_ids,
        key=lambda neighbor_id: _semantic_neighbor_sort_key(
            node_index[neighbor_id],
            relation_labels=relation_labels_by_neighbor.get(neighbor_id, []),
        ),
        reverse=True,
    )[: max(limit, 0)]
    selected_node_ids = {root_id, *seed_ids, *ranked_neighbor_ids}
    focused_edges = [
        edge
        for edge in incident_edges
        if (
            str(edge.get("source") or "") in selected_node_ids
            and str(edge.get("target") or "") in selected_node_ids
        )
    ]
    neighbor_records: list[SemanticNeighbor] = [
        {
            **node_index[neighbor_id],
            "followCommand": _semantic_node_follow_command(
                node_index[neighbor_id],
                lineage=lineage,
            ),
            "relations": relation_labels_by_neighbor.get(neighbor_id, []),
        }
        for neighbor_id in ranked_neighbor_ids
    ]
    focused_nodes: list[SemanticNode | SemanticNeighbor] = [
        *seed_records,
        *neighbor_records,
    ]
    return SemanticFocusSelection(
        root=root,
        anchors=seed_records[1:],
        neighbors=neighbor_records,
        nodes=focused_nodes,
        edges=focused_edges,
        suppressed_structural_edge_count=max(
            len(incident_edges) - len(focused_edges), 0
        ),
    )
