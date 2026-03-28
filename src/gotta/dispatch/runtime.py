"""Runtime session and execution helpers for dispatch."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import os
import sys
from typing import Any

from gotta.builtin import get_plugin
from gotta.capture import Capture
from gotta.content.env import CONTENT_ENV, SESSION_ENV
from gotta.content.model import CommonOptions, ContentError, ResolvedDirs
from gotta.content.scope import resolve_dirs, session_is_initialized
from gotta.resolve.invoke import ResolvedInvocation


def system_exit_status(exc: SystemExit, *, emit: bool = True) -> int:
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    message = str(code).strip()
    if emit and message:
        print(message, file=sys.stderr)
    return 1


@contextmanager
def scoped_runtime_env(dirs: ResolvedDirs) -> Any:
    previous = {
        SESSION_ENV: os.environ.get(SESSION_ENV),
        CONTENT_ENV: os.environ.get(CONTENT_ENV),
    }
    os.environ[SESSION_ENV] = str(dirs.session_dir)
    os.environ[CONTENT_ENV] = str(dirs.content_dir)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_callable(func: Callable[[list[str]], int], argv: list[str]) -> int:
    try:
        return int(func(argv))
    except SystemExit as exc:
        return system_exit_status(exc)


def _captured_execution(
    plugin: str,
    argv: list[str],
    options: CommonOptions,
) -> tuple[Capture, bytes]:
    spec = get_plugin(plugin)
    if spec is None or spec.capture is None or spec.project is None:
        raise RuntimeError(f"plugin `{plugin}` does not support canonical capture")
    capture = spec.capture(argv, options)
    display = spec.project(argv, capture)
    return capture, display


def require_operational_session(dirs: ResolvedDirs) -> None:
    if not session_is_initialized(dirs.session_dir):
        raise ContentError(
            "start or bind a session first with `gotta ...` before running "
            "operational commands. Stable interactive contexts adopt and "
            "scaffold their deterministic session on first session-aware use. "
            "Use `gotta session init --session <root>` only when you "
            "intentionally want to scaffold one exact root."
        )


def _runtime_dirs(options: CommonOptions, *, access: str) -> ResolvedDirs:
    if access == "ambient" and os.environ.get(SESSION_ENV, "").strip():
        return resolve_dirs(CommonOptions(), create=False)
    return resolve_dirs(options, create=False)


def _sessionless_notice_bytes(resolved: ResolvedInvocation) -> bytes:
    if os.environ.get("GOTTA_AMBIENT_SESSIONLESS", "") != "1":
        return b""
    if not sys.stderr.isatty():
        return b""
    if resolved.artifact_intent == "discovery":
        return (
            "ran sessionless; bind or pass `--session <session-id>` to store this as a "
            "discovery artifact\n"
        ).encode("utf-8")
    if resolved.artifact_intent == "evidence":
        return (
            "ran sessionless; bind or pass `--session <session-id>` to store this as an "
            "evidence artifact\n"
        ).encode("utf-8")
    return b""


def _streams_live(plugin: str, argv: list[str]) -> bool:
    return plugin == "actor" and bool(argv) and argv[0] == "launch"
