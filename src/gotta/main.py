#!/usr/bin/env python3
"""Canonical gotta entrypoints."""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import time
import json

from gotta.compat import UTC, datetime
from gotta.actors import resolve_actor_context, seed_actor_context
from gotta.dispatch import available_plugins, print_usage, run_plugin
from gotta.helptext import is_long_help_request, strip_long_help_boilerplate
from gotta.actor import session_actor, supervisor_stop_message, supervisor_stop_pending
from gotta.content import (
    CONTENT_ENV,
    CONTEXT_ACTIVE_ENV,
    CONTEXT_ID_ENV,
    CONTEXT_SOURCE_ENV,
    current_context_binding,
    default_session_id,
    SESSION_ACTIVATION_ENV,
    SESSION_CREATED_ENV,
    SESSION_ENV,
    SESSION_ID_ENV,
    SESSION_ACTOR_ENV,
    SESSION_REPO_ENV,
    SESSION_INITIALIZED_ENV,
    CommonOptions,
    DEFAULT_SESSION_ROOT,
    load_state_env_at_root,
    resolve_dirs,
    resolve_session_reference,
    session_id,
    session_identity as content_session_identity,
    session_is_initialized,
    shared_session_root,
    write_session_state,
)
from gotta import session as session_plugin
from gotta import topology


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def _silence_stdout() -> None:
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(devnull, sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        pass
    finally:
        os.close(devnull)


def normalize_help_aliases(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    if argv[0] == "help":
        if len(argv) == 1:
            return ["--help-all"]
        if len(argv) >= 2 and argv[1] == "all":
            return ["--help-all", *argv[2:]]
        return [argv[1], "--help-all", *argv[2:]]
    normalized: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "help":
            normalized.append("--help-all")
            if index + 1 < len(argv) and argv[index + 1] == "all":
                index += 2
                continue
            index += 1
            continue
        normalized.append(token)
        index += 1
    return normalized


def _gotta_main(argv: list[str]) -> int:
    argv = normalize_help_aliases(argv)
    if not argv or (len(argv) == 1 and argv[0] in {"-h", "--help"}):
        return print_usage()
    if is_long_help_request(argv):
        print("# gotta")
        print("")
        print("usage: gotta <plugin> [args...]")
        print("")
        print("Canonical operator path: `gotta ...`")
        print("")
        print(
            "Session synthesis surfaces live under `gotta session`: "
            "`manifest`, `timeline`, `graph`, `leads`, `analyze`"
        )
        print("")
        print("This top-level long help shows only plugin root surfaces.")
        print("Use `gotta <plugin> --help-all` for recursive help within one plugin.")
        print("")
        print("available plugins:")
        for plugin in available_plugins():
            print(f"  - {plugin}")
        for plugin in available_plugins():
            print("")
            print(f"## gotta {plugin}")
            print("")
            buffer = io.StringIO()
            try:
                with redirect_stdout(buffer), redirect_stderr(buffer):
                    result = run_plugin(plugin, ["--help"])
            except SystemExit as exc:
                result = int(exc.code or 0)
            if result != 0:
                return result
            rendered = strip_long_help_boilerplate(buffer.getvalue())
            if rendered:
                print(rendered)
        print("")
        print("---")
        print("")
        print("End of top-level long help for `gotta`.")
        print("Plugin subtrees were intentionally omitted at this level.")
        print("Use `gotta <plugin> --help-all` for recursive help within one plugin.")
        return 0

    plugin = argv[0]
    if plugin not in available_plugins():
        plugins = ", ".join(available_plugins())
        return die(
            f"unknown gotta plugin: {plugin}. available plugins: {plugins}"
        )
    return run_plugin(plugin, argv[1:])


def _session_token(context_id: str) -> str:
    return default_session_id(context_id)


_SESSION_LOCK_TIMEOUT_SECONDS = 5.0
_SESSION_LOCK_SLEEP_SECONDS = 0.05


@contextmanager
def _session_creation_lock(base_dir: Path, context_id: str):
    base_dir.mkdir(parents=True, exist_ok=True)
    lock_path = base_dir / f".{_session_token(context_id)}.lock"
    deadline = time.monotonic() + _SESSION_LOCK_TIMEOUT_SECONDS
    fd: int | None = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise SystemExit(
                    f"timed out waiting for gotta session bind lock: {lock_path}"
                )
            time.sleep(_SESSION_LOCK_SLEEP_SECONDS)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _discover_repo_root() -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _actor_stop_warning(root: Path) -> str:
    actor_name = session_actor(root)
    if not actor_name:
        return ""
    path = root / "state" / "actor.json"
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    if not supervisor_stop_pending(payload):
        return ""
    return supervisor_stop_message(
        actor_name,
        status_payload=payload,
    )


def _iter_session_roots(base_dir: Path) -> list[Path]:
    if not base_dir.exists():
        return []
    roots: list[Path] = []
    for session_dir in base_dir.iterdir():
        if not session_dir.is_dir():
            continue
        actors_dir = session_dir / "actors"
        if not actors_dir.is_dir():
            continue
        for actor_dir in actors_dir.iterdir():
            if actor_dir.is_dir() and (actor_dir / "state" / "env").exists():
                roots.append(actor_dir.resolve())
    return sorted(roots)


def _matching_session_roots(base_dir: Path, context_id: str) -> list[Path]:
    matches: list[Path] = []
    for root in _iter_session_roots(base_dir):
        state = load_state_env_at_root(root)
        if state.get(CONTEXT_ID_ENV, "").strip() == context_id:
            matches.append(root)
    return matches


def _create_session_root(
    root: Path,
    *,
    context_id: str,
    context_source: str,
    activation: str,
) -> tuple[Path, bool]:
    current_session_id = topology.shared_session_id(root)
    actor = topology.session_identity(root)
    session_dir = shared_session_root(current_session_id)
    content_dir = session_dir / "content"
    session_dir.mkdir(parents=True, exist_ok=True)
    content_dir.mkdir(parents=True, exist_ok=True)
    dirs = resolve_dirs(
        CommonOptions(
            session_dir=str(root),
            content_dir=str(content_dir),
            actor=actor,
        ),
        create=True,
    )
    dirs.session_dir.joinpath("bin").mkdir(parents=True, exist_ok=True)
    repo_root = _discover_repo_root()
    write_session_state(
        dirs,
        {
            SESSION_CREATED_ENV: load_state_env_at_root(root).get(SESSION_CREATED_ENV, "")
            or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            SESSION_REPO_ENV: str(repo_root) if repo_root is not None else "",
            SESSION_ID_ENV: current_session_id,
            SESSION_ACTOR_ENV: actor,
        },
    )
    content_link = dirs.session_dir / "content"
    if content_link.is_symlink() or content_link.is_file():
        content_link.unlink(missing_ok=True)
    elif content_link.exists():
        os.rmdir(content_link)
    content_link.symlink_to(os.path.relpath(content_dir, start=content_link.parent))
    session_link = dirs.session_dir / "session"
    if session_link.is_symlink() or session_link.is_file():
        session_link.unlink(missing_ok=True)
    metadata_path = session_dir / "session.json"
    payload: dict[str, object]
    if metadata_path.exists():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        payload = existing if isinstance(existing, dict) else {}
    else:
        payload = {}
    members = payload.get("members")
    if not isinstance(members, list):
        members = []
    normalized_members = [
        topology.normalize_identity(str(item))
        for item in members
        if str(item).strip()
    ]
    if actor not in normalized_members:
        normalized_members.append(actor)
    actors = payload.get("actors")
    if not isinstance(actors, dict):
        actors = {}
    actors.setdefault(
        actor,
        {
            "label": actor,
            "model": session_plugin.ACTOR_DEFAULT_MODEL,
            "resume_uuid": "",
            "template": "",
        },
    )
    payload["session_id"] = current_session_id
    payload.setdefault("created_at", datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    payload["updated_at"] = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["members"] = sorted(dict.fromkeys(normalized_members))
    payload["actors"] = actors
    metadata_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return dirs.session_dir.resolve(), True


def _bind_session_root(context_id: str, context_source: str) -> tuple[Path, bool]:
    fingerprint = _session_token(context_id)
    session_id = fingerprint
    root = topology.session_root_for(session_id, fingerprint)
    shared_session_root(session_id).mkdir(parents=True, exist_ok=True)
    created = False
    with _session_creation_lock(DEFAULT_SESSION_ROOT.expanduser().resolve(), context_id):
        if not session_is_initialized(root):
            root, _created = _create_session_root(
                root,
                context_id=context_id,
                context_source=context_source,
                activation="gotta",
            )
            created = True
        dirs = resolve_dirs(CommonOptions(session_dir=str(root)), create=False)
        write_session_state(
            dirs,
            {
                SESSION_ID_ENV: session_id,
                SESSION_ACTOR_ENV: fingerprint,
            },
        )
        topology.write_binding(fingerprint, root)
    return root, created


def _resolve_existing_session_root(context_id: str, context_source: str) -> Path | None:
    binding = topology.resolve_binding(_session_token(context_id))
    if binding is not None and session_is_initialized(binding):
        return binding
    return None


def _hydrate_environment(root: Path, *, context_id: str, context_source: str) -> None:
    state = load_state_env_at_root(root)
    for key, value in state.items():
        os.environ[key] = value
    os.environ[SESSION_ENV] = str(root)
    os.environ[SESSION_ID_ENV] = str(state.get(SESSION_ID_ENV) or session_id(root))
    os.environ[CONTENT_ENV] = str(state.get(CONTENT_ENV) or (root / "content"))
    os.environ[SESSION_ACTOR_ENV] = str(
        state.get(SESSION_ACTOR_ENV) or content_session_identity(root)
    )
    os.environ[CONTEXT_ACTIVE_ENV] = "1"
    os.environ[CONTEXT_ID_ENV] = context_id
    os.environ[CONTEXT_SOURCE_ENV] = context_source
    os.environ[SESSION_ACTIVATION_ENV] = "gotta"
    repo_root = state.get(SESSION_REPO_ENV, "").strip()
    if repo_root:
        os.environ[SESSION_REPO_ENV] = repo_root
        venv = Path(repo_root) / ".venv"
        venv_bin = venv / "bin"
        if venv_bin.is_dir():
            path_entries = [str(venv_bin)]
            current_path = os.environ.get("PATH", "")
            if current_path:
                path_entries.append(current_path)
            os.environ["PATH"] = ":".join(path_entries)
            os.environ["VIRTUAL_ENV"] = str(venv)


def _ensure_scaffolded_session(
    root: Path,
    *,
    context_id: str,
    context_source: str,
) -> tuple[Path, bool]:
    shared_session = topology.parse_shared_session_root(root)
    if shared_session is not None:
        raise RuntimeError(
            f"shared session roots require an actor: {root}"
        )
    created = False
    if not session_is_initialized(root):
        root, created = _create_session_root(
            root,
            context_id=context_id,
            context_source=context_source,
            activation="gotta",
        )
    state = load_state_env_at_root(root)
    if state.get(SESSION_INITIALIZED_ENV, "").strip() != "1":
        from gotta import session as sessionlib

        sessionlib.scaffold_session(root)
    return root, created


def _explicit_session_arg(argv: list[str]) -> str | None:
    for index, token in enumerate(argv):
        if token == "--session" and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith("--session="):
            return token.split("=", 1)[1]
    return None


def _flag_value(argv: list[str], flag: str) -> str | None:
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith(f"{flag}="):
            return token.split("=", 1)[1]
    return None
def _explicit_actor_arg(argv: list[str]) -> str | None:
    return _flag_value(argv, "--actor")


def _prefer_bound_session_root() -> Path | None:
    explicit = os.environ.get(SESSION_ENV, "").strip()
    if explicit:
        root = resolve_session_reference(explicit, allow_missing=False)
        if root is not None and session_is_initialized(root):
            return root
    root = topology.resolve_binding(_session_token(current_context_binding()[0]))
    if root is not None and session_is_initialized(root):
        return root
    return None


def _active_identity(context_id: str) -> str:
    current = _prefer_bound_session_root()
    if current is not None:
        current_identity = topology.session_identity(current)
        if current_identity and not topology.is_placeholder_identity(current_identity):
            return current_identity
    explicit = os.environ.get(SESSION_ACTOR_ENV, "").strip()
    if explicit and not topology.is_placeholder_identity(explicit):
        return topology.normalize_identity(explicit)
    return _session_token(context_id)


def _is_nonbinding_help(argv: list[str]) -> bool:
    if not argv:
        return True
    if len(argv) == 1 and argv[0] in {"-h", "--help", "--help-all"}:
        return True
    return "--help" in argv or "--help-all" in argv


def _is_read_only_explicit_target(argv: list[str]) -> bool:
    if not argv:
        return False
    if argv[0] == "session":
        subcommand = "show"
        if len(argv) >= 2 and not argv[1].startswith("-"):
            subcommand = argv[1]
        return subcommand in {
            "show",
            "doctor",
            "manifest",
            "timeline",
            "graph",
            "leads",
            "analyze",
        }
    if argv[0] == "actor":
        subcommand = "status"
        if len(argv) >= 2 and not argv[1].startswith("-"):
            subcommand = argv[1]
        return subcommand == "status"
    return False


def _existing_actor_root_for_session(
    root: Path,
    *,
    preferred_identities: list[str],
) -> Path | None:
    session_id = topology.shared_session_id(root)
    actors_dir = topology.shared_session_root_for(session_id) / "actors"
    if not actors_dir.is_dir():
        return None
    initialized: dict[str, Path] = {}
    for actor_dir in sorted(actors_dir.iterdir()):
        if not actor_dir.is_dir():
            continue
        resolved = actor_dir.resolve()
        if not session_is_initialized(resolved):
            continue
        identity = topology.session_identity(resolved)
        if not identity or topology.is_placeholder_identity(identity):
            continue
        initialized[identity] = resolved
    for identity in preferred_identities:
        normalized = topology.normalize_identity(identity)
        match = initialized.get(normalized)
        if match is not None:
            return match
    return next(iter(initialized.values()), None)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    normalized = normalize_help_aliases(args)
    try:
        if _is_nonbinding_help(normalized):
            return _gotta_main(normalized)
        plugin_name = normalized[0] if normalized else ""
        context_id, context_source = current_context_binding()
        explicit_session = _explicit_session_arg(normalized)
        explicit_actor = _explicit_actor_arg(normalized)
        if plugin_name == "session" and len(normalized) >= 2 and normalized[1] in {"bind"}:
            return _gotta_main(normalized)
        if explicit_session:
            target_identity = topology.normalize_identity(
                explicit_actor or _active_identity(context_id)
            )
            explicit_root = resolve_session_reference(
                explicit_session,
                identity=target_identity,
                allow_missing=False,
            )
            if explicit_root is not None and explicit_actor:
                resolved_actor = session_plugin._resolve_bound_actor_name(
                    explicit_root,
                    explicit_actor,
                )
                root = topology.session_root_for(
                    session_id(explicit_root),
                    resolved_actor,
                )
            elif explicit_root is not None:
                root = explicit_root
            else:
                if explicit_actor:
                    return die(
                        "explicit actor targeting requires an existing shared session and a bound actor"
                    )
                root = resolve_session_reference(
                    explicit_session,
                    identity=target_identity,
                    allow_missing=True,
                )
            if root is None:
                return die(
                    "session references must be an absolute path, a shared session id, "
                    "or an explicit <session>/<actor> session reference"
                )
        elif explicit_actor:
            current = _prefer_bound_session_root()
            if current is None:
                current = _resolve_existing_session_root(context_id, context_source)
            if current is None:
                current, _created = _bind_session_root(context_id, context_source)
            resolved_actor = session_plugin._resolve_bound_actor_name(
                current,
                explicit_actor,
            )
            root = topology.session_root_for(session_id(current), resolved_actor)
        else:
            root = _prefer_bound_session_root()
        read_only_explicit_target = bool(explicit_session) and _is_read_only_explicit_target(
            normalized
        )
        created = False
        if root is None:
            root = _resolve_existing_session_root(context_id, context_source)
        if root is None:
            root, created = _bind_session_root(context_id, context_source)
        if read_only_explicit_target and not session_is_initialized(root):
            existing = _existing_actor_root_for_session(
                root,
                preferred_identities=[
                    explicit_actor or "",
                    _active_identity(context_id),
                ],
            )
            if existing is not None:
                root = existing
        scaffold_created = False
        if read_only_explicit_target:
            if not session_is_initialized(root):
                return die(
                    "explicit session inspection requires an initialized actor root in the "
                    "target shared session; bind an actor there first or pass --actor"
                )
        else:
            root, scaffold_created = _ensure_scaffolded_session(
                root,
                context_id=context_id,
                context_source=context_source,
            )
        created = created or scaffold_created
        original_env = os.environ.copy()
        try:
            acting_actor = resolve_actor_context(
                default_speaker=_active_identity(context_id)
            ).speaker or _active_identity(context_id)
            _hydrate_environment(root, context_id=context_id, context_source=context_source)
            seed_actor_context(acting_actor)
            if explicit_actor:
                os.environ[SESSION_ACTOR_ENV] = content_session_identity(root)
            if created:
                print(
                    "\n".join(
                        [
                            "created a new gotta session:",
                            f"- session root: {root}",
                            f"- context id: {context_id}",
                            f"- context source: {context_source}",
                        ]
                    ),
                    file=sys.stderr,
                )
            warning = _actor_stop_warning(root)
            if warning:
                print(warning, file=sys.stderr)
            return _gotta_main(normalized)
        finally:
            os.environ.clear()
            os.environ.update(original_env)
    except BrokenPipeError:
        _silence_stdout()
        return 0
