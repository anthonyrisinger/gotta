"""Text rendering for `gotta session leads`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from gotta.lead.model import LeadEdge, LeadSourceSummary

from ..core import (
    append_count_section,
    filter_suffix,
    lead_signal_labels,
    paging_summary_line,
    render_search_origins,
    stored_target_locators,
    visibility_summary,
)
from .model import LeadArtifact, LeadsPayload


def _int_value(value: object) -> int:
    return value if isinstance(value, int) else 0


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


@dataclass(slots=True)
class _LeadRenderState:
    payload: LeadsPayload
    lines: list[str] = field(default_factory=list)

    def render(self) -> str:
        self._append_header()
        self._append_count_sections()
        if self.payload["nextStep"]:
            self.lines.append(f"next: {self.payload['nextStep']}")
        self._append_aggregated_section(
            heading="best leads:",
            leads=self.payload.get("bestOverall") or [],
            include_detail=True,
        )
        self._append_aggregated_section(
            heading="provider highlights:",
            leads=self.payload.get("providerHighlights") or [],
            include_detail=False,
        )
        self._append_source_context()
        return "\n".join(self.lines)

    def _append_header(self) -> None:
        self.lines.extend(
            [
                f"session: {self.payload['sessionDir']}",
                f"target: {self.payload['target'] or '(session-wide)'}",
                (
                    "artifacts: "
                    f"{self.payload['artifactCount']} "
                    f"(discovery {self.payload['discoveryArtifactCount']}, "
                    f"evidence {self.payload['evidenceArtifactCount']})"
                ),
                (
                    "leads: "
                    f"{self.payload['leadCount']} total "
                    f"(showing {self.payload['shownCount']}; "
                    f"materialized {self.payload['materializedLeadCount']}, "
                    f"unmaterialized {self.payload['unmaterializedLeadCount']})"
                    f"{filter_suffix(self.payload.get('filter'))}"
                ),
                paging_summary_line(
                    label="page",
                    total_count=_int_value(self.payload.get("totalCount")),
                    shown_count=_int_value(self.payload.get("shownCount")),
                    offset=_int_value(self.payload.get("offset")),
                    next_offset=(
                        _int_value(self.payload.get("nextOffset"))
                        if self.payload.get("nextOffset") is not None
                        else None
                    ),
                ),
            ]
        )
        if (
            _int_value(self.payload.get("shownCount")) == 0
            and _int_value(self.payload.get("totalCount")) > 0
        ):
            self.lines.append("page: no results in this page window")

    def _append_count_sections(self) -> None:
        top_providers_lines: list[str] = []
        append_count_section(
            top_providers_lines,
            heading="top providers",
            records=self.payload.get("topProviders") or [],
            key="provider",
        )
        self.lines.extend(top_providers_lines)
        top_relation_lines: list[str] = []
        append_count_section(
            top_relation_lines,
            heading="top relations",
            records=self.payload.get("topRelations") or [],
            key="relation",
        )
        self.lines.extend(top_relation_lines)

    def _append_aggregated_section(
        self,
        *,
        heading: str,
        leads: Sequence[LeadSourceSummary],
        include_detail: bool,
    ) -> None:
        if not leads:
            return
        self.lines.append(heading)
        for lead in leads:
            self._append_aggregated_lead(lead, include_detail=include_detail)

    def _append_aggregated_lead(
        self,
        lead: LeadSourceSummary,
        *,
        include_detail: bool,
    ) -> None:
        relation = ", ".join(
            value for value in _string_items(lead.get("relationKinds")) if value
        )
        self.lines.append(
            f"  - [{'; '.join(lead_signal_labels(lead, aggregated=True))}] "
            f"{lead['locator']} ({lead['provider']}, {relation or 'lead'})"
        )
        if include_detail:
            visibility = visibility_summary(lead)
            if visibility:
                self.lines.append(f"    visibility: {visibility}")
        self.lines.append(f"    follow: `{lead['followCommand']}`")
        if not include_detail:
            return
        stored_targets = stored_target_locators(lead)
        if stored_targets:
            self.lines.append(
                "    stored_target: "
                + ", ".join(f"`{value}`" for value in stored_targets)
            )
        search_origins = render_search_origins(lead)
        if search_origins:
            self.lines.append(f"    search_origin: {search_origins}")
        contexts = _string_items(lead.get("contexts"))
        if contexts:
            self.lines.append(f"    context: {contexts[0]}")

    def _append_source_context(self) -> None:
        artifacts: Sequence[LeadArtifact] = self.payload.get("artifacts") or []
        if not artifacts or not (
            self.payload["target"] or self.payload["artifactCount"] == 1
        ):
            return
        self.lines.append("source context:")
        for artifact in artifacts:
            self._append_artifact(artifact)

    def _append_artifact(self, artifact: LeadArtifact) -> None:
        self.lines.append(
            f"- {artifact['preferredName']} ({str(artifact['checksum'])[:12]})"
        )
        self.lines.append(f"  source: `{artifact['sourceLocator'] or 'unknown'}`")
        if artifact.get("artifactKind"):
            self.lines.append(f"  artifact_kind: {artifact['artifactKind']}")
        artifact_visibility = visibility_summary(artifact)
        if artifact_visibility:
            self.lines.append(f"  visibility: {artifact_visibility}")
        self.lines.append(
            f"  stored: `{artifact['artifactLocator']}`, `{artifact['contentLocator']}`"
        )
        if artifact["lastFetchedAt"]:
            self.lines.append(f"  fetched: {artifact['lastFetchedAt']}")
        leads: Sequence[LeadEdge] = artifact.get("leads") or []
        if not leads:
            self.lines.append("  leads: none")
            return
        for lead in leads:
            self._append_artifact_lead(lead)

    def _append_artifact_lead(self, lead: LeadEdge) -> None:
        self.lines.append(
            f"  - [{'; '.join(lead_signal_labels(lead, aggregated=False))}] "
            f"{lead['targetLocator']} ({lead['provider']}, {lead['relation']})"
        )
        visibility = visibility_summary(lead)
        if visibility:
            self.lines.append(f"    visibility: {visibility}")
        self.lines.append(f"    follow: `{lead['followCommand']}`")
        stored_targets = stored_target_locators(lead)
        if stored_targets:
            self.lines.append(
                "    stored_target: "
                + ", ".join(f"`{value}`" for value in stored_targets)
            )
        source_origin = self._source_origin(lead)
        if source_origin:
            self.lines.append(f"    source_origin: {source_origin}")
        contexts = _string_items(lead.get("contexts"))
        if contexts:
            self.lines.append(f"    context: {contexts[0]}")

    def _source_origin(self, lead: LeadEdge) -> str:
        if not bool(lead.get("sourceSearchLike")):
            return ""
        provider = str(lead.get("sourceProvider") or "unknown")
        subcommand = str(lead.get("sourceSubcommand") or "search")
        rank = _int_value(lead.get("sourceRank"))
        origin = f"{provider}/{subcommand}"
        if rank > 0:
            origin += f" #{rank}"
        return origin


def render_leads_text(payload: LeadsPayload) -> str:
    return _LeadRenderState(payload).render()
