"""Source metadata derivation for dispatch materialization."""

from __future__ import annotations

import json
import re
from typing import Any

from gotta.resolve.invoke import canonical_locator as resolve_canonical_locator
from gotta.source.stamp import (
    derive_source_metadata_from_payload,
    normalize_source_timestamp,
    slack_timestamp_to_iso,
)
from gotta.source.visibility import (
    classify_visibility_metadata,
    extract_visibility_metadata_from_markdown,
)


_MARKDOWN_SOURCE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"^\s*-\s*(?:\*\*)?Created:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE
        ),
        "source_created_at",
    ),
    (
        re.compile(
            r"^\s*-\s*(?:\*\*)?Updated:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE
        ),
        "source_updated_at",
    ),
    (
        re.compile(
            r"^\s*-\s*(?:\*\*)?Modified:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE
        ),
        "source_updated_at",
    ),
    (
        re.compile(
            r"^\s*-\s*(?:\*\*)?Published:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE
        ),
        "source_published_at",
    ),
    (
        re.compile(
            r"^\s*-\s*(?:\*\*)?Authored:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE
        ),
        "source_created_at",
    ),
)


def _json_value(data: bytes) -> Any | None:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload


def _json_nested(payload: dict[str, Any], *path: str) -> str:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current or "").strip()


def _extract_source_metadata_from_json(payload: Any) -> dict[str, Any]:
    metadata = derive_source_metadata_from_payload(payload)
    if not isinstance(payload, dict):
        return metadata
    candidates = (
        ("source_published_at", _json_nested(payload, "published_at")),
        ("source_published_at", _json_nested(payload, "created_at")),
        ("source_updated_at", _json_nested(payload, "updatedAt")),
        ("source_created_at", _json_nested(payload, "createdAt")),
        ("source_updated_at", _json_nested(payload, "updated")),
        ("source_created_at", _json_nested(payload, "created")),
        ("source_updated_at", _json_nested(payload, "modifiedTime")),
        ("source_created_at", _json_nested(payload, "createdTime")),
        ("source_created_at", _json_nested(payload, "commit", "author", "date")),
        ("source_created_at", _json_nested(payload, "author", "date")),
    )
    for key, value in candidates:
        parsed = normalize_source_timestamp(value) or str(value or "").strip()
        if parsed and key not in metadata:
            metadata[key] = parsed
    return metadata


def _extract_source_metadata_from_markdown(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    metadata: dict[str, str] = {}
    for pattern, key in _MARKDOWN_SOURCE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        value = match.group("value").strip()
        parsed = normalize_source_timestamp(value) or value
        if parsed and key not in metadata:
            metadata[key] = parsed
    authored_match = re.search(r"\bauthored (?P<value>\d{4}-\d{2}-\d{2}T\S+Z?)\b", text)
    if authored_match and "source_created_at" not in metadata:
        metadata["source_created_at"] = authored_match.group("value")
    return metadata


def _derived_source_metadata(
    plugin: str,
    argv: list[str],
    data: bytes,
    *,
    provider: str = "",
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    canonical = resolve_canonical_locator(plugin, argv)
    defaults: dict[str, str] = {}
    if canonical.startswith("slack:thread:"):
        thread_ts = canonical.rsplit(":", 1)[-1]
        source_time = slack_timestamp_to_iso(thread_ts)
        if source_time:
            defaults["source_created_at"] = source_time
            defaults["source_updated_at"] = source_time
    if plugin != "slack" or not argv or argv[0] != "get":
        payload = _json_value(data)
        if payload is not None:
            metadata.update(_extract_source_metadata_from_json(payload))
            metadata.update(
                classify_visibility_metadata(
                    payload,
                    provider=provider,
                    plugin=plugin,
                    subcommand=argv[0] if argv else "",
                    locator=canonical,
                )
            )
        metadata.update(
            {
                key: value
                for key, value in _extract_source_metadata_from_markdown(data).items()
                if key not in metadata
            }
        )
        metadata.update(
            {
                key: value
                for key, value in extract_visibility_metadata_from_markdown(
                    data
                ).items()
                if key not in metadata
            }
        )
        if "visibility_level" not in metadata:
            metadata.update(
                classify_visibility_metadata(
                    {},
                    provider=provider,
                    plugin=plugin,
                    subcommand=argv[0] if argv else "",
                    locator=canonical,
                )
            )
        for key, value in defaults.items():
            metadata.setdefault(key, value)
        return metadata
    payload = _json_value(data)
    if isinstance(payload, dict):
        first_ts = slack_timestamp_to_iso(str(payload.get("firstTs") or ""))
        last_ts = slack_timestamp_to_iso(str(payload.get("lastTs") or ""))
        if first_ts:
            metadata["source_created_at"] = first_ts
        if last_ts:
            metadata["source_updated_at"] = last_ts
        metadata.update(
            classify_visibility_metadata(
                payload,
                provider=provider,
                plugin=plugin,
                subcommand=argv[0] if argv else "",
                locator=canonical,
            )
        )
    metadata.update(
        {
            key: value
            for key, value in _extract_source_metadata_from_markdown(data).items()
            if key not in metadata
        }
    )
    metadata.update(
        {
            key: value
            for key, value in extract_visibility_metadata_from_markdown(data).items()
            if key not in metadata
        }
    )
    if "visibility_level" not in metadata:
        metadata.update(
            classify_visibility_metadata(
                {},
                provider=provider,
                plugin=plugin,
                subcommand=argv[0] if argv else "",
                locator=canonical,
            )
        )
    for key, value in defaults.items():
        metadata.setdefault(key, value)
    return metadata
