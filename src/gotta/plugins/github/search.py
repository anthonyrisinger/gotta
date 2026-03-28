"""GitHub search payloads and markdown rendering."""

from __future__ import annotations

import re
import urllib.parse

from gotta.source.render import (
    render_source_metadata_lines,
    render_visibility_metadata_lines,
)
from gotta.source.stamp import derive_source_metadata_from_payload
from gotta.source.visibility import with_visibility_metadata

from .api import gh_json_object, gh_json_value


def _int_value(value: object) -> int:
    return int(value) if isinstance(value, int) else 0


def _dict_items(value: object) -> list[dict[str, object]]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


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
            "stars": _int_value(item.get("stargazers_count")),
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
        return str(
            item.get("html_url") or item.get("full_name") or item.get("name") or ""
        )
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
        total_count += _int_value(payload.get("total_count"))
        candidates = _dict_items(payload.get("items"))
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
        if _search_item_owner(item, search_type=search_type).casefold()
        not in excluded_owners
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
        global_total = _int_value(payload.get("total_count"))
        global_items = _dict_items(payload.get("items"))
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
        global_total = _int_value(payload.get("total_count"))
        global_items = _dict_items(payload.get("items"))
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
    payload = with_visibility_metadata(dict(payload), provider="github")
    search_type = str(payload.get("type") or "repo")
    _ = _search_type_label(search_type)
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
        f"- _Matches_: {_int_value(payload.get('resultCount')) or len(results)}",
    ]
    scope_repo = str(payload.get("scopeRepo") or "")
    if scope_repo:
        lines.append(f"- _Repo Scope_: `{scope_repo}`")
    else:
        search_plan = str(payload.get("searchPlan") or "")
        global_result_count = _int_value(payload.get("globalResultCount"))
        if search_plan == "owned-only":
            lines.append(
                "- _Search scope_: owned repositories and visible organizations only"
            )
        elif search_plan == "global-excluding-owned":
            lines.append(
                "- _Search scope_: global GitHub excluding owned-scope results"
            )
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
    lines.extend(
        render_source_metadata_lines(derive_source_metadata_from_payload(payload))
    )
    lines.append("")
    if search_type == "repo":
        for item in results:
            if not isinstance(item, dict):
                continue
            full_name = str(item.get("fullName") or item.get("name") or "(unknown)")
            url = str(item.get("url") or "")
            line = f"- [{full_name}]({url})" if url else f"- {full_name}"
            details: list[str] = []
            item_language = str(item.get("language") or "")
            created = str(item.get("createdAt") or "")
            updated = str(item.get("updatedAt") or "")
            pushed = str(item.get("pushedAt") or "")
            stars = item.get("stars")
            if item_language:
                details.append(f"language `{item_language}`")
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
            label = (
                f"{repository}:{path}"
                if repository and path
                else (path or repository or "code result")
            )
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
