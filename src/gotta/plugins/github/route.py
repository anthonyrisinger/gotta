"""GitHub target routing and URL shape recognition."""

from __future__ import annotations

import re

from gotta.resolve.route import query_route, split_locator_tail, strip_http_url_fragment


BLOB_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")
TREE_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)(?:/(.*))?$")
PULL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/([0-9]+)(/.*)?$")
ISSUE_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/issues/([0-9]+)(/.*)?$")
COMMIT_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/commit/([0-9a-f]{7,40})(/.*)?$"
)
COMMITS_ROOT_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/commits/?$")
COMMITS_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/commits/([^/]+)(/.*)?$")
ACTIONS_JOB_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/actions/runs/([0-9]+)/job/([0-9]+)(/.*)?$"
)
ACTIONS_RUN_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/actions/runs/([0-9]+)(/.*)?$"
)
RELEASES_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/releases/?$")
RELEASE_TAG_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/releases/tag/([^/]+)(/.*)?$"
)
REPO_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/?$")


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
        ACTIONS_JOB_RE,
        ACTIONS_RUN_RE,
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
