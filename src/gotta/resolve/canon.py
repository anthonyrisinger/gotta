"""Canonical locator derivation for provider invocations."""

from __future__ import annotations

import re
import urllib.parse

from gotta.builtin import get_plugin
from gotta.providers import atlassian as atl


def invocation_locator(plugin: str, argv: list[str]) -> str:
    spec = get_plugin(plugin)
    if spec and spec.invocation_locator is not None:
        return spec.invocation_locator(argv)
    if not argv:
        return plugin
    return " ".join(argv)


def _canonicalize_github_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    trimmed = parsed._replace(query="", fragment="")
    path = trimmed.path.rstrip("/")
    return f"github:{trimmed.netloc}{path}"


def _canonicalize_slack_ref(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    doc_match = re.search(r"/docs/([^/]+)/([^/]+)", parsed.path)
    if doc_match:
        return f"slack:doc:{doc_match.group(1)}:{doc_match.group(2)}"
    match = re.search(r"/archives/([^/]+)(?:/p(\d+))?", parsed.path)
    if not match:
        return f"slack:{value}"
    channel_id = match.group(1)
    thread_ref = match.group(2)
    if thread_ref:
        return f"slack:thread:{channel_id}:{thread_ref}"
    return f"slack:channel:{channel_id}"


def _canonicalize_confluence_ref(value: str) -> str:
    page_id = atl.extract_confluence_page_id(value)
    if page_id:
        return f"confluence:{page_id}"
    return f"confluence:{value}"


def _canonicalize_jira_ref(value: str) -> str:
    issue_match = re.search(r"/browse/([A-Z][A-Z0-9]+-\d+)(?:/|$)", value)
    if issue_match:
        return f"jira:{issue_match.group(1)}"
    key_match = re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", value)
    if key_match:
        return f"jira:{value}"
    return f"jira:{value}"


def _canonicalize_gdocs_ref(value: str) -> str:
    doc_match = re.search(r"/document/d/([^/]+)", value)
    if doc_match:
        return f"gdocs:{doc_match.group(1)}"
    return f"gdocs:{value}"


def _canonicalize_gdrive_ref(value: str) -> str:
    file_match = re.search(r"/d/([^/]+)", value)
    if file_match:
        return f"gdrive:{file_match.group(1)}"
    query_match = re.search(r"[?&]id=([^&]+)", value)
    if query_match:
        return f"gdrive:{query_match.group(1)}"
    return f"gdrive:{value}"


def _canonicalize_gsheets_ref(value: str) -> str:
    sheet_match = re.search(r"/spreadsheets/d/([^/]+)", value)
    if sheet_match:
        return f"gsheets:{sheet_match.group(1)}"
    return f"gsheets:{value}"


def canonical_locator(plugin: str, argv: list[str]) -> str:
    spec = get_plugin(plugin)
    if spec and spec.canonical_locator is not None:
        return spec.canonical_locator(argv)
    tokens = [arg for arg in argv if not arg.startswith("-")]
    if not tokens:
        return plugin
    subject = tokens[-1]
    if plugin == "github":
        return _canonicalize_github_url(subject)
    if plugin == "jira" and tokens[0] == "get" and len(tokens) >= 2:
        return _canonicalize_jira_ref(tokens[1])
    if plugin == "confluence" and tokens[0] == "get" and len(tokens) >= 2:
        return _canonicalize_confluence_ref(tokens[1])
    if plugin == "gdrive" and tokens[0] == "get" and len(tokens) >= 2:
        return _canonicalize_gdrive_ref(tokens[1])
    if plugin == "gdocs" and tokens[0] == "get" and len(tokens) >= 2:
        return _canonicalize_gdocs_ref(tokens[1])
    if plugin == "gsheets" and tokens[0] == "get" and len(tokens) >= 2:
        return _canonicalize_gsheets_ref(tokens[1])
    if plugin == "slack" and tokens[0] == "get" and len(tokens) >= 2:
        return _canonicalize_slack_ref(tokens[1])
    return f"{plugin}:{invocation_locator(plugin, argv)}"
