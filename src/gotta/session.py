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
    current_actor,
    discover_state_env,
    env_mapping,
    load_state_env_at_root,
    resolve_dirs,
    session_identity,
    session_is_initialized,
    session_relative_path,
    session_shared_id,
    session_token,
    sh_quote,
    shared_session_root,
    state_dir_path,
    state_env_path,
    stdin_has_meaningful_text,
    session_surface_initialized,
    write_session_state,
)
from gotta.friction import OOPS_CHANNEL, oops_log_path, render_oops_markdown, visible_channel_records
from gotta.helptext import format_long_help, is_long_help_request
from gotta.logs import (
    append_log_record,
    logs_state_path,
    sync_logs_projection,
)
from gotta.notes import (
    actor_voice,
    actor_notes_ready,
    visible_actor_notes_records,
    actor_notes_surface_path,
    sync_actor_notes_projection,
)
from gotta import topology
from gotta.actor import (
    normalize_actor_name as _shared_normalize_actor_name,
    session_actor,
    actor_session_root,
    requested_disposition_label,
    writer_role,
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
    "closing",
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
    "closing",
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
        explicit_actor=None,
    )
    if current is None:
        raise SystemExit(
            "start or bind a session first with `gotta ...` or bootstrap one "
            "manually with `gotta session init --session \"$WS\"`"
        )
    if explicit_actor:
        current = _actor_session_dir(
            current,
            _resolve_bound_actor_name(current, explicit_actor),
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


def _group_session_root(work_dir: Path) -> Path:
    resolved = work_dir.expanduser().resolve()
    if (resolved / "actors").is_dir():
        return resolved
    if resolved.parent.name == "actors":
        return resolved.parent.parent.resolve()
    return resolved


def _shared_session_dir(
    *,
    explicit_session: str | None,
) -> Path:
    current = _current_session_dir(
        explicit_session,
        explicit_actor=None,
    )
    if current is None:
        raise SystemExit(
            "start or bind a session first with `gotta ...` or bootstrap one "
            "manually with `gotta session init --session \"$WS\"`"
        )
    return _group_session_root(current)


def _read_scope(
    *,
    explicit_session: str | None,
) -> tuple[Path, str]:
    current = _session_dir(
        explicit_session=explicit_session,
        explicit_actor=None,
    ).resolve()
    if current.parent.name == "actors" or topology.parse_grouped_session_root(current) is not None:
        actor_name = session_identity(current)
        if actor_name:
            return current, actor_name
    return current, ""


def _observation_scope(
    *,
    explicit_session: str | None,
    explicit_actor: str | None = None,
) -> tuple[Path, str]:
    if explicit_actor:
        current = _session_dir(
            explicit_session=explicit_session,
            explicit_actor=explicit_actor,
        ).resolve()
        return current, session_identity(current)
    current = _session_dir(
        explicit_session=explicit_session,
        explicit_actor=None,
    ).resolve()
    grouped_root = _group_session_root(current)
    if grouped_root != current:
        return grouped_root, ""
    return current, session_identity(current) if current.parent.name == "actors" else ""


def _target_actor_ids(work_dir: Path, actor_ref: str | None = None) -> tuple[str, ...]:
    selected = _selected_actor_ids(work_dir)
    if (
        not selected
        and topology.parse_grouped_session_root(work_dir) is None
        and topology.parse_shared_session_root(work_dir) is None
        and session_is_initialized(work_dir)
    ):
        identity = session_identity(work_dir)
        if identity:
            selected = (identity,)
    if actor_ref:
        resolved = _resolve_bound_actor_name(work_dir, actor_ref)
        if resolved not in selected:
            raise SystemExit(
                f"{resolved} is not bound for this session; bind them first with "
                f"`gotta actor bind {actor_ref}`"
            )
        return (resolved,)
    return selected


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


def argv_has_flag(argv: list[str], flag: str) -> bool:
    return any(token == flag or token.startswith(f"{flag}=") for token in argv)


def argv_positionals(
    argv: list[str],
    *,
    valued_flags: tuple[str, ...] = (),
) -> list[str]:
    positionals: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            positionals.extend(part for part in argv[index + 1 :] if part)
            break
        if token in valued_flags:
            index += 2
            continue
        matched_flag = next(
            (flag for flag in valued_flags if token.startswith(f"{flag}=")),
            "",
        )
        if matched_flag:
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        positionals.append(token)
        index += 1
    return positionals


def charter_session_access_mode(argv: list[str]) -> str:
    return "write" if (argv_has_flag(argv, "--stdin") or argv_has_flag(argv, "--from-file")) else "read"


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
    has_payload = bool(args.from_file or args.use_stdin)
    if not has_payload:
        if getattr(args, "actor", None):
            work_dir = _session_dir(
                explicit_session=getattr(args, "session", None),
                explicit_actor=getattr(args, "actor", None),
            )
            path = work_dir / surface_name
            if not path.is_file():
                raise SystemExit(f"missing {surface_name} surface: {path}")
            print(path.read_text(encoding="utf-8"), end="")
            return 0
        work_dir, scoped_actor = _read_scope(
            explicit_session=getattr(args, "session", None),
        )
        if scoped_actor:
            path = work_dir / surface_name
            if not path.is_file():
                raise SystemExit(f"missing {surface_name} surface: {path}")
            print(path.read_text(encoding="utf-8"), end="")
            return 0
        actor_ids = _target_actor_ids(work_dir)
        if not actor_ids:
            raise SystemExit(
                "no actors bound for this session; bind one intentionally with "
                + _actor_bind_examples(prefix="gotta actor bind")
            )
        if len(actor_ids) == 1:
            path = _actor_session_dir(work_dir, actor_ids[0]) / surface_name
            if not path.is_file():
                raise SystemExit(f"missing {surface_name} surface: {path}")
            print(path.read_text(encoding="utf-8"), end="")
            return 0
        sections: list[str] = []
        for actor_name in actor_ids:
            path = _actor_session_dir(work_dir, actor_name) / surface_name
            if not path.is_file():
                continue
            label = _actor_label(actor_name, work_dir=work_dir)
            body = path.read_text(encoding="utf-8").rstrip()
            sections.append(
                "\n".join(
                    [
                        f"## {label} ({actor_name})",
                        "",
                        body or "_empty_",
                    ]
                ).rstrip()
            )
        if not sections:
            raise SystemExit(
                f"missing {surface_name} surface across bound actors in {work_dir}"
            )
        print("\n\n".join(sections) + "\n", end="")
        return 0
    work_dir = _session_dir(
        explicit_session=getattr(args, "session", None),
        explicit_actor=getattr(args, "actor", None),
    )
    path = work_dir / surface_name
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
ACTOR_DEFAULT_MODEL = ACTOR_INDEX["codex"].default_model
SESSION_ACTORS_METADATA_KEY = "actors"
SESSION_MEMBERS_METADATA_KEY = "members"


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


def _actor_is_fingerprint(value: str) -> bool:
    normalized = topology.normalize_identity(value)
    return len(normalized) == 12 and all(ch in "0123456789abcdef" for ch in normalized)


def _actor_template_spec(value: str) -> ActorSpec | None:
    normalized = _shared_normalize_actor_name(value)
    if not normalized:
        return None
    template_id = ACTOR_ALIASES.get(normalized)
    if template_id is None:
        for spec in DEFAULT_ACTORS:
            if _shared_normalize_actor_name(spec.label) == normalized:
                template_id = spec.actor_id
                break
    if template_id is None:
        return None
    return ACTOR_INDEX.get(template_id)


def _resolve_actor_name(value: str, *, kind: str = "actor") -> str:
    normalized = _shared_normalize_actor_name(value)
    if not normalized:
        raise SystemExit(f"missing {kind}")
    return normalized


def _normalize_actor_name(value: str) -> str:
    return _resolve_actor_name(value, kind="actor")


def _actor_label(actor_name: str, *, work_dir: Path | None = None) -> str:
    normalized = _normalize_actor_name(actor_name)
    if work_dir is not None:
        payload = _actor_registry(work_dir).get(normalized)
        label = str(payload.get("label") or "").strip() if payload is not None else ""
        if label:
            return label
    spec = _actor_template_spec(normalized)
    if spec is not None:
        return spec.label
    return normalized.replace("-", " ").title()


def _default_actor_registry() -> dict[str, dict[str, str]]:
    return {}


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
            spec = _actor_template_spec(str(actor_payload.get("template") or normalized))
            registry[normalized] = {
                "label": str(
                    actor_payload.get("label")
                    or (spec.label if spec else normalized.title())
                ),
                "model": str(
                    actor_payload.get("model")
                    or (spec.default_model if spec else ACTOR_DEFAULT_MODEL)
                ).strip(),
                "resume_uuid": str(actor_payload.get("resume_uuid") or "").strip(),
                "template": str(actor_payload.get("template") or (spec.actor_id if spec else "")).strip(),
            }
    for actor_id, actor_payload in registry.items():
        spec = _actor_template_spec(str(actor_payload.get("template") or actor_id))
        if spec is not None:
            actor_payload["label"] = actor_payload.get("label") or spec.label
            actor_payload["model"] = actor_payload.get("model") or spec.default_model
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
            "template": str(payload.get("template") or ""),
        }
        for actor_id, payload in registry.items()
    }
    return json.dumps(ordered, sort_keys=True, separators=(",", ":"))


def _actor_ids_for_state(state: dict[str, str]) -> tuple[str, ...]:
    return tuple(_actor_registry_from_state(state))


def _session_metadata_path(work_dir: Path) -> Path:
    return shared_session_root(session_shared_id(work_dir)) / "session.json"


def _load_session_metadata(work_dir: Path) -> dict[str, object]:
    path = _session_metadata_path(work_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid session metadata: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"invalid session metadata: {path}")
    return payload


def _write_session_metadata(work_dir: Path, payload: dict[str, object]) -> None:
    path = _session_metadata_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = dict(payload)
    cleaned["session_id"] = session_shared_id(work_dir)
    cleaned.setdefault("created_at", datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    cleaned["updated_at"] = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(cleaned, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _actor_registry_from_metadata(work_dir: Path) -> dict[str, dict[str, str]]:
    payload = _load_session_metadata(work_dir)
    registry: dict[str, dict[str, str]] = {}
    raw_actors = payload.get(SESSION_ACTORS_METADATA_KEY)
    if isinstance(raw_actors, dict):
        for actor_id, actor_payload in raw_actors.items():
            normalized = topology.normalize_identity(str(actor_id))
            if not normalized or topology.is_placeholder_identity(normalized):
                continue
            if not isinstance(actor_payload, dict):
                raise SystemExit(f"invalid session actor payload for {normalized}")
            spec = _actor_template_spec(str(actor_payload.get("template") or ""))
            registry[normalized] = {
                "label": str(actor_payload.get("label") or (spec.label if spec else normalized)).strip() or normalized,
                "model": str(actor_payload.get("model") or (spec.default_model if spec else ACTOR_DEFAULT_MODEL)).strip(),
                "resume_uuid": str(actor_payload.get("resume_uuid") or "").strip(),
                "template": str(actor_payload.get("template") or (spec.actor_id if spec else "")).strip(),
            }
    raw_members = payload.get(SESSION_MEMBERS_METADATA_KEY)
    if isinstance(raw_members, list):
        for actor_id in raw_members:
            normalized = topology.normalize_identity(str(actor_id))
            if not normalized or topology.is_placeholder_identity(normalized):
                continue
            registry.setdefault(
                normalized,
                {
                    "label": normalized,
                    "model": ACTOR_DEFAULT_MODEL,
                    "resume_uuid": "",
                    "template": "",
                },
            )
    return registry


def _discovered_actor_registry(work_dir: Path) -> dict[str, dict[str, str]]:
    resolved = work_dir.resolve()
    actors_dir = resolved / "actors"
    if not actors_dir.is_dir():
        actors_dir = shared_session_root(session_shared_id(work_dir)) / "actors"
    registry: dict[str, dict[str, str]] = {}
    if not actors_dir.is_dir():
        return registry
    for actor_dir in sorted(actors_dir.iterdir()):
        if not actor_dir.is_dir():
            continue
        actor_id = topology.normalize_identity(actor_dir.name)
        if not _actor_is_fingerprint(actor_id) or topology.is_placeholder_identity(actor_id):
            continue
        if not session_is_initialized(actor_dir.resolve()):
            continue
        registry[actor_id] = {
            "label": actor_id,
            "model": ACTOR_DEFAULT_MODEL,
            "resume_uuid": "",
            "template": "",
        }
    return registry


def _actor_registry(work_dir: Path) -> dict[str, dict[str, str]]:
    registry = _actor_registry_from_metadata(work_dir)
    discovered_registry = _discovered_actor_registry(work_dir)
    if registry:
        for actor_id, payload in discovered_registry.items():
            registry.setdefault(actor_id, payload)
        return registry
    if discovered_registry:
        return discovered_registry
    state_registry = _actor_registry_from_state(_load_session_state(work_dir))
    if state_registry:
        return state_registry
    if topology.parse_grouped_session_root(work_dir) is not None:
        current = session_identity(work_dir)
    else:
        current = ""
    if current and not topology.is_placeholder_identity(current):
        return {
            current: {
                "label": current,
                "model": ACTOR_DEFAULT_MODEL,
                "resume_uuid": "",
                "template": "",
            }
        }
    return {}


def _store_actor_registry(work_dir: Path, registry: dict[str, dict[str, str]]) -> None:
    metadata = _load_session_metadata(work_dir)
    metadata[SESSION_MEMBERS_METADATA_KEY] = sorted(registry)
    metadata[SESSION_ACTORS_METADATA_KEY] = {
        actor_id: {
            "label": str(payload.get("label") or actor_id),
            "model": str(payload.get("model") or ACTOR_DEFAULT_MODEL),
            "resume_uuid": str(payload.get("resume_uuid") or ""),
            "template": str(payload.get("template") or ""),
        }
        for actor_id, payload in sorted(registry.items())
    }
    _write_session_metadata(work_dir, metadata)


def _resolve_bound_actor_name(work_dir: Path, actor_ref: str, *, kind: str = "actor") -> str:
    normalized = _normalize_actor_name(actor_ref)
    if not normalized:
        raise SystemExit(f"missing {kind}")
    registry = _actor_registry(work_dir)
    if normalized in registry:
        return normalized
    for actor_id, payload in registry.items():
        label = _shared_normalize_actor_name(str(payload.get("label") or ""))
        if label and label == normalized:
            return actor_id
    raise SystemExit(
        f"{normalized} is not bound for this session; bind them first with "
        f"`gotta actor bind {actor_ref}`"
    )


def _new_actor_identity(registry: dict[str, dict[str, str]], *, seed: str = "") -> str:
    candidate = topology.normalize_identity(seed) if seed else ""
    if candidate and _actor_is_fingerprint(candidate) and candidate not in registry:
        return candidate
    while True:
        candidate = session_token(str(uuid.uuid4()).lower())
        if candidate not in registry:
            return candidate


def _bind_actor_identity(session_root: Path, actor_ref: str) -> tuple[str, bool]:
    registry = _actor_registry(session_root)
    normalized = _normalize_actor_name(actor_ref)
    if normalized in registry:
        return normalized, False
    for actor_id, payload in registry.items():
        label = _shared_normalize_actor_name(str(payload.get("label") or ""))
        if label and label == normalized:
            return actor_id, False
    template = _actor_template_spec(actor_ref)
    label = template.label if template is not None else actor_ref.strip() or normalized
    model = template.default_model if template is not None else ACTOR_DEFAULT_MODEL
    resume_uuid = str(uuid.uuid4()).lower()
    actor_id = _new_actor_identity(
        registry,
        seed=normalized if _actor_is_fingerprint(normalized) else "",
    )
    registry[actor_id] = {
        "label": label,
        "model": model,
        "resume_uuid": resume_uuid,
        "template": template.actor_id if template is not None else "",
    }
    _store_actor_registry(session_root, registry)
    return actor_id, True


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
    registry = _actor_registry(work_dir)
    normalized = (
        _resolve_bound_actor_name(work_dir, actor_name)
        if registry
        else _normalize_actor_name(actor_name)
    )
    if not _actor_is_selected(work_dir, normalized):
        return {
            "actor": normalized,
            "label": _actor_label(normalized, work_dir=work_dir),
            "status": "pending",
        }
    path = _actor_state_path(work_dir, normalized)
    if not path.exists():
        return {
            "actor": normalized,
            "label": _actor_label(normalized, work_dir=work_dir),
            "status": "pending",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid actor state file: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"invalid actor state file: {path}")
    data.setdefault("actor", normalized)
    data.setdefault("label", _actor_label(normalized, work_dir=work_dir))
    data["status"] = _normalize_actor_status(data.get("status") or "pending")
    return data


def _write_actor_state(
    work_dir: Path,
    actor_name: str,
    payload: dict[str, object],
) -> Path:
    normalized = (
        _resolve_bound_actor_name(work_dir, actor_name)
        if _actor_registry(work_dir)
        else _normalize_actor_name(actor_name)
    )
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
    merged["label"] = _actor_label(normalized, work_dir=work_dir)
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


def _bootstrap_actor_want(*, actor_name: str, label: str) -> str:
    actor_name = _normalize_actor_name(actor_name)
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
        - Provider-native `search` commands can seed discovery artifacts and
          provider-native `get` / plain `gotta read <locator>` retrieval can land
          evidence artifacts, but only when an initialized session is already
          active or passed explicitly. Sessionless retrieval still works; it just
          does not store artifacts.
        - Do not worry if `session manifest`, `session leads`, `session graph`, or
          `session analyze` are empty before the first stored retrieval; land one
          strong source anchor first, then use those session surfaces to continue.
          `session leads` shows the current best leads first and keeps the
          provenance signals visible so you can choose between materialized,
          native, search-seed, and otherwise promising unmaterialized branches.
        - Rewrite `GOAL.md` in place so it reflects the current moment.
        - Keep `GOAL.md` durable for actor launches and reruns.
        - Launch actors once `WANT.md` and `GOAL.md` are real.
        """
    )


def _bootstrap_actor_goal(*, actor_name: str, label: str, actor_dir: Path, work_dir: Path) -> str:
    actor_name = _normalize_actor_name(actor_name)
    return _finish(
        f"""
        # Seed Actor Goal Placeholder

        > Generated by `gotta actor bind {label}`.
        > Rewrite this file before actor launch.

        This file turns the operator-authored actor `WANT.md` into a concrete
        evidence-collection contract for {label}.

        Bootstrap facts:

        - Actor session root: `{actor_dir}`
        - Actor-local logs and friction: `{actor_dir / 'LOGS.md'}`, `{actor_dir / 'OOPS.md'}`
        - Actor-local checklist: `{actor_dir / 'TODO.md'}`
        - Actor-local notes: `{actor_dir / 'NOTES.md'}`

        Rewrite rule:

        - Read actor-local `WANT.md` first.
        - Turn that charter into concrete evidence-collection steps.
        - Treat `TODO.md` as the live actor-local checklist.
        - Treat `NOTES.md` as the canonical actor-authored narration surface.
        - Treat `LOGS.md` as procedural/system trace rather than the primary narration surface.
        - Append an initial short heartbeat note immediately after launch, even before the first evidence wave.
        - Continue appending short notes during the run; do not wait until the end.
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


def _actor_readme(actor_name: str, label: str) -> str:
    actor_name = _normalize_actor_name(actor_name)
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
        - `NOTES.md`: projected actor-authored narration plus live actor state
        - `LOGS.md`: actor-local procedural/system trace
        - `OOPS.md`: actor-local friction log

        Canonical truth:

        - this session root is real; operate from here with `gotta ...`
        - actor selection is explicit actor selection inside one shared session, not path traversal under `actors/`
        - actor-local checklist truth is `state/todo.jsonl`
        - actor lifecycle truth is `state/actor.json` and `state/actor.jsonl`
        - actor-authored narration truth is `state/notes.jsonl`
        - actor-local procedural/system trace is `state/logs.jsonl`
        - actor-local friction truth is `state/oops.jsonl`
        - shared cross-actor truth is the shared evidence web at `content/`
        - provenance still belongs to the actor that produced the evidence
        - actor-local `WANT.md` and `GOAL.md` are live operator-authored charters, not hidden templates
        - rewrite actor-local charters with `{_actor_charter_command(actor_name, 'want')}` and `{_actor_charter_command(actor_name, 'goal')}` before launch
        - prefer native `gotta` surfaces over shell-side spelunking
        - prefer `gotta notes ...` for actor-authored narration; short notes are valid
        - use `gotta logs ...` for chronology and runtime/system trace, not as the primary narration path
        - do not author the final dossier from this session; stop at evidence and handoff notes
        """
    )


def _seed_actor_surface(
    actor_dir: Path,
    actor_name: str,
    label: str,
    *,
    work_dir: Path,
) -> None:
    actor_dir.mkdir(parents=True, exist_ok=True)
    _seed_file(actor_dir / "README.md", _actor_readme(actor_name, label))
    _seed_file(
        actor_dir / WANT_FILE,
        _bootstrap_actor_want(actor_name=actor_name, label=label),
    )
    _seed_file(
        actor_dir / "GOAL.md",
        _bootstrap_actor_goal(
            actor_name=actor_name,
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
    registry = _actor_registry(work_dir)
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
            text="Append a first short note as soon as the main actor lands a strong anchor, then append another short note after the first substantive evidence wave and before final synthesis/signoff.",
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
        bootstrap_lines.append("Seeded as the procedural/system trace for this session")
        bootstrap_lines.append(
            "Keep actor-authored narration in `NOTES.md`; one-line notes after strong anchors, substantive evidence waves, and final synthesis/signoff are expected."
        )
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
    actors = _actor_registry(session_dir)
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
) -> dict[str, str]:
    dirs = resolve_dirs(
        CommonOptions(session_dir=str(actor_dir), content_dir=str(content_dir)),
        create=True,
    )
    write_session_state(dirs)
    dirs.session_dir.joinpath("bin").mkdir(parents=True, exist_ok=True)
    _ensure_symlink(actor_dir / "content", content_dir)
    session_link = actor_dir / "session"
    if session_link.is_symlink() or session_link.is_file():
        session_link.unlink(missing_ok=True)
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
        text="Append an initial short heartbeat note immediately after the actor is alive, even before the first evidence wave.",
    )
    create_todo_item(
        actor_dir,
        section="Status",
        text="Materialize the first strong source anchor and append a first short note as soon as it lands.",
    )
    create_todo_item(
        actor_dir,
        section="Status",
        text="Append another short note after each material evidence wave or plan change; do not request completion or sign-off with empty NOTES.md.",
    )


def _session_surface_path(work_dir: Path, surface: str) -> Path:
    return work_dir / surface


def _record_session_activity(
    work_dir: Path,
    *,
    plugin: str,
    surface: str,
    action: str,
    actor: str = "",
    target_actor: str = "",
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
    activity_actor = actor.strip() or current_actor(default_actor=session_identity(work_dir))
    payload = {
        "plugin": plugin,
        "surface": surface,
        "action": action,
        "actor": activity_actor,
        "locator": resolved_locator,
        "preferred_name": preferred_name.strip() or resolved_name,
        "follow_command": follow_command.strip() or resolved_follow,
        "detail": detail,
        "time_field": "session_recorded_at",
    }
    normalized_target = _normalize_actor_name(target_actor) if target_actor.strip() else ""
    if normalized_target and normalized_target != activity_actor:
        payload["target_actor"] = normalized_target
    append_activity_event(work_dir, payload)


def _append_actor_event(
    work_dir: Path,
    actor_name: str,
    *,
    event: str,
    detail: str = "",
    extra: dict[str, object] | None = None,
    author: str = "",
) -> None:
    normalized_actor = _normalize_actor_name(actor_name)
    event_author = author.strip() or current_actor(default_actor=normalized_actor)
    payload: dict[str, object] = {
        "timestamp": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actor": normalized_actor,
        "author": event_author,
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
            actor=event_author,
            target_actor=normalized_actor,
            locator=f"actor:{payload['actor']}",
            preferred_name=str(payload["actor"]),
            follow_command=f"gotta actor status {payload['actor']}",
            detail=detail or event,
        )


def _actor_log_line(session_root: Path, actor_name: str, message: str, *, author: str = "") -> None:
    normalized_actor = _normalize_actor_name(actor_name)
    log_author = author.strip() or current_actor(default_actor=normalized_actor)
    if log_author == normalized_actor:
        rendered = f"[{normalized_actor}] {message}"
    else:
        rendered = f"[{log_author} -> {normalized_actor}] {message}"
    append_log_record(session_root, message=rendered, actor=log_author)


def _record_actor_projection_activity(
    session_root: Path,
    *,
    actor_name: str,
    surface: str,
    action: str,
    log_path: Path,
    projection_path: Path,
    detail: str,
    actor: str = "",
) -> None:
    normalized_actor = _normalize_actor_name(actor_name)
    _record_session_activity(
        session_root,
        plugin="actor",
        surface=surface,
        action=action,
        actor=actor.strip() or current_actor(default_actor=normalized_actor),
        target_actor=normalized_actor,
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
    return current == _bootstrap_actor_want(
        actor_name=actor_name,
        label=_actor_label(actor_name, work_dir=work_root),
    ).strip()


def _actor_goal_rewrite_pending(work_root: Path, actor_name: str) -> bool:
    path = _actor_goal_path(work_root, actor_name)
    if not path.is_file():
        return True
    return path.read_text(encoding="utf-8").strip() == _bootstrap_actor_goal(
        actor_name=actor_name,
        label=_actor_label(actor_name, work_dir=work_root),
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


def _actor_activity_summary(
    event: str,
    detail: str,
    *,
    author: str = "",
    target_actor: str = "",
) -> str:
    cleaned_detail = detail.strip()
    author_prefix = ""
    if author and target_actor and author != target_actor:
        author_prefix = f"{author}: "
    if event == "note":
        return (author_prefix + cleaned_detail) if cleaned_detail else (author_prefix + "note").strip()
    label = event.replace("_", " ")
    if cleaned_detail:
        return f"{label}: {author_prefix}{cleaned_detail}".strip()
    return f"{label}: {author_prefix}".strip(": ")


def _actor_event_records(work_dir: Path, actor_name: str) -> list[dict[str, object]]:
    path = _actor_events_path(work_dir, actor_name)
    if not path.exists():
        return []
    normalized_actor = _normalize_actor_name(actor_name)
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
        author = str(payload.get("author") or "").strip()
        if writer_role(work_dir, normalized_actor, writer=author or normalized_actor) == "foreign":
            continue
        events.append(
            {
                "timestamp": timestamp,
                "event": event,
                "author": author,
                "detail": detail,
                "summary": _actor_activity_summary(
                    event,
                    detail,
                    author=author,
                    target_actor=normalized_actor,
                ),
                "_order": index,
            }
        )
    return events


def _actor_recent_activity(work_dir: Path, actor_name: str, *, limit: int = 5) -> dict[str, object]:
    events = _actor_event_records(work_dir, actor_name)
    if not events:
        return {
            "recent_activity": [],
            "recent_lifecycle": [],
            "last_lifecycle_at": "",
            "last_lifecycle_summary": "",
        }
    ordered = sorted(
        events,
        key=lambda item: (
            str(item.get("timestamp") or ""),
            int(item.get("_order") or 0),
        ),
        reverse=True,
    )
    lifecycle = [
        item
        for item in ordered
        if str(item.get("event") or "") != "note"
    ]
    recent_activity = [
        {
            "timestamp": str(item.get("timestamp") or ""),
            "event": str(item.get("event") or ""),
            "author": str(item.get("author") or ""),
            "detail": str(item.get("detail") or ""),
            "summary": str(item.get("summary") or ""),
        }
        for item in ordered[:limit]
    ]
    recent_lifecycle = [
        {
            "timestamp": str(item.get("timestamp") or ""),
            "event": str(item.get("event") or ""),
            "author": str(item.get("author") or ""),
            "detail": str(item.get("detail") or ""),
            "summary": str(item.get("summary") or ""),
        }
        for item in lifecycle[:limit]
    ]
    latest = recent_lifecycle[0] if recent_lifecycle else {}
    return {
        "recent_activity": recent_activity,
        "recent_lifecycle": recent_lifecycle,
        "last_lifecycle_at": str(latest.get("timestamp") or ""),
        "last_lifecycle_summary": str(latest.get("summary") or ""),
    }


def _actor_note_summary(work_dir: Path, actor_name: str) -> dict[str, object]:
    normalized_actor = _normalize_actor_name(actor_name)
    notes: list[dict[str, str]] = []
    for record in visible_actor_notes_records(work_dir, normalized_actor):
        if str(record.get("author") or "").strip() != normalized_actor:
            continue
        timestamp = str(record.get("timestamp") or "").strip()
        message = str(record.get("message") or "").strip()
        if not timestamp or not message:
            continue
        first_line = message.splitlines()[0] if message.splitlines() else message
        notes.append(
            {
                "timestamp": timestamp,
                "summary": first_line.strip() or message,
            }
        )
    ordered = sorted(notes, key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    latest = ordered[0] if ordered else {}
    notes_stale = False
    latest_timestamp = str(latest.get("timestamp") or "")
    if latest_timestamp:
        try:
            latest_dt = datetime.fromisoformat(latest_timestamp.replace("Z", "+00:00"))
        except ValueError:
            latest_dt = None
        if latest_dt is not None:
            notes_stale = (time.time() - latest_dt.timestamp()) > ACTOR_STALL_SECONDS
    return {
        "last_note_at": latest_timestamp,
        "last_note_summary": str(latest.get("summary") or ""),
        "notes_stale": notes_stale,
    }


def _actor_progress_summary(work_dir: Path, actor_name: str, *, limit: int = 5) -> dict[str, object]:
    normalized_actor = _normalize_actor_name(actor_name)
    actor_root = _actor_session_dir(work_dir, normalized_actor)
    events: list[dict[str, object]] = []
    order = 0

    def append_progress_event(
        *,
        timestamp: str,
        event: str,
        detail: str,
        summary: str,
        priority: int,
    ) -> None:
        nonlocal order
        cleaned_timestamp = timestamp.strip()
        cleaned_detail = detail.strip()
        if not cleaned_timestamp or not cleaned_detail:
            return
        events.append(
            {
                "timestamp": cleaned_timestamp,
                "event": event,
                "author": normalized_actor,
                "detail": cleaned_detail,
                "summary": summary.strip() or cleaned_detail,
                "_priority": priority,
                "_order": order,
            }
        )
        order += 1

    for record in visible_actor_notes_records(work_dir, normalized_actor):
        if str(record.get("author") or "").strip() != normalized_actor:
            continue
        message = str(record.get("message") or "").strip()
        append_progress_event(
            timestamp=str(record.get("timestamp") or ""),
            event="note",
            detail=message,
            summary=_actor_activity_summary(
                "note",
                message,
                author=normalized_actor,
                target_actor=normalized_actor,
            ),
            priority=4,
        )

    for record in visible_channel_records(actor_root, OOPS_CHANNEL):
        if str(record.get("actor") or "").strip() != normalized_actor:
            continue
        message = str(record.get("message") or "").strip()
        append_progress_event(
            timestamp=str(record.get("timestamp") or ""),
            event="oops",
            detail=message,
            summary=_actor_activity_summary(
                "oops",
                message,
                author=normalized_actor,
                target_actor=normalized_actor,
            ),
            priority=3,
        )

    manifest_path = work_dir / "content" / "manifest.jsonl"
    if manifest_path.exists():
        for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if str(payload.get("actor") or "").strip() != normalized_actor:
                continue
            locator = str(
                payload.get("canonical_locator") or payload.get("locator") or payload.get("preferred_name") or ""
            ).strip()
            append_progress_event(
                timestamp=str(payload.get("fetched_at") or ""),
                event="evidence",
                detail=locator,
                summary=f"evidence: {locator}",
                priority=2,
            )

    ordered = sorted(
        events,
        key=lambda item: (
            str(item.get("timestamp") or ""),
            int(item.get("_priority") or 0),
            int(item.get("_order") or 0),
        ),
        reverse=True,
    )
    recent_progress = [
        {
            "timestamp": str(item.get("timestamp") or ""),
            "event": str(item.get("event") or ""),
            "author": str(item.get("author") or ""),
            "detail": str(item.get("detail") or ""),
            "summary": str(item.get("summary") or ""),
        }
        for item in ordered[:limit]
    ]
    latest = recent_progress[0] if recent_progress else {}
    progress_kind = (
        "evidence"
        if any(str(item.get("event") or "") == "evidence" for item in ordered)
        else "narration" if ordered else "none"
    )
    progress_stale = False
    latest_timestamp = str(latest.get("timestamp") or "")
    if latest_timestamp:
        try:
            latest_dt = datetime.fromisoformat(latest_timestamp.replace("Z", "+00:00"))
        except ValueError:
            latest_dt = None
        if latest_dt is not None:
            progress_stale = (time.time() - latest_dt.timestamp()) > ACTOR_STALL_SECONDS
    return {
        "recent_progress": recent_progress,
        "last_activity_at": str(latest.get("timestamp") or ""),
        "last_activity_summary": str(latest.get("summary") or ""),
        "progress_kind": progress_kind,
        "progress_stale": progress_stale,
    }


def _actor_status_payload(work_dir: Path, actor_name: str) -> dict[str, object]:
    actor_name = _resolve_bound_actor_name(work_dir, actor_name)
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
    voice = actor_voice(work_dir, _normalize_actor_name(actor_name))
    notes_ready = voice == "present"
    evidence = _actor_evidence_summary(work_dir, actor_name)
    evidence_note = _actor_evidence_note(evidence)
    recent_activity = _actor_recent_activity(work_dir, actor_name)
    note_summary = _actor_note_summary(work_dir, actor_name)
    progress = _actor_progress_summary(work_dir, actor_name)
    lifecycle_entries = [dict(item) for item in recent_activity.get("recent_lifecycle", [])]
    if lifecycle_entries and str(lifecycle_entries[0].get("event") or "") == "runtime_exit":
        lifecycle_detail = str(lifecycle_entries[0].get("detail") or "")
        request_labels = {
            "stop_requested": "graceful stop request",
            "failed_requested": "failure request",
            "signoff_requested": "sign-off request",
            "complete_requested": "completion request",
        }
        honored = next(
            (
                request_labels.get(str(item.get("event") or ""))
                for item in lifecycle_entries[1:]
                if str(item.get("event") or "") in request_labels
            ),
            "",
        )
        if honored and "code 0" in lifecycle_detail:
            lifecycle_entries[0]["summary"] = (
                f"runtime exit: actor process exited cleanly after honoring {honored}"
            )
    if lifecycle_entries:
        recent_activity["recent_lifecycle"] = lifecycle_entries
        recent_activity["last_lifecycle_at"] = str(lifecycle_entries[0].get("timestamp") or "")
        recent_activity["last_lifecycle_summary"] = str(
            lifecycle_entries[0].get("summary") or ""
        )
    evidence_live = int(evidence["artifact_count"]) > 0
    if signoff_at:
        derived_status = "signed_off"
    notes_status = (
        "present"
        if actor_notes_ready(work_dir, _normalize_actor_name(actor_name))
        else "empty" if notes_path.exists() else "missing"
    )
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
    if requested_status and derived_status in {"starting", "active", "producing_evidence"}:
        derived_status = "closing"
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
                    + " The actor should wind down, append one final short note, and sign off."
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
    voice_missing = voice == "missing"
    voice_setup = voice == "setup"
    voice_pulse = voice == "pulse"
    progress_stale = bool(progress.get("progress_stale"))
    last_note_at = str(note_summary.get("last_note_at") or "")
    last_artifact_at = str(evidence.get("last_artifact_at") or "")
    needs_note_refresh = bool(
        evidence_live and last_artifact_at and (not last_note_at or last_artifact_at > last_note_at)
    )
    low_signal_progress = (
        bool(runtime_live)
        and progress_stale
        and int(evidence.get("artifact_count") or 0) == 0
    )
    if derived_status == "closing":
        if notes_ready and needs_note_refresh:
            next_step = (
                "actor close-out is pending while the runtime is still live, and new "
                "actor-attributed evidence landed after the last short note. "
                + (evidence_note + " " if evidence_note else "")
                + "Land one final short note now so the close-out reflects the latest evidence, "
                "then wait for runtime exit before treating the terminal disposition as authoritative."
                + request_note
                + runtime_note
            )
        elif notes_ready:
            next_step = (
                "actor close-out is pending while the runtime is still live. "
                + (evidence_note + " " if evidence_note else "")
                + "Let the actor finish the current wave, then wait for runtime exit before "
                "treating the terminal disposition as authoritative."
                + request_note
                + runtime_note
            )
        elif voice_pulse:
            next_step = (
                "actor close-out is pending while the runtime is still live. Non-note signal is "
                "already landing through friction or shared evidence, but the final short actor "
                "note is still missing. "
                + (evidence_note + " " if evidence_note else "")
                + "Land one final short actor-authored note before runtime exit so the close-out "
                "has durable voice, then recheck actor status."
                + request_note
                + runtime_note
            )
        elif voice_setup:
            next_step = (
                "actor close-out is pending while the runtime is still live. Setup note is present, "
                "but actor voice is still missing. "
                + (evidence_note + " " if evidence_note else "")
                + "Land one final short actor-authored note before runtime exit so the close-out "
                "has real voice, then recheck actor status."
                + request_note
                + runtime_note
            )
        else:
            next_step = (
                "actor close-out is pending while the runtime is still live, but actor voice is "
                "still missing. "
                + (evidence_note + " " if evidence_note else "")
                + "Land one final short actor-authored note before runtime exit so the close-out "
                "has voice, then recheck actor status."
                + request_note
                + runtime_note
            )
    elif derived_status == "producing_evidence":
        if notes_ready and needs_note_refresh:
            next_step = (
                "actor is still active and producing evidence artifacts, and new evidence landed "
                "after the last short note. "
                + (evidence_note + " " if evidence_note else "")
                + "Land a short note now so the latest evidence wave has durable actor narration, "
                f"then recheck `gotta actor status {_normalize_actor_name(actor_name)}` shortly."
                + request_note
                + runtime_note
            )
        elif notes_ready:
            next_step = (
                "actor is still active and producing evidence artifacts. "
                + (evidence_note + " " if evidence_note else "")
                + "Use NOTES.md for live actor visibility; recheck `gotta actor status "
                f"{_normalize_actor_name(actor_name)}` shortly before closing the actor out."
                + request_note
                + runtime_note
            )
        elif voice_pulse:
            next_step = (
                "actor is still active and producing evidence artifacts. Non-note signal is "
                "already present through friction or shared evidence, but the first short actor "
                "note has not landed yet. "
                + (evidence_note + " " if evidence_note else "")
                + "Let the current evidence wave finish, then append a short actor-authored note "
                "before requesting completion or sign-off."
                + request_note
                + runtime_note
            )
        elif voice_setup:
            next_step = (
                "actor is still active and producing evidence artifacts. Setup note is present, "
                "but actor voice has not landed yet. "
                + (evidence_note + " " if evidence_note else "")
                + "Append a short actor-authored note before requesting completion or sign-off, "
                f"then recheck `gotta actor status {_normalize_actor_name(actor_name)}` shortly."
                + request_note
                + runtime_note
            )
        else:
            next_step = (
                "actor is still active and producing evidence artifacts, but actor voice is still "
                "missing. "
                + (evidence_note + " " if evidence_note else "")
                + "Append a short actor-authored note before requesting completion or sign-off, "
                f"then recheck `gotta actor status {_normalize_actor_name(actor_name)}` shortly."
                + request_note
                + runtime_note
            )
    elif derived_status in {"starting", "active"} and notes_ready and needs_note_refresh:
        next_step = (
            "actor is still active and new actor-attributed evidence landed after the last short "
            "note. "
            + (evidence_note + " " if evidence_note else "")
            + "Land a short note now so the current evidence wave is narrated before close-out."
            + request_note
            + runtime_note
        )
    elif derived_status in {"starting", "active"} and notes_ready:
        next_step = (
            "actor is still active and actor voice is present. "
            "Use NOTES.md for live actor visibility; recheck `gotta actor status "
            f"{_normalize_actor_name(actor_name)}` shortly before closing the actor out."
            + request_note
            + runtime_note
        )
    elif derived_status in {"starting", "active"} and voice_pulse:
        next_step = (
            "actor is live and non-note signal is already landing through friction or shared "
            "evidence, but the first short actor note has not landed yet. Give the runtime a brief "
            "window to turn that signal into a short note before treating this as a "
            "visibility failure."
            + request_note
            + runtime_note
        )
    elif derived_status in {"starting", "active"} and voice_setup:
        next_step = (
            "setup note is present, but actor voice is still missing. Give the runtime a brief "
            "startup window to land the first short actor-authored note before treating this as a "
            "visibility failure. If actor voice is still missing after one heartbeat interval or "
            "after the first materialized artifact, intervene and recheck actor status."
            + request_note
            + runtime_note
        )
    elif derived_status in {"starting", "active"}:
        next_step = (
            "actor is live, but actor voice is still missing. Give the runtime a brief startup "
            "window to land the first short actor-authored note before treating this as a "
            "visibility failure. If actor voice is still missing after one heartbeat interval or "
            "after the first materialized artifact, intervene and recheck actor status."
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
    elif derived_status == "stalled" and (not voice_missing or evidence_live):
        next_step = (
            "actor heartbeat is stale, but material actor state already exists in NOTES.md or the "
            "shared evidence web. "
            + (evidence_note + " " if evidence_note else "")
            + "Inspect the notes and decide whether to wait, relaunch, or disposition manually."
            + request_note
        )
    elif derived_status == "completed" and (notes_ready or evidence_live):
        if notes_ready and needs_note_refresh:
            next_step = (
                "actor run is complete, but new actor-attributed evidence landed after the last "
                "short note. Land one short note now, then record durable sign-off intentionally."
            )
        elif notes_ready:
            next_step = (
                "actor run is complete; inspect NOTES.md plus the shared evidence web, then record "
                "durable sign-off with "
                f"`gotta actor signoff {_normalize_actor_name(actor_name)} --summary ...`."
            )
        elif voice_pulse:
            next_step = (
                "actor run is complete and non-note signal landed through friction or shared "
                "evidence, but the final short note is still missing. Add one short actor-authored "
                "note now, then sign off intentionally."
            )
        else:
            next_step = (
                "actor run is complete and evidence landed, but actor voice is still missing. Wait "
                "for a short actor-authored note or sign off intentionally only if you are "
                "explicitly accepting an evidence-only actor contribution."
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
    elif derived_status in {"pending", "bound"} and notes_ready and evidence_live and needs_note_refresh:
        next_step = (
            "actor already has actor-authored narration and shared evidence, but new evidence "
            "landed after the last short note. "
            + (evidence_note + " " if evidence_note else "")
            + "Land a short note now, then keep landing short notes after each substantive "
            "evidence wave so review, handoff, and session-wide inspection surfaces stay current."
        )
    elif derived_status in {"pending", "bound"} and notes_ready and evidence_live:
        next_step = (
            "actor already has actor-authored narration and shared evidence without an active runtime. "
            + (evidence_note + " " if evidence_note else "")
            + "Keep landing notes as the session evolves; when this actor's contribution is "
            "materially complete, record the authoritative close-out intentionally with "
            f"`gotta actor signoff {_normalize_actor_name(actor_name)} --summary ...`."
        )
    elif derived_status in {"pending", "bound"} and voice_pulse:
        next_step = (
            "non-note signal is present through friction or shared evidence, but no short actor "
            "note has landed yet. "
            + (evidence_note + " " if evidence_note else "")
            + "Land one short actor-authored note now, then keep landing short notes after each "
            "material evidence wave so review, handoff, and session-wide inspection surfaces have "
            "continuous actor voice."
        )
    elif derived_status in {"pending", "bound"} and notes_ready:
        next_step = (
            "actor already has actor-authored narration but no shared evidence artifacts yet. "
            "Continue retrieval if more evidence should land, or close out intentionally once "
            f"the narrative is complete with `gotta actor signoff {_normalize_actor_name(actor_name)} --summary ...`."
        )
    elif derived_status in {"pending", "bound"} and voice_setup:
        next_step = (
            "setup note is present, but actor voice has not landed yet. Continue retrieval until the "
            "actor writes a short note, or close this branch out intentionally only if setup-only "
            "state is truly sufficient."
        )
    elif evidence_live and not notes_ready:
        next_step = (
            "actor-attributed evidence is already live in the shared session web, but actor voice is "
            "still missing. "
            + (evidence_note + " " if evidence_note else "")
            + "Land one short actor-authored note now, then keep landing short notes as the story "
            "moves so review, handoff, and session-wide inspection surfaces have actor voice "
            "instead of evidence-only state."
        )
    else:
        next_step = ""
    if low_signal_progress:
        next_step = (
            "actor runtime is still live, but actor-authored progress is stale and no "
            "actor-attributed evidence has landed yet. Treat this as a low-signal run until "
            "fresh actor-authored progress or evidence appears."
            + request_note
            + runtime_note
        )
    return {
        **state,
        "label": _actor_label(actor_name, work_dir=work_dir),
        "status": derived_status,
        "state_path": str(_actor_state_path(work_dir, actor_name)),
        "events_path": str(_actor_events_path(work_dir, actor_name)),
        "actor_dir": str(actor_dir),
        "notes_path": str(notes_path),
        "notes_status": notes_status,
        "notes_ready": notes_ready,
        "voice": voice,
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
        **note_summary,
        **progress,
        **recent_activity,
        **evidence,
    }


def _ensure_actor_todo_items(work_dir: Path, actor_name: str) -> None:
    actor = _normalize_actor_name(actor_name)
    label = _actor_label(actor, work_dir=work_dir)
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
        label=_actor_label(actor, work_dir=work_dir),
        status_payload=_actor_status_payload(work_dir, actor),
    )


def _actor_launch_command(work_dir: Path, actor_name: str) -> str:
    return f"gotta actor launch {actor_name} --session {sh_quote(str(work_dir))}"


def _ensure_actor_surface(work_dir: Path, actor_name: str) -> Path:
    actor_name = _resolve_bound_actor_name(work_dir, actor_name)
    state = _load_session_state(work_dir)
    actors = _actor_registry(work_dir)
    actor = actors.get(actor_name)
    if actor is None:
        raise SystemExit(f"unknown actor: {actor_name}")
    repo_raw = str(state.get(SESSION_REPO_ENV) or "").strip()
    actor_dir = actor_session_root(work_dir, actor_name)
    bin_path = work_dir / "bin" / actor_name
    shared_content_dir = shared_session_root(session_shared_id(work_dir)) / "content"
    if actor_dir != work_dir:
        if actor_dir.exists() and not _actor_is_selected(work_dir, actor_name):
            _reset_orphaned_actor_surface(actor_dir)
        _seed_actor_surface(
            actor_dir,
            actor_name,
            _actor_label(actor_name, work_dir=work_dir),
            work_dir=work_dir,
        )
        _copy_if_present(work_dir / "AGENTS.md", actor_dir / "AGENTS.md")
        _copy_if_present(work_dir / "VOICE.md", actor_dir / "VOICE.md")
        _ensure_actor_session_exports(
            actor_dir,
            content_dir=shared_content_dir,
        )
        _ensure_actor_parent_links(work_dir, actor_name, actor_dir)
    else:
        _seed_file(
            actor_dir / "README.md",
            _actor_readme(actor_name, _actor_label(actor_name, work_dir=work_dir)),
        )
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
    actor, created = _bind_actor_identity(session_root, actor_name)
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
            f"; not launched. Rewrite `{actor_want}` and `{actor_goal}` for {_actor_label(actor, work_dir=session_root)} with `{want_cmd}` and `{goal_cmd}` first. "
            f"`{actor_todo}` is already seeded with a minimal actor-local checklist and you may extend it before launch if useful, "
            f"then launch with `{launch_cmd}` when you actually want {_actor_label(actor, work_dir=session_root)} to start"
        )
    else:
        suffix = (
            f"; not launched. `{actor_want}` and `{actor_goal}` are already real. "
            f"`{actor_todo}` is already seeded with a minimal actor-local checklist and you may extend it before launch if useful, "
            f"then launch with `{launch_cmd}` when you actually want {_actor_label(actor, work_dir=session_root)} to start"
        )
    if already_bound:
        return f"{actor} ({_actor_label(actor, work_dir=session_root)}) already bound{suffix}"
    _append_actor_event(session_root, actor, event="bound", detail="bound actor session")
    _actor_log_line(session_root, actor, "bound session")
    created_note = " [new actor]" if created else ""
    return (
        f"bound {actor} ({_actor_label(actor, work_dir=session_root)}) session, seeded actor-local WANT/GOAL placeholders, seeded actor-local TODO, launch shim, actor-local notes/logs/oops surfaces, and shared evidence access{created_note}"
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
    if stdin_has_meaningful_text():
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
    elif stdin_has_meaningful_text():
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
