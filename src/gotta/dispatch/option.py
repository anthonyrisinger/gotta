"""Common dispatch option parsing."""

from __future__ import annotations

from gotta.content import CommonOptions, ContentError


def split_common_options(
    argv: list[str],
    *,
    strip_actor: bool = False,
) -> tuple[CommonOptions, list[str]]:
    session_dir: str | None = None
    content_dir: str | None = None
    actor: str | None = None
    save_as: str | None = None

    cleaned: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--":
            cleaned.extend(argv[index:])
            break
        if item.startswith("--session="):
            session_dir = item.split("=", 1)[1]
            index += 1
            continue
        if item == "--session":
            if index + 1 >= len(argv):
                raise ContentError("--session requires a value")
            session_dir = argv[index + 1]
            index += 2
            continue
        if item.startswith("--content-dir="):
            content_dir = item.split("=", 1)[1]
            index += 1
            continue
        if item == "--content-dir":
            if index + 1 >= len(argv):
                raise ContentError("--content-dir requires a value")
            content_dir = argv[index + 1]
            index += 2
            continue
        if strip_actor and item.startswith("--actor="):
            actor = item.split("=", 1)[1]
            index += 1
            continue
        if strip_actor and item == "--actor":
            if index + 1 >= len(argv):
                raise ContentError("--actor requires a value")
            actor = argv[index + 1]
            index += 2
            continue
        if item.startswith("--save-as="):
            save_as = item.split("=", 1)[1]
            index += 1
            continue
        if item == "--save-as":
            if index + 1 >= len(argv):
                raise ContentError("--save-as requires a value")
            save_as = argv[index + 1]
            index += 2
            continue
        cleaned.append(item)
        index += 1

    return CommonOptions(
        session_dir=session_dir,
        content_dir=content_dir,
        actor=actor,
        save_as=save_as,
    ), cleaned


def strip_quiet_flag(argv: list[str]) -> tuple[bool, list[str]]:
    quiet = False
    cleaned: list[str] = []
    for token in argv:
        if token == "--quiet":
            quiet = True
            continue
        cleaned.append(token)
    return quiet, cleaned


def strip_full_output_flag(argv: list[str]) -> tuple[bool, list[str]]:
    full_output = False
    cleaned: list[str] = []
    for token in argv:
        if token == "--full-output":
            full_output = True
            continue
        cleaned.append(token)
    return full_output, cleaned
