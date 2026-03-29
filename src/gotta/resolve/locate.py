"""Session, artifact, and local-path lookup for read resolution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
import urllib.parse

from gotta import topology
from gotta.content.env import CONTENT_ENV, SESSION_ENV, load_state_env_at_root
from gotta.content.filesystem import FileSystemLedgerStore
from gotta.content.model import CommonOptions
from gotta.content.path import artifact_locator, is_sha256_digest, sanitize_name


def nearby_session_context() -> tuple[str, str]:
    session_root = os.environ.get(SESSION_ENV, "").strip()
    content_root = os.environ.get(CONTENT_ENV, "").strip()
    if session_root and content_root:
        return session_root, content_root
    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        data = load_state_env_at_root(parent)
        if not data:
            continue
        session_root = session_root or str(data.get(SESSION_ENV, "")).strip()
        content_root = content_root or str(data.get(CONTENT_ENV, "")).strip()
        if session_root or content_root:
            break
    if session_root and not content_root:
        content_root = str((Path(session_root).expanduser() / "content").resolve())
    return session_root, content_root


def explicit_session_context(options: CommonOptions | Any | None) -> tuple[str, str]:
    if options is None:
        return "", ""
    session_root = str(getattr(options, "session_dir", "") or "").strip()
    content_root = str(getattr(options, "content_dir", "") or "").strip()
    if session_root and not content_root:
        session_path = Path(session_root).expanduser().resolve()
        in_shared_topology = (
            topology.parse_grouped_session_root(session_path) is not None
            or topology.parse_shared_session_root(session_path) is not None
        )
        if in_shared_topology:
            shared_id = topology.shared_session_id(session_path)
            content_root = str(
                (topology.shared_session_root_for(shared_id) / "content").resolve()
            )
        else:
            content_root = str((session_path / "content").resolve())
    return session_root, content_root


def resolve_local_target(
    target: str,
    *,
    session_root: str = "",
    content_root: str = "",
) -> Path | None:
    candidate = Path(target).expanduser()
    session_root = session_root or nearby_session_context()[0]
    content_root = content_root or nearby_session_context()[1]
    digest_target = (
        target.removeprefix("content:") if target.startswith("content:") else target
    )
    if content_root and is_sha256_digest(digest_target):
        digest_candidate = (
            Path(content_root).expanduser() / digest_target / "data"
        ).resolve()
        if digest_candidate.exists():
            return digest_candidate
    if candidate.is_absolute():
        return candidate.resolve() if candidate.exists() else None
    if session_root:
        session_candidate = (Path(session_root).expanduser() / target).resolve()
        if session_candidate.exists():
            return session_candidate
    return None


def expected_local_target(target: str, *, session_root: str = "") -> Path | None:
    candidate = Path(target).expanduser()
    session_root = session_root or nearby_session_context()[0]
    if candidate.is_absolute():
        return candidate.resolve()
    if session_root:
        return (Path(session_root).expanduser() / target).resolve()
    return None


def resolve_session_artifact_name(
    target: str,
    *,
    content_root: str = "",
) -> Path | None:
    content_root = content_root or nearby_session_context()[1]
    if not content_root:
        return None
    root = Path(content_root).expanduser()
    if not root.exists():
        return None
    matches = [
        snapshot
        for snapshot in FileSystemLedgerStore.for_content_dir(root).scan_artifacts()
        if any(alias.name == target for alias in snapshot.aliases)
    ]
    if not matches:
        return None
    if len(matches) > 1:
        suggestions = ", ".join(
            artifact_locator(target, snapshot.digest) for snapshot in matches[:5]
        )
        raise SystemExit(
            f"ambiguous stored artifact name '{target}' in the active session content store; "
            f"use `artifact:{sanitize_name(target)}@<digest12>` or `content:<digest>` instead. "
            f"matching artifact locators: {suggestions}"
        )
    return matches[0].layout.blob_path


def resolve_artifact_locator(target: str, *, content_root: str = "") -> Path | None:
    if not target.startswith("artifact:"):
        return None
    content_root = content_root or nearby_session_context()[1]
    if not content_root:
        raise SystemExit(
            f"artifact locator '{target}' requires an active or discoverable session content store"
        )
    root = Path(content_root).expanduser()
    if not root.exists():
        return None
    raw = target.removeprefix("artifact:")
    if "@" in raw:
        name_part, digest_hint = raw.rsplit("@", 1)
    else:
        name_part, digest_hint = raw, ""
    desired_name = sanitize_name(name_part)
    matches = [
        snapshot
        for snapshot in FileSystemLedgerStore.for_content_dir(root).scan_artifacts()
        if any(alias.name == desired_name for alias in snapshot.aliases)
        and (not digest_hint or snapshot.digest.startswith(digest_hint))
    ]
    if not matches:
        return None
    if len(matches) > 1:
        suggestions = ", ".join(
            artifact_locator(desired_name, snapshot.digest) for snapshot in matches[:5]
        )
        raise SystemExit(
            f"ambiguous artifact locator '{target}' in the active session content store; "
            f"disambiguate with one of: {suggestions}"
        )
    return matches[0].layout.blob_path


def url_name(target: str) -> str:
    parsed = urllib.parse.urlparse(target)
    name = Path(parsed.path.rstrip("/")).name or parsed.netloc or "read"
    if "." not in name:
        name = f"{name}.md"
    return name
