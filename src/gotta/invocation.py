"""Invocation resolution and metadata derivation for gotta commands."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Literal
import urllib.parse

from gotta.builtin import PluginSpec, get_plugin
from gotta.content import CommonOptions
from gotta.searching import SearchRouteError, resolve_search_route
from gotta.target import resolve_read_target
from gotta.providers import atlassian as atl


SUPPRESS_MATERIALIZATION_ENV = "GOTTA_SUPPRESS_MATERIALIZATION"
HELP_TOKENS = {"-h", "--help", "--help-all", "help-all"}
ArtifactIntent = Literal["none", "discovery", "evidence"]

CONTROL_SUBCOMMANDS = {
    "confluence": {"auth", "mcp", "status"},
    "grafana": {"auth", "status"},
    "gdrive": {"auth", "status"},
    "gdocs": {"auth", "status"},
    "gsheets": {"auth", "status"},
    "github": {"status"},
    "jira": {"auth", "mcp", "status"},
    "slack": {"auth", "mcp", "status", "workspaces"},
}


@dataclass(frozen=True, slots=True)
class ResolvedInvocation:
    entry_plugin: str
    entry_argv: list[str]
    resolved_plugin: str
    resolved_argv: list[str]
    canonical_locator: str
    preferred_name: str
    content_type: str
    artifact_intent: ArtifactIntent
    artifact_kind: str
    should_materialize: bool
    provider: str


DISCOVERY_SUBCOMMANDS: dict[str, set[str]] = {
    "confluence": {"search", "cql"},
    "gdocs": {"search"},
    "gdrive": {"search"},
    "grafana": {"search"},
    "granola": {"list", "search", "search-transcript"},
    "gsheets": {"search"},
    "jira": {"search", "jql"},
    "slack": {"search"},
}

EVIDENCE_SUBCOMMANDS: dict[str, set[str]] = {
    "confluence": {"get"},
    "gdocs": {"get"},
    "gdrive": {"get"},
    "grafana": {"get"},
    "granola": {"get", "transcript"},
    "gsheets": {"get"},
    "jira": {"get"},
    "slack": {"get"},
}

NON_ARTIFACT_SUBCOMMANDS: dict[str, set[str]] = {
    "confluence": {
        "auth",
        "batch",
        "create-page",
        "find",
        "mcp",
        "render-markdown",
        "replace",
        "replace-section",
        "resolve-page",
        "status",
        "update-body",
    },
    "gdocs": {"auth", "status"},
    "gdrive": {"auth", "status"},
    "grafana": {"auth", "datasources", "query", "status"},
    "granola": {"export", "status"},
    "gsheets": {"auth", "status"},
    "jira": {
        "add-to-sprint",
        "auth",
        "comment",
        "create",
        "fields",
        "issue-types",
        "link",
        "link-types",
        "mcp",
        "projects",
        "sprints",
        "status",
        "transition",
        "transitions",
        "update",
    },
    "slack": {
        "auth",
        "list-channels",
        "list-users",
        "mcp",
        "schema",
        "sql",
        "status",
        "sync",
        "workspaces",
    },
}


def _plugin_spec(plugin: str) -> PluginSpec | None:
    return get_plugin(plugin)


def _materialization_enabled() -> bool:
    return os.environ.get(SUPPRESS_MATERIALIZATION_ENV) != "1"


def _artifact_kind(intent: ArtifactIntent) -> str:
    return "" if intent == "none" else intent


def _invocation_locator(plugin: str, argv: list[str]) -> str:
    spec = _plugin_spec(plugin)
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


def _canonical_locator(plugin: str, argv: list[str]) -> str:
    spec = _plugin_spec(plugin)
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
    return f"{plugin}:{_invocation_locator(plugin, argv)}"


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


def _preferred_name(plugin: str, argv: list[str], options: CommonOptions) -> str:
    spec = _plugin_spec(plugin)
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


def _infer_content_type(plugin: str, argv: list[str], name: str) -> str:
    spec = _plugin_spec(plugin)
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


def _generic_artifact_intent(plugin: str, argv: list[str]) -> ArtifactIntent:
    spec = _plugin_spec(plugin)
    if spec and spec.should_materialize is not None:
        return "evidence" if bool(spec.should_materialize(argv)) else "none"
    if plugin == "session":
        return "none"
    if not argv:
        return "evidence"
    subcommand = argv[0]
    if subcommand == "status":
        return "none"
    return (
        "none" if subcommand in CONTROL_SUBCOMMANDS.get(plugin, set()) else "evidence"
    )


def _artifact_intent(plugin: str, argv: list[str]) -> ArtifactIntent:
    if any(arg in HELP_TOKENS for arg in argv):
        return "none"
    if plugin == "github":
        if not argv or argv[0] == "status":
            return "none"
        if argv[0] == "search":
            return "discovery"
        return "evidence"
    subcommand = argv[0] if argv else ""
    if subcommand in NON_ARTIFACT_SUBCOMMANDS.get(plugin, set()):
        return "none"
    if subcommand in DISCOVERY_SUBCOMMANDS.get(plugin, set()):
        return "discovery"
    if subcommand in EVIDENCE_SUBCOMMANDS.get(plugin, set()):
        return "evidence"
    return _generic_artifact_intent(plugin, argv)


def artifact_intent(plugin: str, argv: list[str]) -> ArtifactIntent:
    if plugin == "search":
        try:
            resolve_search_route(argv)
        except SearchRouteError:
            return "none"
        return "discovery"
    if plugin == "read":
        try:
            target = resolve_read_target(argv, CommonOptions())
        except SystemExit:
            return "none"
        return "evidence" if target.should_materialize else "none"
    return _artifact_intent(plugin, argv)


def session_access_mode(plugin: str, argv: list[str]) -> str:
    if plugin == "search":
        return "ambient" if artifact_intent(plugin, argv) != "none" else "none"
    if plugin == "read":
        return "ambient"
    return "ambient" if artifact_intent(plugin, argv) != "none" else "none"


def resolve_invocation(
    plugin: str,
    argv: list[str],
    options: CommonOptions | None = None,
) -> ResolvedInvocation:
    options = options or CommonOptions()
    entry_plugin = plugin
    entry_argv = list(argv)
    resolved_plugin = plugin
    resolved_argv = list(argv)
    if any(arg in HELP_TOKENS for arg in argv):
        preferred = options.save_as or f"{plugin}.txt"
        return ResolvedInvocation(
            entry_plugin=entry_plugin,
            entry_argv=entry_argv,
            resolved_plugin=plugin,
            resolved_argv=resolved_argv,
            canonical_locator=f"{plugin}:help",
            preferred_name=preferred,
            content_type=_infer_content_type(plugin, argv, preferred),
            artifact_intent="none",
            artifact_kind="",
            should_materialize=False,
            provider=plugin,
        )
    if plugin == "read":
        try:
            resolved_target = resolve_read_target(argv, options)
        except SystemExit:
            target = next((item for item in argv if not item.startswith("-")), "")
            preferred = options.save_as or "read.txt"
            return ResolvedInvocation(
                entry_plugin=entry_plugin,
                entry_argv=entry_argv,
                resolved_plugin="read",
                resolved_argv=resolved_argv,
                canonical_locator=target or "read",
                preferred_name=preferred,
                content_type=_infer_content_type("read", resolved_argv, preferred),
                artifact_intent="none",
                artifact_kind="",
                should_materialize=False,
                provider="read",
            )
        if resolved_target.routed_plugin is not None:
            resolved_plugin = resolved_target.routed_plugin
            resolved_argv = list(resolved_target.routed_argv)
        provider = resolved_plugin
        canonical = resolved_target.canonical_locator
        preferred = resolved_target.preferred_name
        content_type = _infer_content_type(resolved_plugin, resolved_argv, preferred)
        if resolved_target.routed_plugin is not None:
            routed_intent = _artifact_intent(resolved_plugin, resolved_argv)
            intent: ArtifactIntent = (
                routed_intent if resolved_target.should_materialize else "none"
            )
        else:
            intent = "evidence" if resolved_target.should_materialize else "none"
        return ResolvedInvocation(
            entry_plugin=entry_plugin,
            entry_argv=entry_argv,
            resolved_plugin=resolved_plugin,
            resolved_argv=resolved_argv,
            canonical_locator=canonical,
            preferred_name=preferred,
            content_type=content_type,
            artifact_intent=intent,
            artifact_kind=_artifact_kind(intent),
            should_materialize=intent != "none" and _materialization_enabled(),
            provider=provider,
        )
    if plugin == "search":
        try:
            search_route = resolve_search_route(argv)
        except SearchRouteError:
            preferred = options.save_as or "search.md"
            return ResolvedInvocation(
                entry_plugin=entry_plugin,
                entry_argv=entry_argv,
                resolved_plugin="search",
                resolved_argv=resolved_argv,
                canonical_locator="search",
                preferred_name=preferred,
                content_type="text/markdown",
                artifact_intent="none",
                artifact_kind="",
                should_materialize=False,
                provider="search",
            )
        canonical = _canonical_locator(
            search_route.provider, search_route.provider_argv
        )
        preferred = _preferred_name(
            search_route.provider, search_route.provider_argv, options
        )
        content_type = _infer_content_type(
            search_route.provider, search_route.provider_argv, preferred
        )
        intent: ArtifactIntent = "discovery"
        return ResolvedInvocation(
            entry_plugin=entry_plugin,
            entry_argv=entry_argv,
            resolved_plugin=search_route.provider,
            resolved_argv=search_route.provider_argv,
            canonical_locator=canonical,
            preferred_name=preferred,
            content_type=content_type,
            artifact_intent=intent,
            artifact_kind=_artifact_kind(intent),
            should_materialize=_materialization_enabled(),
            provider=search_route.provider,
        )
    canonical = _canonical_locator(plugin, argv)
    preferred = _preferred_name(plugin, argv, options)
    content_type = _infer_content_type(plugin, argv, preferred)
    intent = _artifact_intent(plugin, argv)
    return ResolvedInvocation(
        entry_plugin=entry_plugin,
        entry_argv=entry_argv,
        resolved_plugin=plugin,
        resolved_argv=argv,
        canonical_locator=canonical,
        preferred_name=preferred,
        content_type=content_type,
        artifact_intent=intent,
        artifact_kind=_artifact_kind(intent),
        should_materialize=intent != "none" and _materialization_enabled(),
        provider=plugin,
    )


def should_materialize(plugin: str, argv: list[str]) -> bool:
    return resolve_invocation(plugin, argv, CommonOptions()).should_materialize


def invocation_locator(plugin: str, argv: list[str]) -> str:
    return _invocation_locator(plugin, argv)


def canonical_locator(plugin: str, argv: list[str]) -> str:
    return resolve_invocation(plugin, argv, CommonOptions()).canonical_locator


def preferred_name(plugin: str, argv: list[str], options: CommonOptions) -> str:
    return resolve_invocation(plugin, argv, options).preferred_name


def infer_content_type(plugin: str, argv: list[str], name: str) -> str:
    resolved = resolve_invocation(plugin, argv, CommonOptions())
    if resolved.preferred_name == name:
        return resolved.content_type
    return _infer_content_type(resolved.resolved_plugin, resolved.resolved_argv, name)
