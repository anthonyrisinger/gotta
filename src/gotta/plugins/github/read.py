"""GitHub read-surface payload and content loaders."""

from __future__ import annotations

import base64
from pathlib import Path
import re
import urllib.parse

from gotta.source.visibility import with_visibility_metadata

from .api import gh_json_object, gh_json_value


README_CANDIDATES = (
    "readme.md",
    "readme.markdown",
    "readme.mdown",
    "readme.mkd",
    "readme",
    "readme.txt",
    "readme.rst",
)

WORKFLOW_RUN_JSON_FIELDS = (
    "attempt,conclusion,createdAt,displayTitle,event,headBranch,headSha,jobs,"
    "name,number,startedAt,status,updatedAt,url,workflowDatabaseId,workflowName"
)


def workflow_run_payload(
    gh: str, *, owner: str, repo: str, run_id: str
) -> dict[str, object]:
    payload = gh_json_object(
        gh,
        [
            "run",
            "view",
            run_id,
            "--repo",
            f"{owner}/{repo}",
            "--json",
            WORKFLOW_RUN_JSON_FIELDS,
        ],
    )
    return with_visibility_metadata(
        payload,
        provider="github",
        locator=github_actions_run_url(owner, repo, run_id),
    )


def workflow_job_payload(
    gh: str,
    *,
    owner: str,
    repo: str,
    run_id: str,
    job_id: str,
) -> dict[str, object]:
    payload = gh_json_object(gh, ["api", f"repos/{owner}/{repo}/actions/jobs/{job_id}"])
    payload_run_id = str(payload.get("run_id") or "").strip()
    if payload_run_id and payload_run_id != run_id:
        raise RuntimeError(
            f"GitHub Actions job `{job_id}` belongs to run `{payload_run_id}`, "
            f"not `{run_id}`."
        )
    return with_visibility_metadata(
        payload,
        provider="github",
        locator=github_actions_job_url(owner, repo, run_id, job_id),
    )


def decode_content_blob(payload: dict[str, object]) -> bytes:
    content = str(payload.get("content") or "").replace("\n", "")
    encoding = str(payload.get("encoding") or "")
    if encoding != "base64":
        raise RuntimeError("GitHub API returned content without base64 encoding")
    try:
        return base64.b64decode(content)
    except ValueError as exc:
        raise RuntimeError(f"invalid base64 blob from GitHub API: {exc}") from exc


def normalize_fragment_hint(fragment: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", fragment.casefold())


def split_render_url(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    base = urllib.parse.urlunparse(parsed._replace(query="", fragment=""))
    return base, parsed.fragment


def normalize_ref_path(path: str | None) -> str:
    if not path:
        return ""
    return path.split("#", 1)[0].split("?", 1)[0].strip("/")


def github_tree_url(owner: str, repo: str, ref: str, path: str = "") -> str:
    if path:
        return f"https://github.com/{owner}/{repo}/tree/{ref}/{path}"
    return f"https://github.com/{owner}/{repo}/tree/{ref}"


def github_blob_url(owner: str, repo: str, ref: str, path: str) -> str:
    return f"https://github.com/{owner}/{repo}/blob/{ref}/{path}"


def github_actions_run_url(owner: str, repo: str, run_id: str) -> str:
    return f"https://github.com/{owner}/{repo}/actions/runs/{run_id}"


def github_actions_job_url(owner: str, repo: str, run_id: str, job_id: str) -> str:
    return f"{github_actions_run_url(owner, repo, run_id)}/job/{job_id}"


def list_directory_entries(
    gh: str,
    *,
    owner: str,
    repo: str,
    ref: str,
    path: str = "",
) -> list[dict[str, object]]:
    target = f"repos/{owner}/{repo}/contents"
    if path:
        target += f"/{path}"
    target += f"?ref={ref}"
    payload = gh_json_value(gh, ["api", target])
    if isinstance(payload, dict):
        if str(payload.get("type") or "") == "file":
            return [payload]
        raise RuntimeError("GitHub API returned unexpected directory payload")
    if not isinstance(payload, list):
        raise RuntimeError("GitHub API returned unexpected directory payload")
    entries: list[dict[str, object]] = []
    for item in payload:
        if isinstance(item, dict):
            entries.append(item)
    return entries


def find_readme_entry(entries: list[dict[str, object]]) -> dict[str, object] | None:
    ranked = {name: index for index, name in enumerate(README_CANDIDATES)}
    best: tuple[int, dict[str, object]] | None = None
    for entry in entries:
        if str(entry.get("type") or "") != "file":
            continue
        name = str(entry.get("name") or "")
        key = name.casefold()
        if key not in ranked:
            continue
        score = ranked[key]
        if best is None or score < best[0]:
            best = (score, entry)
    return None if best is None else best[1]


def fetch_content_file(
    gh: str,
    *,
    owner: str,
    repo: str,
    ref: str,
    path: str,
) -> dict[str, object]:
    payload = gh_json_object(
        gh, ["api", f"repos/{owner}/{repo}/contents/{path}?ref={ref}"]
    )
    if str(payload.get("type") or "") != "file":
        raise RuntimeError("GitHub API returned a non-file payload")
    return payload


def is_readme_path(path: str) -> bool:
    return Path(path).name.casefold() in README_CANDIDATES


def load_directory_readme(
    gh: str,
    *,
    owner: str,
    repo: str,
    ref: str,
    path: str = "",
    entries: list[dict[str, object]] | None = None,
) -> tuple[str, bytes] | None:
    if entries is None:
        entries = list_directory_entries(gh, owner=owner, repo=repo, ref=ref, path=path)
    readme_entry = find_readme_entry(entries)
    if readme_entry is None:
        return None
    readme_path = str(readme_entry.get("path") or "")
    payload = fetch_content_file(gh, owner=owner, repo=repo, ref=ref, path=readme_path)
    return readme_path, decode_content_blob(payload)


def resolve_directory_fragment_entry(
    entries: list[dict[str, object]],
    *,
    fragment: str,
) -> dict[str, object] | None:
    hint = normalize_fragment_hint(fragment)
    if not hint:
        return None
    if hint == "readme":
        return find_readme_entry(entries)
    matches: list[dict[str, object]] = []
    for entry in entries:
        if str(entry.get("type") or "") != "file":
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        tokens = {
            normalize_fragment_hint(name),
            normalize_fragment_hint(Path(name).stem),
        }
        if hint in tokens:
            matches.append(entry)
    if len(matches) != 1:
        return None
    return matches[0]


def load_directory_fragment_file(
    gh: str,
    *,
    owner: str,
    repo: str,
    ref: str,
    entries: list[dict[str, object]],
    fragment: str,
) -> tuple[str, bytes] | None:
    entry = resolve_directory_fragment_entry(entries, fragment=fragment)
    if entry is None:
        return None
    path = str(entry.get("path") or "")
    if not path:
        return None
    payload = fetch_content_file(gh, owner=owner, repo=repo, ref=ref, path=path)
    return path, decode_content_blob(payload)


def readme_excerpt(data: bytes, *, limit: int = 240) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and lines[0].startswith("#"):
        lines = lines[1:]
    if not lines:
        return ""
    excerpt = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if len(excerpt) <= limit:
        return excerpt
    return excerpt[: limit - 1].rstrip() + "…"


def readme_rollup(
    gh: str,
    *,
    owner: str,
    repo: str,
    ref: str,
    entries: list[dict[str, object]],
    path: str = "",
) -> tuple[str, str]:
    entry = find_readme_entry(entries)
    if entry is None:
        return "", ""
    readme_path = str(entry.get("path") or "")
    if not readme_path:
        return "", ""
    try:
        loaded = load_directory_readme(
            gh,
            owner=owner,
            repo=repo,
            ref=ref,
            path=path,
            entries=entries,
        )
    except RuntimeError:
        return readme_path, ""
    if loaded is None:
        return readme_path, ""
    _, blob = loaded
    return readme_path, readme_excerpt(blob)
