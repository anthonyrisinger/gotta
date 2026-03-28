#!/usr/bin/env python3
"""Entrypoint and plugin hooks for the GitHub surface."""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
import signal
import sys
from typing import Any
import urllib.parse

from gotta.capture import Capture, json_bytes
from gotta.project import pretty_json
from gotta.source.visibility import with_visibility_metadata

from .api import (
    default_branch_name,
    ensure_gh,
    ensure_gh_auth,
    gh_json_object,
    gh_json_value,
    gh_status_payload,
    looks_text,
)
from .parse import (
    ParsedArgs,
    canonical_locator,
    die,
    parse_args,
    preferred_name,
    unsupported_render_limit_error,
)
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
from .render import (
    emit_markdown,
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
    render_bytes,
    render_content,
)
from .route import (
    ACTIONS_JOB_RE,
    ACTIONS_RUN_RE,
    BLOB_RE,
    COMMIT_RE,
    COMMITS_RE,
    COMMITS_ROOT_RE,
    ISSUE_RE,
    PULL_RE,
    RELEASES_RE,
    RELEASE_TAG_RE,
    REPO_RE,
    TREE_RE,
    route_target,
)
from .search import (
    markdown_search,
    search_code_payload,
    search_issueish_payload,
    search_repositories_payload,
)


__all__ = [
    "ParsedArgs",
    "canonical_locator",
    "capture",
    "main",
    "parse_args",
    "preferred_name",
    "project",
    "route_target",
]


def _search_limit(limit: int | None) -> int:
    return limit if isinstance(limit, int) else 10


def _dict_payload(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _dict_items(value: object) -> list[dict[str, object]]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _blob_content_type(path: str, data: bytes) -> str:
    guessed, _encoding = mimetypes.guess_type(path)
    if guessed:
        return guessed
    return "text/plain" if looks_text(data) else "application/octet-stream"


def _blob_json_payload(
    path: str, data: bytes, *, owner: str, repo: str, ref: str
) -> dict[str, object]:
    return {
        "path": path,
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
        "size": len(data),
        "url": github_blob_url(owner, repo, ref, path),
    }


def _repo_capture_payload(
    payload: dict[str, object],
    *,
    owner: str,
    repo: str,
    ref: str,
    entries: list[dict[str, object]],
    readme_path: str = "",
    readme_summary: str = "",
) -> dict[str, object]:
    return {
        "kind": "repo",
        "owner": owner,
        "repo": repo,
        "ref": ref,
        "payload": payload,
        "entries": entries,
        "readmePath": readme_path,
        "readmeSummary": readme_summary,
    }


def _tree_capture_payload(
    *,
    owner: str,
    repo: str,
    ref: str,
    path: str,
    entries: list[dict[str, object]],
    readme_path: str = "",
    readme_summary: str = "",
) -> dict[str, object]:
    return {
        "kind": "tree",
        "owner": owner,
        "repo": repo,
        "ref": ref,
        "path": path,
        "entries": entries,
        "readmePath": readme_path,
        "readmeSummary": readme_summary,
    }


def _object_capture_payload(
    kind: str,
    payload: object,
    *,
    owner: str,
    repo: str,
    ref: str = "",
    path: str = "",
) -> dict[str, object]:
    return {
        "kind": kind,
        "owner": owner,
        "repo": repo,
        "ref": ref,
        "path": path,
        "payload": payload,
    }


def _canonicalize_capture_url(target: str) -> str:
    if not target.startswith(("http://", "https://")):
        return target
    try:
        parsed = urllib.parse.urlsplit(target)
    except ValueError:
        return target
    host = parsed.netloc.strip().lower()
    if host in {"raw.githubusercontent.com", "raw.github.com"} or host.endswith(
        ".githubusercontent.com"
    ):
        filtered = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=True
            )
            if key.casefold() != "token"
        ]
        return urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urllib.parse.urlencode(filtered, doseq=True),
                parsed.fragment,
            )
        )
    return target


def _canonicalize_capture_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: _canonicalize_capture_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonicalize_capture_value(item) for item in value]
    if isinstance(value, str):
        return _canonicalize_capture_url(value)
    return value


def capture(argv: list[str], _options: Any) -> Capture:
    parsed = parse_args(argv, emit_help=False)
    if parsed.command == "search":
        gh = ensure_gh()
        ensure_gh_auth(gh)
        if parsed.search_type == "repo":
            payload = search_repositories_payload(
                gh,
                query=parsed.query,
                repo=parsed.repo,
                limit=_search_limit(parsed.limit),
                global_search=parsed.global_search,
            )
        elif parsed.search_type == "code":
            payload = search_code_payload(
                gh,
                query=parsed.query,
                repo=parsed.repo,
                limit=_search_limit(parsed.limit),
                global_search=parsed.global_search,
                filename=parsed.filename,
                extension=parsed.extension,
                language=parsed.language,
                match=parsed.match,
            )
        else:
            payload = search_issueish_payload(
                gh,
                query=parsed.query,
                repo=parsed.repo,
                limit=_search_limit(parsed.limit),
                search_type=parsed.search_type,
                global_search=parsed.global_search,
            )
        payload = _dict_payload(_canonicalize_capture_value(payload))
        return Capture(
            data=json_bytes(payload),
            name=preferred_name(argv, object()),
            type="application/json",
            meta={
                "projector": "github",
                "github_kind": "search",
            },
        )
    if parsed.command != "render":
        raise NotImplementedError("github capture does not support this command")
    gh = ensure_gh()
    ensure_gh_auth(gh)
    url, fragment = split_render_url(parsed.url)
    if match := ACTIONS_JOB_RE.match(url):
        owner, repo, run_id, job_id, _ = match.groups()
        payload = workflow_job_payload(
            gh,
            owner=owner,
            repo=repo,
            run_id=run_id,
            job_id=job_id,
        )
        payload = _dict_payload(_canonicalize_capture_value(payload))
        return Capture(
            data=json_bytes(
                _object_capture_payload("workflow_job", payload, owner=owner, repo=repo)
            ),
            name=preferred_name(argv, object()),
            type="application/json",
            meta={
                "projector": "github",
                "github_kind": "workflow_job",
                "github_owner": owner,
                "github_repo": repo,
                "source_created_at": str(payload.get("created_at") or ""),
                "source_updated_at": str(
                    payload.get("completed_at") or payload.get("started_at") or ""
                ),
            },
        )
    if match := ACTIONS_RUN_RE.match(url):
        owner, repo, run_id, _ = match.groups()
        payload = workflow_run_payload(gh, owner=owner, repo=repo, run_id=run_id)
        payload = _dict_payload(_canonicalize_capture_value(payload))
        return Capture(
            data=json_bytes(
                _object_capture_payload("workflow_run", payload, owner=owner, repo=repo)
            ),
            name=preferred_name(argv, object()),
            type="application/json",
            meta={
                "projector": "github",
                "github_kind": "workflow_run",
                "github_owner": owner,
                "github_repo": repo,
                "source_created_at": str(payload.get("createdAt") or ""),
                "source_updated_at": str(
                    payload.get("updatedAt") or payload.get("startedAt") or ""
                ),
            },
        )
    if match := BLOB_RE.match(url):
        owner, repo, ref, path = match.groups()
        path = normalize_ref_path(path)
        try:
            payload = fetch_content_file(gh, owner=owner, repo=repo, ref=ref, path=path)
            blob = decode_content_blob(payload)
        except RuntimeError as exc:
            if not is_readme_path(path):
                raise RuntimeError(str(exc)) from exc
            parent_path = str(Path(path).parent).replace("\\", "/").strip(".")
            readme = load_directory_readme(
                gh, owner=owner, repo=repo, ref=ref, path=parent_path
            )
            if readme is None:
                raise RuntimeError(str(exc)) from exc
            path, blob = readme
            payload = _blob_json_payload(path, blob, owner=owner, repo=repo, ref=ref)
        payload = _dict_payload(_canonicalize_capture_value(payload))
        return Capture(
            data=blob,
            name=Path(path).name or "github.bin",
            type=_blob_content_type(path, blob),
            meta={
                "projector": "github",
                "github_kind": "blob",
                "github_owner": owner,
                "github_repo": repo,
                "github_ref": ref,
                "github_path": path,
                "source_created_at": "",
                "source_updated_at": "",
            },
            view={"payload": payload},
        )
    if match := TREE_RE.match(url):
        owner, repo, ref, path = match.groups()
        path = normalize_ref_path(path)
        entries = list_directory_entries(gh, owner=owner, repo=repo, ref=ref, path=path)
        if len(entries) == 1 and str(entries[0].get("type") or "") == "file":
            file_path = str(entries[0].get("path") or path)
            payload = _dict_payload(_canonicalize_capture_value(entries[0]))
            blob = decode_content_blob(payload)
            return Capture(
                data=blob,
                name=Path(file_path).name or "github.bin",
                type=_blob_content_type(file_path, blob),
                meta={
                    "projector": "github",
                    "github_kind": "blob",
                    "github_owner": owner,
                    "github_repo": repo,
                    "github_ref": ref,
                    "github_path": file_path,
                },
                view={"payload": payload},
            )
        readme_path, readme_summary = readme_rollup(
            gh,
            owner=owner,
            repo=repo,
            ref=ref,
            entries=entries,
            path=path,
        )
        view: dict[str, object] = {}
        if fragment:
            hinted = load_directory_fragment_file(
                gh,
                owner=owner,
                repo=repo,
                ref=ref,
                entries=entries,
                fragment=fragment,
            )
            if hinted is not None:
                hinted_path, blob = hinted
                view["hinted_path"] = hinted_path
                view["hinted_blob"] = blob
        payload = _tree_capture_payload(
            owner=owner,
            repo=repo,
            ref=ref,
            path=path,
            entries=entries,
            readme_path=readme_path,
            readme_summary=readme_summary,
        )
        payload = _dict_payload(_canonicalize_capture_value(payload))
        return Capture(
            data=json_bytes(payload),
            name=preferred_name(argv, object()),
            type="application/json",
            meta={
                "projector": "github",
                "github_kind": "tree",
                "github_owner": owner,
                "github_repo": repo,
                "github_ref": ref,
                "github_path": path,
            },
            view=view,
        )
    if match := PULL_RE.match(url):
        owner, repo, number, _ = match.groups()
        payload = gh_json_object(
            gh,
            [
                "pr",
                "view",
                number,
                "--repo",
                f"{owner}/{repo}",
                "--json",
                "title,number,url,state,author,createdAt,updatedAt,body,labels",
            ],
        )
        payload = with_visibility_metadata(payload, provider="github", locator=url)
        payload = _dict_payload(_canonicalize_capture_value(payload))
        return Capture(
            data=json_bytes(
                _object_capture_payload("pr", payload, owner=owner, repo=repo)
            ),
            name=preferred_name(argv, object()),
            type="application/json",
            meta={
                "projector": "github",
                "github_kind": "pr",
                "github_owner": owner,
                "github_repo": repo,
                "source_created_at": str(payload.get("createdAt") or ""),
                "source_updated_at": str(payload.get("updatedAt") or ""),
            },
        )
    if match := ISSUE_RE.match(url):
        owner, repo, number, _ = match.groups()
        payload = gh_json_object(
            gh,
            [
                "issue",
                "view",
                number,
                "--repo",
                f"{owner}/{repo}",
                "--json",
                "title,number,url,state,author,createdAt,updatedAt,body,labels",
            ],
        )
        payload = with_visibility_metadata(payload, provider="github", locator=url)
        payload = _dict_payload(_canonicalize_capture_value(payload))
        return Capture(
            data=json_bytes(
                _object_capture_payload("issue", payload, owner=owner, repo=repo)
            ),
            name=preferred_name(argv, object()),
            type="application/json",
            meta={
                "projector": "github",
                "github_kind": "issue",
                "github_owner": owner,
                "github_repo": repo,
                "source_created_at": str(payload.get("createdAt") or ""),
                "source_updated_at": str(payload.get("updatedAt") or ""),
            },
        )
    if match := COMMIT_RE.match(url):
        owner, repo, sha, _ = match.groups()
        payload = gh_json_object(gh, ["api", f"repos/{owner}/{repo}/commits/{sha}"])
        payload = with_visibility_metadata(payload, provider="github", locator=url)
        payload = _dict_payload(_canonicalize_capture_value(payload))
        commit = _dict_payload(payload.get("commit"))
        author = _dict_payload(commit.get("author"))
        return Capture(
            data=json_bytes(
                _object_capture_payload("commit", payload, owner=owner, repo=repo)
            ),
            name=preferred_name(argv, object()),
            type="application/json",
            meta={
                "projector": "github",
                "github_kind": "commit",
                "github_owner": owner,
                "github_repo": repo,
                "source_created_at": str(author.get("date") or ""),
                "source_updated_at": str(author.get("date") or ""),
            },
        )
    if match := COMMITS_ROOT_RE.match(url):
        owner, repo = match.groups()
        ref = default_branch_name(gh, owner=owner, repo=repo)
        if not ref:
            raise RuntimeError(f"could not determine default branch for {owner}/{repo}")
        url = f"https://github.com/{owner}/{repo}/commits/{ref}"
    if match := COMMITS_RE.match(url):
        owner, repo, ref, extra = match.groups()
        path = normalize_ref_path(extra)
        limit = max(1, min(parsed.limit or 20, 100))
        api_target = (
            f"repos/{owner}/{repo}/commits?sha={urllib.parse.quote(ref, safe='')}"
            f"&per_page={limit}"
        )
        if path:
            api_target += f"&path={urllib.parse.quote(path, safe='/')}"
        raw_payload = gh_json_value(gh, ["api", api_target])
        payload = (
            [item for item in raw_payload if isinstance(item, dict)]
            if isinstance(raw_payload, list)
            else []
        )
        payload = _dict_items(_canonicalize_capture_value(payload))
        authored_dates: list[str] = []
        for item in payload:
            commit = _dict_payload(item.get("commit"))
            author = _dict_payload(commit.get("author"))
            authored_at = str(author.get("date") or "")
            if authored_at:
                authored_dates.append(authored_at)
        return Capture(
            data=json_bytes(
                _object_capture_payload(
                    "commits", payload, owner=owner, repo=repo, ref=ref, path=path
                )
            ),
            name=preferred_name(argv, object()),
            type="application/json",
            meta={
                "projector": "github",
                "github_kind": "commits",
                "github_owner": owner,
                "github_repo": repo,
                "github_ref": ref,
                "github_path": path,
                "source_created_at": min(
                    (value for value in authored_dates if value), default=""
                ),
                "source_updated_at": max(
                    (value for value in authored_dates if value), default=""
                ),
            },
        )
    if match := RELEASE_TAG_RE.match(url):
        owner, repo, tag, _ = match.groups()
        payload = gh_json_object(
            gh, ["api", f"repos/{owner}/{repo}/releases/tags/{tag}"]
        )
        payload = with_visibility_metadata(payload, provider="github", locator=url)
        payload = _dict_payload(_canonicalize_capture_value(payload))
        published = str(payload.get("published_at") or payload.get("created_at") or "")
        return Capture(
            data=json_bytes(
                _object_capture_payload("release", payload, owner=owner, repo=repo)
            ),
            name=preferred_name(argv, object()),
            type="application/json",
            meta={
                "projector": "github",
                "github_kind": "release",
                "github_owner": owner,
                "github_repo": repo,
                "source_created_at": published,
                "source_updated_at": published,
            },
        )
    if match := RELEASES_RE.match(url):
        owner, repo = match.groups()
        raw_payload = gh_json_value(
            gh, ["api", f"repos/{owner}/{repo}/releases?per_page=20"]
        )
        payload = (
            [item for item in raw_payload if isinstance(item, dict)]
            if isinstance(raw_payload, list)
            else []
        )
        payload = _dict_items(_canonicalize_capture_value(payload))
        published = [
            str(item.get("published_at") or item.get("created_at") or "")
            for item in payload
        ]
        return Capture(
            data=json_bytes(
                _object_capture_payload("releases", payload, owner=owner, repo=repo)
            ),
            name=preferred_name(argv, object()),
            type="application/json",
            meta={
                "projector": "github",
                "github_kind": "releases",
                "github_owner": owner,
                "github_repo": repo,
                "source_created_at": min(
                    (value for value in published if value), default=""
                ),
                "source_updated_at": max(
                    (value for value in published if value), default=""
                ),
            },
        )
    if match := REPO_RE.match(url):
        owner, repo = match.groups()
        payload = gh_json_object(
            gh,
            [
                "repo",
                "view",
                f"{owner}/{repo}",
                "--json",
                "name,visibility,defaultBranchRef,url,createdAt,updatedAt,pushedAt",
            ],
        )
        payload = with_visibility_metadata(payload, provider="github", locator=url)
        payload = _dict_payload(_canonicalize_capture_value(payload))
        default_branch_ref = payload.get("defaultBranchRef")
        default_branch = (
            str(default_branch_ref.get("name") or "")
            if isinstance(default_branch_ref, dict)
            else ""
        )
        entries: list[dict[str, object]] = []
        readme_path = ""
        readme_summary = ""
        view: dict[str, object] = {}
        if default_branch:
            entries = list_directory_entries(
                gh, owner=owner, repo=repo, ref=default_branch, path=""
            )
            readme_path, readme_summary = readme_rollup(
                gh,
                owner=owner,
                repo=repo,
                ref=default_branch,
                entries=entries,
                path="",
            )
            if fragment:
                hinted = load_directory_fragment_file(
                    gh,
                    owner=owner,
                    repo=repo,
                    ref=default_branch,
                    entries=entries,
                    fragment=fragment,
                )
                if hinted is not None:
                    hinted_path, blob = hinted
                    view["hinted_path"] = hinted_path
                    view["hinted_blob"] = blob
        capture_payload = _repo_capture_payload(
            payload,
            owner=owner,
            repo=repo,
            ref=default_branch,
            entries=entries,
            readme_path=readme_path,
            readme_summary=readme_summary,
        )
        capture_payload = _canonicalize_capture_value(capture_payload)
        return Capture(
            data=json_bytes(capture_payload),
            name=preferred_name(argv, object()),
            type="application/json",
            meta={
                "projector": "github",
                "github_kind": "repo",
                "github_owner": owner,
                "github_repo": repo,
                "github_ref": default_branch,
                "source_created_at": str(payload.get("createdAt") or ""),
                "source_updated_at": str(
                    payload.get("updatedAt") or payload.get("pushedAt") or ""
                ),
            },
            view=view,
        )
    raise RuntimeError(f"unsupported GitHub URL format: {url}")


def project(argv: list[str], capture: Capture) -> bytes:
    kind = str(capture.meta.get("github_kind") or "").strip()
    if kind == "search":
        payload = json.loads(capture.data.decode("utf-8"))
        parsed = (
            parse_args(argv, emit_help=False)
            if argv
            else ParsedArgs(command="search", output="markdown")
        )
        if parsed.output == "json":
            return pretty_json(capture.data)
        return markdown_search(
            payload, include_details=(parsed.output != "summary")
        ).encode("utf-8")
    owner = str(capture.meta.get("github_owner") or "").strip()
    repo = str(capture.meta.get("github_repo") or "").strip()
    ref = str(capture.meta.get("github_ref") or "").strip()
    path = str(capture.meta.get("github_path") or "").strip()
    if kind == "blob":
        if not argv:
            if looks_text(capture.data):
                return capture.data
            return markdown_binary_blob(
                owner=owner, repo=repo, ref=ref, path=path
            ).encode("utf-8")
        parsed = parse_args(argv, emit_help=False)
        if parsed.output == "json":
            payload = capture.view.get("payload")
            if isinstance(payload, dict):
                return json_bytes(payload)
            return json_bytes(
                _blob_json_payload(path, capture.data, owner=owner, repo=repo, ref=ref)
            )
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
    payload = json.loads(capture.data.decode("utf-8"))
    if kind == "tree":
        entries = payload.get("entries") if isinstance(payload, dict) else []
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
        parsed = parse_args(argv, emit_help=False)
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
        hinted_path = capture.view.get("hinted_path")
        hinted_blob = capture.view.get("hinted_blob")
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
    if kind == "repo":
        repo_payload = payload.get("payload") if isinstance(payload, dict) else {}
        if not isinstance(repo_payload, dict):
            repo_payload = {}
        entries = payload.get("entries") if isinstance(payload, dict) else []
        if not isinstance(entries, list):
            entries = []
        if not argv:
            if capture.view.get("hinted_path") and capture.view.get("hinted_blob"):
                hinted_path = str(capture.view["hinted_path"])
                hinted_blob = capture.view["hinted_blob"]
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
        parsed = parse_args(argv, emit_help=False)
        if parsed.output == "json":
            return pretty_json(capture.data)
        if parsed.output == "summary":
            return markdown_repo(repo_payload).encode("utf-8")
        if capture.view.get("hinted_path") and capture.view.get("hinted_blob"):
            hinted_path = str(capture.view["hinted_path"])
            hinted_blob = capture.view["hinted_blob"]
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
        parsed = (
            parse_args(argv, emit_help=False)
            if argv
            else ParsedArgs(command="render", output="markdown")
        )
        object_payload = (
            payload.get("payload") if isinstance(payload, dict) else payload
        )
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
    return capture.data


def main(argv: list[str]) -> int:
    parsed = parse_args(argv)
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
        gh = ensure_gh()
        ensure_gh_auth(gh)
    except RuntimeError as exc:
        return die(str(exc), code=1)
    if parsed.command == "search":
        try:
            if parsed.search_type == "repo":
                payload = search_repositories_payload(
                    gh,
                    query=parsed.query,
                    repo=parsed.repo,
                    limit=_search_limit(parsed.limit),
                    global_search=parsed.global_search,
                )
            elif parsed.search_type == "code":
                payload = search_code_payload(
                    gh,
                    query=parsed.query,
                    repo=parsed.repo,
                    limit=_search_limit(parsed.limit),
                    global_search=parsed.global_search,
                    filename=parsed.filename,
                    extension=parsed.extension,
                    language=parsed.language,
                    match=parsed.match,
                )
            else:
                payload = search_issueish_payload(
                    gh,
                    query=parsed.query,
                    repo=parsed.repo,
                    limit=_search_limit(parsed.limit),
                    search_type=parsed.search_type,
                    global_search=parsed.global_search,
                )
        except RuntimeError as exc:
            return die(str(exc), code=1)
        if parsed.output == "json":
            render_bytes(
                json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json"
            )
            return 0
        emit_markdown(
            markdown_search(payload, include_details=(parsed.output == "markdown"))
        )
        return 0
    url, fragment = split_render_url(parsed.url)
    if match := ACTIONS_JOB_RE.match(url):
        if parsed.limit is not None:
            return unsupported_render_limit_error()
        owner, repo, run_id, job_id, _ = match.groups()
        try:
            payload = workflow_job_payload(
                gh,
                owner=owner,
                repo=repo,
                run_id=run_id,
                job_id=job_id,
            )
        except RuntimeError as exc:
            return die(str(exc), code=1)
        if parsed.output in {"summary", "markdown"}:
            emit_markdown(
                markdown_workflow_job(
                    payload,
                    owner=owner,
                    repo=repo,
                    include_steps=(parsed.output == "markdown"),
                )
            )
            return 0
        render_bytes(
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json"
        )
        return 0
    if match := ACTIONS_RUN_RE.match(url):
        if parsed.limit is not None:
            return unsupported_render_limit_error()
        owner, repo, run_id, _ = match.groups()
        try:
            payload = workflow_run_payload(gh, owner=owner, repo=repo, run_id=run_id)
        except RuntimeError as exc:
            return die(str(exc), code=1)
        if parsed.output in {"summary", "markdown"}:
            emit_markdown(
                markdown_workflow_run(
                    payload,
                    owner=owner,
                    repo=repo,
                    include_jobs=(parsed.output == "markdown"),
                )
            )
            return 0
        render_bytes(
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json"
        )
        return 0
    if match := BLOB_RE.match(url):
        if parsed.limit is not None:
            return unsupported_render_limit_error()
        owner, repo, ref, path = match.groups()
        path = normalize_ref_path(path)
        try:
            payload = fetch_content_file(gh, owner=owner, repo=repo, ref=ref, path=path)
        except RuntimeError as exc:
            if not is_readme_path(path):
                return die(str(exc), code=1)
            parent_path = str(Path(path).parent).replace("\\", "/").strip(".")
            try:
                readme = load_directory_readme(
                    gh,
                    owner=owner,
                    repo=repo,
                    ref=ref,
                    path=parent_path,
                )
            except RuntimeError as nested_exc:
                return die(str(nested_exc), code=1)
            if readme is None:
                return die(str(exc), code=1)
            fallback_path, blob = readme
            if parsed.output == "json":
                fallback_payload = {
                    "path": fallback_path,
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(blob).decode("ascii"),
                    "size": len(blob),
                }
                render_bytes(
                    json.dumps(fallback_payload, indent=2, sort_keys=True).encode(
                        "utf-8"
                    ),
                    "json",
                )
                return 0
            if parsed.output == "summary":
                emit_markdown(
                    markdown_text_blob_summary(
                        owner=owner,
                        repo=repo,
                        ref=ref,
                        path=fallback_path,
                        payload={"size": len(blob)},
                    )
                )
                return 0
            render_content(blob, fallback_path)
            return 0
        if parsed.output == "json":
            render_bytes(
                json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json"
            )
            return 0
        if parsed.output == "summary":
            emit_markdown(
                markdown_text_blob_summary(
                    owner=owner,
                    repo=repo,
                    ref=ref,
                    path=path,
                    payload=payload,
                )
            )
            return 0
        try:
            blob = decode_content_blob(payload)
        except RuntimeError as exc:
            return die(str(exc), code=1)
        if looks_text(blob):
            render_content(blob, path)
            return 0
        emit_markdown(markdown_binary_blob(owner=owner, repo=repo, ref=ref, path=path))
        return 0
    if match := TREE_RE.match(url):
        if parsed.limit is not None:
            return unsupported_render_limit_error()
        owner, repo, ref, path = match.groups()
        path = normalize_ref_path(path)
        try:
            entries = list_directory_entries(
                gh, owner=owner, repo=repo, ref=ref, path=path
            )
        except RuntimeError as exc:
            return die(str(exc), code=1)
        if len(entries) == 1 and str(entries[0].get("type") or "") == "file":
            payload = entries[0]
            if parsed.output == "json":
                render_bytes(
                    json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
                    "json",
                )
                return 0
            file_path = str(payload.get("path") or path)
            if parsed.output == "summary":
                emit_markdown(
                    markdown_text_blob_summary(
                        owner=owner,
                        repo=repo,
                        ref=ref,
                        path=file_path,
                        payload=payload,
                    )
                )
                return 0
            try:
                blob = decode_content_blob(payload)
            except RuntimeError as exc:
                return die(str(exc), code=1)
            if looks_text(blob):
                render_content(blob, file_path)
                return 0
            emit_markdown(
                markdown_binary_blob(owner=owner, repo=repo, ref=ref, path=file_path)
            )
            return 0
        if parsed.output == "json":
            render_bytes(
                json.dumps(entries, indent=2, sort_keys=True).encode("utf-8"), "json"
            )
            return 0
        if parsed.output == "summary":
            emit_markdown(
                markdown_directory(
                    owner=owner, repo=repo, ref=ref, path=path, entries=entries
                )
            )
            return 0
        if fragment:
            try:
                hinted = load_directory_fragment_file(
                    gh,
                    owner=owner,
                    repo=repo,
                    ref=ref,
                    entries=entries,
                    fragment=fragment,
                )
            except RuntimeError as exc:
                return die(str(exc), code=1)
            if hinted is not None:
                hinted_path, blob = hinted
                render_content(blob, hinted_path)
                return 0
        readme_path, readme_summary = readme_rollup(
            gh,
            owner=owner,
            repo=repo,
            ref=ref,
            entries=entries,
            path=path,
        )
        emit_markdown(
            markdown_directory(
                owner=owner,
                repo=repo,
                ref=ref,
                path=path,
                entries=entries,
                readme_path=readme_path,
                readme_summary=readme_summary,
            )
        )
        return 0
    if match := PULL_RE.match(url):
        if parsed.limit is not None:
            return unsupported_render_limit_error()
        owner, repo, number, _ = match.groups()
        try:
            payload = gh_json_object(
                gh,
                [
                    "pr",
                    "view",
                    number,
                    "--repo",
                    f"{owner}/{repo}",
                    "--json",
                    "title,number,url,state,author,createdAt,updatedAt,body,labels",
                ],
            )
        except RuntimeError as exc:
            return die(str(exc), code=1)
        payload = with_visibility_metadata(payload, provider="github", locator=url)
        if parsed.output in {"summary", "markdown"}:
            emit_markdown(
                markdown_issue_or_pr(
                    payload,
                    "pull request",
                    include_body=(parsed.output == "markdown"),
                )
            )
            return 0
        render_bytes(
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json"
        )
        return 0
    if match := ISSUE_RE.match(url):
        if parsed.limit is not None:
            return unsupported_render_limit_error()
        owner, repo, number, _ = match.groups()
        try:
            payload = gh_json_object(
                gh,
                [
                    "issue",
                    "view",
                    number,
                    "--repo",
                    f"{owner}/{repo}",
                    "--json",
                    "title,number,url,state,author,createdAt,updatedAt,body,labels",
                ],
            )
        except RuntimeError as exc:
            return die(str(exc), code=1)
        payload = with_visibility_metadata(payload, provider="github", locator=url)
        if parsed.output == "markdown":
            emit_markdown(markdown_issue_or_pr(payload, "issue", include_body=True))
            return 0
        if parsed.output == "summary":
            emit_markdown(markdown_issue_or_pr(payload, "issue", include_body=False))
            return 0
        render_bytes(
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json"
        )
        return 0
    if match := COMMIT_RE.match(url):
        if parsed.limit is not None:
            return unsupported_render_limit_error()
        owner, repo, sha, _ = match.groups()
        try:
            payload = gh_json_object(gh, ["api", f"repos/{owner}/{repo}/commits/{sha}"])
        except RuntimeError as exc:
            return die(str(exc), code=1)
        payload = with_visibility_metadata(payload, provider="github", locator=url)
        if parsed.output in {"summary", "markdown"}:
            emit_markdown(
                markdown_commit(
                    payload,
                    owner=owner,
                    repo=repo,
                    include_patch=(parsed.output == "markdown"),
                )
            )
            return 0
        render_bytes(
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json"
        )
        return 0
    if match := COMMITS_ROOT_RE.match(url):
        owner, repo = match.groups()
        try:
            ref = default_branch_name(gh, owner=owner, repo=repo)
        except RuntimeError as exc:
            return die(str(exc), code=1)
        if not ref:
            return die(f"could not determine default branch for {owner}/{repo}", code=1)
        url = f"https://github.com/{owner}/{repo}/commits/{ref}"
    if match := COMMITS_RE.match(url):
        owner, repo, ref, extra = match.groups()
        path = normalize_ref_path(extra)
        limit = max(1, min(parsed.limit or 20, 100))
        api_target = (
            f"repos/{owner}/{repo}/commits?sha={urllib.parse.quote(ref, safe='')}"
            f"&per_page={limit}"
        )
        if path:
            api_target += f"&path={urllib.parse.quote(path, safe='/')}"
        try:
            raw_payload = gh_json_value(
                gh,
                ["api", api_target],
            )
        except RuntimeError as exc:
            message = str(exc)
            if "404" in message or "Not Found" in message:
                try:
                    default_branch = default_branch_name(gh, owner=owner, repo=repo)
                except RuntimeError:
                    default_branch = ""
                if default_branch and default_branch != ref:
                    default_history = (
                        f"https://github.com/{owner}/{repo}/commits/{default_branch}"
                    )
                    head_history = f"https://github.com/{owner}/{repo}/commits/HEAD"
                    if path:
                        default_history = f"{default_history}/{path}"
                        head_history = f"{head_history}/{path}"
                    fallback = f"https://github.com/{owner}/{repo}/commits"
                    guidance = (
                        f"GitHub commit-history ref `{ref}` was not found for {owner}/{repo}. "
                        f"Specific `/commits/<ref>` URLs require a real branch, tag, or commit-ish. "
                    )
                    if path:
                        guidance += f"Try `{head_history}` or `{default_history}`."
                    else:
                        guidance += f"Try `{fallback}`, `{head_history}`, or `{default_history}`."
                    return die(guidance, code=1)
            return die(message, code=1)
        payload = (
            [item for item in raw_payload if isinstance(item, dict)]
            if isinstance(raw_payload, list)
            else []
        )
        if parsed.output in {"summary", "markdown"}:
            emit_markdown(
                markdown_commit_list(
                    payload, owner=owner, repo=repo, ref=ref, path=path
                )
            )
            return 0
        render_bytes(
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json"
        )
        return 0
    if match := RELEASE_TAG_RE.match(url):
        if parsed.limit is not None:
            return unsupported_render_limit_error()
        owner, repo, tag, _ = match.groups()
        try:
            payload = gh_json_object(
                gh, ["api", f"repos/{owner}/{repo}/releases/tags/{tag}"]
            )
        except RuntimeError as exc:
            return die(str(exc), code=1)
        payload = with_visibility_metadata(payload, provider="github", locator=url)
        if parsed.output in {"summary", "markdown"}:
            emit_markdown(markdown_release(payload, owner=owner, repo=repo))
            return 0
        render_bytes(
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json"
        )
        return 0
    if match := RELEASES_RE.match(url):
        if parsed.limit is not None:
            return unsupported_render_limit_error()
        owner, repo = match.groups()
        try:
            raw_payload = gh_json_value(
                gh, ["api", f"repos/{owner}/{repo}/releases?per_page=20"]
            )
        except RuntimeError as exc:
            return die(str(exc), code=1)
        payload = (
            [item for item in raw_payload if isinstance(item, dict)]
            if isinstance(raw_payload, list)
            else []
        )
        if parsed.output == "markdown":
            emit_markdown(
                markdown_release_list(owner=owner, repo=repo, payload=payload)
            )
            return 0
        if parsed.output == "summary":
            emit_markdown(
                markdown_release_list(owner=owner, repo=repo, payload=payload[:10])
            )
            return 0
        render_bytes(
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json"
        )
        return 0
    if match := REPO_RE.match(url):
        if parsed.limit is not None:
            return unsupported_render_limit_error()
        owner, repo = match.groups()
        try:
            payload = gh_json_object(
                gh,
                [
                    "repo",
                    "view",
                    f"{owner}/{repo}",
                    "--json",
                    "name,visibility,defaultBranchRef,url,createdAt,updatedAt,pushedAt",
                ],
            )
        except RuntimeError as exc:
            return die(str(exc), code=1)
        payload = with_visibility_metadata(payload, provider="github", locator=url)
        if parsed.output == "summary":
            emit_markdown(markdown_repo(payload))
            return 0
        if parsed.output == "markdown":
            default_branch_ref = payload.get("defaultBranchRef")
            default_branch = ""
            if isinstance(default_branch_ref, dict):
                default_branch = str(default_branch_ref.get("name") or "")
            if default_branch:
                try:
                    entries = list_directory_entries(
                        gh,
                        owner=owner,
                        repo=repo,
                        ref=default_branch,
                        path="",
                    )
                except RuntimeError as exc:
                    return die(str(exc), code=1)
                if fragment:
                    try:
                        hinted = load_directory_fragment_file(
                            gh,
                            owner=owner,
                            repo=repo,
                            ref=default_branch,
                            entries=entries,
                            fragment=fragment,
                        )
                    except RuntimeError as exc:
                        return die(str(exc), code=1)
                    if hinted is not None:
                        hinted_path, blob = hinted
                        render_content(blob, hinted_path)
                        return 0
                readme_path, readme_summary = readme_rollup(
                    gh,
                    owner=owner,
                    repo=repo,
                    ref=default_branch,
                    entries=entries,
                    path="",
                )
                emit_markdown(
                    markdown_repo_directory(
                        payload,
                        owner=owner,
                        repo=repo,
                        ref=default_branch,
                        entries=entries,
                        readme_path=readme_path,
                        readme_summary=readme_summary,
                    )
                )
                return 0
            emit_markdown(markdown_repo(payload))
            return 0
        render_bytes(
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json"
        )
        return 0
    return die(f"unsupported GitHub URL format: {url}")


if __name__ == "__main__":
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    raise SystemExit(main(sys.argv[1:]))
