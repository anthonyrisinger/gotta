"""Shared content-store helpers for gotta."""

from __future__ import annotations

from dataclasses import dataclass
import array
import hashlib
import io
import json
import os
from pathlib import Path
import re
import select
import shlex
import sys
import tempfile
from typing import Any

try:
    import fcntl
    import termios
except ImportError:  # pragma: no cover - platform-specific fallback
    fcntl = None
    termios = None

from gotta.compat import UTC, datetime
from gotta.actors import resolve_actor_context
from gotta import topology

SESSION_ENV = "GOTTA_SESSION_DIR"
SESSION_ID_ENV = "GOTTA_SESSION_ID"
CONTENT_ENV = "GOTTA_SESSION_CONTENT_DIR"
STATE_DIR_ENV = "GOTTA_SESSION_STATE_DIR"
SESSION_ACTOR_ENV = "GOTTA_SESSION_ACTOR"
SESSION_REPO_ENV = "GOTTA_SESSION_REPO"
SESSION_CREATED_ENV = "GOTTA_SESSION_CREATED"
SESSION_ACTIVATION_ENV = "GOTTA_SESSION_ACTIVATION"
CONTEXT_ACTIVE_ENV = "GOTTA_CONTEXT_ACTIVE"
CONTEXT_ID_ENV = "GOTTA_CONTEXT_ID"
CONTEXT_SOURCE_ENV = "GOTTA_CONTEXT_SOURCE"
SESSION_INITIALIZED_ENV = "GOTTA_SESSION_INITIALIZED"
ACTOR_ID_ENV = "GOTTA_ACTOR_ID"
ACTOR_LABEL_ENV = "GOTTA_ACTOR_LABEL"

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
DEFAULT_SESSION_ROOT = topology.DEFAULT_SESSIONS_ROOT
DEFAULT_BINDINGS_ROOT = topology.DEFAULT_BINDINGS_ROOT
ACTIVITY_LOG_NAME = "activity.jsonl"


class ContentError(RuntimeError):
    """Raised when the shared content contract cannot be satisfied."""


@dataclass(frozen=True)
class CommonOptions:
    session_dir: str | None = None
    content_dir: str | None = None
    session_id: str | None = None
    actor: str | None = None
    save_as: str | None = None


@dataclass(frozen=True)
class ResolvedDirs:
    session_dir: Path
    content_dir: Path


@dataclass(frozen=True)
class ContextBinding:
    context_id: str
    context_source: str
    binding_id: str


@dataclass(frozen=True)
class Materialization:
    content_dir: Path
    data_path: Path
    meta_path: Path
    names_dir: Path
    logs_dir: Path
    name_link: Path
    fetch_link: Path
    digest: str
    artifact_kind: str


@dataclass(frozen=True)
class ContentEvent:
    timestamp: str
    link_name: str
    link_path: Path
    log_path: Path


@dataclass(frozen=True)
class ContentSnapshot:
    digest: str
    content_dir: Path
    data_path: Path
    meta_path: Path | None
    names_dir: Path
    logs_dir: Path
    names: list[str]
    events: list[ContentEvent]
    metadata: dict[str, Any]


def _append_jsonl_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _append_manifest(dirs: ResolvedDirs, entry: dict[str, Any]) -> None:
    _append_jsonl_line(dirs.content_dir / "manifest.jsonl", entry)


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def iso_utc(dt: datetime | None = None) -> str:
    value = dt or utc_now()
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def sanitize_name(name: str) -> str:
    stripped = name.strip().replace("/", "-").replace("\\", "-")
    stripped = _SANITIZE_RE.sub("-", stripped)
    stripped = stripped.strip("-.")
    return stripped or "blob"


def content_locator(digest: str) -> str:
    return f"content:{digest.strip()}"


def artifact_locator(preferred_name: str, digest: str) -> str:
    return f"artifact:{sanitize_name(preferred_name)}@{digest.strip()[:12]}"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(data)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def write_text_atomic(path: Path, text: str) -> Path:
    _write_atomic(path, text.encode("utf-8"))
    return path


def _write_text_if_changed(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return path
        except OSError:
            pass
    path.write_text(text, encoding="utf-8")
    return path


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_dir_path(root: Path) -> Path:
    return root / "state"


def activity_log_path(root: Path) -> Path:
    return state_dir_path(root) / ACTIVITY_LOG_NAME


def current_context_binding() -> ContextBinding:
    explicit = os.environ.get(CONTEXT_ID_ENV, "").strip()
    if explicit:
        source = os.environ.get(CONTEXT_SOURCE_ENV, "").strip() or "explicit"
        return ContextBinding(
            context_id=explicit,
            context_source=source,
            binding_id=session_token(explicit),
        )
    codex_thread = os.environ.get("CODEX_THREAD_ID", "").strip()
    if codex_thread:
        return ContextBinding(
            context_id=codex_thread,
            context_source="codex_thread",
            binding_id=session_token(codex_thread),
        )
    term_session = os.environ.get("TERM_SESSION_ID", "").strip()
    if term_session:
        return ContextBinding(
            context_id=term_session,
            context_source="terminal_session",
            binding_id=session_token(term_session),
        )
    values = [
        os.environ.get("TTY", "").strip(),
        os.environ.get("SHELL", "").strip(),
        os.environ.get("TERM_PROGRAM", "").strip(),
        os.environ.get("TERM", "").strip(),
        os.environ.get("COPILOT_LOADER_PID", "").strip(),
        os.environ.get("COPILOT_CLI_BINARY_VERSION", "").strip(),
    ]
    if not values[0]:
        values.append(os.getcwd())
    payload = "\n".join(values)
    context_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return ContextBinding(
        context_id=context_id,
        context_source="terminal_fingerprint",
        binding_id=session_token(context_id),
    )


def session_token(context_id: str) -> str:
    return hashlib.sha256(context_id.encode("utf-8")).hexdigest()[:12]


def default_session_id(context_id: str) -> str:
    return session_token(context_id)


def context_bound_session_root() -> Path | None:
    binding = current_context_binding()
    root = topology.resolve_binding(binding.binding_id)
    if root is None:
        legacy = (
            DEFAULT_SESSION_ROOT.expanduser().resolve()
            / binding.binding_id
            / "actors"
            / binding.binding_id
        )
        if session_is_initialized(legacy):
            return legacy
        return None
    if session_is_initialized(root):
        return root
    return None


def current_actor(*, default_actor: str = "") -> str:
    fallback = (
        os.environ.get(ACTOR_ID_ENV, "").strip()
        or default_actor.strip()
        or os.environ.get(SESSION_ACTOR_ENV, "").strip()
        or current_context_binding().binding_id
    )
    speaker = resolve_actor_context(default_speaker=fallback).speaker
    normalized = topology.normalize_identity(str(speaker or fallback).strip())
    if normalized and not topology.is_placeholder_identity(normalized):
        return normalized
    fallback_normalized = topology.normalize_identity(fallback)
    if fallback_normalized and not topology.is_placeholder_identity(fallback_normalized):
        return fallback_normalized
    return current_context_binding().binding_id


def _current_actor() -> str:
    return current_actor()


def append_activity_event(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    event = dict(payload)
    event.setdefault("timestamp", iso_utc())
    event.setdefault("actor", current_actor(default_actor=session_identity(root)))
    _append_jsonl_line(activity_log_path(root), event)
    return event


def activity_events(root: Path) -> list[dict[str, Any]]:
    path = activity_log_path(root)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def state_env_path(root: Path) -> Path:
    return state_dir_path(root) / "env"


def load_state_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        try:
            parts = shlex.split(line, posix=True)
        except ValueError:
            continue
        token = parts[0] if parts else line
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        result[key] = value
    return result


def load_state_env_at_root(root: Path) -> dict[str, str]:
    return load_state_env(state_env_path(root))


def _nearest_session_root(start: Path) -> Path | None:
    resolved = start.expanduser().resolve()
    for parent in (resolved, *resolved.parents):
        if session_is_initialized(parent):
            return parent
    return None


def discover_state_env(*, include_context_session: bool = True) -> dict[str, str]:
    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        data = load_state_env_at_root(parent)
        if data:
            return data
    if include_context_session:
        context_root = context_bound_session_root()
        if context_root is not None:
            return load_state_env_at_root(context_root)
    return {}


def write_state_env(dirs: ResolvedDirs) -> Path:
    path = state_env_path(dirs.session_dir)
    lines = export_env_lines(dirs)
    return _write_text_if_changed(path, "\n".join(lines) + "\n")


def write_session_state(
    dirs: ResolvedDirs,
    updates: dict[str, str] | None = None,
) -> Path:
    existing = {
        key: value
        for key, value in load_state_env_at_root(dirs.session_dir).items()
        if key not in {SESSION_ACTIVATION_ENV, CONTEXT_ID_ENV, CONTEXT_SOURCE_ENV}
    }
    current_session_id = session_id(dirs.session_dir)
    identity = session_identity(dirs.session_dir)
    merged = {
        **existing,
        **env_mapping(dirs),
        SESSION_ID_ENV: current_session_id,
        SESSION_ACTOR_ENV: identity,
    }
    if updates:
        merged.update({key: value for key, value in updates.items() if value is not None})
    ordered = [
        SESSION_ENV,
        SESSION_ID_ENV,
        CONTENT_ENV,
        STATE_DIR_ENV,
        SESSION_ACTOR_ENV,
        SESSION_REPO_ENV,
        SESSION_CREATED_ENV,
        SESSION_INITIALIZED_ENV,
    ]
    extras = [key for key in merged if key not in ordered]
    lines: list[str] = []
    for key in ordered + sorted(extras):
        value = merged.get(key)
        if value is None or value == "":
            continue
        lines.append(f"export {key}={sh_quote(str(value))}")
    path = state_env_path(dirs.session_dir)
    return _write_text_if_changed(path, "\n".join(lines) + "\n")


def session_is_initialized(root: Path) -> bool:
    if topology.parse_shared_session_root(root) is not None:
        return False
    return state_env_path(root).exists()
def session_id(root: Path) -> str:
    state = load_state_env_at_root(root)
    explicit = str(state.get(SESSION_ID_ENV) or "").strip()
    if explicit:
        return explicit
    return topology.shared_session_id(root)


def resolve_session_root_by_id(session_ref: str) -> Path | None:
    root = topology.resolve_session_root_by_id(session_ref)
    if root is None:
        return None
    if session_is_initialized(root):
        return root.resolve()
    return root.resolve()


def resolve_session_reference(
    raw: str,
    *,
    identity: str | None = None,
    allow_missing: bool = False,
) -> Path | None:
    normalized = raw.strip()
    if not normalized:
        return None
    candidate = Path(normalized).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        shared_id = topology.parse_shared_session_root(resolved)
        if shared_id is not None and identity:
            return topology.session_root_for(shared_id, identity).resolve()
        return resolved
    by_id = resolve_session_root_by_id(normalized)
    if by_id is not None:
        shared_id = topology.parse_shared_session_root(by_id)
        if shared_id is not None and identity:
            return topology.session_root_for(shared_id, identity).resolve()
        return by_id
    if "/" in normalized:
        session_id, session_identity = normalized.split("/", 1)
        root = topology.session_root_for(session_id, session_identity)
        if allow_missing or root.exists() or root.is_symlink():
            return root.resolve()
    if identity:
        shared_root = topology.shared_session_root_for(normalized)
        if allow_missing or shared_root.exists() or shared_root.is_symlink():
            return topology.session_root_for(normalized, identity).resolve()
    return None


def bound_session_root(*, include_context_session: bool = True) -> Path | None:
    discovered = discover_state_env(include_context_session=include_context_session)
    identity_raw = (
        os.environ.get(ACTOR_ID_ENV, "").strip()
        or os.environ.get(SESSION_ACTOR_ENV, "").strip()
        or str(discovered.get(SESSION_ACTOR_ENV) or "").strip()
    )
    identity = (
        topology.normalize_identity(identity_raw)
        if identity_raw and not topology.is_placeholder_identity(identity_raw)
        else ""
    )
    explicit = os.environ.get(SESSION_ENV, "").strip()
    if explicit:
        candidate = resolve_session_reference(explicit, identity=identity, allow_missing=False)
        if candidate is not None and session_is_initialized(candidate):
            return candidate
    discovered_root = str(discovered.get(SESSION_ENV) or "").strip()
    if discovered_root:
        candidate = resolve_session_reference(
            discovered_root,
            identity=identity,
            allow_missing=False,
        )
        if candidate is not None and session_is_initialized(candidate):
            return candidate
    for raw_id in (
        os.environ.get(SESSION_ID_ENV, "").strip(),
        str(discovered.get(SESSION_ID_ENV) or "").strip(),
    ):
        if not raw_id:
            continue
        if identity:
            sibling = topology.session_root_for(raw_id, identity)
            if session_is_initialized(sibling):
                return sibling
        candidate = resolve_session_root_by_id(raw_id)
        if candidate is not None and session_is_initialized(candidate):
            return candidate
    if include_context_session:
        return context_bound_session_root()
    return None


def resolve_dirs(options: CommonOptions, *, create: bool) -> ResolvedDirs:
    discovered = discover_state_env()
    explicit_session = bool(options.session_dir)
    session_raw = (
        options.session_dir
        or os.environ.get(SESSION_ENV, "").strip()
        or discovered.get(SESSION_ENV, "").strip()
    )
    content_raw = (
        options.content_dir
        or ("" if explicit_session else os.environ.get(CONTENT_ENV, "").strip())
        or ("" if explicit_session else discovered.get(CONTENT_ENV, "").strip())
    )
    session_id_raw = (
        options.session_id
        or os.environ.get(SESSION_ID_ENV, "").strip()
        or discovered.get(SESSION_ID_ENV, "").strip()
    )
    identity_raw = (
        options.actor
        or os.environ.get(ACTOR_ID_ENV, "").strip()
        or os.environ.get(SESSION_ACTOR_ENV, "").strip()
        or discovered.get(SESSION_ACTOR_ENV, "").strip()
    )
    if not identity_raw or topology.is_placeholder_identity(identity_raw):
        identity_raw = current_context_binding().binding_id

    session: Path | None = (
        resolve_session_reference(
            session_raw,
            identity=identity_raw,
            allow_missing=create,
        )
        if session_raw
        else None
    )
    if session_raw and session is None:
        session_id = topology.normalize_session_id(session_raw)
        identity = topology.normalize_identity(identity_raw)
        session = topology.session_root_for(session_id, identity)
        if create:
            shared_session_root(session_id).mkdir(parents=True, exist_ok=True)
        content_raw = str(shared_session_root(session_id) / "content")
    if session is None and session_id_raw:
        session_id = topology.normalize_session_id(session_id_raw)
        identity = topology.normalize_identity(identity_raw)
        session = topology.session_root_for(session_id, identity)
        if create:
            shared_session_root(session_id).mkdir(parents=True, exist_ok=True)
        content_raw = str(shared_session_root(session_id) / "content")
    if not content_raw and session is not None:
        parsed = topology.parse_grouped_session_root(session)
        if parsed is not None:
            content_raw = str(shared_session_root(parsed[0]) / "content")
    content: Path | None = Path(content_raw).expanduser() if content_raw else None

    if session is None and content is not None:
        session = content.parent
    if session is not None:
        content = content or (session / "content")

    if session is None or content is None:
        raise ContentError(
            "missing shared content context; gotta needs a session root and content root. "
            "Set GOTTA_SESSION_DIR / GOTTA_SESSION_CONTENT_DIR, pass --session/--content-dir, "
            "or use `gotta ...` so gotta can bind or create the correct session "
            "for you. For low-level manual bootstrap, use `gotta session init \"$WS\"`."
        )

    if create:
        session = _ensure_dir(session.resolve())
        content = _ensure_dir(content.resolve())
    else:
        session = session.resolve()
        content = content.resolve()

    return ResolvedDirs(session, content)


def export_env_lines(dirs: ResolvedDirs) -> list[str]:
    return [
        f"export {SESSION_ENV}={sh_quote(str(dirs.session_dir))}",
        f"export {SESSION_ID_ENV}={sh_quote(session_id(dirs.session_dir))}",
        f"export {CONTENT_ENV}={sh_quote(str(dirs.content_dir))}",
        f"export {STATE_DIR_ENV}={sh_quote(str(state_dir_path(dirs.session_dir)))}",
        f"export {SESSION_ACTOR_ENV}={sh_quote(session_identity(dirs.session_dir))}",
    ]


def env_mapping(dirs: ResolvedDirs) -> dict[str, str]:
    return {
        SESSION_ENV: str(dirs.session_dir),
        SESSION_ID_ENV: session_id(dirs.session_dir),
        CONTENT_ENV: str(dirs.content_dir),
        STATE_DIR_ENV: str(state_dir_path(dirs.session_dir)),
        SESSION_ACTOR_ENV: session_identity(dirs.session_dir),
    }


def shared_session_root(session_id: str) -> Path:
    return topology.shared_session_root_for(session_id)


def session_shared_id(root: Path) -> str:
    state = load_state_env_at_root(root)
    explicit = str(state.get(SESSION_ID_ENV) or "").strip()
    if explicit:
        return topology.normalize_session_id(explicit)
    return topology.shared_session_id(root)


def session_identity(root: Path) -> str:
    state = load_state_env_at_root(root)
    explicit = str(state.get(SESSION_ACTOR_ENV) or state.get("GOTTA_SESSION_ACTOR") or "").strip()
    if explicit and not topology.is_placeholder_identity(explicit):
        return topology.normalize_identity(explicit)
    derived = topology.session_identity(root)
    if derived and not topology.is_placeholder_identity(derived):
        return derived
    resolved = root.expanduser().resolve()
    if topology.parse_shared_session_root(resolved) is not None:
        return _current_actor()
    fallback = topology.normalize_identity(resolved.name)
    if topology.is_placeholder_identity(fallback):
        return current_context_binding().binding_id
    return fallback


def session_surface_initialized(root: Path) -> bool:
    state = load_state_env_at_root(root)
    if state.get(SESSION_INITIALIZED_ENV, "").strip() == "1":
        return True
    required = ("WANT.md", "TODO.md", "LOGS.md", "GOAL.md", "OOPS.md")
    return all((root / name).exists() for name in required)


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def stdin_has_meaningful_text() -> bool:
    stream = sys.stdin
    try:
        if stream.isatty():
            return False
    except Exception:
        return False
    if hasattr(stream, "getvalue"):
        try:
            value = stream.getvalue()
            if not isinstance(value, str):
                return bool(value)
            cursor = stream.tell() if hasattr(stream, "tell") else 0
            return len(value) > int(cursor)
        except Exception:
            return False
    try:
        fileno = stream.fileno()
    except (AttributeError, OSError, io.UnsupportedOperation):
        return False
    try:
        readable, _, _ = select.select([fileno], [], [], 0)
    except (OSError, ValueError):
        return False
    if not readable:
        return False
    buffer = getattr(stream, "buffer", None)
    if buffer is not None and hasattr(buffer, "peek"):
        try:
            return bool(buffer.peek(1))
        except Exception:
            pass
    if fcntl is not None and termios is not None:
        try:
            available = array.array("i", [0])
            fcntl.ioctl(fileno, termios.FIONREAD, available, True)
            return bool(available[0])
        except Exception:
            pass
    return False


def stdin_has_readable_text() -> bool:
    return stdin_has_meaningful_text()


def session_member_path(root: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        raise ContentError("session member paths must be relative to the active session root")
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


def timestamp_log_path(logs_dir: Path, timestamp: str) -> Path:
    candidate = logs_dir / timestamp
    if not candidate.exists() and not candidate.is_symlink():
        return candidate
    index = 1
    while True:
        candidate = logs_dir / f"{timestamp}--{index:02d}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        index += 1


def _ensure_name_link(names_dir: Path, data_path: Path, preferred_name: str) -> Path:
    names_dir.mkdir(parents=True, exist_ok=True)
    name_link = names_dir / sanitize_name(preferred_name)
    if name_link.exists() or name_link.is_symlink():
        if not name_link.is_symlink():
            raise ContentError(
                f"integrity error: expected symlink at {name_link}, found regular file"
            )
        target = os.readlink(name_link)
        expected = os.path.relpath(data_path, start=names_dir)
        if target != expected:
            raise ContentError(
                f"integrity error: {name_link} points to {target!r} instead of {expected!r}"
            )
        return name_link
    name_link.symlink_to(os.path.relpath(data_path, start=names_dir))
    return name_link


def _merge_meta(existing: dict[str, Any] | None, update: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if existing:
        payload.update(existing)
    payload.update(update)
    if existing and "created_at" in existing:
        payload["created_at"] = existing["created_at"]
    return payload


def _read_existing_meta(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContentError(f"invalid metadata file at {path}: {exc}") from exc


def materialize_bytes(
    data: bytes,
    *,
    dirs: ResolvedDirs,
    preferred_name: str,
    metadata: dict[str, Any],
    timestamp: str | None = None,
) -> Materialization:
    digest = hashlib.sha256(data).hexdigest()
    content_dir = dirs.content_dir / digest
    data_path = content_dir / "data"
    meta_path = content_dir / "meta.json"
    names_dir = content_dir / "names"
    logs_dir = content_dir / "logs"
    names_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    if not data_path.exists():
        _write_atomic(data_path, data)

    name_link = _ensure_name_link(names_dir, data_path, preferred_name)
    ts = timestamp or iso_utc()
    fetch_link = timestamp_log_path(logs_dir, ts)
    fetch_link.symlink_to(os.path.relpath(name_link, start=logs_dir))

    existing = _read_existing_meta(meta_path)
    update = _merge_meta(
        existing,
        {
            "hash": digest,
            "algorithm": "sha256",
            "bytes": len(data),
            "preferred_name": sanitize_name(preferred_name),
            "original_name": preferred_name,
            **metadata,
        },
    )
    if not update.get("created_at"):
        update["created_at"] = ts
    update["fetched_at"] = ts
    _write_atomic(
        meta_path,
        (json.dumps(update, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    _append_manifest(
        dirs,
        {
            "locator": metadata.get("locator", ""),
            "canonical_locator": metadata.get("canonical_locator", ""),
            "canonical_path": str(data_path),
            "fetched_at": ts,
            "fetcher": metadata.get("tool", "gotta"),
            "plugin": metadata.get("plugin", ""),
            "provider": metadata.get("provider", ""),
            "artifact_kind": metadata.get("artifact_kind", ""),
            "actor": metadata.get("actor") or session_identity(dirs.session_dir) or _current_actor(),
            "actor_dir": metadata.get("actor_dir", ""),
            "session_root": metadata.get("session_root", ""),
            "visibility_level": metadata.get("visibility_level", ""),
            "visibility_boundary": metadata.get("visibility_boundary", ""),
            "visibility_confidence": metadata.get("visibility_confidence", ""),
            "visibility_basis": metadata.get("visibility_basis", []),
            "checksum": digest,
            "preferred_name": sanitize_name(preferred_name),
            "fetch_link": str(fetch_link),
        },
    )
    try:
        from gotta import leads as lead_index

        lead_index.maybe_write_lead_cache(content_dir, data=data)
    except Exception:
        pass

    return Materialization(
        content_dir=content_dir,
        data_path=data_path,
        meta_path=meta_path,
        names_dir=names_dir,
        logs_dir=logs_dir,
        name_link=name_link,
        fetch_link=fetch_link,
        digest=digest,
        artifact_kind=str(metadata.get("artifact_kind", "") or "").strip(),
    )


def is_sha256_digest(value: str) -> bool:
    return bool(_SHA256_RE.fullmatch(value))


def _read_json_file(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContentError(f"invalid metadata file at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContentError(f"invalid metadata file at {path}: expected object")
    return payload


def _collect_directory_snapshot(content_dir: Path) -> ContentSnapshot | None:
    digest = content_dir.name
    if not content_dir.is_dir() or not is_sha256_digest(digest):
        return None
    data_path = content_dir / "data"
    if not data_path.exists():
        return None
    meta_path = content_dir / "meta.json"
    names_dir = content_dir / "names"
    logs_dir = content_dir / "logs"
    names = (
        sorted(path.name for path in names_dir.iterdir() if path.is_symlink())
        if names_dir.exists()
        else []
    )
    events: list[ContentEvent] = []
    if logs_dir.exists():
        for path in sorted(logs_dir.iterdir(), key=lambda item: item.name):
            if not path.is_symlink():
                continue
            target = path.parent / os.readlink(path)
            events.append(
                ContentEvent(
                    timestamp=path.name.split("--", 1)[0],
                    link_name=target.name,
                    link_path=target.resolve(),
                    log_path=path,
                )
            )
    return ContentSnapshot(
        digest=digest,
        content_dir=content_dir,
        data_path=data_path,
        meta_path=meta_path if meta_path.exists() else None,
        names_dir=names_dir,
        logs_dir=logs_dir,
        names=names,
        events=events,
        metadata=_read_json_file(meta_path if meta_path.exists() else None),
    )


def scan_content_store(content_dir: Path) -> list[ContentSnapshot]:
    snapshots: dict[str, ContentSnapshot] = {}
    if not content_dir.exists():
        return []
    for path in sorted(content_dir.iterdir(), key=lambda item: item.name):
        snapshot = _collect_directory_snapshot(path)
        if snapshot is not None:
            snapshots[snapshot.digest] = snapshot
    return [snapshots[digest] for digest in sorted(snapshots)]
