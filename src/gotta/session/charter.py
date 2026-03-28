"""Charter surfaces and shared text-input helpers."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from gotta.content.activity import append_activity_event
from gotta.content.context import stdin_has_meaningful_text
from gotta.content.file import write_text_atomic
from gotta.content.path import session_relative_path
from gotta.content.scope import session_identity
from gotta.helptext import format_long_help, is_long_help_request
from gotta import topology

from .registry import (
    WANT_FILE,
    _actor_bind_examples,
    _actor_label,
    _actor_session_dir,
    _normalize_actor_name,
)
from .scope import (
    _read_scope,
    _session_dir,
    _target_actor_ids,
)


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
    return (
        "write"
        if (argv_has_flag(argv, "--stdin") or argv_has_flag(argv, "--from-file"))
        else "read"
    )


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
        return session_relative_path(session_root, from_file).read_text(
            encoding="utf-8"
        )
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


def _native_surface_locator(surface: str, *, actor_name: str = "") -> str:
    normalized_surface = surface.strip().lower()
    normalized_actor = _normalize_actor_name(actor_name) if actor_name.strip() else ""
    if normalized_actor:
        return f"{normalized_surface}:actor:{normalized_actor}"
    return f"{normalized_surface}:session"


def _native_surface_follow_command(surface: str, *, actor_name: str = "") -> str:
    normalized_surface = surface.strip().lower()
    normalized_actor = _normalize_actor_name(actor_name) if actor_name.strip() else ""
    command = f"gotta {normalized_surface}"
    if normalized_actor:
        command += f" --actor {normalized_actor}"
    return command


def _native_surface_preferred_name(surface: str, *, actor_name: str = "") -> str:
    return _native_surface_locator(surface, actor_name=actor_name)


def _surface_actor_scope(work_dir: Path) -> str:
    return (
        session_identity(work_dir) if work_dir.resolve().parent.name == "actors" else ""
    )


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
        if topology.parse_shared_session_root(work_dir) is None:
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
    write_text_atomic(path, payload + "\n")
    actor_scope = _surface_actor_scope(work_dir)
    surface_key = "want" if surface_name == WANT_FILE else "goal"
    append_activity_event(
        work_dir,
        {
            "plugin": plugin_name,
            "surface": plugin_name,
            "action": "write",
            "locator": _native_surface_locator(surface_key, actor_name=actor_scope),
            "preferred_name": _native_surface_preferred_name(
                surface_key,
                actor_name=actor_scope,
            ),
            "follow_command": _native_surface_follow_command(
                surface_key,
                actor_name=actor_scope,
            ),
            "detail": f"rewrote {surface_name}",
            "time_field": "session_recorded_at",
        },
    )
    print(f"rewrote {surface_name}: {path}")
    return 0


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
        return session_relative_path(session_root, from_file).read_text(
            encoding="utf-8"
        )
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
            raw = session_relative_path(session_root, from_file).read_text(
                encoding="utf-8"
            )
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
