"""Preferred-name and content-type derivation for invocations."""

from __future__ import annotations

from pathlib import Path
import re
import urllib.parse

from gotta.builtin import get_plugin
from gotta.content.model import CommonOptions
from gotta.providers import atlassian as atl


def _infer_output_extension(plugin: str, argv: list[str]) -> str:
    output_value = ""
    for index, item in enumerate(argv):
        if item.startswith("--output="):
            output_value = item.split("=", 1)[1]
        if item == "--output" and index + 1 < len(argv):
            output_value = argv[index + 1]
    mapping = {
        "adf": "json",
        "body": "html",
        "html": "html",
        "json": "json",
        "markdown": "md",
        "md": "md",
        "meta": "json",
        "messages": "json",
        "raw": "bin",
        "csv": "csv",
        "text": "txt",
        "titles": "txt",
        "links": "txt",
    }
    if output_value:
        return mapping.get(output_value, output_value)
    return {
        "read": "txt",
        "confluence": "html" if "render-markdown" in argv else "md",
        "gdrive": "md",
        "gdocs": "md",
        "gsheets": "md",
        "github": "md",
        "jira": "md",
        "slack": "md",
    }.get(plugin, "txt")


def _slug_name(value: str, *, fallback: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    return value.strip("-") or fallback


def _flag_value(argv: list[str], flag: str) -> str:
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return argv[index + 1].strip()
        if token.startswith(f"{flag}="):
            return token.split("=", 1)[1].strip()
    return ""


def _query_tail(
    argv: list[str],
    *,
    valued_flags: tuple[str, ...] = (),
    boolean_flags: tuple[str, ...] = (),
) -> str:
    if not argv:
        return ""
    query_parts: list[str] = []
    index = 1
    while index < len(argv):
        token = argv[index]
        if query_parts:
            query_parts.extend(argv[index:])
            break
        if token == "--":
            query_parts.extend(argv[index + 1 :])
            break
        if token in boolean_flags:
            index += 1
            continue
        if token in valued_flags:
            index += 2
            continue
        if token.startswith("--"):
            index += 1
            continue
        query_parts.extend(argv[index:])
        break
    return " ".join(part.strip() for part in query_parts if part.strip()).strip()


def preferred_name(plugin: str, argv: list[str], options: CommonOptions) -> str:
    spec = get_plugin(plugin)
    if spec and spec.preferred_name is not None:
        return spec.preferred_name(argv, options)
    if options.save_as:
        return options.save_as
    extension = _infer_output_extension(plugin, argv)
    tokens = [arg for arg in argv if not arg.startswith("-")]
    subcommand = tokens[0] if tokens else ""
    if plugin == "confluence" and subcommand == "status":
        return f"confluence.{extension}"
    if plugin == "github" and tokens:
        name = Path(tokens[-1]).name or "github"
        if "." in name:
            return name
        return f"{name}.{extension}"
    if plugin == "confluence" and subcommand == "search" and len(tokens) >= 2:
        query = _query_tail(
            argv,
            valued_flags=(
                "--space",
                "--type",
                "--limit",
                "--next",
                "--output",
                "--base-url",
            ),
            boolean_flags=("--title-only",),
        )
        return (
            f"confluence-search-{_slug_name(query, fallback='confluence')}.{extension}"
        )
    if plugin == "confluence" and subcommand == "cql":
        query = _query_tail(argv, valued_flags=("--limit", "--next", "--base-url"))
        return f"confluence-cql-{_slug_name(query, fallback='confluence')}.{extension}"
    if plugin == "confluence" and len(tokens) >= 2:
        subject = tokens[1]
        parsed = urllib.parse.urlparse(subject)
        page_name = Path(parsed.path.rstrip("/")).name if parsed.scheme else subject
        page_id = atl.extract_confluence_page_id(subject) or ""
        if subcommand == "get" and page_id:
            suffix = "body.html" if extension == "html" else extension
            return f"{page_id}.{suffix}"
        return f"{_slug_name(page_name, fallback='confluence')}.{extension}"
    if plugin == "gdrive" and subcommand == "search" and len(tokens) >= 2:
        query = _query_tail(
            argv,
            valued_flags=("--limit", "--mode", "--mime-type", "--output"),
        )
        return f"gdrive-search-{_slug_name(query, fallback='gdrive')}.{extension}"
    if plugin == "gdrive" and len(tokens) >= 2:
        subject = tokens[1]
        match = re.search(r"/d/([^/]+)", subject)
        if not match:
            match = re.search(r"[?&]id=([^&]+)", subject)
        file_id = match.group(1) if match else subject
        return f"{file_id}.{extension}"
    if plugin == "gdocs" and subcommand == "search" and len(tokens) >= 2:
        query = _query_tail(argv, valued_flags=("--limit", "--mode", "--output"))
        return f"gdocs-search-{_slug_name(query, fallback='gdocs')}.{extension}"
    if plugin == "gdocs" and len(tokens) >= 2:
        subject = tokens[1]
        match = re.search(r"/document/d/([^/]+)", subject)
        doc_id = match.group(1) if match else subject
        return f"{doc_id}.{extension}"
    if plugin == "gsheets" and subcommand == "search" and len(tokens) >= 2:
        query = _query_tail(argv, valued_flags=("--limit", "--mode", "--output"))
        return f"gsheets-search-{_slug_name(query, fallback='gsheets')}.{extension}"
    if plugin == "gsheets" and len(tokens) >= 2:
        subject = tokens[1]
        match = re.search(r"/spreadsheets/d/([^/]+)", subject)
        sheet_id = match.group(1) if match else subject
        return f"{sheet_id}.{extension}"
    if plugin == "jira" and subcommand in {"search", "jql"}:
        query = _query_tail(
            argv,
            valued_flags=("--base-url", "--limit", "--next", "--output"),
        )
        return f"jira-{subcommand}-{_slug_name(query, fallback='jira')}.{extension}"
    if plugin == "jira" and len(tokens) >= 2:
        subject = tokens[1].rstrip("/").split("/")[-1]
        return subject if "." in subject else f"{subject}.{extension}"
    if plugin == "slack" and subcommand == "status":
        workspace = _flag_value(argv, "--workspace") or "workspace"
        return f"slack-workspace-{_slug_name(workspace, fallback='slack')}.{extension}"
    if plugin == "slack" and subcommand == "search":
        workspace = _flag_value(argv, "--workspace")
        query = _query_tail(
            argv,
            valued_flags=(
                "--channel",
                "--workspace",
                "--limit",
                "--output",
                "--source",
                "--match",
                "--live-timeout",
                "--pull-recent",
            ),
            boolean_flags=("--refresh",),
        )
        prefix = "slack-search"
        if workspace:
            prefix = f"{prefix}-{_slug_name(workspace, fallback='slack')}"
        return f"{prefix}-{_slug_name(query, fallback='slack')}.{extension}"
    if plugin == "slack" and len(tokens) >= 2:
        subject = tokens[1].rstrip("/").split("/")[-1]
        return subject if "." in subject else f"{subject}.{extension}"
    return f"{plugin}.{extension}"


def infer_content_type(plugin: str, argv: list[str], name: str) -> str:
    spec = get_plugin(plugin)
    if spec and spec.content_type is not None:
        return spec.content_type(argv, name)
    extension = Path(name).suffix.lower()
    if extension == ".html":
        return "text/html"
    if extension == ".json":
        return "application/json"
    if extension == ".md":
        return "text/markdown"
    if extension == ".csv":
        return "text/csv"
    if extension == ".txt":
        return "text/plain"
    if plugin == "read" and not argv:
        return "text/plain"
    return "application/octet-stream"
