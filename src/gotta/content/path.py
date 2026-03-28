from __future__ import annotations

import os
from pathlib import Path
import re

from gotta.content.model import ContentError

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def sanitize_name(name: str) -> str:
    stripped = name.strip().replace("/", "-").replace("\\", "-")
    stripped = _SANITIZE_RE.sub("-", stripped)
    stripped = stripped.strip("-.")
    return stripped or "blob"


def content_locator(digest: str) -> str:
    return f"content:{digest.strip()}"


def artifact_locator(preferred_name: str, digest: str) -> str:
    return f"artifact:{sanitize_name(preferred_name)}@{digest.strip()[:12]}"


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def session_member_path(root: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        raise ContentError(
            "session member paths must be relative to the active session root"
        )
    root_resolved = root.expanduser().resolve()
    lexical = Path(os.path.normpath(str(root_resolved / candidate)))
    try:
        lexical.relative_to(root_resolved)
    except ValueError as exc:
        raise ContentError(
            "session member paths must stay under the active session root"
        ) from exc
    return lexical


def session_relative_path(root: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()


def is_sha256_digest(value: str) -> bool:
    return bool(_SHA256_RE.fullmatch(value))
