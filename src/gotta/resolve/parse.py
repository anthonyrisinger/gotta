"""Argument parsing for top-level `gotta read` resolution."""

from __future__ import annotations

import argparse

from gotta.resolve.model import ReadRequest
from gotta.resolve.route import partition_routed_target_tokens


def _int_value(value: object, *, default: int) -> int:
    return value if isinstance(value, int) else default


def _string_value(value: object) -> str:
    return str(value or "").strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gotta read",
        description=(
            "Render one local or remote target through the native retrieval surface. "
            "Remote/provider reads store durable evidence only when an "
            "initialized session is already in play or passed explicitly; "
            "`--head`, `--tail`, and `--section` only change what is shown to "
            "the operator, while local/session-owned rereads stay as "
            "non-materializing views."
        ),
    )
    parser.add_argument("target", nargs="?")
    parser.add_argument("--session", help=argparse.SUPPRESS)
    parser.add_argument(
        "--actor",
        help="attribute any materialized artifact from this read to the selected bound actor",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="traverse local directories recursively instead of listing only one level",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="maximum directory traversal depth when rendering local directories",
    )
    parser.add_argument(
        "--head", type=int, default=0, help="show only the first N lines"
    )
    parser.add_argument(
        "--tail", type=int, default=0, help="show only the last N lines"
    )
    parser.add_argument(
        "--section",
        help="show only the markdown section whose heading contains this text",
    )
    return parser


def parse_args(argv: list[str]) -> ReadRequest:
    parser = build_parser()
    if any(token in {"-h", "--help"} for token in argv):
        return ReadRequest(
            target=None,
            recursive=False,
            max_depth=3,
            head=0,
            tail=0,
            section="",
            session="",
            actor="",
            routed_plugin=None,
            routed_argv=(),
        )
    values: dict[str, object] = {
        "recursive": False,
        "max_depth": 3,
        "head": 0,
        "tail": 0,
        "section": "",
    }
    flags = {"--recursive": "recursive"}
    int_fields = {"--max-depth": "max_depth", "--head": "head", "--tail": "tail"}
    str_fields = {
        "--section": "section",
        "--session": "session",
        "--actor": "actor",
    }
    residual: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            residual.extend(argv[index + 1 :])
            break
        name, has_inline, inline_value = token.partition("=")
        if name in flags and not has_inline:
            values[flags[name]] = True
            index += 1
            continue
        if name in int_fields or name in str_fields:
            if not has_inline:
                if index + 1 >= len(argv):
                    parser.error(f"argument {name}: expected one argument")
                inline_value = argv[index + 1]
                index += 1
            field = int_fields.get(name) or str_fields.get(name) or ""
            try:
                values[field] = (
                    int(inline_value) if name in int_fields else inline_value
                )
            except ValueError as exc:
                raise SystemExit(f"argument {name}: invalid int value") from exc
            index += 1
            continue
        residual.append(token)
        index += 1
    target: str | None = None
    routed_plugin: str | None = None
    routed_argv: tuple[str, ...] = ()
    routed = partition_routed_target_tokens(residual)
    if routed is not None:
        routed_plugin, target, routed_argv = routed
    elif residual:
        flagged = next((token for token in residual if token.startswith("-")), "")
        if flagged:
            parser.error(f"unrecognized arguments: {flagged}")
        target = (
            " ".join(part.strip() for part in residual if part.strip()).strip() or None
        )
    return ReadRequest(
        target=target,
        recursive=bool(values["recursive"]),
        max_depth=_int_value(values["max_depth"], default=3),
        head=_int_value(values["head"], default=0),
        tail=_int_value(values["tail"], default=0),
        section=_string_value(values["section"]),
        session=_string_value(values.get("session")),
        actor=_string_value(values.get("actor")),
        routed_plugin=routed_plugin,
        routed_argv=routed_argv,
    )
