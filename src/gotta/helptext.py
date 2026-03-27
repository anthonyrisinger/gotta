"""Long-form recursive help rendering for gotta CLI surfaces."""

from __future__ import annotations

import argparse
import sys


LONG_HELP_TOKENS = {"--help-all", "help-all"}
LONG_HELP_BOILERPLATE_LINES = {
    "Use --help-all for recursive command help.",
    "Use --help-all for the same long-form usage output.",
    "use `gotta ask --help-all` for recursive ask-surface help",
}


def _subparser_action(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction[argparse.ArgumentParser] | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _iter_parser_tree(
    parser: argparse.ArgumentParser,
) -> list[tuple[str, argparse.ArgumentParser]]:
    result = [(parser.prog, parser)]
    subparsers = _subparser_action(parser)
    if subparsers is None:
        return result
    for name, child in subparsers.choices.items():
        if child.prog != f"{parser.prog} {name}":
            child.prog = f"{parser.prog} {name}"
        result.extend(_iter_parser_tree(child))
    return result


def format_long_help(
    parser: argparse.ArgumentParser,
    *,
    extra_parsers: list[argparse.ArgumentParser] | None = None,
    extra_sections: list[tuple[str, argparse.ArgumentParser]] | None = None,
) -> str:
    sections: list[str] = []
    seen: set[str] = set()

    def add_section(title: str, current: argparse.ArgumentParser) -> None:
        if title in seen:
            return
        seen.add(title)
        sections.append(
            f"## {title}\n\n{strip_long_help_boilerplate(current.format_help().rstrip())}"
        )

    for title, current in _iter_parser_tree(parser):
        add_section(title, current)
    for current in extra_parsers or []:
        for title, child in _iter_parser_tree(current):
            add_section(title, child)
    for title, current in extra_sections or []:
        add_section(title, current)
    footer = (
        f"---\n\n"
        f"End of recursive help for `{parser.prog}`.\n"
        f"Use plain `--help` for the root surface only.\n"
    )
    return "\n\n".join(sections) + "\n\n" + footer


def is_long_help_request(argv: list[str] | None) -> bool:
    if not argv:
        return False
    return len(argv) == 1 and argv[0] in LONG_HELP_TOKENS


def strip_long_help_boilerplate(text: str) -> str:
    lines = text.splitlines()
    filtered = [
        line for line in lines if line.strip() not in LONG_HELP_BOILERPLATE_LINES
    ]
    collapsed: list[str] = []
    blank_run = 0
    for line in filtered:
        if line.strip():
            blank_run = 0
            collapsed.append(line)
            continue
        blank_run += 1
        if blank_run <= 1:
            collapsed.append("")
    return "\n".join(collapsed).strip()


def print_long_help(
    parser: argparse.ArgumentParser,
    *,
    extra_parsers: list[argparse.ArgumentParser] | None = None,
    extra_sections: list[tuple[str, argparse.ArgumentParser]] | None = None,
) -> int:
    sys.stdout.write(
        format_long_help(
            parser,
            extra_parsers=extra_parsers,
            extra_sections=extra_sections,
        )
    )
    return 0
