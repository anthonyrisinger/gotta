"""Top-level linked-peer control surface."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import threading
from textwrap import dedent

from gotta.compat import UTC, datetime
from gotta.actors import ACTOR_SPEAKER_ENV, resolve_actor_context
from gotta.content import load_state_env_at_root
from gotta.helptext import format_long_help, is_long_help_request
from gotta.notes import peer_notes_log_path
from gotta.peer import (
    SUPERVISOR_GRACEFUL_STOP_MODE,
    SUPERVISOR_GRACEFUL_STOP_STATUS,
    peer_session_root,
)
from gotta import session as session_plugin
from gotta.session import (
    _actor_registry_from_state,
    _normalize_peer_name,
    _peer_goal_path,
    _peer_label,
    _peer_want_path,
    _read_peer_state,
    _write_peer_state,
)


PEER_HEARTBEAT_SECONDS = 30
LIVE_STATUSES = {"starting", "active", "producing_evidence", "stalled"}
TERMINAL_STATUSES = {"completed", "failed", "incomplete", "rejected", "signed_off"}


def _normalize_args(argv: list[str]) -> list[str]:
    if not argv:
        return ["status"]
    first = argv[0]
    actions = {
        "status",
        "with",
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


def _peer_prompt(*, work_root: Path, peer_name: str) -> str:
    peer_dir = peer_session_root(work_root, peer_name)
    peer_want = _peer_want_path(work_root, peer_name)
    peer_goal = _peer_goal_path(work_root, peer_name)
    notes_file = session_plugin.peer_notes_surface_path(work_root, peer_name)
    notes_log = peer_notes_log_path(work_root, peer_name)
    label = _peer_label(peer_name)
    want_text = peer_want.read_text(encoding="utf-8").strip()
    goal_text = peer_goal.read_text(encoding="utf-8").strip()
    return dedent(
        f"""\
        You are {label.lower()} inside a first-class peer gotta session that shares one evidence web.

        Use `gotta` itself as the primary investigation surface. If a native path
        is thin and you must use another tool, disclose that move through
        `gotta notes append {peer_name} ...` together with why the native path
        was insufficient.

        Session layout:

        - your peer session root: `{peer_dir}`
        - linked shared evidence store: `{peer_dir / 'content'}`

        Readable peer surfaces:

        - peer-local WANT present before launch: `{peer_want}`
        - peer-local GOAL present before launch: `{peer_goal}`
        - peer-local TODO projection: `{peer_dir / 'TODO.md'}`
        - notes projection: `{notes_file}`
        - shared live pulse: `{peer_dir / 'LOGS.md'}`
        - shared friction log: `{peer_dir / 'OOPS.md'}`

        Canonical peer mutation state:

        - peer-local todo log: `{peer_dir / 'state' / 'todo.jsonl'}`
        - notes log: `{notes_log}`

        Current peer want / intent frame:

        - `{peer_want}`
        {want_text or "_empty_"}

        Current peer goal frame:

        - `{peer_goal}`
        {goal_text or "_empty_"}

        Evidence-first peer contract:

        - prefer `gotta` commands over side channels
        - use `gotta read`, `gotta session ...`, `gotta peer ...`, `gotta todo ...`, `gotta logs ...`, `gotta oops ...`, and `gotta notes ...` first for native inspection and mutation
        - for large native outputs, try `gotta read --head`, `--tail`, or `--section` before shell slicing
        - if no native path exists, disclose that gap in a durable note instead of silently routing around it through shell traversal
        - treat peer-local WANT.md and GOAL.md as operator-authored live surfaces, not hidden templates that need to be rediscovered
        - treat peer-local WANT/GOAL/TODO plus the shared LOGS/OOPS surfaces and the evidence web as the live truth surfaces
        - disclose any non-native move as a native-coverage gap instead of hiding it
        - materialized evidence becomes usable immediately through manifest, timeline, leads, and graph even before notes catch up
        - append a first durable note immediately after the first strong anchor or branch; do not wait for a full evidence wave
        - append another durable note after each material evidence wave or when the plan changes
        - if you materially expanded the evidence web since your last note, append a new durable note before requesting completion or sign-off
        - if the supervisor records a pending graceful stop or `failed` disposition, treat that as a stopping signal: stop new retrieval, append one final durable note, and run `gotta peer signoff {peer_name} --summary "<one-line sign-off>"` promptly
        - native `gotta ...` commands will repeat that stopping warning while the supervisor stop request is still pending
        - do not author the final dossier, final brief, or top-level synthesis from this peer workspace
        - do not rewrite another linked session's local surfaces unless you intentionally mean to change shared team state
        - append running notes with `gotta notes append {peer_name} --stdin`; empty notes at closeout are a visibility failure, not success
        - when you are truly done, run:
          `gotta peer signoff {peer_name} --summary "<one-line sign-off>"`
        """
    )


def _peer_runtime_env(work_root: Path, peer_name: str) -> dict[str, str]:
    peer_dir = peer_session_root(work_root, peer_name)
    env = os.environ.copy()
    env.update(load_state_env_at_root(peer_dir))
    env["GOTTA_PEER_LABEL"] = _normalize_peer_name(peer_name)
    env["GOTTA_PEER_DIR"] = str(peer_dir)
    env["GOTTA_PEER_NOTES_PATH"] = str(session_plugin.peer_notes_surface_path(work_root, peer_name))
    env["GOTTA_PEER_NOTES_LOG_PATH"] = str(peer_notes_log_path(work_root, peer_name))
    env[ACTOR_SPEAKER_ENV] = _normalize_peer_name(peer_name)
    repo = env.get("GOTTA_WORK_REPO", "").strip()
    env["PATH"] = f"{peer_dir / 'bin'}:{env.get('PATH', '')}"
    if repo:
        venv_bin = Path(repo) / ".venv" / "bin"
        if venv_bin.is_dir():
            env["VIRTUAL_ENV"] = str(venv_bin.parent)
            env["PATH"] = f"{venv_bin}:{env['PATH']}"
    return env


def _mark_peer_runtime_active(work_root: Path, peer_name: str) -> None:
    current = _read_peer_state(work_root, peer_name)
    if str(current.get("status") or "") in {"completed", "failed", "incomplete", "rejected", "signed_off"}:
        _write_peer_state(
            work_root,
            peer_name,
            {
                "heartbeat_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        session_plugin._sync_peer_projection_surfaces(work_root, peer_name)
        return
    _write_peer_state(
        work_root,
        peer_name,
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
    session_plugin._sync_peer_projection_surfaces(work_root, peer_name)


def _peer_action_is_authoritative(peer_name: str) -> bool:
    return resolve_actor_context().speaker == _normalize_peer_name(peer_name)


def _spawn_peer_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(argv, cwd=cwd, env=env)


def _finalize_peer_runtime_exit(
    work_root: Path,
    peer_name: str,
    *,
    returncode: int,
    finished_at: str,
) -> int:
    current = _read_peer_state(work_root, peer_name)
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
    if final_status not in {"completed", "failed", "incomplete", "rejected", "signed_off"}:
        if returncode == 0 and requested_status in {"completed", "failed", "signed_off"}:
            final_status = requested_status
        elif returncode == 0:
            final_status = "completed"
        else:
            final_status = "failed"
    updates["status"] = final_status
    if final_status == "signed_off":
        updates["signoff_at"] = str(current.get("signoff_at") or requested_at or finished_at)
        updates["signoff_summary"] = str(
            current.get("signoff_summary") or requested_summary or ""
        ) or None
        updates["summary"] = None
    elif final_status == "failed":
        updates["summary"] = str(current.get("summary") or requested_summary or "").strip() or None
        updates["signoff_at"] = None
        updates["signoff_summary"] = None
    else:
        updates["summary"] = str(current.get("summary") or requested_summary or "").strip() or None
    updates["requested_mode"] = None
    updates["requested_status"] = None
    updates["requested_summary"] = None
    updates["requested_at"] = None
    _write_peer_state(work_root, peer_name, updates)
    session_plugin._append_peer_event(
        work_root,
        peer_name,
        event="runtime_exit",
        detail=f"peer process exited with code {returncode}",
        extra={"exit_code": returncode},
    )
    if final_status == "signed_off":
        session_plugin._peer_log_line(work_root, peer_name, "signed off and exited")
    elif final_status == "completed":
        session_plugin._peer_log_line(work_root, peer_name, "completed")
    elif final_status == "failed":
        session_plugin._peer_log_line(
            work_root,
            peer_name,
            f"failed with exit code {returncode}",
        )
    else:
        session_plugin._peer_log_line(
            work_root, peer_name, f"{final_status.replace('_', ' ')} and exited"
        )
    session_plugin._sync_peer_todo_state(work_root)
    session_plugin._sync_peer_projection_surfaces(work_root, peer_name)
    return int(returncode)


def _with_heartbeat(
    work_root: Path,
    peer_name: str,
    stop_event: threading.Event,
) -> threading.Thread:
    def beat() -> None:
        while not stop_event.wait(PEER_HEARTBEAT_SECONDS):
            _mark_peer_runtime_active(work_root, peer_name)
            session_plugin._append_peer_event(work_root, peer_name, event="heartbeat")

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    return thread


def _add_root_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", help="session root")


def build_parser(command_name: str = "gotta peer") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description=(
            "Configure, launch, and disposition linked peer sessions inside the "
            "active session. Peer launch requires a real WANT.md and GOAL.md first; "
            "rewrite peer charters with `gotta want|goal --session peers/<peer> ...`."
        ),
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=[
            "status",
            "with",
            "launch",
            "heartbeat",
            "complete",
            "fail",
            "stop",
            "settle",
            "signoff",
        ],
        help="configure, inspect, launch, or disposition linked peers",
    )
    parser.add_argument("peers", nargs="*")
    _add_root_args(parser)
    parser.add_argument("--output", choices=["json", "text"], default="text")
    parser.add_argument("--summary", default="")
    return parser


def _timestamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _work_root(args: argparse.Namespace) -> Path:
    return session_plugin._work_workspace_dir(explicit_session=getattr(args, "session", None)).resolve()


def _peer_names(args: argparse.Namespace) -> list[str]:
    return [
        _normalize_peer_name(peer)
        for peer in getattr(args, "peers", [])
        if str(peer).strip()
    ]


def _peer_name(args: argparse.Namespace, action: str) -> str:
    names = _peer_names(args)
    if action == "status" and not names:
        return ""
    if len(names) != 1:
        raise SystemExit(f"`gotta peer {action}` requires exactly one peer")
    return names[0]


def _require_peer_name(peer_name: str, action: str) -> None:
    if not peer_name:
        raise SystemExit(f"missing peer for `gotta peer {action}`")


def _status_peers(work_root: Path, peer_name: str) -> list[str]:
    selected = list(session_plugin._selected_actor_ids(work_root))
    if not peer_name:
        return selected
    if peer_name not in selected:
        raise SystemExit(
            f"{peer_name} is not configured for this session; choose them first with "
            f"`gotta peer with {_peer_label(peer_name)}`"
        )
    return [peer_name]


def _render_status_text(payload: dict[str, dict[str, object]]) -> None:
    for peer_name, state in payload.items():
        line = (
            f"{peer_name}: {state['status']} "
            f"(notes: {state['notes_status']}, artifacts: {state['artifact_count']})"
        )
        if state.get("still_running"):
            line += " [still running]"
        print(line)
        if state.get("evidence_live"):
            print("  evidence: live")
        last_activity_at = str(state.get("last_activity_at") or "").strip()
        last_activity_summary = str(state.get("last_activity_summary") or "").strip()
        if last_activity_at and last_activity_summary:
            print(f"  recent_activity: {last_activity_at} {last_activity_summary}")
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
            print(f"  pending_disposition: {state.get('requested_label') or state['requested_status']}")
        if state.get("next_step"):
            print(f"  next_step: {state['next_step']}")


def _sync_peer_outputs(work_root: Path, peer_name: str, *, sync_todo: bool = False) -> None:
    if sync_todo:
        session_plugin._sync_peer_todo_state(work_root)
    session_plugin._sync_peer_projection_surfaces(work_root, peer_name)


def _record_requested_disposition(
    work_root: Path,
    peer_name: str,
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
        "sign-off" if requested_status == "signed_off" else requested_status.replace("_", "-")
    )
    timestamp = _timestamp()
    _write_peer_state(
        work_root,
        peer_name,
        {
            "requested_mode": requested_mode or None,
            "requested_status": requested_status,
            "requested_summary": summary,
            "requested_at": timestamp,
        },
    )
    session_plugin._append_peer_event(work_root, peer_name, event=event, detail=summary)
    session_plugin._peer_log_line(work_root, peer_name, log_message)
    _sync_peer_outputs(work_root, peer_name)
    print(
        f"recorded {disposition_label} request for {peer_name}; "
        f"peer is still live so authoritative status stays {current_status}"
    )
    return 0


def _write_terminal_disposition(
    work_root: Path,
    peer_name: str,
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
    _write_peer_state(work_root, peer_name, payload)
    session_plugin._append_peer_event(
        work_root,
        peer_name,
        event=event,
        detail=detail if detail is not None else summary,
    )
    session_plugin._peer_log_line(work_root, peer_name, log_message)
    _sync_peer_outputs(work_root, peer_name, sync_todo=True)
    if status == "signed_off":
        print(f"recorded sign-off for {peer_name}")
    else:
        print(f"marked {peer_name} {status}")
    return 0


def _runtime_closeout_note(current: dict[str, object]) -> bool:
    status = str(current.get("status") or "").strip()
    return bool(current.get("runtime_live")) or status in LIVE_STATUSES


def _cmd_status(args: argparse.Namespace, work_root: Path, peer_name: str) -> int:
    peers = _status_peers(work_root, peer_name)
    payload = {
        current_peer: session_plugin._peer_status_payload(work_root, current_peer)
        for current_peer in peers
    }
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if not peers:
        print(
            "no peers configured for this session; choose one intentionally with "
            + session_plugin._work_with_examples(prefix="gotta peer with")
        )
        return 0
    _render_status_text(payload)
    return 0


def _cmd_with(work_root: Path, peer_names: list[str]) -> int:
    if not peer_names:
        raise SystemExit("`gotta peer with` requires at least one actor")
    results: list[str] = []
    for peer_name in peer_names:
        results.append(session_plugin._configure_peer(work_root, peer_name))
    for result in results:
        print(result)
    return 0


def _cmd_heartbeat(work_root: Path, peer_name: str) -> int:
    _mark_peer_runtime_active(work_root, peer_name)
    session_plugin._append_peer_event(work_root, peer_name, event="heartbeat")
    print(f"heartbeat recorded for {peer_name}")
    return 0


def _cmd_complete(args: argparse.Namespace, work_root: Path, peer_name: str) -> int:
    current = session_plugin._peer_status_payload(work_root, peer_name)
    live_closeout = _runtime_closeout_note(current)
    detail = (
        f"{args.summary}; closed while runtime still live"
        if args.summary and live_closeout
        else "closed while runtime still live"
        if live_closeout
        else args.summary
    )
    log_message = (
        f"reviewed while runtime still live{': ' + args.summary if args.summary else ''}"
        if live_closeout
        else f"reviewed{': ' + args.summary if args.summary else ''}"
    )
    return _write_terminal_disposition(
        work_root,
        peer_name,
        status="completed",
        summary=args.summary,
        event="completed",
        log_message=log_message,
        detail=detail,
    )


def _cmd_fail(args: argparse.Namespace, work_root: Path, peer_name: str) -> int:
    current = session_plugin._peer_status_payload(work_root, peer_name)
    current_status = str(current.get("status") or "")
    if current_status in LIVE_STATUSES and not _peer_action_is_authoritative(peer_name):
        return _record_requested_disposition(
            work_root,
            peer_name,
            requested_status="failed",
            summary=args.summary,
            event="failed_requested",
            log_message=f"requested failure{': ' + args.summary if args.summary else ''}",
            current_status=current_status,
        )
    return _write_terminal_disposition(
        work_root,
        peer_name,
        status="failed",
        summary=args.summary,
        event="failed",
        log_message=f"failed{': ' + args.summary if args.summary else ''}",
    )


def _cmd_stop(args: argparse.Namespace, work_root: Path, peer_name: str) -> int:
    current = session_plugin._peer_status_payload(work_root, peer_name)
    current_status = str(current.get("status") or "")
    live_closeout = _runtime_closeout_note(current)
    if current_status in LIVE_STATUSES and not _peer_action_is_authoritative(peer_name):
        return _record_requested_disposition(
            work_root,
            peer_name,
            requested_status=SUPERVISOR_GRACEFUL_STOP_STATUS,
            requested_mode=SUPERVISOR_GRACEFUL_STOP_MODE,
            summary=args.summary,
            event="stop_requested",
            log_message=f"requested graceful stop{': ' + args.summary if args.summary else ''}",
            current_status=current_status,
            action_label="stop",
        )
    detail = (
        f"{args.summary}; stopped while runtime still live"
        if args.summary and live_closeout
        else "stopped while runtime still live"
        if live_closeout
        else args.summary
    )
    log_message = (
        f"stopped while runtime still live{': ' + args.summary if args.summary else ''}"
        if live_closeout
        else f"stopped{': ' + args.summary if args.summary else ''}"
    )
    return _write_terminal_disposition(
        work_root,
        peer_name,
        status="signed_off",
        summary=args.summary,
        event="stopped",
        log_message=log_message,
        signoff_at=_timestamp(),
        detail=detail,
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


def _live_peer_settle_override(current: dict[str, object]) -> bool:
    return (
        bool(current.get("runtime_live"))
        and str(current.get("status") or "").strip() == "stalled"
        and bool(current.get("requested_pending"))
    )


def _cmd_settle(work_root: Path, peer_name: str) -> int:
    current = session_plugin._peer_status_payload(work_root, peer_name)
    if bool(current.get("runtime_live")) and not _live_peer_settle_override(current):
        requested_status = str(current.get("requested_status") or "").strip()
        requested_label = str(current.get("requested_label") or "").strip()
        raise SystemExit(
            f"{peer_name} is still live; "
            + (
                f"the pending `{requested_label}` request will become authoritative when "
                "the runtime exits. "
                if requested_status
                else ""
            )
            + f"Recheck `gotta peer status {peer_name}` before settling."
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
    _write_peer_state(work_root, peer_name, updates)
    session_plugin._append_peer_event(
        work_root,
        peer_name,
        event="settled",
        detail=f"settled peer lifecycle as {final_status}",
    )
    session_plugin._peer_log_line(
        work_root, peer_name, f"settled as {final_status.replace('_', ' ')}"
    )
    _sync_peer_outputs(work_root, peer_name, sync_todo=True)
    print(f"settled {peer_name} as {final_status}")
    return 0


def _cmd_signoff(args: argparse.Namespace, work_root: Path, peer_name: str) -> int:
    summary = str(args.summary or "").strip()
    if not summary:
        raise SystemExit("signoff requires `--summary`")
    current = session_plugin._peer_status_payload(work_root, peer_name)
    live_closeout = _runtime_closeout_note(current)
    detail = (
        f"{summary}; closed while runtime still live"
        if live_closeout
        else summary
    )
    log_message = (
        f"signed off while runtime still live: {summary}"
        if live_closeout
        else f"signed off: {summary}"
    )
    return _write_terminal_disposition(
        work_root,
        peer_name,
        status="signed_off",
        summary=summary,
        event="signed_off",
        log_message=log_message,
        signoff_at=_timestamp(),
        detail=detail,
    )


def _cmd_launch(work_root: Path, peer_name: str) -> int:
    current = session_plugin._peer_status_payload(work_root, peer_name)
    if current["status"] in {"starting", "active"}:
        raise SystemExit(
            f"{peer_name} is already {current['status']}; inspect with `gotta peer status {peer_name}`"
        )
    goal_path = work_root / "GOAL.md"
    if not goal_path.is_file():
        raise SystemExit(f"goal file does not exist: {goal_path}")
    blockers = session_plugin._peer_launch_blockers(work_root, peer_name=peer_name)
    if blockers:
        raise SystemExit(
            "peer launch is blocked until the work framing is real: "
            + "; ".join(blockers)
            + ". Rewrite WANT first, then GOAL, then relaunch the peer."
        )
    session_plugin._ensure_peer_surface(work_root, peer_name)
    prompt = _peer_prompt(work_root=work_root, peer_name=peer_name)
    env = _peer_runtime_env(work_root, peer_name)
    state = load_state_env_at_root(work_root)
    actor = _actor_registry_from_state(state).get(peer_name)
    if actor is None:
        raise SystemExit(f"unknown peer: {peer_name}")
    model = str(actor.get("model") or "")
    resume_uuid = str(actor.get("resume_uuid") or "")
    repo_root = str(state.get("GOTTA_WORK_REPO") or "").strip()
    peer_dir = peer_session_root(work_root, peer_name)
    started_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    argv = [
        "copilot",
        "--allow-all",
        "--add-dir",
        str(work_root),
        *(
            ["--add-dir", str(Path(repo_root).expanduser().resolve())]
            if repo_root
            else []
        ),
        "--experimental",
        "--no-ask-user",
        "--no-alt-screen",
        "--stream=on",
        f"--model={model}",
        f"--resume={resume_uuid}",
        f"--prompt={prompt}",
    ]
    try:
        proc = _spawn_peer_process(argv, cwd=peer_dir, env=env)
    except OSError as exc:
        raise SystemExit(f"failed to launch {peer_name}: {exc}") from exc
    _write_peer_state(
        work_root,
        peer_name,
        {
            "status": "starting",
            "started_at": started_at,
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
    session_plugin._append_peer_event(work_root, peer_name, event="starting", detail=str(goal_path))
    session_plugin._peer_log_line(work_root, peer_name, f"starting with {model}")
    session_plugin._sync_peer_todo_state(work_root)
    session_plugin._sync_peer_projection_surfaces(work_root, peer_name)
    stop_event = threading.Event()
    heartbeat = _with_heartbeat(work_root, peer_name, stop_event)
    try:
        returncode = proc.wait()
    finally:
        stop_event.set()
        heartbeat.join(timeout=1)
    finished_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _finalize_peer_runtime_exit(
        work_root,
        peer_name,
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
    work_root = _work_root(args)
    if action == "with":
        return _cmd_with(work_root, _peer_names(args))
    peer_name = _peer_name(args, action)
    if action == "status":
        return _cmd_status(args, work_root, peer_name)
    _require_peer_name(peer_name, action)
    if action == "launch":
        return _cmd_launch(work_root, peer_name)
    session_plugin._ensure_peer_surface(work_root, peer_name)
    if action == "heartbeat":
        return _cmd_heartbeat(work_root, peer_name)
    if action == "complete":
        return _cmd_complete(args, work_root, peer_name)
    if action == "fail":
        return _cmd_fail(args, work_root, peer_name)
    if action == "stop":
        return _cmd_stop(args, work_root, peer_name)
    if action == "settle":
        return _cmd_settle(work_root, peer_name)
    if action == "signoff":
        return _cmd_signoff(args, work_root, peer_name)
    raise SystemExit(f"unsupported peer action: {action}")
