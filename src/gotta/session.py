"""Shared session-surface helpers for charters, state, and linked peers."""

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
from typing import Callable
import uuid

from gotta.compat import UTC, datetime
from gotta.content import (
    CONTENT_ENV,
    SESSION_ENV,
    SESSION_REPO_ENV,
    WORK_INITIALIZED_ENV,
    CommonOptions,
    append_activity_event,
    discover_state_env,
    env_mapping,
    load_state_env_at_root,
    resolve_dirs,
    resolve_session_reference,
    session_is_initialized,
    session_relative_path,
    sh_quote,
    state_dir_path,
    state_env_path,
    stdin_has_readable_text,
    work_is_initialized,
    write_session_state,
)
from gotta.friction import oops_log_path, render_oops_markdown
from gotta.helptext import format_long_help, is_long_help_request
from gotta.logs import append_log_record, logs_state_path, sync_logs_projection
from gotta.notes import (
    peer_notes_ready,
    peer_notes_surface_path,
    sync_peer_notes_projection,
)
from gotta.peer import (
    PEER_SESSION_ACTOR_ENV,
    normalize_peer_name as _shared_normalize_peer_name,
    peer_link_path,
    session_actor,
    peer_state_link_dir,
    peer_session_root,
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


GOTTA_WORK_ACTORS_JSON = "GOTTA_WORK_ACTORS_JSON"
PEER_STATE_STATUS = {
    "pending",
    "configured",
    "starting",
    "active",
    "stalled",
    "completed",
    "failed",
    "signed_off",
}
PEER_TERMINAL_STATUS = {
    "completed",
    "failed",
    "incomplete",
    "rejected",
    "signed_off",
}
PEER_STALL_SECONDS = 180
PEER_RUNNING_STATUS = {
    "starting",
    "active",
    "producing_evidence",
}
WANT_FILE = "WANT.md"
ROOT_SHARED_FILES = (
    "LOGS.md",
    "OOPS.md",
)
WORK_STATE_KEYS = (
    WORK_INITIALIZED_ENV,
    "GOTTA_WORK_REPO",
    "GOTTA_WORK_CREATED",
    GOTTA_WORK_ACTORS_JSON,
    "GOTTA_WORK_WANT_PATH",
    "GOTTA_WORK_AGENTS_SOURCE",
    "GOTTA_WORK_VOICE_SOURCE",
    PEER_SESSION_ACTOR_ENV,
)
PEER_SHARED_FILES = (
    "LOGS.md",
    "OOPS.md",
)
PEER_SHARED_STATE_FILES = (
    "logs.jsonl",
    "oops.jsonl",
)
FINAL_SIGNOFF_MARKER = "actors-final-signoff"


def _current_work_session_dir(
    explicit_session: str | None,
    *,
    include_context_session: bool = True,
) -> Path | None:
    session_raw = (
        explicit_session
        or os.environ.get(SESSION_ENV, "").strip()
        or discover_state_env(include_context_session=include_context_session).get(
            SESSION_ENV, ""
        ).strip()
    )
    if not session_raw:
        return None
    resolved = resolve_session_reference(session_raw, allow_missing=False)
    if resolved is None and explicit_session:
        raise SystemExit(
            "relative session references require an active or discoverable "
            "session root. Use `gotta ...` to bind one first or pass an "
            "absolute `--session` path."
        )
    return resolved


def _work_workspace_dir(*, explicit_session: str | None) -> Path:
    current = _current_work_session_dir(explicit_session)
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
    if not work_is_initialized(current):
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
    work_dir = _work_workspace_dir(explicit_session=getattr(args, "session", None))
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
WORK_ACTOR_ALIASES = {
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


def _work_with_examples(*, prefix: str = "gotta peer with") -> str:
    return " or ".join(f"`{prefix} {actor.label} ...`" for actor in DEFAULT_ACTORS)


def _peer_session_ref(peer_name: str) -> str:
    return f"peers/{_normalize_peer_name(peer_name)}"


def _peer_charter_command(peer_name: str, surface: str, *, mode: str = "--stdin") -> str:
    return f"gotta {surface} --session {_peer_session_ref(peer_name)} {mode}"


def _resolve_actor_name(value: str, *, kind: str = "actor") -> str:
    normalized = value.strip().lower()
    if normalized in WORK_ACTOR_ALIASES:
        return WORK_ACTOR_ALIASES[normalized]
    if kind == "peer":
        raise SystemExit(
            f"unknown peer: {value}. expected one of {', '.join(ACTOR_IDS)}"
        )
    raise SystemExit(f"unknown actor: {value}")


def _normalize_peer_name(value: str) -> str:
    return _shared_normalize_peer_name(_resolve_actor_name(value, kind="peer"))


def _peer_label(peer_name: str) -> str:
    normalized = _normalize_peer_name(peer_name)
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
    raw = str(state.get(GOTTA_WORK_ACTORS_JSON) or "").strip()
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


def _peer_dir_path(work_dir: Path, peer_name: str) -> Path:
    return peer_link_path(work_dir, _normalize_peer_name(peer_name))


def _peer_state_link_root(work_dir: Path) -> Path:
    return state_dir_path(work_dir) / "peers"


def _peer_state_path(work_dir: Path, peer_name: str) -> Path:
    return _peer_session_dir(work_dir, peer_name) / "state" / "peer.json"


def _peer_events_path(work_dir: Path, peer_name: str) -> Path:
    return _peer_session_dir(work_dir, peer_name) / "state" / "peer.jsonl"


def _peer_want_path(work_dir: Path, peer_name: str) -> Path:
    return _peer_session_dir(work_dir, peer_name) / WANT_FILE


def _peer_goal_path(work_dir: Path, peer_name: str) -> Path:
    return _peer_session_dir(work_dir, peer_name) / "GOAL.md"


def _canonical_work_root(session_dir: Path) -> Path:
    return session_dir.expanduser().resolve()


def _peer_session_dir(work_dir: Path, peer_name: str) -> Path:
    return peer_session_root(work_dir.resolve(), _normalize_peer_name(peer_name))


def _peer_is_selected(work_dir: Path, peer_name: str) -> bool:
    normalized = _normalize_peer_name(peer_name)
    resolved = work_dir.resolve()
    if session_actor(resolved) == normalized:
        return True
    link = peer_link_path(resolved, normalized)
    state_link = peer_state_link_dir(resolved, normalized)
    return (
        link.exists()
        or link.is_symlink()
        or state_link.exists()
        or state_link.is_symlink()
    )


def _read_peer_state(work_dir: Path, peer_name: str) -> dict[str, object]:
    normalized = _normalize_peer_name(peer_name)
    if not _peer_is_selected(work_dir, normalized):
        return {
            "peer": normalized,
            "label": _peer_label(normalized),
            "status": "pending",
        }
    path = _peer_state_path(work_dir, normalized)
    if not path.exists():
        return {
            "peer": normalized,
            "label": _peer_label(normalized),
            "status": "pending",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid peer state file: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"invalid peer state file: {path}")
    data.setdefault("peer", normalized)
    data.setdefault("label", _peer_label(normalized))
    data.setdefault("status", "pending")
    return data


def _write_peer_state(
    work_dir: Path,
    peer_name: str,
    payload: dict[str, object],
) -> Path:
    normalized = _normalize_peer_name(peer_name)
    state_dir = _peer_state_link_root(work_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _peer_state_path(work_dir, normalized)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = _read_peer_state(work_dir, normalized)
    for key, value in payload.items():
        if value is None:
            merged.pop(key, None)
            continue
        merged[key] = value
    merged["peer"] = normalized
    merged["label"] = _peer_label(normalized)
    merged["updated_at"] = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = str(merged.get("status") or "pending")
    if status not in PEER_STATE_STATUS:
        raise SystemExit(f"invalid peer status: {status}")
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


def _bootstrap_peer_want(*, label: str) -> str:
    peer_name = _normalize_peer_name(label)
    return _finish(
        f"""
        # Peer Want Placeholder

        > Generated by `gotta peer with {label}`.
        > Rewrite this file before peer launch.

        This is the operator-authored peer-local intent frame for {label}.

        It is a live charter, not an append surface and not a hidden template.
        Rewrite it intentionally so the peer starts from a real contract instead of
        trying to infer one from the shared session.

        Native rewrite:

        - `{_peer_charter_command(peer_name, 'want')}`
        - `{_peer_charter_command(peer_name, 'want', mode='--from-file <path>')}`
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
        context before launching peers or treating it as current truth.

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
        - Keep `GOAL.md` durable for peer launches and reruns.
        - Launch peers once `WANT.md` and `GOAL.md` are real.
        """
    )


def _bootstrap_peer_goal(*, label: str, peer_dir: Path, work_dir: Path) -> str:
    peer_name = _normalize_peer_name(label)
    return _finish(
        f"""
        # Seed Peer Goal Placeholder

        > Generated by `gotta peer with {label}`.
        > Rewrite this file before peer launch.

        This file turns the operator-authored peer `WANT.md` into a concrete
        evidence-collection contract for {label}.

        Bootstrap facts:

        - Peer session root: `{peer_dir}`
        - Shared live logs: `{work_dir / 'LOGS.md'}`, `{work_dir / 'OOPS.md'}`
        - Peer-local checklist: `{peer_dir / 'TODO.md'}`
        - Peer-local notes: `{peer_dir / 'NOTES.md'}`

        Rewrite rule:

        - Read peer-local `WANT.md` first.
        - Turn that charter into concrete evidence-collection steps.
        - Treat `TODO.md` as the live peer-local checklist.
        - Append durable notes during the run; do not wait until the end.
        - Do not author the final dossier from this workspace.

        Native rewrite:

        - `{_peer_charter_command(peer_name, 'goal')}`
        - `{_peer_charter_command(peer_name, 'goal', mode='--from-file <path>')}`
        """
    )


def _bootstrap_oops() -> str:
    return render_oops_markdown([])


def _seed_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _peer_readme(label: str) -> str:
    peer_name = _normalize_peer_name(label)
    return dedent(
        f"""\
        # {label} Workspace

        > Generated by `gotta peer with {label}`.
        > This file is a bootstrap guide, not a live append surface.

        This directory is a first-class peer session root for {label.lower()} inside one
        linked gotta investigation.

        Readable peer surfaces:

        - `WANT.md`: peer-local intent frame seeded here, then rewritten before launch
        - `GOAL.md`: peer-local goal seeded here, then rewritten before launch
        - `TODO.md`: peer-local checklist projected from `state/todo.jsonl`
        - `NOTES.md`: projected running notes plus live peer state
        - `LOGS.md` / `OOPS.md`: shared cross-actor continuous logs

        Canonical truth:

        - this session root is real; work from here with `gotta ...`
        - this peer session is top-level under the canonical gotta session home and may be linked into other sessions
        - peer-local checklist truth is `state/todo.jsonl`
        - peer lifecycle truth is `state/peer.json` and `state/peer.jsonl`
        - peer notes truth is `state/notes.jsonl`
        - shared cross-actor truth is `state/logs.jsonl`, `state/oops.jsonl`, and the shared evidence web
        - shared evidence and cross-session readable surfaces may be linked into this session
        - provenance still belongs to the peer that performed the work
        - peer-local `WANT.md` and `GOAL.md` are live operator-authored charters, not hidden templates
        - rewrite peer-local charters with `{_peer_charter_command(peer_name, 'want')}` and `{_peer_charter_command(peer_name, 'goal')}` before launch
        - prefer native `gotta` surfaces over shell-side spelunking
        - prefer `gotta notes ...` for peer-note mutation
        - do not author the final dossier from this workspace; stop at evidence and handoff notes
        """
    )


def _seed_peer_surface(
    peer_dir: Path,
    label: str,
    *,
    work_dir: Path,
    ensure_symlink: Callable[[Path, Path], None],
) -> None:
    peer_dir.mkdir(parents=True, exist_ok=True)
    _seed_file(peer_dir / "README.md", _peer_readme(label))
    _seed_file(peer_dir / WANT_FILE, _bootstrap_peer_want(label=label))
    _seed_file(
        peer_dir / "GOAL.md",
        _bootstrap_peer_goal(
            label=label,
            peer_dir=peer_dir,
            work_dir=work_dir,
        ),
    )
    for name in ROOT_SHARED_FILES:
        ensure_symlink(peer_dir / name, work_dir / name)


def _reset_orphaned_peer_surface(peer_dir: Path) -> None:
    managed_paths = (
        peer_dir / "README.md",
        peer_dir / WANT_FILE,
        peer_dir / "GOAL.md",
        peer_dir / "TODO.md",
        peer_dir / "NOTES.md",
        peer_dir / "LOGS.md",
        peer_dir / "OOPS.md",
        peer_dir / "content",
        peer_dir / "peers",
        peer_dir / "state",
    )
    for path in managed_paths:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.exists():
            shutil.rmtree(path)


def _peer_script(*, work_dir: Path, peer_dir: Path, peer_name: str) -> str:
    peer_name = _normalize_peer_name(peer_name)
    peer_session = _peer_session_dir(work_dir, peer_name)
    return dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail

        ws={sh_quote(str(peer_session))}
        if [[ "${{1:-}}" == "--help" || "${{1:-}}" == "-h" ]]; then
          exec gotta peer launch {peer_name} --session "$ws" --help
        fi
        exec gotta peer launch {peer_name} --session "$ws" "$@"
        """
    )


def _load_work_state(work_dir: Path) -> dict[str, str]:
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


def _peer_todo_marker(peer_name: str, phase: str) -> str:
    return f"peer-{_normalize_peer_name(peer_name)}-{phase}"


def _peer_todo_redirect(peer_name: str, phase: str) -> str:
    actor = _normalize_peer_name(peer_name)
    label = _peer_label(actor)
    if phase == "initial":
        return (
            f"that TODO item is owned by {label} lifecycle; inspect with "
            f"`gotta peer status {actor}` and advance it through "
            f"`gotta peer complete {actor}` or `gotta peer signoff {actor} ...`"
        )
    if phase == "complete":
        return (
            f"that TODO item is owned by {label} lifecycle; use "
            f"`gotta peer complete {actor}` once the peer run has materially landed"
        )
    if phase == "dispositioned":
        return (
            f"that TODO item is owned by {label} disposition; use "
            f"`gotta peer signoff {actor} --summary ...` after review"
        )
    raise ValueError(f"unknown peer TODO phase: {phase}")


def _managed_todo_redirect(managed_key: str) -> str:
    if managed_key == FINAL_SIGNOFF_MARKER:
        return (
            "that TODO item is owned by final peer sign-off; inspect all peers with "
            "`gotta peer status` and sign off each peer through "
            "`gotta peer signoff ...`"
        )
    prefix = "peer-"
    suffixes = ("-initial", "-complete", "-dispositioned")
    if managed_key.startswith(prefix):
        for suffix in suffixes:
            if managed_key.endswith(suffix):
                actor = managed_key[len(prefix) : -len(suffix)]
                phase = suffix.removeprefix("-")
                return _peer_todo_redirect(actor, phase)
    return (
        "that TODO item is managed by native peer state; use "
        "`gotta peer ...` to advance it instead of "
        "`gotta todo check`"
    )


def _selected_actor_ids(work_dir: Path) -> tuple[str, ...]:
    registry = _actor_registry_from_state(_load_work_state(work_dir))
    selected: list[str] = []
    for actor_id in registry:
        if _peer_is_selected(work_dir, actor_id):
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
    ordered_keys = [SESSION_ENV, CONTENT_ENV, SESSION_REPO_ENV, *WORK_STATE_KEYS]
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
            text=f"{WANT_FILE} rewritten by the active agent from live context",
        )
        create_todo_item(
            session_dir,
            section="Status",
            text="GOAL.md rewritten by the active agent from live context",
        )
        create_todo_item(
            session_dir,
            section="Status",
            text="TODO.md expanded by the active agent into a real working checklist",
        )
        create_todo_item(
            session_dir,
            section="Status",
            text="Decide whether to configure a peer with `gotta peer with ...` and actually consult them if it helps.",
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
                f"  - {str(payload.get('label') or _peer_label(actor_id))}: model = `{payload.get('model', '')}`, resume = `{payload.get('resume_uuid', '')}`"
            )
        if agents_src:
            bootstrap_lines.append("Imported `AGENTS.md`")
        if voice_src:
            bootstrap_lines.append("Imported `VOICE.md`")
        bootstrap_lines.append("Seeded as the durable execution log for this session")
        append_log_record(session_dir, message="\n".join(bootstrap_lines))
        sync_logs_projection(session_dir)


def scaffold_session(session_dir: Path, *, repo: Path | None = None) -> None:
    current = _load_work_state(session_dir)
    existing_repo = str(
        current.get("GOTTA_WORK_REPO") or current.get(SESSION_REPO_ENV) or ""
    ).strip()
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
    created_at = current.get("GOTTA_WORK_CREATED") or datetime.now(tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
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
            WORK_INITIALIZED_ENV: "1",
            "GOTTA_WORK_REPO": str(repo_path or ""),
            "GOTTA_WORK_CREATED": created_at,
            GOTTA_WORK_ACTORS_JSON: _actor_registry_json(actors),
            "GOTTA_WORK_WANT_PATH": str(session_dir / WANT_FILE),
            "GOTTA_WORK_AGENTS_SOURCE": str(agents_src or ""),
            "GOTTA_WORK_VOICE_SOURCE": str(voice_src or ""),
        },
    )
    _seed_session_files(
        session_dir=session_dir,
        repo=repo_path,
        actors=actors,
        agents_src=agents_src,
        voice_src=voice_src,
    )
    _record_work_activity(
        session_dir,
        plugin="session",
        surface="session.init",
        action="init",
        target=session_dir / WANT_FILE,
        detail="scaffolded WANT.md, GOAL.md, TODO.md, LOGS.md, and OOPS.md",
    )


def _ensure_peer_session_exports(
    peer_dir: Path,
    *,
    content_dir: Path,
    session_root: Path,
) -> dict[str, str]:
    dirs = resolve_dirs(
        CommonOptions(session_dir=str(peer_dir), content_dir=str(content_dir)),
        create=True,
    )
    write_session_state(dirs)
    dirs.session_dir.joinpath("bin").mkdir(parents=True, exist_ok=True)
    _ensure_symlink(peer_dir / "content", content_dir)
    for name in PEER_SHARED_FILES:
        target = session_root / name
        _ensure_symlink(peer_dir / name, target)
    _ensure_symlink(peer_dir / "peers", session_root / "peers")
    peer_state_dir = peer_dir / "state"
    for name in PEER_SHARED_STATE_FILES:
        target = session_root / "state" / name
        _ensure_symlink(peer_state_dir / name, target)
    _ensure_symlink(peer_state_dir / "peers", session_root / "state" / "peers")
    return env_mapping(dirs)


def _ensure_peer_parent_links(session_root: Path, peer_name: str, peer_dir: Path) -> None:
    _ensure_symlink(peer_link_path(session_root, peer_name), peer_dir)
    _ensure_symlink(peer_state_link_dir(session_root, peer_name), peer_dir / "state")


def _ensure_peer_initial_todo(peer_dir: Path) -> None:
    if todo_items(peer_dir):
        return
    create_todo_item(
        peer_dir,
        section="Status",
        text="Rewrite WANT.md and GOAL.md into a concrete peer-local checklist in TODO.md.",
    )
    create_todo_item(
        peer_dir,
        section="Status",
        text="Materialize the first strong source anchor and append a first durable note as soon as it lands.",
    )
    create_todo_item(
        peer_dir,
        section="Status",
        text="Append another durable note after each material evidence wave or plan change; do not request completion or sign-off with empty NOTES.md.",
    )


def _work_surface_path(work_dir: Path, surface: str) -> Path:
    return work_dir / surface


def _record_work_activity(
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
        todo_surface = _work_surface_path(work_dir, "TODO.md").resolve()
        logs_surface = _work_surface_path(work_dir, "LOGS.md").resolve()
        oops_surface = _work_surface_path(work_dir, "OOPS.md").resolve()
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


def _append_peer_event(
    work_dir: Path,
    peer_name: str,
    *,
    event: str,
    detail: str = "",
    extra: dict[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {
        "timestamp": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "peer": _normalize_peer_name(peer_name),
        "event": event,
        "detail": detail,
    }
    if extra:
        payload.update(extra)
    _append_jsonl(_peer_events_path(work_dir, peer_name), payload)
    if event != "heartbeat":
        _record_work_activity(
            work_dir,
            plugin="peer",
            surface="peer.lifecycle",
            action=event,
            locator=f"peer:{payload['peer']}",
            preferred_name=str(payload["peer"]),
            follow_command=f"gotta peer status {payload['peer']}",
            detail=detail or event,
        )


def _peer_log_line(session_root: Path, peer_name: str, message: str) -> None:
    append_log_record(session_root, message=f"[{peer_name}] {message}", actor=peer_name)


def _record_peer_projection_activity(
    session_root: Path,
    *,
    peer_name: str,
    surface: str,
    action: str,
    log_path: Path,
    projection_path: Path,
    detail: str,
) -> None:
    _record_work_activity(
        session_root,
        plugin="peer",
        surface=surface,
        action=action,
        locator=_session_relative_locator(session_root, log_path),
        preferred_name=projection_path.name,
        follow_command=f"gotta read {_session_relative_locator(session_root, projection_path)!r}",
        detail=detail,
    )


def _want_rewrite_pending(work_dir: Path) -> bool:
    want_path = _work_surface_path(work_dir, WANT_FILE)
    if not want_path.is_file():
        return True
    current = want_path.read_text(encoding="utf-8").strip()
    return current == _bootstrap_want().strip()


def _goal_rewrite_pending(goal_path: Path) -> bool:
    if not goal_path.is_file():
        return True
    return goal_path.read_text(encoding="utf-8").strip().startswith("# Seed Goal Placeholder")


def _peer_want_rewrite_pending(work_root: Path, peer_name: str) -> bool:
    path = _peer_want_path(work_root, peer_name)
    if not path.is_file():
        return True
    current = path.read_text(encoding="utf-8").strip()
    return current == _bootstrap_peer_want(label=_peer_label(peer_name)).strip()


def _peer_goal_rewrite_pending(work_root: Path, peer_name: str) -> bool:
    path = _peer_goal_path(work_root, peer_name)
    if not path.is_file():
        return True
    return path.read_text(encoding="utf-8").strip() == _bootstrap_peer_goal(
        label=_peer_label(peer_name),
        peer_dir=_peer_session_dir(work_root, peer_name),
        work_dir=work_root,
    ).strip()


def _peer_launch_blockers(work_root: Path, *, peer_name: str = "") -> list[str]:
    blockers: list[str] = []
    goal_path = _work_surface_path(work_root, "GOAL.md")
    if _want_rewrite_pending(work_root):
        blockers.append(f"rewrite `{_work_surface_path(work_root, WANT_FILE)}` first")
    if _goal_rewrite_pending(goal_path):
        blockers.append(f"rewrite `{goal_path}` from the current moment before launch")
    if peer_name:
        peer_want = _peer_dir_path(work_root, peer_name) / WANT_FILE
        peer_goal = _peer_dir_path(work_root, peer_name) / "GOAL.md"
        want_cmd = _peer_charter_command(peer_name, "want")
        goal_cmd = _peer_charter_command(peer_name, "goal")
        if _peer_want_rewrite_pending(work_root, peer_name):
            blockers.append(
                f"rewrite `{peer_want}` as the peer-local intent frame with `{want_cmd}` before launch"
            )
        if _peer_goal_rewrite_pending(work_root, peer_name):
            blockers.append(
                f"rewrite `{peer_goal}` as the peer-local goal with `{goal_cmd}` before launch"
            )
    return blockers


def _peer_evidence_summary(work_dir: Path, peer_name: str) -> dict[str, object]:
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
            if isinstance(payload, dict) and str(payload.get("actor") or "primary") == _normalize_peer_name(peer_name):
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


def _peer_evidence_note(evidence: dict[str, object]) -> str:
    artifact_count = int(evidence.get("artifact_count") or 0)
    if artifact_count <= 0:
        return ""
    noun = "artifact" if artifact_count == 1 else "artifacts"
    return (
        f"{artifact_count} actor-attributed {noun} already live in session manifest, "
        "timeline, leads, and graph."
    )


def _peer_activity_summary(event: str, detail: str) -> str:
    cleaned_detail = detail.strip()
    if event == "note":
        return cleaned_detail or "note"
    label = event.replace("_", " ")
    if cleaned_detail:
        return f"{label}: {cleaned_detail}"
    return label


def _peer_recent_activity(work_dir: Path, peer_name: str, *, limit: int = 5) -> dict[str, object]:
    path = _peer_events_path(work_dir, peer_name)
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
                "summary": _peer_activity_summary(event, detail),
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


def _peer_status_payload(work_dir: Path, peer_name: str) -> dict[str, object]:
    state = _read_peer_state(work_dir, peer_name)
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
            if age > PEER_STALL_SECONDS:
                derived_status = "stalled"
                heartbeat_stale = True
    elif status in {"starting", "active"} and not heartbeat_at:
        started_at = str(state.get("started_at") or "")
        try:
            started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            started_dt = None
        if started_dt is not None and (time.time() - started_dt.timestamp()) > PEER_STALL_SECONDS:
            derived_status = "stalled"
            heartbeat_stale = True
    signoff_at = str(state.get("signoff_at") or "")
    peer_dir = _peer_session_dir(work_dir, peer_name)
    notes_path = peer_dir / "NOTES.md"
    notes_ready = peer_notes_ready(work_dir, _normalize_peer_name(peer_name))
    evidence = _peer_evidence_summary(work_dir, peer_name)
    evidence_note = _peer_evidence_note(evidence)
    recent_activity = _peer_recent_activity(work_dir, peer_name)
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
            " Peer runtime is no longer live, so this is awaiting an explicit "
            "completion, failure, or sign-off record."
        )
    if derived_status in {"starting", "active"} and evidence_live:
        derived_status = "producing_evidence"
    still_running = derived_status in PEER_RUNNING_STATUS and not heartbeat_stale
    request_note = ""
    if requested_status and derived_status not in PEER_TERMINAL_STATUS:
        if requested_label == "stop":
            if derived_status == "stalled":
                request_note = (
                    " Operator already requested a graceful stop"
                    + (f" ({requested_summary})." if requested_summary else ".")
                    + " Because the heartbeat is stale, you can settle now with "
                    f"`gotta peer settle {_normalize_peer_name(peer_name)}`."
                )
            else:
                request_note = (
                    " Operator already requested a graceful stop"
                    + (f" ({requested_summary})." if requested_summary else ".")
                    + " The peer should wind down, append one final durable note, and sign off."
                )
        elif derived_status == "stalled":
            request_note = (
                f" Operator already requested `{requested_label}`"
                + (f" ({requested_summary})." if requested_summary else ".")
                + " Because the heartbeat is stale, you can settle now with "
                f"`gotta peer settle {_normalize_peer_name(peer_name)}`."
            )
        else:
            request_note = (
                f" Operator already requested `{requested_label}`"
                + (f" ({requested_summary})." if requested_summary else ".")
                + " That pending disposition will become authoritative automatically "
                "when the peer runtime exits."
            )
    if derived_status == "producing_evidence":
        if notes_ready:
            next_step = (
                "peer is still active and producing evidence artifacts. "
                + (evidence_note + " " if evidence_note else "")
                + "Use NOTES.md for live peer visibility; recheck `gotta peer status "
                f"{_normalize_peer_name(peer_name)}` shortly before closing the peer out."
                + request_note
                + runtime_note
            )
        else:
            next_step = (
                "peer is still active and producing evidence artifacts, but NOTES.md is still empty. "
                + (evidence_note + " " if evidence_note else "")
                + "Append a durable note before requesting completion or sign-off, then recheck "
                f"`gotta peer status {_normalize_peer_name(peer_name)}` shortly."
                + request_note
                + runtime_note
            )
    elif derived_status in {"starting", "active"} and notes_ready:
        next_step = (
            "peer is still active and has already started landing durable notes. "
            "Use NOTES.md for live peer visibility; recheck `gotta peer status "
            f"{_normalize_peer_name(peer_name)}` shortly before closing the peer out."
            + request_note
            + runtime_note
        )
    elif derived_status == "awaiting_disposition":
        next_step = (
            "peer runtime is no longer running, but no durable terminal lifecycle was recorded yet. "
            + (evidence_note + " " if evidence_note else "")
            + "Inspect NOTES.md plus the shared evidence web, then settle with "
            f"`gotta peer settle {_normalize_peer_name(peer_name)}`"
            + (
                f" to honor the pending `{requested_label}` request."
                if requested_status
                else " to record the authoritative terminal disposition."
            )
            + request_note
        )
    elif derived_status == "stalled" and (notes_ready or evidence_live):
        next_step = (
            "peer heartbeat is stale, but material peer work already exists in NOTES.md or the "
            "shared evidence web. "
            + (evidence_note + " " if evidence_note else "")
            + "Inspect the notes and decide whether to wait, relaunch, or disposition manually."
            + request_note
        )
    elif derived_status == "completed" and (notes_ready or evidence_live):
        if notes_ready:
            next_step = (
                "peer run is complete; inspect NOTES.md plus the shared evidence web, then record "
                "durable sign-off with "
                f"`gotta peer signoff {_normalize_peer_name(peer_name)} --summary ...`."
            )
        else:
            next_step = (
                "peer run is complete and evidence landed, but NOTES.md is still empty. Wait for a "
                "durable note or sign off intentionally only if you are explicitly accepting an "
                "evidence-only peer contribution."
            )
    elif derived_status == "incomplete":
        next_step = (
            "peer finished without material notes or evidence. Decide whether to relaunch, "
            "fail, or sign off intentionally."
        )
    elif derived_status == "failed" and evidence_note:
        next_step = (
            "peer was manually marked failed, but evidence already landed in shared state. "
            + evidence_note
            + " Keep or reject that evidence intentionally instead of assuming it vanished."
        )
    else:
        next_step = ""
    return {
        **state,
        "status": derived_status,
        "state_path": str(_peer_state_path(work_dir, peer_name)),
        "events_path": str(_peer_events_path(work_dir, peer_name)),
        "peer_dir": str(peer_dir),
        "notes_path": str(notes_path),
        "notes_status": notes_status,
        "notes_ready": notes_ready,
        "evidence_live": bool(evidence_live),
        "evidence_note": evidence_note,
        "requested_status": requested_status,
        "requested_summary": requested_summary,
        "requested_label": requested_label,
        "requested_pending": bool(
            requested_status and derived_status not in PEER_TERMINAL_STATUS
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


def _ensure_peer_todo_items(work_dir: Path, peer_name: str) -> None:
    actor = _normalize_peer_name(peer_name)
    label = _peer_label(actor)
    ensure_managed_todo_item(
        work_dir,
        section="Peer Checklist",
        text=f"Initial peer pass collected from {label}",
        managed_key=_peer_todo_marker(actor, "initial"),
    )
    ensure_managed_todo_item(
        work_dir,
        section="Peer Checklist",
        text=f"{label} run materially complete",
        managed_key=_peer_todo_marker(actor, "complete"),
    )
    ensure_managed_todo_item(
        work_dir,
        section="Peer Checklist",
        text=f"{label} findings dispositioned",
        managed_key=_peer_todo_marker(actor, "dispositioned"),
    )
    ensure_managed_todo_item(
        work_dir,
        section="Peer Checklist",
        text="Final peer sign-off collected after edits for the chosen team",
        managed_key=FINAL_SIGNOFF_MARKER,
    )


def _sync_peer_todo_state(work_dir: Path) -> None:
    actor_ids = _selected_actor_ids(work_dir)
    peer_payloads = {peer: _peer_status_payload(work_dir, peer) for peer in actor_ids}
    launched_actor_ids = [
        peer
        for peer in actor_ids
        if str(peer_payloads[peer].get("status") or "pending") not in {"pending", "configured"}
    ]
    for peer_name in launched_actor_ids:
        _ensure_peer_todo_items(work_dir, peer_name)
    items_by_key = {
        str(item.get("managed_key") or ""): item for item in todo_items(work_dir) if item.get("managed_key")
    }
    for peer_name in launched_actor_ids:
        payload = peer_payloads[peer_name]
        materially_complete = bool(payload.get("notes_ready") or payload.get("evidence_live"))
        terminal = str(payload.get("status") or "") in {"completed", "failed", "rejected", "signed_off", "incomplete"}
        signed_off = str(payload.get("status") or "") == "signed_off"
        for marker, checked in (
            (_peer_todo_marker(peer_name, "initial"), materially_complete),
            (_peer_todo_marker(peer_name, "complete"), terminal),
            (_peer_todo_marker(peer_name, "dispositioned"), signed_off),
        ):
            item = items_by_key.get(marker)
            if item is None:
                continue
            updated = set_todo_checked(work_dir, str(item["id"]), checked=checked)
            if updated is not None:
                items_by_key[marker] = updated
    final_item = items_by_key.get(FINAL_SIGNOFF_MARKER)
    final_checked = bool(launched_actor_ids) and all(
        str(peer_payloads[peer].get("status") or "") == "signed_off"
        for peer in launched_actor_ids
    )
    if final_item is not None:
        set_todo_checked(work_dir, str(final_item["id"]), checked=final_checked)


def _sync_peer_projection_surfaces(work_dir: Path, peer_name: str) -> None:
    peer = _normalize_peer_name(peer_name)
    sync_peer_notes_projection(
        work_dir,
        peer,
        label=_peer_label(peer),
        status_payload=_peer_status_payload(work_dir, peer),
    )


def _peer_launch_command(work_dir: Path, peer_name: str) -> str:
    return f"gotta peer launch {peer_name} --session {sh_quote(str(work_dir))}"


def _ensure_peer_surface(work_dir: Path, peer_name: str) -> Path:
    peer_name = _normalize_peer_name(peer_name)
    state = _load_work_state(work_dir)
    actors = _actor_registry_from_state(state)
    actor = actors.get(peer_name)
    if actor is None:
        raise SystemExit(f"unknown peer: {peer_name}")
    repo_raw = str(state.get("GOTTA_WORK_REPO") or "").strip()
    peer_dir = peer_session_root(work_dir, peer_name)
    bin_path = work_dir / "bin" / peer_name
    if peer_dir != work_dir:
        if peer_dir.exists() and not _peer_is_selected(work_dir, peer_name):
            _reset_orphaned_peer_surface(peer_dir)
        _seed_peer_surface(
            peer_dir,
            _peer_label(peer_name),
            work_dir=work_dir,
            ensure_symlink=_ensure_symlink,
        )
        _copy_if_present(work_dir / "AGENTS.md", peer_dir / "AGENTS.md")
        _copy_if_present(work_dir / "VOICE.md", peer_dir / "VOICE.md")
        _ensure_peer_session_exports(
            peer_dir,
            content_dir=work_dir / "content",
            session_root=work_dir,
        )
        _ensure_peer_parent_links(work_dir, peer_name, peer_dir)
    else:
        _seed_file(peer_dir / "README.md", _peer_readme(_peer_label(peer_name)))
        _ensure_state_exports(peer_dir)
    _write_state_file(
        peer_dir,
        {
            WORK_INITIALIZED_ENV: "1",
            "GOTTA_WORK_REPO": repo_raw,
            "GOTTA_WORK_CREATED": str(state.get("GOTTA_WORK_CREATED") or ""),
            GOTTA_WORK_ACTORS_JSON: _actor_registry_json(actors),
            "GOTTA_WORK_WANT_PATH": str(peer_dir / WANT_FILE),
            "GOTTA_WORK_AGENTS_SOURCE": str(state.get("GOTTA_WORK_AGENTS_SOURCE") or ""),
            "GOTTA_WORK_VOICE_SOURCE": str(state.get("GOTTA_WORK_VOICE_SOURCE") or ""),
            PEER_SESSION_ACTOR_ENV: peer_name,
        },
    )
    if not todo_state_path(peer_dir).exists():
        todo_state_path(peer_dir).parent.mkdir(parents=True, exist_ok=True)
        todo_state_path(peer_dir).touch()
        sync_todo_projection(peer_dir)
    _ensure_peer_initial_todo(peer_dir)
    _write_peer_state(
        work_dir,
        peer_name,
        {
            "status": str(_read_peer_state(work_dir, peer_name).get("status") or "pending"),
            "notes_path": str(peer_notes_surface_path(work_dir, peer_name)),
        },
    )
    _sync_peer_projection_surfaces(work_dir, peer_name)
    if peer_dir != work_dir:
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        bin_path.write_text(
            _peer_script(
                work_dir=work_dir,
                peer_dir=peer_dir,
                peer_name=peer_name,
            ),
            encoding="utf-8",
        )
        bin_path.chmod(0o755)
    return peer_dir


def _configure_peer(session_root: Path, peer_name: str) -> str:
    peer = _normalize_peer_name(peer_name)
    bin_path = session_root / "bin" / peer
    already_configured = _peer_is_selected(session_root, peer) and bin_path.exists()
    _ensure_peer_surface(session_root, peer)
    current_status = str(_read_peer_state(session_root, peer).get("status") or "pending")
    if current_status in {"", "pending"}:
        _write_peer_state(session_root, peer, {"status": "configured"})
        _sync_peer_projection_surfaces(session_root, peer)
    launch_cmd = _peer_launch_command(session_root, peer)
    peer_want = _peer_dir_path(session_root, peer) / WANT_FILE
    peer_goal = _peer_dir_path(session_root, peer) / "GOAL.md"
    peer_todo = _peer_dir_path(session_root, peer) / "TODO.md"
    want_cmd = _peer_charter_command(peer, "want")
    goal_cmd = _peer_charter_command(peer, "goal")
    peer_blockers = _peer_launch_blockers(session_root, peer_name=peer)
    if peer_blockers:
        suffix = (
            f"; not launched. Rewrite `{peer_want}` and `{peer_goal}` for {_peer_label(peer)} with `{want_cmd}` and `{goal_cmd}` first. "
            f"`{peer_todo}` is already seeded with a minimal peer-local checklist and you may extend it before launch if useful, "
            f"then launch with `{launch_cmd}` when you actually want {_peer_label(peer)} to start"
        )
    else:
        suffix = (
            f"; not launched. `{peer_want}` and `{peer_goal}` are already real. "
            f"`{peer_todo}` is already seeded with a minimal peer-local checklist and you may extend it before launch if useful, "
            f"then launch with `{launch_cmd}` when you actually want {_peer_label(peer)} to start"
        )
    if already_configured:
        return f"{_peer_label(peer)} already configured{suffix}"
    _append_peer_event(session_root, peer, event="configured", detail="configured peer workspace")
    _peer_log_line(session_root, peer, "configured workspace")
    return (
        f"configured {_peer_label(peer)} workspace, linked peer session, seeded peer-local WANT/GOAL placeholders, seeded peer-local TODO, launch shim, linked shared logs, and evidence access only"
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
