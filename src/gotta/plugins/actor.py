"""Top-level linked-actor control surface."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from textwrap import dedent
from typing import TextIO

from gotta.compat import UTC, datetime
from gotta.actors import ACTOR_SPEAKER_ENV, resolve_actor_context
from gotta.content.env import SESSION_REPO_ENV, load_state_env_at_root
from gotta.content.scope import session_is_initialized
from gotta.helptext import format_long_help, is_long_help_request
from gotta.friction import append_oops_record
from gotta.notes import (
    actor_notes_log_path,
    append_actor_note,
)
from gotta.actor import (
    ACTOR_ID_ENV,
    ACTOR_LABEL_ENV,
    SUPERVISOR_GRACEFUL_STOP_MODE,
    SUPERVISOR_GRACEFUL_STOP_STATUS,
    actor_session_root,
    require_writer,
    writer_name,
)
from gotta import topology
from gotta.session.activity.note import _reset_note_check_feedback
from gotta.session.activity.record import (
    _actor_log_line,
    _append_actor_event,
    _record_actor_surface_activity,
)
from gotta.session import bootstrap as session_bootstrap
from gotta.session import charter as session_charter
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope
from gotta.session.registry import (
    _normalize_actor_name,
    _actor_goal_path,
    _actor_label,
    _actor_want_path,
    _read_actor_state,
    _write_actor_state,
)
from gotta.session.status.bind import _bind_actor
from gotta.session.status.blocker import _actor_launch_blockers
from gotta.session.status.payload import _actor_status_payload
from gotta.session.status.todo import _sync_actor_todo_state


ACTOR_HEARTBEAT_SECONDS = 30
LIVE_STATUSES = {"starting", "active", "closing", "producing_evidence", "stalled"}
TERMINAL_STATUSES = {"completed", "failed", "incomplete", "rejected", "signed_off"}
FEEDBACK_DIRECTIVE_PREFIX = "@@gotta "
FEEDBACK_SURFACES = {"notes", "oops"}
LAUNCHER_AUTHOR = "launcher"
LAUNCHER_HEARTBEAT_NOTE = (
    "launcher pulse: actor runtime spawned successfully; wait for actor voice and do "
    "not pair another agent into this actor."
)


def _normalize_args(argv: list[str]) -> list[str]:
    if not argv:
        return ["status"]
    first = argv[0]
    actions = {
        "status",
        "bind",
        "launch",
        "heartbeat",
        "complete",
        "fail",
        "stop",
        "settle",
        "signoff",
    }
    if first in actions or first.startswith("-"):
        return argv
    return ["status", *argv]


def session_access_mode(argv: list[str]) -> str:
    normalized = _normalize_args(list(argv or []))
    positionals = session_charter.argv_positionals(
        normalized,
        valued_flags=("--session", "--actor", "--output"),
    )
    action = positionals[0] if positionals else "status"
    return "read" if action == "status" else "write"


def _actor_prompt(*, work_root: Path, actor_name: str) -> str:
    actor_name = session_registry._resolve_bound_actor_name(work_root, actor_name)
    actor_dir = actor_session_root(work_root, actor_name)
    actor_want = _actor_want_path(work_root, actor_name)
    actor_goal = _actor_goal_path(work_root, actor_name)
    notes_log = actor_notes_log_path(work_root, actor_name)
    label = _actor_label(actor_name, work_dir=work_root)
    want_text = actor_want.read_text(encoding="utf-8").strip()
    goal_text = actor_goal.read_text(encoding="utf-8").strip()
    return dedent(
        f"""\
        You are {label.lower()} inside a first-class gotta session for one shared session.

        This runtime already is the actor. Do not spawn, pair, or delegate another
        agent from inside this actor runtime. If a narrower parallel branch is needed,
        the supervisor must bind and launch a sibling actor instead.

        Use `gotta` itself as the primary investigation surface. If a native path
        is thin and you must use another tool, disclose that move through
        `gotta notes append ...` together with why the
        native path was insufficient.

        Session layout:

        - your actor session root: `{actor_dir}`
        - shared evidence store: `{actor_dir / "content"}`

        Readable actor surfaces:

        - actor-local WANT present before launch: `{actor_want}`
        - actor-local GOAL present before launch: `{actor_goal}`
        - actor-local live checklist: `gotta todo`
        - actor-local notes surface: `gotta notes`
        - actor-local procedural trace: `gotta logs`
        - actor-local friction surface: `gotta oops`

        Canonical actor mutation state:

        - actor-local todo log: `{actor_dir / "state" / "todo.jsonl"}`
        - notes log: `{notes_log}`

        Current actor want / intent frame:

        - `{actor_want}`
        {want_text or "_empty_"}

        Current actor goal frame:

        - `{actor_goal}`
        {goal_text or "_empty_"}

        Fast durable updates:

        - the shortest path to visible progress is one consumed launcher directive line:
          `@@gotta {{"actor":"{actor_name}","surface":"notes","message":"alive: runtime is up and tracing the first branch"}}`
        - use the same shape for other short durable notes:
          `@@gotta {{"actor":"{actor_name}","surface":"notes","message":"first anchor: thread root and 4 replies hydrated"}}`
          `@@gotta {{"actor":"{actor_name}","surface":"notes","message":"evidence wave: importer continuity artifacts landed"}}`
          `@@gotta {{"actor":"{actor_name}","surface":"notes","message":"signoff: gateway reuse confirmed; no further retrieval needed"}}`
          `@@gotta {{"actor":"{actor_name}","surface":"oops","message":"reply permalink lost thread context"}}`
        - valid directive surfaces are `notes` and `oops`
        - directive lines are private launcher protocol: they are consumed immediately, update the matching durable surface, and never appear in the visible transcript
        - `notes` are the canonical actor-authored narration surface; one-line notes are valid and expected
        - use `notes` for cheap durable heartbeat, first-anchor, evidence-wave, and signoff narration
        - use `oops` for native-surface friction
        - emit one immediate `@@gotta` note as soon as the runtime is alive
        - emit another `@@gotta` note after the first strong anchor, after each material evidence wave, and before sign-off if the last short note is stale
        - use native `gotta` mutation for multiline, structured, or more deliberate edits

        Evidence-first actor contract:

        - prefer `gotta` commands over side channels
        - use `gotta read`, `gotta session ...`, `gotta actor ...`, `gotta todo ...`, `gotta notes ...`, `gotta oops ...`, and `gotta logs ...` first for native inspection and mutation
        - treat `gotta logs ...` as chronology and system trace, not as your primary narration path
        - for large native outputs, try `gotta read --head`, `--tail`, or `--section` before shell slicing
        - do not start with shell traversal, `gh`, `rg`, `jq`, ad hoc Python, or other side channels; exhaust native `gotta` surfaces first
        - if no native path exists, disclose that gap in a short note instead of silently routing around it through shell traversal
        - treat actor-local WANT.md and GOAL.md as operator-authored live surfaces, not hidden templates that need to be rediscovered
        - treat actor-local WANT.md and GOAL.md plus the live `gotta todo|notes|logs|oops` surfaces and the shared evidence web as the live truth surfaces
        - disclose any non-native move as a native-coverage gap instead of hiding it
        - materialized evidence becomes usable immediately through manifest, timeline, leads, and graph even before notes catch up
        - append an initial short heartbeat note as soon as the actor runtime is alive, even before the first strong anchor
        - append a first substantive short note immediately after the first strong anchor or branch; do not wait for a full evidence wave
        - append another short note after each material evidence wave or when the plan changes
        - if you materially expanded the evidence web since your last note, append a new short note before requesting completion or sign-off
        - if the supervisor records a pending graceful stop or `failed` disposition, treat that as a stopping signal: stop new retrieval, append one final short note, and run `gotta actor signoff {actor_name} --summary "<one-line sign-off>"` promptly
        - session-rooted `gotta ...` commands will repeat that stopping warning while the supervisor stop request is still pending
        - if actor-local `gotta ...` commands warn that the supervisor has checked your notes repeatedly, treat that as live pulse feedback and answer it with one short note if real progress exists
        - do not author the final dossier, final brief, or top-level synthesis from this actor session
        - do not rewrite another linked session's local surfaces unless you intentionally mean to change shared team state
        - append running notes with `gotta notes append --stdin`; add `--actor {actor_name}` only when you are intentionally targeting this actor from another bound root
        - do not satisfy the narration contract by opening another native agent; if native `gotta` narration is blocked, record that as friction instead
        - when you are truly done, run:
          `gotta actor signoff {actor_name} --summary "<one-line sign-off>"`
        """
    )


def _actor_runtime_env(work_root: Path, actor_name: str) -> dict[str, str]:
    actor_name = session_registry._resolve_bound_actor_name(work_root, actor_name)
    actor_dir = actor_session_root(work_root, actor_name)
    env = os.environ.copy()
    env.update(load_state_env_at_root(actor_dir))
    env[ACTOR_ID_ENV] = actor_name
    env[ACTOR_LABEL_ENV] = _actor_label(actor_name, work_dir=work_root)
    env["GOTTA_ACTOR_DIR"] = str(actor_dir)
    env["GOTTA_ACTOR_NOTES_LOG_PATH"] = str(actor_notes_log_path(work_root, actor_name))
    env[ACTOR_SPEAKER_ENV] = actor_name
    repo = env.get(SESSION_REPO_ENV, "").strip()
    env.pop("GOTTA_CONTEXT_ID", None)
    env.pop("GOTTA_CONTEXT_SOURCE", None)
    env.pop("GOTTA_SESSION_ACTIVATION", None)
    if repo:
        venv_bin = Path(repo) / ".venv" / "bin"
        if venv_bin.is_dir():
            env["VIRTUAL_ENV"] = str(venv_bin.parent)
            env["PATH"] = f"{venv_bin}:{env['PATH']}"
    return env


def _actor_copilot_config_dir(work_root: Path, actor_name: str) -> Path:
    actor_name = session_registry._resolve_bound_actor_name(work_root, actor_name)
    actor_dir = actor_session_root(work_root, actor_name)
    return actor_dir / "state" / "copilot"


def _mark_actor_runtime_active(work_root: Path, actor_name: str) -> None:
    current = _read_actor_state(work_root, actor_name)
    if str(current.get("status") or "") in {
        "completed",
        "failed",
        "incomplete",
        "rejected",
        "signed_off",
    }:
        _write_actor_state(
            work_root,
            actor_name,
            {
                "heartbeat_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        return
    _write_actor_state(
        work_root,
        actor_name,
        {
            "status": "active",
            "heartbeat_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": None,
            "summary": None,
            "signoff_at": None,
            "signoff_summary": None,
            "exit_code": None,
        },
    )


def _actor_action_is_authoritative(actor_name: str) -> bool:
    return resolve_actor_context().speaker == _normalize_actor_name(actor_name)


def _spawn_actor_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )


def _feedback_warning(reason: str) -> str:
    return f"gotta actor feedback ignored: {reason}\n"


def _parse_feedback_directive(
    raw_line: str,
    *,
    actor_name: str,
) -> tuple[dict[str, str] | None, str | None]:
    if not raw_line.startswith(FEEDBACK_DIRECTIVE_PREFIX):
        return None, None
    payload_text = raw_line[len(FEEDBACK_DIRECTIVE_PREFIX) :].rstrip("\r\n")
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return None, "invalid json payload"
    if not isinstance(payload, dict):
        return None, "directive payload must be a JSON object"
    directive_actor = str(payload.get("actor") or "").strip()
    if directive_actor != actor_name:
        return None, f"actor mismatch for {actor_name}"
    surface = str(payload.get("surface") or "").strip()
    if surface == "logs":
        return (
            None,
            "logs directives are disabled; use surface `notes` for heartbeat/anchor/wave/signoff narration",
        )
    if surface not in FEEDBACK_SURFACES:
        return None, f"unsupported surface `{surface or 'missing'}`"
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        return None, "missing message"
    return {
        "actor": actor_name,
        "surface": surface,
        "message": message,
    }, None


def _apply_feedback_directive(
    work_root: Path,
    actor_name: str,
    *,
    surface: str,
    message: str,
    author: str = "",
) -> None:
    actor_root = session_registry._actor_session_dir(work_root, actor_name)
    first_line = message.splitlines()[0] if message.splitlines() else message
    rendered_author = author.strip() or actor_name
    require_writer(
        work_root,
        actor_name,
        writer=rendered_author,
        action="write into this actor branch",
    )
    if surface == "notes":
        append_actor_note(
            actor_root, actor_name, message=message, author=rendered_author
        )
        _reset_note_check_feedback(actor_root, actor_name)
        _append_actor_event(
            actor_root,
            actor_name,
            event="note",
            detail=first_line,
            author=rendered_author,
        )
        _actor_log_line(
            actor_root,
            actor_name,
            f"noted: {first_line}",
            author=rendered_author,
        )
        _record_actor_surface_activity(
            actor_root,
            actor_name=actor_name,
            surface="notes",
            action="append",
            detail="appended actor note",
            actor=rendered_author,
        )
        return
    append_oops_record(actor_root, message=message, actor=rendered_author)


def _record_launcher_heartbeat(work_root: Path, actor_name: str) -> None:
    _apply_feedback_directive(
        work_root,
        actor_name,
        surface="notes",
        message=LAUNCHER_HEARTBEAT_NOTE,
        author=LAUNCHER_AUTHOR,
    )


def _forward_actor_stream(
    stream: TextIO | None,
    *,
    target: TextIO,
    work_root: Path,
    actor_name: str,
    lock: threading.Lock,
) -> None:
    if stream is None:
        return
    for raw_line in iter(stream.readline, ""):
        directive, error = _parse_feedback_directive(raw_line, actor_name=actor_name)
        if error is not None:
            with lock:
                sys.stderr.write(_feedback_warning(error))
                sys.stderr.flush()
            continue
        if directive is not None:
            try:
                _apply_feedback_directive(
                    work_root,
                    actor_name,
                    surface=directive["surface"],
                    message=directive["message"],
                )
            except Exception as exc:
                with lock:
                    sys.stderr.write(_feedback_warning(str(exc)))
                    sys.stderr.flush()
            continue
        with lock:
            target.write(raw_line)
            target.flush()
    try:
        stream.close()
    except OSError:
        return


def _finalize_actor_runtime_exit(
    work_root: Path,
    actor_name: str,
    *,
    returncode: int,
    finished_at: str,
) -> int:
    current = _read_actor_state(work_root, actor_name)
    current_status = str(current.get("status") or "pending")
    requested_status = str(current.get("requested_status") or "")
    requested_summary = str(current.get("requested_summary") or "")
    requested_at = str(current.get("requested_at") or "")
    updates: dict[str, object] = {
        "finished_at": finished_at,
        "exit_code": returncode,
        "pid": None,
    }
    final_status = current_status
    if final_status not in {
        "completed",
        "failed",
        "incomplete",
        "rejected",
        "signed_off",
    }:
        if returncode == 0 and requested_status in {
            "completed",
            "failed",
            "signed_off",
        }:
            final_status = requested_status
        elif returncode == 0:
            final_status = "completed"
        else:
            final_status = "failed"
    updates["status"] = final_status
    if final_status == "signed_off":
        updates["signoff_at"] = str(
            current.get("signoff_at") or requested_at or finished_at
        )
        updates["signoff_summary"] = (
            str(current.get("signoff_summary") or requested_summary or "") or None
        )
        updates["summary"] = None
    elif final_status == "failed":
        updates["summary"] = (
            str(current.get("summary") or requested_summary or "").strip() or None
        )
        updates["signoff_at"] = None
        updates["signoff_summary"] = None
    else:
        updates["summary"] = (
            str(current.get("summary") or requested_summary or "").strip() or None
        )
    updates["requested_mode"] = None
    updates["requested_status"] = None
    updates["requested_summary"] = None
    updates["requested_at"] = None
    _write_actor_state(work_root, actor_name, updates)
    _append_actor_event(
        work_root,
        actor_name,
        event="runtime_exit",
        detail=f"actor process exited with code {returncode}",
        extra={"exit_code": returncode},
    )
    if final_status == "signed_off":
        _actor_log_line(work_root, actor_name, "signed off and exited")
    elif final_status == "completed":
        _actor_log_line(work_root, actor_name, "completed")
    elif final_status == "failed":
        _actor_log_line(
            work_root,
            actor_name,
            f"failed with exit code {returncode}",
        )
    else:
        _actor_log_line(
            work_root, actor_name, f"{final_status.replace('_', ' ')} and exited"
        )
    _sync_actor_todo_state(work_root)
    return int(returncode)


def _with_heartbeat(
    work_root: Path,
    actor_name: str,
    stop_event: threading.Event,
) -> threading.Thread:
    def beat() -> None:
        while not stop_event.wait(ACTOR_HEARTBEAT_SECONDS):
            _mark_actor_runtime_active(work_root, actor_name)
            _append_actor_event(work_root, actor_name, event="heartbeat")

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    return thread


def _add_root_args(parser: argparse.ArgumentParser) -> None:
    session_charter.add_target_args(parser)


def build_parser(command_name: str = "gotta actor") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description=(
            "Bind, launch, and disposition sibling actor sessions inside the "
            "active session. Actor launch creates the live actor runtime itself; do "
            "not pair another agent into that actor. If you need parallelism, bind "
            "and launch a sibling actor. Actor launch requires a real WANT.md and "
            "GOAL.md first; bind the actor, then rewrite actor charters with "
            "`gotta want|goal --actor <actor> ...`."
        ),
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=[
            "status",
            "bind",
            "launch",
            "heartbeat",
            "complete",
            "fail",
            "stop",
            "settle",
            "signoff",
        ],
        help="bind, inspect, launch, or disposition linked actors",
    )
    parser.add_argument("actors", nargs="*")
    _add_root_args(parser)
    parser.add_argument("--output", choices=["json", "text"], default="text")
    parser.add_argument("--summary", default="")
    return parser


def _timestamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session_root(args: argparse.Namespace) -> Path:
    root = session_scope._current_session_dir(
        explicit_session=getattr(args, "session", None),
        explicit_actor=None,
    )
    if root is None or not session_is_initialized(root):
        raise SystemExit(
            "start or bind a session first with `gotta ...`. Stable interactive "
            "contexts adopt and scaffold their deterministic session on first "
            'session-aware use. Use `gotta session init --session "$WS"` only '
            "when you intentionally want to scaffold one exact root."
        )
    return root.resolve()


def _actor_names(args: argparse.Namespace) -> list[str]:
    names: list[str] = []
    for value in [*getattr(args, "actors", []), getattr(args, "actor", "")]:
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized and normalized not in names:
            names.append(normalized)
    return names


def _actor_name(args: argparse.Namespace, action: str) -> str:
    names = _actor_names(args)
    if action == "status" and not names:
        return ""
    if len(names) != 1:
        raise SystemExit(f"`gotta actor {action}` requires exactly one actor")
    return names[0]


def _require_actor_name(actor_name: str, action: str) -> None:
    if not actor_name:
        raise SystemExit(f"missing actor for `gotta actor {action}`")


def _status_actors(
    work_root: Path,
    actor_name: str,
    *,
    explicit_session: str = "",
) -> list[str]:
    selected = list(session_scope._selected_actor_ids(work_root))
    if not actor_name:
        explicit_root = (
            Path(explicit_session).expanduser().resolve() if explicit_session else None
        )
        explicit_shared = bool(
            explicit_root is not None
            and (
                topology.parse_shared_session_root(explicit_root) is not None
                or (explicit_root / "actors").is_dir()
            )
        )
        if explicit_shared:
            primary = session_scope._primary_actor_name(work_root)
            if primary:
                return [primary]
            if len(selected) > 1:
                raise SystemExit(
                    "this shared session does not resolve to one canonical actor root; "
                    "pass `--actor <actor>` explicitly"
                )
        return selected
    resolved = session_registry._resolve_bound_actor_name(work_root, actor_name)
    if resolved not in selected:
        raise SystemExit(
            f"{resolved} is not bound for this session; bind them first with "
            f"`gotta actor bind {actor_name}`"
        )
    return [resolved]


def _render_status_text(payload: dict[str, dict[str, object]]) -> None:
    for actor_name, state in payload.items():
        line = (
            f"{actor_name}: {state['status']} "
            f"(voice: {state.get('voice', 'missing')}, progress: {state.get('progress_kind', 'none')}, "
            f"notes: {state['notes_status']}, artifacts: {state['artifact_count']})"
        )
        if state.get("still_running"):
            line += " [still running]"
        if (
            state.get("still_running")
            and state.get("progress_stale")
            and int(state.get("artifact_count") or 0) == 0
        ):
            line += " [low signal]"
        print(line)
        if state.get("evidence_live"):
            print("  evidence: live")
        if state.get("progress_stale") and state.get("progress_kind") != "none":
            print("  progress_stale: true")
        last_note_at = str(state.get("last_note_at") or "").strip()
        last_note_summary = str(state.get("last_note_summary") or "").strip()
        if last_note_at and last_note_summary:
            print(f"  recent_note: {last_note_at} {last_note_summary}")
        note_checks = int(state.get("note_checks_since_update") or 0)
        last_note_check_by = str(state.get("last_note_check_by") or "").strip()
        if note_checks > 0:
            suffix = f" by {last_note_check_by}" if last_note_check_by else ""
            print(f"  note_reads_since_update: {note_checks}{suffix}")
        last_activity_at = str(state.get("last_activity_at") or "").strip()
        last_activity_summary = str(state.get("last_activity_summary") or "").strip()
        if last_activity_at and last_activity_summary:
            print(f"  recent_progress: {last_activity_at} {last_activity_summary}")
        last_lifecycle_at = str(state.get("last_lifecycle_at") or "").strip()
        last_lifecycle_summary = str(state.get("last_lifecycle_summary") or "").strip()
        if last_lifecycle_at and last_lifecycle_summary:
            print(f"  recent_lifecycle: {last_lifecycle_at} {last_lifecycle_summary}")
        recent_artifacts = [
            str(item.get("locator") or "").strip()
            for item in state.get("recent_artifacts", [])
            if str(item.get("locator") or "").strip()
        ]
        if recent_artifacts:
            rendered = ", ".join(f"`{locator}`" for locator in recent_artifacts[:3])
            if len(recent_artifacts) > 3:
                rendered += f" (+{len(recent_artifacts) - 3} more)"
            print(f"  recent_artifacts: {rendered}")
        if state.get("still_running"):
            print("  still_running: true")
        if state.get("requested_pending"):
            print(
                f"  pending_disposition: {state.get('requested_label') or state['requested_status']}"
            )
        if state.get("next_step"):
            print(f"  next_step: {state['next_step']}")


def _sync_actor_outputs(
    work_root: Path, actor_name: str, *, sync_todo: bool = False
) -> None:
    if sync_todo:
        _sync_actor_todo_state(work_root)


def _record_requested_disposition(
    work_root: Path,
    actor_name: str,
    *,
    requested_status: str,
    requested_mode: str = "",
    summary: str,
    event: str,
    log_message: str,
    current_status: str,
    action_label: str | None = None,
) -> int:
    disposition_label = action_label or (
        "sign-off"
        if requested_status == "signed_off"
        else requested_status.replace("_", "-")
    )
    timestamp = _timestamp()
    _write_actor_state(
        work_root,
        actor_name,
        {
            "requested_mode": requested_mode or None,
            "requested_status": requested_status,
            "requested_summary": summary,
            "requested_at": timestamp,
        },
    )
    _append_actor_event(work_root, actor_name, event=event, detail=summary)
    _actor_log_line(work_root, actor_name, log_message)
    _sync_actor_outputs(work_root, actor_name)
    print(
        f"recorded {disposition_label} request for {actor_name}; "
        f"actor is still live so authoritative status stays {current_status}"
    )
    return 0


def _write_terminal_disposition(
    work_root: Path,
    actor_name: str,
    *,
    status: str,
    summary: str,
    event: str,
    log_message: str,
    signoff_at: str | None = None,
    detail: str | None = None,
) -> int:
    payload: dict[str, object] = {
        "status": status,
        "requested_mode": None,
        "requested_status": None,
        "requested_summary": None,
        "requested_at": None,
    }
    if status == "signed_off":
        cleaned_summary = summary.strip()
        payload.update(
            {
                "signoff_at": signoff_at or _timestamp(),
                "signoff_summary": cleaned_summary or None,
                "summary": None,
            }
        )
    else:
        payload.update(
            {
                "finished_at": _timestamp(),
                "summary": summary or None,
            }
        )
        if status == "failed":
            payload["signoff_at"] = None
            payload["signoff_summary"] = None
    _write_actor_state(work_root, actor_name, payload)
    _append_actor_event(
        work_root,
        actor_name,
        event=event,
        detail=detail if detail is not None else summary,
    )
    _actor_log_line(work_root, actor_name, log_message)
    _sync_actor_outputs(work_root, actor_name, sync_todo=True)
    if status == "signed_off":
        print(f"recorded sign-off for {actor_name}")
    else:
        print(f"marked {actor_name} {status}")
    return 0


def _runtime_closeout_note(current: dict[str, object]) -> bool:
    status = str(current.get("status") or "").strip()
    return bool(current.get("runtime_live")) or status in LIVE_STATUSES


def _cmd_status(args: argparse.Namespace, work_root: Path, actor_name: str) -> int:
    actors = _status_actors(
        work_root,
        actor_name,
        explicit_session=str(getattr(args, "session", None) or "").strip(),
    )
    payload = {
        current_actor: _actor_status_payload(work_root, current_actor)
        for current_actor in actors
    }
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if not actors:
        print(
            "no actors bound for this session; bind one intentionally with "
            + session_registry._actor_bind_examples(prefix="gotta actor bind")
        )
        return 0
    _render_status_text(payload)
    return 0


def _cmd_bind(args: argparse.Namespace, work_root: Path, actor_names: list[str]) -> int:
    if not actor_names:
        raise SystemExit(
            "missing actor for `gotta actor bind`; use "
            + session_registry._actor_bind_examples(prefix="gotta actor bind")
        )
    results: list[dict[str, object]] = []
    for actor_name in actor_names:
        results.append(_bind_actor(work_root, actor_name))
    if args.output == "json":
        print(
            json.dumps(
                {
                    "sessionRoot": str(work_root.resolve()),
                    "bindings": results,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    for result in results:
        print(str(result.get("message") or "").strip())
    return 0


def _cmd_heartbeat(work_root: Path, actor_name: str) -> int:
    _mark_actor_runtime_active(work_root, actor_name)
    _append_actor_event(work_root, actor_name, event="heartbeat")
    print(f"heartbeat recorded for {actor_name}")
    return 0


def _cmd_complete(args: argparse.Namespace, work_root: Path, actor_name: str) -> int:
    current = _actor_status_payload(work_root, actor_name)
    live_closeout = _runtime_closeout_note(current)
    if live_closeout:
        return _record_requested_disposition(
            work_root,
            actor_name,
            requested_status="completed",
            summary=args.summary,
            event="complete_requested",
            log_message=f"requested completion{': ' + args.summary if args.summary else ''}",
            current_status=str(current.get("status") or ""),
            action_label="completion",
        )
    return _write_terminal_disposition(
        work_root,
        actor_name,
        status="completed",
        summary=args.summary,
        event="completed",
        log_message=f"reviewed{': ' + args.summary if args.summary else ''}",
        detail=args.summary,
    )


def _cmd_fail(args: argparse.Namespace, work_root: Path, actor_name: str) -> int:
    current = _actor_status_payload(work_root, actor_name)
    if _runtime_closeout_note(current):
        return _record_requested_disposition(
            work_root,
            actor_name,
            requested_status="failed",
            summary=args.summary,
            event="failed_requested",
            log_message=f"requested failure{': ' + args.summary if args.summary else ''}",
            current_status=str(current.get("status") or ""),
        )
    return _write_terminal_disposition(
        work_root,
        actor_name,
        status="failed",
        summary=args.summary,
        event="failed",
        log_message=f"failed{': ' + args.summary if args.summary else ''}",
    )


def _cmd_stop(args: argparse.Namespace, work_root: Path, actor_name: str) -> int:
    current = _actor_status_payload(work_root, actor_name)
    live_closeout = _runtime_closeout_note(current)
    if live_closeout:
        return _record_requested_disposition(
            work_root,
            actor_name,
            requested_status=SUPERVISOR_GRACEFUL_STOP_STATUS,
            requested_mode=SUPERVISOR_GRACEFUL_STOP_MODE,
            summary=args.summary,
            event="stop_requested",
            log_message=f"requested graceful stop{': ' + args.summary if args.summary else ''}",
            current_status=str(current.get("status") or ""),
            action_label="stop",
        )
    return _write_terminal_disposition(
        work_root,
        actor_name,
        status="signed_off",
        summary=args.summary,
        event="stopped",
        log_message=f"stopped{': ' + args.summary if args.summary else ''}",
        signoff_at=_timestamp(),
        detail=args.summary,
    )


def _settled_status(current: dict[str, object]) -> str:
    requested_status = str(current.get("requested_status") or "").strip()
    if requested_status:
        return requested_status
    exit_code = current.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return "failed"
    if bool(current.get("notes_ready") or current.get("evidence_live")):
        return "completed"
    return "incomplete"


def _live_actor_settle_override(current: dict[str, object]) -> bool:
    return (
        bool(current.get("runtime_live"))
        and str(current.get("status") or "").strip() == "stalled"
        and bool(current.get("requested_pending"))
    )


def _cmd_settle(work_root: Path, actor_name: str) -> int:
    current = _actor_status_payload(work_root, actor_name)
    if bool(current.get("runtime_live")) and not _live_actor_settle_override(current):
        requested_status = str(current.get("requested_status") or "").strip()
        requested_label = str(current.get("requested_label") or "").strip()
        raise SystemExit(
            f"{actor_name} is still live; "
            + (
                f"the pending `{requested_label}` request will become authoritative when "
                "the runtime exits. "
                if requested_status
                else ""
            )
            + f"Recheck `gotta actor status {actor_name}` before settling."
        )
    final_status = _settled_status(current)
    requested_summary = str(current.get("requested_summary") or "").strip()
    timestamp = _timestamp()
    updates: dict[str, object] = {
        "status": final_status,
        "finished_at": str(current.get("finished_at") or timestamp),
        "pid": None,
        "requested_mode": None,
        "requested_status": None,
        "requested_summary": None,
        "requested_at": None,
    }
    if final_status == "signed_off":
        updates["signoff_at"] = str(current.get("signoff_at") or timestamp)
        updates["signoff_summary"] = (
            str(current.get("signoff_summary") or requested_summary).strip() or None
        )
        updates["summary"] = None
    else:
        updates["summary"] = (
            requested_summary or str(current.get("summary") or "").strip() or None
        )
        if final_status == "failed":
            updates["signoff_at"] = None
            updates["signoff_summary"] = None
    _write_actor_state(work_root, actor_name, updates)
    _append_actor_event(
        work_root,
        actor_name,
        event="settled",
        detail=f"settled actor lifecycle as {final_status}",
    )
    _actor_log_line(
        work_root, actor_name, f"settled as {final_status.replace('_', ' ')}"
    )
    _sync_actor_outputs(work_root, actor_name, sync_todo=True)
    print(f"settled {actor_name} as {final_status}")
    return 0


def _cmd_signoff(args: argparse.Namespace, work_root: Path, actor_name: str) -> int:
    summary = str(args.summary or "").strip()
    if not summary:
        raise SystemExit("signoff requires `--summary`")
    current = _actor_status_payload(work_root, actor_name)
    live_closeout = _runtime_closeout_note(current)
    if live_closeout:
        return _record_requested_disposition(
            work_root,
            actor_name,
            requested_status="signed_off",
            summary=summary,
            event="signoff_requested",
            log_message=f"requested sign-off: {summary}",
            current_status=str(current.get("status") or ""),
        )
    return _write_terminal_disposition(
        work_root,
        actor_name,
        status="signed_off",
        summary=summary,
        event="signed_off",
        log_message=f"signed off: {summary}",
        signoff_at=_timestamp(),
        detail=summary,
    )


def _cmd_launch(work_root: Path, actor_name: str) -> int:
    actor_name = session_registry._resolve_bound_actor_name(work_root, actor_name)
    current = _actor_status_payload(work_root, actor_name)
    if _runtime_closeout_note(current):
        raise SystemExit(
            f"{actor_name} is already {current['status']}; inspect with `gotta actor status {actor_name}`"
        )
    goal_path = work_root / "GOAL.md"
    if not goal_path.is_file():
        raise SystemExit(f"goal file does not exist: {goal_path}")
    blockers = _actor_launch_blockers(work_root, actor_name=actor_name)
    if blockers:
        raise SystemExit(
            "actor launch is blocked until the session framing is real: "
            + "; ".join(blockers)
            + ". Rewrite WANT first, then GOAL, then relaunch the actor."
        )
    session_bootstrap._ensure_actor_surface(work_root, actor_name)
    prompt = _actor_prompt(work_root=work_root, actor_name=actor_name)
    env = _actor_runtime_env(work_root, actor_name)
    state = load_state_env_at_root(work_root)
    actor = session_registry._actor_registry(work_root).get(actor_name)
    if actor is None:
        raise SystemExit(f"unknown actor: {actor_name}")
    model = str(actor.get("model") or "")
    resume_uuid = str(actor.get("resume_uuid") or "")
    launcher_actor = writer_name()
    launched_by = (
        launcher_actor
        if launcher_actor in set(session_scope._selected_actor_ids(work_root))
        and launcher_actor != actor_name
        else ""
    )
    repo_root = str(state.get(SESSION_REPO_ENV) or "").strip()
    actor_dir = actor_session_root(work_root, actor_name)
    copilot_config_dir = _actor_copilot_config_dir(work_root, actor_name)
    copilot_config_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    argv = [
        "copilot",
        "--config-dir",
        str(copilot_config_dir),
        "--allow-all",
        "--add-dir",
        str(work_root),
        *(
            ["--add-dir", str(Path(repo_root).expanduser().resolve())]
            if repo_root
            else []
        ),
        "--no-ask-user",
        "--stream=on",
        f"--model={model}",
        f"--resume={resume_uuid}",
        f"--prompt={prompt}",
    ]
    try:
        proc = _spawn_actor_process(argv, cwd=actor_dir, env=env)
    except OSError as exc:
        raise SystemExit(f"failed to launch {actor_name}: {exc}") from exc
    _write_actor_state(
        work_root,
        actor_name,
        {
            "status": "starting",
            "started_at": started_at,
            "launched_at": started_at,
            "launched_by": launched_by,
            "heartbeat_at": started_at,
            "pid": proc.pid,
            "model": model,
            "resume_uuid": resume_uuid,
            "finished_at": None,
            "summary": None,
            "signoff_at": None,
            "signoff_summary": None,
            "exit_code": None,
            "requested_mode": None,
            "requested_status": None,
            "requested_summary": None,
            "requested_at": None,
        },
    )
    _append_actor_event(work_root, actor_name, event="starting", detail=str(goal_path))
    _actor_log_line(work_root, actor_name, f"starting with {model}")
    _sync_actor_todo_state(work_root)
    _record_launcher_heartbeat(work_root, actor_name)
    print(
        f"launcher pulse landed for {actor_name}; the launched background runtime already is "
        "the actor. Wait for actor voice. Do not pair another agent into this actor; "
        "bind and launch a sibling actor for parallel work.",
        file=sys.stderr,
        flush=True,
    )
    stop_event = threading.Event()
    heartbeat = _with_heartbeat(work_root, actor_name, stop_event)
    output_lock = threading.Lock()
    stdout_thread = threading.Thread(
        target=_forward_actor_stream,
        kwargs={
            "stream": getattr(proc, "stdout", None),
            "target": sys.stdout,
            "work_root": work_root,
            "actor_name": actor_name,
            "lock": output_lock,
        },
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_forward_actor_stream,
        kwargs={
            "stream": getattr(proc, "stderr", None),
            "target": sys.stderr,
            "work_root": work_root,
            "actor_name": actor_name,
            "lock": output_lock,
        },
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = proc.wait()
    finally:
        stop_event.set()
        heartbeat.join(timeout=1)
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
    finished_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _finalize_actor_runtime_exit(
        work_root,
        actor_name,
        returncode=returncode,
        finished_at=finished_at,
    )


def main(argv: list[str] | None = None) -> int:
    argv = _normalize_args(list(argv or []))
    if is_long_help_request(argv):
        print(format_long_help(build_parser()))
        return 0
    parser = build_parser()
    args = parser.parse_args(argv)
    action = args.action or "status"
    work_root = _session_root(args)
    if action == "bind":
        return _cmd_bind(args, work_root, _actor_names(args))
    actor_name = _actor_name(args, action)
    if action == "status":
        return _cmd_status(args, work_root, actor_name)
    _require_actor_name(actor_name, action)
    actor_name = session_registry._resolve_bound_actor_name(work_root, actor_name)
    if action == "launch":
        return _cmd_launch(work_root, actor_name)
    session_bootstrap._ensure_actor_surface(work_root, actor_name)
    if action == "heartbeat":
        return _cmd_heartbeat(work_root, actor_name)
    if action == "complete":
        return _cmd_complete(args, work_root, actor_name)
    if action == "fail":
        return _cmd_fail(args, work_root, actor_name)
    if action == "stop":
        return _cmd_stop(args, work_root, actor_name)
    if action == "settle":
        return _cmd_settle(work_root, actor_name)
    if action == "signoff":
        return _cmd_signoff(args, work_root, actor_name)
    raise SystemExit(f"unsupported actor action: {action}")
