from __future__ import annotations

from pathlib import Path
import shlex

from gotta.content.file import write_text_if_changed
from gotta.content.model import ResolvedDirs
from gotta.content.path import sh_quote

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


def state_dir_path(root: Path) -> Path:
    return root / "state"


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


def discover_state_env(*, include_context_session: bool = True) -> dict[str, str]:
    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        data = load_state_env_at_root(parent)
        if data:
            return data
    if include_context_session:
        from gotta.content.scope import context_bound_session_root

        context_root = context_bound_session_root()
        if context_root is not None:
            return load_state_env_at_root(context_root)
    return {}


def write_state_env(dirs: ResolvedDirs) -> Path:
    path = state_env_path(dirs.session_dir)
    lines = export_env_lines(dirs)
    return write_text_if_changed(path, "\n".join(lines) + "\n")


def write_session_state(
    dirs: ResolvedDirs,
    updates: dict[str, str] | None = None,
) -> Path:
    from gotta.content.scope import session_actor_scope, session_id

    existing = {
        key: value
        for key, value in load_state_env_at_root(dirs.session_dir).items()
        if key not in {SESSION_ACTIVATION_ENV, CONTEXT_ID_ENV, CONTEXT_SOURCE_ENV}
    }
    current_session_id = session_id(dirs.session_dir)
    actor_scope = session_actor_scope(dirs.session_dir)
    merged = {
        **existing,
        **env_mapping(dirs),
        SESSION_ID_ENV: current_session_id,
        SESSION_ACTOR_ENV: actor_scope,
    }
    if updates:
        merged.update(
            {key: value for key, value in updates.items() if value is not None}
        )
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
    return write_text_if_changed(path, "\n".join(lines) + "\n")


def export_env_lines(dirs: ResolvedDirs) -> list[str]:
    from gotta.content.scope import session_actor_scope, session_id

    lines = [
        f"export {SESSION_ENV}={sh_quote(str(dirs.session_dir))}",
        f"export {SESSION_ID_ENV}={sh_quote(session_id(dirs.session_dir))}",
        f"export {CONTENT_ENV}={sh_quote(str(dirs.content_dir))}",
        f"export {STATE_DIR_ENV}={sh_quote(str(state_dir_path(dirs.session_dir)))}",
    ]
    actor_scope = session_actor_scope(dirs.session_dir)
    if actor_scope:
        lines.append(f"export {SESSION_ACTOR_ENV}={sh_quote(actor_scope)}")
    return lines


def env_mapping(dirs: ResolvedDirs) -> dict[str, str]:
    from gotta.content.scope import session_actor_scope, session_id

    return {
        SESSION_ENV: str(dirs.session_dir),
        SESSION_ID_ENV: session_id(dirs.session_dir),
        CONTENT_ENV: str(dirs.content_dir),
        STATE_DIR_ENV: str(state_dir_path(dirs.session_dir)),
        SESSION_ACTOR_ENV: session_actor_scope(dirs.session_dir),
    }
