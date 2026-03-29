"""GitHub projection from canonical capture bytes."""

from __future__ import annotations

import base64
import json

from gotta.capture import Capture, json_bytes
from gotta.projection import Projection, projection_bytes
from gotta.project import looks_text, pretty_json

from .parse import ParsedArgs, parse_args
from .render import (
    markdown_binary_blob,
    markdown_commit,
    markdown_commit_list,
    markdown_directory,
    markdown_issue_or_pr,
    markdown_release,
    markdown_release_list,
    markdown_repo,
    markdown_repo_directory,
    markdown_text_blob_summary,
    markdown_workflow_job,
    markdown_workflow_run,
)
from .search import markdown_search


def _dict_payload(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _blob_json_payload(
    path: str,
    data: bytes,
    *,
    owner: str,
    repo: str,
    ref: str,
) -> dict[str, object]:
    return {
        "path": path,
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
        "size": len(data),
        "url": f"https://github.com/{owner}/{repo}/blob/{ref}/{path}",
    }


def _blob_payload(
    capture: Capture, *, owner: str, repo: str, ref: str, path: str
) -> bytes:
    payload = capture.view_data.get("payload")
    if isinstance(payload, dict):
        return json_bytes(payload)
    return json_bytes(
        _blob_json_payload(path, capture.data, owner=owner, repo=repo, ref=ref)
    )


def _parsed_args(argv: list[str], *, command: str, output: str) -> ParsedArgs:
    return (
        parse_args(argv, emit_help=False)
        if argv
        else ParsedArgs(command=command, output=output)
    )


def _project_search(argv: list[str], capture: Capture) -> bytes:
    payload = json.loads(capture.data.decode("utf-8"))
    parsed = _parsed_args(argv, command="search", output="markdown")
    if parsed.output == "json":
        return pretty_json(capture.data)
    return markdown_search(
        payload, include_details=(parsed.output != "summary")
    ).encode("utf-8")


def _project_blob(
    argv: list[str],
    capture: Capture,
    *,
    owner: str,
    repo: str,
    ref: str,
    path: str,
) -> bytes:
    if not argv:
        if looks_text(capture.data):
            return capture.data
        return markdown_binary_blob(owner=owner, repo=repo, ref=ref, path=path).encode(
            "utf-8"
        )
    parsed = _parsed_args(argv, command="render", output="markdown")
    if parsed.output == "json":
        return _blob_payload(capture, owner=owner, repo=repo, ref=ref, path=path)
    if parsed.output == "summary":
        return markdown_text_blob_summary(
            owner=owner,
            repo=repo,
            ref=ref,
            path=path,
            payload={"size": len(capture.data)},
        ).encode("utf-8")
    if looks_text(capture.data):
        return capture.data
    return markdown_binary_blob(owner=owner, repo=repo, ref=ref, path=path).encode(
        "utf-8"
    )


def _project_tree(
    argv: list[str],
    capture: Capture,
    payload: dict[str, object],
    *,
    owner: str,
    repo: str,
    ref: str,
) -> bytes:
    entries = payload.get("entries")
    if not isinstance(entries, list):
        entries = []
    if not argv:
        return markdown_directory(
            owner=owner,
            repo=repo,
            ref=str(payload.get("ref") or ref),
            path=str(payload.get("path") or ""),
            entries=entries,
            readme_path=str(payload.get("readmePath") or ""),
            readme_summary=str(payload.get("readmeSummary") or ""),
        ).encode("utf-8")
    parsed = _parsed_args(argv, command="render", output="markdown")
    if parsed.output == "json":
        return pretty_json(capture.data)
    if parsed.output == "summary":
        return markdown_directory(
            owner=owner,
            repo=repo,
            ref=str(payload.get("ref") or ref),
            path=str(payload.get("path") or ""),
            entries=entries,
        ).encode("utf-8")
    hinted_path = capture.view_data.get("hinted_path")
    hinted_blob = capture.view_data.get("hinted_blob")
    if isinstance(hinted_path, str) and isinstance(hinted_blob, bytes):
        if looks_text(hinted_blob):
            return hinted_blob
        return markdown_binary_blob(
            owner=owner,
            repo=repo,
            ref=str(payload.get("ref") or ref),
            path=hinted_path,
        ).encode("utf-8")
    return markdown_directory(
        owner=owner,
        repo=repo,
        ref=str(payload.get("ref") or ref),
        path=str(payload.get("path") or ""),
        entries=entries,
        readme_path=str(payload.get("readmePath") or ""),
        readme_summary=str(payload.get("readmeSummary") or ""),
    ).encode("utf-8")


def _project_repo(
    argv: list[str],
    capture: Capture,
    payload: dict[str, object],
    *,
    owner: str,
    repo: str,
    ref: str,
) -> bytes:
    repo_payload = payload.get("payload")
    if not isinstance(repo_payload, dict):
        repo_payload = {}
    entries = payload.get("entries")
    if not isinstance(entries, list):
        entries = []
    if not argv:
        if capture.view_data.get("hinted_path") and capture.view_data.get(
            "hinted_blob"
        ):
            hinted_path = str(capture.view_data["hinted_path"])
            hinted_blob = capture.view_data["hinted_blob"]
            if isinstance(hinted_blob, bytes) and looks_text(hinted_blob):
                return hinted_blob
            return markdown_binary_blob(
                owner=owner,
                repo=repo,
                ref=str(payload.get("ref") or ref),
                path=hinted_path,
            ).encode("utf-8")
        if entries:
            return markdown_repo_directory(
                repo_payload,
                owner=owner,
                repo=repo,
                ref=str(payload.get("ref") or ref),
                entries=entries,
                readme_path=str(payload.get("readmePath") or ""),
                readme_summary=str(payload.get("readmeSummary") or ""),
            ).encode("utf-8")
        return markdown_repo(repo_payload).encode("utf-8")
    parsed = _parsed_args(argv, command="render", output="markdown")
    if parsed.output == "json":
        return pretty_json(capture.data)
    if parsed.output == "summary":
        return markdown_repo(repo_payload).encode("utf-8")
    if capture.view_data.get("hinted_path") and capture.view_data.get("hinted_blob"):
        hinted_path = str(capture.view_data["hinted_path"])
        hinted_blob = capture.view_data["hinted_blob"]
        if isinstance(hinted_blob, bytes) and looks_text(hinted_blob):
            return hinted_blob
        return markdown_binary_blob(
            owner=owner,
            repo=repo,
            ref=str(payload.get("ref") or ref),
            path=hinted_path,
        ).encode("utf-8")
    if entries:
        return markdown_repo_directory(
            repo_payload,
            owner=owner,
            repo=repo,
            ref=str(payload.get("ref") or ref),
            entries=entries,
            readme_path=str(payload.get("readmePath") or ""),
            readme_summary=str(payload.get("readmeSummary") or ""),
        ).encode("utf-8")
    return markdown_repo(repo_payload).encode("utf-8")


def _project_object(
    argv: list[str],
    capture: Capture,
    payload: dict[str, object],
    *,
    kind: str,
    owner: str,
    repo: str,
    ref: str,
) -> bytes:
    parsed = _parsed_args(argv, command="render", output="markdown")
    object_payload = payload.get("payload") if isinstance(payload, dict) else payload
    if parsed.output == "json":
        return pretty_json(capture.data)
    if kind == "issue":
        return markdown_issue_or_pr(
            object_payload if isinstance(object_payload, dict) else {},
            "issue",
            include_body=parsed.output == "markdown",
        ).encode("utf-8")
    if kind == "pr":
        return markdown_issue_or_pr(
            object_payload if isinstance(object_payload, dict) else {},
            "pull request",
            include_body=parsed.output == "markdown",
        ).encode("utf-8")
    if kind == "commit":
        return markdown_commit(
            object_payload if isinstance(object_payload, dict) else {},
            owner=owner,
            repo=repo,
            include_patch=parsed.output == "markdown",
        ).encode("utf-8")
    if kind == "commits":
        commits = object_payload if isinstance(object_payload, list) else []
        return markdown_commit_list(
            commits,
            owner=owner,
            repo=repo,
            ref=str(payload.get("ref") or ref),
            path=str(payload.get("path") or ""),
        ).encode("utf-8")
    if kind == "release":
        return markdown_release(
            object_payload if isinstance(object_payload, dict) else {},
            owner=owner,
            repo=repo,
        ).encode("utf-8")
    if kind == "workflow_run":
        return markdown_workflow_run(
            object_payload if isinstance(object_payload, dict) else {},
            owner=owner,
            repo=repo,
            include_jobs=parsed.output == "markdown",
        ).encode("utf-8")
    if kind == "workflow_job":
        return markdown_workflow_job(
            object_payload if isinstance(object_payload, dict) else {},
            owner=owner,
            repo=repo,
            include_steps=parsed.output == "markdown",
        ).encode("utf-8")
    releases = object_payload if isinstance(object_payload, list) else []
    if parsed.output == "summary":
        releases = releases[:10]
    return markdown_release_list(owner=owner, repo=repo, payload=releases).encode(
        "utf-8"
    )


def project(argv: list[str], capture: Capture) -> Projection:
    kind = str(capture.metadata.get("github_kind") or "").strip()
    if kind == "search":
        return projection_bytes(
            _project_search(argv, capture), content_type="text/markdown"
        )
    owner = str(capture.metadata.get("github_owner") or "").strip()
    repo = str(capture.metadata.get("github_repo") or "").strip()
    ref = str(capture.metadata.get("github_ref") or "").strip()
    path = str(capture.metadata.get("github_path") or "").strip()
    if kind == "blob":
        data = _project_blob(argv, capture, owner=owner, repo=repo, ref=ref, path=path)
        return projection_bytes(data, content_type="text/markdown")
    payload = json.loads(capture.data.decode("utf-8"))
    if kind == "tree":
        return projection_bytes(
            _project_tree(argv, capture, payload, owner=owner, repo=repo, ref=ref),
            content_type="text/markdown",
        )
    if kind == "repo":
        return projection_bytes(
            _project_repo(argv, capture, payload, owner=owner, repo=repo, ref=ref),
            content_type="text/markdown",
        )
    if kind in {
        "issue",
        "pr",
        "commit",
        "commits",
        "release",
        "releases",
        "workflow_run",
        "workflow_job",
    }:
        return projection_bytes(
            _project_object(
                argv,
                capture,
                _dict_payload(payload),
                kind=kind,
                owner=owner,
                repo=repo,
                ref=ref,
            ),
            content_type="text/markdown",
        )
    return projection_bytes(capture.data, content_type=capture.content_type)
