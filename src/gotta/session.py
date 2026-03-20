"""Shared session-surface helpers for charters, state, and linked actors."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from textwrap import dedent
import uuid

from gotta.compat import UTC, datetime
from gotta.content import (
    CONTENT_ENV,
    SESSION_ENV,
    SESSION_CREATED_ENV,
    SESSION_REPO_ENV,
    SESSION_ACTOR_ENV,
    SESSION_INITIALIZED_ENV,
    CommonOptions,
    append_activity_event,
    discover_state_env,
    env_mapping,
    load_state_env_at_root,
    resolve_dirs,
    session_identity,
    session_is_initialized,
    session_relative_path,
    sh_quote,
    state_dir_path,
    state_env_path,
    stdin_has_readable_text,
    session_surface_initialized,
    write_session_state,
)
from gotta.friction import oops_log_path, render_oops_markdown
from gotta.helptext import format_long_help, is_long_help_request
from gotta.logs import append_log_record, logs_state_path, sync_logs_projection
from gotta.notes import (
    actor_notes_ready,
    actor_notes_surface_path,
    sync_actor_notes_projection,
)
from gotta.actor import (
    normalize_actor_name as _shared_normalize_actor_name,
    session_actor,
    actor_session_root,
    requested_disposition_label,
)
from gotta.todo import (
    create_todo_item,
    ensure_managed_todo_item,
    set_todo_checked,
    sync_todo_projection,
    todo_items,
    todo_state_path,
)


SESSION_ACTORS_ENV = "GOTTA_SESSION_ACTORS_JSON"
SESSION_WANT_PATH_ENV = "GOTTA_SESSION_WANT_PATH"
SESSION_ACTORS_SOURCE_ENV = "GOTTA_SESSION_ACTORS_SOURCE"
SESSION_VOICE_SOURCE_ENV = "GOTTA_SESSION_VOICE_SOURCE"
ACTOR_STATE_STATUS = {
    "pending",
    "bound",
    "starting",
    "active",
    "stalled",
    "completed",
    "failed",
    "signed_off",
}
ACTOR_TERMINAL_STATUS = {
    "completed",
    "failed",
    "incomplete",
    "rejected",
    "signed_off",
}


def _normalize_actor_status(value: object) -> str:
    status = str(value or "pending")
    if status == "configured":
        return "bound"
    return status
ACTOR_STALL_SECONDS = 180
ACTOR_RUNNING_STATUS = {
    "starting",
    "active",
    "producing_evidence",
}
WANT_FILE = "WANT.md"
ROOT_SHARED_FILES = (
    "LOGS.md",
    "OOPS.md",
)
SESSION_STATE_KEYS = (
    SESSION_INITIALIZED_ENV,
    SESSION_CREATED_ENV,
    SESSION_ACTORS_ENV,
    SESSION_WANT_PATH_ENV,
    SESSION_ACTORS_SOURCE_ENV,
    SESSION_VOICE_SOURCE_ENV,
    SESSION_ACTOR_ENV,
)
ACTOR_SHARED_FILES = (
    "LOGS.md",
    "OOPS.md",
)
ACTOR_SHARED_STATE_FILES = (
    "logs.jsonl",
    "oops.jsonl",
)
FINAL_SIGNOFF_MARKER = "actors-final-signoff"


def _current_session_dir(
    explicit_session: str | None,
    explicit_actor: str | None = None,
    *,
    include_context_session: bool = True,
) -> Path | None:
    discovered = discover_state_env(include_context_session=include_context_session)
    try:
        dirs = resolve_dirs(
            CommonOptions(
                session_dir=explicit_session
                or os.environ.get(SESSION_ENV, "").strip()
                or discovered.get(SESSION_ENV, "").strip(),
                actor=explicit_actor
                or os.environ.get(SESSION_ACTOR_ENV, "").strip()
                or discovered.get(SESSION_ACTOR_ENV, "").strip(),
            ),
            create=False,
        )
    except Exception:
        return None
    return dirs.session_dir


def _session_dir(
    *,
    explicit_session: str | None,
    explicit_actor: str | None = None,
) -> Path:
    current = _current_session_dir(
        explicit_session,
        explicit_actor=explicit_actor,
    )
    if current is None:
        raise SystemExit(
            "start or bind a session first with `gotta ...` or bootstrap one "
            "manually with `gotta session init --session \"$WS\"`"
        )
    if not session_is_initialized(current):
        raise SystemExit(
            "start or bind a session first with `gotta ...` or bootstrap one "
            "manually with `gotta session init --session \"$WS\"`"
        )
    if not session_surface_initialized(current):
        raise SystemExit(
            "this session has no canonical session surface yet; run "
            "`gotta session init` to scaffold the active session"
        )
    return current


def _build_charter_parser(
    *,
    command_name: str,
    description: str,
    value_name: str,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description=description,
    )
    parser.add_argument("--session", help="session root")
    parser.add_argument("--actor", help="actor within the current session")
    parser.add_argument(
        "--from-file",
        help=f"read {value_name} text from a UTF-8 file instead of inline text; use '-' for stdin",
    )
    parser.add_argument(
        "--stdin",
        dest="use_stdin",
        action="store_true",
        help=f"read {value_name} text from stdin explicitly",
    )
    return parser


def add_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", help="session root")
    parser.add_argument("--actor", help="actor within the current session")


def _read_charter_text_source(
    *,
    session_root: Path,
    from_file: str | None,
    use_stdin: bool,
    value_name: str,
) -> str:
    used = int(bool(from_file)) + int(bool(use_stdin))
    if used > 1:
        raise SystemExit(f"use only one {value_name} text source")
    if from_file:
        if from_file == "-":
            return sys.stdin.read()
        return session_relative_path(session_root, from_file).read_text(encoding="utf-8")
    if use_stdin:
        return sys.stdin.read()
    raise SystemExit(f"missing {value_name} text; use --stdin or --from-file")


def _normalize_charter_text(text: str, *, value_name: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not normalized.strip():
        raise SystemExit(f"missing {value_name} text")
    return normalized


def _session_relative_locator(work_dir: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(work_dir.resolve()))
    except ValueError:
        return str(resolved)


def run_charter_surface(
    argv: list[str] | None,
    *,
    command_name: str,
    description: str,
    surface_name: str,
    plugin_name: str,
    value_name: str,
) -> int:
    argv = list(argv or [])
    if is_long_help_request(argv):
        print(
            format_long_help(
                _build_charter_parser(
                    command_name=command_name,
                    description=description,
                    value_name=value_name,
                )
            )
        )
        return 0
    parser = _build_charter_parser(
        command_name=command_name,
        description=description,
        value_name=value_name,
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if int(exc.code or 0) == 0:
            return 0
        raise
    work_dir = _session_dir(
        explicit_session=getattr(args, "session", None),
        explicit_actor=getattr(args, "actor", None),
    )
    path = work_dir / surface_name
    has_payload = bool(args.from_file or args.use_stdin)
    if not has_payload:
        if not path.is_file():
            raise SystemExit(f"missing {surface_name} surface: {path}")
        print(path.read_text(encoding="utf-8"), end="")
        return 0
    payload = _normalize_charter_text(
        _read_charter_text_source(
            session_root=work_dir,
            from_file=args.from_file,
            use_stdin=args.use_stdin,
            value_name=value_name,
        ),
        value_name=value_name,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload + "\n", encoding="utf-8")
    append_activity_event(
        work_dir,
        {
            "plugin": plugin_name,
            "surface": plugin_name,
            "action": "write",
            "locator": _session_relative_locator(work_dir, path),
            "preferred_name": path.name,
            "follow_command": f"gotta read {surface_name!r}",
            "detail": f"rewrote {surface_name}",
            "time_field": "session_recorded_at",
        },
    )
    print(f"rewrote {surface_name}: {path}")
    return 0


@dataclass(frozen=True, slots=True)
class ActorSpec:
    actor_id: str
    label: str
    default_model: str
    aliases: tuple[str, ...]


DEFAULT_ACTORS = (
    ActorSpec(
        actor_id="claude",
        label="Claude",
        default_model="claude-sonnet-4.6",
        aliases=(),
    ),
    ActorSpec(
        actor_id="codex",
        label="Codex",
        default_model="gpt-5.3-codex",
        aliases=("gpt",),
    ),
)
ACTOR_INDEX = {actor.actor_id: actor for actor in DEFAULT_ACTORS}
ACTOR_IDS = tuple(actor.actor_id for actor in DEFAULT_ACTORS)
ACTOR_ALIASES = {
    alias: actor.actor_id
    for actor in DEFAULT_ACTORS
    for alias in (actor.actor_id, *actor.aliases)
}


def _join_labels(labels: tuple[str, ...], joiner: str, *, code: bool = False) -> str:
    rendered = tuple(f"`{label}`" if code else label for label in labels)
    if len(rendered) <= 1:
        return "".join(rendered)
    if len(rendered) == 2:
        return f" {joiner} ".join(rendered)
    return ", ".join(rendered[:-1]) + f", {joiner} {rendered[-1]}"


def _default_actor_summary(*, code: bool = False) -> str:
    return _join_labels(
        tuple(actor.label for actor in DEFAULT_ACTORS),
        "and",
        code=code,
    )


def _actor_bind_examples(*, prefix: str = "gotta actor bind") -> str:
    return " or ".join(f"`{prefix} {actor.label} ...`" for actor in DEFAULT_ACTORS)


def _actor_session_ref(actor_name: str) -> str:
    return _normalize_actor_name(actor_name)


def _actor_charter_command(actor_name: str, surface: str, *, mode: str = "--stdin") -> str:
    return f"gotta {surface} --actor {_actor_session_ref(actor_name)} {mode}"


def _resolve_actor_name(value: str, *, kind: str = "actor") -> str:
    normalized = _shared_normalize_actor_name(value)
    if not normalized:
        raise SystemExit(f"missing {kind}")
    if normalized in ACTOR_ALIASES:
        return ACTOR_ALIASES[normalized]
    return normalized


def _normalize_actor_name(value: str) -> str:
    return _resolve_actor_name(value, kind="actor")


def _actor_label(actor_name: str) -> str:
    normalized = _normalize_actor_name(actor_name)
    spec = ACTOR_INDEX.get(normalized)
    return spec.label if spec is not None else normalized.replace("-", " ").title()


def _default_actor_registry() -> dict[str, dict[str, str]]:
    return {
        actor.actor_id: {
            "label": actor.label,
            "model": actor.default_model,
            "resume_uuid": "",
        }
        for actor in DEFAULT_ACTORS
    }


def _actor_registry_from_state(state: dict[str, str]) -> dict[str, dict[str, str]]:
    raw = str(state.get(SESSION_ACTORS_ENV) or "").strip()
    registry = _default_actor_registry()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid actor registry: {exc}") from exc
        if not isinstance(payload, dict):
            raise SystemExit("invalid actor registry: expected an object")
        for actor_id, actor_payload in payload.items():
            normalized = _resolve_actor_name(str(actor_id))
            if not isinstance(actor_payload, dict):
                raise SystemExit(f"invalid actor registry payload for {normalized}")
            spec = ACTOR_INDEX.get(normalized)
            registry[normalized] = {
                "label": str(
                    actor_payload.get("label")
                    or (spec.label if spec else normalized.title())
                ),
                "model": str(
                    actor_payload.get("model")
                    or (spec.default_model if spec else "")
                ).strip(),
                "resume_uuid": str(actor_payload.get("resume_uuid") or "").strip(),
            }
    for actor_id, actor_payload in registry.items():
        spec = ACTOR_INDEX.get(actor_id)
        if spec is not None:
            actor_payload["label"] = actor_payload.get("label") or spec.label
            actor_payload["model"] = actor_payload.get("model") or spec.default_model
        if not actor_payload.get("resume_uuid"):
            actor_payload["resume_uuid"] = str(uuid.uuid4()).lower()
    return registry


def _actor_registry_json(registry: dict[str, dict[str, str]]) -> str:
    ordered = {
        actor_id: {
            "label": str(
                payload.get("label")
                or ACTOR_INDEX.get(
                    actor_id,
                    ActorSpec(actor_id, actor_id.title(), "", ()),
                ).label
            ),
            "model": str(payload.get("model") or ""),
            "resume_uuid": str(payload.get("resume_uuid") or ""),
        }
        for actor_id, payload in registry.items()
    }
    return json.dumps(ordered, sort_keys=True, separators=(",", ":"))


def _actor_ids_for_state(state: dict[str, str]) -> tuple[str, ...]:
    return tuple(_actor_registry_from_state(state))


def _actor_dir_path(work_dir: Path, actor_name: str) -> Path:
    return _actor_session_dir(work_dir, actor_name)


def _actor_state_link_root(work_dir: Path) -> Path:
    return state_dir_path(work_dir)


def _actor_state_path(work_dir: Path, actor_name: str) -> Path:
    return _actor_session_dir(work_dir, actor_name) / "state" / "actor.json"


def _actor_events_path(work_dir: Path, actor_name: str) -> Path:
    return _actor_session_dir(work_dir, actor_name) / "state" / "actor.jsonl"


def _actor_want_path(work_dir: Path, actor_name: str) -> Path:
    return _actor_session_dir(work_dir, actor_name) / WANT_FILE


def _actor_goal_path(work_dir: Path, actor_name: str) -> Path:
    return _actor_session_dir(work_dir, actor_name) / "GOAL.md"


def _canonical_work_root(session_dir: Path) -> Path:
    return session_dir.expanduser().resolve()


def _actor_session_dir(work_dir: Path, actor_name: str) -> Path:
    return actor_session_root(work_dir.resolve(), _normalize_actor_name(actor_name))


def _actor_is_selected(work_dir: Path, actor_name: str) -> bool:
    normalized = _normalize_actor_name(actor_name)
    resolved = work_dir.resolve()
    if session_actor(resolved) == normalized:
        return True
    actor_root = actor_session_root(resolved, normalized)
    return session_is_initialized(actor_root)


def _read_actor_state(work_dir: Path, actor_name: str) -> dict[str, object]:
    normalized = _normalize_actor_name(actor_name)
    if not _actor_is_selected(work_dir, normalized):
        return {
            "actor": normalized,
            "label": _actor_label(normalized),
            "status": "pending",
        }
    path = _actor_state_path(work_dir, normalized)
    if not path.exists():
        return {
            "actor": normalized,
            "label": _actor_label(normalized),
            "status": "pending",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid actor state file: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"invalid actor state file: {path}")
    data.setdefault("actor", normalized)
    data.setdefault("label", _actor_label(normalized))
    data["status"] = _normalize_actor_status(data.get("status") or "pending")
    return data


def _write_actor_state(
    work_dir: Path,
    actor_name: str,
    payload: dict[str, object],
) -> Path:
    normalized = _normalize_actor_name(actor_name)
    state_dir = _actor_state_link_root(work_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _actor_state_path(work_dir, normalized)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = _read_actor_state(work_dir, normalized)
    for key, value in payload.items():
        if value is None:
            merged.pop(key, None)
            continue
        merged[key] = value
    merged["actor"] = normalized
    merged["label"] = _actor_label(normalized)
    merged["updated_at"] = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = _normalize_actor_status(merged.get("status") or "pending")
    merged["status"] = status
    if status not in ACTOR_STATE_STATUS:
        raise SystemExit(f"invalid actor status: {status}")
    path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _finish(text: str) -> str:
    return dedent(text).strip() + "\n"


def _bootstrap_want() -> str:
    return _finish(
        """
        # Want

        > Generated by `gotta session init`.
        > This is a rewrite-on-purpose intent frame, not an append surface.

        This file carries the current expression of what the operator wants.

        Rewrite it intentionally from live context. Keep it current as the desired
        outcome, pressure, and deliverable sharpen.

        ## Want

        _empty_
        """
    )


def _bootstrap_actor_want(*, label: str) -> str:
    actor_name = _normalize_actor_name(label)
    return _finish(
        f"""
        # Actor Want Placeholder

        > Generated by `gotta actor bind {label}`.
        > Rewrite this file before actor launch.

        This is the operator-authored actor-local intent frame for {label}.

        It is a live charter, not an append surface and not a hidden template.
        Rewrite it intentionally so the actor starts from a real contract instead of
        trying to infer one from the shared session.

        Native rewrite:

        - `{_actor_charter_command(actor_name, 'want')}`
        - `{_actor_charter_command(actor_name, 'want', mode='--from-file <path>')}`
        """
    )


def _bootstrap_goal(
    *,
    repo: Path | None,
    work_dir: Path,
    agents_src: Path | None,
    voice_src: Path | None,
) -> str:
    markers = "".join(
        line
        for line in (
            f"- Repository: `{repo}`\n" if repo is not None else "",
            "- AGENTS.md present in this directory\n" if agents_src else "",
            "- VOICE.md present in this directory\n" if voice_src else "",
        )
    )
    return _finish(
        f"""
        # Seed Goal Placeholder

        > Generated by `gotta session init`.
        > This is an intentional rewrite target, not an append log.

        This file is a rewrite-on-purpose execution charter. Rewrite it from live
        context before launching actors or treating it as current truth.

        Bootstrap facts:

        - Operating directory: `{work_dir}`
        - Durable intent frame: `WANT.md`
        - Canonical coordination state: `state/todo.jsonl`
        - Readable coordination view: `TODO.md`
        - Supporting canonical logs: `state/logs.jsonl`, `state/oops.jsonl`, `state/env`
        - Readable projections: `LOGS.md`, `OOPS.md`
        {markers}
        GOAL.md rule:

        - Read `WANT.md` first.
        - Read `TODO.md`, `LOGS.md`, and `OOPS.md`.
        - Treat `state/todo.jsonl`, `state/logs.jsonl`, and `state/oops.jsonl` as the
          canonical continuous state behind those readable views.
        - Do not worry if `session manifest`, `session leads`, `session graph`, or
          `session analyze` are empty before the first retrieval; materialize one
          strong source anchor first, then use those session surfaces to continue.
          `session leads` shows the current best leads first and keeps the
          provenance signals visible so you can choose between materialized,
          native, search-seed, and otherwise promising unmaterialized branches.
        - Rewrite `GOAL.md` in place so it reflects the current moment.
        - Keep `GOAL.md` durable for actor launches and reruns.
        - Launch actors once `WANT.md` and `GOAL.md` are real.
        """
    )


def _bootstrap_actor_goal(*, label: str, actor_dir: Path, work_dir: Path) -> str:
    actor_name = _normalize_actor_name(label)
    return _finish(
        f"""
        # Seed Actor Goal Placeholder

        > Generated by `gotta actor bind {label}`.
        > Rewrite this file before actor launch.

        This file turns the operator-authored actor `WANT.md` into a concrete
        evidence-collection contract for {label}.

        Bootstrap facts:

        - Actor session root: `{actor_dir}`
        - Actor-local logs: `{actor_dir / 'LOGS.md'}`, `{actor_dir / 'OOPS.md'}`
        - Actor-local checklist: `{actor_dir / 'TODO.md'}`
        - Actor-local notes: `{actor_dir / 'NOTES.md'}`

        Rewrite rule:

        - Read actor-local `WANT.md` first.
        - Turn that charter into concrete evidence-collection steps.
        - Treat `TODO.md` as the live actor-local checklist.
        - Append durable notes during the run; do not wait until the end.
        - Do not author the final dossier from this session.

        Native rewrite:

        - `{_actor_charter_command(actor_name, 'goal')}`
        - `{_actor_charter_command(actor_name, 'goal', mode='--from-file <path>')}`
        """
    )


def _bootstrap_oops() -> str:
    return render_oops_markdown([])


def _seed_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _actor_readme(label: str) -> str:
    actor_name = _normalize_actor_name(label)
    return dedent(
        f"""\
        # {label} Session

        > Generated by `gotta actor bind {label}`.
        > This file is a bootstrap guide, not a live append surface.

        This directory is the canonical {label.lower()} actor root for one shared gotta session.

        Readable actor surfaces:

        - `WANT.md`: actor-local intent frame seeded here, then rewritten before launch
        - `GOAL.md`: actor-local goal seeded here, then rewritten before launch
        - `TODO.md`: actor-local checklist projected from `state/todo.jsonl`
        - `NOTES.md`: projected running notes plus live actor state
        - `LOGS.md` / `OOPS.md`: actor-local continuous logs

        Canonical truth:

        - this session root is real; operate from here with `gotta ...`
        - actor selection is explicit actor selection inside one shared session, not path traversal under `actors/`
        - actor-local checklist truth is `state/todo.jsonl`
        - actor lifecycle truth is `state/actor.json` and `state/actor.jsonl`
        - actor notes truth is `state/notes.jsonl`
        - actor-local continuous truth is `state/logs.jsonl` and `state/oops.jsonl`
        - shared cross-actor truth is the shared evidence web at `content/`
        - provenance still belongs to the actor that produced the evidence
        - actor-local `WANT.md` and `GOAL.md` are live operator-authored charters, not hidden templates
        - rewrite actor-local charters with `{_actor_charter_command(actor_name, 'want')}` and `{_actor_charter_command(actor_name, 'goal')}` before launch
        - prefer native `gotta` surfaces over shell-side spelunking
        - prefer `gotta notes ...` for actor-note mutation
        - do not author the final dossier from this session; stop at evidence and handoff notes
        """
    )


def _seed_actor_surface(
    actor_dir: Path,
    label: str,
    *,
    work_dir: Path,
) -> None:
    actor_dir.mkdir(parents=True, exist_ok=True)
    _seed_file(actor_dir / "README.md", _actor_readme(label))
    _seed_file(actor_dir / WANT_FILE, _bootstrap_actor_want(label=label))
    _seed_file(
        actor_dir / "GOAL.md",
        _bootstrap_actor_goal(
            label=label,
            actor_dir=actor_dir,
            work_dir=work_dir,
        ),
    )


def _reset_orphaned_actor_surface(actor_dir: Path) -> None:
    managed_paths = (
        actor_dir / "README.md",
        actor_dir / WANT_FILE,
        actor_dir / "GOAL.md",
        actor_dir / "TODO.md",
        actor_dir / "NOTES.md",
        actor_dir / "content",
        actor_dir / "session",
    )
    for path in managed_paths:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.exists():
            shutil.rmtree(path)


def _actor_script(*, work_dir: Path, actor_dir: Path, actor_name: str) -> str:
    actor_name = _normalize_actor_name(actor_name)
    actor_session = _actor_session_dir(work_dir, actor_name)
    return dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail

        ws={sh_quote(str(actor_session))}
        if [[ "${{1:-}}" == "--help" || "${{1:-}}" == "-h" ]]; then
          exec gotta actor launch {actor_name} --session "$ws" --help
        fi
        exec gotta actor launch {actor_name} --session "$ws" "$@"
        """
    )


def _load_session_state(work_dir: Path) -> dict[str, str]:
    return load_state_env_at_root(work_dir)


def _ensure_symlink(path: Path, target: Path) -> None:
    desired = os.path.relpath(target, start=path.parent)
    if path.is_symlink():
        current = os.readlink(path)
        if current == desired:
            return
        path.unlink()
    elif path.exists():
        if path.is_file():
            path.unlink()
        else:
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.symlink_to(desired)
    except FileExistsError:
        if path.is_symlink():
            current = os.readlink(path)
            if current == desired:
                return
            path.unlink()
        elif path.exists() and path.is_file():
            path.unlink()
        else:
            raise
        path.symlink_to(desired)


def _append_chunk(path: Path, chunk: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, chunk.encode("utf-8"))
    finally:
        os.close(fd)


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    _append_chunk(path, json.dumps(payload, sort_keys=True) + "\n")


def _actor_todo_marker(actor_name: str, phase: str) -> str:
    return f"actor-{_normalize_actor_name(actor_name)}-{phase}"


def _actor_todo_redirect(actor_name: str, phase: str) -> str:
    actor = _normalize_actor_name(actor_name)
    label = _actor_label(actor)
    if phase == "initial":
        return (
            f"that TODO item is owned by {label} lifecycle; inspect with "
            f"`gotta actor status {actor}` and advance it through "
            f"`gotta actor complete {actor}` or `gotta actor signoff {actor} ...`"
        )
    if phase == "complete":
        return (
            f"that TODO item is owned by {label} lifecycle; use "
            f"`gotta actor complete {actor}` once the actor run has materially landed"
        )
    if phase == "dispositioned":
        return (
            f"that TODO item is owned by {label} disposition; use "
            f"`gotta actor signoff {actor} --summary ...` after review"
        )
    raise ValueError(f"unknown actor TODO phase: {phase}")


def _managed_todo_redirect(managed_key: str) -> str:
    if managed_key == FINAL_SIGNOFF_MARKER:
        return (
            "that TODO item is owned by final actor sign-off; inspect all actors with "
            "`gotta actor status` and sign off each actor through "
            "`gotta actor signoff ...`"
        )
    prefix = "actor-"
    suffixes = ("-initial", "-complete", "-dispositioned")
    if managed_key.startswith(prefix):
        for suffix in suffixes:
            if managed_key.endswith(suffix):
                actor = managed_key[len(prefix) : -len(suffix)]
                phase = suffix.removeprefix("-")
                return _actor_todo_redirect(actor, phase)
    return (
        "that TODO item is managed by native actor state; use "
        "`gotta actor ...` to advance it instead of "
        "`gotta todo check`"
    )


def _selected_actor_ids(work_dir: Path) -> tuple[str, ...]:
    registry = _actor_registry_from_state(_load_session_state(work_dir))
    selected: list[str] = []
    for actor_id in registry:
        if _actor_is_selected(work_dir, actor_id):
            selected.append(actor_id)
    return tuple(selected)


def _find_upward_file(start: Path, name: str) -> Path | None:
    for directory in (start.resolve(), *start.resolve().parents):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _copy_if_present(source: Path | None, destination: Path) -> None:
    if source is None or not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _default_repo() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def _ensure_state_exports(work_dir: Path) -> dict[str, str]:
    dirs = resolve_dirs(CommonOptions(session_dir=str(work_dir)), create=True)
    existing = load_state_env_at_root(work_dir)
    preserved = {
        key: value
        for key, value in existing.items()
        if key not in {SESSION_ENV, CONTENT_ENV, SESSION_REPO_ENV}
    }
    write_session_state(dirs, preserved)
    dirs.session_dir.joinpath("bin").mkdir(parents=True, exist_ok=True)
    return env_mapping(dirs)


def _write_state_file(work_dir: Path, values: dict[str, str]) -> None:
    existing = dict(load_state_env_at_root(work_dir).items())
    base_exports = _ensure_state_exports(work_dir)
    merged = {
        **existing,
        **base_exports,
        **values,
    }
    ordered_keys = [SESSION_ENV, CONTENT_ENV, SESSION_REPO_ENV, *SESSION_STATE_KEYS]
    extras = [key for key in merged if key not in ordered_keys]
    lines: list[str] = []
    for key in ordered_keys + sorted(extras):
        value = merged.get(key)
        if value is None:
            continue
        lines.append(f"export {key}={sh_quote(str(value))}")
    path = state_env_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return
        except OSError:
            pass
    path.write_text(text, encoding="utf-8")


def _seed_session_files(
    *,
    session_dir: Path,
    repo: Path | None,
    actors: dict[str, dict[str, str]],
    agents_src: Path | None,
    voice_src: Path | None,
) -> None:
    bin_dir = session_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _copy_if_present(agents_src, session_dir / "AGENTS.md")
    _copy_if_present(voice_src, session_dir / "VOICE.md")
    _seed_file(session_dir / WANT_FILE, _bootstrap_want())
    _seed_file(
        session_dir / "GOAL.md",
        _bootstrap_goal(
            repo=repo,
            work_dir=session_dir,
            agents_src=agents_src,
            voice_src=voice_src,
        ),
    )
    _seed_file(session_dir / "OOPS.md", _bootstrap_oops())
    if not todo_state_path(session_dir).exists():
        create_todo_item(
            session_dir,
            section="Status",
            text=f"Session root established at `{session_dir}`",
            checked=True,
        )
        create_todo_item(
            session_dir,
            section="Status",
            text=f"{WANT_FILE} rewritten by the active actor from live context",
        )
        create_todo_item(
            session_dir,
            section="Status",
            text="GOAL.md rewritten by the active actor from live context",
        )
        create_todo_item(
            session_dir,
            section="Status",
            text="TODO.md expanded by the active actor into a real working checklist",
        )
        create_todo_item(
            session_dir,
            section="Status",
            text="Decide whether to bind an actor with `gotta actor bind ...` and actually consult them if it helps.",
        )
        sync_todo_projection(session_dir)
    if not logs_state_path(session_dir).exists():
        bootstrap_lines = [
            "Session surface initialized by `gotta session init`",
            f"Session root: `{session_dir}`",
            "Actors:",
        ]
        if repo is not None:
            bootstrap_lines.insert(1, f"Repository: `{repo}`")
        for actor_id, payload in actors.items():
            bootstrap_lines.append(
                f"  - {str(payload.get('label') or _actor_label(actor_id))}: model = `{payload.get('model', '')}`, resume = `{payload.get('resume_uuid', '')}`"
            )
        if agents_src:
            bootstrap_lines.append("Imported `AGENTS.md`")
        if voice_src:
            bootstrap_lines.append("Imported `VOICE.md`")
        bootstrap_lines.append("Seeded as the durable execution log for this session")
        append_log_record(session_dir, message="\n".join(bootstrap_lines))
        sync_logs_projection(session_dir)


def scaffold_session(session_dir: Path, *, repo: Path | None = None) -> None:
    current = _load_session_state(session_dir)
    existing_repo = str(current.get(SESSION_REPO_ENV) or "").strip()
    repo_path: Path | None
    if repo is not None:
        repo_path = repo.expanduser().resolve()
    elif existing_repo:
        repo_path = Path(existing_repo).expanduser().resolve()
    else:
        try:
            repo_path = _default_repo().resolve()
        except (subprocess.CalledProcessError, FileNotFoundError):
            repo_path = None
    actors = _actor_registry_from_state(current)
    created_at = current.get(SESSION_CREATED_ENV) or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    home = Path.home()
    agents_src = _find_upward_file(repo_path, "AGENTS.md") if repo_path else None
    voice_src = _find_upward_file(repo_path, "VOICE.md") if repo_path else None
    if agents_src is None and (home / "AGENTS.md").is_file():
        agents_src = home / "AGENTS.md"
    if voice_src is None and (home / "VOICE.md").is_file():
        voice_src = home / "VOICE.md"
    _write_state_file(
        session_dir,
        {
            SESSION_REPO_ENV: str(repo_path or ""),
            SESSION_INITIALIZED_ENV: "1",
            SESSION_CREATED_ENV: created_at,
            SESSION_ACTORS_ENV: _actor_registry_json(actors),
            SESSION_WANT_PATH_ENV: str(session_dir / WANT_FILE),
            SESSION_ACTORS_SOURCE_ENV: str(agents_src or ""),
            SESSION_VOICE_SOURCE_ENV: str(voice_src or ""),
        },
    )
    _seed_session_files(
        session_dir=session_dir,
        repo=repo_path,
        actors=actors,
        agents_src=agents_src,
        voice_src=voice_src,
    )
    _record_session_activity(
        session_dir,
        plugin="session",
        surface="session.init",
        action="init",
        target=session_dir / WANT_FILE,
        detail="scaffolded WANT.md, GOAL.md, TODO.md, LOGS.md, and OOPS.md",
    )


def _ensure_actor_session_exports(
    actor_dir: Path,
    *,
    content_dir: Path,
    session_dir: Path,
) -> dict[str, str]:
    dirs = resolve_dirs(
        CommonOptions(session_dir=str(actor_dir), content_dir=str(content_dir)),
        create=True,
    )
    write_session_state(dirs)
    dirs.session_dir.joinpath("bin").mkdir(parents=True, exist_ok=True)
    _ensure_symlink(actor_dir / "content", content_dir)
    _ensure_symlink(actor_dir / "session", session_dir)
    return env_mapping(dirs)


def _ensure_actor_parent_links(session_root: Path, actor_name: str, actor_dir: Path) -> None:
    return None


def _ensure_actor_initial_todo(actor_dir: Path) -> None:
    if todo_items(actor_dir):
        return
    create_todo_item(
        actor_dir,
        section="Status",
        text="Rewrite WANT.md and GOAL.md into a concrete actor-local checklist in TODO.md.",
    )
    create_todo_item(
        actor_dir,
        section="Status",
        text="Materialize the first strong source anchor and append a first durable note as soon as it lands.",
    )
    create_todo_item(
        actor_dir,
        section="Status",
        text="Append another durable note after each material evidence wave or plan change; do not request completion or sign-off with empty NOTES.md.",
    )


def _session_surface_path(work_dir: Path, surface: str) -> Path:
    return work_dir / surface


def _record_session_activity(
    work_dir: Path,
    *,
    plugin: str,
    surface: str,
    action: str,
    target: Path | None = None,
    locator: str = "",
    preferred_name: str = "",
    follow_command: str = "",
    detail: str = "",
) -> None:
    if target is not None:
        resolved = target.resolve()
        todo_surface = _session_surface_path(work_dir, "TODO.md").resolve()
        logs_surface = _session_surface_path(work_dir, "LOGS.md").resolve()
        oops_surface = _session_surface_path(work_dir, "OOPS.md").resolve()
        if resolved == todo_surface:
            resolved_locator = _session_relative_locator(work_dir, todo_state_path(work_dir))
            resolved_name = "TODO.md"
            resolved_follow = "gotta read 'TODO.md'"
        elif resolved == logs_surface:
            resolved_locator = _session_relative_locator(work_dir, logs_state_path(work_dir))
            resolved_name = "LOGS.md"
            resolved_follow = "gotta read 'LOGS.md'"
        elif resolved == oops_surface:
            resolved_locator = _session_relative_locator(work_dir, oops_log_path(work_dir))
            resolved_name = "OOPS.md"
            resolved_follow = "gotta read 'OOPS.md'"
        else:
            resolved_locator = _session_relative_locator(work_dir, resolved)
            resolved_name = resolved.name
            resolved_follow = f"gotta read {resolved_locator!r}"
    else:
        resolved_locator = locator.strip() or f"{plugin}:{surface}"
        resolved_name = preferred_name.strip() or resolved_locator
        resolved_follow = follow_command.strip()
    append_activity_event(
        work_dir,
        {
            "plugin": plugin,
            "surface": surface,
            "action": action,
            "locator": resolved_locator,
            "preferred_name": preferred_name.strip() or resolved_name,
            "follow_command": follow_command.strip() or resolved_follow,
            "detail": detail,
            "time_field": "session_recorded_at",
        },
    )


def _append_actor_event(
    work_dir: Path,
    actor_name: str,
    *,
    event: str,
    detail: str = "",
    extra: dict[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {
        "timestamp": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actor": _normalize_actor_name(actor_name),
        "event": event,
        "detail": detail,
    }
    if extra:
        payload.update(extra)
    _append_jsonl(_actor_events_path(work_dir, actor_name), payload)
    if event != "heartbeat":
        _record_session_activity(
            work_dir,
            plugin="actor",
            surface="actor.lifecycle",
            action=event,
            locator=f"actor:{payload['actor']}",
            preferred_name=str(payload["actor"]),
            follow_command=f"gotta actor status {payload['actor']}",
            detail=detail or event,
        )


def _actor_log_line(session_root: Path, actor_name: str, message: str) -> None:
    append_log_record(session_root, message=f"[{actor_name}] {message}", actor=actor_name)


def _record_actor_projection_activity(
    session_root: Path,
    *,
    actor_name: str,
    surface: str,
    action: str,
    log_path: Path,
    projection_path: Path,
    detail: str,
) -> None:
    _record_session_activity(
        session_root,
        plugin="actor",
        surface=surface,
        action=action,
        locator=_session_relative_locator(session_root, log_path),
        preferred_name=projection_path.name,
        follow_command=f"gotta read {_session_relative_locator(session_root, projection_path)!r}",
        detail=detail,
    )


def _want_rewrite_pending(work_dir: Path) -> bool:
    want_path = _session_surface_path(work_dir, WANT_FILE)
    if not want_path.is_file():
        return True
    current = want_path.read_text(encoding="utf-8").strip()
    return current == _bootstrap_want().strip()


def _goal_rewrite_pending(goal_path: Path) -> bool:
    if not goal_path.is_file():
        return True
    return goal_path.read_text(encoding="utf-8").strip().startswith("# Seed Goal Placeholder")


def _actor_want_rewrite_pending(work_root: Path, actor_name: str) -> bool:
    path = _actor_want_path(work_root, actor_name)
    if not path.is_file():
        return True
    current = path.read_text(encoding="utf-8").strip()
    return current == _bootstrap_actor_want(label=_actor_label(actor_name)).strip()


def _actor_goal_rewrite_pending(work_root: Path, actor_name: str) -> bool:
    path = _actor_goal_path(work_root, actor_name)
    if not path.is_file():
        return True
    return path.read_text(encoding="utf-8").strip() == _bootstrap_actor_goal(
        label=_actor_label(actor_name),
        actor_dir=_actor_session_dir(work_root, actor_name),
        work_dir=work_root,
    ).strip()


def _actor_launch_blockers(work_root: Path, *, actor_name: str = "") -> list[str]:
    blockers: list[str] = []
    goal_path = _session_surface_path(work_root, "GOAL.md")
    if _want_rewrite_pending(work_root):
        blockers.append(f"rewrite `{_session_surface_path(work_root, WANT_FILE)}` first")
    if _goal_rewrite_pending(goal_path):
        blockers.append(f"rewrite `{goal_path}` from the current moment before launch")
    if actor_name:
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


def _actor_evidence_summary(work_dir: Path, actor_name: str) -> dict[str, object]:
    manifest_path = work_dir / "content" / "manifest.jsonl"
    entries: list[dict[str, object]] = []
    if manifest_path.exists():
        for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and str(
                payload.get("actor") or session_identity(work_dir)
            ) == _normalize_actor_name(actor_name):
                entries.append(payload)
    ordered = sorted(
        entries,
        key=lambda entry: (
            str(entry.get("fetched_at") or ""),
            str(entry.get("checksum") or ""),
        ),
        reverse=True,
    )
    return {
        "artifact_count": len(entries),
        "last_artifact_at": str(ordered[0].get("fetched_at") or "").strip()
        if ordered
        else "",
        "recent_artifacts": [
            {
                "locator": str(
                    entry.get("canonical_locator") or entry.get("locator") or ""
                ).strip(),
                "preferred_name": str(entry.get("preferred_name") or "data").strip(),
                "fetched_at": str(entry.get("fetched_at") or "").strip(),
            }
            for entry in ordered[:5]
        ],
    }


def _actor_evidence_note(evidence: dict[str, object]) -> str:
    artifact_count = int(evidence.get("artifact_count") or 0)
    if artifact_count <= 0:
        return ""
    noun = "artifact" if artifact_count == 1 else "artifacts"
    return (
        f"{artifact_count} actor-attributed {noun} already live in session manifest, "
        "timeline, leads, and graph."
    )


def _actor_activity_summary(event: str, detail: str) -> str:
    cleaned_detail = detail.strip()
    if event == "note":
        return cleaned_detail or "note"
    label = event.replace("_", " ")
    if cleaned_detail:
        return f"{label}: {cleaned_detail}"
    return label


def _actor_recent_activity(work_dir: Path, actor_name: str, *, limit: int = 5) -> dict[str, object]:
    path = _actor_events_path(work_dir, actor_name)
    if not path.exists():
        return {
            "recent_activity": [],
            "last_activity_at": "",
            "last_activity_summary": "",
        }
    events: list[dict[str, object]] = []
    for index, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event = str(payload.get("event") or "").strip()
        if not event or event == "heartbeat":
            continue
        timestamp = str(payload.get("timestamp") or "").strip()
        detail = str(payload.get("detail") or "").strip()
        events.append(
            {
                "timestamp": timestamp,
                "event": event,
                "detail": detail,
                "summary": _actor_activity_summary(event, detail),
                "_order": index,
            }
        )
    ordered = sorted(
        events,
        key=lambda item: (
            str(item.get("timestamp") or ""),
            int(item.get("_order") or 0),
        ),
        reverse=True,
    )
    recent_activity = [
        {
            "timestamp": str(item.get("timestamp") or ""),
            "event": str(item.get("event") or ""),
            "detail": str(item.get("detail") or ""),
            "summary": str(item.get("summary") or ""),
        }
        for item in ordered[:limit]
    ]
    latest = recent_activity[0] if recent_activity else {}
    return {
        "recent_activity": recent_activity,
        "last_activity_at": str(latest.get("timestamp") or ""),
        "last_activity_summary": str(latest.get("summary") or ""),
    }


def _actor_status_payload(work_dir: Path, actor_name: str) -> dict[str, object]:
    state = _read_actor_state(work_dir, actor_name)
    status = str(state.get("status") or "pending")
    requested_status = str(state.get("requested_status") or "")
    requested_summary = str(state.get("requested_summary") or "")
    requested_label = requested_disposition_label(state)
    heartbeat_at = str(state.get("heartbeat_at") or "")
    derived_status = status
    heartbeat_stale = False
    runtime_live: bool | None = None
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0:
        try:
            os.kill(pid, 0)
        except OSError:
            runtime_live = False
        else:
            runtime_live = True
    if status in {"starting", "active"} and heartbeat_at:
        try:
            heartbeat_dt = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
        except ValueError:
            heartbeat_dt = None
        if heartbeat_dt is not None:
            age = time.time() - heartbeat_dt.timestamp()
            state["heartbeat_age_seconds"] = round(age, 1)
            if age > ACTOR_STALL_SECONDS:
                derived_status = "stalled"
                heartbeat_stale = True
    elif status in {"starting", "active"} and not heartbeat_at:
        started_at = str(state.get("started_at") or "")
        try:
            started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            started_dt = None
        if started_dt is not None and (time.time() - started_dt.timestamp()) > ACTOR_STALL_SECONDS:
            derived_status = "stalled"
            heartbeat_stale = True
    signoff_at = str(state.get("signoff_at") or "")
    actor_dir = _actor_session_dir(work_dir, actor_name)
    notes_path = actor_dir / "NOTES.md"
    notes_ready = actor_notes_ready(work_dir, _normalize_actor_name(actor_name))
    evidence = _actor_evidence_summary(work_dir, actor_name)
    evidence_note = _actor_evidence_note(evidence)
    recent_activity = _actor_recent_activity(work_dir, actor_name)
    evidence_live = int(evidence["artifact_count"]) > 0
    if signoff_at:
        derived_status = "signed_off"
    notes_status = "present" if notes_ready else "empty" if notes_path.exists() else "missing"
    runtime_note = ""
    if status in {"starting", "active"} and runtime_live is False:
        if requested_status in {"completed", "failed", "signed_off"}:
            derived_status = requested_status
        else:
            derived_status = "awaiting_disposition"
        runtime_note = (
            " Actor runtime is no longer live, so this is awaiting an explicit "
            "completion, failure, or sign-off record."
        )
    if derived_status in {"starting", "active"} and evidence_live:
        derived_status = "producing_evidence"
    still_running = derived_status in ACTOR_RUNNING_STATUS and not heartbeat_stale
    request_note = ""
    if requested_status and derived_status not in ACTOR_TERMINAL_STATUS:
        if requested_label == "stop":
            if derived_status == "stalled":
                request_note = (
                    " Operator already requested a graceful stop"
                    + (f" ({requested_summary})." if requested_summary else ".")
                    + " Because the heartbeat is stale, you can settle now with "
                    f"`gotta actor settle {_normalize_actor_name(actor_name)}`."
                )
            else:
                request_note = (
                    " Operator already requested a graceful stop"
                    + (f" ({requested_summary})." if requested_summary else ".")
                    + " The actor should wind down, append one final durable note, and sign off."
                )
        elif derived_status == "stalled":
            request_note = (
                f" Operator already requested `{requested_label}`"
                + (f" ({requested_summary})." if requested_summary else ".")
                + " Because the heartbeat is stale, you can settle now with "
                f"`gotta actor settle {_normalize_actor_name(actor_name)}`."
            )
        else:
            request_note = (
                f" Operator already requested `{requested_label}`"
                + (f" ({requested_summary})." if requested_summary else ".")
                + " That pending disposition will become authoritative automatically "
                "when the actor runtime exits."
            )
    if derived_status == "producing_evidence":
        if notes_ready:
            next_step = (
                "actor is still active and producing evidence artifacts. "
                + (evidence_note + " " if evidence_note else "")
                + "Use NOTES.md for live actor visibility; recheck `gotta actor status "
                f"{_normalize_actor_name(actor_name)}` shortly before closing the actor out."
                + request_note
                + runtime_note
            )
        else:
            next_step = (
                "actor is still active and producing evidence artifacts, but NOTES.md is still empty. "
                + (evidence_note + " " if evidence_note else "")
                + "Append a durable note before requesting completion or sign-off, then recheck "
                f"`gotta actor status {_normalize_actor_name(actor_name)}` shortly."
                + request_note
                + runtime_note
            )
    elif derived_status in {"starting", "active"} and notes_ready:
        next_step = (
            "actor is still active and has already started landing durable notes. "
            "Use NOTES.md for live actor visibility; recheck `gotta actor status "
            f"{_normalize_actor_name(actor_name)}` shortly before closing the actor out."
            + request_note
            + runtime_note
        )
    elif derived_status == "awaiting_disposition":
        next_step = (
            "actor runtime is no longer running, but no durable terminal lifecycle was recorded yet. "
            + (evidence_note + " " if evidence_note else "")
            + "Inspect NOTES.md plus the shared evidence web, then settle with "
            f"`gotta actor settle {_normalize_actor_name(actor_name)}`"
            + (
                f" to honor the pending `{requested_label}` request."
                if requested_status
                else " to record the authoritative terminal disposition."
            )
            + request_note
        )
    elif derived_status == "stalled" and (notes_ready or evidence_live):
        next_step = (
            "actor heartbeat is stale, but material actor state already exists in NOTES.md or the "
            "shared evidence web. "
            + (evidence_note + " " if evidence_note else "")
            + "Inspect the notes and decide whether to wait, relaunch, or disposition manually."
            + request_note
        )
    elif derived_status == "completed" and (notes_ready or evidence_live):
        if notes_ready:
            next_step = (
                "actor run is complete; inspect NOTES.md plus the shared evidence web, then record "
                "durable sign-off with "
                f"`gotta actor signoff {_normalize_actor_name(actor_name)} --summary ...`."
            )
        else:
            next_step = (
                "actor run is complete and evidence landed, but NOTES.md is still empty. Wait for a "
                "durable note or sign off intentionally only if you are explicitly accepting an "
                "evidence-only actor contribution."
            )
    elif derived_status == "incomplete":
        next_step = (
            "actor finished without material notes or evidence. Decide whether to relaunch, "
            "fail, or sign off intentionally."
        )
    elif derived_status == "failed" and evidence_note:
        next_step = (
            "actor was manually marked failed, but evidence already landed in shared state. "
            + evidence_note
            + " Keep or reject that evidence intentionally instead of assuming it vanished."
        )
    else:
        next_step = ""
    return {
        **state,
        "status": derived_status,
        "state_path": str(_actor_state_path(work_dir, actor_name)),
        "events_path": str(_actor_events_path(work_dir, actor_name)),
        "actor_dir": str(actor_dir),
        "notes_path": str(notes_path),
        "notes_status": notes_status,
        "notes_ready": notes_ready,
        "evidence_live": bool(evidence_live),
        "evidence_note": evidence_note,
        "requested_status": requested_status,
        "requested_summary": requested_summary,
        "requested_label": requested_label,
        "requested_pending": bool(
            requested_status and derived_status not in ACTOR_TERMINAL_STATUS
        ),
        "still_running": still_running,
        "runtime_live": runtime_live,
        "review_ready": bool(
            derived_status in {"completed", "signed_off"} and (notes_ready or evidence_live)
        ),
        "next_step": next_step,
        **recent_activity,
        **evidence,
    }


def _ensure_actor_todo_items(work_dir: Path, actor_name: str) -> None:
    actor = _normalize_actor_name(actor_name)
    label = _actor_label(actor)
    ensure_managed_todo_item(
        work_dir,
        section="Actor Checklist",
        text=f"Initial actor pass collected from {label}",
        managed_key=_actor_todo_marker(actor, "initial"),
    )
    ensure_managed_todo_item(
        work_dir,
        section="Actor Checklist",
        text=f"{label} run materially complete",
        managed_key=_actor_todo_marker(actor, "complete"),
    )
    ensure_managed_todo_item(
        work_dir,
        section="Actor Checklist",
        text=f"{label} findings dispositioned",
        managed_key=_actor_todo_marker(actor, "dispositioned"),
    )
    ensure_managed_todo_item(
        work_dir,
        section="Actor Checklist",
        text="Final actor sign-off collected after edits for the chosen team",
        managed_key=FINAL_SIGNOFF_MARKER,
    )


def _sync_actor_todo_state(work_dir: Path) -> None:
    actor_ids = _selected_actor_ids(work_dir)
    actor_payloads = {actor: _actor_status_payload(work_dir, actor) for actor in actor_ids}
    launched_actor_ids = [
        actor
        for actor in actor_ids
        if str(actor_payloads[actor].get("status") or "pending") not in {"pending", "bound"}
    ]
    for actor_name in launched_actor_ids:
        _ensure_actor_todo_items(work_dir, actor_name)
    items_by_key = {
        str(item.get("managed_key") or ""): item for item in todo_items(work_dir) if item.get("managed_key")
    }
    for actor_name in launched_actor_ids:
        payload = actor_payloads[actor_name]
        materially_complete = bool(payload.get("notes_ready") or payload.get("evidence_live"))
        terminal = str(payload.get("status") or "") in {"completed", "failed", "rejected", "signed_off", "incomplete"}
        signed_off = str(payload.get("status") or "") == "signed_off"
        for marker, checked in (
            (_actor_todo_marker(actor_name, "initial"), materially_complete),
            (_actor_todo_marker(actor_name, "complete"), terminal),
            (_actor_todo_marker(actor_name, "dispositioned"), signed_off),
        ):
            item = items_by_key.get(marker)
            if item is None:
                continue
            updated = set_todo_checked(work_dir, str(item["id"]), checked=checked)
            if updated is not None:
                items_by_key[marker] = updated
    final_item = items_by_key.get(FINAL_SIGNOFF_MARKER)
    final_checked = bool(launched_actor_ids) and all(
        str(actor_payloads[actor].get("status") or "") == "signed_off"
        for actor in launched_actor_ids
    )
    if final_item is not None:
        set_todo_checked(work_dir, str(final_item["id"]), checked=final_checked)


def _sync_actor_projection_surfaces(work_dir: Path, actor_name: str) -> None:
    actor = _normalize_actor_name(actor_name)
    sync_actor_notes_projection(
        work_dir,
        actor,
        label=_actor_label(actor),
        status_payload=_actor_status_payload(work_dir, actor),
    )


def _actor_launch_command(work_dir: Path, actor_name: str) -> str:
    return f"gotta actor launch {actor_name} --session {sh_quote(str(work_dir))}"


def _ensure_actor_surface(work_dir: Path, actor_name: str) -> Path:
    actor_name = _normalize_actor_name(actor_name)
    state = _load_session_state(work_dir)
    actors = _actor_registry_from_state(state)
    actor = actors.get(actor_name)
    if actor is None:
        raise SystemExit(f"unknown actor: {actor_name}")
    repo_raw = str(state.get(SESSION_REPO_ENV) or "").strip()
    actor_dir = actor_session_root(work_dir, actor_name)
    bin_path = work_dir / "bin" / actor_name
    if actor_dir != work_dir:
        if actor_dir.exists() and not _actor_is_selected(work_dir, actor_name):
            _reset_orphaned_actor_surface(actor_dir)
        _seed_actor_surface(
            actor_dir,
            _actor_label(actor_name),
            work_dir=work_dir,
        )
        _copy_if_present(work_dir / "AGENTS.md", actor_dir / "AGENTS.md")
        _copy_if_present(work_dir / "VOICE.md", actor_dir / "VOICE.md")
        _ensure_actor_session_exports(
            actor_dir,
            content_dir=work_dir / "content",
            session_dir=work_dir / "session" if (work_dir / "session").exists() else work_dir,
        )
        _ensure_actor_parent_links(work_dir, actor_name, actor_dir)
    else:
        _seed_file(actor_dir / "README.md", _actor_readme(_actor_label(actor_name)))
        _ensure_state_exports(actor_dir)
    _write_state_file(
        actor_dir,
        {
            SESSION_INITIALIZED_ENV: "1",
            SESSION_REPO_ENV: repo_raw,
            SESSION_CREATED_ENV: str(state.get(SESSION_CREATED_ENV) or ""),
            SESSION_ACTORS_ENV: _actor_registry_json(actors),
            SESSION_WANT_PATH_ENV: str(actor_dir / WANT_FILE),
            SESSION_ACTORS_SOURCE_ENV: str(state.get(SESSION_ACTORS_SOURCE_ENV) or ""),
            SESSION_VOICE_SOURCE_ENV: str(state.get(SESSION_VOICE_SOURCE_ENV) or ""),
            SESSION_ACTOR_ENV: actor_name,
        },
    )
    if not session_surface_initialized(actor_dir):
        repo_path = Path(repo_raw).expanduser().resolve() if repo_raw else None
        scaffold_session(actor_dir, repo=repo_path)
    _ensure_actor_initial_todo(actor_dir)
    _write_actor_state(
        work_dir,
        actor_name,
        {
            "status": str(_read_actor_state(work_dir, actor_name).get("status") or "pending"),
            "notes_path": str(actor_notes_surface_path(work_dir, actor_name)),
        },
    )
    _sync_actor_projection_surfaces(work_dir, actor_name)
    if actor_dir != work_dir:
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        bin_path.write_text(
            _actor_script(
                work_dir=work_dir,
                actor_dir=actor_dir,
                actor_name=actor_name,
            ),
            encoding="utf-8",
        )
        bin_path.chmod(0o755)
    return actor_dir


def _bind_actor(session_root: Path, actor_name: str) -> str:
    actor = _normalize_actor_name(actor_name)
    bin_path = session_root / "bin" / actor
    already_bound = _actor_is_selected(session_root, actor) and bin_path.exists()
    _ensure_actor_surface(session_root, actor)
    current_status = str(_read_actor_state(session_root, actor).get("status") or "pending")
    if current_status in {
        "",
        "pending",
        "completed",
        "failed",
        "incomplete",
        "rejected",
        "signed_off",
    }:
        _write_actor_state(session_root, actor, {"status": "bound"})
        _sync_actor_projection_surfaces(session_root, actor)
    launch_cmd = _actor_launch_command(session_root, actor)
    actor_want = _actor_dir_path(session_root, actor) / WANT_FILE
    actor_goal = _actor_dir_path(session_root, actor) / "GOAL.md"
    actor_todo = _actor_dir_path(session_root, actor) / "TODO.md"
    want_cmd = _actor_charter_command(actor, "want")
    goal_cmd = _actor_charter_command(actor, "goal")
    actor_blockers = _actor_launch_blockers(session_root, actor_name=actor)
    if actor_blockers:
        suffix = (
            f"; not launched. Rewrite `{actor_want}` and `{actor_goal}` for {_actor_label(actor)} with `{want_cmd}` and `{goal_cmd}` first. "
            f"`{actor_todo}` is already seeded with a minimal actor-local checklist and you may extend it before launch if useful, "
            f"then launch with `{launch_cmd}` when you actually want {_actor_label(actor)} to start"
        )
    else:
        suffix = (
            f"; not launched. `{actor_want}` and `{actor_goal}` are already real. "
            f"`{actor_todo}` is already seeded with a minimal actor-local checklist and you may extend it before launch if useful, "
            f"then launch with `{launch_cmd}` when you actually want {_actor_label(actor)} to start"
        )
    if already_bound:
        return f"{_actor_label(actor)} already bound{suffix}"
    _append_actor_event(session_root, actor, event="bound", detail="bound actor session")
    _actor_log_line(session_root, actor, "bound session")
    return (
        f"bound {_actor_label(actor)} session, seeded actor-local WANT/GOAL placeholders, seeded actor-local TODO, launch shim, actor-local logs/oops, and shared evidence access"
        f"{suffix}"
    )


def _read_text_source(
    *,
    session_root: Path,
    inline: str | None,
    from_file: str | None,
    use_stdin: bool,
    input_name: str,
) -> str:
    used = int(bool(inline)) + int(bool(from_file)) + int(bool(use_stdin))
    if used > 1:
        raise SystemExit(f"use only one {input_name} source")
    if from_file:
        if from_file == "-":
            return sys.stdin.read()
        return session_relative_path(session_root, from_file).read_text(encoding="utf-8")
    if use_stdin:
        return sys.stdin.read()
    if inline is not None:
        return inline
    if stdin_has_readable_text():
        return sys.stdin.read()
    raise SystemExit(
        f"missing {input_name}; pass inline text, use --stdin, use --from-file, or pipe stdin"
    )


def _read_text_items_source(
    *,
    session_root: Path,
    inline_items: list[str],
    from_file: str | None,
    use_stdin: bool,
    input_name: str,
) -> list[str]:
    used = int(bool(inline_items)) + int(bool(from_file)) + int(bool(use_stdin))
    if used > 1:
        raise SystemExit(f"use only one {input_name} source")
    if from_file:
        if from_file == "-":
            raw = sys.stdin.read()
        else:
            raw = session_relative_path(session_root, from_file).read_text(encoding="utf-8")
    elif use_stdin:
        raw = sys.stdin.read()
    elif inline_items:
        raw = "\n".join(inline_items)
    elif stdin_has_readable_text():
        raw = sys.stdin.read()
    else:
        raise SystemExit(
            f"missing {input_name}; pass one or more inline items, use --stdin, use --from-file, or pipe stdin"
        )
    items: list[str] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        if normalized.startswith("- [ ] "):
            normalized = normalized[6:]
        elif normalized.startswith("- "):
            normalized = normalized[2:]
        items.append(normalized)
    if not items:
        raise SystemExit(f"missing {input_name}")
    return items


def _normalize_entry_text(text: str, *, input_name: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not normalized.strip():
        raise SystemExit(f"missing {input_name}")
    return normalized
