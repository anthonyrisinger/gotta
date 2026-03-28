"""Lead cache IO and snapshot-level extraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gotta import stored
from gotta.content.file import write_text_atomic
from gotta.content.model import ContentSnapshot

from .canon import provider_for_locator
from .extract import (
    extract_explicit_leads,
    extract_semantic_search_leads,
    filter_explicit_mentions,
)
from .model import LEADS_CACHE_NAME, LEADS_CACHE_VERSION, LeadMention
from .snapshot import snapshot_locator, snapshot_provider


def lead_cache_path(content_dir: Path) -> Path:
    return content_dir / LEADS_CACHE_NAME


def maybe_write_lead_cache(content_dir: Path) -> Path | None:
    path = lead_cache_path(content_dir)
    payload = _load_lead_cache_payload(path)
    if payload is not None and int(payload.get("version") or 0) == LEADS_CACHE_VERSION:
        return path
    text, degradations = _lead_text_for_path(content_dir / "data")
    mentions = extract_explicit_leads(text)
    provider = _provider_for_content_dir(content_dir)
    source_locator = ""
    meta_path = content_dir / "meta.json"
    if meta_path.exists():
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            source_locator = str(
                payload.get("canonical_locator", "") or payload.get("locator", "")
            ).strip()
    mentions = filter_explicit_mentions(
        source_locator=source_locator,
        provider=provider,
        mentions=mentions,
    )
    if provider and not any(
        mention.canonical_locator.strip()
        and mention.canonical_locator.strip() != source_locator
        for mention in mentions
    ):
        mentions.extend(extract_semantic_search_leads(text, provider=provider))
    if not text.strip() and not degradations:
        return None
    return _write_lead_cache(
        content_dir,
        mentions,
        degradations=degradations,
    )


def lead_mentions_for_snapshot(snapshot: ContentSnapshot) -> list[LeadMention]:
    cached = _read_lead_cache(snapshot)
    if cached is not None:
        return cached
    text, degradations = _lead_text_for_path(snapshot.data_path)
    if not text.strip() and not degradations:
        return []
    mentions = filter_explicit_mentions(
        source_locator=snapshot_locator(snapshot).strip(),
        provider=snapshot_provider(snapshot),
        mentions=extract_explicit_leads(text),
    )
    if not mentions:
        mentions.extend(
            extract_semantic_search_leads(
                text,
                provider=snapshot_provider(snapshot),
            )
        )
    _write_lead_cache(snapshot.content_dir, mentions, degradations=degradations)
    return mentions


def _lead_text_for_path(path: Path) -> tuple[str, tuple[str, ...]]:
    rendered = stored.stored_display(path)
    if rendered.data.startswith(b"# Binary Content\n"):
        return ("", rendered.degradations)
    return (rendered.data.decode("utf-8", errors="ignore"), rendered.degradations)


def _serialize_mentions(
    mentions: list[LeadMention], *, degradations: tuple[str, ...] = ()
) -> dict[str, Any]:
    payload = {
        "version": LEADS_CACHE_VERSION,
        "leadCount": len(mentions),
        "entries": [
            {
                "raw": mention.raw,
                "canonical_locator": mention.canonical_locator,
                "provider": mention.provider,
                "relation": mention.relation,
                "follow_command": mention.follow_command,
                "snippet": mention.snippet,
                "ordinal": mention.ordinal,
            }
            for mention in mentions
        ],
    }
    if degradations:
        payload["degradations"] = list(degradations)
    return payload


def _deserialize_mentions(payload: dict[str, Any]) -> list[LeadMention] | None:
    if int(payload.get("version") or 0) != LEADS_CACHE_VERSION:
        return None
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return None
    mentions: list[LeadMention] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mentions.append(
            LeadMention(
                raw=str(entry.get("raw") or ""),
                canonical_locator=str(entry.get("canonical_locator") or ""),
                provider=str(entry.get("provider") or ""),
                relation=str(entry.get("relation") or ""),
                follow_command=str(entry.get("follow_command") or ""),
                snippet=str(entry.get("snippet") or ""),
                ordinal=int(entry.get("ordinal") or 0),
            )
        )
    return mentions


def _load_lead_cache_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_lead_cache(
    content_dir: Path,
    mentions: list[LeadMention],
    *,
    degradations: tuple[str, ...] = (),
) -> Path:
    return write_text_atomic(
        lead_cache_path(content_dir),
        json.dumps(
            _serialize_mentions(mentions, degradations=degradations),
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def _read_lead_cache(snapshot: ContentSnapshot) -> list[LeadMention] | None:
    path = lead_cache_path(snapshot.content_dir)
    payload = _load_lead_cache_payload(path)
    if payload is None:
        return None
    return _deserialize_mentions(payload)


def _provider_for_content_dir(content_dir: Path) -> str:
    meta_path = content_dir / "meta.json"
    if not meta_path.exists():
        return ""
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    locator = str(
        payload.get("canonical_locator", "") or payload.get("locator", "")
    ).strip()
    provider = provider_for_locator(locator)
    if provider != "external":
        return provider
    return str(payload.get("plugin") or "").strip()
