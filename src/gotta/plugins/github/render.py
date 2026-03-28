"""GitHub output rendering helpers."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from gotta.source.render import render_visibility_metadata_lines
from gotta.source.visibility import with_visibility_metadata

from .read import (
    github_blob_url,
    github_tree_url,
)


def _dict_payload(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _dict_items(value: object) -> list[dict[str, object]]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _render_visibility_payload(payload: dict[str, object]) -> dict[str, object]:
    return with_visibility_metadata(dict(payload), provider="github")


def guess_lang_from_path(path: str) -> str:
    basename = Path(path).name.lower()
    if basename in {
        "readme",
        "readme.md",
        "readme.markdown",
        "readme.mdown",
        "readme.mkd",
    }:
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
    commit = _dict_payload(payload.get("commit"))
    message = str(commit.get("message") or "").strip()
    author = _dict_payload(commit.get("author"))
    authored = str(author.get("date") or "")
    author_name = str(author.get("name") or "")
    files = _dict_items(payload.get("files"))
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
    authored_dates: list[str] = []
    for item in payload:
        commit = _dict_payload(item.get("commit"))
        author = _dict_payload(commit.get("author"))
        authored_at = str(author.get("date") or "")
        if authored_at:
            authored_dates.append(authored_at)
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
        message = (
            str(commit.get("message") or "").strip().splitlines()[0] if commit else ""
        )
        author = commit.get("author")
        if not isinstance(author, dict):
            author = {}
        author_name = str(author.get("name") or "")
        authored_at = str(author.get("date") or "")
        summary = (
            f"[{sha}]({html_url})"
            if html_url and sha
            else (sha or html_url or "commit")
        )
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


def markdown_workflow_run(
    payload: dict[str, object],
    *,
    owner: str,
    repo: str,
    include_jobs: bool,
) -> str:
    display_title = str(
        payload.get("displayTitle") or payload.get("name") or "workflow run"
    )
    run_number = str(payload.get("number") or "").strip()
    url = str(payload.get("url") or "")
    workflow_name = str(
        payload.get("workflowName") or payload.get("name") or ""
    ).strip()
    event = str(payload.get("event") or "").strip()
    status = str(payload.get("status") or "").strip()
    conclusion = str(payload.get("conclusion") or "").strip()
    branch = str(payload.get("headBranch") or "").strip()
    head_sha = str(payload.get("headSha") or "").strip()
    created = str(payload.get("createdAt") or "").strip()
    started = str(payload.get("startedAt") or "").strip()
    updated = str(payload.get("updatedAt") or "").strip()
    attempt = payload.get("attempt")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        jobs = []
    heading = f"# {owner}/{repo} workflow run"
    if run_number:
        heading += f" #{run_number}"
    heading += f": {display_title}"
    lines = [heading, ""]
    if url:
        lines.append(f"- **URL:** {url}")
    lines.extend(render_visibility_metadata_lines(payload))
    if workflow_name:
        lines.append(f"- **Workflow:** `{workflow_name}`")
    if event:
        lines.append(f"- **Event:** `{event}`")
    if status:
        lines.append(f"- **Status:** `{status}`")
    if conclusion:
        lines.append(f"- **Conclusion:** `{conclusion}`")
    if branch:
        lines.append(f"- **Branch:** `{branch}`")
    if head_sha:
        lines.append(f"- **Commit:** `{head_sha}`")
    if created:
        lines.append(f"- **Created:** {created}")
    if started:
        lines.append(f"- **Started:** {started}")
    if updated:
        lines.append(f"- **Updated:** {updated}")
    if isinstance(attempt, int):
        lines.append(f"- **Attempt:** {attempt}")
    lines.append(f"- **Jobs:** {len(jobs)}")
    if include_jobs and jobs:
        lines.extend(["", "## Jobs", ""])
        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_name = str(job.get("name") or "job").strip()
            job_url = str(job.get("url") or "").strip()
            job_status = str(job.get("status") or "").strip()
            job_conclusion = str(job.get("conclusion") or "").strip()
            job_started = str(job.get("startedAt") or "").strip()
            job_completed = str(job.get("completedAt") or "").strip()
            line = f"- [{job_name}]({job_url})" if job_url else f"- {job_name}"
            details: list[str] = []
            if job_status:
                details.append(f"status `{job_status}`")
            if job_conclusion:
                details.append(f"conclusion `{job_conclusion}`")
            if job_started:
                details.append(f"started `{job_started}`")
            if job_completed:
                details.append(f"completed `{job_completed}`")
            if details:
                line += " - " + ", ".join(details)
            lines.append(line)
    return "\n".join(lines) + "\n"


def markdown_workflow_job(
    payload: dict[str, object],
    *,
    owner: str,
    repo: str,
    include_steps: bool,
) -> str:
    job_id = str(payload.get("id") or "").strip()
    job_name = str(payload.get("name") or "workflow job").strip()
    url = str(payload.get("html_url") or "").strip()
    workflow_name = str(payload.get("workflow_name") or "").strip()
    run_id = str(payload.get("run_id") or "").strip()
    status = str(payload.get("status") or "").strip()
    conclusion = str(payload.get("conclusion") or "").strip()
    branch = str(payload.get("head_branch") or "").strip()
    head_sha = str(payload.get("head_sha") or "").strip()
    created = str(payload.get("created_at") or "").strip()
    started = str(payload.get("started_at") or "").strip()
    completed = str(payload.get("completed_at") or "").strip()
    runner_name = str(payload.get("runner_name") or "").strip()
    runner_group = str(payload.get("runner_group_name") or "").strip()
    labels = payload.get("labels")
    if not isinstance(labels, list):
        labels = []
    steps = payload.get("steps")
    if not isinstance(steps, list):
        steps = []
    heading = f"# {owner}/{repo} workflow job"
    if job_id:
        heading += f" {job_id}"
    heading += f": {job_name}"
    lines = [heading, ""]
    if url:
        lines.append(f"- **URL:** {url}")
    lines.extend(render_visibility_metadata_lines(payload))
    if workflow_name:
        lines.append(f"- **Workflow:** `{workflow_name}`")
    if run_id:
        lines.append(f"- **Run ID:** `{run_id}`")
    if status:
        lines.append(f"- **Status:** `{status}`")
    if conclusion:
        lines.append(f"- **Conclusion:** `{conclusion}`")
    if branch:
        lines.append(f"- **Branch:** `{branch}`")
    if head_sha:
        lines.append(f"- **Commit:** `{head_sha}`")
    if created:
        lines.append(f"- **Created:** {created}")
    if started:
        lines.append(f"- **Started:** {started}")
    if completed:
        lines.append(f"- **Completed:** {completed}")
    if runner_name:
        lines.append(f"- **Runner:** `{runner_name}`")
    if runner_group:
        lines.append(f"- **Runner group:** `{runner_group}`")
    if labels:
        rendered_labels = [str(label).strip() for label in labels if str(label).strip()]
        if rendered_labels:
            lines.append(
                f"- **Labels:** {', '.join(f'`{label}`' for label in rendered_labels)}"
            )
    if include_steps and steps:
        lines.extend(["", "## Steps", ""])
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_name = str(step.get("name") or "step").strip()
            number = step.get("number")
            step_status = str(step.get("status") or "").strip()
            step_conclusion = str(step.get("conclusion") or "").strip()
            step_started = str(step.get("started_at") or "").strip()
            step_completed = str(step.get("completed_at") or "").strip()
            label = f"{number}. {step_name}" if isinstance(number, int) else step_name
            details: list[str] = []
            if step_status:
                details.append(f"status `{step_status}`")
            if step_conclusion:
                details.append(f"conclusion `{step_conclusion}`")
            if step_started:
                details.append(f"started `{step_started}`")
            if step_completed:
                details.append(f"completed `{step_completed}`")
            if details:
                lines.append(f"- {label} - " + ", ".join(details))
            else:
                lines.append(f"- {label}")
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
        lines.append(
            f"- **README:** [{Path(readme_path).name}]({github_blob_url(owner, repo, ref, readme_path)})"
        )
    if readme_summary:
        lines.append(f"- **README excerpt:** {readme_summary}")
    lines.extend(["", "## Contents", ""])
    for entry in sorted(
        entries,
        key=lambda item: (
            str(item.get("type") or ""),
            str(item.get("name") or "").casefold(),
        ),
    ):
        name = str(entry.get("name") or "")
        entry_path = str(entry.get("path") or "")
        entry_type = str(entry.get("type") or "")
        if not name or not entry_path:
            continue
        if entry_type == "dir":
            lines.append(
                f"- [{name}/]({github_tree_url(owner, repo, ref, entry_path)})"
            )
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
    contents = (
        directory_markdown[4:] if len(directory_markdown) >= 4 else directory_markdown
    )
    return "\n".join([repo_markdown, "", *contents]).rstrip() + "\n"
