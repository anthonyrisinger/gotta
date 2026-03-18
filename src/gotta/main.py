#!/usr/bin/env python3
"""Canonical gotta entrypoints."""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
import io
import os
from pathlib import Path
import subprocess
import sys
import time
import json

from gotta.actors import seed_primary_actor_context
from gotta.dispatch import available_plugins, print_usage, run_plugin
from gotta.helptext import is_long_help_request, strip_long_help_boilerplate
from gotta.peer import session_actor, supervisor_stop_message, supervisor_stop_pending
from gotta.content import (
    CONTENT_ENV,
    CONTEXT_ACTIVE_ENV,
    CONTEXT_ID_ENV,
    CONTEXT_SOURCE_ENV,
    current_context_binding,
    SESSION_ACTIVATION_ENV,
    SESSION_CREATED_ENV,
    SESSION_ENV,
    SESSION_ID_ENV,
    SESSION_REPO_ENV,
    WORK_INITIALIZED_ENV,
    CommonOptions,
    DEFAULT_SESSION_ROOT,
    bound_session_root,
    load_state_env_at_root,
    resolve_dirs,
    resolve_session_reference,
    resolve_session_root_by_id,
    session_token,
    session_is_initialized,
    write_session_state,
)


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
            "Investigative session surfaces live under `gotta session`: "
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
    return session_token(context_id)


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


def _peer_stop_warning(root: Path) -> str:
    peer_name = session_actor(root)
    if not peer_name:
        return ""
    path = root / "state" / "peer.json"
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
        peer_name,
        status_payload=payload,
    )


def _iter_session_roots(base_dir: Path) -> list[Path]:
    if not base_dir.exists():
        return []
    return sorted(
        path.resolve()
        for path in base_dir.iterdir()
        if path.is_dir() and (path / "state" / "env").exists()
    )


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
    dirs = resolve_dirs(CommonOptions(session_dir=str(root)), create=True)
    dirs.session_dir.joinpath("bin").mkdir(parents=True, exist_ok=True)
    repo_root = _discover_repo_root()
    write_session_state(
        dirs,
        {
            CONTEXT_ID_ENV: context_id,
            CONTEXT_SOURCE_ENV: context_source,
            SESSION_CREATED_ENV: load_state_env_at_root(root).get(SESSION_CREATED_ENV, "")
            or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            SESSION_ACTIVATION_ENV: activation,
            SESSION_REPO_ENV: str(repo_root) if repo_root is not None else "",
        },
    )
    return dirs.session_dir.resolve(), True


def _bind_session_root(context_id: str, context_source: str) -> tuple[Path, bool]:
    base_dir = DEFAULT_SESSION_ROOT.expanduser().resolve()
    canonical = base_dir / _session_token(context_id)
    with _session_creation_lock(base_dir, context_id):
        matches = _matching_session_roots(base_dir, context_id)
        if len(matches) > 1:
            grit = "\n".join(
                [
                    "ambiguous gotta session binding for the current context:",
                    f"- context id: {context_id}",
                    f"- context source: {context_source}",
                    *[f"- candidate: {path}" for path in matches],
                    "repair the duplicate session roots so one canonical session remains",
                    "for this context before rerunning `gotta ...`",
                ]
            )
            raise SystemExit(grit)
        if len(matches) == 1:
            return matches[0], False
        if session_is_initialized(canonical):
            dirs = resolve_dirs(CommonOptions(session_dir=str(canonical)), create=False)
            write_session_state(
                dirs,
                {
                    CONTEXT_ID_ENV: context_id,
                    CONTEXT_SOURCE_ENV: context_source,
                    SESSION_ACTIVATION_ENV: "gotta",
                },
            )
            return canonical, False
        return _create_session_root(
            canonical,
            context_id=context_id,
            context_source=context_source,
            activation="gotta",
        )


def _resolve_existing_session_root(context_id: str, context_source: str) -> Path | None:
    base_dir = DEFAULT_SESSION_ROOT.expanduser().resolve()
    matches = _matching_session_roots(base_dir, context_id)
    if len(matches) > 1:
        grit = "\n".join(
            [
                "ambiguous gotta session binding for the current context:",
                f"- context id: {context_id}",
                f"- context source: {context_source}",
                *[f"- candidate: {path}" for path in matches],
                "repair the duplicate session roots so one canonical session remains",
                "for this context before rerunning `gotta ...`",
            ]
        )
        raise SystemExit(grit)
    if len(matches) == 1:
        return matches[0]
    return None


def _hydrate_environment(root: Path, *, context_id: str, context_source: str) -> None:
    state = load_state_env_at_root(root)
    for key in list(os.environ):
        if key.startswith("GOTTA_WORK_"):
            os.environ.pop(key, None)
    for key, value in state.items():
        os.environ[key] = value
    os.environ[SESSION_ENV] = str(root)
    os.environ[SESSION_ID_ENV] = str(state.get(SESSION_ID_ENV) or root.name)
    os.environ[CONTENT_ENV] = str(root / "content")
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
    if state.get(WORK_INITIALIZED_ENV, "").strip() != "1":
        for key in list(os.environ):
            if key.startswith("GOTTA_WORK_"):
                os.environ.pop(key, None)


def _ensure_scaffolded_session(
    root: Path,
    *,
    context_id: str,
    context_source: str,
) -> tuple[Path, bool]:
    created = False
    if not session_is_initialized(root):
        root, created = _create_session_root(
            root,
            context_id=context_id,
            context_source=context_source,
            activation="gotta",
        )
    state = load_state_env_at_root(root)
    if state.get(WORK_INITIALIZED_ENV, "").strip() != "1":
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


def _prefer_bound_session_root() -> Path | None:
    root = bound_session_root()
    if root is not None and session_is_initialized(root):
        return root
    return None


def _is_nonbinding_help(argv: list[str]) -> bool:
    if not argv:
        return True
    if len(argv) == 1 and argv[0] in {"-h", "--help", "--help-all"}:
        return True
    return "--help" in argv or "--help-all" in argv


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    normalized = normalize_help_aliases(args)
    try:
        if _is_nonbinding_help(normalized):
            return _gotta_main(normalized)
        context_id, context_source = current_context_binding()
        explicit_session = _explicit_session_arg(normalized)
        if explicit_session:
            explicit_root = resolve_session_reference(explicit_session, allow_missing=False)
            if explicit_root is None:
                return die(
                    "relative session references require an active or discoverable "
                    "session root. Use `gotta ...` to bind one first or pass an "
                    "absolute `--session` path."
                )
            if session_is_initialized(explicit_root):
                root = explicit_root
            else:
                root = resolve_session_root_by_id(explicit_session) or explicit_root
        else:
            root = _prefer_bound_session_root()
        created = False
        if root is None:
            root = _resolve_existing_session_root(context_id, context_source)
        if root is None:
            root, created = _bind_session_root(context_id, context_source)
        root, scaffold_created = _ensure_scaffolded_session(
            root,
            context_id=context_id,
            context_source=context_source,
        )
        created = created or scaffold_created
        original_env = os.environ.copy()
        try:
            _hydrate_environment(root, context_id=context_id, context_source=context_source)
            seed_primary_actor_context()
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
            warning = _peer_stop_warning(root)
            if warning:
                print(warning, file=sys.stderr)
            return _gotta_main(normalized)
        finally:
            os.environ.clear()
            os.environ.update(original_env)
    except BrokenPipeError:
        _silence_stdout()
        return 0
