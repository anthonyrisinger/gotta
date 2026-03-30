"""Source visibility classification and normalization helpers."""

from __future__ import annotations

import re
from typing import Any, Mapping

from gotta.builtin import get_binding

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
    normalized_confidence = (
        confidence if confidence in VISIBILITY_CONFIDENCES else "low"
    )
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


def normalize_visibility_metadata(
    payload: Mapping[str, Any] | dict[str, Any],
) -> dict[str, Any]:
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


def _classify_local_gotta_visibility(
    *, plugin: str, subcommand: str, locator: str
) -> dict[str, Any]:
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


def _surface_visibility_metadata(
    provider: str,
    payload: Any,
    *,
    subcommand: str,
    locator: str,
) -> dict[str, Any]:
    binding = get_binding(provider)
    if binding is None or binding.classify_visibility is None:
        return {}
    return dict(binding.classify_visibility(payload, subcommand, locator))


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
    classified = _surface_visibility_metadata(
        normalized_provider,
        payload,
        subcommand=normalized_subcommand,
        locator=locator.strip(),
    )
    if classified:
        return classified
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


def best_visibility_metadata(
    *candidates: Mapping[str, Any] | dict[str, Any],
) -> dict[str, Any]:
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
