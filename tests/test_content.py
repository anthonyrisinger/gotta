from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat

import gotta.content.activity as content_activity
import gotta.content.context as content_context
import gotta.content.env as content_env
import gotta.content.file as content_file
from gotta.content.filesystem import FileSystemLedgerStore
import gotta.content.model as content_model
import gotta.content.scope as content_scope
from gotta import topology


def materialize_bytes(
    data: bytes,
    *,
    dirs: content_model.ResolvedDirs,
    preferred_name: str,
    metadata: dict[str, object],
    timestamp: str | None = None,
) -> content_model.Materialization:
    return FileSystemLedgerStore.for_dirs(dirs).materialize_bytes(
        data,
        preferred_name=preferred_name,
        metadata=dict(metadata),
        timestamp=timestamp,
    )


def scan_content_store(content_dir: Path) -> list[content_model.ContentSnapshot]:
    return FileSystemLedgerStore.for_content_dir(content_dir).scan_artifacts()


def make_dirs(root: Path) -> content_model.ResolvedDirs:
    dirs = content_model.ResolvedDirs(
        session_dir=root,
        content_dir=root / "content",
    )
    content_file.ensure_private_dir(dirs.session_dir)
    content_file.ensure_private_dir(dirs.content_dir)
    return dirs


def initialize_session(root: Path) -> content_model.ResolvedDirs:
    dirs = make_dirs(root)
    content_env.write_state_env(dirs)
    return dirs


def test_materialize_bytes_creates_content_directory_and_manifest(
    tmp_path: Path,
) -> None:
    dirs = make_dirs(tmp_path / "ws")

    result = materialize_bytes(
        b"hello world",
        dirs=dirs,
        preferred_name="demo.md",
        metadata={"tool": "gotta", "plugin": "read", "locator": "demo"},
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    assert result.layout.blob_path.read_bytes() == b"hello world"
    assert result.alias.path.is_symlink()
    assert result.event.event_path.is_symlink()
    assert result.alias.path.resolve() == result.layout.blob_path.resolve()
    assert result.event.event_path.resolve() == result.layout.blob_path.resolve()
    assert result.layout.artifact_dir.name == result.digest
    assert result.layout.blob_path.name == "data"
    assert result.layout.metadata_path is not None
    assert result.layout.metadata_path.name == "meta.json"
    assert result.layout.alias_dir.name == "names"
    assert result.layout.event_dir.name == "logs"
    assert result.event.event_path.name == "2026-03-11T00:00:00.000001Z"

    meta = json.loads(result.layout.metadata_path.read_text(encoding="utf-8"))
    assert meta["hash"] == result.digest
    assert meta["preferred_name"] == "demo.md"
    assert meta["original_name"] == "demo.md"
    assert meta["fetched_at"] == "2026-03-11T00:00:00.000001Z"

    manifest_lines = (
        (dirs.content_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert len(manifest_lines) == 1
    manifest = json.loads(manifest_lines[0])
    assert manifest["plugin"] == "read"
    assert manifest["locator"] == "demo"
    assert manifest["actor"] == "ws"
    assert manifest["preferred_name"] == "demo.md"
    assert Path(manifest["fetch_link"]).name == result.event.event_path.name
    assert Path(manifest["canonical_path"]).name == "data"


def test_materialize_bytes_records_projection_degradation_in_lead_cache(
    tmp_path: Path,
) -> None:
    dirs = make_dirs(tmp_path / "ws")

    result = materialize_bytes(
        b"<p>Depends on ABC-2.</p>",
        dirs=dirs,
        preferred_name="demo.html",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get ABC-1",
            "canonical_locator": "jira:ABC-1",
            "content_type": "text/html",
            "projector": "missing.projector",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    payload = json.loads(
        (result.layout.artifact_dir / "leads.json").read_text(encoding="utf-8")
    )

    assert payload["degradations"] == [
        "stored projector `missing.projector` is unavailable; using canonical projection"
    ]
    assert {entry["canonical_locator"] for entry in payload["entries"]} == {
        "jira:ABC-2"
    }


def test_private_state_and_content_paths_use_private_modes(tmp_path: Path) -> None:
    if os.name == "nt":
        return

    dirs = initialize_session(tmp_path / "ws")
    content_activity.append_activity_event(
        dirs.session_dir,
        {
            "plugin": "logs",
            "surface": "logs",
            "action": "append",
            "locator": "logs:session",
            "preferred_name": "logs:session",
            "follow_command": "gotta logs",
            "detail": "appended",
            "time_field": "session_recorded_at",
        },
    )
    result = materialize_bytes(
        b"hello world",
        dirs=dirs,
        preferred_name="demo.md",
        metadata={"tool": "gotta", "plugin": "read", "locator": "demo"},
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    file_paths = (
        content_env.state_env_path(dirs.session_dir),
        content_activity.activity_log_path(dirs.session_dir),
        dirs.content_dir / "manifest.jsonl",
        result.layout.blob_path,
        result.layout.metadata_path,
        result.layout.artifact_dir / "leads.json",
    )
    dir_paths = (
        content_env.state_dir_path(dirs.session_dir),
        result.layout.artifact_dir,
        result.layout.alias_dir,
        result.layout.event_dir,
    )

    assert all(
        stat.S_IMODE(path.stat().st_mode) == content_file.PRIVATE_FILE_MODE
        for path in file_paths
    )
    assert all(
        stat.S_IMODE(path.stat().st_mode) == content_file.PRIVATE_DIR_MODE
        for path in dir_paths
    )


def test_scan_content_store_reads_directory_layout(tmp_path: Path) -> None:
    dirs = make_dirs(tmp_path / "ws")
    result = materialize_bytes(
        b"hello world",
        dirs=dirs,
        preferred_name="demo.md",
        metadata={"tool": "gotta", "plugin": "read", "locator": "demo"},
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    snapshots = scan_content_store(dirs.content_dir)

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.digest == result.digest
    assert snapshot.layout.artifact_dir == result.layout.artifact_dir
    assert [alias.name for alias in snapshot.aliases] == ["demo.md"]
    assert [event.timestamp for event in snapshot.events] == [
        "2026-03-11T00:00:00.000001Z"
    ]


def test_scan_content_store_handles_missing_names_directory(tmp_path: Path) -> None:
    dirs = make_dirs(tmp_path / "ws")
    digest = "a" * 64
    content_dir = dirs.content_dir / digest
    content_dir.mkdir(parents=True, exist_ok=True)
    (content_dir / "data").write_bytes(b"hello world")

    snapshots = scan_content_store(dirs.content_dir)

    assert len(snapshots) == 1
    assert snapshots[0].digest == digest
    assert snapshots[0].aliases == []


def test_activity_events_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    initialize_session(root)

    content_activity.append_activity_event(
        root,
        {
            "timestamp": "2026-03-14T10:00:00Z",
            "plugin": "logs",
            "surface": "logs",
            "action": "append",
            "locator": "logs:session",
            "preferred_name": "logs:session",
            "follow_command": "gotta logs",
            "detail": "appended 1 logs entry",
            "time_field": "session_recorded_at",
        },
    )

    records = content_activity.activity_events(root)

    assert records == [
        {
            "action": "append",
            "actor": "ws",
            "detail": "appended 1 logs entry",
            "follow_command": "gotta logs",
            "locator": "logs:session",
            "plugin": "logs",
            "preferred_name": "logs:session",
            "surface": "logs",
            "time_field": "session_recorded_at",
            "timestamp": "2026-03-14T10:00:00Z",
        }
    ]
    assert content_activity.activity_log_path(root).is_file()


def test_resolve_dirs_prefers_local_state_over_default_root(
    tmp_path: Path, monkeypatch
) -> None:
    default_root = tmp_path / "default"
    local_root = tmp_path / "local"
    initialize_session(default_root)
    initialize_session(local_root)

    monkeypatch.setattr(content_scope, "DEFAULT_SESSION_ROOT", default_root)
    monkeypatch.chdir(local_root)

    resolved = content_scope.resolve_dirs(content_model.CommonOptions(), create=False)
    assert resolved.session_dir == local_root.resolve()
    assert resolved.content_dir == (local_root / "content").resolve()


def test_resolve_dirs_falls_back_to_context_bound_session(
    tmp_path: Path, monkeypatch
) -> None:
    default_root = tmp_path / "default"
    monkeypatch.setattr(content_scope, "DEFAULT_SESSION_ROOT", default_root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    fingerprint = content_context.session_token("thread-123")
    bound_root = default_root / fingerprint / "actors" / fingerprint
    dirs = content_model.ResolvedDirs(
        session_dir=bound_root,
        content_dir=default_root / fingerprint / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content_env.write_state_env(dirs)
    topology.write_binding(
        fingerprint,
        bound_root,
        context_id="thread-123",
        context_source="codex_thread",
        session_id=fingerprint,
        actor=fingerprint,
        created_at="2026-03-22T00:00:00Z",
        updated_at="2026-03-22T00:00:00Z",
    )

    resolved = content_scope.resolve_dirs(content_model.CommonOptions(), create=False)
    assert resolved.session_dir == bound_root.resolve()
    assert resolved.content_dir == (default_root / fingerprint / "content").resolve()


def test_resolve_dirs_treats_env_session_root_as_explicit(
    tmp_path: Path, monkeypatch
) -> None:
    exact_root = tmp_path / "exact"
    unrelated_content = tmp_path / "unrelated-content"
    initialize_session(exact_root)
    unrelated_content.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv(content_env.SESSION_ENV, str(exact_root))
    monkeypatch.setenv(content_env.CONTENT_ENV, str(unrelated_content))

    resolved = content_scope.resolve_dirs(content_model.CommonOptions(), create=False)

    assert resolved.session_dir == exact_root.resolve()
    assert resolved.content_dir == (exact_root / "content").resolve()


def test_current_context_binding_uses_term_session_as_first_class_identity(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv(content_env.CONTEXT_ID_ENV, raising=False)
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
    left = content_context.current_context_binding()
    monkeypatch.chdir(second)
    right = content_context.current_context_binding()

    assert left.context_source == "terminal_session"
    assert right.context_source == "terminal_session"
    assert left.context_id == "term-session-1"
    assert right.context_id == "term-session-1"
    assert left.binding_id == right.binding_id


def test_current_context_binding_prefers_codex_thread_over_term_session(
    monkeypatch,
) -> None:
    monkeypatch.delenv(content_env.CONTEXT_ID_ENV, raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    monkeypatch.setenv("TERM_SESSION_ID", "term-session-1")

    binding = content_context.current_context_binding()

    assert binding.context_source == "codex_thread"
    assert binding.context_id == "thread-123"


def test_current_context_binding_uses_sandbox_boot_id_when_present(
    tmp_path: Path, monkeypatch
) -> None:
    boot_id_path = tmp_path / "boot_id"
    boot_id_path.write_text("00000000-0000-4000-8000-000000000001\n", encoding="utf-8")
    monkeypatch.delenv(content_env.CONTEXT_ID_ENV, raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("TERM_SESSION_ID", raising=False)
    monkeypatch.setenv("IS_SANDBOX", "yes")
    monkeypatch.setattr(content_context, "_SANDBOX_BOOT_ID_PATH", boot_id_path)

    binding = content_context.current_context_binding()

    assert binding.context_source == "sandbox_boot_id"
    assert binding.context_id == "00000000-0000-4000-8000-000000000001"


def test_current_context_binding_prefers_term_session_over_sandbox_boot_id(
    tmp_path: Path, monkeypatch
) -> None:
    boot_id_path = tmp_path / "boot_id"
    boot_id_path.write_text("00000000-0000-4000-8000-000000000001\n", encoding="utf-8")
    monkeypatch.delenv(content_env.CONTEXT_ID_ENV, raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setenv("TERM_SESSION_ID", "term-session-1")
    monkeypatch.setenv("IS_SANDBOX", "yes")
    monkeypatch.setattr(content_context, "_SANDBOX_BOOT_ID_PATH", boot_id_path)

    binding = content_context.current_context_binding()

    assert binding.context_source == "terminal_session"
    assert binding.context_id == "term-session-1"


def test_current_context_binding_uses_cwd_only_as_last_resort_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv(content_env.CONTEXT_ID_ENV, raising=False)
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
    left = content_context.current_context_binding()
    monkeypatch.chdir(second)
    right = content_context.current_context_binding()

    assert left.context_source == "terminal_fingerprint"
    assert right.context_source == "terminal_fingerprint"
    assert left.context_id != right.context_id


def test_session_is_initialized_depends_on_state_env_only(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    dirs = make_dirs(root)

    assert content_scope.session_is_initialized(root) is False

    content_env.write_state_env(dirs)
    assert content_scope.session_is_initialized(root) is True


def test_session_surface_initialized_requires_authored_charters(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    initialize_session(root)

    assert content_scope.session_surface_initialized(root) is False

    (root / "WANT.md").write_text("", encoding="utf-8")
    assert content_scope.session_surface_initialized(root) is False

    (root / "GOAL.md").write_text("", encoding="utf-8")
    assert content_scope.session_surface_initialized(root) is True


def test_stdin_has_readable_text_rejects_empty_stringio(monkeypatch) -> None:
    monkeypatch.setattr(content_context.sys, "stdin", io.StringIO(""))
    assert content_context.stdin_has_readable_text() is False


def test_stdin_has_readable_text_accepts_populated_stringio(monkeypatch) -> None:
    monkeypatch.setattr(content_context.sys, "stdin", io.StringIO("payload\n"))
    assert content_context.stdin_has_readable_text() is True
