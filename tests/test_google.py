from __future__ import annotations

import base64
import json
import urllib.parse
import zlib

import pytest

from gotta.plugins import gdocs, gdrive, gsheets
from gotta.providers import google


def test_parse_doc_ref_accepts_document_url_and_raw_id() -> None:
    assert google.parse_doc_ref("doc-123") == (
        "doc-123",
        "https://docs.google.com/document/d/doc-123/edit",
    )
    assert google.parse_doc_ref("https://docs.google.com/document/d/doc-123/edit") == (
        "doc-123",
        "https://docs.google.com/document/d/doc-123/edit",
    )


def test_parse_doc_ref_rejects_non_document_urls() -> None:
    with pytest.raises(google.GoogleError, match="could not parse Google Docs"):
        google.parse_doc_ref("https://drive.google.com/file/d/drive-123/view")


def test_parse_drive_ref_accepts_drive_and_docs_urls() -> None:
    assert google.parse_drive_ref("drive-123") == (
        "drive-123",
        "https://drive.google.com/open?id=drive-123",
    )
    assert google.parse_drive_ref("https://drive.google.com/file/d/drive-123/view") == (
        "drive-123",
        "https://drive.google.com/file/d/drive-123/view",
    )
    assert google.parse_drive_ref("https://drive.google.com/open?id=drive-123") == (
        "drive-123",
        "https://drive.google.com/open?id=drive-123",
    )
    assert google.parse_drive_ref(
        "https://docs.google.com/spreadsheets/d/sheet-123/edit"
    ) == (
        "sheet-123",
        "https://docs.google.com/spreadsheets/d/sheet-123/edit",
    )


def test_parse_sheet_ref_accepts_sheet_url_and_raw_id() -> None:
    assert google.parse_sheet_ref("sheet-123") == (
        "sheet-123",
        "https://docs.google.com/spreadsheets/d/sheet-123/edit",
    )
    assert google.parse_sheet_ref(
        "https://docs.google.com/spreadsheets/d/sheet-123/edit"
    ) == (
        "sheet-123",
        "https://docs.google.com/spreadsheets/d/sheet-123/edit",
    )


def test_parse_drive_ref_rejects_unstructured_input() -> None:
    with pytest.raises(google.GoogleError, match="could not parse Google Drive"):
        google.parse_drive_ref("not a valid drive ref")


def test_drive_file_meta_includes_shared_drive_support(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_google_json(url: str, _access_token: str) -> dict[str, object]:
        captured["url"] = url
        return {"id": "file-123"}

    monkeypatch.setattr(google, "google_json", fake_google_json)

    payload = google.drive_file_meta("token", "file-123", fields="id,name")

    parsed = urllib.parse.urlparse(captured["url"])
    query = urllib.parse.parse_qs(parsed.query)
    assert payload == {"id": "file-123"}
    assert parsed.path.endswith("/file-123")
    assert query["fields"] == ["id,name"]
    assert query["supportsAllDrives"] == ["true"]


def test_drive_export_uses_only_documented_parameters(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_google_bytes(url: str, _access_token: str) -> bytes:
        captured["url"] = url
        return b"<p>Generic document</p>"

    monkeypatch.setattr(google, "google_bytes", fake_google_bytes)

    payload = google.drive_export("token", "doc-123", "text/html")

    parsed = urllib.parse.urlparse(captured["url"])
    query = urllib.parse.parse_qs(parsed.query)
    assert payload == b"<p>Generic document</p>"
    assert parsed.path.endswith("/doc-123/export")
    assert query["mimeType"] == ["text/html"]
    assert "supportsAllDrives" not in query


def test_drive_download_bytes_includes_shared_drive_support(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_google_bytes(url: str, _access_token: str) -> bytes:
        captured["url"] = url
        return b"generic-bytes"

    monkeypatch.setattr(google, "google_bytes", fake_google_bytes)

    payload = google.drive_download_bytes("token", "file-123")

    parsed = urllib.parse.urlparse(captured["url"])
    query = urllib.parse.parse_qs(parsed.query)
    assert payload == b"generic-bytes"
    assert parsed.path.endswith("/file-123")
    assert query["alt"] == ["media"]
    assert query["supportsAllDrives"] == ["true"]


def test_drive_search_files_includes_shared_drive_flags(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_google_json(url: str, _access_token: str) -> dict[str, object]:
        captured["url"] = url
        return {"files": []}

    monkeypatch.setattr(google, "google_json", fake_google_json)

    payload = google.drive_search_files(
        "token", "name contains 'generic'", limit=7, fields="id,name"
    )

    parsed = urllib.parse.urlparse(captured["url"])
    query = urllib.parse.parse_qs(parsed.query)
    assert payload == []
    assert parsed.path.endswith("/files")
    assert query["q"] == ["name contains 'generic'"]
    assert query["pageSize"] == ["7"]
    assert query["supportsAllDrives"] == ["true"]
    assert query["includeItemsFromAllDrives"] == ["true"]
    assert "corpora" not in query


def test_google_status_payload_reports_missing_session(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(google, "TOKEN_FILE", tmp_path / "oauth.json")
    monkeypatch.setattr(google, "CONFIG_FILE", tmp_path / "gotta.toml")
    monkeypatch.setattr(google, "load_google_config_env", lambda: {})

    payload = google.google_status_payload()

    assert payload["sessionStatus"] == "missing"
    assert not payload["credentialsConfigured"]


def test_google_status_payload_prefers_native_refresh_before_auth(monkeypatch) -> None:
    monkeypatch.setattr(google, "google_credentials_present", lambda: True)
    monkeypatch.setattr(google, "load_google_config_env", lambda: {})
    monkeypatch.setattr(
        google,
        "load_cached_oauth_state",
        lambda: {
            "access_token": "expired-token",
            "refresh_token": "refresh-token",
            "expires_at": 0.0,
        },
    )

    payload = google.google_status_payload()

    assert payload["sessionStatus"] == "expired"
    assert (
        payload["nextStep"]
        == "rerun a native Google command first; gotta will usually refresh automatically "
        "when a refresh token is present. Re-run auth only if refresh does not recover "
        "the session"
    )


def test_ensure_google_session_distinguishes_missing_session_from_missing_credentials(
    monkeypatch,
) -> None:
    monkeypatch.setattr(google, "load_cached_oauth_state", lambda: None)
    monkeypatch.setattr(google, "google_credentials_present", lambda: True)

    with pytest.raises(google.GoogleError, match="no cached Google OAuth session"):
        google.ensure_google_session(
            allow_bootstrap=False,
            interactive_ok=False,
            auth_command="gdocs",
        )


def test_google_missing_credentials_message_points_to_gotta_config(monkeypatch) -> None:
    monkeypatch.delenv("GOTTA_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOTTA_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(google, "load_google_config_env", lambda: {})

    with pytest.raises(
        google.GoogleError, match=r"\[providers\.google\.env\].*gotta\.toml"
    ):
        google.load_oauth_runtime_config()


def test_load_cached_oauth_state_recovers_single_object_with_extra_closing_brace(
    tmp_path, monkeypatch
) -> None:
    token_file = tmp_path / "oauth.json"
    monkeypatch.setattr(google, "TOKEN_FILE", token_file)
    monkeypatch.setattr(google, "OAUTH_DIR", tmp_path)
    payload = {
        "access_token": "token",
        "refresh_token": "refresh",
        "expires_at": 123.0,
    }
    token_file.write_text(json.dumps(payload) + "}\n", encoding="utf-8")

    recovered = google.load_cached_oauth_state()

    assert recovered == payload
    assert json.loads(token_file.read_text(encoding="utf-8")) == payload


def test_load_cached_oauth_state_rejects_ambiguous_malformed_json(
    tmp_path, monkeypatch
) -> None:
    token_file = tmp_path / "oauth.json"
    monkeypatch.setattr(google, "TOKEN_FILE", token_file)
    token_file.write_text(
        '{"access_token":"one"}{"access_token":"two"}', encoding="utf-8"
    )

    with pytest.raises(google.GoogleError, match="invalid Google OAuth state file"):
        google.load_cached_oauth_state()


def test_google_doc_and_drive_get_default_to_markdown() -> None:
    assert gdocs.build_parser().parse_args(["get", "doc-123"]).output == "markdown"
    assert gdrive.build_parser().parse_args(["get", "file-123"]).output == "markdown"
    assert gsheets.build_parser().parse_args(["get", "sheet-123"]).output == "markdown"


def test_gdrive_preferred_name_for_get_is_canonical_not_projection_shaped() -> None:
    options = type("Options", (), {"save_as": ""})()

    assert (
        gdrive.preferred_name(["get", "--output", "markdown", "file-123"], options)
        == "file-123.bin"
    )


def test_gdrive_capture_name_uses_truthful_suffixes_for_binary_content() -> None:
    assert (
        gdrive._capture_name("file-123", {"name": "Quarterly Deck"}, "application/pdf")
        == "Quarterly Deck.pdf"
    )
    assert (
        gdrive._capture_name(
            "file-123", {"name": "Quarterly Deck.pdf"}, "application/pdf"
        )
        == "Quarterly Deck.pdf"
    )


def test_gdocs_capture_normalizes_google_redirect_wrappers(monkeypatch) -> None:
    monkeypatch.setattr(
        gdocs, "ensure_google_session", lambda **kwargs: {"access_token": "token"}
    )
    monkeypatch.setattr(
        gdocs,
        "document_meta",
        lambda _token, _doc_id: {
            "title": "Doc",
            "url": "https://docs.google.com/document/d/doc-123/edit",
        },
    )
    monkeypatch.setattr(
        gdocs, "document_json", lambda _token, _doc_id: {"documentId": "doc-123"}
    )
    monkeypatch.setattr(
        gdocs,
        "drive_export",
        lambda _token, _doc_id, _mime: (
            b'<a href="https://www.google.com/url?q=https://drive.google.com/file/d/file-123/view?ts%3D123'
            b'&amp;sa=D&amp;source=editors&amp;ust=999&amp;usg=abc">Diagram</a>'
        ),
    )

    capture = gdocs.capture(["get", "doc-123"], object())

    text = capture.data.decode("utf-8")
    assert "https://drive.google.com/file/d/file-123/view?ts=123" in text
    assert "https://www.google.com/url?" not in text
    assert "ust=" not in text
    assert "usg=" not in text


def test_gdocs_capture_normalizes_google_redirect_wrappers_without_www(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        gdocs, "ensure_google_session", lambda **kwargs: {"access_token": "token"}
    )
    monkeypatch.setattr(
        gdocs,
        "document_meta",
        lambda _token, _doc_id: {
            "title": "Doc",
            "url": "https://docs.google.com/document/d/doc-123/edit",
        },
    )
    monkeypatch.setattr(
        gdocs, "document_json", lambda _token, _doc_id: {"documentId": "doc-123"}
    )
    monkeypatch.setattr(
        gdocs,
        "drive_export",
        lambda _token, _doc_id, _mime: (
            b'<a href="https://google.com/url?q=https://drive.google.com/file/d/file-123/view?ts%3D123'
            b'&amp;sa=D&amp;source=editors&amp;ust=999&amp;usg=abc">Diagram</a>'
        ),
    )

    capture = gdocs.capture(["get", "doc-123"], object())

    text = capture.data.decode("utf-8")
    assert "https://drive.google.com/file/d/file-123/view?ts=123" in text
    assert "https://google.com/url?" not in text
    assert "ust=" not in text
    assert "usg=" not in text


def test_google_status_defaults_to_summary() -> None:
    assert gdocs.build_parser().parse_args(["status"]).output == "summary"
    assert (
        gdocs.build_parser().parse_args(["status", "--output", "summary"]).output
        == "summary"
    )
    assert gdrive.build_parser().parse_args(["status"]).output == "summary"
    assert (
        gdrive.build_parser().parse_args(["status", "--output", "summary"]).output
        == "summary"
    )
    assert gsheets.build_parser().parse_args(["status"]).output == "summary"
    assert (
        gsheets.build_parser().parse_args(["status", "--output", "summary"]).output
        == "summary"
    )


def test_gdocs_search_reports_gdocs_as_source(monkeypatch) -> None:
    monkeypatch.setattr(
        gdocs, "ensure_google_session", lambda **kwargs: {"access_token": "token"}
    )
    monkeypatch.setattr(
        gdocs,
        "drive_search_files",
        lambda *args, **kwargs: [
            {
                "id": "doc-123",
                "name": "Doc",
                "webViewLink": "https://docs.google.com/document/d/doc-123/edit",
            }
        ],
    )

    payload = gdocs.search_documents("token", "needle", mode="title", limit=5)

    assert payload["source"] == "gdocs"


def test_gdrive_search_reports_google_drive_as_source(monkeypatch) -> None:
    monkeypatch.setattr(
        gdrive, "ensure_google_session", lambda **kwargs: {"access_token": "token"}
    )
    monkeypatch.setattr(
        gdrive,
        "drive_search_files",
        lambda *args, **kwargs: [
            {
                "id": "file-123",
                "name": "Generic File",
                "webViewLink": "https://drive.google.com/file/d/file-123/view",
            }
        ],
    )

    payload = gdrive.search_files(
        "token", "generic", mode="title", limit=5, mime_type=""
    )

    assert payload["source"] == "google-drive"


def test_gdocs_search_markdown_uses_docs_specific_heading() -> None:
    rendered = gdocs.render_search_markdown(
        {
            "query": "needle",
            "mode": "auto",
            "resultCount": 1,
            "results": [
                {
                    "title": "Doc",
                    "url": "https://docs.google.com/document/d/doc-123/edit",
                    "matchedBy": ["title"],
                }
            ],
        }
    )

    assert "### Google Docs Search: needle" in rendered
    assert "- _Surface_: `gdocs`" in rendered


def test_document_meta_merges_drive_created_and_modified(monkeypatch) -> None:
    monkeypatch.setattr(
        gdocs,
        "google_json",
        lambda url, access_token: {
            "documentId": "doc-123",
            "title": "Doc",
            "revisionId": "rev-1",
        },
    )
    monkeypatch.setattr(
        gdocs,
        "drive_file_meta",
        lambda access_token, doc_id, fields: {
            "createdTime": "2026-01-08T17:19:19.520Z",
            "modifiedTime": "2026-01-08T18:49:54.309Z",
            "webViewLink": "https://docs.google.com/document/d/doc-123/edit",
        },
    )

    payload = gdocs.document_meta("token", "doc-123")

    assert payload["createdTime"] == "2026-01-08T17:19:19.520Z"
    assert payload["modifiedTime"] == "2026-01-08T18:49:54.309Z"


def test_gdocs_search_markdown_includes_created_and_updated() -> None:
    rendered = gdocs.render_search_markdown(
        {
            "query": "needle",
            "mode": "auto",
            "resultCount": 1,
            "results": [
                {
                    "title": "Doc",
                    "url": "https://docs.google.com/document/d/doc-123/edit",
                    "matchedBy": ["title"],
                    "createdTime": "2026-01-08T17:19:19.520Z",
                    "modifiedTime": "2026-01-08T18:49:54.309Z",
                }
            ],
        }
    )

    assert "- Created: 2026-01-08T17:19:19.520Z" in rendered
    assert "- Updated: 2026-01-08T18:49:54.309Z" in rendered
    assert "created `2026-01-08T17:19:19.520Z`" in rendered
    assert "modified `2026-01-08T18:49:54.309Z`" in rendered


def test_gdocs_get_markdown_includes_metadata_header(monkeypatch, capsys) -> None:
    monkeypatch.setattr(gdocs, "is_interactive", lambda: False)
    monkeypatch.setattr(
        gdocs,
        "ensure_google_session",
        lambda **kwargs: {"access_token": "token"},
    )
    monkeypatch.setattr(gdocs, "parse_doc_ref", lambda ref: ("doc-123", ""))
    monkeypatch.setattr(
        gdocs,
        "document_meta",
        lambda access_token, doc_id: {
            "url": "https://docs.google.com/document/d/doc-123/edit",
            "createdTime": "2026-01-08T17:19:19.520Z",
            "modifiedTime": "2026-01-08T18:49:54.309Z",
            "revisionId": "rev-1",
            "owners": [{"displayName": "Alex Example"}],
        },
    )
    monkeypatch.setattr(
        gdocs, "drive_export", lambda access_token, doc_id, _mime: b"# Body\n"
    )

    result = gdocs.main(["get", "doc-123", "--output", "markdown"])

    captured = capsys.readouterr()
    assert result == 0
    assert "- Created: 2026-01-08T17:19:19.520Z" in captured.out
    assert "- Updated: 2026-01-08T18:49:54.309Z" in captured.out
    assert "- Revision: rev-1" in captured.out
    assert "# Body" in captured.out


def test_gdrive_get_reads_textlike_content_from_accessible_shared_drive_file(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(gdrive, "is_interactive", lambda: False)
    monkeypatch.setattr(
        gdrive,
        "ensure_google_session",
        lambda **kwargs: {"access_token": "token"},
    )
    monkeypatch.setattr(
        gdrive,
        "drive_file_meta",
        lambda access_token, file_id, fields: {
            "id": file_id,
            "name": "Generic Notes",
            "mimeType": "text/plain",
            "webViewLink": "https://drive.google.com/file/d/file-123/view",
        },
    )
    monkeypatch.setattr(
        gdrive, "drive_download_bytes", lambda access_token, file_id: b"generic body\n"
    )

    result = gdrive.main(["get", "file-123", "--output", "markdown"])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == "generic body\n"


def test_gsheets_search_reports_gsheets_as_source(monkeypatch) -> None:
    monkeypatch.setattr(
        gsheets,
        "drive_search_files",
        lambda *args, **kwargs: [
            {
                "id": "sheet-123",
                "name": "Sheet",
                "webViewLink": "https://docs.google.com/spreadsheets/d/sheet-123/edit",
                "mimeType": google.GOOGLE_SHEET_MIME,
            }
        ],
    )

    payload = gsheets.search_spreadsheets("token", "needle", mode="title", limit=5)

    assert payload["source"] == "gsheets"


def test_gsheets_search_markdown_uses_sheets_specific_heading() -> None:
    rendered = gsheets.render_search_markdown(
        {
            "query": "needle",
            "mode": "auto",
            "resultCount": 1,
            "results": [
                {
                    "title": "Sheet",
                    "url": "https://docs.google.com/spreadsheets/d/sheet-123/edit",
                    "matchedBy": ["title"],
                }
            ],
        }
    )

    assert "### Google Sheets Search: needle" in rendered
    assert "- _Surface_: `gsheets`" in rendered


def test_gsheets_search_markdown_includes_time_envelope() -> None:
    rendered = gsheets.render_search_markdown(
        {
            "query": "needle",
            "mode": "auto",
            "resultCount": 1,
            "results": [
                {
                    "title": "Sheet",
                    "url": "https://docs.google.com/spreadsheets/d/sheet-123/edit",
                    "matchedBy": ["title"],
                    "createdTime": "2026-01-08T17:19:19.520Z",
                    "modifiedTime": "2026-01-08T18:49:54.309Z",
                }
            ],
        }
    )

    assert "- Created: 2026-01-08T17:19:19.520Z" in rendered
    assert "- Updated: 2026-01-08T18:49:54.309Z" in rendered


def test_gsheets_summary_includes_created_and_modified() -> None:
    rendered = gsheets.spreadsheet_summary(
        {
            "spreadsheetId": "sheet-123",
            "url": "https://docs.google.com/spreadsheets/d/sheet-123/edit",
            "createdTime": "2026-01-08T17:19:19.520Z",
            "modifiedTime": "2026-01-08T18:49:54.309Z",
            "properties": {"title": "Sheet"},
            "sheets": [{"properties": {"title": "Tab 1"}}],
        }
    )

    assert "- **Created:** 2026-01-08T17:19:19.520Z" in rendered
    assert "- **Modified:** 2026-01-08T18:49:54.309Z" in rendered


def test_gsheets_build_preview_bundle_merges_drive_metadata_for_accessible_shared_drive_sheet(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        gsheets,
        "sheets_spreadsheet_meta",
        lambda access_token, spreadsheet_id: {
            "spreadsheetId": spreadsheet_id,
            "properties": {"title": "Generic Sheet"},
            "sheets": [
                {
                    "properties": {
                        "title": "Overview",
                        "gridProperties": {"rowCount": 4, "columnCount": 2},
                    }
                }
            ],
        },
    )
    monkeypatch.setattr(
        gsheets,
        "drive_file_meta",
        lambda access_token, spreadsheet_id, fields: {
            "createdTime": "2026-01-08T17:19:19.520Z",
            "modifiedTime": "2026-01-08T18:49:54.309Z",
            "webViewLink": "https://docs.google.com/spreadsheets/d/sheet-123/edit",
            "owners": [{"displayName": "Morgan Example"}],
        },
    )
    monkeypatch.setattr(
        gsheets,
        "sheets_values",
        lambda access_token, spreadsheet_id, a1_range: {
            "range": "Overview!A1:B4",
            "values": [["Name", "Value"], ["Status", "Open"]],
        },
    )

    bundle = gsheets.build_preview_bundle(
        "token",
        "sheet-123",
        sheet_name="",
        a1_range=None,
        rows=4,
        cols=2,
    )

    assert bundle["url"] == "https://docs.google.com/spreadsheets/d/sheet-123/edit"
    assert bundle["meta"]["createdTime"] == "2026-01-08T17:19:19.520Z"
    assert bundle["meta"]["modifiedTime"] == "2026-01-08T18:49:54.309Z"
    assert bundle["meta"]["owners"] == [{"displayName": "Morgan Example"}]
    assert bundle["previews"][0]["range"] == "Overview!A1:B4"


def test_gdrive_summary_includes_created_and_modified() -> None:
    rendered = gdrive.drive_file_summary(
        {
            "id": "file-123",
            "name": "File",
            "createdTime": "2026-01-08T17:19:19.520Z",
            "modifiedTime": "2026-01-08T18:49:54.309Z",
        }
    )

    assert "- **Created:** 2026-01-08T17:19:19.520Z" in rendered
    assert "- **Modified:** 2026-01-08T18:49:54.309Z" in rendered


def test_gdrive_project_summarizes_drawio_mxfile() -> None:
    model = (
        "<mxGraphModel><root>"
        '<mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="a" value="Generic Start" vertex="1" parent="1"/>'
        '<mxCell id="b" value="Generic Finish" vertex="1" parent="1"/>'
        '<mxCell id="e1" edge="1" source="a" target="b" parent="1"/>'
        "</root></mxGraphModel>"
    )
    compressed = base64.b64encode(
        zlib.compress(urllib.parse.quote(model).encode("utf-8"))[2:-4]
    ).decode("ascii")
    mxfile = (
        '<mxfile><diagram id="page-1" name="Generic Page">'
        f"{compressed}"
        "</diagram></mxfile>"
    ).encode("utf-8")
    capture = gdrive.Capture(
        data=mxfile,
        name="diagram.mxfile",
        type="application/vnd.jgraph.mxfile",
        meta={
            "projector": "gdrive",
            "file_id": "file-123",
            "source_title": "Generic Diagram",
            "source_url": "https://drive.google.com/file/d/file-123/view",
            "source_mime": "application/vnd.jgraph.mxfile",
        },
        view={
            "meta": {
                "id": "file-123",
                "name": "Generic Diagram",
                "mimeType": "application/vnd.jgraph.mxfile",
            }
        },
    )

    rendered = gdrive.project(
        ["get", "--output", "markdown", "file-123"], capture
    ).decode("utf-8")

    assert "# Generic Diagram" in rendered
    assert "- **Pages:** 1" in rendered
    assert "Generic Page: 2 nodes, 1 edges" in rendered
    assert "Generic Start" in rendered
    assert "Generic Finish" in rendered


def test_gdrive_search_markdown_includes_time_envelope() -> None:
    rendered = gdrive.render_search_markdown(
        {
            "query": "needle",
            "mode": "auto",
            "resultCount": 1,
            "results": [
                {
                    "title": "File",
                    "url": "https://drive.google.com/file/d/file-123/view",
                    "matchedBy": ["title"],
                    "createdTime": "2026-01-08T17:19:19.520Z",
                    "modifiedTime": "2026-01-08T18:49:54.309Z",
                }
            ],
        }
    )

    assert "- Created: 2026-01-08T17:19:19.520Z" in rendered
    assert "- Updated: 2026-01-08T18:49:54.309Z" in rendered


def test_gdocs_auth_prefers_reuse_or_refresh(monkeypatch, capsys) -> None:
    monkeypatch.setattr(gdocs, "is_interactive", lambda: True)
    monkeypatch.setattr(
        gdocs,
        "ensure_google_session",
        lambda **kwargs: {"access_token": "token", "expires_at": 123.0},
    )
    monkeypatch.setattr(
        gdocs,
        "run_oauth_bootstrap",
        lambda **kwargs: pytest.fail("full bootstrap should not run by default"),
    )

    result = gdocs.main(["auth"])

    captured = capsys.readouterr()
    assert result == 0
    assert '"authenticated": true' in captured.out
    assert '"expires_at": 123.0' in captured.out
    assert '"token_file":' in captured.out


def test_gdocs_auth_full_forces_browser_bootstrap(monkeypatch, capsys) -> None:
    monkeypatch.setattr(gdocs, "is_interactive", lambda: True)
    monkeypatch.setattr(
        gdocs,
        "ensure_google_session",
        lambda **kwargs: pytest.fail(
            "default refresh path should be bypassed with --full"
        ),
    )
    monkeypatch.setattr(
        gdocs,
        "run_oauth_bootstrap",
        lambda **kwargs: {"access_token": "token", "expires_at": 456.0},
    )

    result = gdocs.main(["auth", "--full"])

    captured = capsys.readouterr()
    assert result == 0
    assert '"authenticated": true' in captured.out
    assert '"expires_at": 456.0' in captured.out
    assert '"token_file":' in captured.out


def test_gdrive_auth_prefers_reuse_or_refresh(monkeypatch, capsys) -> None:
    monkeypatch.setattr(gdrive, "is_interactive", lambda: True)
    monkeypatch.setattr(
        gdrive,
        "ensure_google_session",
        lambda **kwargs: {"access_token": "token", "expires_at": 123.0},
    )
    monkeypatch.setattr(
        gdrive,
        "run_oauth_bootstrap",
        lambda **kwargs: pytest.fail("full bootstrap should not run by default"),
    )

    result = gdrive.main(["auth"])

    captured = capsys.readouterr()
    assert result == 0
    assert '"authenticated": true' in captured.out
    assert '"expires_at": 123.0' in captured.out
    assert '"token_file":' in captured.out


def test_gdrive_auth_full_forces_browser_bootstrap(monkeypatch, capsys) -> None:
    monkeypatch.setattr(gdrive, "is_interactive", lambda: True)
    monkeypatch.setattr(
        gdrive,
        "ensure_google_session",
        lambda **kwargs: pytest.fail(
            "default refresh path should be bypassed with --full"
        ),
    )
    monkeypatch.setattr(
        gdrive,
        "run_oauth_bootstrap",
        lambda **kwargs: {"access_token": "token", "expires_at": 456.0},
    )

    result = gdrive.main(["auth", "--full"])

    captured = capsys.readouterr()
    assert result == 0
    assert '"authenticated": true' in captured.out
    assert '"expires_at": 456.0' in captured.out
    assert '"token_file":' in captured.out
