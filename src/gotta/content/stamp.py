from __future__ import annotations

from gotta.compat import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def iso_utc(dt: datetime | None = None) -> str:
    value = dt or utc_now()
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
