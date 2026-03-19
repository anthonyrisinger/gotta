from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from gotta.plugins import granola


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


def test_granola_parser_defaults() -> None:
    assert granola.build_parser().parse_args(["status"]).output == "summary"
    assert granola.build_parser().parse_args(["list"]).output == "markdown"
    assert granola.build_parser().parse_args(["list"]).sort == "created"
    assert granola.build_parser().parse_args(["list"]).order == "desc"
    assert granola.build_parser().parse_args(["list"]).offset == 0
    assert granola.build_parser().parse_args(["list"]).time_range == "last_90_days"
    assert granola.build_parser().parse_args(["search", "needle"]).mode == "auto"
    assert granola.build_parser().parse_args(["search", "needle"]).time_range == "last_90_days"
    assert granola.build_parser().parse_args(["get", "doc-1"]).output == "markdown"
    assert granola.build_parser().parse_args(["transcript", "doc-1"]).output == "markdown"
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
    assert "- Transcript Locator: `granola:transcript 11111111-1111-1111-1111-111111111111`" in out
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


def test_granola_transcript_markdown_includes_segment_metadata(monkeypatch, capsys) -> None:
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


def test_granola_transcript_summary_prints_id_count_and_title(monkeypatch, capsys) -> None:
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
    assert out.strip() == (
        "11111111-1111-1111-1111-111111111111\t2\tWeekly Review"
    )


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
        ["list", "--sort", "created", "--order", "asc", "--offset", "1", "--limit", "1", "--output", "summary"]
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "33333333-3333-3333-3333-333333333333\tMiddle Created" in out
    assert "Oldest Created" not in out
    assert "Newest Created" not in out


def test_granola_export_writes_only_notes_with_body(monkeypatch, capsys, tmp_path: Path) -> None:
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
        ["list", "--after", "2026-03-01", "--before", "2026-03-31", "--output", "summary"]
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "March Note" in out
    assert "February Note" not in out


def test_granola_list_window_uses_created_time_not_recent_activity(monkeypatch, capsys) -> None:
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
            ["transcript", "11111111-1111-1111-1111-111111111111", "--query", "latency"],
            options,
        )
        == "11111111-1111-1111-1111-111111111111-transcript-query-latency.md"
    )


def test_granola_search_transcript_defaults_to_bounded_window(monkeypatch, capsys) -> None:
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


def test_granola_export_supports_bounded_sort_and_offset(monkeypatch, capsys, tmp_path: Path) -> None:
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
