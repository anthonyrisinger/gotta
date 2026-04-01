from __future__ import annotations

import json
from pathlib import Path

import pytest

from gotta.capture import Capture
from gotta.plugins import confluence, jira
from gotta.providers import atlassian


def test_site_root_normalizes_confluence_base_urls() -> None:
    assert atlassian.site_root("https://example.atlassian.net/wiki") == (
        "https://example.atlassian.net"
    )
    assert atlassian.site_root("https://example.atlassian.net/wiki/") == (
        "https://example.atlassian.net"
    )
    assert atlassian.site_root("https://example.atlassian.net") == (
        "https://example.atlassian.net"
    )


def test_discover_cloud_id_prefers_matching_base_url(monkeypatch) -> None:
    monkeypatch.setattr(
        atlassian,
        "api_json",
        lambda method, url, token: [
            {"id": "other", "url": "https://other.atlassian.net"},
            {"id": "wanted", "url": "https://example.atlassian.net"},
        ],
    )

    assert (
        atlassian.discover_cloud_id(
            "token",
            "https://example.atlassian.net/wiki",
            base_url_env="GOTTA_CONFLUENCE_BASE_URL",
        )
        == "wanted"
    )


def test_discover_cloud_id_reports_env_name_when_ambiguous(monkeypatch) -> None:
    monkeypatch.setattr(
        atlassian,
        "api_json",
        lambda method, url, token: [
            {"id": "a", "url": "https://a.atlassian.net"},
            {"id": "b", "url": "https://b.atlassian.net"},
        ],
    )

    with pytest.raises(atlassian.AtlassianError, match="GOTTA_JIRA_BASE_URL"):
        atlassian.discover_cloud_id("", "", base_url_env="GOTTA_JIRA_BASE_URL")


def test_discover_cloud_id_dedupes_identical_site_urls(monkeypatch) -> None:
    monkeypatch.setattr(
        atlassian,
        "api_json",
        lambda method, url, token: [
            {"id": "a", "url": "https://example.atlassian.net"},
            {"id": "b", "url": "https://example.atlassian.net/wiki"},
        ],
    )

    assert (
        atlassian.discover_cloud_id(
            "token",
            "",
            base_url_env="GOTTA_CONFLUENCE_BASE_URL",
        )
        == "a"
    )


def test_resolve_accessible_resource_uses_cached_cloud_id(monkeypatch) -> None:
    monkeypatch.setattr(
        atlassian,
        "api_json",
        lambda method, url, token: [
            {"id": "other", "url": "https://other.atlassian.net"},
            {"id": "wanted", "url": "https://example.atlassian.net/wiki"},
        ],
    )

    assert atlassian.resolve_accessible_resource(
        "token",
        "",
        base_url_env="GOTTA_CONFLUENCE_BASE_URL",
        cloud_id="wanted",
    ) == ("wanted", "https://example.atlassian.net")


def test_load_token_uses_cached_oauth_state_when_present(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(atlassian, "TOKEN_FILE", tmp_path / "oauth.json")
    monkeypatch.setattr(
        atlassian,
        "load_cached_oauth_json",
        lambda: {"access_token": "oauth-token", "expires_at": 9999999999.0},
    )

    assert atlassian.load_token(
        auth_command="confluence",
        base_url_env="GOTTA_CONFLUENCE_BASE_URL",
    ) == ("oauth-token")


def test_atlassian_next_step_prefers_native_refresh_before_auth() -> None:
    next_step = atlassian._next_atlassian_step(
        {
            "credentialsConfigured": True,
            "sessionStatus": "expired",
            "tokenPreflight": "invalid",
            "hasRefreshToken": True,
            "baseUrl": "https://example.atlassian.net",
        },
        auth_command="jira",
    )

    assert "rerun a native jira command first" in next_step
    assert "`gotta jira auth` only if refresh does not recover the session" in next_step


def test_atlassian_missing_credentials_message_points_to_gotta_config(
    monkeypatch,
) -> None:
    monkeypatch.delenv("GOTTA_ATLASSIAN_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOTTA_ATLASSIAN_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(atlassian, "load_atlassian_config_env", lambda: {})

    with pytest.raises(
        atlassian.AtlassianError,
        match=r"\[providers\.atlassian\.env\].*gotta\.toml",
    ):
        atlassian.load_client_credentials()


def test_jira_coerce_field_value_serializes_sprint_as_bare_number() -> None:
    meta = {
        "name": "Sprint",
        "schema": {
            "custom": "com.pyxis.greenhopper.jira:gh-sprint",
            "customId": 10020,
            "items": "json",
            "type": "array",
        },
    }

    assert jira.coerce_field_value("customfield_10020", meta, ["11425"]) == 11425


def test_jira_bare_issue_requires_base_url(monkeypatch) -> None:
    monkeypatch.delenv("GOTTA_JIRA_BASE_URL", raising=False)
    monkeypatch.setattr(jira, "load_atlassian_config_env", lambda: {})

    with pytest.raises(jira.ToolError, match="missing Jira base URL"):
        jira.parse_issue_ref("PROJ-123")


def test_confluence_page_ref_accepts_bare_and_prefixed_page_ids(monkeypatch) -> None:
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})

    bare = confluence.parse_page_ref("10101")
    prefixed = confluence.parse_page_ref("confluence:10101")
    query_url = confluence.parse_page_ref(
        "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=20202"
    )
    short_url = confluence.parse_page_ref("https://example.atlassian.net/wiki/x/1J0AAA")
    long_short_url = confluence.parse_page_ref(
        "https://example.atlassian.net/wiki/x/GoD9AgE"
    )

    assert bare.page_id == "10101"
    assert bare.base_url == ""
    assert prefixed.page_id == "10101"
    assert prefixed.base_url == ""
    assert query_url.page_id == "20202"
    assert short_url.page_id == "40404"
    assert long_short_url.page_id == "4345135130"


def test_confluence_page_ref_rejects_blogpost_urls(monkeypatch) -> None:
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})

    with pytest.raises(confluence.ToolError) as excinfo:
        confluence.parse_page_ref(
            "https://example.atlassian.net/wiki/spaces/ENG/blog/2026/03/21/50505/Launch"
        )

    assert "blog post refs are not supported" in str(excinfo.value)


def test_confluence_content_ref_accepts_blogpost_urls(monkeypatch) -> None:
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})

    blog_url = confluence.parse_content_ref(
        "https://example.atlassian.net/wiki/spaces/ENG/blog/2026/03/21/50505/Launch"
    )

    assert blog_url.requested_id == "50505"
    assert blog_url.page_id == "50505"


def test_confluence_page_ref_accepts_trimmed_shortlinks(monkeypatch) -> None:
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})

    short_url = confluence.parse_page_ref("https://example.atlassian.net/wiki/x/AoA12")

    assert short_url.page_id == "3627384834"


@pytest.mark.parametrize("token", ["AAAAA", "abc"])
def test_decode_confluence_tiny_page_id_rejects_implausible_tokens(token: str) -> None:
    assert atlassian.decode_confluence_tiny_page_id(token) is None


def test_confluence_get_trimmed_shortlink_resolves_to_page(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref, allow_reauth=True: confluence.Session(
            token="token",
            cloud_id="cloud",
            base_url="https://example.atlassian.net",
        ),
    )
    seen_urls: list[str] = []

    def fake_api_json(method: str, url: str, token: str, payload=None):
        seen_urls.append(url)
        if "/pages/3627384834?" in url:
            return {
                "id": "3627384834",
                "title": "The Ultimate Guide to the Pipeline (WIP)",
                "status": "current",
                "spaceId": "239828996",
                "version": {"number": 16, "createdAt": "2026-01-22T20:07:38.855Z"},
            }
        raise AssertionError(url)

    monkeypatch.setattr(confluence, "api_json", fake_api_json)
    url = "https://example.atlassian.net/wiki/x/AoA12"

    assert confluence.canonical_locator(["get", url]) == "confluence:3627384834"
    assert confluence.main(["get", url, "--output", "meta"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == "3627384834"
    assert payload["type"] == "page"
    assert payload["title"] == "The Ultimate Guide to the Pipeline (WIP)"
    assert any("/pages/3627384834?" in url for url in seen_urls)


def test_confluence_get_shortlink_falls_back_to_blogpost_lookup(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref, allow_reauth=True: confluence.Session(
            token="token",
            cloud_id="cloud",
            base_url="https://example.atlassian.net",
        ),
    )
    seen_urls: list[str] = []

    def fake_api_json(method: str, url: str, token: str, payload=None):
        seen_urls.append(url)
        if "/pages/40404?" in url:
            raise confluence.ToolError(f"{method} {url} failed with 404: no page")
        if "/blogposts/40404?" in url:
            return {
                "id": "40404",
                "title": "Launch Notes",
                "status": "current",
                "spaceId": "10101",
                "body": {"storage": {"value": "<p>Blog body</p>"}},
                "version": {"number": 2, "createdAt": "2026-03-20T20:36:03Z"},
            }
        raise AssertionError(url)

    monkeypatch.setattr(confluence, "api_json", fake_api_json)

    assert (
        confluence.main(
            ["get", "https://example.atlassian.net/wiki/x/1J0AAA", "--output", "meta"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == "40404"
    assert payload["type"] == "blogpost"
    assert payload["title"] == "Launch Notes"
    assert any("/pages/40404?" in url for url in seen_urls)
    assert any("/blogposts/40404?" in url for url in seen_urls)


def test_confluence_update_body_rejects_blogpost_urls_before_api_calls(
    monkeypatch,
) -> None:
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})

    with pytest.raises(confluence.ToolError) as excinfo:
        confluence.main(
            [
                "update-body",
                "https://example.atlassian.net/wiki/spaces/ENG/blog/2026/03/21/50505/Launch",
                "<p>after</p>",
            ]
        )

    assert "blog post refs are not supported" in str(excinfo.value)


def test_confluence_get_comment_locator_falls_back_from_page_lookup(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref, allow_reauth=True: confluence.Session(
            token="token",
            cloud_id="cloud",
            base_url="https://example.atlassian.net",
        ),
    )
    seen_urls: list[str] = []

    def fake_api_json(method: str, url: str, token: str, payload=None):
        seen_urls.append(url)
        if "/pages/30303?" in url:
            raise confluence.ToolError(f"{method} {url} failed with 404: no page")
        if "/footer-comments/30303?" in url:
            return {
                "id": "30303",
                "pageId": "10101",
                "body": {"storage": {"value": "<p>Generic comment body</p>"}},
                "version": {"number": 3, "createdAt": "2026-03-17T20:36:03Z"},
            }
        raise AssertionError(url)

    monkeypatch.setattr(confluence, "api_json", fake_api_json)

    assert confluence.main(["get", "confluence:30303", "--output", "markdown"]) == 0
    rendered = capsys.readouterr().out
    assert "# Confluence Comment" in rendered
    assert "- Comment ID: 30303" in rendered
    assert "Generic comment body" in rendered
    assert any("/pages/30303?" in url for url in seen_urls)
    assert any("/footer-comments/30303?" in url for url in seen_urls)


def test_confluence_get_focused_comment_url_prefers_comment_identity(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref, allow_reauth=True: confluence.Session(
            token="token",
            cloud_id="cloud",
            base_url="https://example.atlassian.net",
        ),
    )
    seen_urls: list[str] = []

    def fake_api_json(method: str, url: str, token: str, payload=None):
        seen_urls.append(url)
        if "/footer-comments/30303?" in url:
            return {
                "id": "30303",
                "pageId": "10101",
                "status": "current",
                "body": {"storage": {"value": "<p>Focused comment body</p>"}},
                "version": {"number": 2, "createdAt": "2026-03-17T20:36:03Z"},
            }
        raise AssertionError(url)

    monkeypatch.setattr(confluence, "api_json", fake_api_json)
    url = (
        "https://example.atlassian.net/wiki/spaces/ENG/pages/10101/Page"
        "?focusedCommentId=30303"
    )

    assert confluence.canonical_locator(["get", url]) == "confluence:30303"
    assert confluence.main(["get", url, "--output", "meta"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == "30303"
    assert payload["type"] == "comment"
    assert payload["pageId"] == "10101"
    assert any("/footer-comments/30303?" in url for url in seen_urls)
    assert not any("/pages/10101?" in url for url in seen_urls)


def test_confluence_resolve_page_reports_missing_child(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref, allow_reauth=True: confluence.Session(
            token="token",
            cloud_id="cloud",
            base_url="https://example.atlassian.net",
        ),
    )

    def fake_api_json(method: str, url: str, token: str, payload=None):
        if "/pages/10101?" in url:
            return {
                "id": "10101",
                "title": "Parent Page",
                "spaceId": "20202",
                "version": {"number": 7},
                "body": {"storage": {"value": "<p>Parent</p>"}},
            }
        if "/wiki/api/v2/pages?" in url:
            return {"results": [], "_links": {}}
        raise AssertionError(url)

    monkeypatch.setattr(confluence, "api_json", fake_api_json)

    assert (
        confluence.main(
            [
                "resolve-page",
                "--parent",
                "10101",
                "--title",
                "Evidence Dossier",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["found"] is False
    assert payload["matchCount"] == 0
    assert payload["target"]["parentId"] == "10101"
    assert payload["target"]["spaceId"] == "20202"


def test_confluence_resolve_page_reports_exact_child(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref, allow_reauth=True: confluence.Session(
            token="token",
            cloud_id="cloud",
            base_url="https://example.atlassian.net",
        ),
    )

    def fake_api_json(method: str, url: str, token: str, payload=None):
        if "/pages/10101?" in url:
            return {
                "id": "10101",
                "title": "Parent Page",
                "spaceId": "20202",
                "version": {"number": 7},
                "body": {"storage": {"value": "<p>Parent</p>"}},
            }
        if "/wiki/api/v2/pages?" in url:
            return {
                "results": [
                    {
                        "id": "30303",
                        "title": "Evidence Dossier",
                        "status": "current",
                        "spaceId": "20202",
                        "parentId": "10101",
                    }
                ],
                "_links": {},
            }
        raise AssertionError(url)

    monkeypatch.setattr(confluence, "api_json", fake_api_json)

    assert (
        confluence.main(
            [
                "resolve-page",
                "--parent",
                "10101",
                "--title",
                "Evidence Dossier",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["found"] is True
    assert payload["matchCount"] == 1
    assert payload["matches"][0]["id"] == "30303"
    assert payload["matches"][0]["url"] == (
        "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=30303"
    )


def test_confluence_create_page_dry_run_from_markdown_reports_target(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    markdown_path = tmp_path / "dossier.md"
    markdown_path.write_text("# Evidence dossier\n", encoding="utf-8")
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})
    monkeypatch.setattr(
        confluence, "render_markdown_to_storage", lambda markdown: "<h1>Evidence</h1>"
    )
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref, allow_reauth=True: confluence.Session(
            token="token",
            cloud_id="cloud",
            base_url="https://example.atlassian.net",
        ),
    )

    def fake_api_json(method: str, url: str, token: str, payload=None):
        if "/pages/10101?" in url:
            return {
                "id": "10101",
                "title": "Parent Page",
                "spaceId": "20202",
                "version": {"number": 7},
                "body": {"storage": {"value": "<p>Parent</p>"}},
            }
        if "/wiki/api/v2/pages?" in url:
            return {"results": [], "_links": {}}
        raise AssertionError(url)

    monkeypatch.setattr(confluence, "api_json", fake_api_json)

    assert (
        confluence.main(
            [
                "create-page",
                "--parent",
                "10101",
                "--title",
                "Evidence Dossier",
                "--from-markdown",
                str(markdown_path),
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["bodySource"] == "markdown"
    assert payload["siblingExists"] is False
    assert payload["target"]["apiUrl"] == (
        "https://api.atlassian.com/ex/confluence/cloud/wiki/api/v2/pages"
    )
    assert payload["target"]["parentId"] == "10101"
    assert payload["target"]["spaceId"] == "20202"
    assert payload["bodyPreview"]["preview"] == "<h1>Evidence</h1>"


def test_confluence_create_page_from_markdown_strips_matching_leading_h1(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    markdown_path = tmp_path / "dossier.md"
    markdown_path.write_text(
        "# Evidence Dossier\n\nBody paragraph.\n", encoding="utf-8"
    )
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})
    seen_markdown: list[str] = []
    monkeypatch.setattr(
        confluence,
        "render_markdown_to_storage",
        lambda markdown: seen_markdown.append(markdown) or "<p>Body paragraph.</p>",
    )
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref, allow_reauth=True: confluence.Session(
            token="token",
            cloud_id="cloud",
            base_url="https://example.atlassian.net",
        ),
    )

    def fake_api_json(method: str, url: str, token: str, payload=None):
        if "/pages/10101?" in url:
            return {
                "id": "10101",
                "title": "Parent Page",
                "spaceId": "20202",
                "version": {"number": 7},
                "body": {"storage": {"value": "<p>Parent</p>"}},
            }
        if "/wiki/api/v2/pages?" in url:
            return {"results": [], "_links": {}}
        raise AssertionError(url)

    monkeypatch.setattr(confluence, "api_json", fake_api_json)

    assert (
        confluence.main(
            [
                "create-page",
                "--parent",
                "10101",
                "--title",
                "Evidence Dossier",
                "--from-markdown",
                str(markdown_path),
                "--output",
                "json",
            ]
        )
        == 0
    )
    assert seen_markdown == ["Body paragraph.\n"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["bodyPreview"]["preview"] == "<p>Body paragraph.</p>"


def test_confluence_create_page_from_markdown_keeps_nonmatching_leading_h1(
    monkeypatch, tmp_path: Path
) -> None:
    markdown_path = tmp_path / "dossier.md"
    markdown_path.write_text(
        "# Alternate Heading\n\nBody paragraph.\n", encoding="utf-8"
    )
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})
    seen_markdown: list[str] = []
    monkeypatch.setattr(
        confluence,
        "render_markdown_to_storage",
        lambda markdown: seen_markdown.append(markdown) or "<p>Body paragraph.</p>",
    )
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref, allow_reauth=True: confluence.Session(
            token="token",
            cloud_id="cloud",
            base_url="https://example.atlassian.net",
        ),
    )

    def fake_api_json(method: str, url: str, token: str, payload=None):
        if "/pages/10101?" in url:
            return {
                "id": "10101",
                "title": "Parent Page",
                "spaceId": "20202",
                "version": {"number": 7},
                "body": {"storage": {"value": "<p>Parent</p>"}},
            }
        if "/wiki/api/v2/pages?" in url:
            return {"results": [], "_links": {}}
        raise AssertionError(url)

    monkeypatch.setattr(confluence, "api_json", fake_api_json)

    assert (
        confluence.main(
            [
                "create-page",
                "--parent",
                "10101",
                "--title",
                "Evidence Dossier",
                "--from-markdown",
                str(markdown_path),
                "--output",
                "json",
            ]
        )
        == 0
    )
    assert seen_markdown == ["# Alternate Heading\n\nBody paragraph.\n"]


def test_strip_matching_leading_h1_supports_setext_titles() -> None:
    markdown = "Evidence Dossier\n================\n\nBody paragraph.\n"

    stripped = confluence.strip_matching_leading_h1(markdown, title="Evidence Dossier")

    assert stripped == "Body paragraph.\n"


def test_confluence_create_page_apply_posts_storage_body(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    markdown_path = tmp_path / "dossier.md"
    markdown_path.write_text("# Evidence dossier\n", encoding="utf-8")
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})
    monkeypatch.setattr(
        confluence, "render_markdown_to_storage", lambda markdown: "<h1>Evidence</h1>"
    )
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref, allow_reauth=True: confluence.Session(
            token="token",
            cloud_id="cloud",
            base_url="https://example.atlassian.net",
        ),
    )
    seen_payloads: list[dict[str, object]] = []

    def fake_api_json(method: str, url: str, token: str, payload=None):
        if method == "GET" and "/pages/10101?" in url:
            return {
                "id": "10101",
                "title": "Parent Page",
                "spaceId": "20202",
                "version": {"number": 7},
                "body": {"storage": {"value": "<p>Parent</p>"}},
            }
        if method == "GET" and "/wiki/api/v2/pages?" in url:
            return {"results": [], "_links": {}}
        if method == "POST" and url.endswith("/wiki/api/v2/pages"):
            assert isinstance(payload, dict)
            seen_payloads.append(payload)
            return {
                "id": "30303",
                "title": "Evidence Dossier",
                "status": "current",
                "spaceId": "20202",
                "parentId": "10101",
            }
        if method == "GET" and "/pages/30303?" in url:
            return {
                "id": "30303",
                "title": "Evidence Dossier",
                "status": "current",
                "spaceId": "20202",
                "parentId": "10101",
                "version": {"number": 1},
                "body": {"storage": {"value": "<h1>Evidence</h1>"}},
            }
        raise AssertionError(f"{method} {url}")

    monkeypatch.setattr(confluence, "api_json", fake_api_json)

    assert (
        confluence.main(
            [
                "create-page",
                "--parent",
                "10101",
                "--title",
                "Evidence Dossier",
                "--from-markdown",
                str(markdown_path),
                "--apply",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "created"
    assert payload["id"] == "30303"
    assert payload["parentId"] == "10101"
    assert seen_payloads == [
        {
            "spaceId": "20202",
            "status": "current",
            "title": "Evidence Dossier",
            "parentId": "10101",
            "body": {"representation": "storage", "value": "<h1>Evidence</h1>"},
        }
    ]


def test_confluence_create_page_from_html_uses_verbatim_storage(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    html_path = tmp_path / "dossier.body.html"
    html_path.write_text("<p>Rendered storage body</p>", encoding="utf-8")
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref, allow_reauth=True: confluence.Session(
            token="token",
            cloud_id="cloud",
            base_url="https://example.atlassian.net",
        ),
    )

    def fake_api_json(method: str, url: str, token: str, payload=None):
        if "/pages/10101?" in url:
            return {
                "id": "10101",
                "title": "Parent Page",
                "spaceId": "20202",
                "version": {"number": 7},
                "body": {"storage": {"value": "<p>Parent</p>"}},
            }
        if "/wiki/api/v2/pages?" in url:
            return {"results": [], "_links": {}}
        raise AssertionError(url)

    monkeypatch.setattr(confluence, "api_json", fake_api_json)

    assert (
        confluence.main(
            [
                "create-page",
                "--parent",
                "10101",
                "--title",
                "Evidence Dossier",
                "--from-html",
                str(html_path),
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["bodySource"] == "html"
    assert payload["bodyPreview"]["preview"] == "<p>Rendered storage body</p>"


def test_confluence_create_page_apply_rejects_existing_sibling(
    monkeypatch, tmp_path: Path
) -> None:
    html_path = tmp_path / "dossier.body.html"
    html_path.write_text("<p>Rendered storage body</p>", encoding="utf-8")
    monkeypatch.delenv("GOTTA_CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.setattr(confluence, "load_atlassian_config_env", lambda: {})
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref, allow_reauth=True: confluence.Session(
            token="token",
            cloud_id="cloud",
            base_url="https://example.atlassian.net",
        ),
    )

    def fake_api_json(method: str, url: str, token: str, payload=None):
        if "/pages/10101?" in url:
            return {
                "id": "10101",
                "title": "Parent Page",
                "spaceId": "20202",
                "version": {"number": 7},
                "body": {"storage": {"value": "<p>Parent</p>"}},
            }
        if "/wiki/api/v2/pages?" in url:
            return {
                "results": [
                    {
                        "id": "30303",
                        "title": "Evidence Dossier",
                        "status": "current",
                        "spaceId": "20202",
                        "parentId": "10101",
                    }
                ],
                "_links": {},
            }
        if method == "POST":
            raise AssertionError("create should not run when sibling exists")
        raise AssertionError(url)

    monkeypatch.setattr(confluence, "api_json", fake_api_json)

    with pytest.raises(
        confluence.ToolError,
        match=r"a sibling page with the same title already exists.*confluence:30303",
    ):
        confluence.main(
            [
                "create-page",
                "--parent",
                "10101",
                "--title",
                "Evidence Dossier",
                "--from-html",
                str(html_path),
                "--apply",
            ]
        )


def test_jira_and_confluence_get_default_to_markdown() -> None:
    assert jira.build_parser().parse_args(["get", "PROJ-123"]).output == "markdown"
    assert confluence.build_parser().parse_args(["get", "10101"]).output == "markdown"


def test_atlassian_status_defaults_to_summary() -> None:
    assert jira.build_parser().parse_args(["status"]).output == "summary"
    assert (
        jira.build_parser().parse_args(["status", "--output", "summary"]).output
        == "summary"
    )
    assert confluence.build_parser().parse_args(["status"]).output == "summary"
    assert (
        confluence.build_parser().parse_args(["status", "--output", "summary"]).output
        == "summary"
    )


def test_jira_search_rejects_obvious_raw_jql() -> None:
    with pytest.raises(jira.ToolError) as excinfo:
        jira.main(["search", 'project = OPS AND text ~ "ABC"'])

    message = str(excinfo.value)
    assert "`gotta jira search` accepts plain-text only" in message
    assert "gotta jira jql 'project = OPS AND text ~ \"ABC\"'" in message


def test_jira_search_keeps_plain_text_queries_plain(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_search_jira(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "query": kwargs["jql"],
            "limit": kwargs["limit"],
            "requestedNext": kwargs["cursor"],
            "next": "",
            "size": 0,
            "results": [],
        }

    monkeypatch.setattr(jira, "search_jira", fake_search_jira)

    result = jira.main(
        ["search", "service continuity and ownership", "--output", "json"]
    )

    assert result == 0
    assert captured["jql"] == jira.build_plaintext_jql(
        "service continuity and ownership"
    )
    payload = capsys.readouterr().out
    assert '"size": 0' in payload


def test_jira_and_confluence_search_use_next_for_continuation_tokens() -> None:
    jira_args = jira.build_parser().parse_args(
        ["search", "service outage", "--next", "abc123"]
    )
    confluence_args = confluence.build_parser().parse_args(
        ["cql", 'text ~ "service"', "--next", "def456"]
    )

    assert jira_args.next == "abc123"
    assert confluence_args.next == "def456"


def test_jira_search_resolves_exact_issue_keys_directly(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        jira,
        "fetch_issue",
        lambda issue_ref, *, fields: (
            captured.update(
                {
                    "issue_key": issue_ref.issue_key,
                    "base_url": issue_ref.base_url,
                    "fields": fields,
                }
            )
            or {
                "key": issue_ref.issue_key,
                "summary": "Direct issue match",
                "status": {"name": "Done"},
                "issueType": {"name": "Task"},
                "project": {"key": "OPS"},
                "priority": {"name": "Medium"},
                "assignee": {"displayName": "Alex"},
                "labels": ["continuity"],
                "created": "2026-03-01T10:00:00Z",
                "updated": "2026-03-02T12:00:00Z",
                "issueUrl": f"{issue_ref.base_url}/browse/{issue_ref.issue_key}",
            }
        ),
    )

    result = jira.main(
        [
            "search",
            "proj-123",
            "--base-url",
            "https://example.atlassian.net",
            "--output",
            "json",
        ]
    )

    assert result == 0
    assert captured["issue_key"] == "PROJ-123"
    payload = json.loads(capsys.readouterr().out)
    assert payload["size"] == 1
    assert payload["results"][0]["key"] == "PROJ-123"
    assert payload["visibility_level"] == "restricted"
    assert payload["results"][0]["visibility_level"] == "restricted"

    assert (
        jira.main(["search", "proj-123", "--base-url", "https://example.atlassian.net"])
        == 0
    )
    rendered = capsys.readouterr().out
    assert "- Visibility: restricted (same_company, medium)" in rendered


def test_jira_get_and_meta_include_visibility(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        jira,
        "load_session",
        lambda base_url, allow_reauth=True: jira.Session(
            token="token",
            cloud_id="cloud",
            base_url="https://example.atlassian.net",
        ),
    )
    monkeypatch.setattr(
        jira,
        "api_json",
        lambda method, url, token, payload=None: {
            "id": "10001",
            "key": "OPS-1",
            "fields": {
                "summary": "Permissions regression",
                "status": {"name": "In Progress"},
                "issuetype": {"name": "Bug"},
                "security": {"name": "Engineering"},
                "project": {"key": "OPS", "name": "Operations"},
                "priority": {"name": "High"},
                "assignee": {"displayName": "Alex"},
                "reporter": {"displayName": "Morgan"},
                "parent": {
                    "id": "10000",
                    "key": "OPS-0",
                    "fields": {
                        "summary": "Parent issue",
                        "issuetype": {"name": "Epic"},
                        "status": {"name": "In Progress"},
                    },
                },
                "labels": ["visibility"],
                "created": "2026-03-01T10:00:00Z",
                "updated": "2026-03-02T12:00:00Z",
                "description": None,
            },
        },
    )

    assert (
        jira.main(
            [
                "get",
                "OPS-1",
                "--base-url",
                "https://example.atlassian.net",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["visibility_level"] == "restricted"
    assert payload["visibility_confidence"] == "high"
    assert payload["parent"] == {
        "id": "10000",
        "key": "OPS-0",
        "summary": "Parent issue",
        "issueType": {"name": "Epic"},
        "status": {"name": "In Progress"},
    }

    assert (
        jira.main(
            [
                "get",
                "OPS-1",
                "--base-url",
                "https://example.atlassian.net",
                "--output",
                "meta",
            ]
        )
        == 0
    )
    meta = json.loads(capsys.readouterr().out)
    assert meta["visibility_level"] == "restricted"
    assert meta["visibility_confidence"] == "high"
    assert meta["parent"] == {
        "id": "10000",
        "key": "OPS-0",
        "summary": "Parent issue",
        "issueType": {"name": "Epic"},
        "status": {"name": "In Progress"},
    }

    assert (
        jira.main(["get", "OPS-1", "--base-url", "https://example.atlassian.net"]) == 0
    )
    rendered = capsys.readouterr().out
    assert "- Visibility: restricted (same_company, high)" in rendered
    assert "- Parent: OPS-0 Parent issue" in rendered


def test_search_jira_attaches_visibility_to_payload_and_results(monkeypatch) -> None:
    monkeypatch.setattr(
        jira,
        "load_session",
        lambda base_url, allow_reauth=True: jira.Session(
            token="token",
            cloud_id="cloud",
            base_url="https://example.atlassian.net",
        ),
    )
    monkeypatch.setattr(
        jira,
        "api_json",
        lambda method, url, token, payload=None: {
            "issues": [
                {
                    "id": "10001",
                    "key": "OPS-1",
                    "fields": {
                        "summary": "Permissions regression",
                        "status": {"name": "In Progress"},
                        "issuetype": {"name": "Bug"},
                        "project": {"key": "OPS", "name": "Operations"},
                        "priority": {"name": "High"},
                        "assignee": {"displayName": "Alex"},
                        "labels": ["visibility"],
                        "updated": "2026-03-02T12:00:00Z",
                    },
                }
            ],
            "nextPageToken": "",
        },
    )

    payload = jira.search_jira(
        base_url="https://example.atlassian.net",
        jql='project = OPS AND text ~ "visibility"',
        limit=5,
        cursor=None,
        fields=jira.DEFAULT_SEARCH_FIELDS,
        subcommand="jql",
    )

    assert payload["visibility_level"] == "restricted"
    assert payload["visibility_boundary"] == "same_company"
    assert payload["results"][0]["visibility_level"] == "restricted"
    rendered = jira.render_search_markdown(payload)
    assert "- Visibility: restricted (same_company, medium)" in rendered


def test_confluence_markdown_projection_strips_wide_layout_artifacts() -> None:
    markdown = (
        "wide760kubectl -n tunnel-proxy port-forward svc/example 10000:10000\n"
        "wide760127.0.0.1 tunnel-proxy1-internal\n"
        "normal line\n"
    )

    assert confluence._clean_markdown_projection(markdown) == (
        "kubectl -n tunnel-proxy port-forward svc/example 10000:10000\n"
        "127.0.0.1 tunnel-proxy1-internal\n"
        "normal line\n"
    )


def test_confluence_storage_sanitization_strips_confluence_markup_noise() -> None:
    storage_html = (
        '<table data-table-width="760" ac:local-id="abc">'
        '<tr><td data-layout="default">1333 448fe996-a791-4657-88e4-5815764847c3 '
        'incomplete <span class="placeholder-inline-tasks"> </span></td></tr>'
        '<tr><td><a href="https://example.com" data-card-appearance="inline">x</a></td></tr>'
        "</table>"
    )

    cleaned = confluence._sanitize_storage_html_for_markdown(storage_html)

    assert "ac:local-id=" not in cleaned
    assert "data-table-width=" not in cleaned
    assert "data-layout=" not in cleaned
    assert "data-card-appearance=" not in cleaned
    assert "placeholder-inline-tasks" not in cleaned
    assert "incomplete" not in cleaned
    assert '<a href="https://example.com">x</a>' in cleaned


def test_confluence_render_page_markdown_includes_created_and_updated() -> None:
    rendered = confluence.render_page_markdown(
        {
            "id": "10101",
            "title": "Architecture Overview",
            "createdAt": "2025-09-29T18:58:25.400Z",
            "spaceId": "50505",
            "version": {
                "number": 126,
                "createdAt": "2026-03-12T23:23:06.334Z",
            },
            "body": {"storage": {"value": "<p>Hello</p>"}},
        },
        confluence.Session(
            token="token",
            cloud_id="cloud-123",
            base_url="https://example.atlassian.net",
        ),
    )

    assert "- Created: 2025-09-29T18:58:25.400Z" in rendered
    assert "- Updated: 2026-03-12T23:23:06.334Z" in rendered
    assert "- Version: 126" in rendered


def test_confluence_render_page_markdown_marks_lossy_projection() -> None:
    original = confluence.render_storage_to_markdown
    confluence.render_storage_to_markdown = lambda storage_html, **_kwargs: (
        "<table><tr><td>x</td></tr></table>\nUntitled Diagram-111111.drawio\n"
    )
    try:
        rendered = confluence.render_page_markdown(
            {
                "id": "111111",
                "title": "Example Architecture Page",
                "createdAt": "2026-02-19T16:29:02.479Z",
                "version": {"number": 43, "createdAt": "2026-03-11T16:48:50.034Z"},
                "body": {"storage": {"value": "<table><tr><td>x</td></tr></table>"}},
            },
            confluence.Session(
                token="token",
                cloud_id="cloud-123",
                base_url="https://example.atlassian.net",
            ),
        )
    finally:
        confluence.render_storage_to_markdown = original

    assert "Projection: approximate markdown" in rendered
    assert "gotta confluence get 111111 --output body" in rendered
    assert "embedded diagrams, tables, or macros matter" in rendered


def test_confluence_render_page_markdown_marks_drawio_projection_as_lossy() -> None:
    original = confluence.render_storage_to_markdown
    confluence.render_storage_to_markdown = lambda storage_html, **_kwargs: (
        "Embedded draw.io diagram: `example-graph.drawio`\n\n- Pages: `1`\n"
    )
    try:
        rendered = confluence.render_page_markdown(
            {
                "id": "222222",
                "title": "Example Diagram Page",
                "createdAt": "2026-03-23T17:13:55.378Z",
                "version": {"number": 11, "createdAt": "2026-03-24T19:47:30.675Z"},
                "body": {
                    "storage": {"value": '<ac:structured-macro ac:name="drawio" />'}
                },
            },
            confluence.Session(
                token="token",
                cloud_id="cloud-123",
                base_url="https://example.atlassian.net",
            ),
        )
    finally:
        confluence.render_storage_to_markdown = original

    assert "Projection: approximate markdown" in rendered
    assert "gotta confluence get 222222 --output body" in rendered


def test_confluence_render_comment_markdown_marks_drawio_projection_as_lossy() -> None:
    original = confluence.render_storage_to_markdown
    confluence.render_storage_to_markdown = lambda storage_html, **_kwargs: (
        "Embedded draw.io diagram: `example-graph.drawio`\n\n- Pages: `1`\n"
    )
    try:
        rendered = confluence.render_comment_markdown(
            {
                "id": "333333",
                "pageId": "222222",
                "title": "Example Comment",
                "createdAt": "2026-03-23T17:13:55.378Z",
                "version": {"number": 2, "createdAt": "2026-03-24T19:47:30.675Z"},
                "body": {
                    "storage": {"value": '<ac:structured-macro ac:name="drawio" />'}
                },
            },
            confluence.Session(
                token="token",
                cloud_id="cloud-123",
                base_url="https://example.atlassian.net",
            ),
        )
    finally:
        confluence.render_storage_to_markdown = original

    assert "Projection: approximate markdown" in rendered
    assert "gotta confluence get 333333 --output body" in rendered


def test_confluence_markdown_from_capture_reloads_session_for_drawio_projection(
    monkeypatch,
) -> None:
    seen_page_refs: list[confluence.PageRef] = []
    original = confluence.render_storage_to_markdown

    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref, allow_reauth=True: (
            seen_page_refs.append(page_ref)
            or confluence.Session(
                token="token",
                cloud_id="cloud-123",
                base_url="https://example.atlassian.net",
            )
        ),
    )
    confluence.render_storage_to_markdown = lambda storage_html, **kwargs: (
        "Embedded draw.io diagram"
        if kwargs.get("session") is not None
        else "no session"
    )
    try:
        rendered = confluence._markdown_from_capture(
            Capture(
                data=b'<ac:structured-macro ac:name="drawio" />',
                metadata={
                    "content_kind": "page",
                    "content_id": "222222",
                    "source_title": "Example Diagram Page",
                    "source_url": "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=222222",
                    "source_base_url": "https://example.atlassian.net",
                },
            )
        ).decode("utf-8")
    finally:
        confluence.render_storage_to_markdown = original

    assert seen_page_refs
    assert seen_page_refs[0].base_url == "https://example.atlassian.net"
    assert "Embedded draw.io diagram" in rendered


def test_confluence_replace_drawio_macros_emits_structured_fallback() -> None:
    rendered = confluence._replace_drawio_macros(
        (
            "<h2>Data Flow</h2>"
            '<ac:structured-macro ac:name="drawio" ac:schema-version="1">'
            '<ac:parameter ac:name="custContentId">444444</ac:parameter>'
            '<ac:parameter ac:name="pageId">222222</ac:parameter>'
            '<ac:parameter ac:name="diagramDisplayName">example-graph.drawio</ac:parameter>'
            '<ac:parameter ac:name="diagramName">example-graph.drawio</ac:parameter>'
            '<ac:parameter ac:name="width">400</ac:parameter>'
            '<ac:parameter ac:name="height">200</ac:parameter>'
            "</ac:structured-macro>"
        ),
        session=None,
    )

    assert "Embedded draw.io diagram" in rendered
    assert "example-graph.drawio" in rendered
    assert "Custom content ID" in rendered
    assert "Structure" in rendered


def test_confluence_replace_drawio_macros_summarizes_attachment_when_resolvable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        confluence,
        "_resolve_drawio_attachment",
        lambda *_args, **_kwargs: {
            "id": "att-example",
            "title": "example-graph.drawio",
            "mediaType": "application/vnd.jgraph.mxfile",
            "downloadLink": "/download/attachments/222222/example-graph.drawio",
        },
    )
    monkeypatch.setattr(
        confluence,
        "api_bytes",
        lambda *_args, **_kwargs: (
            b'<mxfile><diagram id="example-page" name="Example Graph Page">'
            b'<mxGraphModel><root><mxCell id="0" /><mxCell id="1" parent="0" />'
            b'<mxCell id="2" value="Example Source" vertex="1" parent="1" />'
            b'<mxCell id="3" value="Example Target" vertex="1" parent="1" />'
            b'<mxCell id="4" edge="1" source="2" target="3" parent="1" />'
            b"</root></mxGraphModel></diagram></mxfile>"
        ),
    )

    rendered = confluence._replace_drawio_macros(
        (
            '<ac:structured-macro ac:name="drawio" ac:schema-version="1">'
            '<ac:parameter ac:name="custContentId">444444</ac:parameter>'
            '<ac:parameter ac:name="pageId">222222</ac:parameter>'
            '<ac:parameter ac:name="diagramDisplayName">example-graph.drawio</ac:parameter>'
            "</ac:structured-macro>"
        ),
        session=confluence.Session(
            token="token",
            cloud_id="cloud-123",
            base_url="https://example.atlassian.net",
        ),
    )

    assert "Attachment ID" in rendered
    assert "Pages:" in rendered
    assert "Example Graph Page" in rendered
    assert "2 nodes, 1 edges" in rendered
    assert "Example Source, Example Target" in rendered


def test_confluence_attachment_download_url_prefers_api_redirect_endpoint() -> None:
    session = confluence.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )

    assert confluence._attachment_download_url(
        session,
        {
            "id": "att-example",
            "pageId": "222222",
            "downloadLink": "/download/attachments/222222/example-graph.drawio",
        },
    ) == (
        "https://api.atlassian.com/ex/confluence/cloud-123/wiki/rest/api/"
        "content/222222/child/attachment/att-example/download"
    )


def test_confluence_attachment_download_url_falls_back_to_confluence_download_link() -> (
    None
):
    session = confluence.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )

    assert (
        confluence._attachment_download_url(
            session,
            {
                "downloadLink": "/download/attachments/222222/example-graph.drawio",
            },
        )
        == "https://example.atlassian.net/wiki/download/attachments/222222/example-graph.drawio"
    )


def test_jira_auth_prefers_reuse_or_refresh(monkeypatch, capsys) -> None:
    persisted: list[str] = []
    monkeypatch.setattr(
        jira,
        "load_session",
        lambda base_url: jira.Session(
            token="token",
            cloud_id="cloud-123",
            base_url="https://example.atlassian.net",
        ),
    )
    monkeypatch.setattr(
        jira,
        "atlassian_status_payload",
        lambda **kwargs: {
            "baseUrl": "https://example.atlassian.net",
            "expiresAt": 123.0,
        },
    )
    monkeypatch.setattr(
        jira,
        "run_oauth_bootstrap",
        lambda **kwargs: pytest.fail("full bootstrap should not run by default"),
    )
    monkeypatch.setattr(
        jira, "persist_selected_base_urls", lambda base_url: persisted.append(base_url)
    )

    result = jira.main(["auth"])

    captured = capsys.readouterr()
    assert result == 0
    assert persisted == ["https://example.atlassian.net"]
    assert '"authenticated": true' in captured.out
    assert '"base_url": "https://example.atlassian.net"' in captured.out
    assert '"cloud_id": "cloud-123"' in captured.out
    assert '"expires_at": 123.0' in captured.out
    assert '"token_file":' in captured.out


def test_jira_auth_full_forces_browser_bootstrap(monkeypatch, capsys) -> None:
    persisted: list[str] = []
    monkeypatch.setattr(
        jira,
        "load_session",
        lambda base_url: pytest.fail(
            "default refresh path should be bypassed with --full"
        ),
    )
    monkeypatch.setattr(
        jira,
        "run_oauth_bootstrap",
        lambda **kwargs: {
            "base_url": "https://example.atlassian.net",
            "cloud_id": "cloud-456",
            "expires_at": 456.0,
        },
    )
    monkeypatch.setattr(
        jira, "persist_selected_base_urls", lambda base_url: persisted.append(base_url)
    )

    result = jira.main(["auth", "--full"])

    captured = capsys.readouterr()
    assert result == 0
    assert persisted == ["https://example.atlassian.net"]
    assert '"authenticated": true' in captured.out
    assert '"base_url": "https://example.atlassian.net"' in captured.out
    assert '"cloud_id": "cloud-456"' in captured.out
    assert '"expires_at": 456.0' in captured.out
    assert '"token_file":' in captured.out


def test_confluence_auth_prefers_reuse_or_refresh(monkeypatch, capsys) -> None:
    persisted: list[str] = []
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref: confluence.Session(
            token="token",
            cloud_id="cloud-123",
            base_url="https://example.atlassian.net",
        ),
    )
    monkeypatch.setattr(
        confluence,
        "atlassian_status_payload",
        lambda **kwargs: {
            "baseUrl": "https://example.atlassian.net",
            "expiresAt": 123.0,
        },
    )
    monkeypatch.setattr(
        confluence,
        "run_oauth_bootstrap",
        lambda **kwargs: pytest.fail("full bootstrap should not run by default"),
    )
    monkeypatch.setattr(
        confluence,
        "persist_selected_base_urls",
        lambda base_url: persisted.append(base_url),
    )

    result = confluence.main(["auth"])

    captured = capsys.readouterr()
    assert result == 0
    assert persisted == ["https://example.atlassian.net/wiki"]
    assert '"authenticated": true' in captured.out
    assert '"base_url": "https://example.atlassian.net/wiki"' in captured.out
    assert '"cloud_id": "cloud-123"' in captured.out
    assert '"expires_at": 123.0' in captured.out
    assert '"token_file":' in captured.out


def test_confluence_auth_full_forces_browser_bootstrap(monkeypatch, capsys) -> None:
    persisted: list[str] = []
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref: pytest.fail(
            "default refresh path should be bypassed with --full"
        ),
    )
    monkeypatch.setattr(
        confluence,
        "run_oauth_bootstrap",
        lambda **kwargs: {
            "base_url": "https://example.atlassian.net",
            "cloud_id": "cloud-456",
            "expires_at": 456.0,
        },
    )
    monkeypatch.setattr(
        confluence,
        "persist_selected_base_urls",
        lambda base_url: persisted.append(base_url),
    )

    result = confluence.main(["auth", "--full"])

    captured = capsys.readouterr()
    assert result == 0
    assert persisted == ["https://example.atlassian.net/wiki"]
    assert '"authenticated": true' in captured.out
    assert '"base_url": "https://example.atlassian.net/wiki"' in captured.out
    assert '"cloud_id": "cloud-456"' in captured.out
    assert '"expires_at": 456.0' in captured.out
    assert '"token_file":' in captured.out


def test_confluence_search_defaults_to_page_first_markdown() -> None:
    args = confluence.build_parser().parse_args(["search", "Architecture"])
    assert args.type == "page"
    assert args.output == "markdown"


def test_confluence_cql_shares_output_contract_with_search() -> None:
    args = confluence.build_parser().parse_args(
        ["cql", 'text ~ "Architecture"', "--output", "json"]
    )
    assert args.output == "json"


def test_jira_jql_shares_output_contract_with_search() -> None:
    args = jira.build_parser().parse_args(
        ["jql", 'project = OPS AND text ~ "Architecture"', "--output", "json"]
    )
    assert args.output == "json"


def test_atlassian_api_json_accepts_success_without_body(monkeypatch) -> None:
    class FakeResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return b""

    monkeypatch.setattr(
        atlassian.urllib.request, "urlopen", lambda request: FakeResponse()
    )

    assert (
        atlassian.api_json(
            "PUT", "https://example.invalid/api", "token", payload={"ok": True}
        )
        == {}
    )


def test_jira_write_surfaces_default_to_summary_preview() -> None:
    create_args = jira.build_parser().parse_args(
        ["create", "--project", "OPS", "--type", "Task", "--title", "Example"]
    )
    update_args = jira.build_parser().parse_args(["update", "OPS-1"])
    comment_args = jira.build_parser().parse_args(["comment", "OPS-1"])
    link_args = jira.build_parser().parse_args(["link", "OPS-1", "OPS-2"])
    transition_args = jira.build_parser().parse_args(
        ["transition", "OPS-1", "--to", "Done"]
    )

    assert create_args.output == "summary"
    assert create_args.apply is False
    assert update_args.output == "summary"
    assert comment_args.output == "summary"
    assert link_args.output == "summary"
    assert transition_args.output == "summary"


def test_jira_fields_create_discovery_renders_json(monkeypatch, capsys) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    monkeypatch.setattr(
        jira,
        "resolve_project",
        lambda base_url, project: (
            session,
            {"id": "10000", "key": "OPS", "name": "Operations"},
        ),
    )
    monkeypatch.setattr(
        jira,
        "resolve_issue_type",
        lambda session, project_key, raw: {"id": "20000", "name": "Task"},
    )
    monkeypatch.setattr(
        jira,
        "fetch_create_fields",
        lambda session, project_key, issue_type_id: {
            "summary": jira.normalize_field_metadata(
                "summary",
                {"name": "Summary", "required": True, "schema": {"type": "string"}},
            )
        },
    )

    assert (
        jira.main(
            [
                "fields",
                "--base-url",
                "https://example.atlassian.net",
                "--project",
                "OPS",
                "--type",
                "Task",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["context"] == "create OPS/Task"
    assert payload["fields"]["summary"]["required"] is True


def test_jira_create_preview_reports_missing_required_fields(
    monkeypatch, capsys
) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    monkeypatch.setattr(
        jira,
        "resolve_project",
        lambda base_url, project: (
            session,
            {"id": "10000", "key": "OPS", "name": "Operations"},
        ),
    )
    monkeypatch.setattr(
        jira,
        "resolve_issue_type",
        lambda session, project_key, raw: {"id": "20000", "name": "Task"},
    )
    monkeypatch.setattr(
        jira,
        "fetch_create_fields",
        lambda session, project_key, issue_type_id: {
            "summary": jira.normalize_field_metadata(
                "summary",
                {"name": "Summary", "required": True, "schema": {"type": "string"}},
            ),
            "customfield_10000": jira.normalize_field_metadata(
                "customfield_10000",
                {
                    "name": "Acceptance Criteria",
                    "required": True,
                    "schema": {"type": "string"},
                },
            ),
        },
    )

    assert (
        jira.main(
            [
                "create",
                "--base-url",
                "https://example.atlassian.net",
                "--project",
                "ops",
                "--type",
                "Task",
                "--title",
                "Service handoff",
                "--body",
                "## Scope\n\n- Phase 1",
                "--priority",
                "High",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["target"]["project"] == "OPS"
    assert payload["target"]["issueType"] == "Task"
    assert "Acceptance Criteria (customfield_10000)" in payload["missingRequiredFields"]
    assert payload["fieldValues"]["project"] == {"key": "OPS"}
    assert payload["fieldValues"]["priority"] == {"name": "High"}
    assert payload["bodyMarkdown"] == "## Scope\n\n- Phase 1"


def test_jira_create_apply_creates_issue_and_links(monkeypatch, capsys) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    created_payloads: list[dict[str, object]] = []
    issue_links: list[tuple[str, str, str]] = []
    remote_links: list[dict[str, object]] = []

    monkeypatch.setattr(
        jira,
        "resolve_project",
        lambda base_url, project: (
            session,
            {"id": "10000", "key": "OPS", "name": "Operations"},
        ),
    )
    monkeypatch.setattr(
        jira,
        "resolve_issue_type",
        lambda session, project_key, raw: {"id": "20000", "name": "Task"},
    )
    monkeypatch.setattr(
        jira,
        "fetch_create_fields",
        lambda session, project_key, issue_type_id: {
            "summary": jira.normalize_field_metadata(
                "summary",
                {"name": "Summary", "required": True, "schema": {"type": "string"}},
            ),
        },
    )
    monkeypatch.setattr(
        jira,
        "create_issue",
        lambda session, *, payload_fields: (
            created_payloads.append(payload_fields)
            or {
                "siteUrl": session.base_url,
                "issueUrl": f"{session.base_url}/browse/OPS-101",
                "id": "10101",
                "key": "OPS-101",
                "summary": payload_fields["summary"],
                "status": {"name": "To Do"},
                "issueType": {"name": "Task"},
                "project": {"key": "OPS", "name": "Operations"},
                "priority": payload_fields.get("priority"),
                "assignee": None,
                "reporter": None,
                "labels": [],
                "created": "2026-03-18T00:00:00Z",
                "updated": "2026-03-18T00:00:00Z",
            }
        ),
    )
    monkeypatch.setattr(
        jira,
        "resolve_link_type",
        lambda base_url, raw: (session, {"id": "10", "name": "Relates"}),
    )
    monkeypatch.setattr(
        jira,
        "create_issue_link",
        lambda session, *, source_issue, target_issue, link_type: issue_links.append(
            (source_issue, target_issue, str(link_type.get("name") or ""))
        ),
    )
    monkeypatch.setattr(
        jira,
        "create_remote_link",
        lambda session, issue_key, *, payload: (
            remote_links.append({"issue": issue_key, "payload": payload}) or {}
        ),
    )

    assert (
        jira.main(
            [
                "create",
                "--base-url",
                "https://example.atlassian.net",
                "--project",
                "OPS",
                "--type",
                "Task",
                "--title",
                "Service handoff",
                "--body",
                "Initial draft",
                "--relates",
                "OPS-2",
                "--remote-link",
                "https://example.invalid/request/42",
                "--apply",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert created_payloads[0]["project"] == {"key": "OPS"}
    assert issue_links == [("OPS-101", "OPS-2", "Relates")]
    assert remote_links[0]["issue"] == "OPS-101"
    assert (
        remote_links[0]["payload"]["object"]["url"]
        == "https://example.invalid/request/42"
    )
    assert payload["issue"]["key"] == "OPS-101"


def test_jira_create_preview_resolves_current_sprint(monkeypatch, capsys) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    monkeypatch.setattr(
        jira,
        "resolve_project",
        lambda base_url, project: (
            session,
            {"id": "10000", "key": "OPS", "name": "Operations"},
        ),
    )
    monkeypatch.setattr(
        jira,
        "resolve_issue_type",
        lambda session, project_key, raw: {"id": "20000", "name": "Task"},
    )
    monkeypatch.setattr(
        jira,
        "fetch_create_fields",
        lambda session, project_key, issue_type_id: {
            "summary": jira.normalize_field_metadata(
                "summary",
                {"name": "Summary", "required": True, "schema": {"type": "string"}},
            ),
        },
    )
    monkeypatch.setattr(
        jira,
        "resolve_requested_sprint",
        lambda base_url, *, current_sprint, sprint_id, project_key_or_id="", board_id=None: (
            session,
            {"id": "21", "name": "Delivery Board", "type": "scrum"},
            {"id": "31", "name": "Sprint 31", "state": "active", "originBoardId": "21"},
        ),
    )

    assert (
        jira.main(
            [
                "create",
                "--base-url",
                "https://example.atlassian.net",
                "--project",
                "OPS",
                "--type",
                "Task",
                "--title",
                "Service handoff",
                "--current-sprint",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"]["project"] == "OPS"
    assert payload["target"]["board"] == "21"
    assert payload["target"]["boardName"] == "Delivery Board"
    assert payload["target"]["sprint"] == "31"
    assert payload["target"]["sprintName"] == "Sprint 31"
    assert payload["target"]["sprintState"] == "active"


def test_jira_create_apply_assigns_created_issue_to_sprint(monkeypatch, capsys) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    seen_assignments: list[tuple[str, int]] = []
    monkeypatch.setattr(
        jira,
        "resolve_project",
        lambda base_url, project: (
            session,
            {"id": "10000", "key": "OPS", "name": "Operations"},
        ),
    )
    monkeypatch.setattr(
        jira,
        "resolve_issue_type",
        lambda session, project_key, raw: {"id": "20000", "name": "Task"},
    )
    monkeypatch.setattr(
        jira,
        "fetch_create_fields",
        lambda session, project_key, issue_type_id: {
            "summary": jira.normalize_field_metadata(
                "summary",
                {"name": "Summary", "required": True, "schema": {"type": "string"}},
            ),
        },
    )
    monkeypatch.setattr(
        jira,
        "resolve_requested_sprint",
        lambda base_url, *, current_sprint, sprint_id, project_key_or_id="", board_id=None: (
            session,
            {"id": "21", "name": "Delivery Board", "type": "scrum"},
            {"id": "31", "name": "Sprint 31", "state": "active", "originBoardId": "21"},
        ),
    )
    monkeypatch.setattr(
        jira,
        "create_issue",
        lambda session, *, payload_fields: {
            "siteUrl": session.base_url,
            "issueUrl": f"{session.base_url}/browse/OPS-101",
            "id": "10101",
            "key": "OPS-101",
            "summary": payload_fields["summary"],
            "status": {"name": "To Do"},
            "issueType": {"name": "Task"},
            "project": {"key": "OPS", "name": "Operations"},
            "priority": payload_fields.get("priority"),
            "assignee": None,
            "reporter": None,
            "labels": [],
            "created": "2026-03-18T00:00:00Z",
            "updated": "2026-03-18T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        jira,
        "assign_issue_to_sprint",
        lambda issue_ref, *, sprint_id: (
            seen_assignments.append((issue_ref.issue_key, sprint_id))
            or {
                "siteUrl": session.base_url,
                "issueUrl": f"{session.base_url}/browse/{issue_ref.issue_key}",
                "id": "10101",
                "key": issue_ref.issue_key,
                "summary": "Service handoff",
                "status": {"name": "To Do"},
                "issueType": {"name": "Task"},
                "project": {"key": "OPS", "name": "Operations"},
                "priority": None,
                "assignee": None,
                "reporter": None,
                "labels": [],
                "created": "2026-03-18T00:00:00Z",
                "updated": "2026-03-18T00:00:00Z",
            }
        ),
    )

    assert (
        jira.main(
            [
                "create",
                "--base-url",
                "https://example.atlassian.net",
                "--project",
                "OPS",
                "--type",
                "Task",
                "--title",
                "Service handoff",
                "--current-sprint",
                "--apply",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert seen_assignments == [("OPS-101", 31)]
    assert payload["issue"]["key"] == "OPS-101"
    assert payload["board"]["id"] == "21"
    assert payload["sprint"]["id"] == "31"


def test_jira_update_preview_upserts_markdown_section(monkeypatch, capsys) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    monkeypatch.setattr(
        jira,
        "load_session",
        lambda base_url, allow_reauth=True: session,
    )
    monkeypatch.setattr(
        jira,
        "fetch_edit_fields",
        lambda issue_ref: (
            session,
            {
                "summary": jira.normalize_field_metadata(
                    "summary",
                    {
                        "name": "Summary",
                        "required": False,
                        "schema": {"type": "string"},
                    },
                )
            },
        ),
    )
    monkeypatch.setattr(
        jira,
        "fetch_issue",
        lambda issue_ref, *, fields: {
            "siteUrl": session.base_url,
            "issueUrl": f"{session.base_url}/browse/{issue_ref.issue_key}",
            "id": "9090",
            "key": issue_ref.issue_key,
            "summary": "Existing issue",
            "status": {"name": "To Do"},
            "issueType": {"name": "Task"},
            "project": {"key": "OPS", "name": "Operations"},
            "priority": None,
            "assignee": None,
            "reporter": None,
            "labels": [],
            "created": "2026-03-18T00:00:00Z",
            "updated": "2026-03-18T00:00:00Z",
            "descriptionAdf": jira.markdown_to_adf("## Existing\n\nKeep this section."),
        },
    )

    assert (
        jira.main(
            [
                "update",
                "OPS-9",
                "--base-url",
                "https://example.atlassian.net",
                "--upsert-section",
                "References",
                "--body",
                "- [Spec](https://example.invalid/spec)",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert "## Existing" in payload["bodyMarkdown"]
    assert "## References" in payload["bodyMarkdown"]
    assert "https://example.invalid/spec" in payload["bodyMarkdown"]
    assert payload["fieldValues"]["description"]["type"] == "doc"


def test_jira_update_preview_supports_sprint_only_change(monkeypatch, capsys) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    monkeypatch.setattr(
        jira,
        "load_session",
        lambda base_url, allow_reauth=True: session,
    )
    monkeypatch.setattr(jira, "fetch_edit_fields", lambda issue_ref: (session, {}))
    monkeypatch.setattr(
        jira,
        "fetch_issue",
        lambda issue_ref, *, fields: {
            "siteUrl": session.base_url,
            "issueUrl": f"{session.base_url}/browse/{issue_ref.issue_key}",
            "id": "9090",
            "key": issue_ref.issue_key,
            "summary": "Existing issue",
            "status": {"name": "To Do"},
            "issueType": {"name": "Task"},
            "project": {"key": "OPS", "name": "Operations"},
            "priority": None,
            "assignee": None,
            "reporter": None,
            "labels": [],
            "created": "2026-03-18T00:00:00Z",
            "updated": "2026-03-18T00:00:00Z",
            "descriptionAdf": None,
        },
    )
    monkeypatch.setattr(
        jira,
        "resolve_requested_sprint",
        lambda base_url, *, current_sprint, sprint_id, project_key_or_id="", board_id=None: (
            session,
            {"id": "21", "name": "Delivery Board", "type": "scrum"},
            {"id": "31", "name": "Sprint 31", "state": "active", "originBoardId": "21"},
        ),
    )

    assert (
        jira.main(
            [
                "update",
                "OPS-9",
                "--base-url",
                "https://example.atlassian.net",
                "--current-sprint",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "update"
    assert payload["target"]["issue"] == "OPS-9"
    assert payload["target"]["project"] == "OPS"
    assert payload["target"]["board"] == "21"
    assert payload["target"]["sprint"] == "31"


def test_jira_update_apply_sprint_only_assigns_without_field_update(
    monkeypatch, capsys
) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    fetch_calls: list[str] = []
    seen_assignments: list[tuple[str, int]] = []
    monkeypatch.setattr(
        jira,
        "load_session",
        lambda base_url, allow_reauth=True: session,
    )
    monkeypatch.setattr(jira, "fetch_edit_fields", lambda issue_ref: (session, {}))

    def fake_fetch_issue(issue_ref, *, fields):
        fetch_calls.append(issue_ref.issue_key)
        return {
            "siteUrl": session.base_url,
            "issueUrl": f"{session.base_url}/browse/{issue_ref.issue_key}",
            "id": "9090",
            "key": issue_ref.issue_key,
            "summary": "Existing issue",
            "status": {"name": "To Do"},
            "issueType": {"name": "Task"},
            "project": {"key": "OPS", "name": "Operations"},
            "priority": None,
            "assignee": None,
            "reporter": None,
            "labels": [],
            "created": "2026-03-18T00:00:00Z",
            "updated": "2026-03-18T00:00:00Z",
            "descriptionAdf": None,
        }

    monkeypatch.setattr(jira, "fetch_issue", fake_fetch_issue)
    monkeypatch.setattr(
        jira,
        "resolve_requested_sprint",
        lambda base_url, *, current_sprint, sprint_id, project_key_or_id="", board_id=None: (
            session,
            {"id": "21", "name": "Delivery Board", "type": "scrum"},
            {"id": "31", "name": "Sprint 31", "state": "active", "originBoardId": "21"},
        ),
    )

    def fail_update_issue_fields(session, issue_key, *, payload_fields):
        raise AssertionError(
            "update_issue_fields should not run for sprint-only updates"
        )

    monkeypatch.setattr(jira, "update_issue_fields", fail_update_issue_fields)
    monkeypatch.setattr(
        jira,
        "assign_issue_to_sprint",
        lambda issue_ref, *, sprint_id: (
            seen_assignments.append((issue_ref.issue_key, sprint_id))
            or fake_fetch_issue(issue_ref, fields=jira.DEFAULT_GET_FIELDS)
        ),
    )

    assert (
        jira.main(
            [
                "update",
                "OPS-9",
                "--base-url",
                "https://example.atlassian.net",
                "--current-sprint",
                "--apply",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert seen_assignments == [("OPS-9", 31)]
    assert fetch_calls == ["OPS-9", "OPS-9"]
    assert payload["issue"]["key"] == "OPS-9"
    assert payload["board"]["id"] == "21"
    assert payload["sprint"]["id"] == "31"


def test_jira_update_apply_sprint_only_assigns_via_issue_field_update(
    monkeypatch, capsys
) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    fetch_calls: list[str] = []
    seen_fallback: list[tuple[str, int]] = []
    monkeypatch.setattr(
        jira,
        "load_session",
        lambda base_url, allow_reauth=True: session,
    )
    monkeypatch.setattr(jira, "fetch_edit_fields", lambda issue_ref: (session, {}))

    def fake_fetch_issue(issue_ref, *, fields):
        fetch_calls.append(issue_ref.issue_key)
        return {
            "siteUrl": session.base_url,
            "issueUrl": f"{session.base_url}/browse/{issue_ref.issue_key}",
            "id": "9090",
            "key": issue_ref.issue_key,
            "summary": "Existing issue",
            "status": {"name": "To Do"},
            "issueType": {"name": "Task"},
            "project": {"key": "OPS", "name": "Operations"},
            "priority": None,
            "assignee": None,
            "reporter": None,
            "labels": [],
            "created": "2026-03-18T00:00:00Z",
            "updated": "2026-03-18T00:00:00Z",
            "descriptionAdf": None,
        }

    monkeypatch.setattr(jira, "fetch_issue", fake_fetch_issue)
    monkeypatch.setattr(
        jira,
        "resolve_requested_sprint",
        lambda base_url, *, current_sprint, sprint_id, project_key_or_id="", board_id=None: (
            session,
            {"id": "21", "name": "Delivery Board", "type": "scrum"},
            {"id": "31", "name": "Sprint 31", "state": "active", "originBoardId": "21"},
        ),
    )

    monkeypatch.setattr(
        jira,
        "assign_issue_to_sprint",
        lambda issue_ref, *, sprint_id: (
            seen_fallback.append((issue_ref.issue_key, sprint_id))
            or {
                "siteUrl": session.base_url,
                "issueUrl": f"{session.base_url}/browse/{issue_ref.issue_key}",
                "id": "9090",
                "key": issue_ref.issue_key,
                "summary": "Existing issue",
                "status": {"name": "To Do"},
                "issueType": {"name": "Task"},
                "project": {"key": "OPS", "name": "Operations"},
                "priority": None,
                "assignee": None,
                "reporter": None,
                "labels": [],
                "created": "2026-03-18T00:00:00Z",
                "updated": "2026-03-18T00:00:00Z",
                "descriptionAdf": None,
            }
        ),
    )

    assert (
        jira.main(
            [
                "update",
                "OPS-9",
                "--base-url",
                "https://example.atlassian.net",
                "--current-sprint",
                "--apply",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert fetch_calls == ["OPS-9"]
    assert seen_fallback == [("OPS-9", 31)]
    assert payload["issue"]["key"] == "OPS-9"
    assert payload["board"]["id"] == "21"
    assert payload["sprint"]["id"] == "31"


def test_jira_comment_preview_renders_markdown_to_adf(monkeypatch, capsys) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    monkeypatch.setattr(
        jira,
        "load_session",
        lambda base_url, allow_reauth=True: session,
    )

    assert (
        jira.main(
            [
                "comment",
                "OPS-9",
                "--base-url",
                "https://example.atlassian.net",
                "--body",
                "Need **follow-up** on [spec](https://example.invalid/spec).",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    paragraph = payload["payload"]["body"]["content"][0]
    assert paragraph["type"] == "paragraph"
    assert (
        payload["bodyMarkdown"]
        == "Need **follow-up** on [spec](https://example.invalid/spec)."
    )


def test_jira_link_preview_supports_remote_links(monkeypatch, capsys) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    monkeypatch.setattr(
        jira,
        "load_session",
        lambda base_url, allow_reauth=True: session,
    )

    assert (
        jira.main(
            [
                "link",
                "OPS-9",
                "https://example.invalid/request/42",
                "--base-url",
                "https://example.atlassian.net",
                "--relationship",
                "tracks",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "link"
    assert payload["remoteLinks"] == [
        {"relationship": "tracks", "url": "https://example.invalid/request/42"}
    ]
    assert payload["payload"]["object"]["title"] == "https://example.invalid/request/42"


def test_jira_transition_preview_reports_missing_required_fields(
    monkeypatch, capsys
) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    monkeypatch.setattr(
        jira,
        "load_session",
        lambda base_url, allow_reauth=True: session,
    )
    monkeypatch.setattr(
        jira,
        "resolve_transition",
        lambda issue_ref, raw: (
            session,
            {
                "id": "31",
                "name": "Start Progress",
                "fields": {
                    "customfield_20000": jira.normalize_field_metadata(
                        "customfield_20000",
                        {
                            "name": "Rollout plan",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    )
                },
            },
        ),
    )

    assert (
        jira.main(
            [
                "transition",
                "OPS-9",
                "--base-url",
                "https://example.atlassian.net",
                "--to",
                "Start Progress",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["missingRequiredFields"] == ["Rollout plan (customfield_20000)"]


def test_atlassian_default_scope_includes_jira_software_sprint_scopes() -> None:
    assert "read:board-scope:jira-software" in atlassian.DEFAULT_OAUTH_SCOPE
    assert "read:sprint:jira-software" in atlassian.DEFAULT_OAUTH_SCOPE
    assert "write:sprint:jira-software" in atlassian.DEFAULT_OAUTH_SCOPE


def test_jira_sprints_summary_lists_scrum_boards_and_sprints(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        jira,
        "collect_board_sprints",
        lambda base_url, project_key_or_id="", board_id=None, state="": (
            jira.Session(
                token="token",
                cloud_id="cloud-123",
                base_url="https://example.atlassian.net",
            ),
            [
                {
                    "id": "21",
                    "name": "Delivery Board",
                    "type": "scrum",
                    "sprints": [
                        {
                            "id": "31",
                            "name": "Sprint 31",
                            "state": "active",
                            "goal": "",
                        },
                        {
                            "id": "32",
                            "name": "Sprint 32",
                            "state": "future",
                            "goal": "",
                        },
                    ],
                }
            ],
        ),
    )

    assert jira.main(["sprints", "--project", "OPS"]) == 0
    rendered = capsys.readouterr().out
    assert "project: OPS" in rendered
    assert "- board 21: Delivery Board" in rendered
    assert "sprint 31: Sprint 31 [active]" in rendered
    assert "sprint 32: Sprint 32 [future]" in rendered


def test_jira_projects_supports_bounded_json_pages(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        jira,
        "fetch_projects",
        lambda base_url: (
            jira.Session(
                token="token",
                cloud_id="cloud-123",
                base_url="https://example.atlassian.net",
            ),
            [
                {
                    "id": "10000",
                    "key": "OPS",
                    "name": "Operations",
                    "projectTypeKey": "software",
                    "simplified": False,
                },
                {
                    "id": "10001",
                    "key": "PLAT",
                    "name": "Platform",
                    "projectTypeKey": "software",
                    "simplified": False,
                },
                {
                    "id": "10002",
                    "key": "SEC",
                    "name": "Security",
                    "projectTypeKey": "software",
                    "simplified": False,
                },
            ],
        ),
    )

    assert (
        jira.main(["projects", "--limit", "1", "--offset", "1", "--output", "json"])
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["totalCount"] == 3
    assert payload["shownCount"] == 1
    assert payload["offset"] == 1
    assert payload["nextOffset"] == 2
    assert payload["truncated"] is True
    assert [item["key"] for item in payload["projects"]] == ["PLAT"]


def test_jira_sprints_pages_across_project_sprints(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        jira,
        "collect_board_sprints",
        lambda base_url, project_key_or_id="", board_id=None, state="": (
            jira.Session(
                token="token",
                cloud_id="cloud-123",
                base_url="https://example.atlassian.net",
            ),
            [
                {
                    "id": "21",
                    "name": "Delivery Board",
                    "type": "scrum",
                    "sprints": [
                        {
                            "id": "31",
                            "name": "Sprint 31",
                            "state": "active",
                            "goal": "",
                        },
                        {
                            "id": "32",
                            "name": "Sprint 32",
                            "state": "future",
                            "goal": "",
                        },
                    ],
                },
                {
                    "id": "22",
                    "name": "Platform Board",
                    "type": "scrum",
                    "sprints": [
                        {"id": "41", "name": "Sprint 41", "state": "active", "goal": ""}
                    ],
                },
            ],
        ),
    )

    assert (
        jira.main(
            [
                "sprints",
                "--project",
                "OPS",
                "--limit",
                "1",
                "--offset",
                "1",
                "--output",
                "json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["pagingUnit"] == "sprints"
    assert payload["totalCount"] == 3
    assert payload["shownCount"] == 1
    assert payload["offset"] == 1
    assert payload["nextOffset"] == 2
    assert payload["truncated"] is True
    assert len(payload["boards"]) == 1
    assert payload["boards"][0]["id"] == "21"
    assert [item["id"] for item in payload["boards"][0]["sprints"]] == ["32"]


def test_jira_explicit_board_sprint_page_exhaustion_does_not_render_empty_board(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        jira,
        "collect_board_sprints",
        lambda base_url, project_key_or_id="", board_id=None, state="": (
            jira.Session(
                token="token",
                cloud_id="cloud-123",
                base_url="https://example.atlassian.net",
            ),
            [
                {
                    "id": "21",
                    "name": "Delivery Board",
                    "type": "scrum",
                    "sprints": [
                        {"id": "31", "name": "Sprint 31", "state": "active", "goal": ""}
                    ],
                }
            ],
        ),
    )

    assert (
        jira.main(
            [
                "sprints",
                "--board",
                "21",
                "--limit",
                "1",
                "--offset",
                "99",
                "--output",
                "json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["pagingUnit"] == "sprints"
    assert payload["totalCount"] == 1
    assert payload["shownCount"] == 0
    assert payload["offset"] == 99
    assert payload["boards"] == []

    assert (
        jira.main(["sprints", "--board", "21", "--limit", "1", "--offset", "99"]) == 0
    )
    rendered = capsys.readouterr().out
    assert "sprints: 1 total (showing 0, offset 99)" in rendered
    assert "Delivery Board" not in rendered
    assert "no sprints" not in rendered


def test_jira_add_to_sprint_preview_resolves_current_sprint(
    monkeypatch, capsys
) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    monkeypatch.setattr(
        jira,
        "resolve_assignment_sprint",
        lambda base_url, project_key_or_id="", board_id=None, sprint_id=None, current=False: (
            session,
            {"id": "21", "name": "Delivery Board", "type": "scrum"},
            {"id": "31", "name": "Sprint 31", "state": "active", "originBoardId": "21"},
        ),
    )

    assert (
        jira.main(
            [
                "add-to-sprint",
                "OPS-6994",
                "--current",
                "--project",
                "OPS",
                "--base-url",
                "https://example.atlassian.net",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "add-to-sprint"
    assert payload["target"]["issue"] == "OPS-6994"
    assert payload["target"]["board"] == "21"
    assert payload["target"]["boardName"] == "Delivery Board"
    assert payload["target"]["sprint"] == "31"
    assert payload["target"]["sprintName"] == "Sprint 31"
    assert payload["target"]["sprintState"] == "active"


def test_jira_add_to_sprint_apply_posts_issue_to_resolved_sprint(
    monkeypatch, capsys
) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(
        jira,
        "resolve_assignment_sprint",
        lambda base_url, project_key_or_id="", board_id=None, sprint_id=None, current=False: (
            session,
            {"id": "21", "name": "Delivery Board", "type": "scrum"},
            {"id": "31", "name": "Sprint 31", "state": "active", "originBoardId": "21"},
        ),
    )
    monkeypatch.setattr(
        jira,
        "assign_issue_to_sprint",
        lambda issue_ref, *, sprint_id: (
            seen.append((issue_ref.issue_key, sprint_id))
            or {
                "key": issue_ref.issue_key,
                "issueUrl": f"{session.base_url}/browse/{issue_ref.issue_key}",
            }
        ),
    )

    assert (
        jira.main(
            [
                "add-to-sprint",
                "OPS-6994",
                "--current",
                "--project",
                "OPS",
                "--base-url",
                "https://example.atlassian.net",
                "--apply",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert seen == [("OPS-6994", 31)]
    assert payload["issue"] == "OPS-6994"
    assert payload["sprint"]["id"] == "31"


def test_jira_add_to_sprint_apply_assigns_via_issue_field_update(
    monkeypatch, capsys
) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    seen_fallback: list[tuple[str, int]] = []
    monkeypatch.setattr(
        jira,
        "resolve_assignment_sprint",
        lambda base_url, project_key_or_id="", board_id=None, sprint_id=None, current=False: (
            session,
            {"id": "21", "name": "Delivery Board", "type": "scrum"},
            {"id": "31", "name": "Sprint 31", "state": "active", "originBoardId": "21"},
        ),
    )

    monkeypatch.setattr(
        jira,
        "assign_issue_to_sprint",
        lambda issue_ref, *, sprint_id: (
            seen_fallback.append((issue_ref.issue_key, sprint_id))
            or {
                "key": issue_ref.issue_key,
                "issueUrl": f"{session.base_url}/browse/{issue_ref.issue_key}",
            }
        ),
    )

    assert (
        jira.main(
            [
                "add-to-sprint",
                "OPS-6994",
                "--current",
                "--project",
                "OPS",
                "--base-url",
                "https://example.atlassian.net",
                "--apply",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert seen_fallback == [("OPS-6994", 31)]
    assert payload["issue"] == "OPS-6994"
    assert payload["sprint"]["id"] == "31"


def test_jira_resolve_current_sprint_requires_disambiguation(monkeypatch) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    monkeypatch.setattr(
        jira,
        "collect_board_sprints",
        lambda base_url, project_key_or_id="", board_id=None, state="": (
            session,
            [
                {
                    "id": "21",
                    "name": "Delivery Board",
                    "type": "scrum",
                    "sprints": [{"id": "31", "name": "Sprint 31", "state": "active"}],
                },
                {
                    "id": "22",
                    "name": "Platform Board",
                    "type": "scrum",
                    "sprints": [{"id": "41", "name": "Sprint 41", "state": "active"}],
                },
            ],
        ),
    )

    with pytest.raises(
        jira.ToolError, match="multiple active sprints.*--board or --sprint"
    ):
        jira.resolve_current_sprint(
            "https://example.atlassian.net", project_key_or_id="OPS"
        )


def test_jira_resolve_assignment_sprint_uses_origin_board_without_board_fetch(
    monkeypatch,
) -> None:
    session = jira.Session(
        token="token",
        cloud_id="cloud-123",
        base_url="https://example.atlassian.net",
    )
    monkeypatch.setattr(
        jira, "load_session", lambda base_url, allow_reauth=True: session
    )
    monkeypatch.setattr(
        jira,
        "fetch_sprint",
        lambda session, sprint_id: {
            "id": str(sprint_id),
            "name": "Sprint 31",
            "state": "active",
            "originBoardId": "96",
        },
    )

    def fail_fetch_board(*args, **kwargs):
        raise AssertionError(
            "fetch_board should not run for explicit sprint resolution"
        )

    monkeypatch.setattr(jira, "fetch_board", fail_fetch_board)

    resolved_session, board, sprint = jira.resolve_assignment_sprint(
        "https://example.atlassian.net",
        sprint_id=31,
    )

    assert resolved_session == session
    assert board == {"id": "96", "name": "", "type": "scrum"}
    assert sprint["id"] == "31"


def test_confluence_search_markdown_is_readable() -> None:
    rendered = confluence.render_search_markdown(
        {
            "cql": 'text ~ "Architecture" AND type = page',
            "type": "page",
            "size": 1,
            "results": [
                {
                    "title": "Architecture Overview",
                    "id": "10101",
                    "type": "page",
                    "url": "https://example.atlassian.net/wiki/spaces/ENG/pages/10101",
                    "spaceKey": "ENG",
                    "lastModified": "2026-03-10T11:30:00Z",
                    "excerptHtml": "<p>WireGuard <strong>overlay</strong> notes</p>",
                }
            ],
        }
    )

    assert "### Confluence Search:" in rendered
    assert "- Updated: 2026-03-10T11:30:00Z" in rendered
    assert "locator `confluence:10101`" in rendered
    assert "space `ENG`" in rendered
    assert "WireGuard overlay notes" in rendered


def test_confluence_cql_markdown_is_readable(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        confluence,
        "load_session",
        lambda page_ref: object(),
    )
    monkeypatch.setattr(
        confluence,
        "search_confluence",
        lambda session, cql, limit, cursor: {
            "cql": cql,
            "type": "page",
            "size": 1,
            "results": [
                {
                    "title": "Architecture Overview",
                    "id": "10101",
                    "type": "page",
                    "url": "https://example.atlassian.net/wiki/spaces/ENG/pages/10101",
                    "spaceKey": "ENG",
                    "lastModified": "2026-03-10T11:30:00Z",
                    "excerptHtml": "<p>WireGuard <strong>overlay</strong> notes</p>",
                }
            ],
        },
    )

    assert (
        confluence.main(["cql", 'text ~ "Architecture"', "--output", "markdown"]) == 0
    )
    rendered = capsys.readouterr().out
    assert "### Confluence Search:" in rendered
    assert "locator `confluence:10101`" in rendered
