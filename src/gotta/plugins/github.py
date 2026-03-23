#!/usr/bin/env python3
"""Render common GitHub URLs through the GitHub CLI."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Any
import urllib.parse

from gotta.capture import Capture, json_bytes
from gotta.helptext import is_long_help_request
from gotta.project import pretty_json
from gotta.routing import query_route, split_locator_tail, strip_http_url_fragment
from gotta.source import (
    derive_source_metadata_from_payload,
    render_source_metadata_lines,
    render_visibility_metadata_lines,
    with_visibility_metadata,
)


BLOB_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")
TREE_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)(?:/(.*))?$")
PULL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/([0-9]+)(/.*)?$")
ISSUE_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/issues/([0-9]+)(/.*)?$")
COMMIT_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/commit/([0-9a-f]{7,40})(/.*)?$")
COMMITS_ROOT_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/commits/?$")
COMMITS_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/commits/([^/]+)(/.*)?$")
RELEASES_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/releases/?$")
RELEASE_TAG_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/releases/tag/([^/]+)(/.*)?$")
REPO_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/?$")
README_CANDIDATES = (
    "readme.md",
    "readme.markdown",
    "readme.mdown",
    "readme.mkd",
    "readme",
    "readme.txt",
    "readme.rst",
)

USAGE = """usage: gotta github [status [--output json|summary] | search [--global] [--type repo|issue|pr|code] [--repo owner/repo] [--filename NAME] [--extension EXT] [--language LANG] [--match file|path] [--limit N] [--output json|summary|markdown] <query...> | [--output json|summary|markdown] [--limit N] <github_url>]

Supported URL shapes:
  - repository root
  - blob URLs
  - tree URLs
  - pull request URLs
  - issue URLs
  - commit URLs
  - commit history URLs (`/commits` resolves through the repo default branch; `/commits/HEAD` follows the current tip; `/commits/<ref>` requires an existing ref)
  - release list URLs
  - release tag URLs

Output:
  markdown  body/content rendering (default)
  summary   metadata-first terminal summary
  json      raw-ish structured payload from the GitHub CLI / API

URL list shaping:
  --limit N applies only to commit history URLs (`/commits`, `/commits/HEAD`,
            or `/commits/<ref>`)

Search:
  repo      repository discovery (default)
  issue     issue search
  pr        pull request search
  code      code and file discovery

Unscoped search is owned-scope by default: the authenticated user plus visible
organizations only. Use `--global` to search the wider GitHub corpus while
excluding those owned-scope results.

Use --help-all for the same long-form usage output.
"""

SEARCH_USAGE = """usage: gotta github search [--global] [--type repo|issue|pr|code] [--repo owner/repo] [--filename NAME] [--extension EXT] [--language LANG] [--match file|path] [--limit N] [--output json|summary|markdown] <query...>

Search:
  default             owned-scope only (authenticated user + visible orgs)
  --global            wider GitHub corpus, excluding owned-scope hits
  --type repo         repository discovery (default)
  --type issue        issue search
  --type pr           pull request search
  --type code         code and file discovery
  --repo owner/repo   narrow search to one repository
  --filename NAME     code search only; narrow by filename
  --extension EXT     code search only; narrow by extension
  --language LANG     code search only; narrow by language
  --match file|path   code search only; restrict matches to content or path

Examples:
  gotta github search ABC
  gotta github search --global ABC
  gotta github search --type pr --repo acme/widgets auth proxy
  gotta github search --type code --repo acme/widgets --filename package.json lint
"""


@dataclass(frozen=True)
class ParsedArgs:
    command: str
    output: str
    url: str = ""
    fragment: str = ""
    query: str = ""
    search_type: str = "repo"
    repo: str = ""
    filename: str = ""
    extension: str = ""
    language: str = ""
    match: str = ""
    limit: int | None = None
    global_search: bool = False


def _render_visibility_payload(payload: dict[str, object]) -> dict[str, object]:
    return with_visibility_metadata(dict(payload), provider="github")


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower())
    return normalized.strip("-") or "github"


def _normalize_search_locator_tail(parsed: ParsedArgs) -> list[str]:
    args = ["search"]
    if parsed.global_search and not parsed.repo:
        args.append("--global")
    if parsed.search_type != "repo":
        args.extend(["--type", parsed.search_type])
    if parsed.repo:
        args.extend(["--repo", parsed.repo])
    if parsed.filename:
        args.extend(["--filename", parsed.filename])
    if parsed.extension:
        args.extend(["--extension", parsed.extension])
    if parsed.language:
        args.extend(["--language", parsed.language])
    if parsed.match:
        args.extend(["--match", parsed.match])
    args.append(parsed.query)
    return args


def _canonical_search_locator(parsed: ParsedArgs) -> str:
    return "github:" + " ".join(_normalize_search_locator_tail(parsed))


def _search_route(rest: str) -> list[str] | None:
    if rest.startswith("search "):
        tail = rest.removeprefix("search ").strip()
    else:
        return None
    if not tail:
        return None
    return query_route(
        "search",
        " ".join(split_locator_tail(tail)),
        valued_flags=(
            "--type",
            "--repo",
            "--filename",
            "--extension",
            "--language",
            "--match",
            "--limit",
            "--output",
        ),
        boolean_flags=("--global",),
    )


def _supported_url_route(target: str) -> list[str] | None:
    normalized = strip_http_url_fragment(target)
    if any(char.isspace() for char in target):
        return None
    for pattern in (
        BLOB_RE,
        TREE_RE,
        PULL_RE,
        ISSUE_RE,
        COMMIT_RE,
        COMMITS_ROOT_RE,
        COMMITS_RE,
        RELEASE_TAG_RE,
        RELEASES_RE,
        REPO_RE,
    ):
        if pattern.match(normalized):
            return [target]
    return None


def route_target(target: str) -> list[str] | None:
    if target.startswith("https://github.com/"):
        return _supported_url_route(target)
    if target.startswith("github:"):
        rest = target.removeprefix("github:")
        search_args = _search_route(rest)
        if search_args is not None:
            return search_args
        if rest.startswith("github.com/"):
            routed = _supported_url_route(f"https://{rest}")
            if routed is None:
                return None
            return [f"https://{rest}"]
    return None


def _preferred_render_name(parsed: ParsedArgs, extension: str) -> str:
    def render_name(base: str) -> str:
        suffix = f".{extension}"
        return base if base.endswith(suffix) else f"{base}{suffix}"

    parsed_url = urllib.parse.urlparse(parsed.url.split("#", 1)[0].split("?", 1)[0])
    parts = [part for part in parsed_url.path.split("/") if part]
    if len(parts) >= 2:
        repo = _slug(parts[1])
        if len(parts) == 2:
            return render_name(repo)
        if len(parts) >= 4 and parts[2] == "pull" and parts[3].isdigit():
            suffix = f"{repo}-pr-{parts[3]}"
            if len(parts) >= 5 and parts[4] == "commits":
                suffix = f"{suffix}-commits"
            return render_name(suffix)
        if len(parts) >= 4 and parts[2] == "issues" and parts[3].isdigit():
            return render_name(f"{repo}-issue-{parts[3]}")
        if len(parts) >= 4 and parts[2] == "commit":
            return render_name(f"{repo}-commit-{_slug(parts[3])}")
        if len(parts) >= 3 and parts[2] == "commits":
            suffix = f"{repo}-commits"
            if len(parts) >= 4:
                suffix = f"{suffix}-{_slug(parts[3])}"
            if len(parts) >= 5:
                suffix = f"{suffix}-{_slug('/'.join(parts[4:]))}"
            return render_name(suffix)
        if len(parts) >= 4 and parts[2] == "tree":
            suffix = f"{repo}-tree-{_slug(parts[3])}"
            if len(parts) >= 5:
                suffix = f"{suffix}-{_slug('/'.join(parts[4:]))}"
            return render_name(suffix)
        if len(parts) >= 5 and parts[2] == "blob":
            return render_name(f"{repo}-blob-{_slug(parts[3])}-{_slug('/'.join(parts[4:]))}")
        if len(parts) >= 3 and parts[2] == "releases":
            if len(parts) >= 5 and parts[3] == "tag":
                return render_name(f"{repo}-release-{_slug(parts[4])}")
            return render_name(f"{repo}-releases")
    name = Path(parsed_url.path.rstrip("/")).name or "github"
    return render_name(name)


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def _parse_limit_value(args: list[str], index: int, *, context: str) -> tuple[int, int]:
    if index + 1 >= len(args):
        raise SystemExit(die(f"{context} requires an integer after `--limit`", code=2))
    try:
        value = max(1, min(int(args[index + 1]), 100))
    except ValueError:
        raise SystemExit(die(f"{context} requires an integer after `--limit`", code=2)) from None
    return value, index + 2


def _unsupported_render_limit_error() -> int:
    return die(
        "`--limit` is only supported for GitHub commit-history URLs "
        "(`/commits`, `/commits/HEAD`, or `/commits/<ref>`). "
        "Use `/commits` or `/commits/HEAD` for the canonical branch-agnostic forms.",
        code=2,
    )


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def guess_lang_from_path(path: str) -> str:
    basename = Path(path).name.lower()
    if basename in {"readme", "readme.md", "readme.markdown", "readme.mdown", "readme.mkd"}:
        return "markdown"
    suffix = Path(path).suffix.lower()
    return {
        ".html": "html",
        ".htm": "html",
        ".md": "markdown",
        ".markdown": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".xml": "xml",
        ".css": "css",
        ".js": "javascript",
        ".toml": "toml",
        ".sh": "bash",
        ".py": "python",
        ".go": "go",
        ".tf": "hcl",
        ".tfvars": "hcl",
    }.get(suffix, "txt")


def render_file(path: Path, language: str = "txt") -> None:
    bat = shutil.which("bat")
    if bat and sys.stdout.isatty():
        env = os.environ.copy()
        env.setdefault("BAT_THEME", "ansi")
        result = subprocess.run(
            [
                bat,
                "--paging=never",
                "--style=plain",
                f"--language={language}",
                "--italic-text=always",
                str(path),
            ],
            check=False,
            env=env,
        )
        if result.returncode == 0:
            return
    with path.open("rb") as handle:
        shutil.copyfileobj(handle, sys.stdout.buffer)


def render_bytes(data: bytes, language: str = "txt") -> None:
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        tmp_path = Path(handle.name)
        handle.write(data)
    try:
        render_file(tmp_path, language)
    finally:
        tmp_path.unlink(missing_ok=True)


def emit_markdown(text: str) -> None:
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


def render_content(data: bytes, path: str) -> None:
    render_bytes(data, guess_lang_from_path(path))


def ensure_gh() -> str:
    gh = shutil.which("gh")
    if gh:
        return gh
    raise RuntimeError("missing required dependency: gh")


def ensure_gh_auth(gh: str) -> None:
    status = subprocess.run([gh, "auth", "status"], check=False, capture_output=True)
    if status.returncode == 0:
        return
    if not is_interactive():
        raise RuntimeError("gh is not authenticated. run: gh auth login -w")
    print("GitHub CLI is not authenticated; starting web login...", file=sys.stderr)
    login = subprocess.run([gh, "auth", "login", "-w"], check=False)
    if login.returncode != 0:
        raise RuntimeError("gh auth login failed")


def gh_json(gh: str, args: list[str]) -> bytes:
    env = os.environ.copy()
    env["GH_PAGER"] = "cat"
    proc = subprocess.run([gh, *args], check=False, capture_output=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip() or "gh command failed")
    return proc.stdout


def looks_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def gh_json_object(gh: str, args: list[str]) -> dict[str, object]:
    payload = gh_json_value(gh, args)
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub CLI returned unexpected JSON payload")
    return payload


def gh_json_value(gh: str, args: list[str]) -> object:
    return json.loads(gh_json(gh, args))


def gh_status_payload() -> dict[str, object]:
    gh = shutil.which("gh")
    payload: dict[str, object] = {
        "ghPath": gh or "",
        "ghPresent": bool(gh),
        "authenticated": False,
    }
    if not gh:
        return payload
    proc = subprocess.run(
        [gh, "auth", "status"],
        check=False,
        capture_output=True,
        text=True,
    )
    payload["authenticated"] = proc.returncode == 0
    detail = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    payload["detail"] = detail
    return payload


def default_branch_name(gh: str, *, owner: str, repo: str) -> str:
    payload = gh_json_object(gh, ["api", f"repos/{owner}/{repo}"])
    return str(payload.get("default_branch") or "").strip()


def markdown_repo(payload: dict[str, object]) -> str:
    payload = _render_visibility_payload(payload)
    name = str(payload.get("name") or "")
    url = str(payload.get("url") or "")
    visibility = str(payload.get("visibility") or "")
    created = str(payload.get("createdAt") or "")
    updated = str(payload.get("updatedAt") or "")
    pushed = str(payload.get("pushedAt") or "")
    default_branch = ""
    default_branch_ref = payload.get("defaultBranchRef")
    if isinstance(default_branch_ref, dict):
        default_branch = str(default_branch_ref.get("name") or "")
    lines = [f"# {name}", ""]
    if url:
        lines.append(f"- **URL:** {url}")
    lines.extend(render_visibility_metadata_lines(payload))
    if visibility:
        lines.append(f"- **Visibility:** {visibility}")
    if created:
        lines.append(f"- **Created:** {created}")
    if updated:
        lines.append(f"- **Updated:** {updated}")
    if pushed:
        lines.append(f"- **Pushed:** {pushed}")
    if default_branch:
        lines.append(f"- **Default branch:** `{default_branch}`")
    return "\n".join(lines) + "\n"


def markdown_issue_or_pr(
    payload: dict[str, object], kind: str, *, include_body: bool
) -> str:
    payload = _render_visibility_payload(payload)
    title = str(payload.get("title") or "")
    number = payload.get("number")
    url = str(payload.get("url") or "")
    state = str(payload.get("state") or "")
    created = str(payload.get("createdAt") or "")
    updated = str(payload.get("updatedAt") or "")
    body = str(payload.get("body") or "").strip()
    author_name = ""
    author = payload.get("author")
    if isinstance(author, dict):
        author_name = str(author.get("login") or author.get("name") or "")
    labels = []
    raw_labels = payload.get("labels")
    if isinstance(raw_labels, list):
        for label in raw_labels:
            if isinstance(label, dict):
                name = str(label.get("name") or "")
                if name:
                    labels.append(name)
    heading = f"# {kind.capitalize()} #{number}: {title}" if number else f"# {title}"
    lines = [heading, ""]
    if url:
        lines.append(f"- **URL:** {url}")
    lines.extend(render_visibility_metadata_lines(payload))
    if state:
        lines.append(f"- **State:** {state}")
    if author_name:
        lines.append(f"- **Author:** `{author_name}`")
    if created:
        lines.append(f"- **Created:** {created}")
    if updated:
        lines.append(f"- **Updated:** {updated}")
    if labels:
        lines.append(f"- **Labels:** {', '.join(f'`{label}`' for label in labels)}")
    if include_body and body:
        lines.extend(["", "## Body", "", body])
    return "\n".join(lines) + "\n"


def markdown_commit(
    payload: dict[str, object],
    *,
    owner: str,
    repo: str,
    include_patch: bool,
) -> str:
    payload = _render_visibility_payload(payload)
    sha = str(payload.get("sha") or "")
    html_url = str(payload.get("html_url") or "")
    commit = payload.get("commit") if isinstance(payload.get("commit"), dict) else {}
    message = str(commit.get("message") or "").strip()
    author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    authored = str(author.get("date") or "")
    author_name = str(author.get("name") or "")
    files = payload.get("files") if isinstance(payload.get("files"), list) else []
    subject = message.splitlines()[0] if message else sha[:12]
    lines = [f"# {owner}/{repo} commit {sha[:12]}: {subject}", ""]
    if html_url:
        lines.append(f"- **URL:** {html_url}")
    lines.extend(render_visibility_metadata_lines(payload))
    if sha:
        lines.append(f"- **SHA:** `{sha}`")
    if author_name:
        lines.append(f"- **Author:** `{author_name}`")
    if authored:
        lines.append(f"- **Authored:** {authored}")
    lines.append(f"- **Files changed:** {len(files)}")
    if message:
        lines.extend(["", "## Message", "", message])
    if files:
        lines.extend(["", "## Files", ""])
        for file_payload in files:
            if not isinstance(file_payload, dict):
                continue
            filename = str(file_payload.get("filename") or "")
            status = str(file_payload.get("status") or "")
            additions = file_payload.get("additions")
            deletions = file_payload.get("deletions")
            changes = file_payload.get("changes")
            parts = [f"- `{filename}`"] if filename else ["- file"]
            details: list[str] = []
            if status:
                details.append(status)
            if isinstance(additions, int):
                details.append(f"+{additions}")
            if isinstance(deletions, int):
                details.append(f"-{deletions}")
            if isinstance(changes, int):
                details.append(f"{changes} changes")
            if details:
                parts.append(f"({' / '.join(details)})")
            lines.append(" ".join(parts))
            if include_patch:
                patch = str(file_payload.get("patch") or "").strip()
                if patch:
                    lines.extend(["", "```diff", patch, "```"])
    return "\n".join(lines) + "\n"


def markdown_commit_list(
    payload: list[dict[str, object]],
    *,
    owner: str,
    repo: str,
    ref: str,
    path: str = "",
) -> str:
    lines = [f"# {owner}/{repo} commit history for `{ref}`", ""]
    history_url = f"https://github.com/{owner}/{repo}/commits/{ref}"
    if path:
        history_url = f"{history_url}/{path}"
        lines.append(f"- **Path:** `{path}`")
    lines.append(f"- **URL:** {history_url}")
    if not payload:
        lines.append("_No commits returned._")
        return "\n".join(lines) + "\n"
    authored_dates = [
        str(item.get("commit", {}).get("author", {}).get("date") or "")
        for item in payload
        if isinstance(item, dict)
        and isinstance(item.get("commit"), dict)
        and isinstance(item.get("commit", {}).get("author"), dict)
        and str(item.get("commit", {}).get("author", {}).get("date") or "")
    ]
    if authored_dates:
        lines.append(f"- **Created:** {min(authored_dates)}")
        lines.append(f"- **Updated:** {max(authored_dates)}")
        lines.append(f"- **Commits shown:** {len(authored_dates)}")
        lines.append("")
    for item in payload:
        sha = str(item.get("sha") or "")[:7]
        html_url = str(item.get("html_url") or "")
        commit = item.get("commit")
        if not isinstance(commit, dict):
            commit = {}
        message = str(commit.get("message") or "").strip().splitlines()[0] if commit else ""
        author = commit.get("author")
        if not isinstance(author, dict):
            author = {}
        author_name = str(author.get("name") or "")
        authored_at = str(author.get("date") or "")
        summary = f"[{sha}]({html_url})" if html_url and sha else (sha or html_url or "commit")
        if message:
            summary = f"{summary} {message}"
        lines.append(f"- {summary}")
        details: list[str] = []
        if authored_at:
            details.append(f"authored {authored_at}")
        if author_name:
            details.append(f"by {author_name}")
        if details:
            lines.append(f"  - {'; '.join(details)}")
    return "\n".join(lines) + "\n"


def markdown_release(payload: dict[str, object], *, owner: str, repo: str) -> str:
    name = str(payload.get("name") or payload.get("tag_name") or "")
    url = str(payload.get("html_url") or "")
    tag = str(payload.get("tag_name") or "")
    published = str(payload.get("published_at") or payload.get("created_at") or "")
    draft = bool(payload.get("draft"))
    prerelease = bool(payload.get("prerelease"))
    body = str(payload.get("body") or "").strip()
    lines = [f"# {owner}/{repo}: {name or tag or 'release'}", ""]
    if url:
        lines.append(f"- **URL:** {url}")
    if tag:
        lines.append(f"- **Tag:** `{tag}`")
    if published:
        lines.append(f"- **Published:** {published}")
    lines.append(f"- **Draft:** {str(draft).lower()}")
    lines.append(f"- **Prerelease:** {str(prerelease).lower()}")
    if body:
        lines.extend(["", "## Notes", "", body])
    return "\n".join(lines) + "\n"


def markdown_release_list(
    *,
    owner: str,
    repo: str,
    payload: list[dict[str, object]],
) -> str:
    lines = [f"# {owner}/{repo} Releases", ""]
    for item in payload:
        name = str(item.get("name") or item.get("tag_name") or "")
        tag = str(item.get("tag_name") or "")
        url = str(item.get("html_url") or "")
        published = str(item.get("published_at") or item.get("created_at") or "")
        label = name or tag or "release"
        if url:
            lines.append(f"- [{label}]({url})")
        else:
            lines.append(f"- {label}")
        if tag and tag != label:
            lines.append(f"  - tag: `{tag}`")
        if published:
            lines.append(f"  - published: {published}")
    return "\n".join(lines) + "\n"


def _search_type_label(search_type: str) -> str:
    return {
        "repo": "Repositories",
        "issue": "Issues",
        "pr": "Pull Requests",
    }.get(search_type, "Results")


def _effective_search_query(*, query: str, repo: str) -> str:
    parts: list[str] = []
    if repo:
        parts.append(f"repo:{repo}")
    if query.strip():
        parts.append(query.strip())
    return " ".join(parts).strip()


def _normalize_repo_search_item(item: dict[str, object]) -> dict[str, object]:
    owner = item.get("owner")
    if not isinstance(owner, dict):
        owner = {}
    return with_visibility_metadata(
        {
            "kind": "repo",
            "fullName": str(item.get("full_name") or ""),
            "name": str(item.get("name") or ""),
            "url": str(item.get("html_url") or ""),
            "description": str(item.get("description") or "").strip(),
            "language": str(item.get("language") or ""),
            "stars": int(item.get("stargazers_count") or 0),
            "owner": str(owner.get("login") or ""),
            "createdAt": str(item.get("created_at") or ""),
            "updatedAt": str(item.get("updated_at") or ""),
            "pushedAt": str(item.get("pushed_at") or ""),
            "defaultBranch": str(item.get("default_branch") or ""),
            "visibility": str(item.get("visibility") or ""),
        },
        provider="github",
    )


def _issue_repository_name(item: dict[str, object]) -> str:
    repository_url = str(item.get("repository_url") or "")
    match = re.search(r"/repos/([^/]+/[^/]+)$", repository_url)
    return match.group(1) if match else ""


def _normalize_issue_search_item(item: dict[str, object]) -> dict[str, object]:
    user = item.get("user")
    if not isinstance(user, dict):
        user = {}
    labels = item.get("labels")
    label_names: list[str] = []
    if isinstance(labels, list):
        for label in labels:
            if isinstance(label, dict):
                name = str(label.get("name") or "").strip()
                if name:
                    label_names.append(name)
    is_pr = isinstance(item.get("pull_request"), dict)
    return with_visibility_metadata(
        {
            "kind": "pr" if is_pr else "issue",
            "title": str(item.get("title") or ""),
            "number": item.get("number"),
            "url": str(item.get("html_url") or ""),
            "state": str(item.get("state") or ""),
            "author": str(user.get("login") or ""),
            "repository": _issue_repository_name(item),
            "createdAt": str(item.get("created_at") or ""),
            "updatedAt": str(item.get("updated_at") or ""),
            "body": str(item.get("body") or "").strip(),
            "labels": label_names,
        },
        provider="github",
    )


def _normalize_code_search_item(item: dict[str, object]) -> dict[str, object]:
    repository = item.get("repository")
    if not isinstance(repository, dict):
        repository = {}
    text_matches = item.get("textMatches")
    fragments: list[str] = []
    if isinstance(text_matches, list):
        for entry in text_matches:
            if not isinstance(entry, dict):
                continue
            fragment = str(entry.get("fragment") or "").strip()
            if fragment:
                fragments.append(fragment)
    return with_visibility_metadata(
        {
            "kind": "code",
            "repository": str(repository.get("nameWithOwner") or ""),
            "repositoryUrl": str(repository.get("url") or ""),
            "path": str(item.get("path") or ""),
            "sha": str(item.get("sha") or ""),
            "url": str(item.get("url") or ""),
            "textMatches": fragments,
            "repositoryVisibility": str(repository.get("visibility") or ""),
        },
        provider="github",
    )


def _code_search_item_owner(item: dict[str, object]) -> str:
    repository = item.get("repository")
    if not isinstance(repository, dict):
        return ""
    name_with_owner = str(repository.get("nameWithOwner") or "").strip()
    if "/" not in name_with_owner:
        return ""
    return name_with_owner.split("/", 1)[0]


def _search_page_size(limit: int, *, repo: str) -> int:
    if repo:
        return min(max(limit, 1), 50)
    return min(max(limit * 5, 20), 50)


def _viewer_login(gh: str) -> str:
    payload = gh_json_object(gh, ["api", "user"])
    return str(payload.get("login") or "").strip()


def _viewer_org_logins(gh: str) -> list[str]:
    payload = gh_json_value(gh, ["api", "user/orgs?per_page=100"])
    if not isinstance(payload, list):
        return []
    owners: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        login = str(item.get("login") or "").strip()
        if login:
            owners.append(login)
    seen: set[str] = set()
    ordered: list[str] = []
    for owner in owners:
        folded = owner.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        ordered.append(owner)
    return ordered


def _accessible_owner_targets(gh: str) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    try:
        viewer = _viewer_login(gh)
        if viewer:
            targets.append(("user", viewer))
        viewer_folded = viewer.casefold()
        for org in _viewer_org_logins(gh):
            if org.casefold() == viewer_folded:
                continue
            targets.append(("org", org))
    except RuntimeError:
        return []
    return targets


def _search_api_payload(
    gh: str,
    *,
    endpoint: str,
    query: str,
    per_page: int,
) -> dict[str, object]:
    return gh_json_object(
        gh,
        [
            "api",
            f"{endpoint}?q={urllib.parse.quote(query, safe=':/')}&per_page={per_page}",
        ],
    )


def _search_item_identity(item: dict[str, object], *, search_type: str) -> str:
    if search_type == "repo":
        return str(item.get("html_url") or item.get("full_name") or item.get("name") or "")
    return str(item.get("html_url") or item.get("url") or "")


def _collect_owner_scoped_search_items(
    gh: str,
    *,
    endpoint: str,
    query: str,
    targets: list[tuple[str, str]],
    limit: int,
    search_type: str,
) -> tuple[list[dict[str, object]], int]:
    if not targets:
        return [], 0
    per_owner = min(max(limit, 5), 20)
    items: list[dict[str, object]] = []
    total_count = 0
    seen: set[str] = set()
    for qualifier, owner in targets:
        scoped_query = f"{query} {qualifier}:{owner}"
        payload = _search_api_payload(
            gh,
            endpoint=endpoint,
            query=scoped_query,
            per_page=per_owner,
        )
        total_count += int(payload.get("total_count") or 0)
        raw_items = payload.get("items")
        candidates = (
            [item for item in raw_items if isinstance(item, dict)]
            if isinstance(raw_items, list)
            else []
        )
        for item in candidates:
            identity = _search_item_identity(item, search_type=search_type)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            items.append(item)
            if len(items) >= limit:
                return items, total_count
    return items, total_count


def _search_item_owner(item: dict[str, object], *, search_type: str) -> str:
    if search_type == "repo":
        owner = item.get("owner")
        if isinstance(owner, dict):
            return str(owner.get("login") or "").strip()
        return ""
    if search_type == "code":
        return _code_search_item_owner(item)
    repository = _issue_repository_name(item)
    if "/" in repository:
        return repository.split("/", 1)[0].strip()
    return ""


def _exclude_owner_items(
    items: list[dict[str, object]],
    *,
    excluded_owners: set[str],
    search_type: str,
) -> list[dict[str, object]]:
    if not items or not excluded_owners:
        return items
    return [
        item
        for item in items
        if _search_item_owner(item, search_type=search_type).casefold() not in excluded_owners
    ]


def search_repositories_payload(
    gh: str,
    *,
    query: str,
    repo: str,
    limit: int,
    global_search: bool,
) -> dict[str, object]:
    effective_query = _effective_search_query(query=query, repo=repo)
    accessible_targets = [] if repo else _accessible_owner_targets(gh)
    accessible_owners = {login.casefold() for _, login in accessible_targets}
    scoped_items: list[dict[str, object]] = []
    scoped_total = 0
    if accessible_targets and not global_search:
        scoped_items, scoped_total = _collect_owner_scoped_search_items(
            gh,
            endpoint="search/repositories",
            query=query,
            targets=accessible_targets,
            limit=limit,
            search_type="repo",
        )
    global_items: list[dict[str, object]] = []
    global_total = 0
    if repo or global_search:
        payload = _search_api_payload(
            gh,
            endpoint="search/repositories",
            query=effective_query,
            per_page=_search_page_size(limit, repo=repo),
        )
        global_total = int(payload.get("total_count") or 0)
        raw_items = payload.get("items")
        global_items = (
            [item for item in raw_items if isinstance(item, dict)]
            if isinstance(raw_items, list)
            else []
        )
        if global_search and not repo:
            global_items = _exclude_owner_items(
                global_items,
                excluded_owners=accessible_owners,
                search_type="repo",
            )
    items = scoped_items if not (repo or global_search) else global_items
    results = [_normalize_repo_search_item(item) for item in items[:limit]]
    if repo:
        search_plan = "repo-scope"
    elif global_search:
        search_plan = "global-excluding-owned"
    else:
        search_plan = "owned-only"
    return with_visibility_metadata(
        {
            "surface": "github",
            "type": "repo",
            "query": query,
            "effectiveQuery": effective_query,
            "scopeRepo": repo,
            "searchPlan": search_plan,
            "accessibleOwners": sorted(accessible_owners),
            "scopedResultCount": len(scoped_items[:limit]),
            "globalResultCount": len(global_items[:limit]),
            "scopedTotalCount": scoped_total,
            "globalTotalCount": global_total,
            "totalCount": scoped_total if not (repo or global_search) else global_total,
            "resultCount": len(results),
            "results": results,
        },
        provider="github",
        subcommand="search",
    )


def search_issueish_payload(
    gh: str,
    *,
    query: str,
    repo: str,
    limit: int,
    search_type: str,
    global_search: bool,
) -> dict[str, object]:
    qualifier = "is:pr" if search_type == "pr" else "is:issue"
    effective_query = _effective_search_query(query=f"{qualifier} {query}", repo=repo)
    accessible_targets = [] if repo else _accessible_owner_targets(gh)
    accessible_owners = {login.casefold() for _, login in accessible_targets}
    scoped_items: list[dict[str, object]] = []
    scoped_total = 0
    if accessible_targets and not global_search:
        scoped_items, scoped_total = _collect_owner_scoped_search_items(
            gh,
            endpoint="search/issues",
            query=f"{qualifier} {query}",
            targets=accessible_targets,
            limit=limit,
            search_type=search_type,
        )
    global_items: list[dict[str, object]] = []
    global_total = 0
    if repo or global_search:
        payload = _search_api_payload(
            gh,
            endpoint="search/issues",
            query=effective_query,
            per_page=_search_page_size(limit, repo=repo),
        )
        global_total = int(payload.get("total_count") or 0)
        raw_items = payload.get("items")
        global_items = (
            [item for item in raw_items if isinstance(item, dict)]
            if isinstance(raw_items, list)
            else []
        )
        if global_search and not repo:
            global_items = _exclude_owner_items(
                global_items,
                excluded_owners=accessible_owners,
                search_type=search_type,
            )
    items = scoped_items if not (repo or global_search) else global_items
    results = [_normalize_issue_search_item(item) for item in items[:limit]]
    if repo:
        search_plan = "repo-scope"
    elif global_search:
        search_plan = "global-excluding-owned"
    else:
        search_plan = "owned-only"
    return with_visibility_metadata(
        {
            "surface": "github",
            "type": search_type,
            "query": query,
            "effectiveQuery": effective_query,
            "scopeRepo": repo,
            "searchPlan": search_plan,
            "accessibleOwners": sorted(accessible_owners),
            "scopedResultCount": len(scoped_items[:limit]),
            "globalResultCount": len(global_items[:limit]),
            "scopedTotalCount": scoped_total,
            "globalTotalCount": global_total,
            "totalCount": scoped_total if not (repo or global_search) else global_total,
            "resultCount": len(results),
            "results": results,
        },
        provider="github",
        subcommand="search",
    )


def _code_search_cli_args(
    *,
    query: str,
    limit: int,
    repo: str = "",
    owner: str = "",
    filename: str = "",
    extension: str = "",
    language: str = "",
    match: str = "",
) -> list[str]:
    args = [
        "search",
        "code",
        query,
        "--json",
        "path,repository,sha,textMatches,url",
        "--limit",
        str(limit),
    ]
    if repo:
        args.extend(["--repo", repo])
    if owner:
        args.extend(["--owner", owner])
    if filename:
        args.extend(["--filename", filename])
    if extension:
        args.extend(["--extension", extension])
    if language:
        args.extend(["--language", language])
    if match:
        args.extend(["--match", match])
    return args


def _code_search_items(
    gh: str,
    *,
    query: str,
    limit: int,
    repo: str = "",
    owner: str = "",
    filename: str = "",
    extension: str = "",
    language: str = "",
    match: str = "",
) -> list[dict[str, object]]:
    payload = gh_json_value(
        gh,
        _code_search_cli_args(
            query=query,
            limit=limit,
            repo=repo,
            owner=owner,
            filename=filename,
            extension=extension,
            language=language,
            match=match,
        ),
    )
    if not isinstance(payload, list):
        raise RuntimeError("GitHub CLI returned unexpected code search payload")
    return [item for item in payload if isinstance(item, dict)]


def search_code_payload(
    gh: str,
    *,
    query: str,
    repo: str,
    limit: int,
    global_search: bool,
    filename: str = "",
    extension: str = "",
    language: str = "",
    match: str = "",
) -> dict[str, object]:
    accessible_targets = [] if repo else _accessible_owner_targets(gh)
    accessible_owners = {login.casefold() for _, login in accessible_targets}
    scoped_items: list[dict[str, object]] = []
    if accessible_targets and not global_search:
        seen: set[str] = set()
        for qualifier, owner in accessible_targets:
            if qualifier != "user" and qualifier != "org":
                continue
            candidates = _code_search_items(
                gh,
                query=query,
                limit=limit,
                owner=owner,
                filename=filename,
                extension=extension,
                language=language,
                match=match,
            )
            for item in candidates:
                identity = str(item.get("url") or "")
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                scoped_items.append(item)
                if len(scoped_items) >= limit:
                    break
            if len(scoped_items) >= limit:
                break
    global_items: list[dict[str, object]] = []
    if repo or global_search:
        global_items = _code_search_items(
            gh,
            query=query,
            limit=limit,
            repo=repo,
            filename=filename,
            extension=extension,
            language=language,
            match=match,
        )
        if global_search and not repo:
            global_items = _exclude_owner_items(
                global_items,
                excluded_owners=accessible_owners,
                search_type="code",
            )
    items = scoped_items if not (repo or global_search) else global_items
    results = [_normalize_code_search_item(item) for item in items[:limit]]
    if repo:
        search_plan = "repo-scope"
    elif global_search:
        search_plan = "global-excluding-owned"
    else:
        search_plan = "owned-only"
    return with_visibility_metadata(
        {
            "surface": "github",
            "type": "code",
            "query": query,
            "scopeRepo": repo,
            "filename": filename,
            "extension": extension,
            "language": language,
            "match": match,
            "searchPlan": search_plan,
            "accessibleOwners": sorted(accessible_owners),
            "scopedResultCount": len(scoped_items[:limit]),
            "globalResultCount": len(global_items[:limit]),
            "resultCount": len(results),
            "results": results,
        },
        provider="github",
        subcommand="search",
    )


def markdown_search(payload: dict[str, object], *, include_details: bool) -> str:
    payload = _render_visibility_payload(payload)
    search_type = str(payload.get("type") or "repo")
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return (
            f"### GitHub Search: {payload.get('query') or ''}\n\n"
            f"- _Type_: `{search_type}`\n"
            "- _Matches_: 0\n\n"
            "No GitHub results matched.\n"
        )
    lines: list[str] = [
        f"### GitHub Search: {payload.get('query') or ''}",
        "",
        "- _Surface_: `github`",
        f"- _Type_: `{search_type}`",
        f"- _Matches_: {payload.get('resultCount') or len(results)}",
    ]
    scope_repo = str(payload.get("scopeRepo") or "")
    if scope_repo:
        lines.append(f"- _Repo Scope_: `{scope_repo}`")
    else:
        search_plan = str(payload.get("searchPlan") or "")
        global_result_count = int(payload.get("globalResultCount") or 0)
        if search_plan == "owned-only":
            lines.append("- _Search scope_: owned repositories and visible organizations only")
        elif search_plan == "global-excluding-owned":
            lines.append("- _Search scope_: global GitHub excluding owned-scope results")
            if global_result_count:
                lines.append(f"- _Global hits_: {global_result_count}")
    filename = str(payload.get("filename") or "")
    extension = str(payload.get("extension") or "")
    language = str(payload.get("language") or "")
    match = str(payload.get("match") or "")
    if filename:
        lines.append(f"- _Filename_: `{filename}`")
    if extension:
        lines.append(f"- _Extension_: `{extension}`")
    if language:
        lines.append(f"- _Language_: `{language}`")
    if match:
        lines.append(f"- _Match_: `{match}`")
    lines.extend(render_visibility_metadata_lines(payload))
    lines.extend(render_source_metadata_lines(derive_source_metadata_from_payload(payload)))
    lines.append("")
    if search_type == "repo":
        for item in results:
            if not isinstance(item, dict):
                continue
            full_name = str(item.get("fullName") or item.get("name") or "(unknown)")
            url = str(item.get("url") or "")
            line = f"- [{full_name}]({url})" if url else f"- {full_name}"
            details: list[str] = []
            language = str(item.get("language") or "")
            created = str(item.get("createdAt") or "")
            updated = str(item.get("updatedAt") or "")
            pushed = str(item.get("pushedAt") or "")
            stars = item.get("stars")
            if language:
                details.append(f"language `{language}`")
            if isinstance(stars, int):
                details.append(f"stars `{stars}`")
            if created:
                details.append(f"created `{created}`")
            if updated:
                details.append(f"updated `{updated}`")
            if pushed:
                details.append(f"pushed `{pushed}`")
            visibility = str(item.get("visibility_level") or "").strip()
            boundary = str(item.get("visibility_boundary") or "").strip()
            confidence = str(item.get("visibility_confidence") or "").strip()
            if visibility and boundary and confidence:
                details.append(f"visibility `{visibility}` ({boundary}, {confidence})")
            if details:
                line += " - " + ", ".join(details)
            lines.append(line)
            description = str(item.get("description") or "").strip()
            if include_details and description:
                lines.append(f"  - {description}")
        return "\n".join(lines) + "\n"
    if search_type == "code":
        for item in results:
            if not isinstance(item, dict):
                continue
            repository = str(item.get("repository") or "")
            path = str(item.get("path") or "")
            url = str(item.get("url") or "")
            label = f"{repository}:{path}" if repository and path else (path or repository or "code result")
            line = f"- [{label}]({url})" if url else f"- {label}"
            sha = str(item.get("sha") or "")
            if sha:
                line += f" - sha `{sha[:7]}`"
            lines.append(line)
            if include_details:
                text_matches = item.get("textMatches")
                if isinstance(text_matches, list):
                    for fragment in text_matches[:2]:
                        if not isinstance(fragment, str):
                            continue
                        excerpt = fragment.strip()
                        if not excerpt:
                            continue
                        lines.append(f"  - `{excerpt}`")
        return "\n".join(lines) + "\n"
    for item in results:
        if not isinstance(item, dict):
            continue
        repository = str(item.get("repository") or "")
        title = str(item.get("title") or "(untitled)")
        url = str(item.get("url") or "")
        number = item.get("number")
        kind = str(item.get("kind") or search_type)
        label = f"{kind} #{number}: {title}" if number else title
        if repository:
            label = f"{repository} {label}"
        line = f"- [{label}]({url})" if url else f"- {label}"
        details = []
        state = str(item.get("state") or "")
        author = str(item.get("author") or "")
        created = str(item.get("createdAt") or "")
        updated = str(item.get("updatedAt") or "")
        if state:
            details.append(f"state `{state}`")
        if author:
            details.append(f"author `{author}`")
        if created:
            details.append(f"created `{created}`")
        if updated:
            details.append(f"updated `{updated}`")
        labels = item.get("labels")
        if isinstance(labels, list) and labels:
            details.append(", ".join(f"`{label}`" for label in labels))
        if details:
            line += " - " + ", ".join(details)
        lines.append(line)
        body = str(item.get("body") or "").strip()
        if include_details and body:
            excerpt = body.splitlines()[0].strip()
            if excerpt:
                lines.append(f"  - {excerpt}")
    return "\n".join(lines) + "\n"


def markdown_binary_blob(
    *,
    owner: str,
    repo: str,
    ref: str,
    path: str,
) -> str:
    url = f"https://github.com/{owner}/{repo}/blob/{ref}/{path}"
    lines = [f"# {owner}/{repo}:{path}", "", f"- **URL:** {url}", f"- **Ref:** `{ref}`"]
    lines.append("- **Content:** binary blob")
    return "\n".join(lines) + "\n"


def markdown_text_blob_summary(
    *,
    owner: str,
    repo: str,
    ref: str,
    path: str,
    payload: dict[str, object],
) -> str:
    url = github_blob_url(owner, repo, ref, path)
    size = payload.get("size")
    lines = [f"# {owner}/{repo}:{path}", "", f"- **URL:** {url}", f"- **Ref:** `{ref}`"]
    if isinstance(size, int):
        lines.append(f"- **Size:** {size} bytes")
    return "\n".join(lines) + "\n"


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
    payload = gh_json_object(gh, ["api", f"repos/{owner}/{repo}/contents/{path}?ref={ref}"])
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


def markdown_directory(
    *,
    owner: str,
    repo: str,
    ref: str,
    path: str,
    entries: list[dict[str, object]],
    readme_path: str = "",
    readme_summary: str = "",
) -> str:
    display_path = path or "."
    lines = [f"# {owner}/{repo}:{display_path}", ""]
    lines.append(f"- **URL:** {github_tree_url(owner, repo, ref, path)}")
    lines.append(f"- **Ref:** `{ref}`")
    if readme_path:
        lines.append(f"- **README:** [{Path(readme_path).name}]({github_blob_url(owner, repo, ref, readme_path)})")
    if readme_summary:
        lines.append(f"- **README excerpt:** {readme_summary}")
    lines.extend(["", "## Contents", ""])
    for entry in sorted(entries, key=lambda item: (str(item.get("type") or ""), str(item.get("name") or "").casefold())):
        name = str(entry.get("name") or "")
        entry_path = str(entry.get("path") or "")
        entry_type = str(entry.get("type") or "")
        if not name or not entry_path:
            continue
        if entry_type == "dir":
            lines.append(f"- [{name}/]({github_tree_url(owner, repo, ref, entry_path)})")
            continue
        if entry_type == "file":
            lines.append(f"- [{name}]({github_blob_url(owner, repo, ref, entry_path)})")
            continue
        lines.append(f"- `{name}`")
    return "\n".join(lines) + "\n"


def markdown_repo_directory(
    payload: dict[str, object],
    *,
    owner: str,
    repo: str,
    ref: str,
    entries: list[dict[str, object]],
    readme_path: str = "",
    readme_summary: str = "",
) -> str:
    repo_markdown = markdown_repo(payload).rstrip()
    directory_markdown = markdown_directory(
        owner=owner,
        repo=repo,
        ref=ref,
        path="",
        entries=entries,
        readme_path=readme_path,
        readme_summary=readme_summary,
    ).splitlines()
    contents = directory_markdown[4:] if len(directory_markdown) >= 4 else directory_markdown
    return "\n".join([repo_markdown, "", *contents]).rstrip() + "\n"


def parse_args(argv: list[str], *, emit_help: bool = True) -> ParsedArgs:
    args = list(argv)
    if args[:2] == ["search", "--help"] or args[:2] == ["search", "-h"]:
        if emit_help:
            print(SEARCH_USAGE)
        return ParsedArgs(command="", output="")
    if args and args[0] == "search" and is_long_help_request(args[1:]):
        if emit_help:
            print(SEARCH_USAGE)
        return ParsedArgs(command="", output="")
    if len(args) == 1 and args[0] in {"-h", "--help"}:
        if emit_help:
            print(USAGE)
        return ParsedArgs(command="", output="")
    if is_long_help_request(args):
        if emit_help:
            print(USAGE)
        return ParsedArgs(command="", output="")
    if args and args[0] == "status":
        output = "summary"
        index = 1
        while index < len(args):
            token = args[index]
            if token == "--output":
                if index + 1 >= len(args) or args[index + 1] not in {"json", "summary"}:
                    raise SystemExit(
                        die(
                            "GitHub status accepts only `--output summary` or `--output json`.",
                            code=2,
                        )
                    )
                output = args[index + 1]
                index += 2
                continue
            if token.startswith("--"):
                raise SystemExit(
                    die(
                        f"unsupported GitHub status flag `{token}`. "
                        "Use `gotta github status [--output summary|json]`.",
                        code=2,
                    )
                )
            raise SystemExit(
                die(
                    f"unexpected GitHub status argument `{token}`. "
                    "Use `gotta github status [--output summary|json]`.",
                    code=2,
                )
            )
        return ParsedArgs(command="status", output=output)
    if args and args[0] == "search":
        output = "markdown"
        search_type = "repo"
        repo = ""
        filename = ""
        extension = ""
        language = ""
        match = ""
        limit = 10
        global_search = False
        query_parts: list[str] = []
        index = 1
        while index < len(args):
            token = args[index]
            if token == "--output":
                if index + 1 >= len(args):
                    raise SystemExit(die(USAGE))
                output = args[index + 1]
                if output not in {"json", "summary", "markdown"}:
                    raise SystemExit(die(USAGE))
                index += 2
                continue
            if token == "--type":
                if index + 1 >= len(args):
                    raise SystemExit(die(USAGE))
                search_type = args[index + 1]
                if search_type not in {"repo", "issue", "pr", "code"}:
                    raise SystemExit(die(USAGE))
                index += 2
                continue
            if token == "--repo":
                if index + 1 >= len(args):
                    raise SystemExit(die(USAGE))
                repo = args[index + 1].strip()
                if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
                    raise SystemExit(die(USAGE))
                index += 2
                continue
            if token == "--filename":
                if index + 1 >= len(args):
                    raise SystemExit(die(USAGE))
                filename = args[index + 1].strip()
                if not filename:
                    raise SystemExit(die(USAGE))
                index += 2
                continue
            if token == "--extension":
                if index + 1 >= len(args):
                    raise SystemExit(die(USAGE))
                extension = args[index + 1].strip()
                if not extension:
                    raise SystemExit(die(USAGE))
                index += 2
                continue
            if token == "--language":
                if index + 1 >= len(args):
                    raise SystemExit(die(USAGE))
                language = args[index + 1].strip()
                if not language:
                    raise SystemExit(die(USAGE))
                index += 2
                continue
            if token == "--match":
                if index + 1 >= len(args):
                    raise SystemExit(die(USAGE))
                match = args[index + 1].strip()
                if match not in {"file", "path"}:
                    raise SystemExit(die(USAGE))
                index += 2
                continue
            if token == "--global":
                global_search = True
                index += 1
                continue
            if token == "--limit":
                limit, index = _parse_limit_value(args, index, context="GitHub search")
                continue
            if token.startswith("--"):
                raise SystemExit(die(USAGE))
            query_parts.append(token)
            index += 1
        query = " ".join(part.strip() for part in query_parts if part.strip()).strip()
        if not query:
            raise SystemExit(die(USAGE))
        if search_type != "code" and any((filename, extension, language, match)):
            raise SystemExit(die(USAGE))
        return ParsedArgs(
            command="search",
            output=output,
            query=query,
            search_type=search_type,
            repo=repo,
            filename=filename,
            extension=extension,
            language=language,
            match=match,
            limit=limit,
            global_search=global_search,
        )
    output = "markdown"
    url = ""
    fragment = ""
    limit: int | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--output":
            if index + 1 >= len(args):
                raise SystemExit(die(USAGE))
            output = args[index + 1]
            if output not in {"json", "summary", "markdown"}:
                raise SystemExit(die(USAGE))
            index += 2
            continue
        if token == "--limit":
            limit, index = _parse_limit_value(args, index, context="GitHub URL render")
            continue
        if token.startswith("--"):
            raise SystemExit(
                die(
                    f"unsupported GitHub render flag `{token}`. URL renders accept only "
                    "`--output` and `--limit`; `--limit` applies only to commit-history URLs.",
                    code=2,
                )
            )
        if url:
            raise SystemExit(
                die(
                    f"unexpected second GitHub URL target `{token}`. "
                    "GitHub renders accept exactly one URL target.",
                    code=2,
                )
            )
        url = token
        index += 1
    if not url:
        raise SystemExit(die(USAGE))
    parsed_url = urllib.parse.urlparse(url)
    fragment = parsed_url.fragment
    return ParsedArgs(command="render", output=output, url=url, fragment=fragment, limit=limit)


def canonical_locator(argv: list[str]) -> str:
    parsed = parse_args(argv, emit_help=False)
    if not parsed.command:
        return "github:help"
    if parsed.command == "status":
        return "github:status"
    if parsed.command == "search":
        return _canonical_search_locator(parsed)
    return parsed.url.split("#", 1)[0].split("?", 1)[0]


def preferred_name(argv: list[str], options: Any) -> str:
    if getattr(options, "save_as", ""):
        return str(options.save_as)
    parsed = parse_args(argv, emit_help=False)
    if not parsed.command or parsed.command == "status":
        extension = "json" if parsed.output == "json" else "summary"
        return f"github.{extension}"
    if parsed.command == "search":
        search_type = {
            "repo": "repos",
            "issue": "issues",
            "pr": "prs",
            "code": "code",
        }.get(parsed.search_type, parsed.search_type)
        scope = ""
        if parsed.repo:
            scope = f"-{_slug(parsed.repo)}"
        elif parsed.global_search:
            scope = "-global"
        filter_parts: list[str] = []
        if parsed.filename:
            filter_parts.append(f"file-{_slug(parsed.filename)}")
        if parsed.extension:
            filter_parts.append(f"ext-{_slug(parsed.extension)}")
        if parsed.language:
            filter_parts.append(f"lang-{_slug(parsed.language)}")
        if parsed.match:
            filter_parts.append(f"match-{_slug(parsed.match)}")
        filter_suffix = ("-" + "-".join(filter_parts)) if filter_parts else ""
        return f"github-search-{search_type}{scope}{filter_suffix}-{_slug(parsed.query)}.json"
    extension = {
        "json": "json",
        "markdown": "md",
        "summary": "summary",
    }.get(parsed.output, "md")
    if parsed.command == "render":
        return _preferred_render_name(parsed, "json")
    return _preferred_render_name(parsed, extension)


def _blob_content_type(path: str, data: bytes) -> str:
    guessed, _encoding = mimetypes.guess_type(path)
    if guessed:
        return guessed
    return "text/plain" if looks_text(data) else "application/octet-stream"


def _blob_json_payload(path: str, data: bytes, *, owner: str, repo: str, ref: str) -> dict[str, object]:
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
            for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
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
        return {
            key: _canonicalize_capture_value(item)
            for key, item in value.items()
        }
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
                limit=parsed.limit,
                global_search=parsed.global_search,
            )
        elif parsed.search_type == "code":
            payload = search_code_payload(
                gh,
                query=parsed.query,
                repo=parsed.repo,
                limit=parsed.limit,
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
                limit=parsed.limit,
                search_type=parsed.search_type,
                global_search=parsed.global_search,
            )
        payload = _canonicalize_capture_value(payload)
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
            readme = load_directory_readme(gh, owner=owner, repo=repo, ref=ref, path=parent_path)
            if readme is None:
                raise RuntimeError(str(exc)) from exc
            path, blob = readme
            payload = _blob_json_payload(path, blob, owner=owner, repo=repo, ref=ref)
        payload = _canonicalize_capture_value(payload)
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
            payload = _canonicalize_capture_value(entries[0])
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
        payload = _canonicalize_capture_value(payload)
        return Capture(
            data=json_bytes(payload),
            name=_preferred_render_name(parsed, "json"),
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
        payload = _canonicalize_capture_value(payload)
        return Capture(
            data=json_bytes(_object_capture_payload("pr", payload, owner=owner, repo=repo)),
            name=_preferred_render_name(parsed, "json"),
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
        payload = _canonicalize_capture_value(payload)
        return Capture(
            data=json_bytes(_object_capture_payload("issue", payload, owner=owner, repo=repo)),
            name=_preferred_render_name(parsed, "json"),
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
        payload = _canonicalize_capture_value(payload)
        commit = payload.get("commit") if isinstance(payload.get("commit"), dict) else {}
        author = commit.get("author") if isinstance(commit, dict) else {}
        return Capture(
            data=json_bytes(_object_capture_payload("commit", payload, owner=owner, repo=repo)),
            name=_preferred_render_name(parsed, "json"),
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
        payload = [item for item in raw_payload if isinstance(item, dict)] if isinstance(raw_payload, list) else []
        payload = _canonicalize_capture_value(payload)
        authored_dates = [
            str(item.get("commit", {}).get("author", {}).get("date") or "")
            for item in payload
            if isinstance(item, dict)
        ]
        return Capture(
            data=json_bytes(_object_capture_payload("commits", payload, owner=owner, repo=repo, ref=ref, path=path)),
            name=_preferred_render_name(parsed, "json"),
            type="application/json",
            meta={
                "projector": "github",
                "github_kind": "commits",
                "github_owner": owner,
                "github_repo": repo,
                "github_ref": ref,
                "github_path": path,
                "source_created_at": min((value for value in authored_dates if value), default=""),
                "source_updated_at": max((value for value in authored_dates if value), default=""),
            },
        )
    if match := RELEASE_TAG_RE.match(url):
        owner, repo, tag, _ = match.groups()
        payload = gh_json_object(gh, ["api", f"repos/{owner}/{repo}/releases/tags/{tag}"])
        payload = with_visibility_metadata(payload, provider="github", locator=url)
        payload = _canonicalize_capture_value(payload)
        published = str(payload.get("published_at") or payload.get("created_at") or "")
        return Capture(
            data=json_bytes(_object_capture_payload("release", payload, owner=owner, repo=repo)),
            name=_preferred_render_name(parsed, "json"),
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
        raw_payload = gh_json_value(gh, ["api", f"repos/{owner}/{repo}/releases?per_page=20"])
        payload = [item for item in raw_payload if isinstance(item, dict)] if isinstance(raw_payload, list) else []
        payload = _canonicalize_capture_value(payload)
        published = [str(item.get("published_at") or item.get("created_at") or "") for item in payload]
        return Capture(
            data=json_bytes(_object_capture_payload("releases", payload, owner=owner, repo=repo)),
            name=_preferred_render_name(parsed, "json"),
            type="application/json",
            meta={
                "projector": "github",
                "github_kind": "releases",
                "github_owner": owner,
                "github_repo": repo,
                "source_created_at": min((value for value in published if value), default=""),
                "source_updated_at": max((value for value in published if value), default=""),
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
        payload = _canonicalize_capture_value(payload)
        default_branch_ref = payload.get("defaultBranchRef")
        default_branch = str(default_branch_ref.get("name") or "") if isinstance(default_branch_ref, dict) else ""
        entries: list[dict[str, object]] = []
        readme_path = ""
        readme_summary = ""
        view = {}
        if default_branch:
            entries = list_directory_entries(gh, owner=owner, repo=repo, ref=default_branch, path="")
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
            name=_preferred_render_name(parsed, "json"),
            type="application/json",
            meta={
                "projector": "github",
                "github_kind": "repo",
                "github_owner": owner,
                "github_repo": repo,
                "github_ref": default_branch,
                "source_created_at": str(payload.get("createdAt") or ""),
                "source_updated_at": str(payload.get("updatedAt") or payload.get("pushedAt") or ""),
            },
            view=view,
        )
    raise RuntimeError(f"unsupported GitHub URL format: {url}")


def project(argv: list[str], capture: Capture) -> bytes:
    kind = str(capture.meta.get("github_kind") or "").strip()
    if kind == "search":
        payload = json.loads(capture.data.decode("utf-8"))
        parsed = parse_args(argv, emit_help=False) if argv else ParsedArgs(command="search", output="markdown")
        if parsed.output == "json":
            return pretty_json(capture.data)
        return markdown_search(payload, include_details=(parsed.output != "summary")).encode("utf-8")
    owner = str(capture.meta.get("github_owner") or "").strip()
    repo = str(capture.meta.get("github_repo") or "").strip()
    ref = str(capture.meta.get("github_ref") or "").strip()
    path = str(capture.meta.get("github_path") or "").strip()
    if kind == "blob":
        if not argv:
            if looks_text(capture.data):
                return capture.data
            return markdown_binary_blob(owner=owner, repo=repo, ref=ref, path=path).encode("utf-8")
        parsed = parse_args(argv, emit_help=False)
        if parsed.output == "json":
            payload = capture.view.get("payload")
            if isinstance(payload, dict):
                return json_bytes(payload)
            return json_bytes(_blob_json_payload(path, capture.data, owner=owner, repo=repo, ref=ref))
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
        return markdown_binary_blob(owner=owner, repo=repo, ref=ref, path=path).encode("utf-8")
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
                return markdown_binary_blob(owner=owner, repo=repo, ref=str(payload.get("ref") or ref), path=hinted_path).encode("utf-8")
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
            return markdown_binary_blob(owner=owner, repo=repo, ref=str(payload.get("ref") or ref), path=hinted_path).encode("utf-8")
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
    if kind in {"issue", "pr", "commit", "commits", "release", "releases"}:
        parsed = parse_args(argv, emit_help=False) if argv else ParsedArgs(command="render", output="markdown")
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
        releases = object_payload if isinstance(object_payload, list) else []
        if parsed.output == "summary":
            releases = releases[:10]
        return markdown_release_list(owner=owner, repo=repo, payload=releases).encode("utf-8")
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
                    limit=parsed.limit,
                    global_search=parsed.global_search,
                )
            elif parsed.search_type == "code":
                payload = search_code_payload(
                    gh,
                    query=parsed.query,
                    repo=parsed.repo,
                    limit=parsed.limit,
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
                    limit=parsed.limit,
                    search_type=parsed.search_type,
                    global_search=parsed.global_search,
                )
        except RuntimeError as exc:
            return die(str(exc), code=1)
        if parsed.output == "json":
            render_bytes(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json")
            return 0
        emit_markdown(markdown_search(payload, include_details=(parsed.output == "markdown")))
        return 0
    url, fragment = split_render_url(parsed.url)

    if match := BLOB_RE.match(url):
        if parsed.limit is not None:
            return _unsupported_render_limit_error()
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
                    json.dumps(fallback_payload, indent=2, sort_keys=True).encode("utf-8"),
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
            render_bytes(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json")
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
            return _unsupported_render_limit_error()
        owner, repo, ref, path = match.groups()
        path = normalize_ref_path(path)
        try:
            entries = list_directory_entries(gh, owner=owner, repo=repo, ref=ref, path=path)
        except RuntimeError as exc:
            return die(str(exc), code=1)
        if len(entries) == 1 and str(entries[0].get("type") or "") == "file":
            payload = entries[0]
            if parsed.output == "json":
                render_bytes(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json")
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
            emit_markdown(markdown_binary_blob(owner=owner, repo=repo, ref=ref, path=file_path))
            return 0
        if parsed.output == "json":
            render_bytes(json.dumps(entries, indent=2, sort_keys=True).encode("utf-8"), "json")
            return 0
        if parsed.output == "summary":
            emit_markdown(markdown_directory(owner=owner, repo=repo, ref=ref, path=path, entries=entries))
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
            return _unsupported_render_limit_error()
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
        render_bytes(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json")
        return 0

    if match := ISSUE_RE.match(url):
        if parsed.limit is not None:
            return _unsupported_render_limit_error()
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
            emit_markdown(
                markdown_issue_or_pr(payload, "issue", include_body=True)
            )
            return 0
        if parsed.output == "summary":
            emit_markdown(
                markdown_issue_or_pr(payload, "issue", include_body=False)
            )
            return 0
        render_bytes(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json")
        return 0

    if match := COMMIT_RE.match(url):
        if parsed.limit is not None:
            return _unsupported_render_limit_error()
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
        render_bytes(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json")
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
                    default_history = f"https://github.com/{owner}/{repo}/commits/{default_branch}"
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
        payload = [item for item in raw_payload if isinstance(item, dict)] if isinstance(raw_payload, list) else []
        if parsed.output in {"summary", "markdown"}:
            emit_markdown(markdown_commit_list(payload, owner=owner, repo=repo, ref=ref, path=path))
            return 0
        render_bytes(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json")
        return 0

    if match := RELEASE_TAG_RE.match(url):
        if parsed.limit is not None:
            return _unsupported_render_limit_error()
        owner, repo, tag, _ = match.groups()
        try:
            payload = gh_json_object(gh, ["api", f"repos/{owner}/{repo}/releases/tags/{tag}"])
        except RuntimeError as exc:
            return die(str(exc), code=1)
        payload = with_visibility_metadata(payload, provider="github", locator=url)
        if parsed.output in {"summary", "markdown"}:
            emit_markdown(markdown_release(payload, owner=owner, repo=repo))
            return 0
        render_bytes(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json")
        return 0

    if match := RELEASES_RE.match(url):
        if parsed.limit is not None:
            return _unsupported_render_limit_error()
        owner, repo = match.groups()
        try:
            raw_payload = gh_json_value(gh, ["api", f"repos/{owner}/{repo}/releases?per_page=20"])
        except RuntimeError as exc:
            return die(str(exc), code=1)
        payload = [item for item in raw_payload if isinstance(item, dict)] if isinstance(raw_payload, list) else []
        if parsed.output == "markdown":
            emit_markdown(markdown_release_list(owner=owner, repo=repo, payload=payload))
            return 0
        if parsed.output == "summary":
            emit_markdown(markdown_release_list(owner=owner, repo=repo, payload=payload[:10]))
            return 0
        render_bytes(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json")
        return 0

    if match := REPO_RE.match(url):
        if parsed.limit is not None:
            return _unsupported_render_limit_error()
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
        render_bytes(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), "json")
        return 0

    return die(f"unsupported GitHub URL format: {url}")


if __name__ == "__main__":
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    raise SystemExit(main(sys.argv[1:]))
