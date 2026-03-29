"""Graph payload synthesis."""

from __future__ import annotations

from typing import cast

from gotta.content.backend import scan_content_snapshots
from gotta.content.model import ContentSnapshot, ResolvedDirs

from ..core import (
    artifact_human_locator,
    artifact_kind,
    compile_filter_pattern,
    follow_command,
    match_any,
    match_filter_text,
    provider_name,
    render_variant,
    render_variant_label,
    resolved_visibility_metadata,
    session_read_command,
    top_count_records,
    topology_next_step,
)
from ..manifest.model import ManifestRecord
from ..manifest.record import manifest_entries
from .model import GraphContent, GraphEdge, GraphPayload, GraphSource
from .model import GraphArtifactKindCountRecord, GraphProviderCountRecord, Visibility


def _string(value: object) -> str:
    return str(value or "").strip()


def _source_locator(entry: ManifestRecord) -> str:
    return (
        _string(entry.get("canonical_locator"))
        or _string(entry.get("locator"))
        or "unknown"
    )


def _preferred_name(entry: ManifestRecord) -> str:
    return _string(entry.get("preferred_name")) or "data"


def _content_record(
    checksum: str,
    locators: set[str],
    *,
    content_names: dict[str, str],
    snapshot_by_digest: dict[str, ContentSnapshot],
    session_ref: str,
) -> GraphContent:
    preferred_name = content_names.get(checksum, "data")
    snapshot = snapshot_by_digest.get(checksum)
    metadata = dict(snapshot.artifact.metadata) if snapshot is not None else {}
    plugin = _string(metadata.get("plugin"))
    content: GraphContent = {
        "checksum": checksum,
        "preferredName": preferred_name,
        "artifactKind": artifact_kind(metadata.get("artifact_kind")),
        "contentLocator": f"content:{checksum}",
        "artifactLocator": artifact_human_locator(preferred_name, checksum),
        "followCommand": session_read_command(
            artifact_human_locator(preferred_name, checksum),
            session_ref=session_ref,
        ),
        "sourceCount": len(locators),
        "collision": len(locators) > 1,
    }
    if snapshot is not None:
        return cast(
            GraphContent,
            {
                **content,
                **resolved_visibility_metadata(
                    metadata,
                    provider=plugin,
                    plugin=plugin,
                    subcommand=_string(metadata.get("subcommand")),
                    locator=str(next(iter(locators), "")),
                ),
            },
        )
    return content


def _source_record(
    locator: str,
    checksums: set[str],
    *,
    source_artifact_kinds: dict[str, set[str]],
    source_variants: dict[str, set[tuple[str, str]]],
    source_visibility: dict[str, Visibility],
    session_ref: str,
) -> GraphSource:
    artifact_kinds = sorted(source_artifact_kinds.get(locator, set()))
    variants = sorted(source_variants.get(locator, set()))
    source: GraphSource = {
        "locator": locator,
        "followCommand": follow_command(locator, session_ref=session_ref),
        "contentCount": len(checksums),
        "artifactKind": artifact_kinds[0] if len(artifact_kinds) == 1 else "",
        "artifactKinds": artifact_kinds,
        "collision": False,
        "variant": len(variants) > 1,
        "variantCount": len(variants),
        "variants": [render_variant_label(variant) for variant in variants],
    }
    return cast(GraphSource, {**source, **source_visibility.get(locator, {})})


def graph_payload(
    dirs: ResolvedDirs,
    *,
    filter_query: str = "",
    session_ref: str = "",
) -> GraphPayload:
    entries = manifest_entries(dirs)
    snapshot_by_digest = {
        snapshot.digest: snapshot
        for snapshot in scan_content_snapshots(
            dirs.content_dir,
            session_dir=dirs.session_dir,
        )
    }
    source_to_content: dict[str, set[str]] = {}
    content_to_sources: dict[str, set[str]] = {}
    edge_counts: dict[tuple[str, str, str], int] = {}
    content_names: dict[str, str] = {}
    source_variants: dict[str, set[tuple[str, str]]] = {}
    source_artifact_kinds: dict[str, set[str]] = {}
    source_visibility: dict[str, Visibility] = {}
    source_plugins: dict[str, set[str]] = {}
    source_actors: dict[str, set[str]] = {}
    content_plugins: dict[str, set[str]] = {}
    content_actors: dict[str, set[str]] = {}

    for entry in entries:
        source = _source_locator(entry)
        checksum = _string(entry.get("checksum"))
        if not checksum:
            continue
        plugin = _string(entry.get("plugin")) or "unknown"
        actor = _string(entry.get("actor"))

        source_to_content.setdefault(source, set()).add(checksum)
        content_to_sources.setdefault(checksum, set()).add(source)
        edge_key = (source, checksum, plugin)
        edge_counts[edge_key] = edge_counts.get(edge_key, 0) + 1
        content_names.setdefault(checksum, _preferred_name(entry))

        kind = artifact_kind(entry.get("artifact_kind"))
        if kind:
            source_artifact_kinds.setdefault(source, set()).add(kind)
        source_plugins.setdefault(source, set()).add(plugin)
        if actor:
            source_actors.setdefault(source, set()).add(actor)
            content_actors.setdefault(checksum, set()).add(actor)
        content_plugins.setdefault(checksum, set()).add(plugin)
        source_visibility[source] = cast(
            Visibility,
            resolved_visibility_metadata(
                cast(dict[str, object], source_visibility.get(source, {})),
                provider=plugin,
                plugin=plugin,
                subcommand=_string(entry.get("subcommand")),
                locator=source,
            ),
        )
        snapshot = snapshot_by_digest.get(checksum)
        if snapshot is not None:
            source_variants.setdefault(source, set()).add(render_variant(snapshot))

    sources = [
        _source_record(
            locator,
            checksums,
            source_artifact_kinds=source_artifact_kinds,
            source_variants=source_variants,
            source_visibility=source_visibility,
            session_ref=session_ref,
        )
        for locator, checksums in sorted(source_to_content.items())
    ]
    content = [
        _content_record(
            checksum,
            locators,
            content_names=content_names,
            snapshot_by_digest=snapshot_by_digest,
            session_ref=session_ref,
        )
        for checksum, locators in sorted(content_to_sources.items())
    ]
    edges: list[GraphEdge] = [
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
            source["locator"]
            for source in sources
            if match_any(
                filter_pattern,
                source["locator"],
                source["artifactKind"],
                source["artifactKinds"],
                source["variants"],
                source_plugins.get(source["locator"], set()),
                source_actors.get(source["locator"], set()),
            )
        }
        matched_content = {
            item["checksum"]
            for item in content
            if match_any(
                filter_pattern,
                item["checksum"],
                item["preferredName"],
                item["contentLocator"],
                item["artifactLocator"],
                item["artifactKind"],
                content_plugins.get(item["checksum"], set()),
                content_actors.get(item["checksum"], set()),
            )
        }
        matched_edges = {
            (edge["source"], edge["checksum"], edge["plugin"])
            for edge in edges
            if match_any(
                filter_pattern,
                edge["source"],
                edge["checksum"],
                edge["plugin"],
                content_names.get(edge["checksum"], ""),
            )
        }
        kept_sources = matched_sources.union(
            source for source, _checksum, _plugin in matched_edges
        )
        kept_content = matched_content.union(
            checksum for _source, checksum, _plugin in matched_edges
        )
        sources = [item for item in sources if item["locator"] in kept_sources]
        content = [item for item in content if item["checksum"] in kept_content]
        edges = [
            edge
            for edge in edges
            if edge["source"] in kept_sources and edge["checksum"] in kept_content
        ]

    empty = not sources and not content and not edges
    discovery_count = sum(1 for item in content if item["artifactKind"] == "discovery")
    evidence_count = sum(1 for item in content if item["artifactKind"] == "evidence")
    top_providers = graph_provider_count_records(
        [provider_name(item["locator"]) for item in sources],
    )
    top_artifact_kinds = graph_artifact_kind_count_records(
        [item["artifactKind"] for item in content if item["artifactKind"]],
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


def graph_provider_count_records(values: list[str]) -> list[GraphProviderCountRecord]:
    records: list[GraphProviderCountRecord] = []
    for record in top_count_records(values, key="provider"):
        provider = record.get("provider")
        count = record.get("count")
        if isinstance(provider, str) and isinstance(count, int):
            records.append({"provider": provider, "count": count})
    return records


def graph_artifact_kind_count_records(
    values: list[str],
) -> list[GraphArtifactKindCountRecord]:
    records: list[GraphArtifactKindCountRecord] = []
    for record in top_count_records(values, key="artifactKind"):
        artifact_kind = record.get("artifactKind")
        count = record.get("count")
        if isinstance(artifact_kind, str) and isinstance(count, int):
            records.append({"artifactKind": artifact_kind, "count": count})
    return records
