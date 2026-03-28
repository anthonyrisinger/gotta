#!/usr/bin/env python3
"""Entrypoint and plugin hooks for the GitHub surface."""

from __future__ import annotations

import json
import signal
import sys
from typing import Any

from gotta.capture import Capture

from .api import (
    default_branch_name,
    ensure_gh,
    ensure_gh_auth,
    gh_json_object,
    gh_json_value,
    gh_status_payload,
    looks_text,
)
from .capture import (
    CaptureDeps,
    _canonicalize_capture_value,
    capture as github_capture,
)
from .parse import (
    ParsedArgs,
    canonical_locator,
    die,
    parse_args,
    preferred_name,
)
from .project import project as github_project
from .read import (
    decode_content_blob,
    fetch_content_file,
    github_blob_url,
    is_readme_path,
    list_directory_entries,
    load_directory_fragment_file,
    load_directory_readme,
    normalize_ref_path,
    readme_rollup,
    split_render_url,
    workflow_job_payload,
    workflow_run_payload,
)
from .render import render_bytes, render_content
from .render import markdown_repo
from .route import route_target
from .search import (
    search_code_payload,
    search_issueish_payload,
    search_repositories_payload,
)


__all__ = [
    "ParsedArgs",
    "_canonicalize_capture_value",
    "canonical_locator",
    "capture",
    "main",
    "markdown_repo",
    "parse_args",
    "preferred_name",
    "project",
    "route_target",
]


def _capture_deps() -> CaptureDeps:
    return CaptureDeps(
        parse_args=parse_args,
        preferred_name=preferred_name,
        ensure_gh=ensure_gh,
        ensure_gh_auth=ensure_gh_auth,
        default_branch_name=default_branch_name,
        gh_json_object=gh_json_object,
        gh_json_value=gh_json_value,
        looks_text=looks_text,
        search_repositories_payload=search_repositories_payload,
        search_issueish_payload=search_issueish_payload,
        search_code_payload=search_code_payload,
        split_render_url=split_render_url,
        workflow_run_payload=workflow_run_payload,
        workflow_job_payload=workflow_job_payload,
        decode_content_blob=decode_content_blob,
        fetch_content_file=fetch_content_file,
        is_readme_path=is_readme_path,
        list_directory_entries=list_directory_entries,
        load_directory_fragment_file=load_directory_fragment_file,
        load_directory_readme=load_directory_readme,
        normalize_ref_path=normalize_ref_path,
        readme_rollup=readme_rollup,
        github_blob_url=github_blob_url,
    )


def capture(argv: list[str], options: Any) -> Capture:
    return github_capture(argv, options, deps=_capture_deps())


def project(argv: list[str], capture: Capture) -> bytes:
    return github_project(argv, capture)


def _projected_content_path(
    capture: Capture,
    data: bytes,
    *,
    parsed: ParsedArgs,
) -> str:
    if parsed.output != "markdown":
        return ""
    kind = str(capture.meta.get("github_kind") or "").strip()
    if kind == "blob" and data == capture.data and looks_text(capture.data):
        return str(capture.meta.get("github_path") or "")
    if kind not in {"repo", "tree"}:
        return ""
    hinted_path = capture.view.get("hinted_path")
    hinted_blob = capture.view.get("hinted_blob")
    if (
        isinstance(hinted_path, str)
        and isinstance(hinted_blob, bytes)
        and data == hinted_blob
        and looks_text(hinted_blob)
    ):
        return hinted_path
    return ""


def _emit_projection(data: bytes, *, capture: Capture, parsed: ParsedArgs) -> None:
    content_path = _projected_content_path(capture, data, parsed=parsed)
    if content_path:
        render_content(data, content_path)
        return
    if parsed.output == "json":
        render_bytes(data, "json")
        return
    sys.stdout.buffer.write(data)
    if not data.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")


def _system_exit_status(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    message = str(code).strip()
    if message:
        print(message, file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    try:
        parsed = parse_args(argv)
    except SystemExit as exc:
        return _system_exit_status(exc)
    if not parsed.command:
        return 0
    if parsed.command == "status":
        payload = gh_status_payload()
        if parsed.output == "json":
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            return 0
        sys.stdout.write(
            "\n".join(
                [
                    f"gh_present\t{str(bool(payload['ghPresent'])).lower()}",
                    f"authenticated\t{str(bool(payload['authenticated'])).lower()}",
                ]
            )
            + "\n"
        )
        return 0
    try:
        rendered_capture = capture(argv, object())
        rendered_bytes = project(argv, rendered_capture)
    except SystemExit as exc:
        return _system_exit_status(exc)
    except RuntimeError as exc:
        return die(str(exc), code=1)
    _emit_projection(rendered_bytes, capture=rendered_capture, parsed=parsed)
    return 0


if __name__ == "__main__":
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    raise SystemExit(main(sys.argv[1:]))
