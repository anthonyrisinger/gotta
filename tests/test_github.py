from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import gotta.plugins.github.main as github
import gotta.plugins.github.read as github_read
import gotta.plugins.github.search as github_search


def test_parse_args_supports_status_subcommand() -> None:
    assert github.parse_args(["status"]) == github.ParsedArgs(
        command="status", output="summary"
    )
    assert github.parse_args(["status", "--output", "summary"]) == github.ParsedArgs(
        command="status",
        output="summary",
    )


def test_parse_args_defaults_render_to_markdown() -> None:
    assert github.parse_args(["https://github.com/acme/widgets"]) == github.ParsedArgs(
        command="render",
        output="markdown",
        url="https://github.com/acme/widgets",
    )
    assert github.parse_args(
        ["--output", "markdown", "https://github.com/acme/widgets"]
    ) == github.ParsedArgs(
        command="render",
        output="markdown",
        url="https://github.com/acme/widgets",
    )
    assert github.parse_args(
        ["https://github.com/acme/widgets", "--output", "markdown"]
    ) == github.ParsedArgs(
        command="render",
        output="markdown",
        url="https://github.com/acme/widgets",
    )


def test_github_canonical_locator_and_preferred_name_ignore_flag_order() -> None:
    argv = ["https://github.com/acme/widgets", "--output", "summary"]
    options = SimpleNamespace(save_as="")

    assert github.canonical_locator(argv) == "https://github.com/acme/widgets"
    assert github.preferred_name(argv, options) == "widgets.json"


def test_github_capture_canonicalizes_volatile_download_tokens() -> None:
    payload = {
        "download_url": "https://raw.githubusercontent.com/acme/widgets/main/README.md?token=secret",
        "html_url": "https://github.com/acme/widgets/blob/main/README.md",
        "nested": [
            "https://raw.githubusercontent.com/acme/widgets/main/app.py?token=another",
        ],
    }

    canonical = github._canonicalize_capture_value(payload)

    assert (
        canonical["download_url"]
        == "https://raw.githubusercontent.com/acme/widgets/main/README.md"
    )
    assert (
        canonical["html_url"] == "https://github.com/acme/widgets/blob/main/README.md"
    )
    assert canonical["nested"] == [
        "https://raw.githubusercontent.com/acme/widgets/main/app.py"
    ]


def test_github_repo_capture_canonicalizes_directory_entries_before_storage(
    monkeypatch,
) -> None:
    monkeypatch.setattr(github, "ensure_gh", lambda: object())
    monkeypatch.setattr(github, "ensure_gh_auth", lambda _gh: None)
    monkeypatch.setattr(
        github,
        "gh_json_object",
        lambda _gh, _args: {
            "name": "widgets",
            "visibility": "private",
            "defaultBranchRef": {"name": "main"},
            "url": "https://github.com/acme/widgets",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-02T00:00:00Z",
            "pushedAt": "2026-01-02T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        github,
        "list_directory_entries",
        lambda _gh, **_kwargs: [
            {
                "name": "README.md",
                "path": "README.md",
                "type": "file",
                "download_url": "https://raw.githubusercontent.com/acme/widgets/main/README.md?token=secret",
            }
        ],
    )
    monkeypatch.setattr(
        github,
        "readme_rollup",
        lambda *_args, **_kwargs: ("README.md", "Synthetic summary"),
    )

    capture = github.capture(["https://github.com/acme/widgets"], object())
    payload = json.loads(capture.data.decode("utf-8"))

    assert payload["entries"][0]["download_url"] == (
        "https://raw.githubusercontent.com/acme/widgets/main/README.md"
    )


def test_parse_args_supports_search_surface() -> None:
    assert github.parse_args(["search", "relay"]) == github.ParsedArgs(
        command="search",
        output="markdown",
        query="relay",
        search_type="repo",
        repo="",
        limit=10,
    )
    assert github.parse_args(["search", "--global", "ABC"]) == github.ParsedArgs(
        command="search",
        output="markdown",
        query="ABC",
        search_type="repo",
        repo="",
        limit=10,
        global_search=True,
    )
    assert github.parse_args(
        [
            "search",
            "--type",
            "pr",
            "--repo",
            "acme/widgets",
            "--limit",
            "5",
            "ABC",
            "proxy",
        ]
    ) == github.ParsedArgs(
        command="search",
        output="markdown",
        query="ABC proxy",
        search_type="pr",
        repo="acme/widgets",
        limit=5,
    )
    assert github.parse_args(
        [
            "search",
            "--type",
            "code",
            "--repo",
            "acme/widgets",
            "--filename",
            "package.json",
            "--extension",
            "json",
            "--match",
            "path",
            "lint",
        ]
    ) == github.ParsedArgs(
        command="search",
        output="markdown",
        query="lint",
        search_type="code",
        repo="acme/widgets",
        filename="package.json",
        extension="json",
        match="path",
        limit=10,
    )


def test_parse_args_supports_search_help(capsys) -> None:
    assert github.main(["search", "--help"]) == 0
    output = capsys.readouterr().out
    assert "usage: gotta github search" in output
    assert "--global" in output


def test_github_help_renders_once(capsys) -> None:
    assert github.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert output.count("usage: gotta github [status") == 1
    assert "/commits/HEAD" in output


def test_github_search_canonical_locator_and_preferred_name() -> None:
    options = SimpleNamespace(save_as="")

    argv = ["search", "--type", "pr", "--repo", "acme/widgets", "ABC", "proxy"]

    assert (
        github.canonical_locator(argv)
        == "github:search --type pr --repo acme/widgets ABC proxy"
    )
    assert (
        github.preferred_name(argv, options)
        == "github-search-prs-acme-widgets-abc-proxy.json"
    )
    assert (
        github.canonical_locator(["search", "--global", "ABC"])
        == "github:search --global ABC"
    )
    assert (
        github.preferred_name(["search", "--global", "ABC"], options)
        == "github-search-repos-global-abc.json"
    )
    code_argv = [
        "search",
        "--type",
        "code",
        "--repo",
        "acme/widgets",
        "--filename",
        "package.json",
        "lint",
    ]
    assert (
        github.canonical_locator(code_argv)
        == "github:search --type code --repo acme/widgets --filename package.json lint"
    )
    assert (
        github.preferred_name(code_argv, options)
        == "github-search-code-acme-widgets-file-package.json-lint.json"
    )


def test_github_preferred_name_is_specific_for_rendered_object_urls() -> None:
    options = SimpleNamespace(save_as="")

    assert (
        github.preferred_name(["https://github.com/acme/widgets/pull/19"], options)
        == "widgets-pr-19.json"
    )
    assert (
        github.preferred_name(["https://github.com/acme/widgets/issues/19"], options)
        == "widgets-issue-19.json"
    )
    assert (
        github.preferred_name(["https://github.com/acme/widgets/commits/main"], options)
        == "widgets-commits-main.json"
    )
    assert (
        github.preferred_name(["https://github.com/acme/widgets/commits"], options)
        == "widgets-commits.json"
    )
    assert (
        github.preferred_name(
            ["https://github.com/acme/widgets/blob/main/docs/quickstart.md"],
            options,
        )
        == "widgets-blob-main-docs-quickstart.md.json"
    )
    assert (
        github.preferred_name(
            ["https://github.com/acme/widgets/actions/runs/123456789"],
            options,
        )
        == "widgets-run-123456789.json"
    )
    assert (
        github.preferred_name(
            ["https://github.com/acme/widgets/actions/runs/123456789/job/987654321"],
            options,
        )
        == "widgets-run-123456789-job-987654321.json"
    )


def test_route_target_accepts_clean_supported_github_urls() -> None:
    url = "https://github.com/acme/widgets/commits/main"

    assert github.route_target(url) == [url]
    assert github.route_target(f"{url}#history") == [f"{url}#history"]
    assert github.route_target("github:github.com/acme/widgets/commits/main") == [url]
    run_url = "https://github.com/acme/widgets/actions/runs/123456789"
    job_url = "https://github.com/acme/widgets/actions/runs/123456789/job/987654321"
    assert github.route_target(run_url) == [run_url]
    assert github.route_target(job_url) == [job_url]
    assert github.route_target(
        "github:github.com/acme/widgets/actions/runs/123456789"
    ) == [run_url]


def test_route_target_strips_fragments_from_supported_tree_urls() -> None:
    url = "https://github.com/acme/widgets/tree/main/docs"

    assert github.route_target(f"{url}#readme") == [f"{url}#readme"]


def test_route_target_rejects_whitespace_contaminated_github_urls() -> None:
    url = "https://github.com/acme/widgets/commits/main"

    assert github.route_target(f"{url} --limit 20") is None
    assert (
        github.route_target("github:github.com/acme/widgets/commits/main --limit 20")
        is None
    )


def test_main_supports_commit_urls(monkeypatch, capsys) -> None:
    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)
    monkeypatch.setattr(
        github,
        "gh_json_object",
        lambda gh, args: {
            "sha": "abcdef1",
            "html_url": "https://github.com/acme/widgets/commit/abcdef1",
            "commit": {
                "message": "Tighten control surface",
                "author": {"name": "Alice", "date": "2026-03-12T12:00:00Z"},
            },
            "files": [
                {
                    "filename": "README.md",
                    "status": "modified",
                    "additions": 3,
                    "deletions": 1,
                    "changes": 4,
                }
            ],
        },
    )

    assert github.main(["https://github.com/acme/widgets/commit/abcdef1"]) == 0
    output = capsys.readouterr().out
    assert "# acme/widgets commit abcdef1" in output
    assert "## Files" in output


def test_main_supports_workflow_run_urls(monkeypatch, capsys) -> None:
    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)
    monkeypatch.setattr(
        github,
        "workflow_run_payload",
        lambda gh, owner, repo, run_id: {
            "number": 1529,
            "displayTitle": "Generic deployment",
            "workflowName": "Generic Workflow",
            "url": "https://github.com/acme/widgets/actions/runs/123456789",
            "status": "completed",
            "conclusion": "failure",
            "event": "workflow_dispatch",
            "headBranch": "main",
            "headSha": "abcdef1234567890",
            "createdAt": "2026-03-25T22:31:51Z",
            "startedAt": "2026-03-25T22:31:55Z",
            "updatedAt": "2026-03-25T22:34:22Z",
            "jobs": [
                {
                    "name": "Generic Job",
                    "url": "https://github.com/acme/widgets/actions/runs/123456789/job/987654321",
                    "status": "completed",
                    "conclusion": "failure",
                    "startedAt": "2026-03-25T22:33:52Z",
                    "completedAt": "2026-03-25T22:34:21Z",
                }
            ],
        },
    )

    assert github.main(["https://github.com/acme/widgets/actions/runs/123456789"]) == 0
    output = capsys.readouterr().out
    assert "# acme/widgets workflow run #1529: Generic deployment" in output
    assert "## Jobs" in output
    assert "Generic Job" in output


def test_main_supports_workflow_job_urls(monkeypatch, capsys) -> None:
    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)
    monkeypatch.setattr(
        github,
        "workflow_job_payload",
        lambda gh, owner, repo, run_id, job_id: {
            "id": 987654321,
            "run_id": 123456789,
            "workflow_name": "Generic Workflow",
            "html_url": "https://github.com/acme/widgets/actions/runs/123456789/job/987654321",
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-03-25T22:32:52Z",
            "started_at": "2026-03-25T22:33:52Z",
            "completed_at": "2026-03-25T22:34:21Z",
            "name": "Generic Job",
            "head_branch": "main",
            "head_sha": "abcdef1234567890",
            "labels": ["generic-runner"],
            "runner_name": "runner-01",
            "runner_group_name": "generic-group",
            "steps": [
                {
                    "name": "Generic Step",
                    "status": "completed",
                    "conclusion": "failure",
                    "number": 12,
                    "started_at": "2026-03-25T22:34:14Z",
                    "completed_at": "2026-03-25T22:34:19Z",
                }
            ],
        },
    )

    assert (
        github.main(
            ["https://github.com/acme/widgets/actions/runs/123456789/job/987654321"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "# acme/widgets workflow job 987654321: Generic Job" in output
    assert "## Steps" in output
    assert "Generic Step" in output


def test_capture_and_project_support_workflow_job_urls(monkeypatch) -> None:
    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)
    monkeypatch.setattr(
        github,
        "workflow_job_payload",
        lambda gh, owner, repo, run_id, job_id: {
            "id": 987654321,
            "run_id": 123456789,
            "workflow_name": "Generic Workflow",
            "html_url": "https://github.com/acme/widgets/actions/runs/123456789/job/987654321",
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-03-25T22:32:52Z",
            "started_at": "2026-03-25T22:33:52Z",
            "completed_at": "2026-03-25T22:34:21Z",
            "name": "Generic Job",
            "head_branch": "main",
            "head_sha": "abcdef1234567890",
            "steps": [
                {
                    "name": "Generic Step",
                    "status": "completed",
                    "conclusion": "failure",
                    "number": 12,
                    "started_at": "2026-03-25T22:34:14Z",
                    "completed_at": "2026-03-25T22:34:19Z",
                }
            ],
        },
    )

    capture = github.capture(
        ["https://github.com/acme/widgets/actions/runs/123456789/job/987654321"],
        object(),
    )

    assert capture.meta["github_kind"] == "workflow_job"
    rendered = github.project(
        ["https://github.com/acme/widgets/actions/runs/123456789/job/987654321"],
        capture,
    ).decode("utf-8")
    assert "# acme/widgets workflow job 987654321: Generic Job" in rendered
    assert "Generic Step" in rendered


def test_main_supports_commit_history_urls(monkeypatch, capsys) -> None:
    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)
    monkeypatch.setattr(
        github,
        "gh_json_value",
        lambda gh, args: [
            {
                "sha": "abcdef123456",
                "html_url": "https://github.com/acme/widgets/commit/abcdef123456",
                "commit": {
                    "message": "Add continuity model\n\nDetails",
                    "author": {"name": "Alice", "date": "2026-03-12T12:00:00Z"},
                },
            }
        ],
    )

    assert github.main(["https://github.com/acme/widgets/commits/main"]) == 0
    output = capsys.readouterr().out
    assert "# acme/widgets commit history for `main`" in output
    assert "- **Created:** 2026-03-12T12:00:00Z" in output
    assert "- **Updated:** 2026-03-12T12:00:00Z" in output
    assert "Add continuity model" in output


def test_main_supports_commit_history_limit_for_url_renders(
    monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []

    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)

    def fake_gh_json_value(gh, args):
        seen.append(args)
        return [
            {
                "sha": "abcdef123456",
                "html_url": "https://github.com/acme/widgets/commit/abcdef123456",
                "commit": {
                    "message": "Add continuity model\n\nDetails",
                    "author": {"name": "Alice", "date": "2026-03-12T12:00:00Z"},
                },
            }
        ]

    monkeypatch.setattr(github, "gh_json_value", fake_gh_json_value)

    assert (
        github.main(["https://github.com/acme/widgets/commits/HEAD", "--limit", "50"])
        == 0
    )
    assert "&per_page=50" in seen[0][1]
    output = capsys.readouterr().out
    assert "# acme/widgets commit history for `HEAD`" in output


def test_main_rejects_limit_for_non_list_url_shapes(monkeypatch, capsys) -> None:
    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)

    assert (
        github.main(["https://github.com/acme/widgets/issues/19", "--limit", "10"]) == 2
    )
    err = capsys.readouterr().err
    assert "`--limit` is only supported for GitHub commit-history URLs" in err
    assert "/commits/HEAD" in err


def test_capture_rejects_limit_for_non_list_url_shapes(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        github.capture(
            ["https://github.com/acme/widgets/issues/19", "--limit", "10"], object()
        )

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "`--limit` is only supported for GitHub commit-history URLs" in err
    assert "/commits/HEAD" in err


def test_main_supports_commit_history_root_urls(monkeypatch, capsys) -> None:
    seen: list[list[str]] = []

    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)

    def fake_default_branch_name(gh: str, *, owner: str, repo: str) -> str:
        seen.append(["api", f"repos/{owner}/{repo}"])
        return "main"

    monkeypatch.setattr(github, "default_branch_name", fake_default_branch_name)
    monkeypatch.setattr(
        github,
        "gh_json_value",
        lambda gh, args: [
            {
                "sha": "abcdef123456",
                "html_url": "https://github.com/acme/widgets/commit/abcdef123456",
                "commit": {
                    "message": "Add continuity model\n\nDetails",
                    "author": {"name": "Alice", "date": "2026-03-12T12:00:00Z"},
                },
            }
        ],
    )

    assert github.main(["https://github.com/acme/widgets/commits"]) == 0
    output = capsys.readouterr().out
    assert seen == [["api", "repos/acme/widgets"]]
    assert "# acme/widgets commit history for `main`" in output


def test_main_supports_path_scoped_commit_history_urls(monkeypatch, capsys) -> None:
    seen: list[list[str]] = []

    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)

    def fake_gh_json_value(gh, args):
        seen.append(args)
        return [
            {
                "sha": "abcdef123456",
                "html_url": "https://github.com/acme/widgets/commit/abcdef123456",
                "commit": {
                    "message": "Add continuity model\n\nDetails",
                    "author": {"name": "Alice", "date": "2026-03-12T12:00:00Z"},
                },
            }
        ]

    monkeypatch.setattr(github, "gh_json_value", fake_gh_json_value)

    assert github.main(["https://github.com/acme/widgets/commits/main/docs/adr"]) == 0
    output = capsys.readouterr().out
    assert "&path=docs/adr" in seen[0][1]
    assert "- **Path:** `docs/adr`" in output
    assert "- **URL:** https://github.com/acme/widgets/commits/main/docs/adr" in output


def test_main_commit_history_invalid_ref_suggests_default_branch(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)
    monkeypatch.setattr(
        github,
        "gh_json_value",
        lambda gh, args: (_ for _ in ()).throw(
            RuntimeError("gh: Not Found (HTTP 404)")
        ),
    )
    monkeypatch.setattr(github, "default_branch_name", lambda gh, owner, repo: "trunk")

    assert github.main(["https://github.com/acme/widgets/commits/main"]) == 1
    err = capsys.readouterr().err
    assert "commit-history ref `main` was not found" in err
    assert (
        "Specific `/commits/<ref>` URLs require a real branch, tag, or commit-ish."
        in err
    )
    assert "https://github.com/acme/widgets/commits" in err
    assert "https://github.com/acme/widgets/commits/HEAD" in err
    assert "https://github.com/acme/widgets/commits/trunk" in err


def test_main_supports_repo_search(monkeypatch, capsys) -> None:
    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)
    monkeypatch.setattr(
        github_search, "_accessible_owner_targets", lambda gh: [("org", "acme")]
    )

    def fake_gh_json_object(gh, args):
        assert "search/repositories" in args[1]
        if "org:acme" in args[1]:
            return {
                "total_count": 1,
                "items": [
                    {
                        "full_name": "acme/relay-core",
                        "name": "relay-core",
                        "html_url": "https://github.com/acme/relay-core",
                        "description": "WireGuard overlay",
                        "language": "Go",
                        "stargazers_count": 17,
                        "created_at": "2026-01-01T10:00:00Z",
                        "updated_at": "2026-03-10T12:00:00Z",
                        "pushed_at": "2026-03-11T09:30:00Z",
                        "default_branch": "main",
                        "owner": {"login": "acme"},
                    }
                ],
            }
        raise AssertionError(
            "default owned-scope search should not hit the global corpus"
        )

    monkeypatch.setattr(github_search, "gh_json_object", fake_gh_json_object)

    assert github.main(["search", "relay"]) == 0
    output = capsys.readouterr().out
    assert "### GitHub Search: relay" in output
    assert (
        "- _Search scope_: owned repositories and visible organizations only" in output
    )
    assert "- Created:" in output
    assert "- Updated: 2026-03-11T09:30:00Z" in output
    assert "[acme/relay-core](https://github.com/acme/relay-core)" in output
    assert "[public/noise]" not in output


def test_main_supports_global_repo_search_excluding_owned_results(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)
    monkeypatch.setattr(
        github_search, "_accessible_owner_targets", lambda gh: [("org", "acme")]
    )

    def fake_gh_json_object(gh, args):
        assert "search/repositories" in args[1]
        return {
            "total_count": 3,
            "items": [
                {
                    "full_name": "acme/relay-core",
                    "name": "relay-core",
                    "html_url": "https://github.com/acme/relay-core",
                    "description": "Owned result",
                    "owner": {"login": "acme"},
                },
                {
                    "full_name": "public/noise",
                    "name": "noise",
                    "html_url": "https://github.com/public/noise",
                    "description": "Global noise",
                    "owner": {"login": "public"},
                },
                {
                    "full_name": "public/abc",
                    "name": "abc",
                    "html_url": "https://github.com/public/abc",
                    "description": "Global ABC noise",
                    "owner": {"login": "public"},
                },
            ],
        }

    monkeypatch.setattr(github_search, "gh_json_object", fake_gh_json_object)

    assert github.main(["search", "--global", "ABC"]) == 0
    output = capsys.readouterr().out
    assert "- _Search scope_: global GitHub excluding owned-scope results" in output
    assert "[acme/relay-core]" not in output
    assert "[public/noise](https://github.com/public/noise)" in output


def test_main_supports_pull_request_search(monkeypatch, capsys) -> None:
    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)
    monkeypatch.setattr(
        github_search,
        "gh_json_object",
        lambda gh, args: {
            "total_count": 1,
            "items": [
                {
                    "title": "Replace ABC gateway",
                    "number": 27,
                    "html_url": "https://github.com/acme/widgets/pull/27",
                    "state": "open",
                    "user": {"login": "alice"},
                    "repository_url": "https://api.github.com/repos/acme/widgets",
                    "created_at": "2026-03-01T10:00:00Z",
                    "updated_at": "2026-03-12T09:15:00Z",
                    "body": "This replaces ABC.",
                    "pull_request": {
                        "url": "https://api.github.com/repos/acme/widgets/pulls/27"
                    },
                    "labels": [{"name": "continuity"}],
                }
            ],
        },
    )

    assert github.main(["search", "--type", "pr", "--repo", "acme/widgets", "ABC"]) == 0
    output = capsys.readouterr().out
    assert "### GitHub Search: ABC" in output
    assert "- _Repo Scope_: `acme/widgets`" in output
    assert "- Created: 2026-03-01T10:00:00Z" in output
    assert (
        "[acme/widgets pr #27: Replace ABC gateway](https://github.com/acme/widgets/pull/27)"
        in output
    )


def test_main_supports_code_search(monkeypatch, capsys) -> None:
    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)
    monkeypatch.setattr(
        github,
        "search_code_payload",
        lambda gh, **kwargs: {
            "surface": "github",
            "type": "code",
            "query": "lint",
            "scopeRepo": "acme/widgets",
            "filename": "package.json",
            "extension": "",
            "language": "",
            "match": "",
            "searchPlan": "repo-scope",
            "accessibleOwners": [],
            "scopedResultCount": 0,
            "globalResultCount": 1,
            "resultCount": 1,
            "results": [
                {
                    "kind": "code",
                    "repository": "acme/widgets",
                    "path": "package.json",
                    "sha": "abcdef123456",
                    "url": "https://github.com/acme/widgets/blob/abcdef123456/package.json",
                    "textMatches": ['"lint": "npm run lint"'],
                }
            ],
        },
    )

    assert (
        github.main(
            [
                "search",
                "--type",
                "code",
                "--repo",
                "acme/widgets",
                "--filename",
                "package.json",
                "lint",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "- _Type_: `code`" in output
    assert "- _Filename_: `package.json`" in output
    assert (
        "[acme/widgets:package.json](https://github.com/acme/widgets/blob/abcdef123456/package.json)"
        in output
    )
    assert '"lint": "npm run lint"' in output


def test_search_payload_defaults_to_owned_scope(monkeypatch) -> None:
    monkeypatch.setattr(
        github_search, "_accessible_owner_targets", lambda gh: [("org", "acme")]
    )

    def fake_gh_json_object(gh, args):
        if "org:acme" in args[1]:
            return {
                "total_count": 1,
                "items": [
                    {
                        "full_name": "acme/signal",
                        "name": "signal",
                        "html_url": "https://github.com/acme/signal",
                        "owner": {"login": "acme"},
                        "visibility": "internal",
                    }
                ],
            }
        raise AssertionError(
            "default owned-scope search should not hit the global corpus"
        )

    monkeypatch.setattr(github_search, "gh_json_object", fake_gh_json_object)

    payload = github_search.search_repositories_payload(
        "gh",
        query="ABC",
        repo="",
        limit=2,
        global_search=False,
    )

    assert payload["searchPlan"] == "owned-only"
    assert payload["results"][0]["fullName"] == "acme/signal"
    assert payload["results"][0]["visibility_level"] == "internal"
    assert payload["results"][0]["visibility_boundary"] == "same_company"
    assert payload["scopedResultCount"] == 1
    assert payload["globalResultCount"] == 0


def test_search_payload_global_excludes_owned_hits(monkeypatch) -> None:
    monkeypatch.setattr(
        github_search, "_accessible_owner_targets", lambda gh: [("org", "acme")]
    )
    monkeypatch.setattr(
        github_search,
        "gh_json_object",
        lambda gh, args: {
            "total_count": 3,
            "items": [
                {
                    "full_name": "acme/signal",
                    "name": "signal",
                    "html_url": "https://github.com/acme/signal",
                    "owner": {"login": "acme"},
                },
                {
                    "full_name": "public/noise",
                    "name": "noise",
                    "html_url": "https://github.com/public/noise",
                    "owner": {"login": "public"},
                },
                {
                    "full_name": "public/other-noise",
                    "name": "other-noise",
                    "html_url": "https://github.com/public/other-noise",
                    "owner": {"login": "public"},
                },
            ],
        },
    )

    payload = github_search.search_repositories_payload(
        "gh",
        query="ABC",
        repo="",
        limit=3,
        global_search=True,
    )

    assert payload["searchPlan"] == "global-excluding-owned"
    assert [item["fullName"] for item in payload["results"]] == [
        "public/noise",
        "public/other-noise",
    ]


def test_main_falls_back_to_directory_readme_for_readme_blob(monkeypatch) -> None:
    seen: dict[str, object] = {}

    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)

    def fake_fetch(gh, *, owner, repo, ref, path):
        raise RuntimeError("404")

    monkeypatch.setattr(github, "fetch_content_file", fake_fetch)
    monkeypatch.setattr(
        github,
        "load_directory_readme",
        lambda gh, *, owner, repo, ref, path="", entries=None: (
            "docs/README.md",
            b"# Docs\n",
        ),
    )
    monkeypatch.setattr(
        github,
        "render_content",
        lambda data, path: seen.update({"data": data, "path": path}),
    )

    assert (
        github.main(
            [
                "--output",
                "markdown",
                "https://github.com/acme/widgets/blob/main/README.md",
            ]
        )
        == 0
    )
    assert seen["path"] == "docs/README.md"
    assert seen["data"] == b"# Docs\n"


def test_main_repo_markdown_falls_back_to_directory_listing(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)
    monkeypatch.setattr(
        github,
        "gh_json_object",
        lambda gh, args: {
            "name": "widgets",
            "visibility": "private",
            "url": "https://github.com/acme/widgets",
            "createdAt": "2026-03-01T10:00:00Z",
            "updatedAt": "2026-03-10T11:30:00Z",
            "pushedAt": "2026-03-11T09:15:00Z",
            "defaultBranchRef": {"name": "main"},
        },
    )
    monkeypatch.setattr(github, "load_directory_readme", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        github,
        "list_directory_entries",
        lambda gh, *, owner, repo, ref, path="": [
            {"name": "README.md", "path": "README.md", "type": "file", "size": 12},
            {"name": "docs", "path": "docs", "type": "dir"},
        ],
    )

    assert github.main(["--output", "markdown", "https://github.com/acme/widgets"]) == 0
    output = capsys.readouterr().out
    assert "# widgets" in output
    assert "- Visibility: restricted (same_company, high)" in output
    assert "https://github.com/acme/widgets/tree/main" in output
    assert (
        "- **README:** [README.md](https://github.com/acme/widgets/blob/main/README.md)"
        in output
    )


def test_main_repo_markdown_uses_fragment_to_render_root_readme(monkeypatch) -> None:
    seen: dict[str, object] = {}

    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)
    monkeypatch.setattr(
        github,
        "gh_json_object",
        lambda gh, args: {
            "name": "widgets",
            "visibility": "private",
            "url": "https://github.com/acme/widgets",
            "createdAt": "2026-03-01T10:00:00Z",
            "updatedAt": "2026-03-10T11:30:00Z",
            "pushedAt": "2026-03-11T09:15:00Z",
            "defaultBranchRef": {"name": "main"},
        },
    )
    monkeypatch.setattr(
        github,
        "list_directory_entries",
        lambda gh, *, owner, repo, ref, path="": [
            {"name": "README.md", "path": "README.md", "type": "file"},
            {"name": "docs", "path": "docs", "type": "dir"},
        ],
    )
    monkeypatch.setattr(
        github_read,
        "fetch_content_file",
        lambda gh, *, owner, repo, ref, path: {
            "type": "file",
            "encoding": "base64",
            "content": "IyBSb290IERvY3MK",
        },
    )
    monkeypatch.setattr(
        github,
        "render_content",
        lambda data, path: seen.update({"data": data, "path": path}),
    )

    assert github.main(["https://github.com/acme/widgets#readme"]) == 0
    assert seen["path"] == "README.md"
    assert seen["data"] == b"# Root Docs\n"


def test_main_tree_markdown_defaults_to_directory_listing_even_with_readme(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)
    monkeypatch.setattr(
        github,
        "list_directory_entries",
        lambda gh, *, owner, repo, ref, path="": [
            {"name": "README.md", "path": "docs/README.md", "type": "file"},
            {"name": "QUICKSTART.md", "path": "docs/QUICKSTART.md", "type": "file"},
        ],
    )

    assert github.main(["https://github.com/acme/widgets/tree/main/docs"]) == 0
    output = capsys.readouterr().out
    assert "# acme/widgets:docs" in output
    assert (
        "- **README:** [README.md](https://github.com/acme/widgets/blob/main/docs/README.md)"
        in output
    )
    assert "QUICKSTART.md" in output


def test_main_tree_markdown_uses_fragment_to_render_matching_document(
    monkeypatch,
) -> None:
    seen: dict[str, object] = {}

    monkeypatch.setattr(github, "ensure_gh", lambda: "gh")
    monkeypatch.setattr(github, "ensure_gh_auth", lambda gh: None)
    monkeypatch.setattr(
        github,
        "list_directory_entries",
        lambda gh, *, owner, repo, ref, path="": [
            {"name": "README.md", "path": "docs/README.md", "type": "file"},
            {"name": "QUICKSTART.md", "path": "docs/QUICKSTART.md", "type": "file"},
        ],
    )
    monkeypatch.setattr(
        github_read,
        "fetch_content_file",
        lambda gh, *, owner, repo, ref, path: {
            "type": "file",
            "encoding": "base64",
            "content": "IyBRdWlja3N0YXJ0CgpVc2UgaXQgZmlyc3QuCg==",
        },
    )
    monkeypatch.setattr(
        github,
        "render_content",
        lambda data, path: seen.update({"data": data, "path": path}),
    )

    assert (
        github.main(["https://github.com/acme/widgets/tree/main/docs#quickstart"]) == 0
    )
    assert seen["path"] == "docs/QUICKSTART.md"
    assert seen["data"] == b"# Quickstart\n\nUse it first.\n"


def test_markdown_repo_includes_source_times() -> None:
    rendered = github.markdown_repo(
        {
            "name": "widgets",
            "url": "https://github.com/acme/widgets",
            "visibility": "private",
            "createdAt": "2026-03-01T10:00:00Z",
            "updatedAt": "2026-03-10T11:30:00Z",
            "pushedAt": "2026-03-11T09:15:00Z",
            "defaultBranchRef": {"name": "main"},
        }
    )

    assert "- **Created:** 2026-03-01T10:00:00Z" in rendered
    assert "- **Updated:** 2026-03-10T11:30:00Z" in rendered
    assert "- **Pushed:** 2026-03-11T09:15:00Z" in rendered
    assert "- Visibility: restricted (same_company, high)" in rendered
