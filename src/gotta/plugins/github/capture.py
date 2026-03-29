"""GitHub capture synthesis."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
import mimetypes
from pathlib import Path
from typing import Any
import urllib.parse

from gotta.capture import Capture, json_bytes
from gotta.source.visibility import with_visibility_metadata

from .parse import ParsedArgs
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
)


@dataclass(frozen=True, slots=True)
class CaptureDeps:
    parse_args: Callable[..., ParsedArgs]
    preferred_name: Callable[[list[str], Any], str]
    ensure_gh: Callable[[], str]
    ensure_gh_auth: Callable[[str], None]
    default_branch_name: Callable[..., str]
    gh_json_object: Callable[[str, list[str]], dict[str, object]]
    gh_json_value: Callable[[str, list[str]], object]
    looks_text: Callable[[bytes], bool]
    search_repositories_payload: Callable[..., dict[str, object]]
    search_issueish_payload: Callable[..., dict[str, object]]
    search_code_payload: Callable[..., dict[str, object]]
    split_render_url: Callable[[str], tuple[str, str]]
    workflow_run_payload: Callable[..., dict[str, object]]
    workflow_job_payload: Callable[..., dict[str, object]]
    decode_content_blob: Callable[[dict[str, object]], bytes]
    fetch_content_file: Callable[..., dict[str, object]]
    is_readme_path: Callable[[str], bool]
    list_directory_entries: Callable[..., list[dict[str, object]]]
    load_directory_fragment_file: Callable[..., tuple[str, bytes] | None]
    load_directory_readme: Callable[..., tuple[str, bytes] | None]
    normalize_ref_path: Callable[[str | None], str]
    readme_rollup: Callable[..., tuple[str, str]]
    github_blob_url: Callable[[str, str, str, str], str]


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


def _blob_content_type(path: str, data: bytes, *, deps: CaptureDeps) -> str:
    guessed, _encoding = mimetypes.guess_type(path)
    if guessed:
        return guessed
    return "text/plain" if deps.looks_text(data) else "application/octet-stream"


def _blob_json_payload(
    path: str,
    data: bytes,
    *,
    owner: str,
    repo: str,
    ref: str,
    deps: CaptureDeps,
) -> dict[str, object]:
    return {
        "path": path,
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
        "size": len(data),
        "url": deps.github_blob_url(owner, repo, ref, path),
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


def _capture_search(
    argv: list[str],
    parsed: ParsedArgs,
    *,
    deps: CaptureDeps,
    options: Any,
) -> Capture:
    gh = deps.ensure_gh()
    deps.ensure_gh_auth(gh)
    if parsed.search_type == "repo":
        payload = deps.search_repositories_payload(
            gh,
            query=parsed.query,
            repo=parsed.repo,
            limit=_search_limit(parsed.limit),
            global_search=parsed.global_search,
        )
    elif parsed.search_type == "code":
        payload = deps.search_code_payload(
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
        payload = deps.search_issueish_payload(
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
        preferred_name=deps.preferred_name(argv, options),
        content_type="application/json",
        metadata={
            "projector": "github",
            "github_kind": "search",
        },
    )


def _capture_workflow_job(
    argv: list[str],
    *,
    gh: str,
    owner: str,
    repo: str,
    run_id: str,
    job_id: str,
    deps: CaptureDeps,
    options: Any,
) -> Capture:
    payload = deps.workflow_job_payload(
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
        preferred_name=deps.preferred_name(argv, options),
        content_type="application/json",
        metadata={
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


def _capture_workflow_run(
    argv: list[str],
    *,
    gh: str,
    owner: str,
    repo: str,
    run_id: str,
    deps: CaptureDeps,
    options: Any,
) -> Capture:
    payload = deps.workflow_run_payload(gh, owner=owner, repo=repo, run_id=run_id)
    payload = _dict_payload(_canonicalize_capture_value(payload))
    return Capture(
        data=json_bytes(
            _object_capture_payload("workflow_run", payload, owner=owner, repo=repo)
        ),
        preferred_name=deps.preferred_name(argv, options),
        content_type="application/json",
        metadata={
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


def _capture_blob(
    argv: list[str],
    *,
    gh: str,
    owner: str,
    repo: str,
    ref: str,
    path: str,
    deps: CaptureDeps,
    options: Any,
) -> Capture:
    try:
        payload = deps.fetch_content_file(
            gh, owner=owner, repo=repo, ref=ref, path=path
        )
        blob = deps.decode_content_blob(payload)
    except RuntimeError as exc:
        if not deps.is_readme_path(path):
            raise RuntimeError(str(exc)) from exc
        parent_path = str(Path(path).parent).replace("\\", "/").strip(".")
        readme = deps.load_directory_readme(
            gh, owner=owner, repo=repo, ref=ref, path=parent_path
        )
        if readme is None:
            raise RuntimeError(str(exc)) from exc
        path, blob = readme
        payload = _blob_json_payload(
            path,
            blob,
            owner=owner,
            repo=repo,
            ref=ref,
            deps=deps,
        )
    payload = _dict_payload(_canonicalize_capture_value(payload))
    return Capture(
        data=blob,
        preferred_name=Path(path).name or "github.bin",
        content_type=_blob_content_type(path, blob, deps=deps),
        metadata={
            "projector": "github",
            "github_kind": "blob",
            "github_owner": owner,
            "github_repo": repo,
            "github_ref": ref,
            "github_path": path,
            "source_created_at": "",
            "source_updated_at": "",
        },
        view_data={"payload": payload},
    )


def _capture_tree(
    argv: list[str],
    *,
    gh: str,
    owner: str,
    repo: str,
    ref: str,
    path: str,
    fragment: str,
    deps: CaptureDeps,
    options: Any,
) -> Capture:
    entries = deps.list_directory_entries(
        gh, owner=owner, repo=repo, ref=ref, path=path
    )
    if len(entries) == 1 and str(entries[0].get("type") or "") == "file":
        file_path = str(entries[0].get("path") or path)
        payload = _dict_payload(_canonicalize_capture_value(entries[0]))
        blob = deps.decode_content_blob(payload)
        return Capture(
            data=blob,
            preferred_name=Path(file_path).name or "github.bin",
            content_type=_blob_content_type(file_path, blob, deps=deps),
            metadata={
                "projector": "github",
                "github_kind": "blob",
                "github_owner": owner,
                "github_repo": repo,
                "github_ref": ref,
                "github_path": file_path,
            },
            view_data={"payload": payload},
        )
    readme_path, readme_summary = deps.readme_rollup(
        gh,
        owner=owner,
        repo=repo,
        ref=ref,
        entries=entries,
        path=path,
    )
    view: dict[str, object] = {}
    if fragment:
        hinted = deps.load_directory_fragment_file(
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
        preferred_name=deps.preferred_name(argv, options),
        content_type="application/json",
        metadata={
            "projector": "github",
            "github_kind": "tree",
            "github_owner": owner,
            "github_repo": repo,
            "github_ref": ref,
            "github_path": path,
        },
        view_data=view,
    )


def _capture_pull_or_issue(
    argv: list[str],
    *,
    gh: str,
    owner: str,
    repo: str,
    number: str,
    kind: str,
    url: str,
    deps: CaptureDeps,
    options: Any,
) -> Capture:
    verb = "pr" if kind == "pr" else "issue"
    payload = deps.gh_json_object(
        gh,
        [
            verb,
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
        data=json_bytes(_object_capture_payload(kind, payload, owner=owner, repo=repo)),
        preferred_name=deps.preferred_name(argv, options),
        content_type="application/json",
        metadata={
            "projector": "github",
            "github_kind": kind,
            "github_owner": owner,
            "github_repo": repo,
            "source_created_at": str(payload.get("createdAt") or ""),
            "source_updated_at": str(payload.get("updatedAt") or ""),
        },
    )


def _capture_commit(
    argv: list[str],
    *,
    gh: str,
    owner: str,
    repo: str,
    sha: str,
    url: str,
    deps: CaptureDeps,
    options: Any,
) -> Capture:
    payload = deps.gh_json_object(gh, ["api", f"repos/{owner}/{repo}/commits/{sha}"])
    payload = with_visibility_metadata(payload, provider="github", locator=url)
    payload = _dict_payload(_canonicalize_capture_value(payload))
    commit = _dict_payload(payload.get("commit"))
    author = _dict_payload(commit.get("author"))
    return Capture(
        data=json_bytes(
            _object_capture_payload("commit", payload, owner=owner, repo=repo)
        ),
        preferred_name=deps.preferred_name(argv, options),
        content_type="application/json",
        metadata={
            "projector": "github",
            "github_kind": "commit",
            "github_owner": owner,
            "github_repo": repo,
            "source_created_at": str(author.get("date") or ""),
            "source_updated_at": str(author.get("date") or ""),
        },
    )


def _capture_commits(
    argv: list[str],
    *,
    gh: str,
    owner: str,
    repo: str,
    ref: str,
    path: str,
    limit: int,
    deps: CaptureDeps,
    options: Any,
) -> Capture:
    api_target = (
        f"repos/{owner}/{repo}/commits?sha={urllib.parse.quote(ref, safe='')}"
        f"&per_page={limit}"
    )
    if path:
        api_target += f"&path={urllib.parse.quote(path, safe='/')}"
    try:
        raw_payload = deps.gh_json_value(gh, ["api", api_target])
    except RuntimeError as exc:
        message = str(exc)
        if "404" in message or "Not Found" in message:
            try:
                default_branch = deps.default_branch_name(gh, owner=owner, repo=repo)
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
                    "Specific `/commits/<ref>` URLs require a real branch, tag, or commit-ish. "
                )
                if path:
                    guidance += f"Try `{head_history}` or `{default_history}`."
                else:
                    guidance += (
                        f"Try `{fallback}`, `{head_history}`, or `{default_history}`."
                    )
                raise RuntimeError(guidance) from exc
        raise RuntimeError(message) from exc
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
        preferred_name=deps.preferred_name(argv, options),
        content_type="application/json",
        metadata={
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


def _capture_release(
    argv: list[str],
    *,
    gh: str,
    owner: str,
    repo: str,
    tag: str,
    url: str,
    deps: CaptureDeps,
    options: Any,
) -> Capture:
    payload = deps.gh_json_object(
        gh, ["api", f"repos/{owner}/{repo}/releases/tags/{tag}"]
    )
    payload = with_visibility_metadata(payload, provider="github", locator=url)
    payload = _dict_payload(_canonicalize_capture_value(payload))
    published = str(payload.get("published_at") or payload.get("created_at") or "")
    return Capture(
        data=json_bytes(
            _object_capture_payload("release", payload, owner=owner, repo=repo)
        ),
        preferred_name=deps.preferred_name(argv, options),
        content_type="application/json",
        metadata={
            "projector": "github",
            "github_kind": "release",
            "github_owner": owner,
            "github_repo": repo,
            "source_created_at": published,
            "source_updated_at": published,
        },
    )


def _capture_releases(
    argv: list[str],
    *,
    gh: str,
    owner: str,
    repo: str,
    deps: CaptureDeps,
    options: Any,
) -> Capture:
    raw_payload = deps.gh_json_value(
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
        preferred_name=deps.preferred_name(argv, options),
        content_type="application/json",
        metadata={
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


def _capture_repo(
    argv: list[str],
    *,
    gh: str,
    owner: str,
    repo: str,
    url: str,
    fragment: str,
    deps: CaptureDeps,
    options: Any,
) -> Capture:
    payload = deps.gh_json_object(
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
        entries = deps.list_directory_entries(
            gh, owner=owner, repo=repo, ref=default_branch, path=""
        )
        readme_path, readme_summary = deps.readme_rollup(
            gh,
            owner=owner,
            repo=repo,
            ref=default_branch,
            entries=entries,
            path="",
        )
        if fragment:
            hinted = deps.load_directory_fragment_file(
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
    capture_payload = _dict_payload(_canonicalize_capture_value(capture_payload))
    return Capture(
        data=json_bytes(capture_payload),
        preferred_name=deps.preferred_name(argv, options),
        content_type="application/json",
        metadata={
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
        view_data=view,
    )


def _capture_render(
    argv: list[str],
    parsed: ParsedArgs,
    *,
    deps: CaptureDeps,
    options: Any,
) -> Capture:
    gh = deps.ensure_gh()
    deps.ensure_gh_auth(gh)
    url, fragment = deps.split_render_url(parsed.url)
    if match := ACTIONS_JOB_RE.match(url):
        owner, repo, run_id, job_id, _ = match.groups()
        return _capture_workflow_job(
            argv,
            gh=gh,
            owner=owner,
            repo=repo,
            run_id=run_id,
            job_id=job_id,
            deps=deps,
            options=options,
        )
    if match := ACTIONS_RUN_RE.match(url):
        owner, repo, run_id, _ = match.groups()
        return _capture_workflow_run(
            argv,
            gh=gh,
            owner=owner,
            repo=repo,
            run_id=run_id,
            deps=deps,
            options=options,
        )
    if match := BLOB_RE.match(url):
        owner, repo, ref, path = match.groups()
        return _capture_blob(
            argv,
            gh=gh,
            owner=owner,
            repo=repo,
            ref=ref,
            path=deps.normalize_ref_path(path),
            deps=deps,
            options=options,
        )
    if match := TREE_RE.match(url):
        owner, repo, ref, path = match.groups()
        return _capture_tree(
            argv,
            gh=gh,
            owner=owner,
            repo=repo,
            ref=ref,
            path=deps.normalize_ref_path(path),
            fragment=fragment,
            deps=deps,
            options=options,
        )
    if match := PULL_RE.match(url):
        owner, repo, number, _ = match.groups()
        return _capture_pull_or_issue(
            argv,
            gh=gh,
            owner=owner,
            repo=repo,
            number=number,
            kind="pr",
            url=url,
            deps=deps,
            options=options,
        )
    if match := ISSUE_RE.match(url):
        owner, repo, number, _ = match.groups()
        return _capture_pull_or_issue(
            argv,
            gh=gh,
            owner=owner,
            repo=repo,
            number=number,
            kind="issue",
            url=url,
            deps=deps,
            options=options,
        )
    if match := COMMIT_RE.match(url):
        owner, repo, sha, _ = match.groups()
        return _capture_commit(
            argv,
            gh=gh,
            owner=owner,
            repo=repo,
            sha=sha,
            url=url,
            deps=deps,
            options=options,
        )
    if match := COMMITS_ROOT_RE.match(url):
        owner, repo = match.groups()
        ref = deps.default_branch_name(gh, owner=owner, repo=repo)
        if not ref:
            raise RuntimeError(f"could not determine default branch for {owner}/{repo}")
        url = f"https://github.com/{owner}/{repo}/commits/{ref}"
    if match := COMMITS_RE.match(url):
        owner, repo, ref, extra = match.groups()
        return _capture_commits(
            argv,
            gh=gh,
            owner=owner,
            repo=repo,
            ref=ref,
            path=deps.normalize_ref_path(extra),
            limit=max(1, min(parsed.limit or 20, 100)),
            deps=deps,
            options=options,
        )
    if match := RELEASE_TAG_RE.match(url):
        owner, repo, tag, _ = match.groups()
        return _capture_release(
            argv,
            gh=gh,
            owner=owner,
            repo=repo,
            tag=tag,
            url=url,
            deps=deps,
            options=options,
        )
    if match := RELEASES_RE.match(url):
        owner, repo = match.groups()
        return _capture_releases(
            argv,
            gh=gh,
            owner=owner,
            repo=repo,
            deps=deps,
            options=options,
        )
    if match := REPO_RE.match(url):
        owner, repo = match.groups()
        return _capture_repo(
            argv,
            gh=gh,
            owner=owner,
            repo=repo,
            url=url,
            fragment=fragment,
            deps=deps,
            options=options,
        )
    raise RuntimeError(f"unsupported GitHub URL format: {url}")


def capture(argv: list[str], options: Any, *, deps: CaptureDeps) -> Capture:
    parsed = deps.parse_args(argv, emit_help=False)
    if parsed.command == "search":
        return _capture_search(argv, parsed, deps=deps, options=options)
    if parsed.command != "render":
        raise NotImplementedError("github capture does not support this command")
    return _capture_render(argv, parsed, deps=deps, options=options)
