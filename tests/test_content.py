from __future__ import annotations

import io
import json
from pathlib import Path

from gotta import content


def make_dirs(root: Path) -> content.ResolvedDirs:
    dirs = content.ResolvedDirs(
        session_dir=root,
        content_dir=root / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    return dirs


def initialize_session(root: Path) -> content.ResolvedDirs:
    dirs = make_dirs(root)
    content.write_state_env(dirs)
    dirs.session_dir.joinpath("bin").mkdir(parents=True, exist_ok=True)
    return dirs


def test_materialize_bytes_creates_content_directory_and_manifest(tmp_path: Path) -> None:
    dirs = make_dirs(tmp_path / "ws")

    result = content.materialize_bytes(
        b"hello world",
        dirs=dirs,
        preferred_name="demo.md",
        metadata={"tool": "gotta", "plugin": "read", "locator": "demo"},
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    assert result.data_path.read_bytes() == b"hello world"
    assert result.name_link.is_symlink()
    assert result.fetch_link.is_symlink()
    assert result.name_link.resolve() == result.data_path.resolve()
    assert result.fetch_link.resolve() == result.data_path.resolve()
    assert result.content_dir.name == result.digest
    assert result.data_path.name == "data"
    assert result.meta_path.name == "meta.json"
    assert result.names_dir.name == "names"
    assert result.logs_dir.name == "logs"
    assert result.fetch_link.name == "2026-03-11T00:00:00.000001Z"

    meta = json.loads(result.meta_path.read_text(encoding="utf-8"))
    assert meta["hash"] == result.digest
    assert meta["preferred_name"] == "demo.md"
    assert meta["original_name"] == "demo.md"
    assert meta["fetched_at"] == "2026-03-11T00:00:00.000001Z"

    manifest_lines = (dirs.content_dir / "manifest.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(manifest_lines) == 1
    manifest = json.loads(manifest_lines[0])
    assert manifest["plugin"] == "read"
    assert manifest["locator"] == "demo"
    assert manifest["actor"] == "primary"
    assert manifest["preferred_name"] == "demo.md"
    assert Path(manifest["fetch_link"]).name == result.fetch_link.name
    assert Path(manifest["canonical_path"]).name == "data"


def test_scan_content_store_reads_directory_layout(tmp_path: Path) -> None:
    dirs = make_dirs(tmp_path / "ws")
    result = content.materialize_bytes(
        b"hello world",
        dirs=dirs,
        preferred_name="demo.md",
        metadata={"tool": "gotta", "plugin": "read", "locator": "demo"},
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    snapshots = content.scan_content_store(dirs.content_dir)

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.digest == result.digest
    assert snapshot.content_dir == result.content_dir
    assert snapshot.names == ["demo.md"]
    assert [event.timestamp for event in snapshot.events] == ["2026-03-11T00:00:00.000001Z"]


def test_scan_content_store_handles_missing_names_directory(tmp_path: Path) -> None:
    dirs = make_dirs(tmp_path / "ws")
    digest = "a" * 64
    content_dir = dirs.content_dir / digest
    content_dir.mkdir(parents=True, exist_ok=True)
    (content_dir / "data").write_bytes(b"hello world")

    snapshots = content.scan_content_store(dirs.content_dir)

    assert len(snapshots) == 1
    assert snapshots[0].digest == digest
    assert snapshots[0].names == []


def test_activity_events_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    initialize_session(root)

    content.append_activity_event(
        root,
        {
            "timestamp": "2026-03-14T10:00:00Z",
            "plugin": "logs",
            "surface": "logs",
            "action": "append",
            "locator": "LOGS.md",
            "preferred_name": "LOGS.md",
            "follow_command": "gotta read 'LOGS.md'",
            "detail": "appended 1 logs entry",
            "time_field": "session_recorded_at",
        },
    )

    records = content.activity_events(root)

    assert records == [
        {
            "action": "append",
            "actor": "primary",
            "detail": "appended 1 logs entry",
            "follow_command": "gotta read 'LOGS.md'",
            "locator": "LOGS.md",
            "plugin": "logs",
            "preferred_name": "LOGS.md",
            "surface": "logs",
            "time_field": "session_recorded_at",
            "timestamp": "2026-03-14T10:00:00Z",
        }
    ]
    assert content.activity_log_path(root).is_file()


def test_resolve_dirs_prefers_local_state_over_default_root(
    tmp_path: Path, monkeypatch
) -> None:
    default_root = tmp_path / "default"
    local_root = tmp_path / "local"
    initialize_session(default_root)
    initialize_session(local_root)

    monkeypatch.setattr(content, "DEFAULT_SESSION_ROOT", default_root)
    monkeypatch.chdir(local_root)

    resolved = content.resolve_dirs(content.CommonOptions(), create=False)
    assert resolved.session_dir == local_root.resolve()
    assert resolved.content_dir == (local_root / "content").resolve()


def test_resolve_dirs_falls_back_to_context_bound_session(
    tmp_path: Path, monkeypatch
) -> None:
    default_root = tmp_path / "default"
    monkeypatch.setattr(content, "DEFAULT_SESSION_ROOT", default_root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    bound_root = default_root / content.session_token("thread-123")
    dirs = initialize_session(bound_root)
    content.write_session_state(
        dirs,
        {
            content.CONTEXT_ID_ENV: "thread-123",
            content.CONTEXT_SOURCE_ENV: "codex_thread",
        },
    )

    resolved = content.resolve_dirs(content.CommonOptions(), create=False)
    assert resolved.session_dir == bound_root.resolve()
    assert resolved.content_dir == (bound_root / "content").resolve()


def test_current_context_binding_uses_term_session_as_first_class_identity(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv(content.CONTEXT_ID_ENV, raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setenv("TERM_SESSION_ID", "term-session-1")
    monkeypatch.setenv("TTY", "/dev/ttys001")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COPILOT_LOADER_PID", "4242")
    monkeypatch.setenv("COPILOT_CLI_BINARY_VERSION", "1.2.3")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    monkeypatch.chdir(first)
    left, source = content.current_context_binding()
    monkeypatch.chdir(second)
    right, repeat_source = content.current_context_binding()

    assert source == "term_session"
    assert repeat_source == "term_session"
    assert left == "term-session-1"
    assert right == "term-session-1"
    assert left == right


def test_current_context_binding_prefers_codex_thread_over_term_session(
    monkeypatch,
) -> None:
    monkeypatch.delenv(content.CONTEXT_ID_ENV, raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    monkeypatch.setenv("TERM_SESSION_ID", "term-session-1")

    binding, source = content.current_context_binding()

    assert source == "codex_thread"
    assert binding == "thread-123"


def test_current_context_binding_uses_cwd_only_as_last_resort_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv(content.CONTEXT_ID_ENV, raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("TERM_SESSION_ID", raising=False)
    monkeypatch.delenv("TTY", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("COPILOT_LOADER_PID", raising=False)
    monkeypatch.delenv("COPILOT_CLI_BINARY_VERSION", raising=False)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    monkeypatch.chdir(first)
    left, source = content.current_context_binding()
    monkeypatch.chdir(second)
    right, repeat_source = content.current_context_binding()

    assert source == "terminal_fingerprint"
    assert repeat_source == "terminal_fingerprint"
    assert left != right


def test_session_is_initialized_depends_on_state_env_only(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    dirs = make_dirs(root)

    assert content.session_is_initialized(root) is False

    content.write_state_env(dirs)
    assert content.session_is_initialized(root) is True


def test_work_is_initialized_requires_want_surface(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    initialize_session(root)
    for name in ("TODO.md", "LOGS.md", "GOAL.md", "OOPS.md"):
        (root / name).write_text("", encoding="utf-8")

    assert content.work_is_initialized(root) is False

    (root / "WANT.md").write_text("", encoding="utf-8")
    assert content.work_is_initialized(root) is True


def test_stdin_has_readable_text_rejects_empty_stringio(monkeypatch) -> None:
    monkeypatch.setattr(content.sys, "stdin", io.StringIO(""))
    assert content.stdin_has_readable_text() is False


def test_stdin_has_readable_text_accepts_populated_stringio(monkeypatch) -> None:
    monkeypatch.setattr(content.sys, "stdin", io.StringIO("payload\n"))
    assert content.stdin_has_readable_text() is True
