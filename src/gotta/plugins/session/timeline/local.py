"""Local session activity timeline helpers."""

from __future__ import annotations

from pathlib import Path

from gotta.actor import session_actor
from gotta.content.activity import activity_events, activity_log_path
from gotta.source import classify_visibility_metadata
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope

from ..core import LOCAL_TIMELINE_FILES, rendered_actor
from .stamp import _iso_utc_from_timestamp


def _meaningful_local_surface(path: Path) -> bool:
    return path.is_file()


def _timeline_activity_roots(dirs) -> list[Path]:
    grouped_root = session_registry._group_session_root(dirs.session_dir)
    if grouped_root != dirs.session_dir.resolve() or (grouped_root / "actors").is_dir():
        actor_ids = list(session_scope._selected_actor_ids(grouped_root))
        if actor_ids:
            return [
                session_registry._actor_session_dir(grouped_root, actor_id)
                for actor_id in actor_ids
            ]
    return [dirs.session_dir.resolve()]


def local_activity_timeline_events(dirs) -> tuple[list[dict[str, object]], list[str]]:
    events: list[dict[str, object]] = []
    activity_paths: list[str] = []
    seen_locators: set[tuple[str, str]] = set()
    seen_activity_roots: set[Path] = set()
    for activity_root in _timeline_activity_roots(dirs):
        resolved_root = activity_root.resolve()
        if resolved_root in seen_activity_roots:
            continue
        seen_activity_roots.add(resolved_root)
        activity_paths.append(str(activity_log_path(resolved_root)))
        for raw in activity_events(resolved_root):
            timestamp = str(raw.get("timestamp") or "").strip()
            if not timestamp:
                continue
            locator = str(raw.get("locator") or "").strip() or "unknown"
            seen_locators.add((str(resolved_root), locator))
            plugin = str(raw.get("plugin") or "session").strip() or "session"
            events.append(
                {
                    "mode": "local",
                    "source_time": timestamp,
                    "source_time_field": str(
                        raw.get("time_field") or "session_recorded_at"
                    ),
                    "source_created_at": "",
                    "source_updated_at": "",
                    "source_published_at": "",
                    "fetched_at": timestamp,
                    "plugin": plugin,
                    "actor": rendered_actor(
                        raw.get("actor"), session_root=resolved_root
                    ),
                    "target_actor": str(raw.get("target_actor") or "").strip(),
                    "locator": locator,
                    "preferred_name": str(raw.get("preferred_name") or locator).strip()
                    or locator,
                    "checksum": "",
                    "artifactKind": "",
                    "content_locator": "",
                    "artifact_locator": "",
                    "follow_command": str(raw.get("follow_command") or "").strip(),
                    "detail": str(raw.get("detail") or "").strip(),
                    "event_kind": "local",
                    **classify_visibility_metadata(
                        {},
                        provider="gotta",
                        plugin=plugin,
                        locator=locator,
                    ),
                }
            )
    candidates: list[tuple[str, Path, str, str, str]] = []
    for activity_root in _timeline_activity_roots(dirs):
        resolved_root = activity_root.resolve()
        actor = (
            session_actor(resolved_root)
            if resolved_root.parent.name == "actors"
            else ""
        )
        for relative in LOCAL_TIMELINE_FILES:
            surface = "want" if relative == "WANT.md" else "goal"
            locator = f"{surface}:actor:{actor}" if actor else f"{surface}:session"
            follow = f"gotta {surface} --actor {actor}" if actor else f"gotta {surface}"
            candidates.append(
                (
                    "actor" if actor else "session",
                    resolved_root / relative,
                    locator,
                    follow,
                    locator,
                )
            )
    for plugin, path, locator, follow_path, preferred_name in candidates:
        candidate_scope = str(path.parent.resolve())
        if (candidate_scope, locator) in seen_locators or not _meaningful_local_surface(
            path
        ):
            continue
        timestamp = _iso_utc_from_timestamp(path.stat().st_mtime)
        actor = (
            session_actor(path.parent)
            if plugin == "actor"
            else rendered_actor("", session_root=dirs.session_dir)
        )
        events.append(
            {
                "mode": "local",
                "source_time": timestamp,
                "source_time_field": "filesystem_mtime",
                "source_created_at": "",
                "source_updated_at": "",
                "source_published_at": "",
                "fetched_at": timestamp,
                "plugin": plugin,
                "actor": actor,
                "target_actor": "",
                "locator": locator,
                "preferred_name": preferred_name,
                "checksum": "",
                "artifactKind": "",
                "content_locator": "",
                "artifact_locator": "",
                "follow_command": follow_path,
                "detail": "local surface snapshot",
                "event_kind": "local",
                **classify_visibility_metadata(
                    {},
                    provider="gotta",
                    plugin=plugin,
                    locator=locator,
                ),
            }
        )
    return events, activity_paths
