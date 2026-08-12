from __future__ import annotations

import base64
import datetime as dt
import json
import os
from pathlib import Path

import pytest

from gotta.plugins import granola


@pytest.fixture(autouse=True)
def _isolate_granola_credentials(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "default-gotta.toml"
    config_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))
    oauth_dir = tmp_path / "auth" / "granola"
    monkeypatch.setattr(granola, "MCP_OAUTH_DIR", oauth_dir)
    monkeypatch.setattr(granola, "MCP_OAUTH_FILE", oauth_dir / "oauth.json")


def _doc(
    doc_id: str,
    title: str,
    *,
    created: str = "2026-03-18T10:00:00Z",
    updated: str = "2026-03-18T11:00:00Z",
    **overrides: object,
) -> dict[str, object]:
    document: dict[str, object] = {
        "id": doc_id,
        "title": title,
        "created_at": created,
        "updated_at": updated,
        "people": [{"name": "Alex Example"}],
    }
    document.update(overrides)
    return document


def _freeze_now(monkeypatch) -> None:
    monkeypatch.setattr(
        granola,
        "utc_now",
        lambda: dt.datetime(2026, 3, 18, 12, 0, tzinfo=dt.timezone.utc),
    )


def _jwt_payload(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return encoded.rstrip("=")


def _fake_jwt(payload: dict[str, object]) -> str:
    return ".".join(
        [
            _jwt_payload({"alg": "none", "typ": "JWT"}),
            _jwt_payload(payload),
            "signature",
        ]
    )


def test_granola_parser_defaults() -> None:
    assert granola.build_parser().parse_args(["status"]).output == "summary"
    assert granola.build_parser().parse_args(["list"]).output == "markdown"
    assert granola.build_parser().parse_args(["list"]).sort == "created"
    assert granola.build_parser().parse_args(["list"]).order == "desc"
    assert granola.build_parser().parse_args(["list"]).offset == 0
    assert granola.build_parser().parse_args(["list"]).time_range == "last_90_days"
    assert (
        granola.build_parser().parse_args(["auth", "refresh"]).auth_command == "refresh"
    )
    assert granola.build_parser().parse_args(["auth", "login"]).auth_command == "login"
    assert (
        granola.build_parser().parse_args(["auth", "logout"]).auth_command == "logout"
    )
    assert granola.build_parser().parse_args(["search", "needle"]).mode == "auto"
    assert (
        granola.build_parser().parse_args(["search", "needle"]).time_range
        == "last_90_days"
    )
    assert granola.build_parser().parse_args(["get", "doc-1"]).output == "markdown"
    assert (
        granola.build_parser().parse_args(["transcript", "doc-1"]).output == "markdown"
    )
    assert (
        granola.build_parser().parse_args(["search-transcript", "needle"]).time_range
        == "last_30_days"
    )


def test_load_access_token_reads_nested_workos_tokens(tmp_path: Path) -> None:
    supabase = tmp_path / "supabase.json"
    supabase.write_text(
        json.dumps({"workos_tokens": json.dumps({"access_token": "token-123"})}),
        encoding="utf-8",
    )

    assert granola.load_access_token(supabase) == "token-123"


def test_granola_status_reports_missing_local_session(capsys, tmp_path: Path) -> None:
    result = granola.main(["--supabase", str(tmp_path / "missing.json"), "status"])

    out = capsys.readouterr().out
    assert result == 0
    assert "surface\tgranola" in out
    assert "session_status\tmissing" in out
    assert "local_session_present\tfalse" in out


def test_granola_status_reports_expired_plaintext_token_with_newer_encrypted_session(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    _freeze_now(monkeypatch)
    expired_token = _fake_jwt(
        {
            "iss": "https://auth.granola.ai/user_management/client",
            "iat": 1773770400,
            "exp": 1773774000,
        }
    )
    supabase = tmp_path / "supabase.json"
    supabase.write_text(
        json.dumps(
            {
                "workos_tokens": json.dumps(
                    {
                        "access_token": expired_token,
                        "refresh_token": "refresh-token",
                    }
                )
            }
        ),
        encoding="utf-8",
    )
    encrypted = tmp_path / "supabase.json.enc"
    encrypted.write_bytes(b"encrypted")
    os.utime(supabase, (1773770400, 1773770400))
    os.utime(encrypted, (1773777600, 1773777600))

    def fail_fetch(api_url, token, limit=None):
        raise granola.ToolError(
            'Granola API request failed: HTTP 401: {"message":"Unauthorized"}'
        )

    monkeypatch.setattr(granola, "fetch_documents", fail_fetch)

    result = granola.main(["--supabase", str(supabase), "status"])

    out = capsys.readouterr().out
    assert result == 0
    assert "session_status\tinvalid" in out
    assert "encrypted_session_present\ttrue" in out
    assert "encrypted_session_newer\ttrue" in out
    assert "access_token_expires_at\t2026-03-17T19:00:00Z" in out
    assert "access_token_expired\ttrue" in out
    assert "`gotta granola auth refresh`" in out


def test_granola_auth_refresh_skips_when_plaintext_token_is_current(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    _freeze_now(monkeypatch)
    fresh_token = _fake_jwt(
        {
            "iss": "https://auth.granola.ai/user_management/client",
            "iat": 1773835200,
            "exp": 1773921600,
        }
    )
    supabase = tmp_path / "supabase.json"
    supabase.write_text(
        json.dumps(
            {
                "workos_tokens": json.dumps(
                    {
                        "access_token": fresh_token,
                        "refresh_token": "refresh-token",
                    }
                )
            }
        ),
        encoding="utf-8",
    )

    def unexpected_post_json(*args, **kwargs):
        raise AssertionError("current access tokens should not be refreshed")

    monkeypatch.setattr(granola, "post_json", unexpected_post_json)

    result = granola.main(["--supabase", str(supabase), "auth", "refresh"])

    out = capsys.readouterr().out
    assert result == 0
    assert "status\tskipped" in out
    assert "refreshed\tfalse" in out
    assert "already has a non-expired access token" in out


def test_granola_auth_refresh_updates_plaintext_workos_tokens(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    _freeze_now(monkeypatch)
    expired_token = _fake_jwt(
        {
            "iss": "https://auth.granola.ai/user_management/client",
            "iat": 1773770400,
            "exp": 1773774000,
        }
    )
    refreshed_token = _fake_jwt(
        {
            "iss": "https://auth.granola.ai/user_management/client",
            "iat": 1773835200,
            "exp": 1773921600,
        }
    )
    supabase = tmp_path / "supabase.json"
    supabase.write_text(
        json.dumps(
            {
                "session_id": "old-session",
                "workos_tokens": json.dumps(
                    {
                        "access_token": expired_token,
                        "expires_in": 3600,
                        "refresh_token": "old-refresh-token",
                        "session_id": "old-session",
                        "token_type": "Bearer",
                    }
                ),
            }
        ),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_post_json(url, token, payload, *, extra_headers=None):
        seen["url"] = url
        seen["token"] = token
        seen["payload"] = payload
        return {
            "access_token": refreshed_token,
            "expires_in": 3600,
            "refresh_token": "new-refresh-token",
            "session_id": "new-session",
            "token_type": "Bearer",
        }

    monkeypatch.setattr(granola, "post_json", fake_post_json)

    result = granola.main(["--supabase", str(supabase), "auth", "refresh"])

    out = capsys.readouterr().out
    refreshed = json.loads(supabase.read_text(encoding="utf-8"))
    refreshed_tokens = json.loads(refreshed["workos_tokens"])
    assert result == 0
    assert "status\trefreshed" in out
    assert "refreshed\ttrue" in out
    assert seen["payload"] == {"refresh_token": "old-refresh-token"}
    assert refreshed_tokens["access_token"] == refreshed_token
    assert refreshed_tokens["refresh_token"] == "new-refresh-token"
    assert refreshed_tokens["obtained_at"] == 1773835200000
    assert refreshed["session_id"] == "new-session"


def test_granola_list_refreshes_expired_plaintext_token_before_fetching(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    _freeze_now(monkeypatch)
    expired_token = _fake_jwt(
        {
            "iss": "https://auth.granola.ai/user_management/client",
            "iat": 1773770400,
            "exp": 1773774000,
        }
    )
    refreshed_token = _fake_jwt(
        {
            "iss": "https://auth.granola.ai/user_management/client",
            "iat": 1773835200,
            "exp": 1773921600,
        }
    )
    supabase = tmp_path / "supabase.json"
    supabase.write_text(
        json.dumps(
            {
                "workos_tokens": json.dumps(
                    {
                        "access_token": expired_token,
                        "expires_in": 3600,
                        "refresh_token": "refresh-token",
                        "token_type": "Bearer",
                    }
                )
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        granola,
        "post_json",
        lambda url, token, payload, *, extra_headers=None: {
            "access_token": refreshed_token,
            "expires_in": 3600,
            "refresh_token": "refresh-token",
            "token_type": "Bearer",
        },
    )
    seen_tokens: list[str] = []

    def fake_fetch_documents(api_url, token, limit=None):
        seen_tokens.append(token)
        return [_doc("11111111-1111-1111-1111-111111111111", "Weekly Review")]

    monkeypatch.setattr(granola, "fetch_documents", fake_fetch_documents)

    result = granola.main(
        ["--supabase", str(supabase), "list", "--limit", "1", "--output", "summary"]
    )

    out = capsys.readouterr().out
    assert result == 0
    assert seen_tokens == [refreshed_token]
    assert "Weekly Review" in out


def test_best_note_body_prefers_notes_markdown_then_panel_html() -> None:
    markdown_document = _doc(
        "11111111-1111-1111-1111-111111111111",
        "Weekly Review",
        notes_markdown="## Highlights\n\n- Stable build",
        last_viewed_panel={"original_content": "<p>fallback</p>"},
    )
    html_document = _doc(
        "22222222-2222-2222-2222-222222222222",
        "Planning Note",
        notes_markdown="",
        notes_plain="",
        notes={},
        last_viewed_panel={"original_content": "<p>Rendered <strong>HTML</strong></p>"},
    )

    first = granola.best_note_body(markdown_document)
    second = granola.best_note_body(html_document)

    assert first.source == "notes_markdown"
    assert "## Highlights" in first.body
    assert second.source == "last_viewed_panel.original_content"
    assert "Rendered **HTML**" in second.body


def test_granola_get_markdown_includes_metadata_header(monkeypatch, capsys) -> None:
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc(
                "11111111-1111-1111-1111-111111111111",
                "Weekly Review",
                notes_markdown="## Highlights\n\n- Stable build",
            )
        ],
    )

    result = granola.main(["get", "11111111-1111-1111-1111-111111111111"])

    out = capsys.readouterr().out
    assert result == 0
    assert "# Weekly Review" in out
    assert "- Locator: `granola:11111111-1111-1111-1111-111111111111`" in out
    assert "- Created: 2026-03-18T10:00:00Z" in out
    assert "- Updated: 2026-03-18T11:00:00Z" in out
    assert "- Body Source: `notes_markdown`" in out
    assert (
        "- Transcript Locator: `granola:transcript 11111111-1111-1111-1111-111111111111`"
        in out
    )
    assert "## Highlights" in out


def test_fetch_transcript_returns_sorted_segment_list(monkeypatch) -> None:
    monkeypatch.setattr(
        granola,
        "request_json",
        lambda url, token, payload: [
            {
                "id": "seg-2",
                "document_id": payload["document_id"],
                "start_timestamp": "2026-03-18T11:02:00Z",
                "text": "Second",
            },
            {
                "id": "seg-1",
                "document_id": payload["document_id"],
                "start_timestamp": "2026-03-18T11:01:00Z",
                "text": "First",
            },
        ],
    )

    segments = granola.fetch_transcript(
        "https://example.invalid/v1/get-document-transcript",
        "token",
        "11111111-1111-1111-1111-111111111111",
    )

    assert [segment["id"] for segment in segments] == ["seg-1", "seg-2"]


def test_granola_get_chooses_most_recent_exact_title_match(monkeypatch, capsys) -> None:
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc(
                "11111111-1111-1111-1111-111111111111",
                "Weekly Review",
                updated="2026-03-18T09:00:00Z",
                notes_markdown="Older note",
            ),
            _doc(
                "22222222-2222-2222-2222-222222222222",
                "Weekly Review",
                updated="2026-03-18T12:00:00Z",
                notes_markdown="Newer note",
            ),
        ],
    )

    result = granola.main(["get", "Weekly Review"])

    captured = capsys.readouterr()
    assert result == 0
    assert "multiple exact title matches" in captured.err
    assert "using most recent id 22222222-2222-2222-2222-222222222222" in captured.err
    assert "Newer note" in captured.out


def test_granola_get_meta_includes_transcript_locator(monkeypatch, capsys) -> None:
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc(
                "11111111-1111-1111-1111-111111111111",
                "Weekly Review",
                notes_markdown="## Highlights\n\n- Stable build",
            )
        ],
    )

    result = granola.main(
        ["get", "11111111-1111-1111-1111-111111111111", "--output", "meta"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["locator"] == "granola:11111111-1111-1111-1111-111111111111"
    assert (
        payload["transcriptLocator"]
        == "granola:transcript 11111111-1111-1111-1111-111111111111"
    )
    assert "transcriptEnabled" not in payload


def test_granola_transcript_markdown_includes_segment_metadata(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc(
                "11111111-1111-1111-1111-111111111111",
                "Weekly Review",
                notes_markdown="## Highlights\n\n- Stable build",
                transcribe=True,
            )
        ],
    )
    monkeypatch.setattr(
        granola,
        "fetch_transcript",
        lambda api_url, token, document_id: [
            {
                "id": "seg-1",
                "document_id": document_id,
                "start_timestamp": "2026-03-18T10:01:00Z",
                "end_timestamp": "2026-03-18T10:01:30Z",
                "source": "microphone",
                "text": "Reviewed rollout steps.",
                "is_final": True,
            }
        ],
    )

    result = granola.main(["transcript", "11111111-1111-1111-1111-111111111111"])

    out = capsys.readouterr().out
    assert result == 0
    assert "# Transcript: Weekly Review" in out
    assert "- Locator: `granola:transcript 11111111-1111-1111-1111-111111111111`" in out
    assert "- Segment Count: 1" in out
    assert "## Microphone (2026-03-18T10:01:00Z -> 2026-03-18T10:01:30Z)" in out
    assert "Reviewed rollout steps." in out


def test_granola_transcript_summary_prints_id_count_and_title(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc(
                "11111111-1111-1111-1111-111111111111",
                "Weekly Review",
                transcribe=True,
            )
        ],
    )
    monkeypatch.setattr(
        granola,
        "fetch_transcript",
        lambda api_url, token, document_id: [
            {"id": "seg-1", "document_id": document_id, "text": "One"},
            {"id": "seg-2", "document_id": document_id, "text": "Two"},
        ],
    )

    result = granola.main(
        [
            "transcript",
            "11111111-1111-1111-1111-111111111111",
            "--output",
            "summary",
        ]
    )

    out = capsys.readouterr().out
    assert result == 0
    assert out.strip() == ("11111111-1111-1111-1111-111111111111\t2\tWeekly Review")


def test_granola_search_markdown_reports_match_source_and_excerpt(
    monkeypatch,
    capsys,
) -> None:
    _freeze_now(monkeypatch)
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc(
                "11111111-1111-1111-1111-111111111111",
                "Latency Review",
                created="2026-03-18T10:00:00Z",
                notes_markdown="Trimmed runtime costs and reduced queue delay across the mesh.",
            ),
            _doc(
                "22222222-2222-2222-2222-222222222222",
                "Operations Digest",
                created="2025-11-01T10:00:00Z",
                notes_markdown="Nothing relevant here.",
            ),
        ],
    )

    result = granola.main(["search", "queue delay"])

    out = capsys.readouterr().out
    assert result == 0
    assert "### Granola Search: queue delay" in out
    assert "[Latency Review](granola:11111111-1111-1111-1111-111111111111)" in out
    assert "matched by `title+body`" in out or "matched by `body`" in out
    assert "_Window_: `last 90 days`" in out
    assert "Trimmed runtime costs and reduced queue delay" in out


def test_granola_list_summary_renders_tabular_recent_notes(monkeypatch, capsys) -> None:
    _freeze_now(monkeypatch)
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc("11111111-1111-1111-1111-111111111111", "Weekly Review"),
            _doc(
                "22222222-2222-2222-2222-222222222222",
                "Planning Note",
                created="2025-10-01T10:00:00Z",
                updated="2025-10-01T11:00:00Z",
            ),
        ],
    )

    result = granola.main(["list", "--output", "summary"])

    out = capsys.readouterr().out
    assert result == 0
    assert "11111111-1111-1111-1111-111111111111\tWeekly Review" in out
    assert "22222222-2222-2222-2222-222222222222\tPlanning Note" not in out


def test_granola_list_can_sort_by_created_with_offset(monkeypatch, capsys) -> None:
    _freeze_now(monkeypatch)
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc(
                "11111111-1111-1111-1111-111111111111",
                "Newest Created",
                created="2026-03-18T10:00:00Z",
                updated="2026-03-18T12:00:00Z",
            ),
            _doc(
                "22222222-2222-2222-2222-222222222222",
                "Oldest Created",
                created="2026-03-10T10:00:00Z",
                updated="2026-03-18T11:00:00Z",
            ),
            _doc(
                "33333333-3333-3333-3333-333333333333",
                "Middle Created",
                created="2026-03-14T10:00:00Z",
                updated="2026-03-18T10:30:00Z",
            ),
        ],
    )

    result = granola.main(
        [
            "list",
            "--sort",
            "created",
            "--order",
            "asc",
            "--offset",
            "1",
            "--limit",
            "1",
            "--output",
            "summary",
        ]
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "33333333-3333-3333-3333-333333333333\tMiddle Created" in out
    assert "Oldest Created" not in out
    assert "Newest Created" not in out


def test_granola_export_writes_only_notes_with_body(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    _freeze_now(monkeypatch)
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc(
                "11111111-1111-1111-1111-111111111111",
                "Weekly Review",
                notes_markdown="Export me",
            ),
            _doc(
                "22222222-2222-2222-2222-222222222222",
                "Empty Note",
                notes_markdown="",
            ),
        ],
    )

    result = granola.main(["export", str(tmp_path), "--limit", "5"])

    captured = capsys.readouterr()
    assert result == 0
    exported = list(tmp_path.glob("*.md"))
    assert len(exported) == 1
    assert "Export me" in exported[0].read_text(encoding="utf-8")
    assert "exported 1 notes" in captured.err


def test_granola_list_honors_custom_date_window(monkeypatch, capsys) -> None:
    _freeze_now(monkeypatch)
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc(
                "11111111-1111-1111-1111-111111111111",
                "March Note",
                created="2026-03-10T10:00:00Z",
            ),
            _doc(
                "22222222-2222-2222-2222-222222222222",
                "February Note",
                created="2026-02-10T10:00:00Z",
                updated="2026-02-10T11:00:00Z",
            ),
        ],
    )

    result = granola.main(
        [
            "list",
            "--after",
            "2026-03-01",
            "--before",
            "2026-03-31",
            "--output",
            "summary",
        ]
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "March Note" in out
    assert "February Note" not in out


def test_granola_list_window_uses_created_time_not_recent_activity(
    monkeypatch, capsys
) -> None:
    _freeze_now(monkeypatch)
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc(
                "11111111-1111-1111-1111-111111111111",
                "Long Lived Note",
                created="2025-01-10T10:00:00Z",
                updated="2026-03-17T10:00:00Z",
            ),
            _doc(
                "22222222-2222-2222-2222-222222222222",
                "Dormant Note",
                created="2025-01-10T10:00:00Z",
                updated="2025-01-11T10:00:00Z",
            ),
        ],
    )

    result = granola.main(["list", "--output", "summary"])

    out = capsys.readouterr().out
    assert result == 0
    assert "Long Lived Note" not in out
    assert "Dormant Note" not in out


def test_granola_transcript_query_filters_segments(monkeypatch, capsys) -> None:
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc("11111111-1111-1111-1111-111111111111", "Weekly Review")
        ],
    )
    monkeypatch.setattr(
        granola,
        "fetch_transcript",
        lambda api_url, token, document_id: [
            {
                "id": "seg-1",
                "document_id": document_id,
                "start_timestamp": "2026-03-18T10:01:00Z",
                "text": "Latency dropped after the change.",
            },
            {
                "id": "seg-2",
                "document_id": document_id,
                "start_timestamp": "2026-03-18T10:02:00Z",
                "text": "We should revisit the rollout checklist.",
            },
        ],
    )

    result = granola.main(
        [
            "transcript",
            "11111111-1111-1111-1111-111111111111",
            "--query",
            "latency",
        ]
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "- Query: `latency`" in out
    assert "Latency dropped after the change." in out
    assert "rollout checklist" not in out


def test_granola_transcript_query_preferred_name_is_distinct() -> None:
    options = type("Options", (), {"save_as": ""})()

    assert (
        granola.preferred_name(
            [
                "transcript",
                "11111111-1111-1111-1111-111111111111",
                "--query",
                "latency",
            ],
            options,
        )
        == "11111111-1111-1111-1111-111111111111-transcript-query-latency.json"
    )


def test_granola_search_transcript_defaults_to_bounded_window(
    monkeypatch, capsys
) -> None:
    _freeze_now(monkeypatch)
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc(
                "11111111-1111-1111-1111-111111111111",
                "Recent Sync",
                created="2026-03-10T10:00:00Z",
            ),
            _doc(
                "22222222-2222-2222-2222-222222222222",
                "Older Sync",
                created="2025-12-01T10:00:00Z",
                updated="2025-12-01T11:00:00Z",
            ),
        ],
    )
    seen_ids: list[str] = []

    def fake_fetch_transcript(api_url, token, document_id):
        seen_ids.append(document_id)
        return [
            {
                "id": f"{document_id}-1",
                "document_id": document_id,
                "start_timestamp": "2026-03-18T10:01:00Z",
                "text": "Latency improved after the handoff.",
            }
        ]

    monkeypatch.setattr(granola, "fetch_transcript", fake_fetch_transcript)

    result = granola.main(["search-transcript", "latency"])

    out = capsys.readouterr().out
    assert result == 0
    assert seen_ids == ["11111111-1111-1111-1111-111111111111"]
    assert "### Granola Transcript Search: latency" in out
    assert "_Window_: `last 30 days`" in out
    assert "Recent Sync" in out
    assert "Older Sync" not in out


def test_granola_search_transcript_all_expands_scope(monkeypatch, capsys) -> None:
    _freeze_now(monkeypatch)
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc(
                "11111111-1111-1111-1111-111111111111",
                "Recent Sync",
                created="2026-03-10T10:00:00Z",
            ),
            _doc(
                "22222222-2222-2222-2222-222222222222",
                "Older Sync",
                created="2025-12-01T10:00:00Z",
                updated="2025-12-01T11:00:00Z",
            ),
        ],
    )

    monkeypatch.setattr(
        granola,
        "fetch_transcript",
        lambda api_url, token, document_id: [
            {
                "id": f"{document_id}-1",
                "document_id": document_id,
                "start_timestamp": "2026-03-18T10:01:00Z",
                "text": "Latency improved after the handoff.",
            }
        ],
    )

    result = granola.main(["search-transcript", "latency", "--all", "--output", "json"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["window"]["timeRange"] == "all"
    assert payload["resultCount"] == 2


def test_granola_export_supports_bounded_sort_and_offset(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    _freeze_now(monkeypatch)
    monkeypatch.setattr(granola, "load_access_token", lambda path: "token")
    monkeypatch.setattr(
        granola,
        "fetch_documents",
        lambda api_url, token, limit=None: [
            _doc(
                "11111111-1111-1111-1111-111111111111",
                "Oldest",
                created="2026-03-01T10:00:00Z",
                notes_markdown="one",
            ),
            _doc(
                "22222222-2222-2222-2222-222222222222",
                "Middle",
                created="2026-03-05T10:00:00Z",
                notes_markdown="two",
            ),
            _doc(
                "33333333-3333-3333-3333-333333333333",
                "Newest",
                created="2026-03-10T10:00:00Z",
                notes_markdown="three",
            ),
        ],
    )

    result = granola.main(
        [
            "export",
            str(tmp_path),
            "--sort",
            "created",
            "--order",
            "asc",
            "--offset",
            "1",
            "--limit",
            "1",
        ]
    )

    exported = list(tmp_path.glob("*.md"))
    capsys.readouterr()
    assert result == 0
    assert len(exported) == 1
    assert "Middle" in exported[0].read_text(encoding="utf-8")


def test_granola_encrypted_only_session_directs_user_to_browser_login(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    config_file = tmp_path / "gotta.toml"
    config_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))
    supabase = tmp_path / "supabase.json"
    supabase.with_name("supabase.json.enc").write_bytes(b"encrypted")

    status_result = granola.main(["--supabase", str(supabase), "status"])
    status_output = capsys.readouterr().out
    list_result = granola.main(["--supabase", str(supabase), "list"])
    list_error = capsys.readouterr().err
    refresh_result = granola.main(["--supabase", str(supabase), "auth", "refresh"])
    refresh_error = capsys.readouterr().err

    assert status_result == 0
    assert "encrypted_session_present\ttrue" in status_output
    assert "gotta granola auth login" in status_output
    assert list_result == 1
    assert "desktop session" in list_error
    assert "gotta granola auth login" in list_error
    assert refresh_result == 1
    assert "gotta granola auth login" in refresh_error


def test_granola_mcp_registers_public_client_once_and_secures_state(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        granola,
        "oauth_get_json",
        lambda url, *, context: {
            "registration_endpoint": "https://auth.example/register",
            "device_authorization_endpoint": "https://auth.example/device",
            "token_endpoint": "https://auth.example/token",
        },
    )
    registrations: list[dict[str, object]] = []

    def fake_register(url, payload, *, context):
        registrations.append(payload)
        return {
            "client_id": "client-123",
            "token_endpoint_auth_method": "none",
        }

    monkeypatch.setattr(granola, "oauth_post_json", fake_register)

    first = granola.ensure_mcp_oauth_client(issuer="https://auth.example")
    second = granola.ensure_mcp_oauth_client(issuer="https://auth.example")

    assert first["client_id"] == "client-123"
    assert second["client_id"] == "client-123"
    assert registrations == [
        {
            "client_name": "gotta local CLI",
            "redirect_uris": [granola.DEFAULT_MCP_REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": granola.DEFAULT_MCP_OAUTH_SCOPE,
        }
    ]
    assert granola.MCP_OAUTH_FILE.stat().st_mode & 0o777 == 0o600


def test_granola_mcp_device_login_polls_and_persists_tokens(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        granola,
        "discover_mcp_authorization_server",
        lambda **kwargs: "https://auth.example",
    )
    monkeypatch.setattr(
        granola,
        "ensure_mcp_oauth_client",
        lambda **kwargs: {
            "client_id": "client-123",
            "device_authorization_endpoint": "https://auth.example/device",
            "token_endpoint": "https://auth.example/token",
            "scope": granola.DEFAULT_MCP_OAUTH_SCOPE,
        },
    )
    token_attempts = 0
    requests: list[tuple[str, dict[str, str]]] = []

    def fake_post(url, payload, *, context):
        nonlocal token_attempts
        requests.append((url, payload))
        if url.endswith("/device"):
            return {
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri_complete": "https://auth.example/activate?code=ABCD",
                "interval": 1,
                "expires_in": 300,
            }
        token_attempts += 1
        if token_attempts == 1:
            return {"error": "authorization_pending"}
        return {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "expires_in": 3600,
            "token_type": "Bearer",
        }

    monkeypatch.setattr(granola, "oauth_post_form_json", fake_post)
    monkeypatch.setattr(granola.time, "sleep", lambda seconds: None)

    state = granola.run_mcp_oauth_login(
        mcp_url="https://mcp.example/mcp",
        issuer="https://auth.example",
        open_browser=False,
    )

    err = capsys.readouterr().err
    stored = json.loads(granola.MCP_OAUTH_FILE.read_text(encoding="utf-8"))
    assert state["access_token"] == "access-secret"
    assert stored["refresh_token"] == "refresh-secret"
    assert "ABCD-EFGH" in err
    assert requests[0][1]["resource"] == "https://mcp.example/mcp"
    assert requests[-1][1]["grant_type"] == (
        "urn:ietf:params:oauth:grant-type:device_code"
    )


def test_granola_mcp_discovers_authorization_server_from_resource_metadata(
    monkeypatch,
) -> None:
    seen: dict[str, str] = {}

    def fake_get(url, *, context):
        seen["url"] = url
        return {
            "resource": "https://mcp.example/mcp",
            "authorization_servers": ["https://auth.example"],
        }

    monkeypatch.setattr(granola, "oauth_get_json", fake_get)

    issuer = granola.discover_mcp_authorization_server(
        mcp_url="https://mcp.example/mcp",
        preferred_issuer=granola.DEFAULT_MCP_OAUTH_ISSUER,
    )

    assert issuer == "https://auth.example"
    assert seen["url"] == (
        "https://mcp.example/.well-known/oauth-protected-resource/mcp"
    )


def test_granola_mcp_re_registers_client_when_issuer_changes(monkeypatch) -> None:
    granola.persist_mcp_oauth_state(
        {
            "authorization_server": "https://old-auth.example",
            "client_id": "old-client",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
        }
    )
    monkeypatch.setattr(
        granola,
        "oauth_get_json",
        lambda url, *, context: {
            "registration_endpoint": "https://new-auth.example/register",
            "device_authorization_endpoint": "https://new-auth.example/device",
            "token_endpoint": "https://new-auth.example/token",
        },
    )
    registrations: list[str] = []

    def fake_register(url, payload, *, context):
        registrations.append(url)
        return {"client_id": "new-client"}

    monkeypatch.setattr(granola, "oauth_post_json", fake_register)

    state = granola.ensure_mcp_oauth_client(issuer="https://new-auth.example")

    assert state["client_id"] == "new-client"
    assert "access_token" not in state
    assert "refresh_token" not in state
    assert registrations == ["https://new-auth.example/register"]


def test_granola_mcp_refresh_rotates_tokens(monkeypatch) -> None:
    granola.persist_mcp_oauth_state(
        {
            "client_id": "client-123",
            "token_endpoint": "https://auth.example/token",
            "access_token": "expired-access",
            "refresh_token": "old-refresh",
            "expires_at": 0,
        }
    )
    seen: dict[str, str] = {}

    def fake_post(url, payload, *, context):
        seen.update(payload)
        return {
            "access_token": "fresh-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }

    monkeypatch.setattr(granola, "oauth_post_form_json", fake_post)

    token = granola.ensure_mcp_access_token(mcp_url="https://mcp.example/mcp")

    stored = json.loads(granola.MCP_OAUTH_FILE.read_text(encoding="utf-8"))
    assert token == "fresh-access"
    assert stored["refresh_token"] == "new-refresh"
    assert seen == {
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh",
        "client_id": "client-123",
        "resource": "https://mcp.example/mcp",
    }


def test_granola_mcp_client_initializes_session_before_tool_call(
    monkeypatch,
) -> None:
    requests: list[dict[str, object]] = []

    def fake_request(**kwargs):
        requests.append(kwargs)
        method = kwargs["payload"]["method"]
        if method == "initialize":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"protocolVersion": granola.MCP_PROTOCOL_VERSION},
                },
                "session-123",
            )
        if method == "notifications/initialized":
            return None, "session-123"
        return (
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"content": [{"type": "text", "text": "<meetings_data />"}]},
            },
            "session-123",
        )

    monkeypatch.setattr(granola, "mcp_http_request", fake_request)

    result = granola.call_mcp_tool(
        "https://mcp.example/mcp",
        "access-secret",
        "list_meetings",
        {"time_range": "last_30_days"},
    )

    assert granola.mcp_tool_text(result) == "<meetings_data />"
    assert [request["payload"]["method"] for request in requests] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert requests[-1]["session_id"] == "session-123"
    assert requests[-1]["protocol_version"] == granola.MCP_PROTOCOL_VERSION


def test_granola_mcp_sse_response_parses_json_rpc_payload() -> None:
    payload = granola._parse_mcp_sse(
        b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
    )

    assert payload["result"] == {"ok": True}


def test_granola_mcp_notification_accepts_json_null_response() -> None:
    assert granola._parse_mcp_http_body(b"null", "application/json") is None


def test_granola_mcp_meeting_xml_normalizes_metadata_and_summary() -> None:
    text = """
<access_notice>Personal notes only.</access_notice>
<meetings_data from="Aug 11, 2026" to="Aug 11, 2026" count="1">
  <meeting id="11111111-1111-1111-1111-111111111111"
           title="Weekly Review"
           date="Aug 11, 2026 11:00 AM CDT"
           captured_by_me="true"
           listed_as_participant="true"
           is_workspace_visible="false">
    <known_participants>
      Person One (note creator) from Example Organization &lt;person.one@example.test&gt;,
      Person Two &lt;person.two@example.test&gt;
    </known_participants>
    <summary># Highlights

- Reviewed rollout steps.</summary>
  </meeting>
</meetings_data>
"""

    documents = granola.parse_mcp_meetings(text)

    assert len(documents) == 1
    assert documents[0]["created_at"] == "2026-08-11T16:00:00Z"
    assert documents[0]["notes_markdown"].startswith("# Highlights")
    assert documents[0]["captured_by_me"] is True
    assert granola.extract_people(documents[0]) == ["Person One", "Person Two"]
    assert documents[0]["_granola_api"] == "mcp"


def test_granola_mcp_custom_window_adds_timezone_boundary_margin() -> None:
    arguments = granola._mcp_list_arguments(
        created_after="2026-08-01T00:00:00Z",
        created_before="2026-08-11T23:59:59Z",
    )

    assert arguments == {
        "time_range": "custom",
        "custom_start": "2026-07-31",
        "custom_end": "2026-08-12",
    }


def test_granola_mcp_transcript_json_normalizes_speaker_blocks() -> None:
    payload = granola._mcp_transcript_payload(
        'Warning preamble\n{"id":"meeting-1","title":"Review",'
        '"transcript":"Them: First line\\ncontinued\\nMe: Second line"}'
    )
    segments = granola.normalize_mcp_transcript(
        "meeting-1",
        payload["transcript"],
    )

    assert [segment["speaker"]["name"] for segment in segments] == ["Them", "Me"]
    assert segments[0]["text"] == "First line\ncontinued"
    assert segments[1]["text"] == "Second line"


def test_granola_commands_use_mcp_oauth_before_legacy_session(
    monkeypatch, capsys
) -> None:
    _freeze_now(monkeypatch)
    granola.persist_mcp_oauth_state(
        {
            "client_id": "client-123",
            "access_token": "mcp-access-secret",
            "refresh_token": "mcp-refresh-secret",
            "expires_at": granola.time.time() + 3600,
        }
    )
    seen: dict[str, str] = {}

    def fake_fetch(mcp_url, token, limit=None, **kwargs):
        seen.update({"mcp_url": mcp_url, "token": token})
        return [_doc("11111111-1111-1111-1111-111111111111", "Weekly Review")]

    monkeypatch.setattr(granola, "fetch_mcp_documents", fake_fetch)
    monkeypatch.setattr(
        granola,
        "ensure_access_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy desktop session must not be read")
        ),
    )

    result = granola.main(
        [
            "--mcp-url",
            "https://mcp.example/mcp",
            "list",
            "--output",
            "summary",
        ]
    )

    out = capsys.readouterr().out
    assert result == 0
    assert seen == {
        "mcp_url": "https://mcp.example/mcp",
        "token": "mcp-access-secret",
    }
    assert "Weekly Review" in out


def test_granola_status_uses_mcp_oauth_without_exposing_tokens(
    monkeypatch, capsys
) -> None:
    granola.persist_mcp_oauth_state(
        {
            "client_id": "client-123",
            "access_token": "mcp-access-do-not-print",
            "refresh_token": "mcp-refresh-do-not-print",
            "expires_at": granola.time.time() + 3600,
        }
    )
    monkeypatch.setattr(
        granola,
        "fetch_mcp_documents",
        lambda mcp_url, token, limit=None, **kwargs: [
            _doc("11111111-1111-1111-1111-111111111111", "Weekly Review")
        ],
    )

    result = granola.main(["status"])

    out = capsys.readouterr().out
    assert result == 0
    assert "auth_mode\tmcp_oauth" in out
    assert "mcp_oauth_configured\ttrue" in out
    assert "session_status\tready" in out
    assert "mcp-access-do-not-print" not in out
    assert "mcp-refresh-do-not-print" not in out


def test_granola_auth_login_preflights_mcp_without_printing_tokens(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        granola,
        "run_mcp_oauth_login",
        lambda **kwargs: {
            "access_token": "mcp-access-do-not-print",
            "refresh_token": "mcp-refresh-do-not-print",
            "expires_at": granola.time.time() + 3600,
        },
    )
    monkeypatch.setattr(
        granola,
        "fetch_mcp_documents",
        lambda mcp_url, token, limit=None, **kwargs: [],
    )

    result = granola.main(["auth", "login", "--no-browser"])

    captured = capsys.readouterr()
    assert result == 0
    assert "status\tauthorized" in captured.out
    assert "refresh_token_present\ttrue" in captured.out
    assert "mcp-access-do-not-print" not in captured.out + captured.err
    assert "mcp-refresh-do-not-print" not in captured.out + captured.err


def test_granola_auth_login_handles_cancellation_without_traceback(
    monkeypatch, capsys
) -> None:
    def cancel_login(**_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(granola, "run_mcp_oauth_login", cancel_login)

    result = granola.main(["auth", "login", "--no-browser"])

    captured = capsys.readouterr()
    assert result == 130
    assert captured.out == ""
    assert captured.err == "Granola command canceled.\n"


def test_granola_auth_logout_preserves_registered_public_client(capsys) -> None:
    granola.persist_mcp_oauth_state(
        {
            "client_id": "client-123",
            "registration_endpoint": "https://auth.example/register",
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
        }
    )

    result = granola.main(["auth", "logout"])

    out = capsys.readouterr().out
    stored = json.loads(granola.MCP_OAUTH_FILE.read_text(encoding="utf-8"))
    assert result == 0
    assert "status\tlogged_out" in out
    assert stored["client_id"] == "client-123"
    assert "access_token" not in stored
    assert "refresh_token" not in stored
