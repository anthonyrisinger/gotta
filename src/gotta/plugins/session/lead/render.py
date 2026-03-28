"""Text rendering for `gotta session leads`."""

from __future__ import annotations

from collections.abc import Iterable

from ..core import (
    append_count_section,
    filter_suffix,
    lead_signal_labels,
    paging_summary_line,
    render_search_origins,
    stored_target_locators,
    visibility_summary,
)


def _int_value(value: object) -> int:
    return value if isinstance(value, int) else 0


def _object_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return []
    records: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            records.append(item)
    return records


def _string_items(value: object) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return []
    return [str(item) for item in value if str(item)]


def render_leads_text(payload: dict[str, object]) -> str:
    lines = [
        f"session: {payload['sessionDir']}",
        f"target: {payload['target'] or '(session-wide)'}",
        (
            "artifacts: "
            f"{payload['artifactCount']} "
            f"(discovery {payload['discoveryArtifactCount']}, "
            f"evidence {payload['evidenceArtifactCount']})"
        ),
        (
            "leads: "
            f"{payload['leadCount']} total (showing {payload['shownCount']}; "
            f"materialized {payload['materializedLeadCount']}, "
            f"unmaterialized {payload['unmaterializedLeadCount']})"
            f"{filter_suffix(payload.get('filter'))}"
        ),
        paging_summary_line(
            label="page",
            total_count=_int_value(payload.get("totalCount")),
            shown_count=_int_value(payload.get("shownCount")),
            offset=_int_value(payload.get("offset")),
            next_offset=(
                _int_value(payload.get("nextOffset"))
                if payload.get("nextOffset") is not None
                else None
            ),
        ),
    ]
    if (
        _int_value(payload.get("shownCount")) == 0
        and _int_value(payload.get("totalCount")) > 0
    ):
        lines.append("page: no results in this page window")
    top_providers_lines: list[str] = []
    append_count_section(
        top_providers_lines,
        heading="top providers",
        records=_object_records(payload.get("topProviders")),
        key="provider",
    )
    lines.extend(top_providers_lines)
    top_relation_lines: list[str] = []
    append_count_section(
        top_relation_lines,
        heading="top relations",
        records=_object_records(payload.get("topRelations")),
        key="relation",
    )
    lines.extend(top_relation_lines)
    if payload["nextStep"]:
        lines.append(f"next: {payload['nextStep']}")
    if payload["bestOverall"]:
        lines.append("best leads:")
        for lead in _object_records(payload.get("bestOverall")):
            relation = ", ".join(
                value for value in _string_items(lead.get("relationKinds")) if value
            )
            lines.append(
                f"  - [{'; '.join(lead_signal_labels(lead, aggregated=True))}] "
                f"{lead['locator']} ({lead['provider']}, {relation or 'lead'})"
            )
            visibility = visibility_summary(lead)
            if visibility:
                lines.append(f"    visibility: {visibility}")
            lines.append(f"    follow: `{lead['followCommand']}`")
            stored_targets = stored_target_locators(lead)
            if stored_targets:
                lines.append(
                    "    stored_target: "
                    + ", ".join(f"`{value}`" for value in stored_targets)
                )
            search_origins = render_search_origins(lead)
            if search_origins:
                lines.append(f"    search_origin: {search_origins}")
            contexts = _string_items(lead.get("contexts"))
            if contexts:
                lines.append(f"    context: {contexts[0]}")
    if payload["providerHighlights"]:
        lines.append("provider highlights:")
        for lead in _object_records(payload.get("providerHighlights")):
            relation = ", ".join(
                value for value in _string_items(lead.get("relationKinds")) if value
            )
            lines.append(
                f"  - [{'; '.join(lead_signal_labels(lead, aggregated=True))}] "
                f"{lead['locator']} ({lead['provider']}, {relation or 'lead'})"
            )
            lines.append(f"    follow: `{lead['followCommand']}`")
    if payload["artifacts"] and (payload["target"] or payload["artifactCount"] == 1):
        lines.append("source context:")
        for artifact in _object_records(payload.get("artifacts")):
            lines.append(
                f"- {artifact['preferredName']} ({str(artifact['checksum'])[:12]})"
            )
            lines.append(f"  source: `{artifact['sourceLocator'] or 'unknown'}`")
            if artifact.get("artifactKind"):
                lines.append(f"  artifact_kind: {artifact['artifactKind']}")
            artifact_visibility = visibility_summary(artifact)
            if artifact_visibility:
                lines.append(f"  visibility: {artifact_visibility}")
            lines.append(
                f"  stored: `{artifact['artifactLocator']}`, `{artifact['contentLocator']}`"
            )
            if artifact["lastFetchedAt"]:
                lines.append(f"  fetched: {artifact['lastFetchedAt']}")
            if not artifact["leads"]:
                lines.append("  leads: none")
                continue
            for lead in _object_records(artifact.get("leads")):
                lines.append(
                    f"  - [{'; '.join(lead_signal_labels(lead, aggregated=False))}] "
                    f"{lead['targetLocator']} ({lead['provider']}, {lead['relation']})"
                )
                visibility = visibility_summary(lead)
                if visibility:
                    lines.append(f"    visibility: {visibility}")
                lines.append(f"    follow: `{lead['followCommand']}`")
                stored_targets = stored_target_locators(lead)
                if stored_targets:
                    lines.append(
                        "    stored_target: "
                        + ", ".join(f"`{value}`" for value in stored_targets)
                    )
                if bool(lead.get("sourceSearchLike")):
                    provider = str(lead.get("sourceProvider") or "unknown")
                    subcommand = str(lead.get("sourceSubcommand") or "search")
                    rank = _int_value(lead.get("sourceRank"))
                    origin = f"{provider}/{subcommand}"
                    if rank > 0:
                        origin += f" #{rank}"
                    lines.append(f"    source_origin: {origin}")
                contexts = _string_items(lead.get("contexts"))
                if contexts:
                    lines.append(f"    context: {contexts[0]}")
    return "\n".join(lines)
