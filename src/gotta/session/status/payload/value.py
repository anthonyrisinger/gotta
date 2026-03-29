"""Shared payload constants and coercion helpers."""

from __future__ import annotations

import time

from gotta.compat import datetime

from .model import LifecycleEntry


ACTOR_RUNNING_STATUS = {
    "starting",
    "active",
    "closing",
    "producing_evidence",
}

ACTOR_TERMINAL_STATUS = {
    "completed",
    "failed",
    "incomplete",
    "rejected",
    "signed_off",
}

ACTOR_STARTUP_GRACE_SECONDS = 30


def int_value(value: object, *, default: int = 0) -> int:
    try:
        return int(str(value or default))
    except ValueError:
        return default


def lifecycle_entries(value: object) -> list[LifecycleEntry]:
    if not isinstance(value, list):
        return []
    return [
        {
            "timestamp": str(item.get("timestamp") or ""),
            "event": str(item.get("event") or ""),
            "author": str(item.get("author") or ""),
            "detail": str(item.get("detail") or ""),
            "summary": str(item.get("summary") or ""),
        }
        for item in value
        if isinstance(item, dict)
    ]


def iso_age_seconds(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return time.time() - parsed.timestamp()
