#!/usr/bin/env python3
"""CLI argv normalization and top-level surface dispatch."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

from gotta.builtin import SessionAccessMode, get_binding
from gotta.compat import tomllib
from gotta.dispatch.main import (
    available_surfaces,
    print_usage,
    run_surface,
    system_exit_status,
)
from gotta.helptext import is_long_help_request, strip_long_help_boilerplate
from gotta.cli.notice import die

_GLOBAL_FLAGS = {"--quiet", "--full-output"}


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


def _argv_without_global_flags(argv: list[str]) -> list[str]:
    return [token for token in argv if token not in _GLOBAL_FLAGS]


def _surface_invocation(argv: list[str]) -> tuple[str, list[str]] | None:
    for index, token in enumerate(argv):
        if token in _GLOBAL_FLAGS:
            continue
        surface = token
        surface_argv = [*argv[:index], *argv[index + 1 :]]
        return surface, surface_argv
    return None


def _gotta_version() -> str:
    try:
        return package_version("gotta")
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
        try:
            payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return "unknown"
        project = payload.get("project")
        if not isinstance(project, dict):
            return "unknown"
        return str(project.get("version") or "").strip() or "unknown"


def _is_version_request(argv: list[str]) -> bool:
    if not argv:
        return False
    return len(argv) == 1 and argv[0] in {"version", "--version", "-V"}


def _gotta_main(argv: list[str]) -> int:
    argv = normalize_help_aliases(argv)
    stripped = _argv_without_global_flags(argv)
    if _is_version_request(stripped):
        print(f"gotta {_gotta_version()}")
        return 0
    if not stripped or (len(stripped) == 1 and stripped[0] in {"-h", "--help"}):
        return print_usage()
    if is_long_help_request(stripped):
        print("# gotta")
        print("")
        print("usage: gotta <surface> [args...]")
        print("")
        print("Canonical operator path: `gotta ...`")
        print("")
        print(
            "Builtin non-session surfaces: `gotta version`, `gotta --version`, `gotta search`"
        )
        print("")
        print(
            "Session synthesis surfaces live under `gotta session`: "
            "`manifest`, `timeline`, `graph`, `leads`, `analyze`, `scan`"
        )
        print("")
        print("This top-level long help shows only top-level surface bindings.")
        print("Use `gotta <surface> --help-all` for recursive help within one surface.")
        print("")
        print("available top-level surfaces:")
        for surface in available_surfaces():
            print(f"  - {surface}")
        for surface in available_surfaces():
            print("")
            print(f"## gotta {surface}")
            print("")
            buffer = io.StringIO()
            try:
                with redirect_stdout(buffer), redirect_stderr(buffer):
                    result = run_surface(surface, ["--help"])
            except SystemExit as exc:
                result = system_exit_status(exc, emit=False)
            if result != 0:
                return result
            rendered = strip_long_help_boilerplate(buffer.getvalue())
            if rendered:
                print(rendered)
        print("")
        print("---")
        print("")
        print("End of top-level long help for `gotta`.")
        print("Nested surface trees were intentionally omitted at this level.")
        print("Use `gotta <surface> --help-all` for recursive help within one surface.")
        return 0

    invocation = _surface_invocation(argv)
    if invocation is None:
        return print_usage()
    surface, surface_argv = invocation
    if surface not in available_surfaces():
        surfaces = ", ".join(available_surfaces())
        return die(f"unknown gotta surface: {surface}. available surfaces: {surfaces}")
    return run_surface(surface, surface_argv)


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


def _is_nonbinding_help(argv: list[str]) -> bool:
    if not argv:
        return True
    if len(argv) == 1 and argv[0] in {"-h", "--help", "--help-all"}:
        return True
    return "--help" in argv or "--help-all" in argv


def dispatches_without_session_management(argv: list[str]) -> bool:
    if not argv:
        return True
    surface_name = argv[0]
    if surface_name == "session" and len(argv) >= 2 and argv[1] in {"bind"}:
        return True
    return _session_access_mode(argv) == "none"


def _session_access_mode(argv: list[str]) -> SessionAccessMode:
    if not argv:
        return "none"
    binding = get_binding(argv[0])
    if binding is None:
        return "none"
    access = binding.session_access
    if access is None:
        return "write"
    if callable(access):
        return access(argv[1:])
    return access
