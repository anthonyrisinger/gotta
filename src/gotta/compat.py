"""Compatibility shims for supported Python versions."""

from __future__ import annotations

from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:  # pragma: no cover
    UTC = timezone.utc

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

__all__ = ["UTC", "datetime", "tomllib"]
