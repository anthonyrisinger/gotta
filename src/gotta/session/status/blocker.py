"""Actor launch blocker and rewrite detection helpers."""

from __future__ import annotations

from pathlib import Path

from gotta.session.bootstrap import (
    _bootstrap_actor_goal,
    _bootstrap_actor_want,
    _bootstrap_want,
)
from gotta.session.registry import (
    WANT_FILE,
    _actor_charter_command,
    _actor_dir_path,
    _actor_goal_path,
    _actor_label,
    _actor_session_dir,
    _actor_want_path,
    _resolve_bound_actor_name,
)


def _want_rewrite_pending(work_dir: Path) -> bool:
    want_path = work_dir / WANT_FILE
    if not want_path.is_file():
        return True
    current = want_path.read_text(encoding="utf-8").strip()
    return current == _bootstrap_want().strip()


def _goal_rewrite_pending(goal_path: Path) -> bool:
    if not goal_path.is_file():
        return True
    return (
        goal_path.read_text(encoding="utf-8")
        .strip()
        .startswith("# Seed Goal Placeholder")
    )


def _actor_want_rewrite_pending(work_root: Path, actor_name: str) -> bool:
    path = _actor_want_path(work_root, actor_name)
    if not path.is_file():
        return True
    current = path.read_text(encoding="utf-8").strip()
    return (
        current
        == _bootstrap_actor_want(
            actor_name=actor_name,
            label=_actor_label(actor_name, work_dir=work_root),
        ).strip()
    )


def _actor_goal_rewrite_pending(work_root: Path, actor_name: str) -> bool:
    path = _actor_goal_path(work_root, actor_name)
    if not path.is_file():
        return True
    return (
        path.read_text(encoding="utf-8").strip()
        == _bootstrap_actor_goal(
            actor_name=actor_name,
            label=_actor_label(actor_name, work_dir=work_root),
            actor_dir=_actor_session_dir(work_root, actor_name),
            work_dir=work_root,
        ).strip()
    )


def _actor_launch_blockers(work_root: Path, *, actor_name: str = "") -> list[str]:
    blockers: list[str] = []
    goal_path = work_root / "GOAL.md"
    if _want_rewrite_pending(work_root):
        blockers.append(f"rewrite `{work_root / WANT_FILE}` first")
    if _goal_rewrite_pending(goal_path):
        blockers.append(f"rewrite `{goal_path}` from the current moment before launch")
    if actor_name:
        actor_name = _resolve_bound_actor_name(work_root, actor_name)
        actor_want = _actor_dir_path(work_root, actor_name) / WANT_FILE
        actor_goal = _actor_dir_path(work_root, actor_name) / "GOAL.md"
        want_cmd = _actor_charter_command(actor_name, "want")
        goal_cmd = _actor_charter_command(actor_name, "goal")
        if _actor_want_rewrite_pending(work_root, actor_name):
            blockers.append(
                f"rewrite `{actor_want}` as the actor-local intent frame with `{want_cmd}` before launch"
            )
        if _actor_goal_rewrite_pending(work_root, actor_name):
            blockers.append(
                f"rewrite `{actor_goal}` as the actor-local goal with `{goal_cmd}` before launch"
            )
    return blockers
