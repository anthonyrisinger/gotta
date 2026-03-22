from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import datetime as dt
import json

import pytest

from gotta.plugins import slack
from gotta.providers import slack as slack_provider


def test_slack_status_defaults_to_summary() -> None:
    assert slack.build_parser().parse_args(["status"]).output == "summary"
    assert slack.build_parser().parse_args(["status", "--output", "summary"]).output == "summary"


def test_sqlite_identifier_quotes_embedded_quotes() -> None:
    assert slack.sqlite_identifier('a"b') == '"a""b"'


def test_cmd_schema_supports_directory_db(monkeypatch, tmp_path: Path, capsys) -> None:
    workspace = "demo"
    directory_db = tmp_path / "_directory.sqlite"
    conn = sqlite3.connect(directory_db)
    try:
        conn.execute('CREATE TABLE "M.SESSION_ID" ("value" TEXT)')
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)
    monkeypatch.setattr(slack, "directory_db_path", lambda workspace: directory_db)

    code = slack.main(
        [
            "schema",
            "--workspace",
            workspace,
            "--database",
            "directory",
            "--table",
            "M.SESSION_ID",
            "--output",
            "json",
        ]
    )

    assert code == 0
    assert '"name": "M.SESSION_ID"' in capsys.readouterr().out


def test_cmd_schema_degrades_per_view_instead_of_crashing(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    workspace = "demo"
    archive_db = tmp_path / "slackdump.sqlite"
    conn = sqlite3.connect(archive_db)
    try:
        conn.execute('CREATE TABLE "MESSAGE" ("id" TEXT)')
        conn.execute('CREATE VIEW "V_BROKEN" AS SELECT M.SESSION_ID FROM MESSAGE AS M')
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)

    class Result:
        db_path = archive_db

    monkeypatch.setattr(slack, "workspace_archive_result", lambda workspace: Result())
    monkeypatch.setattr(slack, "ensure_archive_exists", lambda result, description: result)

    code = slack.main(
        [
            "schema",
            "--workspace",
            workspace,
            "--database",
            "archive",
            "--output",
            "json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    by_name = {table["name"]: table for table in payload["tables"]}
    assert by_name["MESSAGE"]["error"] == ""
    assert "no such column: M.SESSION_ID" in by_name["V_BROKEN"]["error"]


def test_list_channels_defaults_to_bounded_json_page(monkeypatch, tmp_path: Path, capsys) -> None:
    workspace = "demo"
    directory_db = tmp_path / "_directory.sqlite"
    conn = sqlite3.connect(directory_db)
    try:
        conn.execute(
            """
            CREATE TABLE channel_directory (
                id TEXT,
                name TEXT,
                type TEXT,
                is_private INTEGER,
                is_archived INTEGER,
                lookup_name TEXT,
                raw_json TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO channel_directory (id, name, type, is_private, is_archived, lookup_name, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "C001",
                    "alpha",
                    "public_channel",
                    0,
                    0,
                    "alpha",
                    json.dumps({"id": "C001", "name": "alpha", "is_private": False, "is_archived": False}),
                ),
                (
                    "C002",
                    "bravo",
                    "public_channel",
                    0,
                    0,
                    "bravo",
                    json.dumps({"id": "C002", "name": "bravo", "is_private": False, "is_archived": False}),
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    def open_db(_workspace: str) -> sqlite3.Connection:
        conn = sqlite3.connect(directory_db)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)
    monkeypatch.setattr(slack, "seed_channel_directory_from_archive", lambda workspace: None)
    monkeypatch.setattr(slack, "open_directory_db", open_db)

    assert slack.main(["list-channels", "--workspace", workspace, "--limit", "1", "--output", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["totalCount"] == 2
    assert payload["shownCount"] == 1
    assert payload["offset"] == 0
    assert payload["nextOffset"] == 1
    assert payload["truncated"] is True
    assert [item["id"] for item in payload["results"]] == ["C001"]


def test_list_users_supports_offset_paging(monkeypatch, tmp_path: Path, capsys) -> None:
    workspace = "demo"
    directory_db = tmp_path / "_directory.sqlite"
    conn = sqlite3.connect(directory_db)
    try:
        conn.execute(
            """
            CREATE TABLE user_directory (
                id TEXT,
                display_name TEXT,
                username TEXT,
                lookup_name TEXT,
                raw_json TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO user_directory (id, display_name, username, lookup_name, raw_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    "U001",
                    "Alpha User",
                    "alpha",
                    "alpha user",
                    json.dumps({"id": "U001", "profile": {"display_name": "Alpha User"}, "name": "alpha"}),
                ),
                (
                    "U002",
                    "Bravo User",
                    "bravo",
                    "bravo user",
                    json.dumps({"id": "U002", "profile": {"display_name": "Bravo User"}, "name": "bravo"}),
                ),
                (
                    "U003",
                    "Charlie User",
                    "charlie",
                    "charlie user",
                    json.dumps({"id": "U003", "profile": {"display_name": "Charlie User"}, "name": "charlie"}),
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    def open_db(_workspace: str) -> sqlite3.Connection:
        conn = sqlite3.connect(directory_db)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)
    monkeypatch.setattr(slack, "seed_user_directory_from_archive", lambda workspace: None)
    monkeypatch.setattr(slack, "open_directory_db", open_db)

    assert (
        slack.main(
            [
                "list-users",
                "--workspace",
                workspace,
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
    assert payload["totalCount"] == 3
    assert payload["shownCount"] == 1
    assert payload["offset"] == 1
    assert payload["nextOffset"] == 2
    assert payload["truncated"] is True
    assert [item["id"] for item in payload["results"]] == ["U002"]


def test_missing_workspace_is_explicit() -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(slack_provider, "default_workspace", lambda: "")
        monkeypatch.setattr(slack_provider, "known_workspaces", lambda: [])
        with pytest.raises(slack_provider.SlackError, match="missing Slack workspace"):
            slack_provider.ensure_workspace_auth("", interactive_ok=False)


def test_default_workspace_reads_gotta_config(monkeypatch) -> None:
    monkeypatch.setattr(
        slack_provider,
        "load_slack_config_env",
        lambda: {slack_provider.SLACK_WORKSPACE_ENV: "demo"},
    )
    assert slack_provider.default_workspace() == "demo"


def test_resolve_workspace_uses_single_known_workspace(monkeypatch) -> None:
    monkeypatch.setattr(slack_provider, "default_workspace", lambda: "")
    monkeypatch.setattr(slack_provider, "known_workspaces", lambda: ["demo"])
    assert slack_provider.resolve_workspace("") == "demo"


def test_slack_mcp_help_passthrough_does_not_require_workspace(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(slack, "ensure_slackdump", lambda: None)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(slack.subprocess, "run", fake_run)

    code = slack.main(["mcp", "--", "--help"])

    assert code == 0
    assert calls == [["slackdump", "mcp", "--help"]]
    assert capsys.readouterr().err == ""


def test_slack_get_defaults_to_markdown() -> None:
    assert slack.build_parser().parse_args(["get", "C12345678"]).output == "markdown"


def test_resolve_slack_ref_prefers_thread_ts_query_over_reply_permalink_ts() -> None:
    ref = slack.resolve_slack_ref(
        "https://demo.slack.com/archives/C12345678/p1773081279142849?thread_ts=1773075428.384009",
        workspace="demo",
    )

    assert ref.kind == "thread"
    assert ref.channel_id == "C12345678"
    assert ref.thread_ts == "1773075428.384009"
    assert ref.url == "https://demo.slack.com/archives/C12345678/p1773075428384009"


def test_render_markdown_never_degrades_channel_label_to_bare_hash() -> None:
    envelope = {
        "workspace": "demo",
        "ref": {
            "kind": "thread",
            "channelId": "",
            "threadTs": "1772819749.912029",
            "url": "https://demo.slack.com/archives/C123/p1772819749912029",
        },
        "channel": {
            "id": "",
            "name": "",
        },
        "title": "Slack Thread: Example",
        "messageCount": 1,
        "messages": [
            {
                "ts": "1772819749.912029",
                "textResolved": "Example",
                "text": "Example",
                "user": {"displayName": "Demo User"},
            }
        ],
    }

    rendered = slack.render_markdown(envelope)
    assert "- _Channel_: `#demo:unknown-channel`" in rendered
    assert "- _Channel_: `#`" not in rendered


def test_render_search_markdown_includes_time_envelope() -> None:
    rendered = slack.render_search_markdown(
        {
            "query": "ABC",
            "scope": "workspace",
            "source": "live-search",
            "matchMode": "literal",
            "resultCount": 1,
            "threadCount": 1,
            "threads": [
                {
                    "title": "ABC replacement",
                    "permalink": "https://demo.slack.com/archives/C123/p1773085070240949",
                    "channelName": "ops",
                    "channelId": "C123",
                    "matchCount": 1,
                    "latestTs": "1773086070.240949",
                    "results": [
                        {
                            "ts": "1773085070.240949",
                            "userDisplayName": "Alice",
                            "textResolved": "hello",
                            "permalink": "https://demo.slack.com/archives/C123/p1773085070240949",
                        }
                    ],
                }
            ],
        }
    )

    assert "- Created: 2026-03-09T19:37:50.240949Z" in rendered
    assert "- Updated: 2026-03-09T19:54:30.240949Z" in rendered


def test_sync_archive_rejects_windows_over_six_weeks() -> None:
    ref = slack.SlackRef(
        raw="C12345678",
        workspace="demo",
        kind="channel",
        channel_id="C12345678",
    )
    with pytest.raises(slack.ToolError, match="capped at 6w"):
        slack.run_sync_archive(ref, lookback="7w", refresh=False)


def test_thread_window_archive_centers_sync_on_permalink(monkeypatch, tmp_path: Path) -> None:
    thread_ref = slack.SlackRef(
        raw="https://example.slack.com/archives/C12345678/p1773085070240949",
        workspace="demo",
        kind="thread",
        channel_id="C12345678",
        thread_ts="1773085070.240949",
        url="https://example.slack.com/archives/C12345678/p1773085070240949",
    )
    result = slack.ArchiveResult(
        root_dir=tmp_path / "archive",
        db_path=tmp_path / "archive" / "slackdump.sqlite",
        log_path=tmp_path / "archive" / "slackdump.log",
    )
    calls: list[tuple[str, str | None, str | None]] = []

    thread_center = slack.slack_ts_to_datetime(thread_ref.thread_ts)
    monkeypatch.setattr(slack, "utc_now", lambda: thread_center + dt.timedelta(weeks=4))
    monkeypatch.setattr(slack, "workspace_archive_result", lambda workspace: result)
    monkeypatch.setattr(slack, "thread_window_is_covered", lambda *args, **kwargs: False)

    def fake_run_archive_into_workspace(ref, *, result, time_from, time_to, timeout_seconds=None):
        calls.append((ref.kind, time_from, time_to))
        return result

    monkeypatch.setattr(slack, "run_archive_into_workspace", fake_run_archive_into_workspace)

    out = slack.ensure_thread_window_archive(thread_ref, refresh=False)

    assert out == result
    assert calls == [
        (
            "thread",
            slack.to_slackdump_time(thread_center - dt.timedelta(weeks=3)),
            slack.to_slackdump_time(thread_center + dt.timedelta(weeks=3)),
        )
    ]


def test_thread_window_archive_reuses_thread_targeted_coverage(
    monkeypatch, tmp_path: Path
) -> None:
    thread_ref = slack.SlackRef(
        raw="https://example.slack.com/archives/C12345678/p1773085070240949",
        workspace="demo",
        kind="thread",
        channel_id="C12345678",
        thread_ts="1773085070.240949",
        url="https://example.slack.com/archives/C12345678/p1773085070240949",
    )
    result = slack.ArchiveResult(
        root_dir=tmp_path / "archive",
        db_path=tmp_path / "archive" / "slackdump.sqlite",
        log_path=tmp_path / "archive" / "slackdump.log",
    )
    result.root_dir.mkdir(parents=True, exist_ok=True)
    result.db_path.touch()

    monkeypatch.setattr(slack, "workspace_archive_result", lambda workspace: result)
    monkeypatch.setattr(slack, "thread_window_is_covered", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        slack,
        "run_archive_into_workspace",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("should not rerun thread-targeted archive")
        ),
    )

    assert slack.ensure_thread_window_archive(thread_ref, refresh=False) == result


def test_sync_supports_explicit_bounded_window(monkeypatch, capsys, tmp_path: Path) -> None:
    ref = slack.SlackRef(
        raw="C12345678",
        workspace="demo",
        kind="channel",
        channel_id="C12345678",
        url="https://demo.slack.com/archives/C12345678",
    )
    result = slack.ArchiveResult(
        root_dir=tmp_path / "archive",
        db_path=tmp_path / "archive" / "slackdump.sqlite",
        log_path=tmp_path / "archive" / "slackdump.log",
    )
    calls: list[tuple[str, str]] = []
    seen_lookbacks: list[str | None] = []

    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)
    monkeypatch.setattr(slack, "resolve_slack_ref", lambda raw, workspace: ref)
    monkeypatch.setattr(
        slack,
        "_run_bounded_archive_window",
        lambda ref, since, until, refresh: calls.append(
            (slack.format_utc_iso(since) or "", slack.format_utc_iso(until) or "")
        )
        or result,
    )
    monkeypatch.setattr(
        slack,
        "sync_result",
        lambda result, ref, lookback: seen_lookbacks.append(lookback)
        or {"workspace": ref.workspace, "channel": ref.channel_id},
    )

    code = slack.main(
        [
            "sync",
            "C12345678",
            "--workspace",
            "demo",
            "--since",
            "2026-01-01T00:00:00Z",
            "--until",
            "2026-01-10T00:00:00Z",
            "--refresh",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["window"] == {
        "since": "2026-01-01T00:00:00Z",
        "until": "2026-01-10T00:00:00Z",
    }
    assert calls == [("2026-01-01T00:00:00Z", "2026-01-10T00:00:00Z")]
    assert seen_lookbacks == [None]


def test_load_sync_summary_uses_directory_channel_when_archive_stub_is_empty(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE message (
                channel_id TEXT,
                ts TEXT,
                thread_ts TEXT,
                txt TEXT,
                data TEXT,
                chunk_id INTEGER,
                idx INTEGER
            );
            """
        )
        conn.execute(
            """
            INSERT INTO message(channel_id, ts, thread_ts, txt, data, chunk_id, idx)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "C12345678",
                "1773085070.240949",
                "1773085070.240949",
                "hello",
                json.dumps({"user": "U1", "text": "hello"}),
                1,
                1,
            ),
        )
        monkeypatch.setattr(slack, "latest_channel_row", lambda conn, channel_id: None)
        monkeypatch.setattr(
            slack,
            "ensure_channel_directory_entries",
            lambda workspace, channel_ids: {
                "C12345678": {
                    "id": "C12345678",
                    "name": "ops",
                    "type": "public_channel",
                    "isPrivate": False,
                    "isArchived": False,
                    "isShared": False,
                    "isExtShared": False,
                    "is_private": False,
                    "is_archived": False,
                    "is_shared": False,
                    "is_ext_shared": False,
                }
            },
        )

        summary = slack.load_sync_summary(
            conn,
            workspace="demo",
            channel_id="C12345678",
        )
    finally:
        conn.close()

    assert summary["channel"]["id"] == "C12345678"
    assert summary["channel"]["name"] == "ops"


def test_query_search_all_and_any_modes() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE message (
                channel_id TEXT,
                ts TEXT,
                thread_ts TEXT,
                txt TEXT,
                data TEXT,
                chunk_id INTEGER,
                idx INTEGER
            );
            """
        )
        conn.execute(
            """
            INSERT INTO message(channel_id, ts, thread_ts, txt, data, chunk_id, idx)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "C12345678",
                "1773085070.240949",
                "1773085070.240949",
                "ABC and MeshLink together",
                json.dumps({"user": "U12345678", "text": "ABC and MeshLink together"}),
                1,
                1,
            ),
        )
        conn.execute(
            """
            INSERT INTO message(channel_id, ts, thread_ts, txt, data, chunk_id, idx)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "C12345678",
                "1773085080.240949",
                "1773085080.240949",
                "Relay only",
                json.dumps({"user": "U12345678", "text": "Relay only"}),
                1,
                1,
            ),
        )

        result_all = slack.query_search(
            conn,
            query="ABC MeshLink",
            limit=20,
            workspace="demo",
            match_mode="all",
        )
        result_any = slack.query_search(
            conn,
            query="ABC Relay",
            limit=20,
            workspace="demo",
            match_mode="any",
        )
    finally:
        conn.close()

    assert result_all["resultCount"] == 1
    assert result_all["results"][0]["followCommand"] == (
        "gotta read https://demo.slack.com/archives/C12345678/p1773085070240949"
    )
    assert result_any["resultCount"] == 2


def test_search_follow_command_prefers_root_thread_permalink() -> None:
    command = slack.search_follow_command(
        "https://demo.slack.com/archives/C12345678/p1773081279142849?thread_ts=1773075428.384009"
    )

    assert command == "gotta read https://demo.slack.com/archives/C12345678/p1773075428384009"


def test_normalize_live_search_match_prefers_root_thread_permalink() -> None:
    item = slack._normalize_live_search_match(
        "demo",
        {
            "channel": {"id": "C12345678", "name": "ops"},
            "ts": "1773081279.142849",
            "permalink": (
                "https://demo.slack.com/archives/C12345678/"
                "p1773081279142849?thread_ts=1773075428.384009"
            ),
            "text": "reply match",
            "user": "U1",
            "username": "Alice",
        },
    )

    assert item["threadTs"] == "1773075428.384009"
    assert item["threadPermalink"] == (
        "https://demo.slack.com/archives/C12345678/p1773075428384009"
    )
    assert item["permalink"] == (
        "https://demo.slack.com/archives/C12345678/"
        "p1773081279142849?thread_ts=1773075428.384009"
    )
    assert item["followCommand"] == (
        "gotta read https://demo.slack.com/archives/C12345678/p1773075428384009"
    )


def test_normalize_live_search_match_synthesizes_reply_permalink_with_thread_context() -> None:
    item = slack._normalize_live_search_match(
        "demo",
        {
            "channel": {"id": "C12345678", "name": "ops"},
            "ts": "1773086070.240949",
            "thread_ts": "1773085070.240949",
            "text": "reply match",
            "user": "U1",
            "username": "Alice",
        },
    )

    assert item["threadTs"] == "1773085070.240949"
    assert item["permalink"] == (
        "https://demo.slack.com/archives/C12345678/"
        "p1773086070240949?thread_ts=1773085070.240949"
    )
    assert item["threadPermalink"] == (
        "https://demo.slack.com/archives/C12345678/p1773085070240949"
    )
    assert item["followCommand"] == (
        "gotta read https://demo.slack.com/archives/C12345678/p1773085070240949"
    )


def test_archive_search_applies_before_date_modifier() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            CREATE TABLE message(
                channel_id TEXT,
                ts TEXT,
                thread_ts TEXT,
                txt TEXT,
                data TEXT,
                chunk_id INTEGER,
                idx INTEGER
            );
            """
        )
        conn.execute(
            """
            INSERT INTO message(channel_id, ts, thread_ts, txt, data, chunk_id, idx)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "C12345678",
                "1735603200.000000",
                "1735603200.000000",
                "ABC old result",
                json.dumps({"user": "U12345678", "text": "ABC old result"}),
                1,
                1,
            ),
        )
        conn.execute(
            """
            INSERT INTO message(channel_id, ts, thread_ts, txt, data, chunk_id, idx)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "C12345678",
                "1735776000.000000",
                "1735776000.000000",
                "ABC new result",
                json.dumps({"user": "U12345678", "text": "ABC new result"}),
                1,
                1,
            ),
        )

        result = slack.query_search(
            conn,
            query="ABC before:2025-01-01",
            limit=20,
            workspace="demo",
            match_mode="all",
        )
    finally:
        conn.close()

    assert result["resultCount"] == 1
    assert result["appliedModifiers"] == ["before:2025-01-01"]
    assert result["results"][0]["text"] == "ABC old result"


def test_slack_search_defaults_to_live_source() -> None:
    args = slack.build_parser().parse_args(["search", "auth"])
    assert args.source == "live"
    assert args.match == "all"
    assert args.output == "markdown"


def test_search_spec_splits_known_modifiers_from_terms() -> None:
    spec = slack.search_spec(
        "ABC Relay before:2025-01-01 in:ops",
        match_mode="any",
    )

    assert spec.terms == ["ABC", "Relay"]
    assert spec.modifiers == ["before:2025-01-01", "in:ops"]


def test_live_search_any_keeps_modifiers_attached_to_each_term() -> None:
    spec = slack.search_spec(
        "ABC Relay before:2025-01-01 in:ops",
        match_mode="any",
    )

    assert slack._live_search_queries(spec) == [
        "ABC before:2025-01-01 in:ops",
        "Relay before:2025-01-01 in:ops",
    ]


def test_live_search_literal_quotes_exact_phrase() -> None:
    spec = slack.search_spec(
        "ghost of relay past, present, and future",
        match_mode="literal",
    )

    assert slack._live_search_queries(spec) == ['"ghost of relay past, present, and future"']


def test_search_live_payload_preserves_native_result_order(monkeypatch) -> None:
    monkeypatch.setattr(
        slack,
        "ensure_live_search_auth",
        lambda workspace, interactive_ok: ({}, None),
    )
    calls: list[dict[str, str]] = []

    def fake_slack_api_post(
        workspace: str,
        auth_state: dict[str, object],
        method: str,
        *,
        data: dict[str, str],
        timeout_seconds: int,
    ) -> dict[str, object]:
        calls.append(data)
        assert method == "search.messages"
        return {
            "messages": {
                "matches": [
                    {
                        "channel": {"id": "C123", "name": "ops"},
                        "ts": "1773085070.240949",
                        "permalink": "https://demo.slack.com/archives/C123/p1773085070240949",
                        "text": "first result",
                        "user": "U1",
                        "username": "Alice",
                    },
                    {
                        "channel": {"id": "C123", "name": "ops"},
                        "ts": "1773086070.240949",
                        "permalink": "https://demo.slack.com/archives/C123/p1773086070240949",
                        "text": "second result",
                        "user": "U2",
                        "username": "Bob",
                    },
                ]
            }
        }

    monkeypatch.setattr(slack, "slack_api_post", fake_slack_api_post)

    result = slack.search_live_payload(
        workspace="demo",
        query="ABC",
        limit=2,
        match_mode="all",
    )

    assert calls == [{"query": "ABC", "count": "20"}]
    assert [item["text"] for item in result["results"]] == ["first result", "second result"]
    assert [thread["title"] for thread in result["threads"]] == ["first result", "second result"]


def test_parse_slackdump_auth_export() -> None:
    payload = slack_provider.parse_slackdump_auth_export(
        "TOKEN=xoxc-demo\n"
        ".slack.com\tTRUE\t/\tTRUE\t4102444800\td\tcookie-value\n"
        ".slack.com\tTRUE\t/\tTRUE\t4102444800\td-s\t123\n"
    )

    assert payload["token"] == "xoxc-demo"
    assert [cookie["name"] for cookie in payload["cookies"]] == ["d", "d-s"]


def test_live_channel_search_does_not_require_recent_sync(monkeypatch, capsys) -> None:
    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)
    monkeypatch.setattr(
        slack,
        "try_opportunistic_sync",
        lambda ref: (_ for _ in ()).throw(AssertionError("should not opportunistically sync")),
    )
    monkeypatch.setattr(
        slack,
        "resolve_slack_ref",
        lambda raw, workspace: slack.SlackRef(
            raw=raw,
            workspace=workspace,
            kind="channel",
            channel_id="C12345678",
            url="https://demo.slack.com/archives/C12345678",
        ),
    )
    monkeypatch.setattr(
        slack,
        "search_live_payload",
        lambda **kwargs: {
            "workspace": "demo",
            "query": kwargs["query"],
            "terms": ["auth"],
            "matchMode": kwargs["match_mode"],
            "scope": "channel",
            "source": "live-search",
            "channel": {"id": "C12345678", "name": "ops"},
            "channelCount": 1,
            "channels": [],
            "resultCount": 0,
            "threadCount": 0,
            "threads": [],
            "results": [],
        },
    )

    code = slack.main(
        [
            "search",
            "auth",
            "--workspace",
            "demo",
            "--channel",
            "ops",
            "--source",
            "live",
            "--output",
            "json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "live-search"


def test_live_search_missing_auth_returns_typed_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)
    monkeypatch.setattr(
        slack,
        "ensure_live_search_auth",
        lambda workspace, interactive_ok: (_ for _ in ()).throw(
            slack.ToolError("missing gotta-owned Slack live-search auth")
        ),
    )

    code = slack.main(["search", "auth", "--workspace", "demo", "--source", "live"])

    assert code == 1
    assert "missing gotta-owned Slack live-search auth" in capsys.readouterr().err


def test_live_search_preserves_date_qualifier_intent(monkeypatch, capsys) -> None:
    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(
        slack,
        "search_live_payload",
        lambda **kwargs: seen.append(kwargs)
        or {
            "workspace": "demo",
            "query": kwargs["query"],
            "terms": ["ABC"],
            "modifiers": ["before:2025-01-01"],
            "matchMode": kwargs["match_mode"],
            "scope": "workspace",
            "source": "live-search",
            "channelCount": 0,
            "channels": [],
            "resultCount": 0,
            "threadCount": 0,
            "threads": [],
            "results": [],
        },
    )

    code = slack.main(
        [
            "search",
            "ABC before:2025-01-01",
            "--workspace",
            "demo",
            "--source",
            "live",
            "--output",
            "json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["modifiers"] == ["before:2025-01-01"]
    assert seen[0]["query"] == "ABC before:2025-01-01"


def test_cmd_get_channel_url_uses_cached_envelope_without_crashing(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)
    monkeypatch.setattr(
        slack,
        "resolve_slack_ref",
        lambda raw, workspace: slack.SlackRef(
            raw=raw,
            workspace=workspace,
            kind="channel",
            channel_id="C12345678",
            url="https://demo.slack.com/archives/C12345678",
        ),
    )
    monkeypatch.setattr(
        slack,
        "resolve_channel_window",
        lambda args: slack.ChannelWindow(
            since=None,
            until=None,
            lookback=None,
            strict=False,
        ),
    )
    monkeypatch.setattr(
        slack,
        "maybe_load_from_cache",
        lambda ref, window=None: {
            "workspace": ref.workspace,
            "ref": ref.raw,
            "channel": {"id": ref.channel_id, "name": "ops"},
            "title": "#ops",
            "messageCount": 0,
            "messages": [],
            "threadPermalink": ref.url,
        },
    )

    code = slack.main(
        [
            "get",
            "https://demo.slack.com/archives/C12345678",
            "--workspace",
            "demo",
            "--output",
            "meta",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["channel"]["id"] == "C12345678"
    assert payload["retrieval"]["state"] == "materialized"
    assert payload["fidelity"]["mode"] == "bounded"
    assert "locally cached archive coverage" in payload["fidelity"]["detail"]


def test_build_threads_falls_back_to_message_channel_id_for_permalink() -> None:
    threads = slack.build_threads(
        workspace="demo",
        channel={"id": "", "name": "ops"},
        messages=[
            {
                "channelId": "C12345678",
                "ts": "1773085070.240949",
                "threadTs": "",
                "permalink": "https://demo.slack.com/archives/C12345678/p1773085070240949",
                "text": "hello",
                "textResolved": "hello",
                "user": {"id": "U1", "displayName": "Operator"},
            }
        ],
    )

    assert threads[0]["permalink"] == "https://demo.slack.com/archives/C12345678/p1773085070240949"


def test_normalize_messages_preserves_thread_context_in_reply_permalink() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE message (
                channel_id TEXT,
                ts TEXT,
                thread_ts TEXT,
                txt TEXT,
                data TEXT,
                chunk_id INTEGER,
                idx INTEGER
            );
            """
        )
        conn.execute(
            """
            INSERT INTO message(channel_id, ts, thread_ts, txt, data, chunk_id, idx)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "C12345678",
                "1773086070.240949",
                "1773085070.240949",
                "reply",
                json.dumps({"user": "U1", "text": "reply"}),
                1,
                1,
            ),
        )
        rows = conn.execute("SELECT channel_id, ts, thread_ts, txt, data FROM message").fetchall()
    finally:
        conn.close()

    messages = slack.normalize_messages(
        rows,
        workspace="demo",
        users={},
        channels={},
    )

    assert messages[0]["permalink"] == (
        "https://demo.slack.com/archives/C12345678/"
        "p1773086070240949?thread_ts=1773085070.240949"
    )


def test_build_envelope_thread_uses_directory_channel_for_root_channel(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE message (
                channel_id TEXT,
                ts TEXT,
                thread_ts TEXT,
                txt TEXT,
                data TEXT,
                chunk_id INTEGER,
                idx INTEGER
            );
            """
        )
        conn.execute(
            """
            INSERT INTO message(channel_id, ts, thread_ts, txt, data, chunk_id, idx)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "C12345678",
                "1773085070.240949",
                "1773085070.240949",
                "hello",
                json.dumps({"user": "U1", "text": "hello"}),
                1,
                1,
            ),
        )
        monkeypatch.setattr(slack, "latest_user_rows", lambda conn, user_ids: {})
        monkeypatch.setattr(slack, "ensure_user_directory_entries", lambda workspace, user_ids: {})
        monkeypatch.setattr(slack, "latest_channel_row", lambda conn, channel_id: None)
        monkeypatch.setattr(
            slack,
            "ensure_channel_directory_entries",
            lambda workspace, channel_ids: {
                "C12345678": {
                    "id": "C12345678",
                    "name": "ops",
                    "type": "public_channel",
                    "isPrivate": False,
                    "isArchived": False,
                    "isShared": False,
                    "isExtShared": False,
                    "is_private": False,
                    "is_archived": False,
                    "is_shared": False,
                    "is_ext_shared": False,
                }
            },
        )

        envelope = slack.build_envelope(
            conn,
            slack.SlackRef(
                raw="https://demo.slack.com/archives/C12345678/p1773085070240949",
                workspace="demo",
                kind="thread",
                channel_id="C12345678",
                thread_ts="1773085070.240949",
                url="https://demo.slack.com/archives/C12345678/p1773085070240949",
            ),
        )
    finally:
        conn.close()

    assert envelope["channel"]["id"] == "C12345678"
    assert envelope["channel"]["name"] == "ops"
    assert envelope["visibility_level"] == "internal"
    assert envelope["visibility_boundary"] == "same_company"


def test_query_search_reply_results_follow_root_thread_permalink() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE message (
                channel_id TEXT,
                ts TEXT,
                thread_ts TEXT,
                txt TEXT,
                data TEXT,
                chunk_id INTEGER,
                idx INTEGER
            );
            """
        )
        conn.execute(
            """
            INSERT INTO message(channel_id, ts, thread_ts, txt, data, chunk_id, idx)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "C12345678",
                "1773086070.240949",
                "1773085070.240949",
                "reply text",
                json.dumps({"user": "U1", "text": "reply text"}),
                1,
                1,
            ),
        )
        result = slack.query_search(
            conn,
            query="reply",
            limit=10,
            workspace="demo",
            match_mode="all",
        )
    finally:
        conn.close()

    assert result["results"][0]["permalink"] == (
        "https://demo.slack.com/archives/C12345678/"
        "p1773086070240949?thread_ts=1773085070.240949"
    )
    assert result["results"][0]["visibility_level"] == "unknown"
    assert result["results"][0]["followCommand"] == (
        "gotta read https://demo.slack.com/archives/C12345678/p1773085070240949"
    )


def test_render_markdown_marks_thread_reads_full_fidelity() -> None:
    rendered = slack.render_markdown(
        {
            "workspace": "demo",
            "ref": {
                "kind": "thread",
                "channelId": "C12345678",
                "threadTs": "1773085070.240949",
                "url": "https://demo.slack.com/archives/C12345678/p1773085070240949",
            },
            "channel": {"id": "C12345678", "name": "ops"},
            "visibility_level": "internal",
            "visibility_boundary": "same_company",
            "visibility_confidence": "high",
            "visibility_basis": ["provider=slack", "channel.type=public_channel"],
            "title": "Slack Thread: Example",
            "messageCount": 1,
            "threadCount": 1,
            "messages": [
                {
                    "ts": "1773085070.240949",
                    "text": "hello",
                    "textResolved": "hello",
                    "user": {"id": "U1", "displayName": "Operator"},
                }
            ],
            "threads": [],
        }
    )

    assert "- _Retrieval_: `materialized`" in rendered
    assert "- _Fidelity_: `full` (full thread render from the hydrated bounded archive window)" in rendered
    assert "- Visibility: internal (same_company, high)" in rendered


def test_normalize_live_search_match_carries_channel_visibility() -> None:
    item = slack._normalize_live_search_match(
        "demo",
        {
            "channel": {
                "id": "G12345678",
                "name": "partners",
                "is_private": True,
                "is_shared": True,
                "is_ext_shared": True,
            },
            "ts": "1773086070.240949",
            "thread_ts": "1773085070.240949",
            "text": "reply match",
            "user": "U1",
            "username": "Alice",
        },
    )

    assert item["visibility_level"] == "restricted"
    assert item["visibility_boundary"] == "cross_company"


def test_cmd_get_thread_hydration_retries_refresh_automatically(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)
    ref = slack.SlackRef(
        raw="https://demo.slack.com/archives/C12345678/p1773085070240949",
        workspace="demo",
        kind="thread",
        channel_id="C12345678",
        thread_ts="1773085070.240949",
        url="https://demo.slack.com/archives/C12345678/p1773085070240949",
    )
    monkeypatch.setattr(
        slack,
        "resolve_slack_ref",
        lambda raw, workspace: ref,
    )
    calls: list[bool] = []
    result = slack.ArchiveResult(
        root_dir=Path("/tmp/archive"),
        db_path=Path("/tmp/archive/slackdump.sqlite"),
        log_path=Path("/tmp/archive/slackdump.log"),
    )
    monkeypatch.setattr(
        slack,
        "ensure_thread_window_archive",
        lambda ref, refresh, timeout_seconds=None: calls.append((refresh, timeout_seconds))
        or ((_ for _ in ()).throw(slack.ToolError("archive sync failed")) if not refresh else result),
    )
    monkeypatch.setattr(
        slack,
        "load_envelope_from_archive",
        lambda result, ref, window=None, coverage=None: {
            "kind": "thread",
            "messages": [{"ts": ref.thread_ts, "text": "ok"}],
        },
    )
    monkeypatch.setattr(slack, "render_markdown", lambda envelope: "ok\n")

    code = slack.main(
        [
            "get",
            "https://demo.slack.com/archives/C12345678/p1773085070240949",
            "--workspace",
            "demo",
        ]
    )

    assert code == 0
    captured = capsys.readouterr()
    assert calls == [
        (False, slack.DEFAULT_THREAD_HYDRATION_TIMEOUT_SECONDS),
        (True, slack.DEFAULT_THREAD_HYDRATION_TIMEOUT_SECONDS),
    ]
    assert "ok" in captured.out
    assert "retrieval state: queued slack thread C12345678:1773085070.240949" in captured.err
    assert "retrieval state: hydrating Slack thread through the native bounded archive window" in captured.err
    assert "retrieval state: hydrating slack thread retry with an explicit archive refresh" in captured.err
    assert "retrieval state: materialized bounded thread refresh; reading hydrated archive" in captured.err


def test_cmd_get_thread_hydration_failure_reports_automatic_refresh_attempt(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)
    monkeypatch.setattr(
        slack,
        "resolve_slack_ref",
        lambda raw, workspace: slack.SlackRef(
            raw=raw,
            workspace=workspace,
            kind="thread",
            channel_id="C12345678",
            thread_ts="1773085070.240949",
            url="https://demo.slack.com/archives/C12345678/p1773085070240949",
        ),
    )
    attempts: list[bool] = []
    monkeypatch.setattr(
        slack,
        "ensure_thread_window_archive",
        lambda ref, refresh, timeout_seconds=None: attempts.append((refresh, timeout_seconds))
        or (_ for _ in ()).throw(slack.ToolError("archive sync failed")),
    )
    monkeypatch.setattr(
        slack,
        "utc_now",
        lambda: dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc),
    )

    code = slack.main(
        [
            "get",
            "https://demo.slack.com/archives/C12345678/p1773085070240949",
            "--workspace",
            "demo",
        ]
    )

    assert code == 1
    assert attempts == [
        (False, slack.DEFAULT_THREAD_HYDRATION_TIMEOUT_SECONDS),
        (True, slack.DEFAULT_THREAD_HYDRATION_TIMEOUT_SECONDS),
    ]
    err = capsys.readouterr().err
    assert "retrieval state: queued slack thread C12345678:1773085070.240949" in err
    assert "thread permalink retrieval has a bounded-archive coverage gap" in err
    assert "attempted the centered six-week hydration window" in err
    assert "retried with an explicit bounded refresh automatically" in err
    assert "gotta slack sync C12345678 --since" not in err


def test_cmd_get_thread_cached_archive_read_failure_retries_refresh(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)
    ref = slack.SlackRef(
        raw="https://demo.slack.com/archives/C12345678/p1773085070240949",
        workspace="demo",
        kind="thread",
        channel_id="C12345678",
        thread_ts="1773085070.240949",
        url="https://demo.slack.com/archives/C12345678/p1773085070240949",
    )
    monkeypatch.setattr(
        slack,
        "resolve_slack_ref",
        lambda raw, workspace: ref,
    )
    attempts: list[tuple[bool, int | None]] = []
    result = slack.ArchiveResult(
        root_dir=Path("/tmp/archive"),
        db_path=Path("/tmp/archive/slackdump.sqlite"),
        log_path=Path("/tmp/archive/slackdump.log"),
    )
    monkeypatch.setattr(
        slack,
        "ensure_thread_window_archive",
        lambda ref, refresh, timeout_seconds=None: attempts.append((refresh, timeout_seconds))
        or result,
    )
    loads = {"count": 0}

    def fake_load(result, ref, window=None, coverage=None):
        loads["count"] += 1
        if loads["count"] == 1:
            raise slack.ToolError(
                "exact Slack thread target is absent from the hydrated bounded archive window"
            )
        return {
            "kind": "thread",
            "messages": [{"ts": ref.thread_ts, "text": "ok"}],
        }

    monkeypatch.setattr(slack, "load_envelope_from_archive", fake_load)
    monkeypatch.setattr(slack, "render_markdown", lambda envelope: "ok\n")

    code = slack.main(
        [
            "get",
            "https://demo.slack.com/archives/C12345678/p1773085070240949",
            "--workspace",
            "demo",
        ]
    )

    assert code == 0
    captured = capsys.readouterr()
    assert attempts == [
        (False, slack.DEFAULT_THREAD_HYDRATION_TIMEOUT_SECONDS),
        (True, slack.DEFAULT_THREAD_HYDRATION_TIMEOUT_SECONDS),
    ]
    assert loads["count"] == 2
    assert "ok" in captured.out
    assert "retrieval state: hydrating slack thread retry with an explicit archive refresh" in captured.err
    assert "retrieval state: materialized bounded thread refresh; reading hydrated archive" in captured.err


def test_cmd_get_reply_permalink_with_thread_ts_query_uses_root_thread_ts(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)
    captured: dict[str, str] = {}

    def fake_ensure(ref, refresh, timeout_seconds=None):
        captured["thread_ts"] = str(ref.thread_ts or "")
        return slack.ArchiveResult(
            root_dir=Path("/tmp/archive"),
            db_path=Path("/tmp/archive/slackdump.sqlite"),
            log_path=Path("/tmp/archive/slackdump.log"),
        )

    monkeypatch.setattr(slack, "ensure_thread_window_archive", fake_ensure)
    monkeypatch.setattr(
        slack,
        "load_envelope_from_archive",
        lambda *args, **kwargs: {
            "workspace": "demo",
            "ref": {"kind": "thread", "channelId": "C12345678", "threadTs": captured["thread_ts"]},
            "channel": {"id": "C12345678", "name": "demo"},
            "title": "Slack Thread: Example",
            "messageCount": 0,
            "threadCount": 0,
            "messages": [],
            "threads": [],
        },
    )

    code = slack.main(
        [
            "get",
            "https://demo.slack.com/archives/C12345678/p1773081279142849?thread_ts=1773075428.384009",
            "--workspace",
            "demo",
            "--output",
            "json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert captured["thread_ts"] == "1773075428.384009"
    assert payload["ref"]["threadTs"] == "1773075428.384009"


def test_cmd_get_permalink_reports_inaccessible_channel_separately(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)
    monkeypatch.setattr(slack, "resolve_slack_ref", slack.parse_slack_ref)
    monkeypatch.setattr(
        slack,
        "ensure_thread_window_archive",
        lambda ref, refresh, timeout_seconds=None: (_ for _ in ()).throw(
            slack.ToolError(
                "channel C12345678 not accessible via slackdump archive (exit code 6): "
                "likely in a private channel"
            )
        ),
    )
    monkeypatch.setattr(
        slack,
        "utc_now",
        lambda: dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc),
    )

    code = slack.main(
        [
            "get",
            "https://demo.slack.com/archives/C12345678/p1773085070240949",
            "--workspace",
            "demo",
        ]
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "thread permalink retrieval could not read the underlying channel archive" in err
    assert "source-access limitation rather than a simple bounded-archive coverage gap" in err
    assert "detail: channel C12345678 not accessible via slackdump archive" in err
    assert "thread permalink retrieval has a bounded-archive coverage gap" not in err


def test_build_envelope_thread_missing_rows_never_suggests_pull_recent(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        monkeypatch.setattr(slack, "load_message_rows", lambda *args, **kwargs: [])
        ref = slack.SlackRef(
            raw="https://demo.slack.com/archives/C12345678/p1773085070240949",
            workspace="demo",
            kind="thread",
            channel_id="C12345678",
            thread_ts="1773085070.240949",
            url="https://demo.slack.com/archives/C12345678/p1773085070240949",
        )
        with pytest.raises(slack.ToolError) as excinfo:
            slack.build_envelope(conn, ref)
    finally:
        conn.close()

    message = str(excinfo.value)
    assert "--pull-recent" not in message
    assert "--refresh" in message
    assert "exact Slack thread target is absent from the hydrated bounded archive window" in message
    assert "coverage hole" in message
