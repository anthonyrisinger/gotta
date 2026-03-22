"""Shared source metadata normalization, visibility, and aggregation helpers."""

from __future__ import annotations

import re
from typing import Any, Mapping

from gotta.compat import UTC, datetime


_SLACK_WHOLE_TS_RE = re.compile(r"^\d{16}$")
_SLACK_FRACTIONAL_TS_RE = re.compile(r"^\d{10}\.\d{6}$")
_ISOISH_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T\s]\S+)?$")

_CREATED_KEYS = {
    "authored",
    "created",
    "createdat",
    "created_at",
    "createdtime",
    "firstts",
    "first_ts",
    "threadts",
    "thread_ts",
    "ts",
}
_UPDATED_KEYS = {
    "lastmodified",
    "lastmodifiedat",
    "last_modified",
    "last_modified_at",
    "latestts",
    "latest_ts",
    "modified",
    "modifiedat",
    "modified_at",
    "modifiedtime",
    "pushedat",
    "pushed_at",
    "updated",
    "updatedat",
    "updated_at",
}
_PUBLISHED_KEYS = {
    "published",
    "publishedat",
    "published_at",
}
VISIBILITY_LEVELS = {
    "personal",
    "direct",
    "restricted",
    "internal",
    "public",
    "unknown",
}
VISIBILITY_BOUNDARIES = {
    "same_user",
    "same_company",
    "cross_company",
    "internet",
    "unknown",
}
VISIBILITY_CONFIDENCES = {
    "high",
    "medium",
    "low",
}
VISIBILITY_FIELDS = (
    "visibility_level",
    "visibility_boundary",
    "visibility_confidence",
    "visibility_basis",
)
_VISIBILITY_MARKDOWN_RE = re.compile(
    r"^\s*-\s*(?:\*\*|_)?Visibility(?:\*\*|_)?\s*:\s*"
    r"(?P<level>personal|direct|restricted|internal|public|unknown)"
    r"\s*\(\s*"
    r"(?P<boundary>same_user|same_company|cross_company|internet|unknown)"
    r"\s*,\s*"
    r"(?P<confidence>high|medium|low)"
    r"\s*\)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_LOCAL_GOTTA_SURFACES = {
    "actor",
    "artifact",
    "brief",
    "content",
    "goal",
    "logs",
    "notes",
    "oops",
    "session",
    "todo",
    "want",
}


def slack_timestamp_to_iso(raw: object) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if _SLACK_WHOLE_TS_RE.fullmatch(value):
        seconds = int(value[:10])
        micros = int(value[10:])
        return (
            datetime.fromtimestamp(seconds, tz=UTC)
            .replace(microsecond=micros)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    if _SLACK_FRACTIONAL_TS_RE.fullmatch(value):
        seconds, micros = value.split(".", 1)
        return (
            datetime.fromtimestamp(int(seconds), tz=UTC)
            .replace(microsecond=int(micros))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    return ""


def normalize_source_timestamp(value: object) -> str:
    slack_value = slack_timestamp_to_iso(value)
    if slack_value:
        return slack_value
    text = str(value or "").strip()
    if not text:
        return ""
    if _ISOISH_RE.fullmatch(text):
        return text
    return ""


def _classify_timestamp_path(path: tuple[str, ...]) -> str:
    if not path:
        return ""
    key = path[-1].casefold()
    if key in _PUBLISHED_KEYS:
        return "published"
    if key in _UPDATED_KEYS:
        return "updated"
    if key in _CREATED_KEYS:
        return "created"
    if key == "date" and len(path) >= 2 and path[-2].casefold() == "author":
        return "created"
    return ""


def _collect_candidates(
    payload: Any,
    *,
    path: tuple[str, ...] = (),
    created: list[str],
    updated: list[str],
    published: list[str],
) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            _collect_candidates(
                value,
                path=(*path, str(key)),
                created=created,
                updated=updated,
                published=published,
            )
        return
    if isinstance(payload, list):
        for item in payload:
            _collect_candidates(
                item,
                path=path,
                created=created,
                updated=updated,
                published=published,
            )
        return
    kind = _classify_timestamp_path(path)
    if not kind:
        return
    normalized = normalize_source_timestamp(payload)
    if not normalized:
        return
    if kind == "created":
        created.append(normalized)
        return
    if kind == "updated":
        updated.append(normalized)
        return
    published.append(normalized)


def derive_source_metadata_from_payload(payload: Any) -> dict[str, str]:
    created: list[str] = []
    updated: list[str] = []
    published: list[str] = []
    _collect_candidates(payload, created=created, updated=updated, published=published)
    metadata: dict[str, str] = {}
    if published:
        metadata["source_published_at"] = min(published)
    if created:
        metadata["source_created_at"] = min(created)
    if updated:
        metadata["source_updated_at"] = max(updated)
    return metadata


def _normalize_visibility_basis(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [value for item in raw if (value := str(item or "").strip())]
    if isinstance(raw, tuple):
        return [value for item in raw if (value := str(item or "").strip())]
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            return []
        return [value]
    return []


def visibility_metadata(
    *,
    level: str,
    boundary: str,
    confidence: str,
    basis: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    normalized_level = level if level in VISIBILITY_LEVELS else "unknown"
    normalized_boundary = boundary if boundary in VISIBILITY_BOUNDARIES else "unknown"
    normalized_confidence = confidence if confidence in VISIBILITY_CONFIDENCES else "low"
    normalized_basis = _normalize_visibility_basis(basis or [])
    if not normalized_basis:
        normalized_basis = ["classification=insufficient_evidence"]
    return {
        "visibility_level": normalized_level,
        "visibility_boundary": normalized_boundary,
        "visibility_confidence": normalized_confidence,
        "visibility_basis": normalized_basis,
    }


def unknown_visibility(
    *,
    provider: str = "",
    basis: list[str] | tuple[str, ...] | None = None,
    confidence: str = "low",
) -> dict[str, Any]:
    normalized_basis = _normalize_visibility_basis(basis or [])
    if provider:
        normalized_basis.insert(0, f"provider={provider}")
    if "classification=insufficient_evidence" not in normalized_basis:
        normalized_basis.append("classification=insufficient_evidence")
    return visibility_metadata(
        level="unknown",
        boundary="unknown",
        confidence=confidence,
        basis=normalized_basis,
    )


def normalize_visibility_metadata(payload: Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
    basis = _normalize_visibility_basis(payload.get("visibility_basis"))
    level = str(payload.get("visibility_level") or "").strip()
    boundary = str(payload.get("visibility_boundary") or "").strip()
    confidence = str(payload.get("visibility_confidence") or "").strip()
    if level and boundary and confidence:
        return visibility_metadata(
            level=level,
            boundary=boundary,
            confidence=confidence,
            basis=basis,
        )
    return {}


def _provider_from_locator(locator: str) -> str:
    value = locator.strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return "web"
    prefix = value.split(":", 1)[0].strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9_-]*", prefix):
        return prefix
    return ""


def _classify_local_gotta_visibility(*, plugin: str, subcommand: str, locator: str) -> dict[str, Any]:
    basis = ["provider=gotta"]
    if plugin:
        basis.append(f"plugin={plugin}")
    if subcommand:
        basis.append(f"subcommand={subcommand}")
    if locator:
        basis.append(f"locator={locator}")
    return visibility_metadata(
        level="personal",
        boundary="same_user",
        confidence="high",
        basis=basis,
    )


def _slack_channel_value(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    channel = payload.get("channel")
    if isinstance(channel, Mapping):
        return channel
    return payload


def _mapping_bool(payload: Mapping[str, Any], *keys: str) -> bool:
    for key in keys:
        if key in payload:
            return bool(payload.get(key))
    return False


def _mapping_str(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _slack_channel_type(payload: Mapping[str, Any]) -> str:
    explicit = _mapping_str(payload, "type")
    if explicit:
        return explicit
    channel_id = _mapping_str(payload, "id", "channelId")
    if _mapping_bool(payload, "is_mpim"):
        return "mpim"
    if channel_id.startswith("D"):
        return "im"
    if _mapping_bool(payload, "is_private", "isPrivate"):
        return "private_channel"
    if channel_id:
        return "public_channel"
    return ""


def _classify_slack_visibility(payload: Mapping[str, Any]) -> dict[str, Any]:
    channel = _slack_channel_value(payload)
    channel_type = _slack_channel_type(channel)
    if not channel_type:
        return unknown_visibility(provider="slack")
    is_shared = _mapping_bool(channel, "is_shared", "isShared")
    is_ext_shared = _mapping_bool(channel, "is_ext_shared", "isExtShared")
    boundary = "cross_company" if (is_shared or is_ext_shared) else "same_company"
    if channel_type in {"im", "mpim"}:
        level = "direct"
    elif channel_type == "private_channel":
        level = "restricted"
    elif channel_type == "public_channel":
        level = "internal"
    else:
        return unknown_visibility(
            provider="slack",
            basis=[f"channel.type={channel_type}"],
        )
    if boundary == "cross_company" and channel_type in {"private_channel", "public_channel"}:
        level = "restricted"
    basis = ["provider=slack", f"channel.type={channel_type}"]
    if is_shared:
        basis.append("channel.is_shared=true")
    if is_ext_shared:
        basis.append("channel.is_ext_shared=true")
    return visibility_metadata(
        level=level,
        boundary=boundary,
        confidence="high",
        basis=basis,
    )


def _extract_github_visibility(payload: Mapping[str, Any]) -> str:
    for key in ("visibility", "repositoryVisibility", "repoVisibility"):
        value = str(payload.get(key) or "").strip().lower()
        if value:
            return value
    repository = payload.get("repository")
    if isinstance(repository, Mapping):
        for key in ("visibility", "repositoryVisibility", "repoVisibility"):
            value = str(repository.get(key) or "").strip().lower()
            if value:
                return value
    return ""


def _classify_github_visibility(payload: Mapping[str, Any]) -> dict[str, Any]:
    visibility = _extract_github_visibility(payload)
    if visibility == "public":
        return visibility_metadata(
            level="public",
            boundary="internet",
            confidence="high",
            basis=["provider=github", "repo.visibility=public"],
        )
    if visibility == "internal":
        return visibility_metadata(
            level="internal",
            boundary="same_company",
            confidence="high",
            basis=["provider=github", "repo.visibility=internal"],
        )
    if visibility == "private":
        return visibility_metadata(
            level="restricted",
            boundary="same_company",
            confidence="high",
            basis=["provider=github", "repo.visibility=private"],
        )
    return unknown_visibility(provider="github")


def _jira_security_name(payload: Mapping[str, Any]) -> str:
    for key in ("security", "securityLevel"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            name = str(value.get("name") or value.get("id") or "").strip()
            if name:
                return name
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _jira_issue_like_payload(payload: Mapping[str, Any]) -> bool:
    if str(payload.get("issueUrl") or "").strip():
        return True
    if str(payload.get("siteUrl") or "").strip() and str(payload.get("key") or "").strip():
        return True
    project = payload.get("project")
    if isinstance(project, Mapping) and str(project.get("key") or "").strip():
        return True
    return False


def _classify_jira_visibility(
    payload: Mapping[str, Any],
    *,
    subcommand: str,
    locator: str,
) -> dict[str, Any]:
    security_name = _jira_security_name(payload)
    if security_name:
        return visibility_metadata(
            level="restricted",
            boundary="same_company",
            confidence="high",
            basis=[
                "provider=jira",
                f"issue.security={security_name}",
            ],
        )
    results = payload.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, Mapping):
                continue
            security_name = _jira_security_name(item)
            if security_name:
                return visibility_metadata(
                    level="restricted",
                    boundary="same_company",
                    confidence="high",
                    basis=[
                        "provider=jira",
                        f"search.result.security={security_name}",
                    ],
                )
        if results:
            return visibility_metadata(
                level="restricted",
                boundary="same_company",
                confidence="medium",
                basis=[
                    "provider=jira",
                    "search.results=present",
                    "classification=authenticated_jira_surface",
                ],
            )
    if _jira_issue_like_payload(payload):
        return visibility_metadata(
            level="restricted",
            boundary="same_company",
            confidence="medium",
            basis=[
                "provider=jira",
                "issue.url=present",
                "classification=authenticated_jira_surface",
            ],
        )
    if subcommand in {"get", "search", "jql"} or locator.startswith("jira:"):
        return visibility_metadata(
            level="restricted",
            boundary="same_company",
            confidence="medium",
            basis=[
                "provider=jira",
                f"subcommand={subcommand or 'default'}",
                "classification=authenticated_jira_surface",
            ],
        )
    return unknown_visibility(provider="jira")


def classify_visibility_metadata(
    payload: Any,
    *,
    provider: str = "",
    plugin: str = "",
    subcommand: str = "",
    locator: str = "",
) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        existing = normalize_visibility_metadata(payload)
        if existing:
            return existing
    normalized_provider = (
        provider.strip().lower()
        or _provider_from_locator(locator)
        or plugin.strip().lower()
    )
    normalized_plugin = plugin.strip().lower()
    normalized_subcommand = subcommand.strip().lower()
    if normalized_provider == "read":
        normalized_provider = _provider_from_locator(locator)
    if normalized_provider == "gotta" or normalized_plugin in _LOCAL_GOTTA_SURFACES:
        return _classify_local_gotta_visibility(
            plugin=normalized_plugin or normalized_provider or "gotta",
            subcommand=normalized_subcommand,
            locator=locator.strip(),
        )
    if normalized_provider == "slack" and isinstance(payload, Mapping):
        return _classify_slack_visibility(payload)
    if normalized_provider == "github" and isinstance(payload, Mapping):
        return _classify_github_visibility(payload)
    if normalized_provider == "jira" and isinstance(payload, Mapping):
        return _classify_jira_visibility(
            payload,
            subcommand=normalized_subcommand,
            locator=locator.strip(),
        )
    if normalized_provider == "jira":
        return _classify_jira_visibility(
            {},
            subcommand=normalized_subcommand,
            locator=locator.strip(),
        )
    if normalized_provider == "web":
        return unknown_visibility(provider="web")
    if normalized_provider in {
        "confluence",
        "gdocs",
        "gdrive",
        "grafana",
        "granola",
        "gsheets",
    }:
        return unknown_visibility(provider=normalized_provider)
    return unknown_visibility(provider=normalized_provider or "unknown")


def with_visibility_metadata(
    payload: dict[str, Any],
    *,
    provider: str = "",
    plugin: str = "",
    subcommand: str = "",
    locator: str = "",
) -> dict[str, Any]:
    return {
        **payload,
        **classify_visibility_metadata(
            payload,
            provider=provider,
            plugin=plugin,
            subcommand=subcommand,
            locator=locator,
        ),
    }


def extract_visibility_metadata_from_markdown(data: bytes) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    match = _VISIBILITY_MARKDOWN_RE.search(text)
    if match is None:
        return {}
    return visibility_metadata(
        level=match.group("level").lower(),
        boundary=match.group("boundary").lower(),
        confidence=match.group("confidence").lower(),
        basis=["source=markdown"],
    )


def render_source_metadata_lines(metadata: dict[str, str]) -> list[str]:
    lines: list[str] = []
    created = metadata.get("source_created_at", "").strip()
    updated = metadata.get("source_updated_at", "").strip()
    published = metadata.get("source_published_at", "").strip()
    if created:
        lines.append(f"- Created: {created}")
    if updated:
        lines.append(f"- Updated: {updated}")
    if published:
        lines.append(f"- Published: {published}")
    return lines


def render_visibility_metadata_lines(metadata: Mapping[str, Any]) -> list[str]:
    visibility = normalize_visibility_metadata(metadata)
    if not visibility:
        return []
    return [
        "- Visibility: "
        f"{visibility['visibility_level']} "
        f"({visibility['visibility_boundary']}, {visibility['visibility_confidence']})"
    ]


def best_visibility_metadata(*candidates: Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for candidate in candidates:
        visibility = normalize_visibility_metadata(candidate)
        if not visibility:
            continue
        level_rank = 0 if visibility["visibility_level"] == "unknown" else 1
        confidence_rank = {
            "low": 0,
            "medium": 1,
            "high": 2,
        }.get(str(visibility["visibility_confidence"]), 0)
        ranked.append((level_rank, confidence_rank, visibility))
    if not ranked:
        return {}
    return max(ranked, key=lambda item: (item[0], item[1]))[2]
