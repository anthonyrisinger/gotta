"""Canonicalize lead targets and infer providers."""

from __future__ import annotations

import re
import urllib.parse

from gotta.providers import atlassian as atl

SLACK_PERMALINK_RE = re.compile(
    r"https://[^/.]+\.slack\.com/archives/(?P<channel>[A-Z0-9]+)(?:/p(?P<pnum>[0-9]{16}))?"
)
JIRA_BROWSE_RE = re.compile(r"/browse/(?P<issue>[A-Z][A-Z0-9]+-\d+)(?:/|$)")
GDOC_URL_RE = re.compile(r"/document/d/(?P<doc_id>[A-Za-z0-9_-]+)(?:/|$)")
GSHEET_URL_RE = re.compile(r"/spreadsheets/d/(?P<sheet_id>[A-Za-z0-9_-]+)(?:/|$)")
GDRIVE_FILE_RE = re.compile(r"/file/d/(?P<file_id>[A-Za-z0-9_-]+)(?:/|$)")
LOW_SIGNAL_WEB_HOSTS = {
    "127.0.0.1",
    "localhost",
    "www.example.com",
    "example.com",
    "img.shields.io",
}
LOW_SIGNAL_WEB_EXTENSIONS = (".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp")
LOW_SIGNAL_MEETING_HOSTS = {
    "meet.google.com",
}
LOW_SIGNAL_HOST_LABELS = {
    "admin",
    "argocd",
    "auth",
    "grafana",
    "jenkins",
    "login",
    "oauth",
    "oidc",
    "sso",
}
LOW_SIGNAL_PATH_MARKERS = (
    "/.well-known/",
    "/auth",
    "/explore",
    "/jwks",
    "/login",
    "/logout",
    "/oauth",
    "/oidc",
    "/openid",
    "/saml",
    "/signin",
    "/sso",
)


def provider_for_url(target: str) -> str:
    try:
        host = urllib.parse.urlparse(target).netloc.strip().lower()
    except ValueError:
        return "web"
    if "github.com" in host:
        return "github"
    if ".slack.com" in host or "enterprise.slack.com" in host:
        return "slack"
    if ".atlassian.net" in host:
        if "/wiki/" in target:
            return "confluence"
        return "jira"
    if "docs.google.com" in host:
        path = urllib.parse.urlparse(target).path
        if "/spreadsheets/" in path:
            return "gsheets"
        return "gdocs"
    if "drive.google.com" in host:
        return "gdrive"
    return "web"


def github_repo_reference(target: str) -> tuple[str, str]:
    try:
        parsed = urllib.parse.urlparse(target)
    except ValueError:
        return ("", "")
    if parsed.netloc.strip().lower() != "github.com":
        return ("", "")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return ("", "")
    repo_ref = f"{parts[0]}/{parts[1]}"
    if len(parts) == 2:
        return (repo_ref, "repo")
    if parts[2] == "tree" and len(parts) >= 4:
        return (repo_ref, "tree")
    if parts[2] == "blob" and len(parts) >= 4:
        return (repo_ref, "blob")
    return (repo_ref, "other")


def provider_for_locator(target: str) -> str:
    if target.startswith(("http://", "https://")):
        return provider_for_url(target)
    prefix = target.split(":", 1)[0].strip()
    if prefix in {
        "jira",
        "confluence",
        "gdocs",
        "gdrive",
        "gsheets",
        "slack",
        "artifact",
        "content",
    }:
        return prefix
    return "external"


def canonicalize_url(target: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(target)
    except ValueError:
        return target
    host = parsed.netloc.strip().lower()
    path = parsed.path
    query = parsed.query
    if host in {"www.google.com", "google.com"} and path == "/url":
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        for key in ("q", "url"):
            redirect = str((params.get(key) or [""])[0] or "").strip()
            if redirect:
                resolved = canonicalize_url(redirect)
                return resolved if resolved is not None else redirect
        filtered = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(query, keep_blank_values=True)
            if key not in {"ust", "usg"}
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
    if _is_low_signal_web_url(target, host=host, path=path):
        return None
    if ".slack.com" in host:
        query_thread_ts = ""
        for value in urllib.parse.parse_qs(query).get("thread_ts") or []:
            candidate = str(value or "").strip()
            if re.fullmatch(r"\d{10}\.\d{6}", candidate):
                query_thread_ts = candidate.replace(".", "")
                break
        match = SLACK_PERMALINK_RE.match(target)
        if match:
            channel = match.group("channel")
            pnum = match.group("pnum")
            if query_thread_ts:
                return f"slack:thread:{channel}:{query_thread_ts}"
            if pnum:
                return f"slack:thread:{channel}:{pnum}"
            return f"slack:channel:{channel}"
    if ".atlassian.net" in host:
        if path.startswith("/wiki") and not re.match(r"^/wiki(?:/|$)", path):
            return None
        issue_match = JIRA_BROWSE_RE.search(path)
        if issue_match:
            return f"jira:{issue_match.group('issue')}"
        if "/browse/" in path:
            return None
        page_id = atl.extract_confluence_page_id(target)
        if page_id:
            return f"confluence:{page_id}"
    if "docs.google.com" in host:
        doc_match = GDOC_URL_RE.search(path)
        if doc_match:
            return f"gdocs:{doc_match.group('doc_id')}"
        sheet_match = GSHEET_URL_RE.search(path)
        if sheet_match:
            return f"gsheets:{sheet_match.group('sheet_id')}"
    if "drive.google.com" in host:
        drive_match = GDRIVE_FILE_RE.search(path)
        if drive_match:
            return f"gdrive:{drive_match.group('file_id')}"
    return target


def low_signal_url_penalty(locator: str) -> int:
    try:
        parsed = urllib.parse.urlparse(locator)
    except ValueError:
        return 0
    host = parsed.netloc.strip().lower()
    if not host:
        return 0
    host_only = host.split(":", 1)[0]
    path = parsed.path.strip().lower()
    labels = {part for part in re.split(r"[.-]", host_only) if part}
    if host_only in LOW_SIGNAL_MEETING_HOSTS or host_only.endswith(".zoom.us"):
        return 3
    if any(marker in path for marker in LOW_SIGNAL_PATH_MARKERS):
        return 3
    if labels & LOW_SIGNAL_HOST_LABELS and path in {"", "/"}:
        return 2
    if path in {"", "/"}:
        return 1
    return 0


def _is_low_signal_web_url(target: str, *, host: str, path: str) -> bool:
    host_only = host.split(":", 1)[0]
    lowered = target.strip().lower()
    if host_only in LOW_SIGNAL_WEB_HOSTS:
        return True
    if host_only.endswith(".example.com"):
        return True
    if "[server_addr]" in lowered or "x.x.x.x" in lowered:
        return True
    if path.lower().endswith(LOW_SIGNAL_WEB_EXTENSIONS):
        return True
    return False
